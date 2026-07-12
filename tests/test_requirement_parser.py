from __future__ import annotations

import random
import string

import pytest

from steam_agent.requirement_parser import (
    MAX_BYTES,
    ByteInterval,
    DeclaredRequirementsText,
    RequirementFact,
    SystemCapacity,
    compare_declared_minimum,
    parse_declared_minimum,
)


def parse(text: str, section: str = "minimum"):
    return parse_declared_minimum(DeclaredRequirementsText(section, text))  # type: ignore[arg-type]


def test_parses_only_anchored_english_minimum_labels() -> None:
    result = parse(
        "OS: Windows 11\nMemory: 8 GB RAM\nStorage: 20 GiB available space\n"
        "Architecture: x86_64\nProcessor: Example 4-core\nGraphics: Example GPU\nDirectX: 12"
    )
    assert result.input_state == "accepted"
    assert result.memory.interval == ByteInterval(8_000_000_000, 8 * (1 << 30))
    assert result.storage.interval == ByteInterval(20 * (1 << 30), 20 * (1 << 30))
    assert result.architecture.architecture == "x86_64"
    assert result.cpu.state == result.gpu.state == "unknown"
    assert result.cpu.reason == "cpu_requirement_opaque"


def test_recommended_section_is_structurally_refused() -> None:
    result = parse("Memory: 1 MiB", "recommended")
    assert result.input_state == "rejected"
    assert result.input_reason == "recommended_section_not_parsed"
    assert result.memory.state == "unknown"


def test_exact_minimum_wrapper_is_accepted_for_normalized_fixture_shape() -> None:
    result = parse("Minimum:\n\nMemory: 2 GB RAM")
    assert result.input_state == "accepted"
    assert result.memory.interval == ByteInterval(2_000_000_000, 2 * (1 << 30))


def test_legacy_embedded_recommended_tail_is_never_parsed() -> None:
    result = parse(
        "Minimum:\nMemory: 2 GB RAM\nRecommended:\nMemory: 64 GiB RAM\nStorage: 2 TiB"
    )
    assert result.input_state == "accepted"
    assert result.memory.interval == ByteInterval(2_000_000_000, 2 * (1 << 30))
    assert result.storage.state == "missing"


def test_inline_minimum_wrapper_prose_is_not_guessed() -> None:
    result = parse("Minimum: 512MB RAM")
    assert result.input_state == "accepted"
    assert result.memory.state == "missing"


def test_unicode_model_text_is_opaque_without_poisoning_capacity_facts() -> None:
    result = parse("Processor: Intel® Core™\nMemory: 8 GB RAM")
    assert result.input_state == "accepted"
    assert result.cpu.reason == "cpu_requirement_opaque"
    assert result.memory.interval == ByteInterval(8_000_000_000, 8 * (1 << 30))


@pytest.mark.parametrize(
    "value,lower,upper",
    [
        ("1 MB", 1_000_000, 1 << 20),
        ("1 GB", 1_000_000_000, 1 << 30),
        ("1 MiB", 1 << 20, 1 << 20),
        ("1 GiB", 1 << 30, 1 << 30),
        ("1.5 GB", 1_500_000_000, 1_610_612_736),
    ],
)
def test_capacity_units_have_safe_intervals(value: str, lower: int, upper: int) -> None:
    fact = parse(f"Memory: {value}").memory
    assert fact.interval == ByteInterval(lower, upper)


def test_ambiguous_interval_comparison_has_safe_boundaries() -> None:
    requirement = parse("Memory: 8 GB\nStorage: 1 GiB\nArchitecture: x86_64")
    below = compare_declared_minimum(
        SystemCapacity(7_999_999_999, 1 << 30, "x86_64"), requirement
    )
    between = compare_declared_minimum(
        SystemCapacity(8_000_000_000, 1 << 30, "x86_64"), requirement
    )
    upper = compare_declared_minimum(
        SystemCapacity(8 * (1 << 30), 1 << 30, "x86_64"), requirement
    )
    assert below.memory.state == "fail"
    assert between.memory.state == "unknown"
    assert upper.memory.state == "pass"
    # Opaque/missing CPU and GPU keep the aggregate unknown; no performance claim.
    assert upper.overall == "unknown"


def test_decisive_capacity_failure_precedes_unknown_prose() -> None:
    requirement = parse("Memory: 32 GiB\nProcessor: FastChip\nGraphics: FastCard")
    result = compare_declared_minimum(
        SystemCapacity(16 * (1 << 30), None, None), requirement
    )
    assert result.memory.state == "fail"
    assert result.cpu.state == result.gpu.state == "unknown"
    assert result.overall == "fail"


def test_exact_isa_requires_explicit_machine_support_without_inferring_failure() -> (
    None
):
    req = parse("Architecture: x86")
    unreviewed = compare_declared_minimum(SystemCapacity(None, None, "arm64"), req)
    supported = compare_declared_minimum(
        SystemCapacity(None, None, "arm64", ("x86",)), req
    )
    assert unreviewed.architecture.state == "unknown"
    assert unreviewed.architecture.reason == "exact_isa_support_relation_unknown"
    assert supported.architecture.state == "pass"


@pytest.mark.parametrize(
    "text,component,reason",
    [
        ("Memory: 8-16 GB", "memory", "memory_range_or_alternative_not_comparable"),
        ("Memory: 8 or 16 GB", "memory", "memory_range_or_alternative_not_comparable"),
        ("Storage: 20/30 GB", "storage", "storage_range_or_alternative_not_comparable"),
        ("Memory: 8 TB", "memory", "memory_quantity_malformed_or_unknown_unit"),
        ("Memory: -1 GB", "memory", "memory_quantity_malformed_or_unknown_unit"),
        ("Memory: 0 GB", "memory", "memory_quantity_out_of_range"),
        ("Memory: 1.0000000001 MiB", "memory", "memory_quantity_not_exact_bytes"),
        ("Architecture: AMD64", "architecture", "architecture_not_exact_isa_token"),
        ("Storage: 8 GB RAM", "storage", "storage_quantity_suffix_conflicts"),
    ],
)
def test_malformed_ambiguous_and_unknown_values_stay_unknown(
    text: str,
    component: str,
    reason: str,
) -> None:
    fact = getattr(parse(text), component)
    assert fact.state == "unknown"
    assert fact.reason == reason


def test_overflow_is_unknown_without_large_integer_work() -> None:
    fact = parse(f"Storage: {MAX_BYTES} GiB").storage
    assert fact.state == "unknown"
    assert fact.reason == "storage_quantity_out_of_range"


def test_conflicting_duplicates_are_unknown_but_identical_duplicates_are_stable() -> (
    None
):
    conflict = parse("Memory: 8 GB\nMemory: 16 GB")
    duplicate = parse("Memory: 8 GB\nMemory: 8 GB")
    assert conflict.memory.reason == "conflicting_duplicate_memory_requirements"
    assert duplicate.memory.interval == ByteInterval(8_000_000_000, 8 * (1 << 30))


@pytest.mark.parametrize(
    "text,reason",
    [
        ("Mémoire: 8 GB", "unknown_or_localized_label"),
        ("Speicher: 8 GB", "unknown_or_localized_label"),
        ("Memory = 8 GB", "unanchored_or_malformed_line"),
        ("<strong>Memory:</strong> 8 GB", "minimum_text_not_sanitized"),
        ("Memory:\t8 GB", "minimum_text_contains_control_character"),
        ("Memory: 8 GB\r", "minimum_text_contains_control_character"),
        ("", "minimum_text_missing"),
    ],
)
def test_unsanitized_localized_or_unanchored_input_is_rejected(
    text: str, reason: str
) -> None:
    result = parse(text)
    assert result.input_state == "rejected"
    assert result.input_reason == reason
    assert all(
        getattr(result, component).state == "unknown"
        for component in ("memory", "storage", "architecture", "cpu", "gpu")
    )


def test_missing_fields_are_unknown_not_authoritative_absence() -> None:
    parsed = parse("OS: Linux")
    compared = compare_declared_minimum(
        SystemCapacity(64 << 30, 1 << 40, "x86_64"), parsed
    )
    assert parsed.memory.state == parsed.cpu.state == "missing"
    assert compared.memory.state == compared.cpu.state == "unknown"
    assert compared.overall == "unknown"


def test_system_and_dto_validation_is_strict() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        SystemCapacity(-1, None, None)
    with pytest.raises(ValueError, match="unique"):
        SystemCapacity(None, None, "x86_64", ("x86", "x86"))
    with pytest.raises(ValueError, match="exactly one"):
        RequirementFact("known")
    with pytest.raises(ValueError, match="positive"):
        ByteInterval(0, 1)
    with pytest.raises(ValueError, match="normalized DTOs"):
        compare_declared_minimum(object(), parse("Memory: 1 GB"))  # type: ignore[arg-type]


def test_hostile_ascii_inputs_are_total_bounded_and_deterministic() -> None:
    rng = random.Random(20260712)
    alphabet = string.printable.replace("\r", "")
    cases = [
        "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 900)))
        for _ in range(1_000)
    ]
    for text in cases:
        first = parse_declared_minimum(DeclaredRequirementsText("minimum", text))
        second = parse_declared_minimum(DeclaredRequirementsText("minimum", text))
        assert first == second


def test_input_bounds_fail_closed() -> None:
    assert (
        parse("Memory: 1 GB\n" * 65).input_reason == "minimum_text_shape_exceeds_bound"
    )
    assert (
        parse("Memory: " + "1" * 600).input_reason == "minimum_text_shape_exceeds_bound"
    )
    assert parse("x" * 8_193).input_reason == "minimum_text_too_large"
