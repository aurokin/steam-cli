"""Privacy-safe preparation and import workflow for qualitative review.

The runner deliberately does not invoke a model judge.  It prepares blinded
cases, wraps externally produced verdicts in the existing hash-bound judgment
contract, and derives agreement adjudications mechanically.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from evals.runner import codex_driver, inspection, judge, matrix, run_state


_CASE_SCHEMA = "steam-agent-eval-review-case/0.2"
_VERDICTS_SCHEMA = "steam-agent-eval-review-verdicts/0.2"
_LEDGER_SCHEMA = "steam-agent-eval-review-ledger/0.2"
_OPERATION_SCHEMA = "steam-agent-eval-review-operation/0.3"
_LEGACY_CASE_SCHEMA = "steam-agent-eval-review-case/0.1"
_LEGACY_VERDICTS_SCHEMA = "steam-agent-eval-review-verdicts/0.1"
_LEGACY_LEDGER_SCHEMA = "steam-agent-eval-review-ledger/0.1"
_STRUCTURED_OUTPUT_VALIDATOR = "codex-structured-output-subset/0.1"
_CANARY_CASE_SCHEMA = "steam-agent-eval-review-canary-case/0.1"
_CANARY_ATTESTATION_SCHEMA = "steam-agent-eval-review-canary-attestation/0.1"
_INCIDENT_SCHEMA = "steam-agent-eval-review-incident/0.1"
_SUPERSESSION_SCHEMA = "steam-agent-eval-review-supersession/0.1"
_REGISTRY_SCHEMA = "steam-agent-eval-review-package-registry/0.1"
_MEASUREMENT_AMENDMENT_SCHEMA = (
    "steam-agent-eval-review-measurement-amendment/0.1"
)
_UNAVAILABLE_OPERATION_SCHEMA = (
    "steam-agent-eval-review-unavailable-duration-operation/0.1"
)
_EVENT_VALIDATOR = "codex-jsonl-verdict-binding/0.1"
_MAX_CASES = 1024
_MAX_ATTEMPTS = 3
_MAX_DURATION_MS = 24 * 60 * 60 * 1000
_HOST_ISOLATION_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "multi_agent",
    "apps",
    "plugins",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "image_generation",
    "in_app_browser",
    "goals",
    "skill_search",
    "workspace_dependencies",
    "tool_suggest",
    "current_time_reminder",
    "hooks",
)
_ISOLATION_ATTESTATION = "codex-0.146-no-shell-host-isolated-profile-v1"
_CANARY_OPERATOR_ATTESTATION = (
    "operator-attested-codex-0.146-gpt-5.6-sol-xhigh-"
    "no-shell-host-isolated-profile-v1"
)
_CANARY_MODEL = "gpt-5.6-sol"
_CANARY_REASONING_EFFORT = "xhigh"
_SUPERSESSION_FILENAME = "supersession.json"
_REGISTRY_FILENAME = "review-package.json"
_MEASUREMENT_AMENDMENT_FILENAME = "review-measurement-amendment.json"
_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_SAFE_WORK_ITEM = re.compile(r"w-[0-9]{6}-[0-9a-f]{16}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PACKAGE_ID = re.compile(r"package-[0-9a-f]{64}\Z", re.ASCII)
_INCIDENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", re.ASCII)
_CANARY_FAILURE_CLASSES = frozenset(
    {"provider_rejection", "structural_failure", "transport_failure"}
)
_MEASUREMENT_AMENDMENT_CLASSES = frozenset(
    {
        "interrupted_attempt_duration_unavailable",
        "recorded_duration_unreliable",
    }
)
_AMENDMENT_UNSET = object()
_CODEX_ITEM_EVENTS = frozenset(
    {"item.started", "item.updated", "item.completed"}
)
_ALLOWED_JUDGE_ITEM_TYPES = frozenset({"agent_message", "reasoning"})
_NATIVE_EXECUTABLE_MAGICS = frozenset(
    {
        b"\x7fELF",
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)


class ReviewError(RuntimeError):
    """A review package or operation is unsafe, stale, or inconsistent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return matrix._canonical_json_bytes(value)  # noqa: SLF001


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        run_state._parse_time(value)  # noqa: SLF001
    except run_state.ManifestStateError:
        return False
    return True


def _node_exists(path: Path) -> bool:
    """Return whether a node exists without following a dangling symlink."""

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise ReviewError("qualitative review filesystem node is unavailable") from None
    return True


def _source_revision(result: inspection.MatrixInspection) -> Any:
    manifest_inputs = getattr(result.manifest, "inputs", None)
    return getattr(
        manifest_inputs,
        "commit",
        getattr(result.manifest, "revision", None),
    )


def _destination_path_sha256(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ReviewError("qualitative review destination is unavailable") from None
    return hashlib.sha256(os.fsencode(resolved)).hexdigest()


def _registry_document(
    *,
    result: inspection.MatrixInspection,
    review_dir: Path,
    package_id: str,
    supersession: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema": _REGISTRY_SCHEMA,
        "matrix_id": result.manifest.matrix_id,
        "manifest_sha256": result.manifest_sha256,
        "package_id": package_id,
        "destination_path_sha256": _destination_path_sha256(review_dir),
        "supersession": supersession,
        "recorded_at": _now(),
    }


def _validate_registry(
    matrix_dir: Path,
    review_dir: Path,
    result: inspection.MatrixInspection,
    *,
    package_id: str,
    supersession: dict[str, Any] | None = None,
    compare_supersession: bool = False,
) -> dict[str, Any]:
    document = _read_json(
        matrix_dir / _REGISTRY_FILENAME,
        require_private=True,
        require_canonical=True,
    )
    retained_supersession = document.get("supersession")
    if retained_supersession is not None and (
        not isinstance(retained_supersession, dict)
        or set(retained_supersession) != {"tombstone_sha256", "recorded_at"}
        or not isinstance(retained_supersession.get("tombstone_sha256"), str)
        or _SHA256.fullmatch(retained_supersession["tombstone_sha256"]) is None
        or not _valid_timestamp(retained_supersession.get("recorded_at"))
    ):
        raise ReviewError("qualitative review package registry is invalid")
    if (
        set(document)
        != {
            "schema",
            "matrix_id",
            "manifest_sha256",
            "package_id",
            "destination_path_sha256",
            "supersession",
            "recorded_at",
        }
        or document.get("schema") != _REGISTRY_SCHEMA
        or document.get("matrix_id") != result.manifest.matrix_id
        or document.get("manifest_sha256") != result.manifest_sha256
        or document.get("package_id") != package_id
        or document.get("destination_path_sha256")
        != _destination_path_sha256(review_dir)
        or not _valid_timestamp(document.get("recorded_at"))
        or (compare_supersession and retained_supersession != supersession)
    ):
        raise ReviewError("qualitative review package registry is invalid")
    return document


def _reserve_registry(
    matrix_dir: Path,
    review_dir: Path,
    result: inspection.MatrixInspection,
    *,
    package_id: str,
    supersession: dict[str, Any] | None,
) -> dict[str, Any]:
    path = matrix_dir / _REGISTRY_FILENAME
    if not _node_exists(path):
        _write_json(
            path,
            _registry_document(
                result=result,
                review_dir=review_dir,
                package_id=package_id,
                supersession=supersession,
            ),
        )
    return _validate_registry(
        matrix_dir,
        review_dir,
        result,
        package_id=package_id,
        supersession=supersession,
        compare_supersession=True,
    )


def _value_has_schema_type(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(
            value, float
        )
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    return False


def _validate_structured_output_schema(document: dict[str, Any]) -> None:
    """Validate the pinned provider-compatible JSON Schema subset."""

    try:
        Draft202012Validator.check_schema(document)
    except (SchemaError, TypeError, ValueError):
        raise ReviewError("qualitative review response schema is invalid") from None

    metadata = {"$schema", "$id", "title", "description"}
    common = {"type", "const", "enum", "description"}
    by_type = {
        "object": {"properties", "required", "additionalProperties"},
        "array": {"items", "minItems", "maxItems"},
        "string": {"pattern", "minLength", "maxLength"},
        "integer": {"minimum", "maximum"},
        "number": {"minimum", "maximum"},
        "boolean": set(),
        "null": set(),
    }

    def reject() -> None:
        raise ReviewError("qualitative review response schema is unsupported")

    def visit(schema: Any, *, root: bool = False) -> None:
        if not isinstance(schema, dict):
            reject()
        type_name = schema.get("type")
        if not isinstance(type_name, str) or type_name not in by_type:
            reject()
        allowed = common | by_type[type_name] | (metadata if root else set())
        if set(schema) - allowed:
            reject()
        if "const" in schema and not _value_has_schema_type(
            schema["const"], type_name
        ):
            reject()
        if "enum" in schema:
            enum = schema["enum"]
            if (
                not isinstance(enum, list)
                or not enum
                or any(not _value_has_schema_type(item, type_name) for item in enum)
            ):
                reject()
        if type_name == "object":
            properties = schema.get("properties")
            required = schema.get("required")
            if (
                not isinstance(properties, dict)
                or not properties
                or schema.get("additionalProperties") is not False
                or not isinstance(required, list)
                or any(not isinstance(item, str) for item in required)
                or len(required) != len(set(required))
                or set(required) != set(properties)
                or any(not isinstance(name, str) or not name for name in properties)
            ):
                reject()
            for child in properties.values():
                visit(child)
        elif type_name == "array":
            if "items" not in schema:
                reject()
            visit(schema["items"])
            for key in ("minItems", "maxItems"):
                value = schema.get(key)
                if value is not None and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                ):
                    reject()
            if schema.get("minItems", 0) > schema.get("maxItems", sys.maxsize):
                reject()
        elif type_name == "string":
            if "pattern" in schema and not isinstance(schema["pattern"], str):
                reject()
            for key in ("minLength", "maxLength"):
                value = schema.get(key)
                if value is not None and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                ):
                    reject()
            if schema.get("minLength", 0) > schema.get("maxLength", sys.maxsize):
                reject()
        elif type_name in {"integer", "number"}:
            for key in ("minimum", "maximum"):
                value = schema.get(key)
                if value is not None and not _value_has_schema_type(value, type_name):
                    reject()
            if schema.get("minimum", float("-inf")) > schema.get(
                "maximum", float("inf")
            ):
                reject()

    visit(document, root=True)


def _read_bytes(path: Path, *, require_private: bool = False) -> bytes:
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        item_stat = os.fstat(descriptor)
        if not stat.S_ISREG(item_stat.st_mode) or (
            require_private and stat.S_IMODE(item_stat.st_mode) != 0o600
        ):
            raise ReviewError("qualitative review input is not a regular file")
        if item_stat.st_size > _MAX_DOCUMENT_BYTES:
            raise ReviewError("qualitative review input is invalid")
        content = bytearray()
        while len(content) <= _MAX_DOCUMENT_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_DOCUMENT_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_DOCUMENT_BYTES:
            raise ReviewError("qualitative review input is invalid")
        return bytes(content)
    except OSError:
        raise ReviewError("qualitative review input is invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _decode_json_document(
    content: bytes,
    *,
    schema_name: str | None = None,
    require_canonical: bool = False,
) -> dict[str, Any]:
    try:
        document = matrix._strict_json_loads(  # noqa: SLF001
            content.decode("utf-8")
        )
    except (OSError, UnicodeError, ValueError, matrix.MatrixError):
        raise ReviewError("qualitative review input is invalid") from None
    if not isinstance(document, dict):
        raise ReviewError("qualitative review input is invalid")
    if require_canonical and content != _canonical_bytes(document):
        raise ReviewError("qualitative review input is not canonical")
    if schema_name is not None:
        try:
            return judge._validate_schema(document, schema_name)  # noqa: SLF001
        except judge.JudgmentError as error:
            raise ReviewError(str(error)) from None
    return document


def _read_json_with_content(
    path: Path,
    *,
    schema_name: str | None = None,
    require_private: bool = False,
    require_canonical: bool = False,
) -> tuple[dict[str, Any], bytes]:
    content = _read_bytes(path, require_private=require_private)
    return (
        _decode_json_document(
            content,
            schema_name=schema_name,
            require_canonical=require_canonical,
        ),
        content,
    )


def _read_json(
    path: Path,
    *,
    schema_name: str | None = None,
    require_private: bool = False,
    require_canonical: bool = False,
) -> dict[str, Any]:
    return _read_json_with_content(
        path,
        schema_name=schema_name,
        require_private=require_private,
        require_canonical=require_canonical,
    )[0]


def _inspect_event_log(path: Path) -> tuple[dict[str, int], str, str]:
    """Return bounded evidence from one successful, tool-free Codex invocation."""

    try:
        content = _read_bytes(Path(path), require_private=True)
        text = content.decode("utf-8")
        lines = text.splitlines()
    except UnicodeError:
        raise ReviewError("qualitative review event log is invalid") from None
    if not lines or any(not line.strip() for line in lines):
        raise ReviewError("qualitative review event log is invalid")
    if len(lines) < 4:
        raise ReviewError("qualitative review event log is invalid")
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = matrix._strict_json_loads(line)  # noqa: SLF001
        except (ValueError, matrix.MatrixError):
            raise ReviewError("qualitative review event log is invalid") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ReviewError("qualitative review event log is invalid")
        events.append(event)
    if (
        events[0]["type"] != "thread.started"
        or events[1]["type"] != "turn.started"
        or events[-1]["type"] != "turn.completed"
    ):
        raise ReviewError("qualitative review event log is invalid")
    agent_messages = 0
    agent_message = ""
    item_states: dict[str, tuple[str, str]] = {}
    for event in events[2:-1]:
        event_type = event["type"]
        if event_type not in _CODEX_ITEM_EVENTS:
            raise ReviewError("qualitative review event log is invalid")
        item = event.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        item_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(item_id, str) or not item_id:
            raise ReviewError("qualitative review event log is invalid")
        if item_type not in _ALLOWED_JUDGE_ITEM_TYPES:
            raise ReviewError("qualitative review event log contains tool use")
        retained = item_states.get(item_id)
        if event_type == "item.started":
            if retained is not None:
                raise ReviewError("qualitative review event log is invalid")
            item_states[item_id] = (item_type, "active")
        elif event_type == "item.updated":
            if retained != (item_type, "active"):
                raise ReviewError("qualitative review event log is invalid")
        else:
            if retained not in {None, (item_type, "active")}:
                raise ReviewError("qualitative review event log is invalid")
            item_states[item_id] = (item_type, "completed")
            if item_type == "agent_message":
                message = item.get("text")
                if not isinstance(message, str):
                    raise ReviewError("qualitative review event log is invalid")
                agent_messages += 1
                agent_message = message
    if (
        agent_messages != 1
        or not item_states
        or any(state != "completed" for _item_type, state in item_states.values())
    ):
        raise ReviewError("qualitative review event log is invalid")
    return (
        {"events": len(lines), "agent_messages": agent_messages},
        agent_message,
        hashlib.sha256(content).hexdigest(),
    )


def check_event_log(path: Path) -> dict[str, int]:
    """Reject malformed, failed, or tool-using Codex JSONL judge events."""

    return _inspect_event_log(path)[0]


def _bound_event_log(path: Path, verdict_content: bytes) -> tuple[int, str]:
    summary, agent_message, digest = _inspect_event_log(path)
    try:
        message_content = agent_message.encode("utf-8")
    except UnicodeError:
        raise ReviewError("qualitative review event log is invalid") from None
    if message_content != verdict_content:
        raise ReviewError("qualitative review event log does not match verdict")
    return summary["events"], digest


def preflight_native_codex(path: Path) -> dict[str, str]:
    """Require a standalone native Codex 0.146 executable, never a JS shim."""

    descriptor = -1
    try:
        supplied = Path(path)
        resolved = supplied.parent.resolve(strict=True) / supplied.name
        supplied_stat = resolved.lstat()
        if not stat.S_ISREG(supplied_stat.st_mode):
            raise ReviewError("qualitative judge native Codex 0.146 is unavailable")
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags)
        item_stat = os.fstat(descriptor)
        magic = os.read(descriptor, 4)
        if (
            not stat.S_ISREG(item_stat.st_mode)
            or item_stat.st_dev != supplied_stat.st_dev
            or item_stat.st_ino != supplied_stat.st_ino
            or not os.access(resolved, os.X_OK)
            or magic not in _NATIVE_EXECUTABLE_MAGICS
        ):
            raise ReviewError("qualitative judge native Codex 0.146 is unavailable")
        result = subprocess.run(
            [str(resolved), "--version"],
            cwd="/",
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            capture_output=True,
            check=False,
            timeout=10,
        )
        retained_stat = resolved.stat(follow_symlinks=False)
        if (
            retained_stat.st_dev != item_stat.st_dev
            or retained_stat.st_ino != item_stat.st_ino
        ):
            raise ReviewError("qualitative judge native Codex 0.146 is unavailable")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        raise ReviewError(
            "qualitative judge native Codex 0.146 is unavailable"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        result.returncode != 0
        or result.stdout.strip()
        != codex_driver._REQUIRED_CODEX_VERSION.encode("ascii")  # noqa: SLF001
    ):
        raise ReviewError("qualitative judge native Codex 0.146 is unavailable")
    return {
        "payload": "native",
        "version": codex_driver._REQUIRED_CODEX_VERSION,  # noqa: SLF001
    }


def _write_json(path: Path, value: Any) -> None:
    try:
        run_state.atomic_publish_private_bytes(path, _canonical_bytes(value))
    except (OSError, FileExistsError, run_state.ManifestStateError):
        raise ReviewError("qualitative review artifact is unavailable") from None


def _private_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    except OSError:
        raise ReviewError("qualitative review directory is unavailable") from None


def _filesystem_identity(path: Path) -> tuple[int, int] | None:
    try:
        item_stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ReviewError("qualitative review roots are unavailable") from None
    return item_stat.st_dev, item_stat.st_ino


def _separated_review_roots(
    matrix_dir: Path, review_dir: Path
) -> tuple[Path, Path]:
    try:
        matrix_root = Path(matrix_dir).resolve()
        review_root = Path(review_dir).resolve()
    except (OSError, RuntimeError):
        raise ReviewError("qualitative review roots are unavailable") from None
    matrix_identity = _filesystem_identity(matrix_root)
    if matrix_identity is None:
        raise ReviewError("qualitative review roots are unavailable")
    if review_root.is_relative_to(matrix_root):
        raise ReviewError("qualitative review directory must be outside matrix")
    for candidate in (review_root, *review_root.parents):
        if _filesystem_identity(candidate) == matrix_identity:
            raise ReviewError("qualitative review directory must be outside matrix")
    return matrix_root, review_root


def _asset_text(matrix_dir: Path, name: str, expected_sha256: str) -> str:
    path = matrix_dir / "calibration" / name
    try:
        content = _read_bytes(path)
    except ReviewError:
        raise ReviewError(
            "qualitative review calibration asset is unavailable"
        ) from None
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ReviewError("qualitative review calibration asset does not match")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise ReviewError("qualitative review calibration asset is invalid") from None


def _asset_json(matrix_dir: Path, name: str, expected_sha256: str) -> dict[str, Any]:
    text = _asset_text(matrix_dir, name, expected_sha256)
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        raise ReviewError("qualitative review calibration asset is invalid") from None
    if not isinstance(value, dict):
        raise ReviewError("qualitative review calibration asset is invalid")
    return value


def _target_for(
    target_index: judge._TargetIndex,  # noqa: SLF001
    work_item_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    observation = target_index.observations.get(work_item_id)
    if observation is None:
        raise ReviewError("qualitative review target is unavailable")
    scenario = target_index.scenarios.get(observation.work_item.scenario_id)
    if scenario is None:
        raise ReviewError("qualitative review scenario is unavailable")
    try:
        report_sha256 = dict(observation.completion.artifact_hashes)["report.json"]
        projection = judge._qualitative_projection(  # noqa: SLF001
            observation,
            scenario,
            campaign=target_index.inspection_result.manifest.campaign,
        )
    except (KeyError, judge.JudgmentError) as error:
        raise ReviewError(str(error)) from None
    target = {
        "matrix_id": target_index.inspection_result.manifest.matrix_id,
        "work_item_id": work_item_id,
        "report_sha256": report_sha256,
        "scenario_sha256": scenario.source_sha256,
        "rubric_sha256": scenario.rubric_sha256,
        "projection_sha256": _sha256(projection),
    }
    try:
        judge._target_observation(target_index, target)  # noqa: SLF001
    except judge.JudgmentError as error:
        raise ReviewError(str(error)) from None
    return target, projection


def _invocation_binding(
    target: dict[str, Any], judge_identifier: str, package_id: str
) -> dict[str, str]:
    return {
        "package_id": package_id,
        "judge_identifier": judge_identifier,
        "binding_sha256": _sha256(
            {
                "schema": "steam-agent-eval-review-invocation/0.2",
                "package_id": package_id,
                "matrix_id": target["matrix_id"],
                "work_item_id": target["work_item_id"],
                "projection_sha256": target["projection_sha256"],
                "judge_identifier": judge_identifier,
            }
        ),
    }


def _case_document(
    matrix_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    work_item_id: str,
    judge_identifier: str,
    package_id: str,
) -> dict[str, Any]:
    campaign = target_index.inspection_result.manifest.campaign
    target, projection = _target_for(target_index, work_item_id)
    response_schema = _read_json(judge.SCHEMA_ROOT / "review-verdicts-0.2.json")
    return {
        "schema": _CASE_SCHEMA,
        "package_id": package_id,
        "execution": {
            "model_input": "this_document_verbatim",
            "criterion_coverage": "every_projection_criterion_exactly_once",
            "external_context": "forbidden",
            "response_schema": {
                "schema": _VERDICTS_SCHEMA,
                "sha256": _sha256(response_schema),
                "validator": _STRUCTURED_OUTPUT_VALIDATOR,
            },
            "invocation": _invocation_binding(
                target, judge_identifier, package_id
            ),
        },
        "target": target,
        "prompt": {
            "version": campaign.prompt_version,
            "sha256": campaign.prompt_sha256,
            "text": _asset_text(
                matrix_dir,
                "matrix-judge-prompt-0.1.md",
                campaign.prompt_sha256,
            ),
        },
        "parser": {
            "version": campaign.parser_version,
            "sha256": campaign.parser_sha256,
            "document": _asset_json(
                matrix_dir,
                "matrix-parser-0.1.json",
                campaign.parser_sha256,
            ),
        },
        "presentation": {"blinded_label": "candidate-A", "order": 0},
        "projection": projection,
    }


def _legacy_invocation_binding(
    target: dict[str, Any], judge_identifier: str
) -> dict[str, str]:
    return {
        "judge_identifier": judge_identifier,
        "binding_sha256": _sha256(
            {
                "schema": "steam-agent-eval-review-invocation/0.1",
                "matrix_id": target["matrix_id"],
                "work_item_id": target["work_item_id"],
                "projection_sha256": target["projection_sha256"],
                "judge_identifier": judge_identifier,
            }
        ),
    }


def _legacy_case_document(
    matrix_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    work_item_id: str,
    judge_identifier: str,
) -> dict[str, Any]:
    campaign = target_index.inspection_result.manifest.campaign
    target, projection = _target_for(target_index, work_item_id)
    response_schema = _read_json(judge.SCHEMA_ROOT / "review-verdicts-0.1.json")
    return {
        "schema": _LEGACY_CASE_SCHEMA,
        "execution": {
            "model_input": "this_document_verbatim",
            "criterion_coverage": "every_projection_criterion_exactly_once",
            "external_context": "forbidden",
            "response_schema": {
                "schema": _LEGACY_VERDICTS_SCHEMA,
                "sha256": _sha256(response_schema),
            },
            "invocation": _legacy_invocation_binding(target, judge_identifier),
        },
        "target": target,
        "prompt": {
            "version": campaign.prompt_version,
            "sha256": campaign.prompt_sha256,
            "text": _asset_text(
                matrix_dir,
                "matrix-judge-prompt-0.1.md",
                campaign.prompt_sha256,
            ),
        },
        "parser": {
            "version": campaign.parser_version,
            "sha256": campaign.parser_sha256,
            "document": _asset_json(
                matrix_dir,
                "matrix-parser-0.1.json",
                campaign.parser_sha256,
            ),
        },
        "presentation": {"blinded_label": "candidate-A", "order": 0},
        "projection": projection,
    }


def _preliminary_case_document(
    matrix_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    work_item_id: str,
    judge_identifier: str,
) -> dict[str, Any]:
    placeholder = "package-" + "0" * 64
    document = _decode_json_document(
        _canonical_bytes(
            _case_document(
                matrix_dir,
                target_index,
                work_item_id,
                judge_identifier,
                placeholder,
            )
        )
    )
    document.pop("package_id")
    invocation = document["execution"]["invocation"]
    invocation.pop("package_id")
    invocation.pop("binding_sha256")
    return document


def _package_id(
    result: inspection.MatrixInspection,
    response_schema: dict[str, Any],
    preliminary_cases: list[dict[str, Any]],
    supersedes: dict[str, Any] | None,
) -> str:
    identity = {
        "schema": "steam-agent-eval-review-package-identity/0.1",
        "matrix_id": result.manifest.matrix_id,
        "manifest_sha256": result.manifest_sha256,
        "protocol": {
            "case_schema": _CASE_SCHEMA,
            "ledger_schema": _LEDGER_SCHEMA,
            "operation_schema": _OPERATION_SCHEMA,
            "registry_schema": _REGISTRY_SCHEMA,
            "canary_case_schema": _CANARY_CASE_SCHEMA,
            "canary_attestation_schema": _CANARY_ATTESTATION_SCHEMA,
            "codex_version": codex_driver._REQUIRED_CODEX_VERSION,  # noqa: SLF001
            "model": _CANARY_MODEL,
            "reasoning_effort": _CANARY_REASONING_EFFORT,
            "isolation_attestation": _ISOLATION_ATTESTATION,
            "operator_invocation_attestation": _CANARY_OPERATOR_ATTESTATION,
        },
        "response_schema": {
            "schema": _VERDICTS_SCHEMA,
            "sha256": _sha256(response_schema),
            "validator": _STRUCTURED_OUTPUT_VALIDATOR,
        },
        "cases": preliminary_cases,
        "supersedes": supersedes,
    }
    return f"package-{_sha256(identity)}"


def _canary_case(
    *, matrix_id: str, package_id: str, response_schema_sha256: str
) -> dict[str, Any]:
    identity = package_id.removeprefix("package-")
    work_item_id = f"w-999999-{identity[:16]}"
    projection = {
        "schema": "steam-agent-eval-qualitative-projection/0.2",
        "criteria": [
            {
                "id": "schema-compatible",
                "source": "judged_answer_rubric",
                "requirement": "Return pass for this structured-output canary.",
                "evidence_path": None,
                "screen_safety_gate": False,
            }
        ],
        "answers": [{"turn": 0, "text": "Structured-output canary."}],
        "claims_sidecars": [
            {"turn": 0, "claims": [], "declined": False}
        ],
    }
    prompt_text = (
        "This is a transport canary with no candidate evidence. Echo the target and "
        "invocation exactly, and return one pass verdict for schema-compatible with "
        "a short rationale."
    )
    parser_document = {
        "criterion_id": "schema-compatible",
        "allowed_verdict": "pass",
    }
    target = {
        "matrix_id": matrix_id,
        "work_item_id": work_item_id,
        "report_sha256": _sha256({"canary": package_id, "kind": "report"}),
        "scenario_sha256": _sha256({"canary": package_id, "kind": "scenario"}),
        "rubric_sha256": _sha256({"canary": package_id, "kind": "rubric"}),
        "projection_sha256": _sha256(projection),
    }
    return {
        "schema": _CASE_SCHEMA,
        "package_id": package_id,
        "execution": {
            "model_input": "this_document_verbatim",
            "criterion_coverage": "every_projection_criterion_exactly_once",
            "external_context": "forbidden",
            "response_schema": {
                "schema": _VERDICTS_SCHEMA,
                "sha256": response_schema_sha256,
                "validator": _STRUCTURED_OUTPUT_VALIDATOR,
            },
            "invocation": _invocation_binding(target, "canary", package_id),
        },
        "target": target,
        "prompt": {
            "version": "matrix-canary/0.1",
            "sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            "text": prompt_text,
        },
        "parser": {
            "version": "matrix-canary-parser/0.1",
            "sha256": _sha256(parser_document),
            "document": parser_document,
        },
        "presentation": {"blinded_label": "candidate-A", "order": 0},
        "projection": projection,
    }


def _raw_sha256(path: Path, *, require_private: bool = True) -> str:
    return hashlib.sha256(
        _read_bytes(path, require_private=require_private)
    ).hexdigest()


def _review_tree_sha256(review_dir: Path) -> str:
    entries: list[dict[str, Any]] = []
    for path in sorted(review_dir.rglob("*"), key=lambda item: item.as_posix()):
        try:
            item_stat = path.lstat()
            relative = path.relative_to(review_dir).as_posix()
        except (OSError, ValueError):
            raise ReviewError("qualitative review superseded tree is invalid") from None
        if relative in {"matrix.lock", _SUPERSESSION_FILENAME}:
            continue
        mode = stat.S_IMODE(item_stat.st_mode)
        if stat.S_ISDIR(item_stat.st_mode):
            if mode != 0o700:
                raise ReviewError("qualitative review superseded tree is not private")
            entries.append({"path": relative, "kind": "directory", "mode": mode})
        elif stat.S_ISREG(item_stat.st_mode):
            if mode != 0o600:
                raise ReviewError("qualitative review superseded tree is not private")
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "sha256": _raw_sha256(path),
                }
            )
        else:
            raise ReviewError("qualitative review superseded tree is invalid")
    return _sha256(entries)


def _legacy_root_identity(
    matrix_dir: Path,
    review_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
) -> dict[str, Any]:
    result = target_index.inspection_result
    try:
        item_stat = review_dir.lstat()
    except OSError:
        raise ReviewError("qualitative review superseded root is unavailable") from None
    if not stat.S_ISDIR(item_stat.st_mode) or stat.S_IMODE(item_stat.st_mode) != 0o700:
        raise ReviewError("qualitative review superseded root is not private")
    allowed = {
        "cases",
        "operations",
        "ledger.json",
        "matrix.lock",
        "response-schema.json",
        _SUPERSESSION_FILENAME,
    }
    if {item.name for item in review_dir.iterdir()} - allowed:
        raise ReviewError("qualitative review superseded root is invalid")
    operations = review_dir / "operations"
    cases_dir = review_dir / "cases"
    for directory in (operations, cases_dir):
        try:
            directory_stat = directory.lstat()
        except OSError:
            raise ReviewError("qualitative review superseded root is invalid") from None
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise ReviewError("qualitative review superseded root is not private")
    if any(operations.iterdir()):
        raise ReviewError("qualitative review superseded root has operations")
    for kind in ("judgments", "adjudications"):
        root = matrix_dir / kind
        if root.is_dir() and any(root.glob("*.json")):
            raise ReviewError("qualitative review supersession has imported artifacts")
    ledger_path = review_dir / "ledger.json"
    response_schema_path = review_dir / "response-schema.json"
    ledger = _read_json(
        ledger_path, require_private=True, require_canonical=True
    )
    required = {
        "schema",
        "matrix_id",
        "manifest_sha256",
        "prepared_at",
        "policy",
        "response_schema",
        "cases",
    }
    if (
        set(ledger) != required
        or ledger.get("schema") != _LEGACY_LEDGER_SCHEMA
        or ledger.get("matrix_id") != result.manifest.matrix_id
        or ledger.get("manifest_sha256") != result.manifest_sha256
        or ledger.get("policy")
        != {
            "maximum_attempts_per_judgment": _MAX_ATTEMPTS,
            "model_invocation": "external",
            "usage_accounting": "unavailable",
        }
    ):
        raise ReviewError("qualitative review superseded ledger is invalid")
    response_schema = _read_json(
        response_schema_path, require_private=True, require_canonical=True
    )
    if (
        response_schema
        != _read_json(judge.SCHEMA_ROOT / "review-verdicts-0.1.json")
        or ledger.get("response_schema")
        != {
            "path": "response-schema.json",
            "sha256": _sha256(response_schema),
        }
    ):
        raise ReviewError("qualitative review superseded schema is invalid")
    expected = {
        (item.work_item_id, configured.identifier)
        for item in result.manifest.work_items
        for configured in result.manifest.campaign.judges
    }
    cases = ledger.get("cases")
    if not isinstance(cases, list) or len(cases) != len(expected):
        raise ReviewError("qualitative review superseded cases are invalid")
    seen: set[tuple[str, str]] = set()
    for entry in cases:
        if not isinstance(entry, dict):
            raise ReviewError("qualitative review superseded cases are invalid")
        work_item_id = entry.get("work_item_id")
        judge_identifier = entry.get("judge_identifier")
        if (
            set(entry) != {"work_item_id", "judge_identifier", "path", "sha256"}
            or not isinstance(work_item_id, str)
            or not isinstance(judge_identifier, str)
            or (work_item_id, judge_identifier) not in expected
            or (work_item_id, judge_identifier) in seen
            or entry.get("path")
            != f"cases/{work_item_id}-{judge_identifier}.json"
        ):
            raise ReviewError("qualitative review superseded cases are invalid")
        key = (work_item_id, judge_identifier)
        document = _read_json(
            review_dir / entry["path"],
            schema_name="review-case-0.1.json",
            require_private=True,
            require_canonical=True,
        )
        expected_document = _legacy_case_document(
            matrix_dir, target_index, work_item_id, judge_identifier
        )
        if document != expected_document or entry.get("sha256") != _sha256(document):
            raise ReviewError("qualitative review superseded case changed")
        seen.add(key)
    if seen != expected or {item.name for item in cases_dir.iterdir()} != {
        f"{work_item_id}-{judge_identifier}.json"
        for work_item_id, judge_identifier in expected
    }:
        raise ReviewError("qualitative review superseded cases are invalid")
    return {
        "ledger_schema": _LEGACY_LEDGER_SCHEMA,
        "tree_sha256": _review_tree_sha256(review_dir),
        "ledger_sha256": _raw_sha256(ledger_path),
        "response_schema_sha256": _raw_sha256(response_schema_path),
    }


def _incident_attempt_summary(
    value: Any,
    *,
    configured_slots: set[tuple[str, str]],
    configured_judges: set[str],
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "total_requests",
        "by_judge",
        "slots",
        "unattempted_slots",
    }:
        return False
    total = value["total_requests"]
    unattempted = value["unattempted_slots"]
    by_judge = value["by_judge"]
    slots = value["slots"]
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
        or not isinstance(unattempted, int)
        or isinstance(unattempted, bool)
        or not isinstance(by_judge, list)
        or not isinstance(slots, list)
    ):
        return False
    judge_counts: dict[str, int] = {}
    for item in by_judge:
        judge_identifier = (
            item.get("judge_identifier") if isinstance(item, dict) else None
        )
        if (
            not isinstance(item, dict)
            or set(item) != {"judge_identifier", "request_count"}
            or not isinstance(judge_identifier, str)
            or judge_identifier in judge_counts
            or judge_identifier not in configured_judges
            or not isinstance(item.get("request_count"), int)
            or isinstance(item.get("request_count"), bool)
            or item["request_count"] < 0
        ):
            return False
        judge_counts[judge_identifier] = item["request_count"]
    if set(judge_counts) != configured_judges or sum(judge_counts.values()) != total:
        return False
    seen: set[tuple[str, str]] = set()
    observed_by_judge = {identifier: 0 for identifier in configured_judges}
    for item in slots:
        if not isinstance(item, dict) or set(item) != {
            "work_item_id",
            "judge_identifier",
            "attempt_count",
            "duration",
        }:
            return False
        work_item_id = item.get("work_item_id")
        judge_identifier = item.get("judge_identifier")
        attempts = item.get("attempt_count")
        duration = item.get("duration")
        if (
            not isinstance(work_item_id, str)
            or not isinstance(judge_identifier, str)
            or (work_item_id, judge_identifier) not in configured_slots
            or (work_item_id, judge_identifier) in seen
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or not 1 <= attempts <= _MAX_ATTEMPTS
            or not isinstance(duration, dict)
        ):
            return False
        key = (work_item_id, judge_identifier)
        if duration.get("state") == "measured":
            if (
                set(duration) != {"state", "duration_ms"}
                or not isinstance(duration.get("duration_ms"), int)
                or isinstance(duration.get("duration_ms"), bool)
                or not 0 <= duration["duration_ms"] <= _MAX_DURATION_MS
            ):
                return False
        elif duration != {"state": "unavailable"}:
            return False
        seen.add(key)
        observed_by_judge[key[1]] += attempts
    return (
        observed_by_judge == judge_counts
        and total == sum(item["attempt_count"] for item in slots)
        and unattempted == len(configured_slots) - len(seen)
        and unattempted >= 0
    )


def _validate_incident(
    document: dict[str, Any],
    *,
    result: inspection.MatrixInspection,
    legacy: dict[str, Any],
) -> None:
    campaign = result.manifest.campaign
    source_revision = _source_revision(result)
    configured_slots = {
        (item.work_item_id, configured.identifier)
        for item in result.manifest.work_items
        for configured in campaign.judges
    }
    configured_judges = {item.identifier for item in campaign.judges}
    required = {
        "schema",
        "incident_id",
        "matrix_id",
        "manifest_sha256",
        "source_revision",
        "superseded",
        "reason",
        "provider_error",
        "codex",
        "attempt_summary",
        "states",
        "diagnostic_evidence",
        "recorded_at",
    }
    provider_error = document.get("provider_error")
    codex = document.get("codex")
    states = document.get("states")
    diagnostic = document.get("diagnostic_evidence")
    if (
        set(document) != required
        or document.get("schema") != _INCIDENT_SCHEMA
        or not isinstance(document.get("incident_id"), str)
        or _INCIDENT_ID.fullmatch(document["incident_id"]) is None
        or document.get("matrix_id") != result.manifest.matrix_id
        or document.get("manifest_sha256") != result.manifest_sha256
        or document.get("source_revision") != source_revision
        or document.get("superseded") != legacy
        or document.get("reason") != "provider_rejected_response_schema"
        or not isinstance(provider_error, dict)
        or set(provider_error) != {"class", "code", "message"}
        or any(
            not isinstance(provider_error.get(key), str)
            or not provider_error[key]
            or len(provider_error[key]) > (512 if key == "message" else 128)
            for key in provider_error
        )
        or judge._contains_private_material(provider_error)  # noqa: SLF001
        or not isinstance(codex, dict)
        or codex
        != {
            "version": codex_driver._REQUIRED_CODEX_VERSION,  # noqa: SLF001
            "isolation_attestation": _ISOLATION_ATTESTATION,
        }
        or not _incident_attempt_summary(
            document.get("attempt_summary"),
            configured_slots=configured_slots,
            configured_judges=configured_judges,
        )
        or states
        != {
            "inference": "absent",
            "model_output": "absent",
            "operations": "absent",
            "imports": "absent",
            "adjudications": "absent",
        }
        or not isinstance(diagnostic, dict)
        or (
            diagnostic != {"state": "unavailable"}
            and (
                set(diagnostic) != {"state", "sha256"}
                or diagnostic.get("state") != "available"
                or not isinstance(diagnostic.get("sha256"), str)
                or _SHA256.fullmatch(diagnostic["sha256"]) is None
            )
        )
        or not _valid_timestamp(document.get("recorded_at"))
    ):
        raise ReviewError("qualitative review incident record is invalid")


def supersession_identity(matrix_dir: Path, review_dir: Path) -> dict[str, Any]:
    """Return privacy-safe identities needed to author a v1 incident record."""

    matrix_dir, review_dir = _separated_review_roots(matrix_dir, review_dir)
    try:
        target_index = judge._target_index(matrix_dir)  # noqa: SLF001
    except (judge.JudgmentError, inspection.InspectionError) as error:
        raise ReviewError(str(error)) from None
    result = target_index.inspection_result
    legacy = _legacy_root_identity(matrix_dir, review_dir, target_index)
    if _node_exists(review_dir / _SUPERSESSION_FILENAME):
        raise ReviewError("qualitative review package is already superseded")
    return {
        "matrix_id": result.manifest.matrix_id,
        "manifest_sha256": result.manifest_sha256,
        "source_revision": _source_revision(result),
        "legacy": legacy,
    }


def _supersession_tombstone(
    *,
    result: inspection.MatrixInspection,
    legacy: dict[str, Any],
    incident: dict[str, Any],
    incident_sha256: str,
    package_id: str,
    destination_path_sha256: str,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": _SUPERSESSION_SCHEMA,
        "kind": "whole_package_supersession",
        "matrix_id": result.manifest.matrix_id,
        "manifest_sha256": result.manifest_sha256,
        "source_revision": _source_revision(result),
        "legacy": legacy,
        "incident_sha256": incident_sha256,
        "incident": incident,
        "replacement_package_id": package_id,
        "destination_path_sha256": destination_path_sha256,
        "recorded_at": _now() if recorded_at is None else recorded_at,
    }


def _validate_supersession_tombstone(
    document: dict[str, Any],
    *,
    result: inspection.MatrixInspection,
    legacy: dict[str, Any],
    destination_path_sha256: str,
) -> tuple[dict[str, Any], str, str]:
    required = {
        "schema",
        "kind",
        "matrix_id",
        "manifest_sha256",
        "source_revision",
        "legacy",
        "incident_sha256",
        "incident",
        "replacement_package_id",
        "destination_path_sha256",
        "recorded_at",
    }
    incident = document.get("incident")
    package_id = document.get("replacement_package_id")
    incident_sha256 = document.get("incident_sha256")
    if (
        set(document) != required
        or document.get("schema") != _SUPERSESSION_SCHEMA
        or document.get("kind") != "whole_package_supersession"
        or document.get("matrix_id") != result.manifest.matrix_id
        or document.get("manifest_sha256") != result.manifest_sha256
        or document.get("source_revision") != _source_revision(result)
        or document.get("legacy") != legacy
        or not isinstance(incident, dict)
        or not isinstance(incident_sha256, str)
        or _SHA256.fullmatch(incident_sha256) is None
        or not isinstance(package_id, str)
        or _PACKAGE_ID.fullmatch(package_id) is None
        or document.get("destination_path_sha256") != destination_path_sha256
        or not _valid_timestamp(document.get("recorded_at"))
    ):
        raise ReviewError("qualitative review supersession tombstone is invalid")
    _validate_incident(incident, result=result, legacy=legacy)
    if incident_sha256 != _sha256(incident):
        raise ReviewError("qualitative review supersession tombstone is invalid")
    return incident, incident_sha256, package_id


def _validate_review_root(
    matrix_dir: Path,
    review_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    measurement_amendment: tuple[dict[str, Any], str] | None | object = (
        _AMENDMENT_UNSET
    ),
    validation_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = target_index.inspection_result
    amendment = (
        _load_measurement_amendment(matrix_dir)
        if measurement_amendment is _AMENDMENT_UNSET
        else measurement_amendment
    )
    try:
        item_stat = review_dir.lstat()
    except OSError:
        raise ReviewError("qualitative review directory is unavailable") from None
    if not stat.S_ISDIR(item_stat.st_mode) or stat.S_IMODE(item_stat.st_mode) != 0o700:
        raise ReviewError("qualitative review directory is not private")
    allowed = {
        "cases",
        "operations",
        "canary-case.json",
        "canary-attestation.json",
        "incident.json",
        "ledger.json",
        "matrix.lock",
        "response-schema.json",
    }
    if {item.name for item in review_dir.iterdir()} - allowed:
        raise ReviewError("qualitative review directory contains unexpected nodes")
    for name in ("cases", "operations"):
        path = review_dir / name
        try:
            directory_stat = path.lstat()
        except OSError:
            raise ReviewError("qualitative review directory is unavailable") from None
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise ReviewError("qualitative review directory is not private")
    ledger, ledger_content = _read_json_with_content(
        review_dir / "ledger.json", require_private=True, require_canonical=True
    )
    required = {
        "schema",
        "package_id",
        "matrix_id",
        "manifest_sha256",
        "prepared_at",
        "policy",
        "response_schema",
        "canary",
        "supersedes",
        "cases",
    }
    package_id = ledger.get("package_id")
    if (
        set(ledger) != required
        or ledger.get("schema") != _LEDGER_SCHEMA
        or not isinstance(package_id, str)
        or _PACKAGE_ID.fullmatch(package_id) is None
        or not _valid_timestamp(ledger.get("prepared_at"))
    ):
        raise ReviewError("qualitative review ledger is invalid")
    if (
        ledger.get("matrix_id") != result.manifest.matrix_id
        or ledger.get("manifest_sha256") != result.manifest_sha256
        or ledger.get("policy")
        != {
            "maximum_attempts_per_judgment": _MAX_ATTEMPTS,
            "model_invocation": "external",
            "usage_accounting": "unavailable",
            "package_protocol": "qualitative-review-package/0.2",
            "operation_schema": _OPERATION_SCHEMA,
            "canary_required": True,
        }
    ):
        raise ReviewError("qualitative review ledger does not match matrix")
    registry = _validate_registry(
        matrix_dir,
        review_dir,
        result,
        package_id=package_id,
    )
    if validation_context is not None:
        validation_context.clear()
        validation_context.update(
            ledger_sha256=hashlib.sha256(ledger_content).hexdigest(),
            registry_sha256=_sha256(registry),
        )
    response_schema = _read_json(
        review_dir / "response-schema.json",
        require_private=True,
        require_canonical=True,
    )
    expected_response_schema = _read_json(
        judge.SCHEMA_ROOT / "review-verdicts-0.2.json"
    )
    if (
        ledger.get("response_schema")
        != {
            "path": "response-schema.json",
            "sha256": _sha256(response_schema),
            "validator": _STRUCTURED_OUTPUT_VALIDATOR,
        }
        or response_schema != expected_response_schema
    ):
        raise ReviewError("qualitative review response schema is invalid")
    _validate_structured_output_schema(response_schema)

    routes = {
        (configured.model, configured.reasoning_effort)
        for configured in result.manifest.campaign.judges
    }
    canary_entry = ledger.get("canary")
    if routes != {(_CANARY_MODEL, _CANARY_REASONING_EFFORT)} or not isinstance(
        canary_entry, dict
    ):
        raise ReviewError("qualitative review canary is invalid")
    canary_model, canary_effort = _CANARY_MODEL, _CANARY_REASONING_EFFORT
    if canary_entry != {
        "case_path": "canary-case.json",
        "case_sha256": canary_entry.get("case_sha256"),
        "attestation_path": "canary-attestation.json",
        "model": canary_model,
        "reasoning_effort": canary_effort,
        "codex_version": codex_driver._REQUIRED_CODEX_VERSION,  # noqa: SLF001
        "isolation_attestation": _ISOLATION_ATTESTATION,
        "operator_invocation_attestation": _CANARY_OPERATOR_ATTESTATION,
    } or not isinstance(canary_entry.get("case_sha256"), str):
        raise ReviewError("qualitative review canary is invalid")

    supersedes = ledger.get("supersedes")
    supersession_identity: dict[str, Any] | None = None
    incident_path = review_dir / "incident.json"
    if supersedes is None:
        if registry["supersession"] is not None:
            raise ReviewError("qualitative review package registry is invalid")
        if _node_exists(incident_path):
            raise ReviewError("qualitative review supersession is invalid")
    else:
        if not isinstance(supersedes, dict) or set(supersedes) != {
            "reason",
            "legacy",
            "incident",
        }:
            raise ReviewError("qualitative review supersession is invalid")
        legacy = supersedes.get("legacy")
        incident_entry = supersedes.get("incident")
        if (
            supersedes.get("reason") != "provider_rejected_response_schema"
            or not isinstance(legacy, dict)
            or set(legacy)
            != {
                "ledger_schema",
                "tree_sha256",
                "ledger_sha256",
                "response_schema_sha256",
            }
            or legacy.get("ledger_schema") != _LEGACY_LEDGER_SCHEMA
            or any(
                not isinstance(legacy.get(name), str)
                or _SHA256.fullmatch(legacy[name]) is None
                for name in (
                    "tree_sha256",
                    "ledger_sha256",
                    "response_schema_sha256",
                )
            )
            or not isinstance(incident_entry, dict)
            or set(incident_entry) != {"path", "sha256"}
            or incident_entry.get("path") != "incident.json"
            or not isinstance(incident_entry.get("sha256"), str)
            or _SHA256.fullmatch(incident_entry["sha256"]) is None
        ):
            raise ReviewError("qualitative review supersession is invalid")
        incident, content = _read_json_with_content(
            incident_path, require_private=True, require_canonical=True
        )
        if hashlib.sha256(content).hexdigest() != incident_entry["sha256"]:
            raise ReviewError("qualitative review supersession is invalid")
        _validate_incident(
            incident,
            result=result,
            legacy=legacy,
        )
        registry_supersession = registry["supersession"]
        if not isinstance(registry_supersession, dict):
            raise ReviewError("qualitative review package registry is invalid")
        expected_tombstone = _supersession_tombstone(
            result=result,
            legacy=legacy,
            incident=incident,
            incident_sha256=incident_entry["sha256"],
            package_id=package_id,
            destination_path_sha256=_destination_path_sha256(review_dir),
            recorded_at=registry_supersession["recorded_at"],
        )
        if (
            _sha256(expected_tombstone)
            != registry_supersession["tombstone_sha256"]
        ):
            raise ReviewError("qualitative review package registry is invalid")
        supersession_identity = {
            "reason": "provider_rejected_response_schema",
            "legacy": legacy,
            "incident_sha256": incident_entry["sha256"],
        }

    preliminary_cases = [
        _preliminary_case_document(
            matrix_dir,
            target_index,
            work_item.work_item_id,
            judge_config.identifier,
        )
        for work_item in result.manifest.work_items
        for judge_config in result.manifest.campaign.judges
    ]
    if package_id != _package_id(
        result, response_schema, preliminary_cases, supersession_identity
    ):
        raise ReviewError("qualitative review package identity is invalid")

    canary = _read_json(
        review_dir / "canary-case.json",
        schema_name="review-case-0.2.json",
        require_private=True,
        require_canonical=True,
    )
    expected_canary = _canary_case(
        matrix_id=result.manifest.matrix_id,
        package_id=package_id,
        response_schema_sha256=_sha256(response_schema),
    )
    if (
        canary != expected_canary
        or _sha256(canary) != canary_entry["case_sha256"]
    ):
        raise ReviewError("qualitative review canary is invalid")

    cases = ledger.get("cases")
    expected_case_count = len(result.manifest.work_items) * len(
        result.manifest.campaign.judges
    )
    if (
        not isinstance(cases, list)
        or len(cases) != expected_case_count
        or expected_case_count > _MAX_CASES * len(result.manifest.campaign.judges)
    ):
        raise ReviewError("qualitative review ledger is invalid")
    expected_work_items = {item.work_item_id for item in result.manifest.work_items}
    expected_judges = {
        item.identifier for item in result.manifest.campaign.judges
    }
    expected_invocations = {
        (work_item_id, judge_identifier)
        for work_item_id in expected_work_items
        for judge_identifier in expected_judges
    }
    seen_invocations: set[tuple[str, str]] = set()
    for item in cases:
        work_item_id = item.get("work_item_id") if isinstance(item, dict) else None
        judge_identifier = (
            item.get("judge_identifier") if isinstance(item, dict) else None
        )
        if (
            not isinstance(item, dict)
            or set(item)
            != {"work_item_id", "judge_identifier", "path", "sha256"}
            or not isinstance(work_item_id, str)
            or _SAFE_WORK_ITEM.fullmatch(work_item_id) is None
            or not isinstance(judge_identifier, str)
            or judge_identifier not in expected_judges
            or item.get("path")
            != f"cases/{work_item_id}-{judge_identifier}.json"
            or not isinstance(item.get("sha256"), str)
            or _SHA256.fullmatch(item["sha256"]) is None
            or (work_item_id, judge_identifier) in seen_invocations
        ):
            raise ReviewError("qualitative review ledger is invalid")
        seen_invocations.add((work_item_id, judge_identifier))
    if seen_invocations != expected_invocations:
        raise ReviewError("qualitative review ledger does not cover matrix")
    if {item.name for item in (review_dir / "cases").iterdir()} != {
        f"{work_item_id}-{judge_identifier}.json"
        for work_item_id, judge_identifier in expected_invocations
    }:
        raise ReviewError("qualitative review case directory is invalid")
    for work_item_id, judge_identifier in sorted(expected_invocations):
        _load_bound_case(
            matrix_dir,
            review_dir,
            target_index,
            ledger,
            work_item_id,
            judge_identifier,
        )

    operation_names = {item.name for item in (review_dir / "operations").iterdir()}
    if len(operation_names) > len(cases) + len(expected_work_items):
        raise ReviewError("qualitative review operation ledger exceeds limits")
    judgment_operations = {
        f"judgment-{work_item_id}-{judge_config.identifier}.json": (
            work_item_id,
            judge_config.identifier,
        )
        for work_item_id in expected_work_items
        for judge_config in result.manifest.campaign.judges
    }
    adjudication_operations = {
        f"adjudication-{work_item_id}-agreement.json": work_item_id
        for work_item_id in expected_work_items
    }
    valid_operation_names = set(judgment_operations) | set(adjudication_operations)
    if not operation_names <= valid_operation_names:
        raise ReviewError("qualitative review operation ledger is invalid")
    canary_sha256: str | None = None
    attestation_path = review_dir / "canary-attestation.json"
    if _node_exists(attestation_path):
        canary_terminal, canary_digest = _validate_canary_terminal(
            review_dir, ledger
        )
        if canary_terminal["status"] == "passed":
            canary_sha256 = canary_digest
    if operation_names and canary_sha256 is None:
        raise ReviewError("qualitative review canary attestation is unavailable")
    if amendment is not None and not isinstance(amendment, tuple):
        raise ReviewError("qualitative review measurement amendment is invalid")
    _validate_measurement_amendment(
        matrix_dir,
        review_dir,
        target_index,
        ledger,
        _sha256(registry),
        hashlib.sha256(ledger_content).hexdigest(),
        canary_sha256,
        amendment,
    )
    retained_judgments: dict[str, dict[str, Any]] | None = None
    retained_judgment_files: list[tuple[Path, str, dict[str, Any]]] | None = None
    retained_adjudication_files: list[
        tuple[Path, str, dict[str, Any]]
    ] | None = None
    first_judge = result.manifest.campaign.judges[0].identifier
    for name in sorted(operation_names):
        operation = _read_json(
            review_dir / "operations" / name,
            require_private=True,
            require_canonical=True,
        )
        try:
            if name in judgment_operations:
                work_item_id, judge_identifier = judgment_operations[name]
                case = _load_bound_case(
                    matrix_dir,
                    review_dir,
                    target_index,
                    ledger,
                    work_item_id,
                    judge_identifier,
                )
                artifact, _effective_duration = _effective_judgment_operation(
                    operation,
                    case=case,
                    judge_identifier=judge_identifier,
                    canary_attestation_sha256=canary_sha256,
                    measurement_amendment=amendment,
                )
                judge._validate_judgment_document(  # noqa: SLF001
                    target_index, artifact
                )
                if retained_judgments is None:
                    retained_judgments = judge._retained_judgments(  # noqa: SLF001
                        matrix_dir, target_index
                    )
                if retained_judgment_files is None:
                    retained_judgment_files = _retained_judgment_files(
                        matrix_dir, retained_judgments
                    )
                matching_files = _matching_judgment_files(
                    retained_judgment_files,
                    target=case["target"],
                    judge_identifier=judge_identifier,
                )
                if len(matching_files) > 1:
                    raise ReviewError("qualitative judgment roster is ambiguous")
                if matching_files:
                    path, digest, retained_artifact = matching_files[0]
                    if (
                        digest != operation["artifact_sha256"]
                        or retained_artifact != artifact
                    ):
                        raise ReviewError(
                            "retained judgment does not match review operation"
                        )
                    if path.name != f"{artifact['judgment_id']}.json":
                        raise ReviewError("retained judgment filename is invalid")
                    if _retained_target(
                        path,
                        artifact,
                        digest,
                        kind="judgment",
                    ) is None:
                        raise ReviewError("retained judgment is unavailable")
            else:
                work_item_id = adjudication_operations[name]
                case = _load_bound_case(
                    matrix_dir,
                    review_dir,
                    target_index,
                    ledger,
                    work_item_id,
                    first_judge,
                )
                artifact = _validate_adjudication_operation(
                    operation,
                    case=case,
                    canary_attestation_sha256=canary_sha256,
                )
                if retained_judgments is None:
                    retained_judgments = judge._retained_judgments(  # noqa: SLF001
                        matrix_dir, target_index
                    )
                judge._validate_adjudication_document(  # noqa: SLF001
                    matrix_dir,
                    target_index,
                    artifact,
                    retained=retained_judgments,
                )
                if retained_adjudication_files is None:
                    retained_adjudication_files = _retained_adjudication_files(
                        matrix_dir, target_index, retained_judgments
                    )
                matching_files = [
                    item
                    for item in retained_adjudication_files
                    if item[2]["target"] == case["target"]
                ]
                if len(matching_files) > 1:
                    raise ReviewError("qualitative adjudication roster is ambiguous")
                if matching_files:
                    path, digest, retained_artifact = matching_files[0]
                    if (
                        digest != operation["artifact_sha256"]
                        or retained_artifact != artifact
                    ):
                        raise ReviewError(
                            "retained adjudication does not match review operation"
                        )
                    if path.name != f"{artifact['adjudication_id']}.json":
                        raise ReviewError("retained adjudication filename is invalid")
                    if _retained_target(
                        path,
                        artifact,
                        digest,
                        kind="adjudication",
                    ) is None:
                        raise ReviewError("retained adjudication is unavailable")
        except judge.JudgmentError:
            raise ReviewError("qualitative review operation ledger is invalid") from None
    return ledger


def _require_reviewable_benchmark(result: inspection.MatrixInspection) -> None:
    if (
        result.manifest.campaign.campaign_kind != "benchmark"
        or not result.structurally_complete
        or not result.manifest.work_items
        or len(result.manifest.work_items) > _MAX_CASES
    ):
        raise ReviewError("qualitative review requires a complete benchmark")


def _reject_retained_review_outcomes(matrix_dir: Path) -> None:
    for kind in ("judgments", "adjudications"):
        root = matrix_dir / kind
        if root.is_dir() and any(root.glob("*.json")):
            raise ReviewError("qualitative review package has retained outcomes")


def _preliminary_cases(
    matrix_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
) -> list[dict[str, Any]]:
    result = target_index.inspection_result
    return [
        _preliminary_case_document(
            matrix_dir,
            target_index,
            work_item.work_item_id,
            judge_config.identifier,
        )
        for work_item in result.manifest.work_items
        for judge_config in result.manifest.campaign.judges
    ]


def _publish_review_package(
    matrix_dir: Path,
    review_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    *,
    response_schema: dict[str, Any],
    package_id: str,
    supersedes: dict[str, Any] | None,
    incident: dict[str, Any] | None,
) -> dict[str, Any]:
    result = target_index.inspection_result
    parent = review_dir.parent
    if not parent.is_dir():
        raise ReviewError("qualitative review parent directory is unavailable")
    judge_routes = {
        (item.model, item.reasoning_effort)
        for item in result.manifest.campaign.judges
    }
    if judge_routes != {(_CANARY_MODEL, _CANARY_REASONING_EFFORT)}:
        raise ReviewError("qualitative review canary route is unsupported")
    canary = _canary_case(
        matrix_id=result.manifest.matrix_id,
        package_id=package_id,
        response_schema_sha256=_sha256(response_schema),
    )
    try:
        judge._validate_schema(canary, "review-case-0.2.json")  # noqa: SLF001
    except judge.JudgmentError as error:
        raise ReviewError(str(error)) from None

    staging = Path(tempfile.mkdtemp(prefix=f".{review_dir.name}-", dir=parent))
    try:
        staging.chmod(0o700)
        cases_dir = staging / "cases"
        operations_dir = staging / "operations"
        _private_dir(cases_dir)
        _private_dir(operations_dir)
        _write_json(staging / "response-schema.json", response_schema)
        _write_json(staging / "canary-case.json", canary)
        if incident is not None:
            _write_json(staging / "incident.json", incident)
        case_entries: list[dict[str, Any]] = []
        for work_item in result.manifest.work_items:
            for judge_config in result.manifest.campaign.judges:
                document = _case_document(
                    matrix_dir,
                    target_index,
                    work_item.work_item_id,
                    judge_config.identifier,
                    package_id,
                )
                try:
                    judge._validate_schema(  # noqa: SLF001
                        document, "review-case-0.2.json"
                    )
                except judge.JudgmentError as error:
                    raise ReviewError(str(error)) from None
                filename = (
                    f"{work_item.work_item_id}-{judge_config.identifier}.json"
                )
                _write_json(cases_dir / filename, document)
                case_entries.append(
                    {
                        "work_item_id": work_item.work_item_id,
                        "judge_identifier": judge_config.identifier,
                        "path": f"cases/{filename}",
                        "sha256": _sha256(document),
                    }
                )
        ledger = {
            "schema": _LEDGER_SCHEMA,
            "package_id": package_id,
            "matrix_id": result.manifest.matrix_id,
            "manifest_sha256": result.manifest_sha256,
            "prepared_at": _now(),
            "policy": {
                "maximum_attempts_per_judgment": _MAX_ATTEMPTS,
                "model_invocation": "external",
                "usage_accounting": "unavailable",
                "package_protocol": "qualitative-review-package/0.2",
                "operation_schema": _OPERATION_SCHEMA,
                "canary_required": True,
            },
            "response_schema": {
                "path": "response-schema.json",
                "sha256": _sha256(response_schema),
                "validator": _STRUCTURED_OUTPUT_VALIDATOR,
            },
            "canary": {
                "case_path": "canary-case.json",
                "case_sha256": _sha256(canary),
                "attestation_path": "canary-attestation.json",
                "model": _CANARY_MODEL,
                "reasoning_effort": _CANARY_REASONING_EFFORT,
                "codex_version": codex_driver._REQUIRED_CODEX_VERSION,  # noqa: SLF001
                "isolation_attestation": _ISOLATION_ATTESTATION,
                "operator_invocation_attestation": _CANARY_OPERATOR_ATTESTATION,
            },
            "supersedes": supersedes,
            "cases": case_entries,
        }
        _write_json(staging / "ledger.json", ledger)
        os.replace(staging, review_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "matrix_id": result.manifest.matrix_id,
        "package_id": package_id,
        "cases": len(case_entries),
        "canary": "canary-case.json",
    }


def _retained_prepare_result(
    matrix_dir: Path,
    review_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    *,
    package_id: str,
) -> dict[str, Any]:
    ledger = _validate_review_root(matrix_dir, review_dir, target_index)
    if ledger["package_id"] != package_id:
        raise ReviewError("qualitative review replacement does not match tombstone")
    return {
        "matrix_id": ledger["matrix_id"],
        "package_id": package_id,
        "cases": len(ledger["cases"]),
        "canary": ledger["canary"]["case_path"],
    }


def prepare(
    matrix_dir: Path,
    review_dir: Path,
    *,
    supersede_review_dir: Path | None = None,
    incident_record: Path | None = None,
) -> dict[str, Any]:
    """Publish one immutable route-blind case package for a benchmark."""

    matrix_dir, review_dir = _separated_review_roots(matrix_dir, review_dir)
    if supersede_review_dir is None and incident_record is not None:
        raise ReviewError("qualitative review supersession inputs are incomplete")
    response_schema = _read_json(
        judge.SCHEMA_ROOT / "review-verdicts-0.2.json"
    )
    _validate_structured_output_schema(response_schema)

    if supersede_review_dir is None:
        with matrix.MatrixLock(matrix_dir):
            try:
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except (judge.JudgmentError, inspection.InspectionError) as error:
                raise ReviewError(str(error)) from None
            result = target_index.inspection_result
            _require_reviewable_benchmark(result)
            _reject_retained_review_outcomes(matrix_dir)
            package_id = _package_id(
                result,
                response_schema,
                _preliminary_cases(matrix_dir, target_index),
                None,
            )
            registry_path = matrix_dir / _REGISTRY_FILENAME
            if not _node_exists(registry_path) and _node_exists(review_dir):
                raise ReviewError(
                    "qualitative review directory lacks package registry"
                )
            _reserve_registry(
                matrix_dir,
                review_dir,
                result,
                package_id=package_id,
                supersession=None,
            )
            if _node_exists(review_dir):
                return _retained_prepare_result(
                    matrix_dir,
                    review_dir,
                    target_index,
                    package_id=package_id,
                )
            return _publish_review_package(
                matrix_dir,
                review_dir,
                target_index,
                response_schema=response_schema,
                package_id=package_id,
                supersedes=None,
                incident=None,
            )

    old_matrix_dir, old_review_dir = _separated_review_roots(
        matrix_dir, supersede_review_dir
    )
    if old_matrix_dir != matrix_dir or (
        review_dir.is_relative_to(old_review_dir)
        or old_review_dir.is_relative_to(review_dir)
    ):
        raise ReviewError("qualitative review supersession roots overlap")

    with matrix.MatrixLock(old_review_dir):
        with matrix.MatrixLock(matrix_dir):
            try:
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except (judge.JudgmentError, inspection.InspectionError) as error:
                raise ReviewError(str(error)) from None
            result = target_index.inspection_result
            _require_reviewable_benchmark(result)
            _reject_retained_review_outcomes(matrix_dir)
            preliminary_cases = _preliminary_cases(matrix_dir, target_index)
            legacy = _legacy_root_identity(matrix_dir, old_review_dir, target_index)
            destination_sha256 = _destination_path_sha256(review_dir)
            tombstone_path = old_review_dir / _SUPERSESSION_FILENAME
            registry_path = matrix_dir / _REGISTRY_FILENAME

            if _node_exists(tombstone_path):
                tombstone = _read_json(
                    tombstone_path, require_private=True, require_canonical=True
                )
                incident, incident_sha256, package_id = (
                    _validate_supersession_tombstone(
                        tombstone,
                        result=result,
                        legacy=legacy,
                        destination_path_sha256=destination_sha256,
                    )
                )
                if incident_record is not None:
                    if not _node_exists(incident_record):
                        raise ReviewError(
                            "qualitative review supersession inputs are incomplete"
                        )
                    supplied, supplied_content = _read_json_with_content(
                        incident_record,
                        require_private=True,
                        require_canonical=True,
                    )
                    if (
                        supplied != incident
                        or hashlib.sha256(supplied_content).hexdigest()
                        != incident_sha256
                    ):
                        raise ReviewError(
                            "qualitative review incident does not match tombstone"
                        )
                registry_supersession = {
                    "tombstone_sha256": _sha256(tombstone),
                    "recorded_at": tombstone["recorded_at"],
                }
                _reserve_registry(
                    matrix_dir,
                    review_dir,
                    result,
                    package_id=package_id,
                    supersession=registry_supersession,
                )
            else:
                if _node_exists(review_dir):
                    raise ReviewError(
                        "qualitative review replacement lacks supersession tombstone"
                    )
                if incident_record is None:
                    raise ReviewError(
                        "qualitative review supersession inputs are incomplete"
                    )
                incident, incident_content = _read_json_with_content(
                    incident_record,
                    require_private=True,
                    require_canonical=True,
                )
                _validate_incident(incident, result=result, legacy=legacy)
                incident_sha256 = hashlib.sha256(incident_content).hexdigest()
                supersession_identity = {
                    "reason": "provider_rejected_response_schema",
                    "legacy": legacy,
                    "incident_sha256": incident_sha256,
                }
                package_id = _package_id(
                    result,
                    response_schema,
                    preliminary_cases,
                    supersession_identity,
                )
                if _node_exists(registry_path):
                    registry = _validate_registry(
                        matrix_dir,
                        review_dir,
                        result,
                        package_id=package_id,
                    )
                    registry_supersession = registry["supersession"]
                    if not isinstance(registry_supersession, dict):
                        raise ReviewError(
                            "qualitative review package registry is invalid"
                        )
                    tombstone = _supersession_tombstone(
                        result=result,
                        legacy=legacy,
                        incident=incident,
                        incident_sha256=incident_sha256,
                        package_id=package_id,
                        destination_path_sha256=destination_sha256,
                        recorded_at=registry_supersession["recorded_at"],
                    )
                    if (
                        _sha256(tombstone)
                        != registry_supersession["tombstone_sha256"]
                    ):
                        raise ReviewError(
                            "qualitative review package registry is invalid"
                        )
                else:
                    tombstone = _supersession_tombstone(
                        result=result,
                        legacy=legacy,
                        incident=incident,
                        incident_sha256=incident_sha256,
                        package_id=package_id,
                        destination_path_sha256=destination_sha256,
                    )
                    registry_supersession = {
                        "tombstone_sha256": _sha256(tombstone),
                        "recorded_at": tombstone["recorded_at"],
                    }
                    _reserve_registry(
                        matrix_dir,
                        review_dir,
                        result,
                        package_id=package_id,
                        supersession=registry_supersession,
                    )
                _write_json(tombstone_path, tombstone)

            supersession_identity = {
                "reason": "provider_rejected_response_schema",
                "legacy": legacy,
                "incident_sha256": incident_sha256,
            }
            expected_package_id = _package_id(
                result,
                response_schema,
                preliminary_cases,
                supersession_identity,
            )
            if package_id != expected_package_id:
                raise ReviewError(
                    "qualitative review supersession tombstone is invalid"
                )
            supersedes = {
                "reason": "provider_rejected_response_schema",
                "legacy": legacy,
                "incident": {
                    "path": "incident.json",
                    "sha256": incident_sha256,
                },
            }
            if _node_exists(review_dir):
                return _retained_prepare_result(
                    matrix_dir,
                    review_dir,
                    target_index,
                    package_id=package_id,
                )
            return _publish_review_package(
                matrix_dir,
                review_dir,
                target_index,
                response_schema=response_schema,
                package_id=package_id,
                supersedes=supersedes,
                incident=incident,
            )


def _canary_evidence_binding(
    case: dict[str, Any], evidence: dict[str, Any]
) -> str:
    return _sha256(
        {
            "schema": _CANARY_ATTESTATION_SCHEMA,
            "case_sha256": _sha256(case),
            "invocation_evidence": {
                key: value
                for key, value in evidence.items()
                if key != "binding_sha256"
            },
        }
    )


def _validate_canary_terminal(
    review_dir: Path, ledger: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    path = review_dir / ledger["canary"]["attestation_path"]
    if not _node_exists(path):
        raise ReviewError("qualitative review canary attestation is unavailable")
    document, content = _read_json_with_content(
        path, require_private=True, require_canonical=True
    )
    evidence = document.get("invocation_evidence")
    canary, canary_content = _read_json_with_content(
        review_dir / ledger["canary"]["case_path"],
        schema_name="review-case-0.2.json",
        require_private=True,
        require_canonical=True,
    )
    required = {
        "schema",
        "status",
        "package_id",
        "matrix_id",
        "response_schema_sha256",
        "case_sha256",
        "model",
        "reasoning_effort",
        "codex_version",
        "isolation_attestation",
        "operator_invocation_attestation",
        "duration_ms",
        "recorded_at",
    }
    status = document.get("status")
    if status == "passed":
        required.add("invocation_evidence")
    elif status == "failed":
        required.add("failure_class")
    if (
        set(document) != required
        or document.get("schema") != _CANARY_ATTESTATION_SCHEMA
        or status not in {"passed", "failed"}
        or document.get("package_id") != ledger["package_id"]
        or document.get("matrix_id") != ledger["matrix_id"]
        or document.get("response_schema_sha256")
        != ledger["response_schema"]["sha256"]
        or hashlib.sha256(canary_content).hexdigest()
        != ledger["canary"]["case_sha256"]
        or document.get("case_sha256") != ledger["canary"]["case_sha256"]
        or document.get("model") != ledger["canary"]["model"]
        or document.get("reasoning_effort")
        != ledger["canary"]["reasoning_effort"]
        or document.get("codex_version") != ledger["canary"]["codex_version"]
        or document.get("isolation_attestation")
        != ledger["canary"]["isolation_attestation"]
        or document.get("operator_invocation_attestation")
        != ledger["canary"]["operator_invocation_attestation"]
        or not isinstance(document.get("duration_ms"), int)
        or isinstance(document.get("duration_ms"), bool)
        or not 0 <= document["duration_ms"] <= _MAX_DURATION_MS
        or not _valid_timestamp(document.get("recorded_at"))
    ):
        raise ReviewError("qualitative review canary attestation is invalid")
    if status == "failed":
        if document.get("failure_class") not in _CANARY_FAILURE_CLASSES:
            raise ReviewError("qualitative review canary attestation is invalid")
    elif (
        not isinstance(evidence, dict)
        or set(evidence)
        != {
            "validator",
            "event_log_sha256",
            "event_count",
            "verdict_bytes_sha256",
            "verdict_document_sha256",
            "binding_sha256",
        }
        or evidence.get("validator") != _EVENT_VALIDATOR
        or not isinstance(evidence.get("event_log_sha256"), str)
        or _SHA256.fullmatch(evidence["event_log_sha256"]) is None
        or not isinstance(evidence.get("event_count"), int)
        or isinstance(evidence.get("event_count"), bool)
        or not 4 <= evidence["event_count"] <= _MAX_DOCUMENT_BYTES
        or not isinstance(evidence.get("verdict_bytes_sha256"), str)
        or _SHA256.fullmatch(evidence["verdict_bytes_sha256"]) is None
        or not isinstance(evidence.get("verdict_document_sha256"), str)
        or _SHA256.fullmatch(evidence["verdict_document_sha256"]) is None
        or evidence.get("binding_sha256")
        != _canary_evidence_binding(canary, evidence)
    ):
        raise ReviewError("qualitative review canary attestation is invalid")
    return document, hashlib.sha256(content).hexdigest()


def _validate_canary_attestation(
    review_dir: Path, ledger: dict[str, Any]
) -> str:
    document, digest = _validate_canary_terminal(review_dir, ledger)
    if document["status"] != "passed":
        raise ReviewError("qualitative review canary is terminally failed")
    return digest


def _publish_canary_failure(
    review_dir: Path,
    ledger: dict[str, Any],
    *,
    duration_ms: int,
    isolation_attestation: str,
    operator_invocation_attestation: str,
    failure_class: str,
) -> tuple[dict[str, Any], str]:
    path = review_dir / ledger["canary"]["attestation_path"]
    if _node_exists(path):
        document, digest = _validate_canary_terminal(review_dir, ledger)
        if document["status"] != "failed":
            raise ReviewError("qualitative review canary is already terminal")
        if (
            document["failure_class"] != failure_class
            or document["duration_ms"] != duration_ms
            or document["isolation_attestation"] != isolation_attestation
            or document["operator_invocation_attestation"]
            != operator_invocation_attestation
        ):
            raise ReviewError(
                "qualitative review canary failure replay is contradictory"
            )
        return document, digest
    document = {
        "schema": _CANARY_ATTESTATION_SCHEMA,
        "status": "failed",
        "package_id": ledger["package_id"],
        "matrix_id": ledger["matrix_id"],
        "response_schema_sha256": ledger["response_schema"]["sha256"],
        "case_sha256": ledger["canary"]["case_sha256"],
        "model": ledger["canary"]["model"],
        "reasoning_effort": ledger["canary"]["reasoning_effort"],
        "codex_version": ledger["canary"]["codex_version"],
        "isolation_attestation": isolation_attestation,
        "operator_invocation_attestation": operator_invocation_attestation,
        "duration_ms": duration_ms,
        "failure_class": failure_class,
        "recorded_at": _now(),
    }
    _write_json(path, document)
    return _validate_canary_terminal(review_dir, ledger)


def record_canary(
    matrix_dir: Path,
    review_dir: Path,
    verdicts_path: Path,
    *,
    events_path: Path,
    duration_ms: int,
    isolation_attestation: str,
    operator_invocation_attestation: str,
) -> dict[str, Any]:
    """Record evidence for one externally invoked structured-output canary."""

    if (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or not 0 <= duration_ms <= _MAX_DURATION_MS
        or isolation_attestation != _ISOLATION_ATTESTATION
        or operator_invocation_attestation != _CANARY_OPERATOR_ATTESTATION
    ):
        raise ReviewError("qualitative review operational measurement is invalid")
    matrix_dir, review_dir = _separated_review_roots(matrix_dir, review_dir)
    with matrix.MatrixLock(review_dir):
        with matrix.MatrixLock(matrix_dir):
            try:
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except (judge.JudgmentError, inspection.InspectionError) as error:
                raise ReviewError(str(error)) from None
            ledger = _validate_review_root(matrix_dir, review_dir, target_index)
            attestation_path = review_dir / ledger["canary"]["attestation_path"]
            if _node_exists(attestation_path):
                document, digest = _validate_canary_terminal(review_dir, ledger)
                if document["status"] != "passed":
                    raise ReviewError("qualitative review canary is terminally failed")
                return {
                    "package_id": ledger["package_id"],
                    "status": "passed",
                    "sha256": digest,
                }
            if any((review_dir / "operations").iterdir()):
                raise ReviewError("qualitative review canary was recorded too late")
            try:
                canary = _read_json(
                    review_dir / ledger["canary"]["case_path"],
                    schema_name="review-case-0.2.json",
                    require_private=True,
                    require_canonical=True,
                )
                verdict, content = _read_json_with_content(
                    Path(verdicts_path),
                    schema_name="review-verdicts-0.2.json",
                    require_private=True,
                )
                expected_target = {
                    "work_item_id": canary["target"]["work_item_id"],
                    "projection_sha256": canary["target"]["projection_sha256"],
                }
                verdicts = verdict.get("verdicts")
                if (
                    verdict.get("target") != expected_target
                    or verdict.get("invocation")
                    != canary["execution"]["invocation"]
                    or not isinstance(verdicts, list)
                    or len(verdicts) != 1
                    or verdicts[0].get("criterion_id") != "schema-compatible"
                    or verdicts[0].get("verdict") != "pass"
                ):
                    raise ReviewError("qualitative review canary verdict is invalid")
                event_count, event_log_sha256 = _bound_event_log(
                    Path(events_path), content
                )
            except ReviewError:
                _publish_canary_failure(
                    review_dir,
                    ledger,
                    duration_ms=duration_ms,
                    isolation_attestation=isolation_attestation,
                    operator_invocation_attestation=(
                        operator_invocation_attestation
                    ),
                    failure_class="structural_failure",
                )
                raise
            evidence = {
                "validator": _EVENT_VALIDATOR,
                "event_log_sha256": event_log_sha256,
                "event_count": event_count,
                "verdict_bytes_sha256": hashlib.sha256(content).hexdigest(),
                "verdict_document_sha256": _sha256(verdict),
            }
            evidence["binding_sha256"] = _canary_evidence_binding(
                canary, evidence
            )
            attestation = {
                "schema": _CANARY_ATTESTATION_SCHEMA,
                "status": "passed",
                "package_id": ledger["package_id"],
                "matrix_id": ledger["matrix_id"],
                "response_schema_sha256": ledger["response_schema"]["sha256"],
                "case_sha256": ledger["canary"]["case_sha256"],
                "model": ledger["canary"]["model"],
                "reasoning_effort": ledger["canary"]["reasoning_effort"],
                "codex_version": ledger["canary"]["codex_version"],
                "isolation_attestation": isolation_attestation,
                "operator_invocation_attestation": (
                    operator_invocation_attestation
                ),
                "duration_ms": duration_ms,
                "invocation_evidence": evidence,
                "recorded_at": _now(),
            }
            _write_json(attestation_path, attestation)
            digest = _validate_canary_attestation(review_dir, ledger)
            return {
                "package_id": ledger["package_id"],
                "status": "passed",
                "sha256": digest,
            }


def record_canary_failure(
    matrix_dir: Path,
    review_dir: Path,
    *,
    failure_class: str,
    duration_ms: int,
    isolation_attestation: str,
    operator_invocation_attestation: str,
) -> dict[str, Any]:
    """Persist a terminal non-output canary failure."""

    if (
        failure_class not in {"provider_rejection", "transport_failure"}
        or not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or not 0 <= duration_ms <= _MAX_DURATION_MS
        or isolation_attestation != _ISOLATION_ATTESTATION
        or operator_invocation_attestation != _CANARY_OPERATOR_ATTESTATION
    ):
        raise ReviewError("qualitative review operational measurement is invalid")
    matrix_dir, review_dir = _separated_review_roots(matrix_dir, review_dir)
    with matrix.MatrixLock(review_dir):
        with matrix.MatrixLock(matrix_dir):
            try:
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except (judge.JudgmentError, inspection.InspectionError) as error:
                raise ReviewError(str(error)) from None
            ledger = _validate_review_root(matrix_dir, review_dir, target_index)
            if any((review_dir / "operations").iterdir()):
                raise ReviewError("qualitative review canary was recorded too late")
            document, digest = _publish_canary_failure(
                review_dir,
                ledger,
                duration_ms=duration_ms,
                isolation_attestation=isolation_attestation,
                operator_invocation_attestation=operator_invocation_attestation,
                failure_class=failure_class,
            )
            return {
                "package_id": ledger["package_id"],
                "status": document["status"],
                "sha256": digest,
            }


def validate_canary(matrix_dir: Path, review_dir: Path) -> dict[str, Any]:
    """Validate a persisted successful canary without invoking a model."""

    matrix_dir, review_dir = _separated_review_roots(matrix_dir, review_dir)
    with matrix.MatrixLock(review_dir):
        with matrix.MatrixLock(matrix_dir):
            try:
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except (judge.JudgmentError, inspection.InspectionError) as error:
                raise ReviewError(str(error)) from None
            ledger = _validate_review_root(matrix_dir, review_dir, target_index)
            document, digest = _validate_canary_terminal(review_dir, ledger)
    return {
        "package_id": ledger["package_id"],
        "status": document["status"],
        "sha256": digest,
    }


def record_measurement_amendment(
    matrix_dir: Path,
    review_dir: Path,
    work_item_id: str,
    *,
    judge_identifier: str,
    amendment_class: str,
) -> dict[str, Any]:
    """Publish the matrix's sole package-bound duration amendment."""

    if amendment_class not in _MEASUREMENT_AMENDMENT_CLASSES:
        raise ReviewError("qualitative review measurement amendment is invalid")
    matrix_dir, review_dir = _separated_review_roots(matrix_dir, review_dir)
    with matrix.MatrixLock(review_dir):
        with matrix.MatrixLock(matrix_dir):
            try:
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except (judge.JudgmentError, inspection.InspectionError) as error:
                raise ReviewError(str(error)) from None
            amendment = _load_measurement_amendment(matrix_dir)
            validation_context: dict[str, str] = {}
            ledger = _validate_review_root(
                matrix_dir,
                review_dir,
                target_index,
                amendment,
                validation_context,
            )
            canary_attestation_sha256 = _validate_canary_attestation(
                review_dir, ledger
            )
            case = _load_bound_case(
                matrix_dir,
                review_dir,
                target_index,
                ledger,
                work_item_id,
                judge_identifier,
            )
            if (
                amendment_class == "recorded_duration_unreliable"
                and target_index.inspection_result.manifest.campaign.required_tracks
                != ("skill",)
            ):
                raise ReviewError(
                    "qualitative review recorded duration is ineligible"
                )
            output = {
                "package_id": ledger["package_id"],
                "amendment_class": amendment_class,
                "work_item_id": work_item_id,
                "judge_identifier": judge_identifier,
                "effective_duration": {"state": "unavailable"},
            }
            if amendment is not None:
                document, digest = amendment
                if (
                    document.get("amendment_class") != amendment_class
                    or document.get("work_item_id") != work_item_id
                    or document.get("judge_identifier") != judge_identifier
                ):
                    raise ReviewError(
                        "qualitative review measurement amendment already exists"
                    )
                return {**output, "sha256": digest}

            operation_path = _operation_path(
                review_dir, "judgment", work_item_id, judge_identifier
            )
            retained = judge._retained_judgments(  # noqa: SLF001
                matrix_dir, target_index
            )
            matching = _matching_judgment_files(
                _retained_judgment_files(matrix_dir, retained),
                target=case["target"],
                judge_identifier=judge_identifier,
            )
            operation: dict[str, Any] | None = None
            operation_sha256: str | None = None
            if amendment_class == "interrupted_attempt_duration_unavailable":
                if _node_exists(operation_path) or matching:
                    raise ReviewError(
                        "qualitative review interrupted attempt is ineligible"
                    )
            else:
                operation, operation_content = _read_json_with_content(
                    operation_path,
                    require_private=True,
                    require_canonical=True,
                )
                artifact = _validate_judgment_operation(
                    operation,
                    case=case,
                    judge_identifier=judge_identifier,
                    canary_attestation_sha256=canary_attestation_sha256,
                )
                if (
                    len(matching) != 1
                    or matching[0][1] != operation["artifact_sha256"]
                    or matching[0][2] != artifact
                ):
                    raise ReviewError(
                        "qualitative review recorded duration is ineligible"
                    )
                operation_sha256 = hashlib.sha256(operation_content).hexdigest()
            document = _measurement_amendment_document(
                result=target_index.inspection_result,
                ledger=ledger,
                ledger_sha256=validation_context["ledger_sha256"],
                registry_sha256=validation_context["registry_sha256"],
                case=case,
                judge_identifier=judge_identifier,
                canary_attestation_sha256=canary_attestation_sha256,
                amendment_class=amendment_class,
                operation=operation,
                operation_sha256=operation_sha256,
            )
            digest = _sha256(document)
            _write_json(matrix_dir / _MEASUREMENT_AMENDMENT_FILENAME, document)
            _validate_review_root(
                matrix_dir,
                review_dir,
                target_index,
                (document, digest),
            )
            return {**output, "sha256": digest}


def _case_entry(
    ledger: dict[str, Any], work_item_id: str, judge_identifier: str
) -> dict[str, Any]:
    matches = [
        item
        for item in ledger["cases"]
        if isinstance(item, dict)
        and item.get("work_item_id") == work_item_id
        and item.get("judge_identifier") == judge_identifier
    ]
    if len(matches) != 1:
        raise ReviewError("qualitative review case is unavailable")
    item = matches[0]
    if set(item) != {"work_item_id", "judge_identifier", "path", "sha256"}:
        raise ReviewError("qualitative review ledger is invalid")
    return item


def _load_bound_case(
    matrix_dir: Path,
    review_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    ledger: dict[str, Any],
    work_item_id: str,
    judge_identifier: str,
) -> dict[str, Any]:
    if _SAFE_WORK_ITEM.fullmatch(work_item_id) is None:
        raise ReviewError("qualitative review work item is invalid")
    entry = _case_entry(ledger, work_item_id, judge_identifier)
    if entry["path"] != f"cases/{work_item_id}-{judge_identifier}.json":
        raise ReviewError("qualitative review case path is invalid")
    case_path = review_dir / entry["path"]
    document = _read_json(
        case_path,
        schema_name="review-case-0.2.json",
        require_private=True,
        require_canonical=True,
    )
    expected = _case_document(
        matrix_dir,
        target_index,
        work_item_id,
        judge_identifier,
        ledger["package_id"],
    )
    if document != expected or _sha256(document) != entry["sha256"]:
        raise ReviewError("qualitative review case does not match matrix")
    return document


def _operation_path(
    review_dir: Path, kind: str, work_item_id: str, suffix: str
) -> Path:
    return review_dir / "operations" / f"{kind}-{work_item_id}-{suffix}.json"


def _publish_operation(path: Path, document: dict[str, Any]) -> None:
    if path.exists():
        retained = _read_json(path, require_private=True)
        if retained != document:
            raise ReviewError("qualitative review operation already exists")
        return
    _write_json(path, document)


def _invocation_evidence_binding(
    case: dict[str, Any], artifact: dict[str, Any], evidence: dict[str, Any]
) -> str:
    return _sha256(
        {
            "case_sha256": _sha256(case),
            "artifact_sha256": _sha256(artifact),
            "invocation_evidence": {
                key: value for key, value in evidence.items() if key != "binding_sha256"
            },
        }
    )


def _validate_judgment_operation(
    operation: dict[str, Any],
    *,
    case: dict[str, Any],
    judge_identifier: str,
    canary_attestation_sha256: str | None = None,
) -> dict[str, Any]:
    required = {
        "schema",
        "package_id",
        "kind",
        "matrix_id",
        "work_item_id",
        "judge_identifier",
        "attempt_count",
        "duration_ms",
        "usage",
        "isolation_attestation",
        "invocation_evidence",
        "canary_attestation_sha256",
        "case_sha256",
        "artifact_sha256",
        "artifact",
        "recorded_at",
    }
    artifact = operation.get("artifact")
    artifact_judge = artifact.get("judge") if isinstance(artifact, dict) else None
    evidence = operation.get("invocation_evidence")
    bound_verdict = (
        {
            "schema": _VERDICTS_SCHEMA,
            "target": {
                "work_item_id": case["target"]["work_item_id"],
                "projection_sha256": case["target"]["projection_sha256"],
            },
            "invocation": case["execution"]["invocation"],
            "verdicts": artifact.get("verdicts"),
        }
        if isinstance(artifact, dict)
        else None
    )
    if (
        set(operation) != required
        or operation.get("schema") != _OPERATION_SCHEMA
        or operation.get("package_id") != case.get("package_id")
        or operation.get("kind") != "judgment_import"
        or operation.get("matrix_id") != case["target"]["matrix_id"]
        or operation.get("work_item_id") != case["target"]["work_item_id"]
        or operation.get("judge_identifier") != judge_identifier
        or not isinstance(operation.get("attempt_count"), int)
        or isinstance(operation.get("attempt_count"), bool)
        or not 1 <= operation["attempt_count"] <= _MAX_ATTEMPTS
        or not isinstance(operation.get("duration_ms"), int)
        or isinstance(operation.get("duration_ms"), bool)
        or not 0 <= operation["duration_ms"] <= _MAX_DURATION_MS
        or operation.get("usage") != {"state": "unavailable"}
        or operation.get("isolation_attestation") != _ISOLATION_ATTESTATION
        or not isinstance(evidence, dict)
        or set(evidence)
        != {
            "validator",
            "event_log_sha256",
            "event_count",
            "verdict_bytes_sha256",
            "verdict_document_sha256",
            "binding_sha256",
        }
        or evidence.get("validator") != _EVENT_VALIDATOR
        or not isinstance(evidence.get("event_log_sha256"), str)
        or _SHA256.fullmatch(evidence["event_log_sha256"]) is None
        or not isinstance(evidence.get("event_count"), int)
        or isinstance(evidence.get("event_count"), bool)
        or not 4 <= evidence["event_count"] <= _MAX_DOCUMENT_BYTES
        or not isinstance(evidence.get("verdict_bytes_sha256"), str)
        or _SHA256.fullmatch(evidence["verdict_bytes_sha256"]) is None
        or not isinstance(evidence.get("verdict_document_sha256"), str)
        or _SHA256.fullmatch(evidence["verdict_document_sha256"]) is None
        or bound_verdict is None
        or evidence["verdict_document_sha256"] != _sha256(bound_verdict)
        or not isinstance(evidence.get("binding_sha256"), str)
        or evidence["binding_sha256"]
        != _invocation_evidence_binding(case, artifact, evidence)
        or not isinstance(operation.get("canary_attestation_sha256"), str)
        or _SHA256.fullmatch(operation["canary_attestation_sha256"]) is None
        or (
            canary_attestation_sha256 is not None
            and operation["canary_attestation_sha256"]
            != canary_attestation_sha256
        )
        or operation.get("case_sha256") != _sha256(case)
        or not isinstance(artifact, dict)
        or artifact.get("target") != case["target"]
        or artifact.get("judgment_id")
        != f"judgment-{case['target']['work_item_id']}-{judge_identifier}"
        or not isinstance(artifact_judge, dict)
        or artifact_judge.get("identifier") != judge_identifier
        or operation.get("artifact_sha256") != _sha256(artifact)
        or not _valid_timestamp(operation.get("recorded_at"))
    ):
        raise ReviewError("qualitative review operation is invalid")
    return artifact


def _load_measurement_amendment(
    matrix_dir: Path,
) -> tuple[dict[str, Any], str] | None:
    path = matrix_dir / _MEASUREMENT_AMENDMENT_FILENAME
    if not _node_exists(path):
        return None
    document, content = _read_json_with_content(
        path,
        require_private=True,
        require_canonical=True,
    )
    return document, hashlib.sha256(content).hexdigest()


def _measurement_amendment_document(
    *,
    result: inspection.MatrixInspection,
    ledger: dict[str, Any],
    ledger_sha256: str,
    registry_sha256: str,
    case: dict[str, Any],
    judge_identifier: str,
    canary_attestation_sha256: str,
    amendment_class: str,
    operation: dict[str, Any] | None,
    operation_sha256: str | None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": _MEASUREMENT_AMENDMENT_SCHEMA,
        "amendment_class": amendment_class,
        "matrix_id": result.manifest.matrix_id,
        "manifest_sha256": result.manifest_sha256,
        "package_id": ledger["package_id"],
        "registry_sha256": registry_sha256,
        "ledger_sha256": ledger_sha256,
        "work_item_id": case["target"]["work_item_id"],
        "judge_identifier": judge_identifier,
        "case_sha256": _sha256(case),
        "canary_attestation_sha256": canary_attestation_sha256,
        "affected_attempt_count": (
            1 if operation is None else operation["attempt_count"]
        ),
        "authorized_attempt_count": 2 if operation is None else None,
        "operation_path": (
            f"operations/judgment-{case['target']['work_item_id']}-"
            f"{judge_identifier}.json"
        ),
        "operation_sha256": operation_sha256,
        "artifact_sha256": (
            None if operation is None else operation["artifact_sha256"]
        ),
        "recorded_duration_ms": (
            None if operation is None else operation["duration_ms"]
        ),
        "effective_duration": {"state": "unavailable"},
        "recorded_at": _now() if recorded_at is None else recorded_at,
    }


def _validate_unavailable_judgment_operation(
    operation: dict[str, Any],
    *,
    case: dict[str, Any],
    judge_identifier: str,
    canary_attestation_sha256: str,
    amendment_sha256: str,
) -> dict[str, Any]:
    if (
        operation.get("schema") != _UNAVAILABLE_OPERATION_SCHEMA
        or operation.get("attempt_count") != 2
        or operation.get("duration")
        != {"state": "unavailable", "amendment_sha256": amendment_sha256}
        or "duration_ms" in operation
    ):
        raise ReviewError("qualitative review operation is invalid")
    shadow = dict(operation)
    shadow["schema"] = _OPERATION_SCHEMA
    shadow["duration_ms"] = 0
    shadow.pop("duration")
    return _validate_judgment_operation(
        shadow,
        case=case,
        judge_identifier=judge_identifier,
        canary_attestation_sha256=canary_attestation_sha256,
    )


def _amendment_targets_slot(
    measurement_amendment: tuple[dict[str, Any], str] | None,
    case: dict[str, Any],
    judge_identifier: str,
) -> bool:
    if measurement_amendment is None:
        return False
    document = measurement_amendment[0]
    return (
        document.get("work_item_id") == case["target"]["work_item_id"]
        and document.get("judge_identifier") == judge_identifier
        and document.get("case_sha256") == _sha256(case)
    )


def _effective_judgment_operation(
    operation: dict[str, Any],
    *,
    case: dict[str, Any],
    judge_identifier: str,
    canary_attestation_sha256: str,
    measurement_amendment: tuple[dict[str, Any], str] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    targeted = _amendment_targets_slot(
        measurement_amendment,
        case,
        judge_identifier,
    )
    amendment_class = (
        measurement_amendment[0].get("amendment_class")
        if targeted and measurement_amendment is not None
        else None
    )
    if operation.get("schema") == _UNAVAILABLE_OPERATION_SCHEMA:
        if amendment_class != "interrupted_attempt_duration_unavailable":
            raise ReviewError("qualitative review operation is invalid")
        artifact = _validate_unavailable_judgment_operation(
            operation,
            case=case,
            judge_identifier=judge_identifier,
            canary_attestation_sha256=canary_attestation_sha256,
            amendment_sha256=measurement_amendment[1],
        )
        return artifact, {
            "state": "unavailable",
            "amendment_sha256": measurement_amendment[1],
        }
    if amendment_class == "interrupted_attempt_duration_unavailable":
        raise ReviewError("qualitative review operation is invalid")
    artifact = _validate_judgment_operation(
        operation,
        case=case,
        judge_identifier=judge_identifier,
        canary_attestation_sha256=canary_attestation_sha256,
    )
    if amendment_class == "recorded_duration_unreliable":
        if (
            measurement_amendment[0].get("operation_sha256")
            != _sha256(operation)
            or measurement_amendment[0].get("artifact_sha256")
            != operation["artifact_sha256"]
            or measurement_amendment[0].get("affected_attempt_count")
            != operation["attempt_count"]
            or measurement_amendment[0].get("recorded_duration_ms")
            != operation["duration_ms"]
        ):
            raise ReviewError("qualitative review measurement amendment is invalid")
        return artifact, {
            "state": "unavailable",
            "amendment_sha256": measurement_amendment[1],
        }
    return artifact, {"state": "measured", "duration_ms": operation["duration_ms"]}


def _validate_measurement_amendment(
    matrix_dir: Path,
    review_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    ledger: dict[str, Any],
    registry_sha256: str,
    ledger_sha256: str,
    canary_attestation_sha256: str | None,
    measurement_amendment: tuple[dict[str, Any], str] | None,
) -> None:
    if measurement_amendment is None:
        return
    document, _ = measurement_amendment
    amendment_class = document.get("amendment_class")
    required = {
        "schema",
        "amendment_class",
        "matrix_id",
        "manifest_sha256",
        "package_id",
        "registry_sha256",
        "ledger_sha256",
        "work_item_id",
        "judge_identifier",
        "case_sha256",
        "canary_attestation_sha256",
        "affected_attempt_count",
        "authorized_attempt_count",
        "operation_path",
        "operation_sha256",
        "artifact_sha256",
        "recorded_duration_ms",
        "effective_duration",
        "recorded_at",
    }
    if (
        set(document) != required
        or document.get("schema") != _MEASUREMENT_AMENDMENT_SCHEMA
        or amendment_class not in _MEASUREMENT_AMENDMENT_CLASSES
        or document.get("matrix_id")
        != target_index.inspection_result.manifest.matrix_id
        or document.get("manifest_sha256")
        != target_index.inspection_result.manifest_sha256
        or document.get("package_id") != ledger["package_id"]
        or document.get("registry_sha256") != registry_sha256
        or document.get("ledger_sha256") != ledger_sha256
        or not isinstance(document.get("work_item_id"), str)
        or not isinstance(document.get("judge_identifier"), str)
        or document.get("canary_attestation_sha256")
        != canary_attestation_sha256
        or document.get("effective_duration") != {"state": "unavailable"}
        or not _valid_timestamp(document.get("recorded_at"))
        or canary_attestation_sha256 is None
    ):
        raise ReviewError("qualitative review measurement amendment is invalid")
    case = _load_bound_case(
        matrix_dir,
        review_dir,
        target_index,
        ledger,
        document["work_item_id"],
        document["judge_identifier"],
    )
    if document.get("case_sha256") != _sha256(case):
        raise ReviewError("qualitative review measurement amendment is invalid")
    operation_path = _operation_path(
        review_dir,
        "judgment",
        document["work_item_id"],
        document["judge_identifier"],
    )
    if document.get("operation_path") != operation_path.relative_to(
        review_dir
    ).as_posix():
        raise ReviewError("qualitative review measurement amendment is invalid")
    retained = judge._retained_judgments(matrix_dir, target_index)  # noqa: SLF001
    matching = _matching_judgment_files(
        _retained_judgment_files(matrix_dir, retained),
        target=case["target"],
        judge_identifier=document["judge_identifier"],
    )
    if amendment_class == "recorded_duration_unreliable":
        if (
            target_index.inspection_result.manifest.campaign.required_tracks
            != ("skill",)
            or not _node_exists(operation_path)
            or not isinstance(document.get("affected_attempt_count"), int)
            or isinstance(document.get("affected_attempt_count"), bool)
            or not 1 <= document["affected_attempt_count"] <= _MAX_ATTEMPTS
            or document.get("authorized_attempt_count") is not None
            or not isinstance(document.get("operation_sha256"), str)
            or _SHA256.fullmatch(document["operation_sha256"]) is None
            or not isinstance(document.get("artifact_sha256"), str)
            or _SHA256.fullmatch(document["artifact_sha256"]) is None
            or not isinstance(document.get("recorded_duration_ms"), int)
            or isinstance(document.get("recorded_duration_ms"), bool)
            or len(matching) != 1
            or matching[0][1] != document["artifact_sha256"]
        ):
            raise ReviewError("qualitative review measurement amendment is invalid")
    elif (
        not isinstance(document.get("affected_attempt_count"), int)
        or isinstance(document.get("affected_attempt_count"), bool)
        or document["affected_attempt_count"] != 1
        or not isinstance(document.get("authorized_attempt_count"), int)
        or isinstance(document.get("authorized_attempt_count"), bool)
        or document["authorized_attempt_count"] != 2
        or document.get("operation_sha256") is not None
        or document.get("artifact_sha256") is not None
        or document.get("recorded_duration_ms") is not None
        or (not _node_exists(operation_path) and matching)
    ):
        raise ReviewError("qualitative review measurement amendment is invalid")


def _validate_adjudication_operation(
    operation: dict[str, Any],
    *,
    case: dict[str, Any],
    canary_attestation_sha256: str | None = None,
) -> dict[str, Any]:
    required = {
        "schema",
        "package_id",
        "kind",
        "matrix_id",
        "work_item_id",
        "canary_attestation_sha256",
        "case_sha256",
        "artifact_sha256",
        "artifact",
        "recorded_at",
    }
    artifact = operation.get("artifact")
    if (
        set(operation) != required
        or operation.get("schema") != _OPERATION_SCHEMA
        or operation.get("package_id") != case.get("package_id")
        or operation.get("kind") != "adjudication_import"
        or operation.get("matrix_id") != case["target"]["matrix_id"]
        or operation.get("work_item_id") != case["target"]["work_item_id"]
        or not isinstance(operation.get("canary_attestation_sha256"), str)
        or _SHA256.fullmatch(operation["canary_attestation_sha256"]) is None
        or (
            canary_attestation_sha256 is not None
            and operation["canary_attestation_sha256"]
            != canary_attestation_sha256
        )
        or operation.get("case_sha256") != _sha256(case)
        or not isinstance(artifact, dict)
        or artifact.get("target") != case["target"]
        or artifact.get("adjudication_id")
        != f"adjudication-{case['target']['work_item_id']}"
        or operation.get("artifact_sha256") != _sha256(artifact)
        or not _valid_timestamp(operation.get("recorded_at"))
    ):
        raise ReviewError("qualitative review operation is invalid")
    return artifact


def _import_document_locked(
    matrix_dir: Path, kind: str, document: dict[str, Any]
) -> tuple[Path, str]:
    """Import a validated artifact while the caller holds the matrix lock."""

    with tempfile.TemporaryDirectory(prefix="steam-agent-review-import-") as name:
        root = Path(name)
        root.chmod(0o700)
        source = root / f"{kind}.json"
        _write_json(source, document)
        if kind == "judgment":
            return judge._import_judgment_locked(matrix_dir, source)  # noqa: SLF001
        return judge._import_adjudication_locked(matrix_dir, source)  # noqa: SLF001


def _retained_target(
    path: Path,
    document: dict[str, Any],
    digest: str,
    *,
    kind: str,
) -> tuple[Path, str] | None:
    """Return an exact private retained target, or ``None`` when absent."""

    try:
        item_stat = path.lstat()
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise ReviewError(f"retained {kind} is unavailable") from None
    if (
        not stat.S_ISREG(item_stat.st_mode)
        or stat.S_IMODE(item_stat.st_mode) != 0o600
        or content != _canonical_bytes(document)
        or hashlib.sha256(content).hexdigest() != digest
    ):
        raise ReviewError(f"retained {kind} does not match review operation")
    return path, digest


def _reject_existing_target(path: Path, *, kind: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ReviewError(f"qualitative {kind} target is unavailable") from None
    raise ReviewError(f"qualitative {kind} target already exists")


def _retained_judgment_files(
    matrix_dir: Path,
    retained: dict[str, dict[str, Any]],
) -> list[tuple[Path, str, dict[str, Any]]]:
    """Preserve every validated retained file, including duplicate digests."""

    root = matrix_dir / "judgments"
    if not root.is_dir():
        return []
    files: list[tuple[Path, str, dict[str, Any]]] = []
    for path in sorted(root.glob("*.json")):
        document = _read_json(
            path,
            schema_name="judgment-0.1.json",
            require_private=True,
            require_canonical=True,
        )
        digest = _sha256(document)
        if retained.get(digest) != document:
            raise ReviewError("retained judgment does not match validated roster")
        files.append((path, digest, document))
    return files


def _retained_adjudication_files(
    matrix_dir: Path,
    target_index: judge._TargetIndex,  # noqa: SLF001
    retained_judgments: dict[str, dict[str, Any]],
) -> list[tuple[Path, str, dict[str, Any]]]:
    root = matrix_dir / "adjudications"
    if not root.is_dir():
        return []
    files: list[tuple[Path, str, dict[str, Any]]] = []
    for path in sorted(root.glob("*.json")):
        document = _read_json(
            path,
            schema_name="adjudication-0.1.json",
            require_private=True,
            require_canonical=True,
        )
        judge._validate_adjudication_document(  # noqa: SLF001
            matrix_dir,
            target_index,
            document,
            retained=retained_judgments,
        )
        files.append((path, _sha256(document), document))
    return files


def _matching_judgment_files(
    files: list[tuple[Path, str, dict[str, Any]]],
    *,
    target: dict[str, Any],
    judge_identifier: str,
) -> list[tuple[Path, str, dict[str, Any]]]:
    return [
        (path, digest, document)
        for path, digest, document in files
        if document["target"] == target
        and document["judge"]["identifier"] == judge_identifier
    ]


def _bound_judgment_roster(
    review_dir: Path,
    *,
    cases_by_judge: dict[str, dict[str, Any]],
    campaign: run_state.MatrixCampaign,
    files: list[tuple[Path, str, dict[str, Any]]],
    canary_attestation_sha256: str,
    measurement_amendment: tuple[dict[str, Any], str] | None = None,
) -> dict[str, tuple[str, dict[str, Any]]]:
    configured = {item.identifier: item for item in campaign.judges}
    if set(cases_by_judge) != set(configured):
        raise ReviewError("qualitative review case roster is incomplete")
    case = cases_by_judge[campaign.judges[0].identifier]
    by_judge: dict[str, tuple[str, dict[str, Any]]] = {}
    for _path, digest, document in files:
        if document["target"] != case["target"]:
            continue
        identifier = document["judge"]["identifier"]
        if identifier not in configured or identifier in by_judge:
            raise ReviewError("qualitative judgment roster is ambiguous")
        by_judge[identifier] = (digest, document)
    if set(by_judge) != set(configured):
        raise ReviewError(
            f"qualitative judgment roster is incomplete for "
            f"{case['target']['work_item_id']}"
        )
    for judge_config in campaign.judges:
        operation = _read_json(
            _operation_path(
                review_dir,
                "judgment",
                case["target"]["work_item_id"],
                judge_config.identifier,
            ),
            require_private=True,
        )
        operation_artifact, _effective_duration = _effective_judgment_operation(
            operation,
            case=cases_by_judge[judge_config.identifier],
            judge_identifier=judge_config.identifier,
            canary_attestation_sha256=canary_attestation_sha256,
            measurement_amendment=measurement_amendment,
        )
        digest, retained_artifact = by_judge[judge_config.identifier]
        if (
            operation.get("artifact_sha256") != digest
            or operation_artifact != retained_artifact
        ):
            raise ReviewError("qualitative judgment does not match operation ledger")
    return by_judge


def assemble_judgment(
    matrix_dir: Path,
    review_dir: Path,
    work_item_id: str,
    verdicts_path: Path,
    *,
    events_path: Path | None = None,
    judge_identifier: str,
    attempt_count: int,
    duration_ms: int | None,
    isolation_attestation: str,
) -> dict[str, Any]:
    """Validate external verdicts, assemble judgment 0.1, and import it."""

    if (
        not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or not 1 <= attempt_count <= _MAX_ATTEMPTS
        or (
            duration_ms is not None
            and (
                not isinstance(duration_ms, int)
                or isinstance(duration_ms, bool)
                or not 0 <= duration_ms <= _MAX_DURATION_MS
            )
        )
        or isolation_attestation != _ISOLATION_ATTESTATION
    ):
        raise ReviewError("qualitative review operational measurement is invalid")
    matrix_dir, review_dir = _separated_review_roots(matrix_dir, review_dir)
    with matrix.MatrixLock(review_dir):
        with matrix.MatrixLock(matrix_dir):
            try:
                judge._reject_finalized_screen(matrix_dir)  # noqa: SLF001
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except judge.JudgmentError as error:
                raise ReviewError(str(error)) from None
            measurement_amendment = _load_measurement_amendment(matrix_dir)
            ledger = _validate_review_root(
                matrix_dir,
                review_dir,
                target_index,
                measurement_amendment,
            )
            canary_attestation_sha256 = _validate_canary_attestation(
                review_dir, ledger
            )
            campaign = target_index.inspection_result.manifest.campaign
            judges = [
                item for item in campaign.judges if item.identifier == judge_identifier
            ]
            if len(judges) != 1:
                raise ReviewError("qualitative review judge is not configured")
            case = _load_bound_case(
                matrix_dir,
                review_dir,
                target_index,
                ledger,
                work_item_id,
                judge_identifier,
            )
            retained_judgments = judge._retained_judgments(  # noqa: SLF001
                matrix_dir, target_index
            )
            retained_files = _retained_judgment_files(matrix_dir, retained_judgments)
            matching_files = _matching_judgment_files(
                retained_files,
                target=case["target"],
                judge_identifier=judge_identifier,
            )
            operation_path = _operation_path(
                review_dir, "judgment", work_item_id, judge_identifier
            )

            # The durable operation is the recovery source. Resuming must not
            # depend on the disposable external response still existing.
            if operation_path.exists():
                operation = _read_json(operation_path, require_private=True)
                document, effective_duration = _effective_judgment_operation(
                    operation,
                    case=case,
                    judge_identifier=judge_identifier,
                    canary_attestation_sha256=canary_attestation_sha256,
                    measurement_amendment=measurement_amendment,
                )
                if (
                    operation["attempt_count"] != attempt_count
                    or (
                        effective_duration["state"] == "measured"
                        and operation["duration_ms"] != duration_ms
                    )
                    or (
                        effective_duration["state"] == "unavailable"
                        and duration_ms is not None
                    )
                    or operation["isolation_attestation"] != isolation_attestation
                ):
                    raise ReviewError(
                        "qualitative review operational measurement does not match "
                        "operation"
                    )
                judge._validate_judgment_document(  # noqa: SLF001
                    target_index, document
                )
                digest = operation["artifact_sha256"]
                if len(matching_files) > 1:
                    raise ReviewError("qualitative judgment roster is ambiguous")
                if matching_files:
                    path, retained_digest, retained_document = matching_files[0]
                    if retained_digest != digest or retained_document != document:
                        raise ReviewError(
                            "retained judgment does not match review operation"
                        )
                    if path.name != f"{document['judgment_id']}.json":
                        raise ReviewError("retained judgment filename is invalid")
                    retained = _retained_target(path, document, digest, kind="judgment")
                    if retained is None:
                        raise ReviewError("retained judgment is unavailable")
                    output = {"path": retained[0].name, "sha256": retained[1]}
                    if effective_duration["state"] == "unavailable":
                        output["duration"] = effective_duration
                    return output
                target = matrix_dir / "judgments" / f"{document['judgment_id']}.json"
                retained = _retained_target(target, document, digest, kind="judgment")
                if retained is None:
                    retained = _import_document_locked(matrix_dir, "judgment", document)
                output = {"path": retained[0].name, "sha256": retained[1]}
                if effective_duration["state"] == "unavailable":
                    output["duration"] = effective_duration
                return output

            if matching_files:
                raise ReviewError("qualitative judgment already exists for judge")
            discovery_amendment = (
                measurement_amendment is not None
                and _amendment_targets_slot(
                    measurement_amendment,
                    case,
                    judge_identifier,
                )
                and measurement_amendment[0].get("amendment_class")
                == "interrupted_attempt_duration_unavailable"
            )
            if discovery_amendment:
                if attempt_count != 2 or duration_ms is not None:
                    raise ReviewError(
                        "qualitative review operational measurement is invalid"
                    )
            elif duration_ms is None:
                raise ReviewError(
                    "qualitative review operational measurement is invalid"
                )
            verdict_document, verdict_content = _read_json_with_content(
                Path(verdicts_path),
                schema_name="review-verdicts-0.2.json",
                require_private=True,
            )
            expected_response_target = {
                "work_item_id": case["target"]["work_item_id"],
                "projection_sha256": case["target"]["projection_sha256"],
            }
            if verdict_document["target"] != expected_response_target:
                raise ReviewError("qualitative verdicts target a different case")
            if verdict_document["invocation"] != case["execution"]["invocation"]:
                raise ReviewError("qualitative verdicts target a different invocation")
            criteria = [item["id"] for item in case["projection"]["criteria"]]
            verdict_map = judge._criterion_map(  # noqa: SLF001
                verdict_document["verdicts"], field="verdict"
            )
            if set(verdict_map) != set(criteria):
                raise ReviewError("qualitative verdicts do not cover the exact rubric")
            ordered_verdicts = sorted(
                verdict_document["verdicts"],
                key=lambda item: criteria.index(item["criterion_id"]),
            )
            if events_path is None:
                raise ReviewError(
                    "qualitative review invocation evidence is unavailable"
                )
            event_count, event_log_sha256 = _bound_event_log(
                Path(events_path), verdict_content
            )
            bound_verdict = {
                "schema": _VERDICTS_SCHEMA,
                "target": expected_response_target,
                "invocation": case["execution"]["invocation"],
                "verdicts": ordered_verdicts,
            }
            document = {
                "schema": "steam-agent-eval-judgment/0.1",
                "judgment_id": f"judgment-{work_item_id}-{judge_identifier}",
                "target": case["target"],
                "judge": judges[0].to_dict(),
                "prompt": {
                    "version": campaign.prompt_version,
                    "sha256": campaign.prompt_sha256,
                },
                "parser": {
                    "version": campaign.parser_version,
                    "sha256": campaign.parser_sha256,
                },
                "presentation": case["presentation"],
                "verdicts": ordered_verdicts,
                "created_at": _now(),
            }
            judge._validate_judgment_document(  # noqa: SLF001
                target_index, document
            )
            artifact_sha256 = _sha256(document)
            target = matrix_dir / "judgments" / f"{document['judgment_id']}.json"
            _reject_existing_target(target, kind="judgment")
            invocation_evidence = {
                "validator": _EVENT_VALIDATOR,
                "event_log_sha256": event_log_sha256,
                "event_count": event_count,
                "verdict_bytes_sha256": hashlib.sha256(verdict_content).hexdigest(),
                "verdict_document_sha256": _sha256(bound_verdict),
            }
            invocation_evidence["binding_sha256"] = _invocation_evidence_binding(
                case, document, invocation_evidence
            )
            operation = {
                "schema": (
                    _UNAVAILABLE_OPERATION_SCHEMA
                    if discovery_amendment
                    else _OPERATION_SCHEMA
                ),
                "package_id": ledger["package_id"],
                "kind": "judgment_import",
                "matrix_id": case["target"]["matrix_id"],
                "work_item_id": work_item_id,
                "judge_identifier": judge_identifier,
                "attempt_count": attempt_count,
                "usage": {"state": "unavailable"},
                "isolation_attestation": isolation_attestation,
                "invocation_evidence": invocation_evidence,
                "canary_attestation_sha256": canary_attestation_sha256,
                "case_sha256": _sha256(case),
                "artifact_sha256": artifact_sha256,
                "artifact": document,
                "recorded_at": _now(),
            }
            if discovery_amendment:
                operation["duration"] = {
                    "state": "unavailable",
                    "amendment_sha256": measurement_amendment[1],
                }
            else:
                operation["duration_ms"] = duration_ms
            _publish_operation(operation_path, operation)
            path, digest = _import_document_locked(matrix_dir, "judgment", document)
            output = {"path": path.name, "sha256": digest}
            if discovery_amendment:
                output["duration"] = operation["duration"]
            return output


def _existing_operation_plan(
    path: Path,
    matrix_dir: Path,
    *,
    case: dict[str, Any],
    target_index: judge._TargetIndex,  # noqa: SLF001
    retained: dict[str, dict[str, Any]],
    matching_files: list[tuple[Path, str, dict[str, Any]]],
    canary_attestation_sha256: str,
) -> tuple[str, dict[str, Any]] | None:
    if not path.exists():
        return None
    operation = _read_json(path, require_private=True)
    artifact = _validate_adjudication_operation(
        operation,
        case=case,
        canary_attestation_sha256=canary_attestation_sha256,
    )
    judge._validate_adjudication_document(  # noqa: SLF001
        matrix_dir, target_index, artifact, retained=retained
    )
    digest = operation["artifact_sha256"]
    if len(matching_files) > 1:
        raise ReviewError("qualitative adjudication roster is ambiguous")
    if matching_files:
        target, retained_digest, retained_artifact = matching_files[0]
        if retained_digest != digest or retained_artifact != artifact:
            raise ReviewError("retained adjudication does not match review operation")
        if target.name != f"{artifact['adjudication_id']}.json":
            raise ReviewError("retained adjudication filename is invalid")
        retained_target = _retained_target(
            target, artifact, digest, kind="adjudication"
        )
        if retained_target is None:
            raise ReviewError("retained adjudication is unavailable")
        return "retained", artifact
    target = matrix_dir / "adjudications" / f"{artifact.get('adjudication_id')}.json"
    retained_target = _retained_target(target, artifact, digest, kind="adjudication")
    if retained_target is not None:
        return "retained", artifact
    return "resume", artifact


def resolve_agreement(matrix_dir: Path, review_dir: Path) -> dict[str, Any]:
    """Import one mechanical agreement adjudication for every complete roster."""

    matrix_dir, review_dir = _separated_review_roots(matrix_dir, review_dir)
    imported = 0
    retained_count = 0
    with matrix.MatrixLock(review_dir):
        with matrix.MatrixLock(matrix_dir):
            try:
                judge._reject_finalized_screen(matrix_dir)  # noqa: SLF001
                target_index = judge._target_index(matrix_dir)  # noqa: SLF001
            except judge.JudgmentError as error:
                raise ReviewError(str(error)) from None
            measurement_amendment = _load_measurement_amendment(matrix_dir)
            ledger = _validate_review_root(
                matrix_dir,
                review_dir,
                target_index,
                measurement_amendment,
            )
            canary_attestation_sha256 = _validate_canary_attestation(
                review_dir, ledger
            )
            retained = judge._retained_judgments(  # noqa: SLF001
                matrix_dir, target_index
            )
            retained_files = _retained_judgment_files(matrix_dir, retained)
            retained_adjudications = _retained_adjudication_files(
                matrix_dir, target_index, retained
            )
            campaign = target_index.inspection_result.manifest.campaign
            plans: list[tuple[str, Path, dict[str, Any]]] = []
            for work_item in target_index.inspection_result.manifest.work_items:
                work_item_id = work_item.work_item_id
                cases_by_judge = {
                    judge_config.identifier: _load_bound_case(
                        matrix_dir,
                        review_dir,
                        target_index,
                        ledger,
                        work_item_id,
                        judge_config.identifier,
                    )
                    for judge_config in campaign.judges
                }
                case = cases_by_judge[campaign.judges[0].identifier]
                operation_path = _operation_path(
                    review_dir, "adjudication", work_item_id, "agreement"
                )
                roster_arguments: dict[str, Any] = {}
                if measurement_amendment is not None:
                    roster_arguments["measurement_amendment"] = measurement_amendment
                by_judge = _bound_judgment_roster(
                    review_dir,
                    cases_by_judge=cases_by_judge,
                    campaign=campaign,
                    files=retained_files,
                    canary_attestation_sha256=canary_attestation_sha256,
                    **roster_arguments,
                )
                matching_adjudications = [
                    item
                    for item in retained_adjudications
                    if item[2]["target"] == case["target"]
                ]
                existing = _existing_operation_plan(
                    operation_path,
                    matrix_dir,
                    case=case,
                    target_index=target_index,
                    retained=retained,
                    matching_files=matching_adjudications,
                    canary_attestation_sha256=canary_attestation_sha256,
                )
                if existing is not None:
                    action, artifact = existing
                    plans.append((action, operation_path, artifact))
                    continue
                if matching_adjudications:
                    raise ReviewError("qualitative adjudication already exists")
                hashes = [by_judge[item.identifier][0] for item in campaign.judges]
                verdict_maps = [
                    judge._criterion_map(  # noqa: SLF001
                        by_judge[item.identifier][1]["verdicts"], field="verdict"
                    )
                    for item in campaign.judges
                ]
                criteria = [item["id"] for item in case["projection"]["criteria"]]
                outcomes = []
                for criterion_id in criteria:
                    values = {item[criterion_id] for item in verdict_maps}
                    outcome = (
                        next(iter(values))
                        if len(values) == 1 and "uncertain" not in values
                        else "unresolved"
                    )
                    outcomes.append({"criterion_id": criterion_id, "outcome": outcome})
                document = {
                    "schema": "steam-agent-eval-adjudication/0.1",
                    "adjudication_id": f"adjudication-{work_item_id}",
                    "target": case["target"],
                    "method": campaign.adjudication_method,
                    "adjudicator": campaign.adjudicator,
                    "judgment_sha256s": hashes,
                    "outcomes": outcomes,
                    "created_at": _now(),
                }
                judge._validate_adjudication_document(  # noqa: SLF001
                    matrix_dir,
                    target_index,
                    document,
                    retained=retained,
                )
                target = (
                    matrix_dir / "adjudications" / f"{document['adjudication_id']}.json"
                )
                _reject_existing_target(target, kind="adjudication")
                operation = {
                    "schema": _OPERATION_SCHEMA,
                    "package_id": ledger["package_id"],
                    "kind": "adjudication_import",
                    "matrix_id": case["target"]["matrix_id"],
                    "work_item_id": work_item_id,
                    "canary_attestation_sha256": canary_attestation_sha256,
                    "case_sha256": _sha256(case),
                    "artifact_sha256": _sha256(document),
                    "artifact": document,
                    "recorded_at": _now(),
                }
                plans.append(("new", operation_path, operation))

            # All rosters, retained artifacts, operations, and target paths are
            # valid before the first append. Phase two retains operation-first
            # crash recovery for each planned import.
            for action, operation_path, payload in plans:
                if action == "retained":
                    retained_count += 1
                    continue
                if action == "resume":
                    _import_document_locked(matrix_dir, "adjudication", payload)
                    retained_count += 1
                    continue
                _publish_operation(operation_path, payload)
                _import_document_locked(
                    matrix_dir, "adjudication", payload["artifact"]
                )
                imported += 1
    return {"imported": imported, "retained": retained_count}


def review_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner review")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("matrix_dir", type=Path)
    prepare_parser.add_argument("review_dir", type=Path)
    prepare_parser.add_argument("--supersede-review-dir", type=Path)
    prepare_parser.add_argument("--incident-record", type=Path)
    identity_parser = subparsers.add_parser("supersession-identity")
    identity_parser.add_argument("matrix_dir", type=Path)
    identity_parser.add_argument("legacy_review_dir", type=Path)
    canary_parser = subparsers.add_parser("record-canary")
    canary_parser.add_argument("matrix_dir", type=Path)
    canary_parser.add_argument("review_dir", type=Path)
    canary_parser.add_argument("verdicts", type=Path)
    canary_parser.add_argument("--events", required=True, type=Path)
    canary_parser.add_argument("--duration-ms", required=True, type=int)
    canary_parser.add_argument(
        "--isolation-attestation",
        required=True,
        choices=(_ISOLATION_ATTESTATION,),
    )
    canary_parser.add_argument(
        "--operator-invocation-attestation",
        required=True,
        choices=(_CANARY_OPERATOR_ATTESTATION,),
    )
    canary_failure_parser = subparsers.add_parser("record-canary-failure")
    canary_failure_parser.add_argument("matrix_dir", type=Path)
    canary_failure_parser.add_argument("review_dir", type=Path)
    canary_failure_parser.add_argument(
        "--failure-class",
        required=True,
        choices=("provider_rejection", "transport_failure"),
    )
    canary_failure_parser.add_argument(
        "--duration-ms", required=True, type=int
    )
    canary_failure_parser.add_argument(
        "--isolation-attestation",
        required=True,
        choices=(_ISOLATION_ATTESTATION,),
    )
    canary_failure_parser.add_argument(
        "--operator-invocation-attestation",
        required=True,
        choices=(_CANARY_OPERATOR_ATTESTATION,),
    )
    validate_canary_parser = subparsers.add_parser("validate-canary")
    validate_canary_parser.add_argument("matrix_dir", type=Path)
    validate_canary_parser.add_argument("review_dir", type=Path)
    amendment_parser = subparsers.add_parser("record-measurement-amendment")
    amendment_parser.add_argument("matrix_dir", type=Path)
    amendment_parser.add_argument("review_dir", type=Path)
    amendment_parser.add_argument("work_item_id")
    amendment_parser.add_argument("--judge", required=True)
    amendment_parser.add_argument(
        "--amendment-class",
        required=True,
        choices=tuple(sorted(_MEASUREMENT_AMENDMENT_CLASSES)),
    )
    events_parser = subparsers.add_parser("check-events")
    events_parser.add_argument("events", type=Path)
    codex_parser = subparsers.add_parser("preflight-codex")
    codex_parser.add_argument("executable", type=Path)
    assemble_parser = subparsers.add_parser("assemble")
    assemble_parser.add_argument("matrix_dir", type=Path)
    assemble_parser.add_argument("review_dir", type=Path)
    assemble_parser.add_argument("work_item_id")
    assemble_parser.add_argument("verdicts", type=Path)
    assemble_parser.add_argument("--events", required=True, type=Path)
    assemble_parser.add_argument("--judge", required=True)
    assemble_parser.add_argument("--attempt-count", required=True, type=int)
    duration_group = assemble_parser.add_mutually_exclusive_group(required=True)
    duration_group.add_argument("--duration-ms", type=int)
    duration_group.add_argument("--duration-unavailable", action="store_true")
    assemble_parser.add_argument(
        "--isolation-attestation",
        required=True,
        choices=(_ISOLATION_ATTESTATION,),
    )
    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("matrix_dir", type=Path)
    resolve_parser.add_argument("review_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            supersession = {}
            if args.supersede_review_dir is not None:
                supersession["supersede_review_dir"] = args.supersede_review_dir
            if args.incident_record is not None:
                supersession["incident_record"] = args.incident_record
            result = prepare(
                args.matrix_dir,
                args.review_dir,
                **supersession,
            )
        elif args.command == "supersession-identity":
            result = supersession_identity(
                args.matrix_dir, args.legacy_review_dir
            )
        elif args.command == "record-canary":
            result = record_canary(
                args.matrix_dir,
                args.review_dir,
                args.verdicts,
                events_path=args.events,
                duration_ms=args.duration_ms,
                isolation_attestation=args.isolation_attestation,
                operator_invocation_attestation=(
                    args.operator_invocation_attestation
                ),
            )
        elif args.command == "record-canary-failure":
            result = record_canary_failure(
                args.matrix_dir,
                args.review_dir,
                failure_class=args.failure_class,
                duration_ms=args.duration_ms,
                isolation_attestation=args.isolation_attestation,
                operator_invocation_attestation=(
                    args.operator_invocation_attestation
                ),
            )
        elif args.command == "validate-canary":
            result = validate_canary(args.matrix_dir, args.review_dir)
        elif args.command == "record-measurement-amendment":
            result = record_measurement_amendment(
                args.matrix_dir,
                args.review_dir,
                args.work_item_id,
                judge_identifier=args.judge,
                amendment_class=args.amendment_class,
            )
        elif args.command == "check-events":
            result = check_event_log(args.events)
        elif args.command == "preflight-codex":
            result = preflight_native_codex(args.executable)
        elif args.command == "assemble":
            result = assemble_judgment(
                args.matrix_dir,
                args.review_dir,
                args.work_item_id,
                args.verdicts,
                events_path=args.events,
                judge_identifier=args.judge,
                attempt_count=args.attempt_count,
                duration_ms=(None if args.duration_unavailable else args.duration_ms),
                isolation_attestation=args.isolation_attestation,
            )
        else:
            result = resolve_agreement(args.matrix_dir, args.review_dir)
    except (
        ReviewError,
        judge.JudgmentError,
        inspection.InspectionError,
        matrix.MatrixError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("qualitative review filesystem operation failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
