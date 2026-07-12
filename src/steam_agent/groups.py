"""Pure deterministic M6 group eligibility and ranking recipes.

This module performs no I/O.  Callers resolve aliases, freshness, and provider
evidence before constructing these deliberately small, three-valued inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal, Mapping


SCHEMA = "group-eligibility/0.1"
RANKING_RECIPE = "group-fit/0.1"
MAX_APPID = (1 << 32) - 1
MAX_MEMBERS = 32
MAX_SOURCES = 64
MAX_SEEDS_PER_MEMBER = 64
MAX_FEATURE_IDS = 512
MAX_CANDIDATES = 100_000
MAX_KEY = 64

ProfileKind = Literal["account", "synthetic"]
OwnershipState = Literal["owned", "not_owned", "unknown"]
FamilyState = Literal["available", "unavailable", "unknown"]
GateState = Literal["pass", "fail", "unknown"]
GuaranteeClass = Literal["guaranteed", "conditional", "insufficient"]
Objective = Literal["no-purchase", "min-copies", "preference-fit"]
ExactMode = Literal[
    "online_coop",
    "online_pvp",
    "lan_coop",
    "lan_pvp",
    "shared_split_coop",
    "shared_split_pvp",
    "remote_play_together",
]

_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_POLICY = re.compile(r"user:[a-z0-9](?:[a-z0-9._-]{0,57}[a-z0-9])?\Z")
_HOST_MODES = frozenset(
    {"shared_split_coop", "shared_split_pvp", "remote_play_together"}
)
_PER_PARTICIPANT_MODES = frozenset(
    {"online_coop", "online_pvp", "lan_coop", "lan_pvp"}
)
_EXACT_MODES = _HOST_MODES | _PER_PARTICIPANT_MODES


def _bounded_key(value: object) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise ValueError("profile key is invalid")
    return value.casefold()


def _appid(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("appid is invalid")
    if not 1 <= value <= MAX_APPID:
        raise ValueError("appid is invalid")
    return value


@dataclass(frozen=True, order=True, slots=True)
class MemberRef:
    kind: ProfileKind
    key: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.kind not in {"account", "synthetic"}:
            raise ValueError("member kind is invalid")
        object.__setattr__(self, "key", _bounded_key(self.key))


@dataclass(frozen=True, order=True, slots=True)
class CopySourceRef:
    kind: ProfileKind
    key: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.kind not in {"account", "synthetic"}:
            raise ValueError("copy-source kind is invalid")
        object.__setattr__(self, "key", _bounded_key(self.key))

    @classmethod
    def for_member(cls, member: MemberRef) -> CopySourceRef:
        return cls(member.kind, member.key)


@dataclass(frozen=True, slots=True)
class OwnershipFact:
    source: CopySourceRef
    state: OwnershipState

    def __post_init__(self) -> None:
        if self.state not in {"owned", "not_owned", "unknown"}:
            raise ValueError("ownership state is invalid")


@dataclass(frozen=True, slots=True)
class FamilyEdge:
    member: MemberRef
    source: CopySourceRef
    state: FamilyState

    def __post_init__(self) -> None:
        if self.state not in {"available", "unavailable", "unknown"}:
            raise ValueError("family state is invalid")
        if CopySourceRef.for_member(self.member) == self.source:
            raise ValueError("self access comes from ownership, not a family edge")


@dataclass(frozen=True, slots=True)
class OwnershipSummary:
    per_member: tuple[tuple[MemberRef, OwnershipState], ...]
    union: OwnershipState
    intersection: OwnershipState


@dataclass(frozen=True, slots=True)
class MissingCopies:
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if not 0 <= self.minimum <= self.maximum:
            raise ValueError("missing-copy range is invalid")


@dataclass(frozen=True, slots=True)
class CopyAssessment:
    required_copies: int
    known_matching: int
    possible_matching: int
    missing: MissingCopies
    guarantee: GuaranteeClass
    known_assignments: tuple[tuple[MemberRef, CopySourceRef], ...]
    possible_assignments: tuple[tuple[MemberRef, CopySourceRef], ...]

    def __post_init__(self) -> None:
        for name in ("required_copies", "known_matching", "possible_matching"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} is invalid")
        if not self.known_matching <= self.possible_matching <= self.required_copies:
            raise ValueError("copy matching counts are inconsistent")
        if self.missing != MissingCopies(
            self.required_copies - self.possible_matching,
            self.required_copies - self.known_matching,
        ):
            raise ValueError("missing-copy range does not match the matchings")
        expected = (
            "guaranteed"
            if self.missing.maximum == 0
            else "conditional"
            if self.missing.minimum == 0
            else "insufficient"
        )
        if self.guarantee != expected:
            raise ValueError("copy guarantee class is inconsistent")
        if len(self.known_assignments) != self.known_matching:
            raise ValueError("known assignments do not match their count")
        if len(self.possible_assignments) != self.possible_matching:
            raise ValueError("possible assignments do not match their count")


@dataclass(frozen=True, slots=True)
class PlayerLimits:
    minimum: int | None = None
    maximum: int | None = None
    conflicting: bool = False

    def __post_init__(self) -> None:
        for name in ("minimum", "maximum"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"player {name} is invalid")
        if (
            not self.conflicting
            and self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("player limits are inverted")


@dataclass(frozen=True, slots=True)
class PolicyFact:
    member: MemberRef
    policy: str
    state: GateState

    def __post_init__(self) -> None:
        if not isinstance(self.policy, str) or _POLICY.fullmatch(self.policy) is None:
            raise ValueError("policy must be a bounded user: slug")
        if self.state not in {"pass", "fail", "unknown"}:
            raise ValueError("policy state is invalid")


@dataclass(frozen=True, slots=True)
class EligibilityGate:
    name: str
    state: GateState

    def __post_init__(self) -> None:
        if self.name not in {"mode", "player_count", "policy"}:
            raise ValueError("eligibility gate name is invalid")
        if self.state not in {"pass", "fail", "unknown"}:
            raise ValueError("eligibility gate state is invalid")


@dataclass(frozen=True, slots=True)
class EligibilityAssessment:
    state: GateState
    gates: tuple[EligibilityGate, ...]

    def __post_init__(self) -> None:
        if self.state not in {"pass", "fail", "unknown"}:
            raise ValueError("eligibility state is invalid")
        if not isinstance(self.gates, tuple) or not self.gates:
            raise ValueError("eligibility gates must be a non-empty tuple")
        states = tuple(gate.state for gate in self.gates)
        expected: GateState = (
            "fail"
            if "fail" in states
            else "unknown"
            if "unknown" in states
            else "pass"
        )
        if self.state != expected:
            raise ValueError("eligibility state is inconsistent with its gates")


@dataclass(frozen=True, slots=True)
class FeatureSet:
    appid: int
    genre_ids: frozenset[int] = frozenset()
    category_ids: frozenset[int] = frozenset()
    known: bool = True

    def __post_init__(self) -> None:
        _appid(self.appid)
        for name in ("genre_ids", "category_ids"):
            values = getattr(self, name)
            if not isinstance(values, frozenset) or len(values) > MAX_FEATURE_IDS:
                raise ValueError(f"{name} must be a bounded frozenset")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in values
            ):
                raise ValueError(f"{name} contains an invalid ID")
        if not isinstance(self.known, bool):
            raise ValueError("feature known state is invalid")
        if not self.known and (self.genre_ids or self.category_ids):
            raise ValueError("unknown features cannot carry IDs")


@dataclass(frozen=True, slots=True)
class MemberPreference:
    member: MemberRef
    liked: tuple[FeatureSet, ...] = ()
    disliked: tuple[FeatureSet, ...] = ()

    def __post_init__(self) -> None:
        if not self.liked and not self.disliked:
            raise ValueError("each member needs at least one preference seed")
        if len(self.liked) + len(self.disliked) > MAX_SEEDS_PER_MEMBER:
            raise ValueError("preference seed limit exceeded")
        appids = [seed.appid for seed in (*self.liked, *self.disliked)]
        if len(appids) != len(set(appids)):
            raise ValueError("preference seeds must be unique per member")


@dataclass(frozen=True, slots=True)
class PreferenceScore:
    per_member: tuple[tuple[MemberRef, int | None], ...]
    least_member: int | None
    total: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.per_member, tuple) or not self.per_member:
            raise ValueError("member scores must be a non-empty tuple")
        members = tuple(member for member, _ in self.per_member)
        if len(members) != len(set(members)):
            raise ValueError("member scores must be unique")
        values = tuple(value for _, value in self.per_member)
        if any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not -10_000 <= value <= 10_000
            )
            for value in values
        ):
            raise ValueError("member preference score is invalid")
        if any(value is None for value in values):
            if self.least_member is not None or self.total is not None:
                raise ValueError("unknown member score requires unknown aggregates")
        else:
            numeric = tuple(value for value in values if value is not None)
            if self.least_member != min(numeric) or self.total != sum(numeric):
                raise ValueError("preference aggregates are inconsistent")


@dataclass(frozen=True, slots=True)
class GroupCandidate:
    appid: int
    copies: CopyAssessment
    eligibility: EligibilityAssessment
    preference: PreferenceScore | None = None

    def __post_init__(self) -> None:
        _appid(self.appid)


def summarize_ownership(
    members: tuple[MemberRef, ...], facts: tuple[OwnershipFact, ...]
) -> OwnershipSummary:
    """Return per-member, union, and intersection three-valued ownership."""

    selected = _members(members)
    by_source = _ownership_map(facts)
    states = tuple(
        (member, by_source.get(CopySourceRef.for_member(member), "unknown"))
        for member in selected
    )
    values = tuple(state for _, state in states)
    union: OwnershipState
    intersection: OwnershipState
    if "owned" in values:
        union = "owned"
    elif all(state == "not_owned" for state in values):
        union = "not_owned"
    else:
        union = "unknown"
    if all(state == "owned" for state in values):
        intersection = "owned"
    elif "not_owned" in values:
        intersection = "not_owned"
    else:
        intersection = "unknown"
    return OwnershipSummary(states, union, intersection)


def assess_copies(
    *,
    members: tuple[MemberRef, ...],
    extra_sources: tuple[CopySourceRef, ...] = (),
    ownership: tuple[OwnershipFact, ...],
    family: tuple[FamilyEdge, ...] = (),
    mode: ExactMode,
    host: MemberRef | None = None,
) -> CopyAssessment:
    """Assess guaranteed and possible capacity using two maximum matchings."""

    selected_members = _members(members)
    topology_members, required = _topology(selected_members, mode, host)
    sources = _sources(selected_members, extra_sources)
    ownership_by_source = _ownership_map(ownership)
    if any(fact.source not in sources for fact in ownership):
        raise ValueError("ownership fact references an unselected copy source")
    family_by_edge: dict[tuple[MemberRef, CopySourceRef], FamilyState] = {}
    for edge in family:
        key = (edge.member, edge.source)
        if edge.member not in selected_members or edge.source not in sources:
            raise ValueError("family edge references an unselected subject")
        if key in family_by_edge:
            raise ValueError("family edges must be unique")
        family_by_edge[key] = edge.state

    known_edges: dict[MemberRef, tuple[CopySourceRef, ...]] = {}
    possible_edges: dict[MemberRef, tuple[CopySourceRef, ...]] = {}
    for member in topology_members:
        known: list[CopySourceRef] = []
        possible: list[CopySourceRef] = []
        own_source = CopySourceRef.for_member(member)
        for source in sources:
            source_state = ownership_by_source.get(source, "unknown")
            if source == own_source:
                edge_state = _self_edge(source_state)
            else:
                edge_state = _family_edge(
                    source_state, family_by_edge.get((member, source))
                )
            if edge_state == "known":
                known.append(source)
                possible.append(source)
            elif edge_state == "possible":
                possible.append(source)
        known_edges[member] = tuple(known)
        possible_edges[member] = tuple(possible)

    known_assignments = _maximum_matching(topology_members, known_edges)
    possible_assignments = _maximum_matching(topology_members, possible_edges)
    known_count = len(known_assignments)
    possible_count = len(possible_assignments)
    missing = MissingCopies(
        max(0, required - possible_count), max(0, required - known_count)
    )
    guarantee: GuaranteeClass
    if missing.maximum == 0:
        guarantee = "guaranteed"
    elif missing.minimum == 0:
        guarantee = "conditional"
    else:
        guarantee = "insufficient"
    return CopyAssessment(
        required,
        known_count,
        possible_count,
        missing,
        guarantee,
        known_assignments,
        possible_assignments,
    )


def assess_eligibility(
    *,
    members: tuple[MemberRef, ...],
    mode_state: GateState,
    player_limits: PlayerLimits = PlayerLimits(),
    required_policy: str | None = None,
    policy_facts: tuple[PolicyFact, ...] = (),
) -> EligibilityAssessment:
    """Combine independent mode, count, and all-member policy gates."""

    selected = _members(members)
    if mode_state not in {"pass", "fail", "unknown"}:
        raise ValueError("mode state is invalid")
    mode_gate = EligibilityGate("mode", mode_state)
    count_gate = EligibilityGate(
        "player_count",
        _count_state(len(selected), mode_state=mode_state, limits=player_limits),
    )
    policy_gate = EligibilityGate(
        "policy",
        _policy_state(selected, required_policy=required_policy, facts=policy_facts),
    )
    gates = (mode_gate, count_gate, policy_gate)
    states = tuple(gate.state for gate in gates)
    state: GateState = (
        "fail" if "fail" in states else "unknown" if "unknown" in states else "pass"
    )
    return EligibilityAssessment(state, gates)


def weighted_jaccard(left: FeatureSet, right: FeatureSet) -> int | None:
    """Return integer basis points over genre(weight 2)/category(weight 1)."""

    if not left.known or not right.known:
        return None
    genre_intersection = len(left.genre_ids & right.genre_ids)
    category_intersection = len(left.category_ids & right.category_ids)
    intersection_weight = 2 * genre_intersection + category_intersection
    union_weight = 2 * len(left.genre_ids | right.genre_ids) + len(
        left.category_ids | right.category_ids
    )
    if union_weight == 0:
        return None
    return intersection_weight * 10_000 // union_weight


def score_preferences(
    candidate: FeatureSet,
    members: tuple[MemberRef, ...],
    preferences: tuple[MemberPreference, ...],
) -> PreferenceScore:
    """Score member-qualified positive and negative similarity separately."""

    selected = _members(members)
    by_member: dict[MemberRef, MemberPreference] = {}
    for preference in preferences:
        if preference.member not in selected:
            raise ValueError("preference references an unselected member")
        if preference.member in by_member:
            raise ValueError("preferences must be unique per member")
        by_member[preference.member] = preference
    if set(by_member) != set(selected):
        raise ValueError("every member needs member-qualified preference seeds")

    scores: list[tuple[MemberRef, int | None]] = []
    for member in selected:
        preference = by_member[member]
        positive = tuple(weighted_jaccard(candidate, seed) for seed in preference.liked)
        negative = tuple(
            weighted_jaccard(candidate, seed) for seed in preference.disliked
        )
        if any(value is None for value in (*positive, *negative)):
            score = None
        else:
            score = max(positive, default=0) - max(negative, default=0)  # type: ignore[type-var]
        scores.append((member, score))
    numeric = tuple(score for _, score in scores if score is not None)
    if len(numeric) != len(scores):
        least = total = None
    else:
        least = min(numeric)
        total = sum(numeric)
    return PreferenceScore(tuple(scores), least, total)


def rank_candidates(
    candidates: tuple[GroupCandidate, ...], *, objective: Objective
) -> tuple[GroupCandidate, ...]:
    """Apply one immutable group-fit objective and AppID tie-breaker."""

    if not isinstance(candidates, tuple) or len(candidates) > MAX_CANDIDATES:
        raise ValueError("candidates must be a bounded tuple")
    if objective not in {"no-purchase", "min-copies", "preference-fit"}:
        raise ValueError("group objective is invalid")
    appids = tuple(candidate.appid for candidate in candidates)
    if len(appids) != len(set(appids)):
        raise ValueError("candidate AppIDs must be unique")
    eligible = tuple(
        candidate for candidate in candidates if candidate.eligibility.state != "fail"
    )
    if objective == "no-purchase":
        eligible = tuple(
            candidate
            for candidate in eligible
            if candidate.copies.missing == MissingCopies(0, 0)
        )
        return tuple(sorted(eligible, key=lambda item: (_eligibility(item), item.appid)))
    if objective == "min-copies":
        return tuple(
            sorted(
                eligible,
                key=lambda item: (
                    item.copies.missing.maximum,
                    item.copies.missing.minimum,
                    _eligibility(item),
                    item.appid,
                ),
            )
        )
    if any(candidate.preference is None for candidate in eligible):
        raise ValueError("preference-fit candidates require preference scores")
    return tuple(sorted(eligible, key=_preference_sort_key))


def _members(values: tuple[MemberRef, ...]) -> tuple[MemberRef, ...]:
    if not isinstance(values, tuple) or not 2 <= len(values) <= MAX_MEMBERS:
        raise ValueError("a group requires between 2 and 32 members")
    if len(values) != len(set(values)):
        raise ValueError("group members must be unique")
    return values


def _sources(
    members: tuple[MemberRef, ...], extra: tuple[CopySourceRef, ...]
) -> tuple[CopySourceRef, ...]:
    if not isinstance(extra, tuple):
        raise ValueError("extra sources must be a tuple")
    values = tuple(CopySourceRef.for_member(member) for member in members) + extra
    if len(values) > MAX_SOURCES:
        raise ValueError("copy-source limit exceeded")
    if len(values) != len(set(values)):
        raise ValueError("copy sources must be distinct from members and each other")
    return values


def _ownership_map(
    facts: tuple[OwnershipFact, ...],
) -> dict[CopySourceRef, OwnershipState]:
    result: dict[CopySourceRef, OwnershipState] = {}
    for fact in facts:
        if fact.source in result:
            raise ValueError("ownership facts must be unique per copy source")
        result[fact.source] = fact.state
    return result


def _topology(
    members: tuple[MemberRef, ...], mode: ExactMode, host: MemberRef | None
) -> tuple[tuple[MemberRef, ...], int]:
    if mode not in _EXACT_MODES:
        raise ValueError("exact multiplayer mode is invalid")
    if mode in _HOST_MODES:
        if host is None or host not in members:
            raise ValueError("host topology requires an explicitly selected host")
        return (host,), 1
    if host is not None:
        raise ValueError("online/LAN topology does not accept a host")
    return members, len(members)


def _self_edge(state: OwnershipState) -> Literal["known", "possible", "absent"]:
    return {"owned": "known", "unknown": "possible", "not_owned": "absent"}[state]


def _family_edge(
    ownership: OwnershipState, family: FamilyState | None
) -> Literal["known", "possible", "absent"]:
    if family is None or family == "unavailable" or ownership == "not_owned":
        return "absent"
    if family == "available" and ownership == "owned":
        return "known"
    return "possible"


def _maximum_matching(
    members: tuple[MemberRef, ...],
    edges: Mapping[MemberRef, tuple[CopySourceRef, ...]],
) -> tuple[tuple[MemberRef, CopySourceRef], ...]:
    """Return a deterministic maximum-cardinality bipartite matching."""

    source_to_member: dict[CopySourceRef, MemberRef] = {}

    def augment(member: MemberRef, seen: set[CopySourceRef]) -> bool:
        for source in sorted(edges.get(member, ())):
            if source in seen:
                continue
            seen.add(source)
            incumbent = source_to_member.get(source)
            if incumbent is None or augment(incumbent, seen):
                source_to_member[source] = member
                return True
        return False

    for member in sorted(members):
        augment(member, set())
    return tuple(
        sorted((member, source) for source, member in source_to_member.items())
    )


def _count_state(
    count: int, *, mode_state: GateState, limits: PlayerLimits
) -> GateState:
    if limits.conflicting:
        return "unknown"
    if limits.minimum is not None and count < limits.minimum:
        return "fail"
    if limits.maximum is not None and count > limits.maximum:
        return "fail"
    if mode_state != "pass":
        return "unknown"
    # An exact multiplayer declaration establishes only the semantic two-player
    # floor.  Larger groups need an explicit maximum that covers their size.
    if count == 2 or (limits.maximum is not None and count <= limits.maximum):
        return "pass"
    return "unknown"


def _policy_state(
    members: tuple[MemberRef, ...],
    *,
    required_policy: str | None,
    facts: tuple[PolicyFact, ...],
) -> GateState:
    if required_policy is None:
        if facts:
            raise ValueError("policy facts require a requested policy")
        return "pass"
    if _POLICY.fullmatch(required_policy) is None:
        raise ValueError("policy must be a bounded user: slug")
    by_member: dict[MemberRef, GateState] = {}
    for fact in facts:
        if fact.policy != required_policy or fact.member not in members:
            raise ValueError("policy fact does not match the all-member request")
        if fact.member in by_member:
            raise ValueError("policy facts must be unique per member")
        by_member[fact.member] = fact.state
    states = tuple(by_member.get(member, "unknown") for member in members)
    if "fail" in states:
        return "fail"
    if "unknown" in states:
        return "unknown"
    return "pass"


def _eligibility(candidate: GroupCandidate) -> int:
    return 0 if candidate.eligibility.state == "pass" else 1


def _preference_sort_key(candidate: GroupCandidate) -> tuple[int, int, int, int, int, int]:
    preference = candidate.preference
    assert preference is not None
    guarantee_order = {"guaranteed": 0, "conditional": 1, "insufficient": 2}
    unknown = preference.least_member is None or preference.total is None
    return (
        guarantee_order[candidate.copies.guarantee],
        _eligibility(candidate),
        1 if unknown else 0,
        0 if unknown else -preference.least_member,
        0 if unknown else -preference.total,
        candidate.appid,
    )


__all__ = [
    "CopyAssessment",
    "CopySourceRef",
    "EligibilityAssessment",
    "EligibilityGate",
    "FamilyEdge",
    "FeatureSet",
    "GroupCandidate",
    "MemberPreference",
    "MemberRef",
    "MissingCopies",
    "OwnershipFact",
    "OwnershipSummary",
    "PlayerLimits",
    "PolicyFact",
    "PreferenceScore",
    "RANKING_RECIPE",
    "SCHEMA",
    "assess_copies",
    "assess_eligibility",
    "rank_candidates",
    "score_preferences",
    "summarize_ownership",
    "weighted_jaccard",
]
