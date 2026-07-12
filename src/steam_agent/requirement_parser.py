"""Conservative parsing and comparison of declared minimum requirements.

The storefront requirements field is prose, not a stable machine-readable
contract.  This module therefore recognizes only a tiny, English, line-based
grammar.  It never orders CPU/GPU model names, infers performance, consumes a
``recommended`` section, or treats a missing field as proof that no constraint
exists.

Ambiguous SI-looking ``MB`` and ``GB`` quantities are represented as intervals:
the decimal interpretation is the lower bound and the IEC interpretation is
the upper bound.  A machine between those bounds remains unknown.  Explicit
``MiB`` and ``GiB`` quantities have one exact byte value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Literal


SCHEMA = "minimum-requirements/0.1"
MAX_TEXT_LENGTH = 8_192
MAX_LINES = 64
MAX_LINE_LENGTH = 512
MAX_BYTES = (1 << 63) - 1

GateState = Literal["pass", "fail", "unknown"]
FactState = Literal["known", "missing", "unknown"]
Section = Literal["minimum", "recommended"]
Architecture = Literal["x86", "x86_64", "arm64"]

_LABEL = re.compile(r"(?P<label>[A-Za-z][A-Za-z ]{0,31}):\s*(?P<value>.*)\Z")
_CAPACITY = re.compile(
    r"(?P<number>(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)\s*"
    r"(?P<unit>MB|GB|MiB|GiB)"
    r"(?P<suffix>\s+RAM|\s+available space)?\Z"
)
_ARCHITECTURES = frozenset({"x86", "x86_64", "arm64"})
_IGNORED_LABELS = frozenset(
    {
        "OS",
        "DirectX",
        "Network",
        "Sound Card",
        "Additional Notes",
    }
)
_COMPONENT_LABELS = {
    "Memory": "memory",
    "Storage": "storage",
    "Architecture": "architecture",
    "Processor": "cpu",
    "CPU": "cpu",
    "Graphics": "gpu",
    "GPU": "gpu",
}


@dataclass(frozen=True, slots=True)
class DeclaredRequirementsText:
    """Bounded provider text with its section provenance kept explicit."""

    section: Section
    text: str | None


@dataclass(frozen=True, slots=True)
class ByteInterval:
    """Inclusive lower/upper interpretations of one declared capacity."""

    lower_bytes: int
    upper_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.lower_bytes, bool)
            or isinstance(self.upper_bytes, bool)
            or not isinstance(self.lower_bytes, int)
            or not isinstance(self.upper_bytes, int)
            or not 0 < self.lower_bytes <= self.upper_bytes <= MAX_BYTES
        ):
            raise ValueError("byte interval must be positive, ordered, and bounded")


@dataclass(frozen=True, slots=True)
class RequirementFact:
    """One parsed requirement without retaining or echoing source prose."""

    state: FactState
    interval: ByteInterval | None = None
    architecture: Architecture | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"known", "missing", "unknown"}:
            raise ValueError("requirement fact state is invalid")
        values = int(self.interval is not None) + int(self.architecture is not None)
        if self.state == "known" and (values != 1 or self.reason is not None):
            raise ValueError("known facts require exactly one value and no reason")
        if self.state != "known" and (values or not self.reason):
            raise ValueError("non-known facts require a reason and no value")


@dataclass(frozen=True, slots=True)
class ParsedMinimumRequirements:
    schema: Literal["minimum-requirements/0.1"]
    input_state: Literal["accepted", "rejected"]
    input_reason: str | None
    memory: RequirementFact
    storage: RequirementFact
    architecture: RequirementFact
    cpu: RequirementFact
    gpu: RequirementFact


@dataclass(frozen=True, slots=True)
class SystemCapacity:
    """Already-normalized machine capacity; collectors live elsewhere."""

    memory_bytes: int | None
    storage_available_bytes: int | None
    architecture: Architecture | None
    supported_architectures: tuple[Architecture, ...] = ()

    def __post_init__(self) -> None:
        for name in ("memory_bytes", "storage_available_bytes"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_BYTES
            ):
                raise ValueError(
                    f"{name} must be a bounded non-negative integer or None"
                )
        if self.architecture is not None and self.architecture not in _ARCHITECTURES:
            raise ValueError("system architecture is invalid")
        if not isinstance(self.supported_architectures, tuple):
            raise ValueError("supported_architectures must be a tuple")
        if any(item not in _ARCHITECTURES for item in self.supported_architectures):
            raise ValueError("supported architecture is invalid")
        if len(self.supported_architectures) != len(set(self.supported_architectures)):
            raise ValueError("supported architectures must be unique")
        object.__setattr__(
            self, "supported_architectures", tuple(sorted(self.supported_architectures))
        )


@dataclass(frozen=True, slots=True)
class ComponentComparison:
    state: GateState
    reason: str


@dataclass(frozen=True, slots=True)
class MinimumRequirementsComparison:
    schema: Literal["minimum-requirements/0.1"]
    overall: GateState
    memory: ComponentComparison
    storage: ComponentComparison
    architecture: ComponentComparison
    cpu: ComponentComparison
    gpu: ComponentComparison


def parse_declared_minimum(
    source: DeclaredRequirementsText,
) -> ParsedMinimumRequirements:
    """Parse a sanitized ``minimum`` section using a closed, anchored grammar.

    Rejected input becomes an all-unknown DTO instead of raising.  Programming
    errors (passing the wrong DTO type) still raise ``ValueError``.
    """

    if not isinstance(source, DeclaredRequirementsText):
        raise ValueError("source must be DeclaredRequirementsText")
    if source.section != "minimum":
        return _rejected("recommended_section_not_parsed")
    invalid_reason = _validate_input(source.text)
    if invalid_reason is not None:
        return _rejected(invalid_reason)
    assert source.text is not None

    found: dict[str, list[RequirementFact]] = {
        "memory": [],
        "storage": [],
        "architecture": [],
        "cpu": [],
        "gpu": [],
    }
    for raw_line in source.text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # Some normalized storefront fixtures retain these exact section
        # wrappers.  A recommended tail is a hard stop, never parser input.
        if line == "Minimum:":
            continue
        if line == "Recommended:":
            break
        match = _LABEL.fullmatch(line)
        if match is None:
            if ":" in line:
                return _rejected("unknown_or_localized_label")
            return _rejected("unanchored_or_malformed_line")
        label, value = match.group("label"), match.group("value").strip()
        if label == "Minimum":
            # A wrapper sharing a line with prose does not establish which
            # component that prose constrains, so it contributes no fact.
            continue
        if label in _IGNORED_LABELS:
            continue
        component = _COMPONENT_LABELS.get(label)
        if component is None:
            return _rejected("unknown_or_localized_label")
        if component in {"memory", "storage"}:
            fact = _parse_capacity(value, component)
        elif component == "architecture":
            fact = (
                RequirementFact("known", architecture=value)  # type: ignore[arg-type]
                if value in _ARCHITECTURES
                else RequirementFact(
                    "unknown", reason="architecture_not_exact_isa_token"
                )
            )
        else:
            # Model names are retained neither as comparable values nor output.
            fact = RequirementFact(
                "unknown",
                reason=f"{component}_requirement_opaque"
                if value
                else f"{component}_requirement_malformed",
            )
        found[component].append(fact)

    facts = {name: _merge(name, values) for name, values in found.items()}
    return ParsedMinimumRequirements(
        SCHEMA,
        "accepted",
        None,
        facts["memory"],
        facts["storage"],
        facts["architecture"],
        facts["cpu"],
        facts["gpu"],
    )


def compare_declared_minimum(
    system: SystemCapacity,
    requirements: ParsedMinimumRequirements,
) -> MinimumRequirementsComparison:
    """Compare exact/interval capacities without making performance claims."""

    if not isinstance(system, SystemCapacity) or not isinstance(
        requirements, ParsedMinimumRequirements
    ):
        raise ValueError("system and requirements must be normalized DTOs")
    memory = _compare_capacity(system.memory_bytes, requirements.memory, "memory")
    storage = _compare_capacity(
        system.storage_available_bytes, requirements.storage, "storage"
    )
    architecture = _compare_architecture(system, requirements.architecture)
    cpu = _opaque_comparison(requirements.cpu, "cpu")
    gpu = _opaque_comparison(requirements.gpu, "gpu")
    states = (memory.state, storage.state, architecture.state, cpu.state, gpu.state)
    overall: GateState = (
        "fail" if "fail" in states else "unknown" if "unknown" in states else "pass"
    )
    return MinimumRequirementsComparison(
        SCHEMA, overall, memory, storage, architecture, cpu, gpu
    )


def _validate_input(text: object) -> str | None:
    if text is None or not isinstance(text, str) or not text.strip():
        return "minimum_text_missing"
    if len(text) > MAX_TEXT_LENGTH:
        return "minimum_text_too_large"
    if any((not character.isprintable()) and character != "\n" for character in text):
        return "minimum_text_contains_control_character"
    if "<" in text or ">" in text:
        return "minimum_text_not_sanitized"
    lines = text.split("\n")
    if len(lines) > MAX_LINES or any(len(line) > MAX_LINE_LENGTH for line in lines):
        return "minimum_text_shape_exceeds_bound"
    return None


def _parse_capacity(value: str, component: str) -> RequirementFact:
    match = _CAPACITY.fullmatch(value)
    if match is None:
        reason = (
            f"{component}_range_or_alternative_not_comparable"
            if re.search(
                r"(?:\bor\b|\bto\b|/|[0-9]\s*[-–—]\s*[0-9])", value, re.IGNORECASE
            )
            else f"{component}_quantity_malformed_or_unknown_unit"
        )
        return RequirementFact("unknown", reason=reason)
    suffix = match.group("suffix")
    if (component == "memory" and suffix == " available space") or (
        component == "storage" and suffix == " RAM"
    ):
        return RequirementFact(
            "unknown", reason=f"{component}_quantity_suffix_conflicts"
        )
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation:
        return RequirementFact(
            "unknown", reason=f"{component}_quantity_malformed_or_unknown_unit"
        )
    if not number.is_finite() or number <= 0:
        return RequirementFact("unknown", reason=f"{component}_quantity_out_of_range")
    unit = match.group("unit")
    decimal_scale = {
        "MB": 1_000_000,
        "GB": 1_000_000_000,
        "MiB": 1 << 20,
        "GiB": 1 << 30,
    }[unit]
    binary_scale = {"MB": 1 << 20, "GB": 1 << 30, "MiB": 1 << 20, "GiB": 1 << 30}[unit]
    lower_decimal = number * decimal_scale
    upper_decimal = number * binary_scale
    if (
        lower_decimal != lower_decimal.to_integral_value()
        or upper_decimal != upper_decimal.to_integral_value()
    ):
        return RequirementFact(
            "unknown", reason=f"{component}_quantity_not_exact_bytes"
        )
    lower, upper = int(lower_decimal), int(upper_decimal)
    if not 0 < lower <= upper <= MAX_BYTES:
        return RequirementFact("unknown", reason=f"{component}_quantity_out_of_range")
    return RequirementFact("known", interval=ByteInterval(lower, upper))


def _merge(component: str, values: list[RequirementFact]) -> RequirementFact:
    if not values:
        return RequirementFact("missing", reason=f"{component}_requirement_missing")
    first = values[0]
    if any(value != first for value in values[1:]):
        return RequirementFact(
            "unknown", reason=f"conflicting_duplicate_{component}_requirements"
        )
    return first


def _rejected(reason: str) -> ParsedMinimumRequirements:
    fact = RequirementFact("unknown", reason=reason)
    return ParsedMinimumRequirements(
        SCHEMA, "rejected", reason, fact, fact, fact, fact, fact
    )


def _compare_capacity(
    actual: int | None, fact: RequirementFact, component: str
) -> ComponentComparison:
    if fact.state != "known":
        return ComponentComparison(
            "unknown", fact.reason or f"{component}_requirement_unknown"
        )
    if actual is None:
        return ComponentComparison("unknown", f"system_{component}_unknown")
    assert fact.interval is not None
    if actual < fact.interval.lower_bytes:
        return ComponentComparison(
            "fail", f"system_{component}_below_required_lower_bound"
        )
    if actual >= fact.interval.upper_bytes:
        return ComponentComparison(
            "pass", f"system_{component}_meets_required_upper_bound"
        )
    return ComponentComparison(
        "unknown", f"system_{component}_within_ambiguous_unit_interval"
    )


def _compare_architecture(
    system: SystemCapacity, fact: RequirementFact
) -> ComponentComparison:
    if fact.state != "known":
        return ComponentComparison(
            "unknown", fact.reason or "architecture_requirement_unknown"
        )
    if system.architecture is None:
        return ComponentComparison("unknown", "system_architecture_unknown")
    assert fact.architecture is not None
    supported = {system.architecture, *system.supported_architectures}
    if fact.architecture in supported:
        return ComponentComparison("pass", "exact_isa_supported")
    # A different native ISA is not evidence that an emulation/translation
    # route is unavailable. Only an explicitly observed supported relation is
    # strong enough to pass; this DTO intentionally has no unsupported claim.
    return ComponentComparison("unknown", "exact_isa_support_relation_unknown")


def _opaque_comparison(fact: RequirementFact, component: str) -> ComponentComparison:
    # An omitted opaque model constraint contributes no comparable requirement.
    # This is not a performance claim: any *present* CPU/GPU prose remains
    # unknown because this parser never orders model names.
    if fact.state == "missing":
        return ComponentComparison("pass", f"{component}_requirement_not_declared")
    reason = fact.reason or f"{component}_comparison_not_supported"
    return ComponentComparison("unknown", reason)
