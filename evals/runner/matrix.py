"""Resumable, route-interleaved calibration matrices over sealed child cohorts.

The matrix scheduler does not grade model output.  Each observation is produced
by the existing single-scenario runner and is committed only after that child
cohort and its private artifacts validate structurally.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from functools import cache
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, IO

from jsonschema import Draft202012Validator, FormatChecker

from evals.runner import codex_driver, controls, run_state


ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = ROOT / "evals" / "results"
SCENARIO_ROOT = ROOT / "evals" / "scenarios"
SCHEMA_ROOT = ROOT / "evals" / "schema"
MATRIX_SCHEMA_PATH = SCHEMA_ROOT / "matrix-0.1.json"
CALIBRATION_ROOT = ROOT / "evals" / "calibration"
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = run_state.MATRIX_MANIFEST_MAX_BYTES
_MAX_CALIBRATED_ASSET_BYTES = 1024 * 1024
_MAX_PREFLIGHT_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_ACCEPTANCE_ARTIFACT_BYTES = 16 * 1024 * 1024
_PREFLIGHT_REPLAY_SCHEMA = "steam-agent-eval-preflight-replay/0.1"
_MAX_CORPUS_OBJECT_BYTES = 1024 * 1024
_MAX_CORPUS_TREE_BYTES = 256 * 1024
_MAX_WORK_ITEMS = 10_000
_ROUTE_PREFLIGHT_TIMEOUT_SECONDS = 30.0
_CHILD_PROCESS_GRACE_SECONDS = 30.0
_CHILD_TERMINATION_GRACE_SECONDS = 10.0
_CHILD_FORCE_KILL_SECONDS = 5.0
_CHILD_BOOTSTRAP = (
    "import os, signal, sys\n"
    "os.kill(os.getpid(), signal.SIGSTOP)\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)
_MAX_CHILD_SCENARIO_TURNS = run_state.MAX_SCENARIO_TURNS
_SCENARIO_ID = re.compile(r"m[1-9][0-9]*-[a-z][0-9]{2,}\Z", re.ASCII)
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", re.ASCII)
_SAFE_SOURCE_COMPONENT = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}\Z", re.ASCII
)
_QUALITATIVE_ARTIFACT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\.json\Z", re.ASCII
)
_ATTEMPT_INITIALIZATION = re.compile(
    r"\.attempt-init-(?P<attempt_id>attempt-[0-9]{6})-[A-Za-z0-9_-]+\Z",
    re.ASCII,
)
_CHILD_REPORT = re.compile(r"^reports: evals/results/([^/\\\s]+)$", re.MULTILINE)
_LEGACY_DETERMINISTIC_ONLY = frozenset({"m5-c03", "m5-c04", "m5-c11"})
_CALIBRATED_JUDGE_ASSETS = {
    ("prompt", "matrix-judge/0.1"): (CALIBRATION_ROOT / "matrix-judge-prompt-0.1.md"),
    ("parser", "matrix-parser/0.1"): CALIBRATION_ROOT / "matrix-parser-0.1.json",
    ("settings", "matrix-judge-settings-0.1"): (
        CALIBRATION_ROOT / "matrix-judge-settings-0.1.json"
    ),
}
_CALIBRATED_JUDGE_ASSET_FILENAMES = {
    identity: path.name for identity, path in _CALIBRATED_JUDGE_ASSETS.items()
}


class MatrixError(RuntimeError):
    """A matrix/config/child-cohort contract was invalid."""


@dataclass(frozen=True, slots=True)
class CalibratedAsset:
    filename: str
    sha256: str
    source_bytes: bytes


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    source_bytes: bytes
    document: Mapping[str, Any]
    sha256: str
    calibrated_assets: tuple[CalibratedAsset, ...] = ()

    @property
    def timeout_seconds(self) -> float:
        return float(self.document["timeout_seconds"])

    @property
    def campaign(self) -> run_state.MatrixCampaign:
        try:
            return run_state.MatrixCampaign.from_config(self.document)
        except run_state.ManifestStateError:
            raise MatrixError("matrix config is invalid") from None


@dataclass(frozen=True, slots=True)
class ChildResult:
    exit_code: int | None
    run_dir: Path | None
    unavailable_reason: str | None = None

    @classmethod
    def unavailable(cls, reason: str) -> ChildResult:
        return cls(None, None, reason)


@dataclass(frozen=True, slots=True)
class ValidatedChildResult:
    child_run_id: str
    artifact_hashes: tuple[tuple[str, str], ...]
    manifest: run_state.RunManifest
    report: dict[str, Any]
    summary: dict[str, Any]
    manifest_bytes: bytes
    report_bytes: bytes
    summary_bytes: bytes
    transcript_bytes: bytes
    controls_bytes: bytes


@dataclass(frozen=True, slots=True)
class _SelectedCorpusSeal:
    scenario_sources: tuple[tuple[str, str, bytes], ...]
    schema_sources: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True, slots=True)
class _PreflightArtifact:
    scenario_id: str
    input_source_bytes: bytes
    input_document: Any
    oracle_document: Any
    grading_result: dict[str, Any]
    replay_definition: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ExecutedPreflight:
    attestation: run_state.MatrixPreflightAttestation
    artifacts: tuple[_PreflightArtifact, ...]


@dataclass(frozen=True, slots=True)
class ValidatedAttemptArtifacts:
    attempt_id: str
    work_item_id: str
    started_at: str
    artifact_hashes: tuple[tuple[str, str], ...]
    completion: run_state.MatrixCompletion | None
    failure: tuple[str, str] | None


ChildExecutor = Callable[[run_state.MatrixWorkItem, float], ChildResult]


def _strict_json_loads(value: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError):
        raise MatrixError("matrix value is not strict JSON") from None


def _preflight_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError):
        raise MatrixError("matrix preflight evidence is invalid") from None


def _read_strict_json(path: Path, *, max_bytes: int = _MAX_CONFIG_BYTES) -> Any:
    try:
        item_stat = path.lstat()
        if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_size > max_bytes:
            raise MatrixError("matrix JSON input is invalid")
        return _strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise MatrixError("matrix JSON input is invalid") from None


def _bounded_regular_bytes(path: Path) -> bytes:
    descriptor = -1
    try:
        item_stat = path.lstat()
        if (
            not stat.S_ISREG(item_stat.st_mode)
            or not 0 < item_stat.st_size <= _MAX_CALIBRATED_ASSET_BYTES
        ):
            raise MatrixError("matrix config calibrated judge asset is invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ) != (item_stat.st_dev, item_stat.st_ino):
            raise MatrixError("matrix config calibrated judge asset is invalid")
        chunks: list[bytes] = []
        remaining = _MAX_CALIBRATED_ASSET_BYTES + 1
        while chunk := os.read(descriptor, min(1024 * 1024, remaining)):
            chunks.append(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                raise MatrixError("matrix config calibrated judge asset is invalid")
        return b"".join(chunks)
    except OSError:
        raise MatrixError("matrix config calibrated judge asset is invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _bounded_regular_digest(path: Path) -> str:
    return hashlib.sha256(_bounded_regular_bytes(path)).hexdigest()


def _calibrated_asset_declarations(
    document: Mapping[str, Any],
) -> dict[str, str]:
    judge = document["judge_policy"]
    declarations: dict[str, str] = {}

    def declare(kind: str, version: str, digest: str) -> None:
        filename = _CALIBRATED_JUDGE_ASSET_FILENAMES.get((kind, version))
        if filename is None:
            raise MatrixError("matrix config calibrated judge asset does not match")
        existing = declarations.get(filename)
        if existing is not None and existing != digest:
            raise MatrixError("matrix config calibrated judge asset does not match")
        declarations[filename] = digest

    for kind in ("prompt", "parser"):
        declare(kind, judge[f"{kind}_version"], judge[f"{kind}_sha256"])
    for configuration in judge["judges"]:
        declare(
            "settings",
            configuration["settings_identity"],
            configuration["settings_sha256"],
        )
    return declarations


def _validate_calibrated_judge_assets(
    document: Mapping[str, Any],
) -> tuple[CalibratedAsset, ...]:
    assets: dict[str, CalibratedAsset] = {}

    def validate(kind: str, version: str, expected_sha256: str) -> None:
        path = _CALIBRATED_JUDGE_ASSETS.get((kind, version))
        filename = _CALIBRATED_JUDGE_ASSET_FILENAMES.get((kind, version))
        if path is None or filename is None:
            raise MatrixError("matrix config calibrated judge asset does not match")
        source_bytes = _bounded_regular_bytes(path)
        digest = hashlib.sha256(source_bytes).hexdigest()
        if digest != expected_sha256:
            raise MatrixError("matrix config calibrated judge asset does not match")
        asset = CalibratedAsset(filename, digest, source_bytes)
        existing = assets.get(asset.filename)
        if existing is not None and existing != asset:
            raise MatrixError("matrix config calibrated judge asset does not match")
        assets[asset.filename] = asset

    judge = document["judge_policy"]
    for kind in ("prompt", "parser"):
        validate(kind, judge[f"{kind}_version"], judge[f"{kind}_sha256"])
    for configuration in judge["judges"]:
        validate(
            "settings",
            configuration["settings_identity"],
            configuration["settings_sha256"],
        )
    return tuple(assets[name] for name in sorted(assets))


def load_config(
    path: Path, *, validate_calibrated_assets: bool = True
) -> LoadedConfig:
    path = Path(path)
    try:
        source_bytes = path.read_bytes()
    except OSError:
        raise MatrixError("matrix config is unavailable") from None
    if not source_bytes or len(source_bytes) > _MAX_CONFIG_BYTES:
        raise MatrixError("matrix config is invalid")
    try:
        document = _strict_json_loads(source_bytes.decode("utf-8"))
        schema = _read_strict_json(MATRIX_SCHEMA_PATH)
        Draft202012Validator.check_schema(schema)
        valid = Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(
            document
        )
    except (UnicodeError, ValueError):
        valid = False
        document = None
    if not valid or not isinstance(document, dict):
        raise MatrixError("matrix config is invalid")
    calibrated_assets = (
        _validate_calibrated_judge_assets(document)
        if validate_calibrated_assets
        else ()
    )
    loaded = LoadedConfig(
        source_bytes=source_bytes,
        document=document,
        sha256=hashlib.sha256(source_bytes).hexdigest(),
        calibrated_assets=calibrated_assets,
    )
    try:
        campaign = loaded.campaign
    except MatrixError:
        raise MatrixError("matrix config is invalid") from None
    if (
        tuple(document["tracks"]) != campaign.required_tracks
        or document["replicates"] != campaign.replicates
    ):
        raise MatrixError("matrix config policy does not match its plan axes")
    return loaded


def _git_commit_and_clean(root: Path) -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise MatrixError("matrix requires a known clean source revision") from None
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit) or status_result.stdout:
        raise MatrixError("matrix requires a known clean source revision")
    _require_execution_roots_match_commit(root, commit)
    return commit


def _generated_source_name(relative: PurePosixPath) -> bool:
    return any(part == "__pycache__" for part in relative.parts) or (
        relative.name == ".DS_Store" or relative.name.endswith((".pyc", ".pyo"))
    )


def _committed_execution_files(
    root: Path, commit: str
) -> dict[str, tuple[str, bool]]:
    try:
        result = subprocess.run(
            [
                "git",
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                commit,
                "--",
                "src",
                "evals/runner",
            ],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=10,
        )
        records = result.stdout.split(b"\0")
        if records[-1] != b"":
            raise ValueError
        committed: dict[str, tuple[str, bool]] = {}
        for record in records[:-1]:
            header, encoded_name = record.split(b"\t", 1)
            mode, kind, object_id = header.decode("ascii").split(" ")
            name = encoded_name.decode("utf-8")
            relative = PurePosixPath(name)
            if (
                kind != "blob"
                or mode not in {"100644", "100755"}
                or not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
                or relative.is_absolute()
                or not (
                    relative.is_relative_to(PurePosixPath("src"))
                    or relative.is_relative_to(PurePosixPath("evals/runner"))
                )
            ):
                raise ValueError
            root_name = (
                PurePosixPath("src")
                if relative.parts[0] == "src"
                else PurePosixPath("evals/runner")
            )
            local_name = relative.relative_to(root_name)
            if _generated_source_name(local_name):
                continue
            if any(
                _SAFE_SOURCE_COMPONENT.fullmatch(part) is None
                for part in relative.parts
            ):
                raise ValueError
            if name in committed:
                raise ValueError
            committed[name] = (object_id, mode == "100755")
        if not committed or len(committed) > 2 * 16_384:
            raise ValueError
        return committed
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise MatrixError("matrix committed source inventory is invalid") from None


def _require_execution_roots_match_commit(root: Path, commit: str) -> None:
    committed = _committed_execution_files(root, commit)
    actual_files: dict[str, run_state.InventoryEntry] = {}
    actual_directories: set[str] = set()
    for prefix in (PurePosixPath("src"), PurePosixPath("evals/runner")):
        try:
            entries = run_state._scan_inventory(  # noqa: SLF001
                root / prefix.as_posix(), ignore_generated=True
            )
        except run_state.SnapshotIntegrityError:
            raise MatrixError("matrix committed source inventory is invalid") from None
        for entry in entries:
            full_name = (
                prefix.as_posix()
                if entry.relative_name == "."
                else f"{prefix.as_posix()}/{entry.relative_name}"
            )
            if entry.kind == "file":
                actual_files[full_name] = entry
            elif entry.kind == "directory":
                actual_directories.add(full_name)
            else:
                raise MatrixError("matrix committed source inventory is invalid")

    expected_files = set(committed)
    expected_directories = {"src", "evals/runner"}
    for name in expected_files:
        parent = PurePosixPath(name).parent
        while parent.as_posix() not in {".", "evals"}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if set(actual_files) != expected_files or actual_directories != expected_directories:
        raise MatrixError("matrix execution source does not match committed revision")
    if any(
        bool(entry.mode & 0o111) != committed[name][1]
        for name, entry in actual_files.items()
    ):
        raise MatrixError("matrix execution source does not match committed revision")
    try:
        ordered_names = sorted(expected_files)
        hashed = subprocess.run(
            ["git", "hash-object", "--no-filters", "--stdin-paths"],
            cwd=root,
            input="".join(f"{name}\n" for name in ordered_names),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        raise MatrixError("matrix committed source inventory is invalid") from None
    if len(hashed) != len(ordered_names) or any(
        not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
        or object_id != committed[name][0]
        for name, object_id in zip(ordered_names, hashed, strict=True)
    ):
        raise MatrixError("matrix execution source does not match committed revision")


def _committed_blob_bytes(
    root: Path, object_id: str, size: int
) -> bytes:
    if not 0 < size <= _MAX_CORPUS_OBJECT_BYTES:
        raise MatrixError("matrix committed corpus input is invalid")
    try:
        source = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise MatrixError("matrix committed corpus input is invalid") from None
    if len(source) != size:
        raise MatrixError("matrix committed corpus input is invalid")
    return source


def _committed_blob_at_path(
    root: Path, commit: str, relative: str
) -> tuple[bytes, bool]:
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-z", "-l", "--full-tree", commit, "--", relative],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=20,
        )
        if len(result.stdout) > _MAX_CORPUS_TREE_BYTES:
            raise ValueError
        records = result.stdout.split(b"\0")
        if len(records) != 2 or records[-1] != b"":
            raise ValueError
        header, encoded_name = records[0].split(b"\t", 1)
        mode, kind, object_id, size_text = header.decode("ascii").split()
        if (
            encoded_name.decode("utf-8") != relative
            or kind != "blob"
            or mode not in {"100644", "100755"}
            or not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
        ):
            raise ValueError
        size = int(size_text)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise MatrixError("matrix committed corpus input is invalid") from None
    return _committed_blob_bytes(root, object_id, size), mode == "100755"


def _working_corpus_bytes(path: Path, *, executable: bool) -> bytes:
    descriptor = -1
    try:
        item_stat = path.lstat()
        if (
            not stat.S_ISREG(item_stat.st_mode)
            or item_stat.st_size <= 0
            or item_stat.st_size > _MAX_CORPUS_OBJECT_BYTES
            or bool(item_stat.st_mode & 0o111) != executable
        ):
            raise MatrixError("matrix corpus input does not match committed revision")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (item_stat.st_dev, item_stat.st_ino)
        ):
            raise MatrixError("matrix corpus input does not match committed revision")
        chunks: list[bytes] = []
        remaining = _MAX_CORPUS_OBJECT_BYTES + 1
        while remaining and (
            chunk := os.read(descriptor, min(1024 * 1024, remaining))
        ):
            chunks.append(chunk)
            remaining -= len(chunk)
        source = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(source) != before.st_size
            or len(source) > _MAX_CORPUS_OBJECT_BYTES
            or any(
                getattr(before, field) != getattr(after, field)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            )
        ):
            raise MatrixError("matrix corpus input changed during verification")
        return source
    except OSError:
        raise MatrixError("matrix corpus input does not match committed revision") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _seal_selected_corpus_inputs(
    root: Path, commit: str, scenario_ids: Sequence[str]
) -> _SelectedCorpusSeal:
    wanted = set(scenario_ids)
    if len(wanted) != len(scenario_ids) or any(
        not isinstance(item, str) or _SCENARIO_ID.fullmatch(item) is None
        for item in scenario_ids
    ):
        raise MatrixError("matrix scenario selection is invalid")
    try:
        listing = subprocess.run(
            [
                "git",
                "ls-tree",
                "-r",
                "-z",
                "-l",
                "--full-tree",
                commit,
                "--",
                "evals/scenarios",
            ],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout
        if len(listing) > _MAX_CORPUS_TREE_BYTES:
            raise ValueError
        records = listing.split(b"\0")
        if records[-1] != b"" or len(records) > 1025:
            raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError):
        raise MatrixError("matrix committed corpus input is invalid") from None

    selected: dict[str, tuple[str, bytes, bool, str]] = {}
    for record in records[:-1]:
        try:
            header, encoded_name = record.split(b"\t", 1)
            mode, kind, object_id, size_text = header.decode("ascii").split()
            relative = encoded_name.decode("utf-8")
            parts = PurePosixPath(relative).parts
            if (
                kind != "blob"
                or mode not in {"100644", "100755"}
                or not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
                or len(parts) != 4
                or parts[:2] != ("evals", "scenarios")
                or any(_SAFE_COMPONENT.fullmatch(part) is None for part in parts[2:])
            ):
                raise ValueError
            if not relative.endswith(".json"):
                continue
            size = int(size_text)
            source = _committed_blob_bytes(root, object_id, size)
            document = _strict_json_loads(source.decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError
            scenario_id = document.get("id")
            if scenario_id not in wanted:
                continue
            schema_version = document.get("schema_version")
            if (
                scenario_id in selected
                or not isinstance(schema_version, str)
                or re.fullmatch(r"steam-agent-eval/[0-9]+\.[0-9]+", schema_version)
                is None
            ):
                raise ValueError
            selected[scenario_id] = (
                "/".join(parts[2:]),
                source,
                mode == "100755",
                schema_version.rsplit("/", 1)[-1],
            )
        except MatrixError:
            raise
        except (UnicodeError, ValueError):
            raise MatrixError("matrix committed corpus input is invalid") from None
    if set(selected) != wanted:
        raise MatrixError("matrix committed corpus selection is incomplete")

    schema_sources: dict[str, bytes] = {}
    scenario_sources: list[tuple[str, str, bytes]] = []
    for scenario_id in scenario_ids:
        source_name, committed_source, executable, version = selected[scenario_id]
        actual_source = _working_corpus_bytes(
            root / "evals" / "scenarios" / source_name,
            executable=executable,
        )
        if actual_source != committed_source:
            raise MatrixError("matrix corpus input does not match committed revision")
        scenario_sources.append((scenario_id, source_name, committed_source))
        schema_name = f"scenario-{version}.json"
        if schema_name not in schema_sources:
            schema_relative = f"evals/schema/{schema_name}"
            committed_schema, schema_executable = _committed_blob_at_path(
                root, commit, schema_relative
            )
            actual_schema = _working_corpus_bytes(
                root / schema_relative, executable=schema_executable
            )
            if actual_schema != committed_schema:
                raise MatrixError("matrix corpus input does not match committed revision")
            schema_sources[schema_name] = committed_schema
    return _SelectedCorpusSeal(
        scenario_sources=tuple(scenario_sources),
        schema_sources=tuple(sorted(schema_sources.items())),
    )


def _expected_child_source_digest(
    *,
    root: Path,
    source_name: str,
    source_bytes: bytes,
    document: Mapping[str, Any],
    schema_name: str,
    schema_bytes: bytes,
) -> str:
    frozen = run_state.FrozenScenario.create(
        source_name=source_name,
        original_bytes=source_bytes,
        document=document,
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="steam-agent-eval-matrix-input-"
        ) as workspace_name:
            workspace = Path(workspace_name)
            workspace.chmod(0o700)
            with run_state.SourceSnapshot.create(
                workspace / "snapshot",
                source_root=root / "src",
                harness_root=root / "evals" / "runner",
                scenarios=[frozen],
                schemas={schema_name: schema_bytes},
            ) as snapshot:
                return snapshot.digest
    except (OSError, ValueError, run_state.SnapshotIntegrityError):
        raise MatrixError("matrix child input attestation failed") from None


def _qualitative_rubric(
    document: Mapping[str, Any],
) -> tuple[
    tuple[run_state.MatrixQualitativeCriterion, ...],
    dict[str, Any],
]:
    rubric = document.get("judged_answer_rubric")
    fact_rubric = document.get("fact_rubric")
    criteria = rubric.get("criteria") if isinstance(rubric, dict) else None
    must_mention = (
        fact_rubric.get("must_mention")
        if isinstance(fact_rubric, dict)
        else None
    )
    fact_criteria = (
        fact_rubric.get("criteria") if isinstance(fact_rubric, dict) else None
    )
    support_if_claimed = (
        fact_rubric.get("support_if_claimed")
        if isinstance(fact_rubric, dict)
        else None
    )
    if not all(
        isinstance(items, list)
        for items in (criteria, fact_criteria, must_mention, support_if_claimed)
    ):
        raise MatrixError("matrix judged rubric is invalid")
    try:
        qualitative_criteria = (
            run_state.scenario_qualitative_criteria(document)
            if document.get("schema_version") == "steam-agent-eval/0.3"
            else run_state.matrix_qualitative_criteria(
                criteria,
                must_mention,
                fact_criteria=fact_criteria,
                support_if_claimed=support_if_claimed,
            )
        )
    except run_state.ManifestStateError as error:
        raise MatrixError(f"matrix judged rubric is invalid: {error}") from None
    return qualitative_criteria, {
        "schema": "steam-agent-eval-qualitative-rubric/0.1",
        "judged_answer_rubric": rubric,
        "fact_rubric_hard_fail_criteria": [
            item for item in fact_criteria if item.get("hard_fail") is True
        ],
        "fact_rubric_must_mention": must_mention,
        "fact_rubric_support_if_claimed": support_if_claimed,
        "criteria": [item.to_dict() for item in qualitative_criteria],
    }


def _scenario_inputs(
    scenario_ids: Sequence[str],
    *,
    root: Path,
    corpus_seal: _SelectedCorpusSeal | None = None,
) -> tuple[
    tuple[run_state.MatrixScenario, ...],
    dict[str, Mapping[str, Any]],
    dict[str, bytes],
]:
    wanted = set(scenario_ids)
    if len(wanted) != len(scenario_ids) or any(
        not isinstance(item, str) or _SCENARIO_ID.fullmatch(item) is None
        for item in scenario_ids
    ):
        raise MatrixError("matrix scenario selection is invalid")
    documents: dict[str, Mapping[str, Any]] = {}
    sources: dict[str, bytes] = {}
    source_names: dict[str, str] = {}
    scenario_root = root / "evals" / "scenarios"
    for path in sorted(scenario_root.glob("*/*.json")):
        try:
            source = path.read_bytes()
            document = _strict_json_loads(source.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise MatrixError("matrix scenario input is invalid") from None
        if not isinstance(document, dict):
            raise MatrixError("matrix scenario input is invalid")
        scenario_id = document.get("id")
        if scenario_id not in wanted:
            continue
        if scenario_id in documents:
            raise MatrixError("matrix scenario selection is ambiguous")
        documents[scenario_id] = document
        sources[scenario_id] = source
        source_names[scenario_id] = path.relative_to(scenario_root).as_posix()
    if set(documents) != wanted:
        raise MatrixError("matrix scenario selection is incomplete")
    sealed_scenarios = (
        {
            scenario_id: (source_name, source)
            for scenario_id, source_name, source in corpus_seal.scenario_sources
        }
        if corpus_seal is not None
        else None
    )
    sealed_schemas = (
        dict(corpus_seal.schema_sources) if corpus_seal is not None else None
    )
    if sealed_scenarios is not None and (
        set(sealed_scenarios) != wanted
        or any(
            sealed_scenarios[scenario_id]
            != (source_names[scenario_id], sources[scenario_id])
            for scenario_id in scenario_ids
        )
    ):
        raise MatrixError("matrix corpus input does not match committed revision")

    scenarios: list[run_state.MatrixScenario] = []
    for scenario_id in scenario_ids:
        document = documents[scenario_id]
        schema_version = document.get("schema_version")
        if not isinstance(schema_version, str) or not schema_version.startswith(
            "steam-agent-eval/"
        ):
            raise MatrixError("matrix scenario schema is invalid")
        version = schema_version.rsplit("/", 1)[-1]
        schema_path = root / "evals" / "schema" / f"scenario-{version}.json"
        try:
            schema_bytes = schema_path.read_bytes()
            if sealed_schemas is not None and sealed_schemas.get(schema_path.name) != schema_bytes:
                raise MatrixError(
                    "matrix corpus input does not match committed revision"
                )
            schema = _strict_json_loads(schema_bytes.decode("utf-8"))
            Draft202012Validator.check_schema(schema)
            valid = Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).is_valid(document)
        except MatrixError:
            raise
        except (OSError, UnicodeError, ValueError):
            valid = False
        if not valid:
            raise MatrixError("matrix scenario schema is invalid")
        execution_support = document.get("execution_support")
        if execution_support is None and schema_version in {
            "steam-agent-eval/0.1",
            "steam-agent-eval/0.2",
        }:
            execution_support = (
                "deterministic_only"
                if scenario_id in _LEGACY_DETERMINISTIC_ONLY
                else "live"
            )
        qualitative_criteria, qualitative_rubric = _qualitative_rubric(document)
        conversation = document.get("conversation")
        conversation_turns = (
            conversation.get("user") if isinstance(conversation, dict) else None
        )
        if not isinstance(conversation_turns, list) or not conversation_turns:
            raise MatrixError("matrix scenario conversation is invalid")
        criterion_ids = tuple(
            item.criterion_id for item in qualitative_criteria
        )
        scenarios.append(
            run_state.MatrixScenario(
                scenario_id=scenario_id,
                source_sha256=hashlib.sha256(sources[scenario_id]).hexdigest(),
                child_source_digest=_expected_child_source_digest(
                    root=root,
                    source_name=source_names[scenario_id],
                    source_bytes=sources[scenario_id],
                    document=document,
                    schema_name=schema_path.name,
                    schema_bytes=schema_bytes,
                ),
                schema_version=schema_version.replace("/", ":"),
                schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
                execution_support=execution_support,
                turn_count=len(conversation_turns),
                rubric_sha256=hashlib.sha256(
                    _canonical_json_bytes(qualitative_rubric)
                ).hexdigest(),
                criterion_ids=criterion_ids,
                qualitative_criteria=qualitative_criteria,
            )
        )
    return tuple(scenarios), documents, sources


def _scenario_documents(
    scenario_ids: Sequence[str],
    *,
    root: Path,
    corpus_seal: _SelectedCorpusSeal | None = None,
) -> tuple[tuple[run_state.MatrixScenario, ...], dict[str, Mapping[str, Any]]]:
    scenarios, documents, _sources = _scenario_inputs(
        scenario_ids, root=root, corpus_seal=corpus_seal
    )
    return scenarios, documents


def collect_inputs(
    config: LoadedConfig, *, root: Path = ROOT
) -> run_state.MatrixInputs:
    root = Path(root)
    commit = _git_commit_and_clean(root)
    corpus_seal = _seal_selected_corpus_inputs(
        root, commit, config.document["scenario_ids"]
    )
    scenarios, _documents = _scenario_documents(
        config.document["scenario_ids"], root=root, corpus_seal=corpus_seal
    )
    try:
        source_digest = run_state.inventory_digest(root / "src")
        harness_digest = run_state.inventory_digest(root / "evals" / "runner")
        codex_version = codex_driver.codex_version().rsplit(" ", 1)[-1]
    except (OSError, ValueError, run_state.SnapshotIntegrityError):
        raise MatrixError("matrix input attestation failed") from None
    _require_execution_roots_match_commit(root, commit)
    return run_state.MatrixInputs(
        commit=commit,
        source_digest=source_digest,
        harness_digest=harness_digest,
        scenarios=scenarios,
        tool_versions=tuple(
            sorted(
                {
                    "codex": codex_version,
                    "controls": controls.CONTROL_SCHEMA_VERSION.replace("/", ":"),
                    "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                }.items()
            )
        ),
    )


def resolve_plan(
    config: LoadedConfig, inputs: run_state.MatrixInputs
) -> tuple[run_state.MatrixWorkItem, ...]:
    live_scenarios = [
        item.scenario_id
        for item in inputs.scenarios
        if item.execution_support == "live"
    ]
    if config.document["campaign_kind"] == "qualification":
        routes = [
            run_state.MatrixRoute(route["model"], route["reasoning_effort"])
            for route in config.document["routes"]
        ]
    else:
        routes = [
            run_state.MatrixRoute(model, effort)
            for model in config.document["models"]
            for effort in config.document["efforts"]
        ]
    total = (
        len(live_scenarios)
        * len(routes)
        * len(config.document["tracks"])
        * config.document["replicates"]
    )
    if total == 0 or total > _MAX_WORK_ITEMS:
        raise MatrixError("matrix work plan exceeds safety limits")
    work_items: list[run_state.MatrixWorkItem] = []
    for track in config.document["tracks"]:
        for replicate in range(1, config.document["replicates"] + 1):
            rotation = (replicate - 1) % len(routes)
            ordered_routes = routes[rotation:] + routes[:rotation]
            for scenario_id in live_scenarios:
                for route in ordered_routes:
                    ordinal = len(work_items)
                    identity_document = {
                        "scenario_id": scenario_id,
                        "track": track,
                        "route": route.to_dict(),
                        "replicate": replicate,
                    }
                    identity = hashlib.sha256(
                        _canonical_json_bytes(identity_document)
                    ).hexdigest()
                    work_items.append(
                        run_state.MatrixWorkItem(
                            work_item_id=f"w-{ordinal:06d}-{identity[:16]}",
                            identity_sha256=identity,
                            ordinal=ordinal,
                            scenario_id=scenario_id,
                            track=track,
                            route=route,
                            replicate=replicate,
                        )
                    )
    return tuple(work_items)


def plan_sha256(work_items: Sequence[run_state.MatrixWorkItem]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes([item.to_dict() for item in work_items])
    ).hexdigest()


def _ensure_private_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
        path.chmod(0o700)
    except OSError:
        raise MatrixError("matrix artifact directory is unavailable") from None


def _require_private_dir(path: Path) -> None:
    try:
        item_stat = path.lstat()
    except OSError:
        raise MatrixError("matrix artifact directory is unavailable") from None
    if not stat.S_ISDIR(item_stat.st_mode) or stat.S_IMODE(item_stat.st_mode) != 0o700:
        raise MatrixError("matrix artifact directory is not private")


def _ensure_results_root(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        _require_private_dir(path)
    except OSError:
        raise MatrixError("matrix results root is unavailable") from None
    else:
        path.chmod(0o700)


def _safe_child(path: Path, child: Path) -> Path:
    root = Path(os.path.abspath(path))
    candidate = Path(os.path.abspath(child))
    try:
        relative = candidate.relative_to(root)
        if not relative.parts:
            raise ValueError
        _require_private_dir(root)
        current = root
        for index, component in enumerate(relative.parts):
            if component in {"", ".", ".."}:
                raise ValueError
            current = current / component
            try:
                item_stat = current.lstat()
            except FileNotFoundError:
                if index != len(relative.parts) - 1:
                    raise ValueError from None
                continue
            if stat.S_ISLNK(item_stat.st_mode):
                raise ValueError
            if index != len(relative.parts) - 1 and not stat.S_ISDIR(item_stat.st_mode):
                raise ValueError
    except (MatrixError, OSError, RuntimeError, ValueError):
        raise MatrixError("matrix artifact path is invalid") from None
    return candidate


class MatrixLock:
    def __init__(self, matrix_dir: Path):
        self.path = matrix_dir / "matrix.lock"
        self._file: IO[bytes] | None = None

    def __enter__(self) -> MatrixLock:
        _require_private_dir(self.path.parent)
        try:
            descriptor = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError:
            raise MatrixError("matrix lock is invalid") from None
        os.fchmod(descriptor, 0o600)
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode):
            os.close(descriptor)
            raise MatrixError("matrix lock is invalid")
        self._file = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            raise MatrixError("matrix is already running") from None
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


def _matrix_id(now: datetime) -> str:
    return "matrix-" + now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _publish_calibrated_assets(matrix_dir: Path, config: LoadedConfig) -> None:
    declarations = _calibrated_asset_declarations(config.document)
    if (
        not config.calibrated_assets
        or {item.filename: item.sha256 for item in config.calibrated_assets}
        != declarations
    ):
        raise MatrixError("matrix calibrated asset retention is invalid")
    asset_root = matrix_dir / "calibration"
    _ensure_private_dir(asset_root)
    for asset in config.calibrated_assets:
        run_state.atomic_publish_private_bytes(
            asset_root / asset.filename, asset.source_bytes
        )


def validate_retained_calibrated_assets(
    matrix_dir: Path, config: LoadedConfig
) -> dict[str, bytes]:
    judge = config.document["judge_policy"]
    expected_digests = {
        judge["prompt_sha256"],
        judge["parser_sha256"],
        *(item["settings_sha256"] for item in judge["judges"]),
    }
    asset_root = Path(matrix_dir) / "calibration"
    try:
        _require_private_dir(asset_root)
        items = tuple(asset_root.iterdir())
        if len(items) != len(expected_digests):
            raise MatrixError("matrix retained calibrated assets are invalid")
        retained: dict[str, bytes] = {}
        retained_digests: set[str] = set()
        for item in items:
            if _SAFE_COMPONENT.fullmatch(item.name) is None:
                raise MatrixError("matrix retained calibrated assets are invalid")
            source_bytes = _private_regular_bytes(
                item, max_bytes=_MAX_CALIBRATED_ASSET_BYTES
            )
            digest = hashlib.sha256(source_bytes).hexdigest()
            if digest not in expected_digests or digest in retained_digests:
                raise MatrixError("matrix retained calibrated assets are invalid")
            retained[item.name] = source_bytes
            retained_digests.add(digest)
        if retained_digests != expected_digests:
            raise MatrixError("matrix retained calibrated assets are invalid")
        return retained
    except MatrixError:
        raise MatrixError("matrix retained calibrated assets are invalid") from None
    except OSError:
        raise MatrixError("matrix retained calibrated assets are invalid") from None


def create_matrix(
    config: LoadedConfig,
    inputs: run_state.MatrixInputs,
    *,
    preflight_attestation: run_state.MatrixPreflightAttestation | None = None,
    root: Path = ROOT,
    results_root: Path = RESULTS_ROOT,
    now: datetime | None = None,
) -> tuple[Path, run_state.MatrixManifest]:
    executed_preflight = _preflight_campaign_scenarios(inputs, root=Path(root))
    exact_attestation = executed_preflight.attestation
    if preflight_attestation is None:
        preflight_attestation = exact_attestation
    elif preflight_attestation != exact_attestation:
        raise MatrixError(
            "matrix deterministic-only preflight attestation is invalid"
        )
    try:
        preflight_attestation.require_matches(inputs)
    except run_state.ManifestStateError:
        raise MatrixError(
            "matrix deterministic-only preflight attestation is invalid"
        ) from None
    work_items = resolve_plan(config, inputs)
    started = now or datetime.now(timezone.utc)
    matrix_id = _matrix_id(started)
    results_root = Path(results_root)
    _ensure_results_root(results_root)
    _verify_qualification_source(config, results_root, started_at=started)
    matrix_dir = results_root / matrix_id
    _ensure_private_dir(matrix_dir)
    _publish_calibrated_assets(matrix_dir, config)
    _publish_preflight_evidence(matrix_dir, executed_preflight)
    manifest = run_state.MatrixManifest.create(
        matrix_id=matrix_id,
        config_sha256=config.sha256,
        campaign=config.campaign,
        plan_sha256=plan_sha256(work_items),
        inputs=inputs,
        preflight_attestation=preflight_attestation,
        work_items=work_items,
        excluded_scenario_ids=[
            item.scenario_id
            for item in inputs.scenarios
            if item.execution_support == "deterministic_only"
        ],
        started_at=started,
    )
    try:
        run_state.atomic_publish_private_bytes(
            matrix_dir / "config.json", config.source_bytes
        )
        manifest.persist(matrix_dir / "manifest.json")
    except BaseException:
        # Leave a private, visibly incomplete directory rather than deleting
        # evidence whose publication state is uncertain.
        raise
    return matrix_dir, manifest


def _preflight_campaign_scenarios(
    inputs: run_state.MatrixInputs, *, root: Path
) -> _ExecutedPreflight:
    deterministic_ids = tuple(
        item.scenario_id
        for item in inputs.scenarios
        if item.execution_support == "deterministic_only"
    )
    if not deterministic_ids:
        return _ExecutedPreflight(run_state.MatrixPreflightAttestation(()), ())
    scenarios, documents, sources = _scenario_inputs(deterministic_ids, root=root)
    expected_scenarios = tuple(
        item
        for item in inputs.scenarios
        if item.execution_support == "deterministic_only"
    )
    if scenarios != expected_scenarios:
        raise MatrixError("matrix deterministic-only preflight inputs changed")
    # Imported lazily because the runner imports this module when dispatching
    # the matrix subcommand. The existing preflight is deterministic: it
    # materializes a private fixture and exercises only the frozen CLI oracle.
    from evals.runner import __main__ as runner_main

    try:
        evidence: dict[str, tuple[str, str, str]] = {}
        artifacts: list[_PreflightArtifact] = []
        for scenario_id in deterministic_ids:
            input_document = documents[scenario_id]
            result = runner_main._preflight_deterministic_scenario(  # noqa: SLF001
                dict(input_document), source_root=root / "src"
            )
            replay_definition = _preflight_replay_definition(
                input_document, executor=result.executor
            )
            evidence[scenario_id] = (
                result.executor,
                _preflight_bundle_digest(
                    input_document,
                    result.document,
                    replay_definition,
                ),
                result.grading_sha256,
            )
            artifacts.append(
                _PreflightArtifact(
                    scenario_id=scenario_id,
                    input_source_bytes=sources[scenario_id],
                    input_document=input_document,
                    oracle_document=result.document,
                    grading_result=result.grading,
                    replay_definition=replay_definition,
                )
            )
        return _ExecutedPreflight(
            run_state.MatrixPreflightAttestation.for_inputs(
                inputs, evidence=evidence
            ),
            tuple(artifacts),
        )
    except MatrixError:
        raise
    except Exception:
        raise MatrixError("matrix deterministic-only preflight failed") from None


def _publish_preflight_evidence(
    matrix_dir: Path, executed: _ExecutedPreflight
) -> None:
    evidence_root = Path(matrix_dir) / "preflight"
    _ensure_private_dir(evidence_root)
    for artifact in executed.artifacts:
        run_state.atomic_publish_private_bytes(
            evidence_root / f"{artifact.scenario_id}.input.json",
            artifact.input_source_bytes,
        )
        for kind, value in (
            ("document", artifact.oracle_document),
            ("definition", artifact.replay_definition),
            ("grading", artifact.grading_result),
        ):
            run_state.atomic_publish_private_bytes(
                evidence_root / f"{artifact.scenario_id}.{kind}.json",
                _preflight_json_bytes(value),
            )


def _preflight_replay_definition(
    input_document: Any, *, executor: str
) -> dict[str, Any]:
    if not isinstance(input_document, dict):
        raise MatrixError("matrix deterministic-only preflight failed")
    oracle = input_document.get("deterministic_oracle")
    assertions = oracle.get("assertions") if isinstance(oracle, dict) else None
    if not isinstance(assertions, list):
        raise MatrixError("matrix deterministic-only preflight failed")
    retained = [
        assertion
        for assertion in assertions
        if isinstance(assertion, dict)
        and assertion.get("source", "cli_document") == "cli_document"
    ]
    if not retained or len(retained) != sum(
        isinstance(assertion, dict)
        and assertion.get("source", "cli_document") == "cli_document"
        for assertion in assertions
    ):
        raise MatrixError("matrix deterministic-only preflight failed")
    return {
        "schema": _PREFLIGHT_REPLAY_SCHEMA,
        "executor": executor,
        "assertions": retained,
    }


def _preflight_bundle_digest(
    input_document: Any,
    oracle_document: Any,
    replay_definition: Any,
) -> str:
    content = _preflight_json_bytes(
        {
            "input": input_document,
            "document": oracle_document,
            "definition": replay_definition,
        }
    )
    return hashlib.sha256(content).hexdigest()


def _replay_json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _replay_json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _replay_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _replay_select(document: Any, path: Any) -> tuple[list[Any], bool]:
    if path == "$":
        return [document], False
    if not isinstance(path, str) or not path.startswith("$.") or len(path) > 1024:
        raise ValueError("unsupported replay path")
    nodes = [document]
    plural = False
    segments: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(path[2:]):
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "." and depth == 0:
            segments.append(path[2:][start:index])
            start = index + 1
        if depth < 0:
            raise ValueError("unsupported replay path")
    if depth != 0:
        raise ValueError("unsupported replay path")
    segments.append(path[2:][start:])
    for segment in segments:
        match = re.fullmatch(r"([a-z_][a-z0-9_]*)((?:\[[^]]+\])*)", segment)
        if match is None:
            raise ValueError("unsupported replay path")
        nodes = [node[match.group(1)] for node in nodes]
        for bracket in re.findall(r"\[([^]]+)\]", match.group(2)):
            if bracket == "*":
                nodes = [item for node in nodes for item in node]
                plural = True
            elif bracket.isascii() and bracket.isdecimal():
                nodes = [node[int(bracket)] for node in nodes]
            else:
                condition = re.fullmatch(
                    r"\?\(@\.([a-z_]+)==(?:([0-9]+)|'([^']+)')\)", bracket
                )
                if condition is None:
                    raise ValueError("unsupported replay path")
                field, number, text = condition.groups()
                expected = int(number) if number is not None else text
                nodes = [
                    item
                    for node in nodes
                    for item in node
                    if isinstance(item, dict)
                    and field in item
                    and _replay_json_equal(item[field], expected)
                ]
                plural = True
            if len(nodes) > 16 * 1024:
                raise ValueError("replay selection exceeded safety limit")
    return nodes, plural


def _replay_assertion(document: Any, assertion: Any) -> bool:
    if not isinstance(assertion, dict) or set(assertion) - {
        "path",
        "operator",
        "expected",
        "source",
    }:
        raise ValueError("invalid replay assertion")
    if assertion.get("source", "cli_document") != "cli_document":
        raise ValueError("invalid replay assertion source")
    values, plural = _replay_select(document, assertion.get("path"))
    operator = assertion.get("operator")
    expected = assertion.get("expected")
    if operator == "ordered_equals":
        actual = values if plural else values[0]
        return _replay_json_equal(actual, expected)
    actual = values[0] if len(values) == 1 else values
    if operator == "equals":
        return _replay_json_equal(actual, expected)
    if operator == "contains":
        if isinstance(actual, list):
            return any(_replay_json_equal(item, expected) for item in actual)
        return expected in actual
    if operator == "omits":
        if isinstance(actual, list):
            return not any(_replay_json_equal(item, expected) for item in actual)
        return expected not in actual
    if operator == "one_of" and isinstance(expected, list) and expected:
        return any(_replay_json_equal(actual, item) for item in expected)
    raise ValueError("unsupported replay operator")


def _replay_preflight(
    oracle_document: Any, replay_definition: Any
) -> dict[str, Any]:
    if (
        not isinstance(replay_definition, dict)
        or set(replay_definition) != {"schema", "executor", "assertions"}
        or replay_definition.get("schema") != _PREFLIGHT_REPLAY_SCHEMA
        or replay_definition.get("executor") not in {"frozen_cli", "domain_oracle"}
        or not isinstance(replay_definition.get("assertions"), list)
        or not replay_definition["assertions"]
    ):
        raise ValueError("invalid preflight replay definition")
    failures = [
        dict(assertion)
        for assertion in replay_definition["assertions"]
        if not _replay_assertion(oracle_document, assertion)
    ]
    return {
        "assertions": len(replay_definition["assertions"]),
        "failed": failures,
        "passed": not failures,
    }


def validate_retained_preflight_evidence(
    matrix_dir: Path,
    inputs: run_state.MatrixInputs,
    attestation: run_state.MatrixPreflightAttestation,
    *,
    root: Path = ROOT,
) -> None:
    del root
    deterministic = tuple(
        item for item in inputs.scenarios if item.execution_support == "deterministic_only"
    )
    scenario_ids = tuple(item.scenario_id for item in deterministic)
    evidence_root = Path(matrix_dir) / "preflight"
    try:
        _require_private_dir(evidence_root)
        expected_names = {
            f"{scenario_id}.{kind}.json"
            for scenario_id in scenario_ids
            for kind in ("input", "document", "definition", "grading")
        }
        items = tuple(evidence_root.iterdir())
        if {item.name for item in items} != expected_names:
            raise MatrixError("matrix retained preflight evidence is invalid")
        attested = {item.scenario_id: item for item in attestation.scenarios}
        if set(attested) != set(scenario_ids):
            raise MatrixError("matrix retained preflight evidence is invalid")
        for scenario_id in scenario_ids:
            retained: dict[str, tuple[Any, bytes]] = {}
            for kind in ("input", "document", "definition", "grading"):
                content = _private_regular_bytes(
                    evidence_root / f"{scenario_id}.{kind}.json",
                    max_bytes=_MAX_PREFLIGHT_ARTIFACT_BYTES,
                )
                value = _strict_json_loads(content.decode("utf-8"))
                if kind != "input" and content != _preflight_json_bytes(value):
                    raise MatrixError("matrix retained preflight evidence is invalid")
                retained[kind] = (value, content)
            input_document = retained["input"][0]
            replay_definition = retained["definition"][0]
            if (
                not isinstance(input_document, dict)
                or input_document.get("id") != scenario_id
                or hashlib.sha256(retained["input"][1]).hexdigest()
                != attested[scenario_id].source_sha256
                or replay_definition
                != _preflight_replay_definition(
                    input_document,
                    executor=attested[scenario_id].executor,
                )
            ):
                raise MatrixError("matrix retained preflight evidence is invalid")
            replayed_grading = _replay_preflight(
                retained["document"][0], replay_definition
            )
            expected = attested[scenario_id]
            if (
                _preflight_bundle_digest(
                    input_document,
                    retained["document"][0],
                    replay_definition,
                )
                != expected.document_sha256
                or replayed_grading.get("passed") is not True
                or retained["grading"][0] != replayed_grading
                or hashlib.sha256(retained["grading"][1]).hexdigest()
                != expected.grading_sha256
            ):
                raise MatrixError("matrix retained preflight evidence is invalid")
    except MatrixError:
        raise MatrixError("matrix retained preflight evidence is invalid") from None
    except (IndexError, KeyError, OSError, TypeError, UnicodeError, ValueError):
        raise MatrixError("matrix retained preflight evidence is invalid") from None


def load_manifest(matrix_dir: Path) -> run_state.MatrixManifest:
    try:
        value = _read_strict_json(
            Path(matrix_dir) / "manifest.json", max_bytes=_MAX_MANIFEST_BYTES
        )
        return run_state.MatrixManifest.from_dict(value)
    except Exception:
        raise MatrixError("matrix manifest is invalid") from None


def _child_timeout_budget(
    timeout_seconds: float, turn_count: int
) -> tuple[float, float]:
    if (
        not isinstance(turn_count, int)
        or isinstance(turn_count, bool)
        or not 1 <= turn_count <= _MAX_CHILD_SCENARIO_TURNS
    ):
        raise MatrixError("matrix child timeout budget is invalid")
    try:
        per_turn_timeout = codex_driver.validate_timeout_seconds(timeout_seconds)
    except (TypeError, ValueError):
        raise MatrixError("matrix child timeout budget is invalid") from None
    outer_timeout = per_turn_timeout * turn_count + _CHILD_PROCESS_GRACE_SECONDS
    if not math.isfinite(outer_timeout):
        raise MatrixError("matrix child timeout budget is invalid")
    return per_turn_timeout, outer_timeout


def _run_child_subprocess(
    work_item: run_state.MatrixWorkItem,
    timeout_seconds: float,
    *,
    turn_count: int,
    root: Path,
    results_root: Path,
) -> ChildResult:
    if results_root.resolve() != (root / "evals" / "results").resolve():
        raise MatrixError("live child cohorts require the canonical results root")
    per_turn_timeout, outer_timeout = _child_timeout_budget(timeout_seconds, turn_count)
    argv = [
        sys.executable,
        "-m",
        "evals.runner",
        "--scenario",
        work_item.scenario_id,
        "--track",
        work_item.track,
        "--timeout-seconds",
        str(per_turn_timeout),
    ]
    if work_item.route.model is not None:
        argv.extend(("--model", work_item.route.model))
    if work_item.route.reasoning_effort is not None:
        argv.extend(("--effort", work_item.route.reasoning_effort))
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_BOOTSTRAP, *argv],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    tracker: _ChildProcessTracker | None = None
    child_released = False
    try:
        tracker = _ChildProcessTracker(process.pid)
        _wait_for_child_bootstrap(process, tracker)
        _continue_child_process(process)
        child_released = True
        _stdout, stderr = process.communicate(timeout=outer_timeout)
    except subprocess.TimeoutExpired:
        if child_released and tracker is not None:
            tracker.stop()
            _terminate_child_process_tree(process, tracker=tracker)
        else:
            if tracker is not None:
                tracker.stop()
            _terminate_untracked_child_process(process)
        raise MatrixError("matrix child cohort timed out") from None
    except BaseException:
        if child_released and tracker is not None:
            tracker.stop()
            _terminate_child_process_tree(process, tracker=tracker)
        else:
            if tracker is not None:
                tracker.stop()
            _terminate_untracked_child_process(process)
        raise
    assert tracker is not None
    tracker.stop()
    _cleanup_child_process_tree(process, tracker=tracker, terminate_root=False)
    matches = _CHILD_REPORT.findall(stderr)
    if not matches or _SAFE_COMPONENT.fullmatch(matches[-1]) is None:
        raise MatrixError("child cohort did not publish a bounded result")
    run_dir = _safe_child(results_root, results_root / matches[-1])
    return ChildResult(exit_code=process.returncode, run_dir=run_dir)


@dataclass(frozen=True, slots=True)
class _ProcessRecord:
    parent_pid: int
    process_group: int
    state: str
    kernel_identity: str


_ProcessIdentity = tuple[int, str]


def _process_table() -> dict[int, _ProcessRecord]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,state="],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        table: dict[int, _ProcessRecord] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                raise ValueError
            pid_text, state = fields
            pid = int(pid_text)
            if (record := _kernel_process_record(pid, state)) is not None:
                table[pid] = record
        return table
    except (OSError, subprocess.SubprocessError, ValueError):
        raise MatrixError("matrix child process cleanup failed") from None


class _DarwinProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("pbi_rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@cache
def _darwin_proc_pidinfo() -> Any:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
    except (AttributeError, OSError):
        raise MatrixError("matrix child process cleanup failed") from None
    return proc_pidinfo


def _darwin_process_record(pid: int, state: str) -> _ProcessRecord | None:
    info = _DarwinProcBSDInfo()
    size = ctypes.sizeof(info)
    copied = _darwin_proc_pidinfo()(pid, 3, 0, ctypes.byref(info), size)
    if copied != size or info.pbi_pid != pid:
        return None
    return _ProcessRecord(
        parent_pid=info.pbi_ppid,
        process_group=info.pbi_pgid,
        state=state,
        kernel_identity=(
            f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
        ),
    )


def _linux_process_record(pid: int) -> _ProcessRecord | None:
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError):
        return None
    closing_parenthesis = stat_line.rfind(")")
    if not stat_line.startswith(f"{pid} (") or closing_parenthesis < len(str(pid)) + 2:
        raise ValueError
    fields = stat_line[closing_parenthesis + 2 :].split()
    if len(fields) < 20:
        raise ValueError
    return _ProcessRecord(
        parent_pid=int(fields[1]),
        process_group=int(fields[2]),
        state=fields[0],
        kernel_identity=f"linux:{fields[19]}",
    )


def _kernel_process_record(pid: int, state: str) -> _ProcessRecord | None:
    if sys.platform == "darwin":
        return _darwin_process_record(pid, state)
    if sys.platform.startswith("linux"):
        return _linux_process_record(pid)
    raise MatrixError("matrix child process cleanup failed")


def _descendant_process_ids(
    root_pid: int, table: Mapping[int, _ProcessRecord]
) -> set[int]:
    descendants: set[int] = set()
    parents = {root_pid}
    while parents:
        children = {
            pid
            for pid, record in table.items()
            if record.parent_pid in parents and pid not in descendants
        }
        descendants.update(children)
        parents = children
    return descendants


def _live_process_ids(
    process_ids: set[int], table: Mapping[int, _ProcessRecord]
) -> set[int]:
    return {
        pid
        for pid in process_ids
        if pid in table and not table[pid].state.startswith("Z")
    }


class _ChildProcessTracker:
    def __init__(self, root_pid: int):
        self.root_pid = root_pid
        self.tracked: dict[int, str] = {}
        self.root_group = 0
        self._root_group_open = True
        self._stop = threading.Event()
        self._error: MatrixError | None = None
        table = _process_table()
        root = table.get(root_pid)
        if (
            root is None
            or root.process_group != root_pid
            or root.process_group == os.getpgrp()
        ):
            raise MatrixError("matrix child process cleanup failed")
        self.root_group = root.process_group
        self.tracked[root_pid] = root.kernel_identity
        self.capture(table)
        self._thread = threading.Thread(
            target=self._monitor,
            name=f"matrix-child-monitor-{root_pid}",
            daemon=True,
        )
        self._thread.start()

    def _monitor(self) -> None:
        while not self._stop.wait(0.01):
            try:
                self.capture()
            except MatrixError as error:
                self._error = error
                return

    def capture(self, table: Mapping[int, _ProcessRecord] | None = None) -> None:
        current = dict(table or _process_table())
        matching = {
            pid
            for pid, kernel_identity in self.tracked.items()
            if (record := current.get(pid)) is not None
            and record.kernel_identity == kernel_identity
        }
        parents = set(matching)
        while parents:
            children = {
                pid
                for pid, record in current.items()
                if record.parent_pid in parents
                and self.tracked.get(pid, record.kernel_identity)
                == record.kernel_identity
            }
            for pid in children:
                self.tracked.setdefault(pid, current[pid].kernel_identity)
            parents = children - matching
            matching.update(children)
        if self._root_group_open:
            group_members = {
                pid
                for pid, record in current.items()
                if record.process_group == self.root_group
            }
            if group_members:
                for pid in group_members:
                    self.tracked.setdefault(pid, current[pid].kernel_identity)
            else:
                self._root_group_open = False

    def live_identities(
        self, table: Mapping[int, _ProcessRecord] | None = None
    ) -> set[_ProcessIdentity]:
        current = dict(table or _process_table())
        return {
            (pid, kernel_identity)
            for pid, kernel_identity in self.tracked.items()
            if (record := current.get(pid)) is not None
            and record.kernel_identity == kernel_identity
            and not record.state.startswith("Z")
        }

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=6)
        if self._thread.is_alive():
            self._error = MatrixError("matrix child process cleanup failed")
        try:
            self.capture()
        except MatrixError as error:
            self._error = error

    def require_healthy(self) -> None:
        if self._error is not None:
            raise MatrixError("matrix child process cleanup failed")


def _wait_for_child_tree(
    process: subprocess.Popen[str], tracker: _ChildProcessTracker, deadline: float
) -> bool:
    while True:
        process.poll()
        table = _process_table()
        tracker.capture(table)
        live = tracker.live_identities(table)
        if process.returncode is not None and not live:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _signal_child_tree(
    tracker: _ChildProcessTracker, sig: signal.Signals
) -> None:
    tracker.capture()
    for pid, kernel_identity in sorted(tracker.live_identities(), reverse=True):
        current = _process_table().get(pid)
        if current is None or current.kernel_identity != kernel_identity:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _continue_child_process(process: subprocess.Popen[str]) -> None:
    try:
        os.kill(process.pid, signal.SIGCONT)
    except ProcessLookupError:
        pass


def _wait_for_child_bootstrap(
    process: subprocess.Popen[str], tracker: _ChildProcessTracker
) -> None:
    deadline = time.monotonic() + _CHILD_FORCE_KILL_SECONDS
    while True:
        table = _process_table()
        tracker.capture(table)
        root = table.get(process.pid)
        if (
            root is None
            or tracker.tracked.get(process.pid) != root.kernel_identity
        ):
            raise MatrixError("matrix child process cleanup failed")
        if root.state.startswith("T"):
            return
        if time.monotonic() >= deadline:
            raise MatrixError("matrix child process cleanup failed")
        time.sleep(0.01)


def _close_child_process_pipes(process: subprocess.Popen[str]) -> None:
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


def _terminate_untracked_child_process(process: subprocess.Popen[str]) -> None:
    try:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=_CHILD_FORCE_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            raise MatrixError("matrix child process cleanup failed") from None
    finally:
        _close_child_process_pipes(process)


def _cleanup_child_process_tree(
    process: subprocess.Popen[str],
    *,
    tracker: _ChildProcessTracker,
    terminate_root: bool,
) -> None:
    try:
        if terminate_root:
            _signal_child_tree(tracker, signal.SIGTERM)
        graceful_deadline = time.monotonic() + _CHILD_TERMINATION_GRACE_SECONDS
        if not _wait_for_child_tree(process, tracker, graceful_deadline):
            _signal_child_tree(tracker, signal.SIGTERM)
            term_deadline = time.monotonic() + _CHILD_FORCE_KILL_SECONDS
            if not _wait_for_child_tree(process, tracker, term_deadline):
                _signal_child_tree(tracker, signal.SIGKILL)
                kill_deadline = time.monotonic() + _CHILD_FORCE_KILL_SECONDS
                if not _wait_for_child_tree(process, tracker, kill_deadline):
                    raise MatrixError("matrix child process cleanup failed")
        try:
            process.wait(timeout=_CHILD_FORCE_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            raise MatrixError("matrix child process cleanup failed") from None
        tracker.require_healthy()
    finally:
        _close_child_process_pipes(process)


def _terminate_child_process_tree(
    process: subprocess.Popen[str], *, tracker: _ChildProcessTracker | None = None
) -> None:
    if tracker is None:
        try:
            owned_tracker = _ChildProcessTracker(process.pid)
        except BaseException:
            _terminate_untracked_child_process(process)
            raise
        owned_tracker.stop()
    else:
        owned_tracker = tracker
    _cleanup_child_process_tree(
        process, tracker=owned_tracker, terminate_root=True
    )


def _private_regular_bytes(path: Path, *, max_bytes: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > max_bytes
        ):
            raise MatrixError("child cohort artifact is not private")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining and (
            chunk := os.read(descriptor, min(1024 * 1024, remaining))
        ):
            chunks.append(chunk)
            remaining -= len(chunk)
        source_bytes = b"".join(chunks)
        after = os.fstat(descriptor)
        path_stat = path.lstat()
        signature = lambda item: (  # noqa: E731
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if (
            len(source_bytes) > max_bytes
            or signature(before) != signature(after)
            or signature(after) != signature(path_stat)
        ):
            raise MatrixError("child cohort artifact changed while being read")
        return source_bytes
    except OSError:
        raise MatrixError("child cohort artifact is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _private_regular_hash(path: Path) -> str:
    return hashlib.sha256(
        _private_regular_bytes(path, max_bytes=_MAX_MANIFEST_BYTES)
    ).hexdigest()


def validate_attempt_start(
    path: Path,
    work_item: run_state.MatrixWorkItem,
    completion: run_state.MatrixCompletion,
) -> str:
    try:
        started = _read_strict_json(path)
    except MatrixError:
        raise MatrixError("matrix attempt start is invalid") from None
    if not isinstance(started, dict) or set(started) != {
        "schema",
        "attempt_id",
        "work_item_id",
        "started_at",
    }:
        raise MatrixError("matrix attempt start is invalid")
    if (
        started.get("schema") != "steam-agent-eval-matrix-attempt/0.1"
        or started.get("attempt_id") != completion.attempt_id
        or started.get("work_item_id") != work_item.work_item_id
        or completion.work_item_id != work_item.work_item_id
    ):
        raise MatrixError("matrix attempt start is invalid")
    try:
        started_at = run_state._parse_time(started.get("started_at"))  # noqa: SLF001
        completed_at = run_state._parse_time(completion.completed_at)  # noqa: SLF001
    except run_state.ManifestStateError:
        raise MatrixError("matrix attempt start is invalid") from None
    try:
        digest = _private_regular_hash(path)
    except MatrixError:
        raise MatrixError("matrix attempt start is invalid") from None
    if started_at > completed_at or digest != completion.started_sha256:
        raise MatrixError("matrix attempt start is invalid")
    return started["started_at"]


def _exact_route_history(
    generator: Mapping[str, Any],
    work_item: run_state.MatrixWorkItem,
    *,
    expected_turn_count: int,
) -> bool:
    model = work_item.route.model
    effort = work_item.route.reasoning_effort
    effective_models = generator.get("effective_model_by_turn")
    effective_efforts = generator.get("effective_reasoning_effort_by_turn")
    observed_models = generator.get("observed_models_by_turn")
    observed_efforts = generator.get("observed_reasoning_efforts_by_turn")
    if (
        model is None
        or effort is None
        or not isinstance(expected_turn_count, int)
        or isinstance(expected_turn_count, bool)
        or expected_turn_count < 1
        or not all(
            isinstance(items, list) and items
            for items in (
                effective_models,
                effective_efforts,
                observed_models,
                observed_efforts,
            )
        )
    ):
        return False
    if any(
        len(items) != expected_turn_count
        for items in (
            effective_models,
            effective_efforts,
            observed_models,
            observed_efforts,
        )
    ):
        return False
    return (
        all(item == model for item in effective_models)
        and all(item == effort for item in effective_efforts)
        and all(
            isinstance(history, list)
            and bool(history)
            and all(item == model for item in history)
            for history in observed_models
        )
        and all(
            isinstance(history, list)
            and bool(history)
            and all(item == effort for item in history)
            for history in observed_efforts
        )
    )


def _summary_pass(metrics: Mapping[str, Any]) -> bool | None:
    try:
        values = [
            metrics[layer]["passed"]
            for layer in ("agent_turns", "tool_policy", "oracle", "claims", "privacy")
        ]
    except (KeyError, TypeError):
        raise MatrixError("child cohort report metrics are invalid") from None
    if any(value is not None and not isinstance(value, bool) for value in values):
        raise MatrixError("child cohort report metrics are invalid")
    if False in values:
        return False
    if None in values:
        return None
    return True


def _validate_controls_artifact(value: Any) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "passed", "controls"}
        or value.get("schema_version") != controls.CONTROL_SCHEMA_VERSION
        or value.get("passed") is not True
        or not isinstance(value.get("controls"), list)
        or len(value["controls"]) != len(controls.SCRIPTED_CONTROLS)
    ):
        raise MatrixError("child cohort controls artifact is invalid")
    for document, expected_case in zip(
        value["controls"], controls.SCRIPTED_CONTROLS, strict=True
    ):
        expected_layers = expected_case.expected_layer_map()
        if (
            not isinstance(document, dict)
            or set(document)
            != {"id", "expected_layers", "observed_layers", "passed"}
            or document.get("id") != expected_case.control_id
            or document.get("expected_layers") != expected_layers
            or document.get("observed_layers") != expected_layers
            or document.get("passed") is not True
        ):
            raise MatrixError("child cohort controls artifact is invalid")


def validate_child_result(
    child: ChildResult,
    work_item: run_state.MatrixWorkItem,
    matrix: run_state.MatrixManifest,
    *,
    results_root: Path = RESULTS_ROOT,
) -> ValidatedChildResult:
    if (
        child.unavailable_reason is not None
        or child.run_dir is None
        or child.exit_code is None
    ):
        raise MatrixError("unavailable child result is not an observation")
    run_dir = _safe_child(Path(results_root), child.run_dir)
    if _SAFE_COMPONENT.fullmatch(run_dir.name) is None:
        raise MatrixError("child cohort identifier is invalid")
    try:
        _require_private_dir(run_dir)
        manifest_bytes = _private_regular_bytes(
            run_dir / "manifest.json", max_bytes=_MAX_MANIFEST_BYTES
        )
        manifest = run_state.RunManifest.from_dict(
            _strict_json_loads(manifest_bytes.decode("utf-8"))
        )
    except Exception:
        raise MatrixError("child cohort manifest is invalid") from None
    if manifest.run_id != run_dir.name:
        raise MatrixError("child cohort identity does not match its directory")
    scenario = next(
        item
        for item in matrix.inputs.scenarios
        if item.scenario_id == work_item.scenario_id
    )
    if manifest.source_digest != scenario.child_source_digest:
        raise MatrixError("child cohort snapshot does not match campaign inputs")
    if (
        child.exit_code not in {0, 1, 3}
        or manifest.state is not run_state.RunState.COMPLETED
        or manifest.commit != matrix.inputs.commit
        or manifest.cleanliness != "clean"
        or manifest.controls_passed is not True
        or manifest.track != work_item.track
        or manifest.scenario_ids != (work_item.scenario_id,)
        or manifest.completed_scenario_ids != (work_item.scenario_id,)
        or len(manifest.requested_routes) != 1
        or manifest.requested_routes[0].model != work_item.route.model
        or manifest.requested_routes[0].reasoning_effort
        != work_item.route.reasoning_effort
    ):
        raise MatrixError("child cohort does not match its work item")
    if (
        dict(manifest.fixture_hashes).get(work_item.scenario_id)
        != scenario.source_sha256
    ):
        raise MatrixError("child cohort scenario digest does not match")
    child_tools = dict(manifest.tool_versions)
    if any(
        child_tools.get(name) != version
        for name, version in matrix.inputs.tool_versions
    ):
        raise MatrixError("child cohort tool versions do not match")
    if manifest.control_set_version != controls.CONTROL_SCHEMA_VERSION:
        raise MatrixError("child cohort controls version does not match")

    scenario_dir = _safe_child(run_dir, run_dir / work_item.scenario_id)
    _require_private_dir(scenario_dir)
    report_path = scenario_dir / "report.json"
    transcript_path = scenario_dir / "transcript.jsonl"
    summary_path = run_dir / "summary.json"
    controls_path = run_dir / "controls.json"
    try:
        report_bytes = _private_regular_bytes(
            report_path, max_bytes=64 * 1024 * 1024
        )
        summary_bytes = _private_regular_bytes(
            summary_path, max_bytes=64 * 1024 * 1024
        )
        transcript_bytes = _private_regular_bytes(
            transcript_path, max_bytes=64 * 1024 * 1024
        )
        report = _strict_json_loads(report_bytes.decode("utf-8"))
        summary = _strict_json_loads(summary_bytes.decode("utf-8"))
    except (MatrixError, UnicodeError, ValueError):
        raise MatrixError("child cohort report is invalid") from None
    try:
        controls_bytes = _private_regular_bytes(
            controls_path, max_bytes=1024 * 1024
        )
        controls_document = _strict_json_loads(controls_bytes.decode("utf-8"))
        _validate_controls_artifact(controls_document)
        if controls_bytes != run_state._strict_json_bytes(controls_document):  # noqa: SLF001
            raise MatrixError("child cohort controls artifact is invalid")
    except (MatrixError, UnicodeError, ValueError):
        raise MatrixError("child cohort controls artifact is invalid") from None
    if (
        not isinstance(report, dict)
        or not isinstance(summary, list)
        or len(summary) != 1
        or not isinstance(summary[0], dict)
    ):
        raise MatrixError("child cohort report is invalid")
    generator = report.get("generator")
    turns = report.get("turns")
    if (
        report.get("artifact_schema_version") != "steam-agent-eval-report/0.2"
        or report.get("scenario") != work_item.scenario_id
        or report.get("track") != work_item.track
        or report.get("fixture_sha256") != scenario.source_sha256
        or not isinstance(generator, dict)
        or not isinstance(turns, list)
        or len(turns) != scenario.turn_count
        or not all(isinstance(turn, dict) for turn in turns)
        or generator.get("requested_model") != work_item.route.model
        or generator.get("requested_reasoning_effort")
        != work_item.route.reasoning_effort
        or generator.get("requested_route_confirmed") is not True
        or not _exact_route_history(
            generator, work_item, expected_turn_count=scenario.turn_count
        )
        or summary[0].get("scenario") != work_item.scenario_id
    ):
        raise MatrixError("child cohort report does not match its work item")
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    transcript_hash = hashlib.sha256(transcript_bytes).hexdigest()
    passed = _summary_pass(report.get("metrics", {}))
    expected_exit = {True: 0, False: 1, None: 3}[passed]
    expected_artifacts = {
        "report.json": report_hash,
        "transcript.jsonl": transcript_hash,
    }
    if (
        summary[0].get("track") != work_item.track
        or summary[0].get("passed") is not passed
        or summary[0].get("layers")
        != {
            layer: report["metrics"][layer]["passed"]
            for layer in ("agent_turns", "tool_policy", "oracle", "claims", "privacy")
        }
        or summary[0].get("artifacts") != expected_artifacts
        or child.exit_code != expected_exit
    ):
        raise MatrixError("child cohort summary does not match its artifacts")
    hashes = tuple(
        sorted(
            {
                "manifest.json": hashlib.sha256(manifest_bytes).hexdigest(),
                "controls.json": hashlib.sha256(controls_bytes).hexdigest(),
                "summary.json": hashlib.sha256(summary_bytes).hexdigest(),
                "report.json": report_hash,
                "transcript.jsonl": transcript_hash,
            }.items()
        )
    )
    return ValidatedChildResult(
        child_run_id=run_dir.name,
        artifact_hashes=hashes,
        manifest=manifest,
        report=report,
        summary=summary[0],
        manifest_bytes=manifest_bytes,
        report_bytes=report_bytes,
        summary_bytes=summary_bytes,
        transcript_bytes=transcript_bytes,
        controls_bytes=controls_bytes,
    )


def _private_attempt_file(path: Path) -> None:
    try:
        item_stat = path.lstat()
    except OSError:
        raise MatrixError("matrix attempt history is unavailable") from None
    if not stat.S_ISREG(item_stat.st_mode) or stat.S_IMODE(item_stat.st_mode) != 0o600:
        raise MatrixError("matrix attempt history is invalid")


def _validate_attempt_staging_directory(path: Path) -> bool:
    match = _ATTEMPT_INITIALIZATION.fullmatch(path.name)
    if match is None:
        return False
    try:
        _require_private_dir(path)
        names = {child.name for child in path.iterdir()}
        if not names <= {"started.json"}:
            raise MatrixError("matrix attempt initialization is invalid")
        if not names:
            return True
        started_path = path / "started.json"
        _private_attempt_file(started_path)
        started = _read_strict_json(started_path)
        if not isinstance(started, dict) or set(started) != {
            "schema",
            "attempt_id",
            "work_item_id",
            "started_at",
        }:
            raise MatrixError("matrix attempt initialization is invalid")
        if (
            started.get("schema") != "steam-agent-eval-matrix-attempt/0.1"
            or started.get("attempt_id") != match.group("attempt_id")
            or started.get("work_item_id") != path.parent.name
        ):
            raise MatrixError("matrix attempt initialization is invalid")
        run_state._parse_time(started.get("started_at"))  # noqa: SLF001
        return True
    except Exception:
        raise MatrixError("matrix attempt initialization is invalid") from None


def validate_attempt_artifacts(
    attempt_dir: Path,
    *,
    work_item_id: str,
) -> ValidatedAttemptArtifacts:
    attempt_dir = Path(attempt_dir)
    attempt_id = attempt_dir.name
    try:
        if re.fullmatch(r"attempt-[0-9]{6}", attempt_id) is None:
            raise MatrixError("matrix attempt history is invalid")
        _require_private_dir(attempt_dir)
        names = {item.name for item in attempt_dir.iterdir()}
        if (
            not names <= {"started.json", "result.json", "failure.json"}
            or "started.json" not in names
            or {"result.json", "failure.json"} <= names
        ):
            raise MatrixError("matrix attempt history is invalid")
        try:
            started_bytes = _private_regular_bytes(
                attempt_dir / "started.json", max_bytes=_MAX_CONFIG_BYTES
            )
            started = _strict_json_loads(started_bytes.decode("utf-8"))
            if not isinstance(started, dict) or set(started) != {
                "schema",
                "attempt_id",
                "work_item_id",
                "started_at",
            }:
                raise ValueError
            if (
                started.get("schema") != "steam-agent-eval-matrix-attempt/0.1"
                or started.get("attempt_id") != attempt_id
                or started.get("work_item_id") != work_item_id
            ):
                raise ValueError
            started_at = started.get("started_at")
            parsed_started = run_state._parse_time(started_at)  # noqa: SLF001
            canonical_started = {
                "schema": "steam-agent-eval-matrix-attempt/0.1",
                "attempt_id": attempt_id,
                "work_item_id": work_item_id,
                "started_at": started_at,
            }
            if started_bytes != run_state._strict_json_bytes(  # noqa: SLF001
                canonical_started
            ):
                raise ValueError
        except Exception:
            raise MatrixError("matrix attempt start is invalid") from None
        hashes = {"started.json": hashlib.sha256(started_bytes).hexdigest()}
        completion: run_state.MatrixCompletion | None = None
        failure: tuple[str, str] | None = None
        if "result.json" in names:
            result_bytes = _private_regular_bytes(
                attempt_dir / "result.json", max_bytes=_MAX_CONFIG_BYTES
            )
            result = _strict_json_loads(result_bytes.decode("utf-8"))
            if (
                not isinstance(result, dict)
                or set(result) != {"schema", "completion"}
                or result.get("schema")
                != "steam-agent-eval-matrix-attempt-result/0.1"
            ):
                raise MatrixError("matrix attempt history is invalid")
            completion = run_state.MatrixCompletion.from_dict(result["completion"])
            canonical_result = {
                "schema": "steam-agent-eval-matrix-attempt-result/0.1",
                "completion": completion.to_dict(),
            }
            if (
                result != canonical_result
                or completion.work_item_id != work_item_id
                or completion.attempt_id != attempt_id
            ):
                raise MatrixError("matrix attempt history is invalid")
            if result_bytes != run_state._strict_json_bytes(  # noqa: SLF001
                canonical_result
            ):
                raise MatrixError("matrix attempt result is invalid")
            if (
                completion.started_sha256 != hashes["started.json"]
                or parsed_started
                > run_state._parse_time(completion.completed_at)  # noqa: SLF001
            ):
                raise MatrixError("matrix attempt start is invalid")
            hashes["result.json"] = hashlib.sha256(result_bytes).hexdigest()
        elif "failure.json" in names:
            failure_bytes = _private_regular_bytes(
                attempt_dir / "failure.json", max_bytes=_MAX_CONFIG_BYTES
            )
            failure_document = _strict_json_loads(failure_bytes.decode("utf-8"))
            if (
                not isinstance(failure_document, dict)
                or set(failure_document) != {"schema", "reason", "error_type"}
                or failure_document.get("schema")
                != "steam-agent-eval-matrix-attempt-failure/0.1"
                or failure_document.get("reason") != "child_cohort_invalid"
                or not isinstance(failure_document.get("error_type"), str)
                or _SAFE_COMPONENT.fullmatch(failure_document["error_type"]) is None
            ):
                raise MatrixError("matrix attempt history is invalid")
            failure = (
                failure_document["reason"],
                failure_document["error_type"],
            )
            if failure_bytes != run_state._strict_json_bytes(  # noqa: SLF001
                {
                    "schema": "steam-agent-eval-matrix-attempt-failure/0.1",
                    "reason": failure[0],
                    "error_type": failure[1],
                }
            ):
                raise MatrixError("matrix attempt history is invalid")
            hashes["failure.json"] = hashlib.sha256(failure_bytes).hexdigest()
        return ValidatedAttemptArtifacts(
            attempt_id=attempt_id,
            work_item_id=work_item_id,
            started_at=started_at,
            artifact_hashes=tuple(sorted(hashes.items())),
            completion=completion,
            failure=failure,
        )
    except MatrixError as error:
        if str(error) in {
            "matrix attempt result is invalid",
            "matrix attempt start is invalid",
        }:
            raise
        raise MatrixError("matrix attempt history is invalid") from None
    except (TypeError, UnicodeError, ValueError, run_state.ManifestStateError):
        raise MatrixError("matrix attempt history is invalid") from None


def _attempt_directories(item_root: Path) -> list[Path]:
    _require_private_dir(item_root)
    attempts: list[Path] = []
    for item in sorted(item_root.iterdir()):
        if _validate_attempt_staging_directory(item):
            continue
        if re.fullmatch(r"attempt-[0-9]{6}", item.name) is None:
            raise MatrixError("matrix work directory is unexpected")
        validate_attempt_artifacts(item, work_item_id=item_root.name)
        attempts.append(item)
    expected = [f"attempt-{index:06d}" for index in range(1, len(attempts) + 1)]
    if [item.name for item in attempts] != expected:
        raise MatrixError("matrix attempt history is not contiguous")
    return attempts


def _verify_qualitative_artifact_directory(path: Path) -> None:
    _require_private_dir(path)
    for item in path.iterdir():
        if _QUALITATIVE_ARTIFACT.fullmatch(item.name) is None:
            raise MatrixError(
                "qualitative artifact directory contains unexpected nodes"
            )
        _private_attempt_file(item)


def _verify_calibration_artifact_directory(path: Path) -> None:
    _require_private_dir(path)
    for item in path.iterdir():
        if _SAFE_COMPONENT.fullmatch(item.name) is None:
            raise MatrixError("matrix calibration directory contains unexpected nodes")
        _private_attempt_file(item)


def _verify_preflight_artifact_directory(path: Path) -> None:
    _require_private_dir(path)
    for item in path.iterdir():
        parts = item.name.rsplit(".", 2)
        if (
            len(parts) != 3
            or _SAFE_COMPONENT.fullmatch(parts[0]) is None
            or parts[1] not in {"input", "document", "definition", "grading"}
            or parts[2] != "json"
        ):
            raise MatrixError("matrix preflight directory contains unexpected nodes")
        _private_attempt_file(item)


def _validate_bound_acceptance_artifact(
    matrix_dir: Path, manifest: run_state.MatrixManifest
) -> None:
    if manifest.acceptance_sha256 is None:
        return
    try:
        content = _private_regular_bytes(
            Path(matrix_dir) / "acceptance.json",
            max_bytes=_MAX_ACCEPTANCE_ARTIFACT_BYTES,
        )
        document = _strict_json_loads(content.decode("ascii"))
        if (
            content != _canonical_json_bytes(document)
            or hashlib.sha256(content).hexdigest() != manifest.acceptance_sha256
            or not isinstance(document, dict)
            or document.get("campaign_kind") != "screen"
            or document.get("status") != "complete"
            or document.get("finalized_at") != manifest.acceptance_finalized_at
        ):
            raise MatrixError("matrix acceptance finalization is invalid")
    except MatrixError:
        raise MatrixError("matrix acceptance finalization is invalid") from None
    except (OSError, UnicodeError, ValueError):
        raise MatrixError("matrix acceptance finalization is invalid") from None


def _verify_matrix_layout(matrix_dir: Path, manifest: run_state.MatrixManifest) -> None:
    _require_private_dir(matrix_dir)
    artifact_directories = {"judgments", "adjudications"}
    allowed_top = {
        "config.json",
        "manifest.json",
        "matrix.lock",
        "work",
        "calibration",
        "preflight",
        *artifact_directories,
    }
    if manifest.campaign.campaign_kind == "screen":
        allowed_top.add("acceptance.json")
    top_names = {item.name for item in matrix_dir.iterdir()}
    if (
        not {"calibration", "preflight", "config.json", "manifest.json"} <= top_names
        or (
            manifest.acceptance_sha256 is not None
            and "acceptance.json" not in top_names
        )
        or not top_names <= allowed_top
    ):
        raise MatrixError("matrix directory contains unexpected nodes")
    for name in top_names - {
        "calibration",
        "preflight",
        "work",
        *artifact_directories,
    }:
        _private_attempt_file(matrix_dir / name)
    _verify_calibration_artifact_directory(matrix_dir / "calibration")
    _verify_preflight_artifact_directory(matrix_dir / "preflight")
    _validate_bound_acceptance_artifact(matrix_dir, manifest)
    for name in top_names & artifact_directories:
        _verify_qualitative_artifact_directory(matrix_dir / name)
    work_root = matrix_dir / "work"
    if "work" not in top_names:
        return
    _require_private_dir(work_root)
    expected_work_ids = {item.work_item_id for item in manifest.work_items}
    for item_root in work_root.iterdir():
        if item_root.name not in expected_work_ids:
            raise MatrixError("matrix work directory is unexpected")
        _attempt_directories(item_root)


def _next_attempt_dir(
    matrix_dir: Path,
    work_item_id: str,
    manifest: run_state.MatrixManifest,
    *,
    started_at: datetime,
) -> tuple[str, Path, str]:
    _verify_matrix_layout(matrix_dir, manifest)
    work_root = matrix_dir / "work"
    if not work_root.exists():
        _ensure_private_dir(work_root)
    else:
        _require_private_dir(work_root)
    item_root = work_root / work_item_id
    if not item_root.exists():
        _ensure_private_dir(item_root)
    existing = _attempt_directories(item_root)
    attempt_id = f"attempt-{len(existing) + 1:06d}"
    attempt_dir = item_root / attempt_id
    try:
        staging_dir = Path(
            tempfile.mkdtemp(prefix=f".attempt-init-{attempt_id}-", dir=item_root)
        )
        staging_dir.chmod(0o700)
        run_state.atomic_publish_private_json(
            staging_dir / "started.json",
            {
                "schema": "steam-agent-eval-matrix-attempt/0.1",
                "attempt_id": attempt_id,
                "work_item_id": work_item_id,
                "started_at": started_at.isoformat().replace("+00:00", "Z"),
            },
        )
        started_sha256 = _private_regular_hash(staging_dir / "started.json")
        os.rename(staging_dir, attempt_dir)
    except FileExistsError:
        raise MatrixError("matrix attempt initialization collided") from None
    except OSError:
        raise MatrixError("matrix attempt initialization failed") from None
    return attempt_id, attempt_dir, started_sha256


def _verify_resume(
    matrix_dir: Path,
    config: LoadedConfig,
    current_inputs: run_state.MatrixInputs,
    manifest: run_state.MatrixManifest,
    *,
    results_root: Path,
    root: Path,
    revalidate_preflight: bool,
) -> None:
    try:
        manifest.preflight_attestation.require_matches(current_inputs)
    except run_state.ManifestStateError:
        raise MatrixError(
            "matrix deterministic-only preflight attestation is invalid"
        ) from None
    _verify_qualification_source(
        config,
        results_root,
        started_at=run_state._parse_time(manifest.started_at),  # noqa: SLF001
    )
    try:
        persisted_config = (matrix_dir / "config.json").read_bytes()
    except OSError:
        raise MatrixError("matrix config history is unavailable") from None
    current_plan = resolve_plan(config, current_inputs)
    if (
        hashlib.sha256(persisted_config).hexdigest() != manifest.config_sha256
        or config.sha256 != manifest.config_sha256
        or config.source_bytes != persisted_config
        or current_inputs != manifest.inputs
        or config.campaign != manifest.campaign
        or config.campaign.sha256 != manifest.campaign_sha256
        or current_plan != manifest.work_items
        or plan_sha256(current_plan) != manifest.plan_sha256
    ):
        raise MatrixError("matrix resume provenance does not match")
    validate_retained_calibrated_assets(matrix_dir, config)
    if revalidate_preflight:
        validate_retained_preflight_evidence(
            matrix_dir,
            current_inputs,
            manifest.preflight_attestation,
            root=root,
        )
    _verify_observation_freshness(manifest.completions)
    for work_item, completion in zip(
        manifest.work_items, manifest.completions, strict=False
    ):
        attempt_start_path = (
            matrix_dir
            / "work"
            / work_item.work_item_id
            / completion.attempt_id
            / "started.json"
        )
        attempt_result_path = (
            matrix_dir
            / "work"
            / work_item.work_item_id
            / completion.attempt_id
            / "result.json"
        )
        validate_attempt_start(attempt_start_path, work_item, completion)
        attempt_result = _read_strict_json(attempt_result_path)
        if (
            not isinstance(attempt_result, dict)
            or set(attempt_result)
            != {
                "schema",
                "completion",
            }
            or (
                attempt_result.get("schema")
                != "steam-agent-eval-matrix-attempt-result/0.1"
                or attempt_result.get("completion") != completion.to_dict()
            )
        ):
            raise MatrixError("matrix attempt history is invalid")
        if completion.outcome == "observed":
            assert completion.child_run_id is not None
            assert completion.child_exit_code is not None
            child_dir = _safe_child(
                results_root, results_root / completion.child_run_id
            )
            child = ChildResult(completion.child_exit_code, child_dir)
            validated_child = validate_child_result(
                child, work_item, manifest, results_root=results_root
            )
            if (
                validated_child.child_run_id != completion.child_run_id
                or validated_child.artifact_hashes != completion.artifact_hashes
            ):
                raise MatrixError("matrix committed artifact changed")


def _verify_qualification_source(
    config: LoadedConfig,
    results_root: Path,
    *,
    started_at: datetime,
) -> None:
    campaign = config.campaign
    if campaign.campaign_kind != "qualification":
        return
    assert campaign.source_screen_matrix_id is not None
    assert campaign.source_screen_manifest_sha256 is not None
    assert campaign.source_screen_acceptance_sha256 is not None
    assert campaign.source_screen_qualitative_evidence_sha256 is not None
    try:
        from evals.runner import acceptance

        screen_dir = _safe_child(
            results_root,
            results_root / campaign.source_screen_matrix_id,
        )
        decision, content, inspected = acceptance.load_finalized_screen(screen_dir)
        configured_routes = {
            run_state.MatrixRoute(item["model"], item["reasoning_effort"])
            for item in config.document["routes"]
        }
        if (
            inspected.manifest.matrix_id != campaign.source_screen_matrix_id
            or (
                inspected.manifest.acceptance_source_sha256
                if inspected.manifest.acceptance_sha256 is not None
                else inspected.manifest_sha256
            )
            != campaign.source_screen_manifest_sha256
            or hashlib.sha256(content).hexdigest()
            != campaign.source_screen_acceptance_sha256
            or decision.qualitative_evidence_sha256
            != campaign.source_screen_qualitative_evidence_sha256
            or set(decision.survivors) != configured_routes
            or decision.finalized_at is None
            or started_at
            <= run_state._parse_time(  # noqa: SLF001
                decision.finalized_at
            )
        ):
            raise MatrixError(
                "qualification source screen acceptance does not match"
            )
    except MatrixError:
        raise
    except (acceptance.AcceptanceError, KeyError, TypeError, ValueError):
        raise MatrixError(
            "qualification source screen acceptance is invalid"
        ) from None


def _verify_observation_freshness(
    completions: Sequence[run_state.MatrixCompletion],
) -> None:
    child_run_ids: set[str] = set()
    for completion in completions:
        if completion.outcome != "observed":
            continue
        assert completion.child_run_id is not None
        if completion.child_run_id in child_run_ids:
            raise MatrixError("matrix observed completion is not fresh")
        child_run_ids.add(completion.child_run_id)


def execute_matrix(
    config_path: Path,
    *,
    matrix_id: str | None = None,
    root: Path = ROOT,
    results_root: Path = RESULTS_ROOT,
    child_executor: ChildExecutor | None = None,
    input_collector: Callable[[LoadedConfig], run_state.MatrixInputs] | None = None,
) -> run_state.MatrixManifest:
    config = load_config(
        config_path, validate_calibrated_assets=matrix_id is None
    )
    root = Path(root)
    results_root = Path(results_root)
    collect = input_collector or (lambda loaded: collect_inputs(loaded, root=root))
    newly_created = matrix_id is None
    if newly_created:
        current_inputs = collect(config)
        matrix_dir, manifest = create_matrix(
            config,
            current_inputs,
            root=root,
            results_root=results_root,
        )
    else:
        if _SAFE_COMPONENT.fullmatch(matrix_id) is None:
            raise MatrixError("matrix identifier is invalid")
        _ensure_results_root(results_root)
        matrix_dir = _safe_child(results_root, results_root / matrix_id)
        manifest = load_manifest(matrix_dir)
        if manifest.matrix_id != matrix_dir.name:
            raise MatrixError("matrix manifest identity does not match its directory")
        current_inputs = collect(config)
    with MatrixLock(matrix_dir):
        manifest = load_manifest(matrix_dir)
        if manifest.matrix_id != matrix_dir.name:
            raise MatrixError("matrix manifest identity does not match its directory")
        _verify_matrix_layout(matrix_dir, manifest)
        _verify_resume(
            matrix_dir,
            config,
            current_inputs,
            manifest,
            results_root=results_root,
            root=root,
            revalidate_preflight=not newly_created,
        )
        if manifest.state is run_state.MatrixState.COMPLETED:
            return manifest
        remaining_work = manifest.work_items[len(manifest.completions) :]
        execute_child = child_executor
        if execute_child is None:
            scenario_turn_counts = {
                scenario.scenario_id: scenario.turn_count
                for scenario in manifest.inputs.scenarios
            }
            try:
                for item in remaining_work:
                    _child_timeout_budget(
                        config.timeout_seconds,
                        scenario_turn_counts[item.scenario_id],
                    )
            except KeyError:
                raise MatrixError("matrix child timeout budget is invalid") from None
            routes = tuple(
                dict.fromkeys(
                    (
                        item.route.model,
                        item.route.reasoning_effort,
                    )
                    for item in remaining_work
                )
            )
            if any(model is None or effort is None for model, effort in routes):
                raise MatrixError("matrix route preflight input is invalid")
            pinned_routes = tuple(
                (model, effort)
                for model, effort in routes
                if model is not None and effort is not None
            )
            try:
                advertised = codex_driver.advertised_model_routes(
                    pinned_routes,
                    timeout_seconds=_ROUTE_PREFLIGHT_TIMEOUT_SECONDS,
                )
            except (codex_driver.CodexProtocolError, ValueError):
                raise MatrixError(
                    "matrix route preflight failed structurally"
                ) from None
            if len(advertised) != len(pinned_routes) or any(
                not isinstance(value, bool) for value in advertised
            ):
                raise MatrixError("matrix route preflight failed structurally")
            route_availability = dict(zip(pinned_routes, advertised, strict=True))

            def execute_child(
                item: run_state.MatrixWorkItem, timeout: float
            ) -> ChildResult:
                route = (item.route.model, item.route.reasoning_effort)
                if route_availability.get(route) is not True:
                    return ChildResult.unavailable("route_not_available")
                return _run_child_subprocess(
                    item,
                    timeout,
                    turn_count=scenario_turn_counts[item.scenario_id],
                    root=root,
                    results_root=results_root,
                )

        for work_item in remaining_work:
            started_at = datetime.now(timezone.utc)
            attempt_id, attempt_dir, started_sha256 = _next_attempt_dir(
                matrix_dir,
                work_item.work_item_id,
                manifest,
                started_at=started_at,
            )
            result_path = attempt_dir / "result.json"
            result_published = False
            try:
                child = execute_child(work_item, config.timeout_seconds)
                completed_at = datetime.now(timezone.utc)
                if child.unavailable_reason is not None:
                    if child.run_dir is not None or child.exit_code is not None:
                        raise MatrixError("unavailable child result is malformed")
                    completion = run_state.MatrixCompletion(
                        work_item_id=work_item.work_item_id,
                        attempt_id=attempt_id,
                        started_sha256=started_sha256,
                        outcome="unavailable",
                        unavailable_reason=child.unavailable_reason,
                        child_run_id=None,
                        child_exit_code=None,
                        artifact_hashes=(),
                        completed_at=(completed_at.isoformat().replace("+00:00", "Z")),
                    )
                else:
                    validated_child = validate_child_result(
                        child,
                        work_item,
                        manifest,
                        results_root=results_root,
                    )
                    completion = run_state.MatrixCompletion(
                        work_item_id=work_item.work_item_id,
                        attempt_id=attempt_id,
                        started_sha256=started_sha256,
                        outcome="observed",
                        unavailable_reason=None,
                        child_run_id=validated_child.child_run_id,
                        child_exit_code=child.exit_code,
                        artifact_hashes=validated_child.artifact_hashes,
                        completed_at=(completed_at.isoformat().replace("+00:00", "Z")),
                    )
                _verify_observation_freshness((*manifest.completions, completion))
                run_state.atomic_publish_private_json(
                    result_path,
                    {
                        "schema": "steam-agent-eval-matrix-attempt-result/0.1",
                        "completion": completion.to_dict(),
                    },
                )
                result_published = True
                manifest = manifest.checkpoint(completion, at=completed_at)
                manifest.persist(matrix_dir / "manifest.json")
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                if not result_published:
                    try:
                        _private_attempt_file(result_path)
                    except MatrixError:
                        run_state.atomic_publish_private_json(
                            attempt_dir / "failure.json",
                            {
                                "schema": "steam-agent-eval-matrix-attempt-failure/0.1",
                                "reason": "child_cohort_invalid",
                                "error_type": type(error).__name__,
                            },
                        )
                if isinstance(error, MatrixError):
                    raise
                raise MatrixError("matrix child cohort failed structurally") from None
        return manifest


def run_cli(argv: Sequence[str] | None = None, *, resume: bool = False) -> int:
    parser = argparse.ArgumentParser(
        prog="evals.runner resume" if resume else "evals.runner matrix"
    )
    if resume:
        parser.add_argument("matrix_id")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = execute_matrix(
            args.config,
            matrix_id=args.matrix_id if resume else None,
        )
    except MatrixError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(manifest.matrix_id)
    return 0
