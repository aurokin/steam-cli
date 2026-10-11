"""Static command capabilities; listing them never probes a host or provider.

This catalog describes implemented interfaces, not current credentials, evidence
freshness, session readiness, or live execution support on a particular machine.
"""

from __future__ import annotations

from typing import Literal, TypedDict


SupportLevel = Literal[
    "local_observation",
    "official",
    "official_undocumented_provisional",
    "provisional",
    "contractual_third_party",
    "derived",
    "user_asserted",
    "policy_gated",
]
Surface = Literal["planner", "broker"]


class ProviderSupport(TypedDict):
    provider: str
    support_level: SupportLevel


class CapabilityEntry(TypedDict):
    id: str
    surface: Surface
    status: Literal["implemented"]
    commands: list[str]
    cache_only: bool
    network_required: bool
    explicit_local_state_mutation: bool
    mutates_steam: bool
    providers: list[ProviderSupport]
    requirements: list[str]
    limitations: list[str]


class CapabilityCatalog(TypedDict):
    catalog_schema: Literal["capability-catalog/0.1"]
    runtime_checked: Literal[False]
    storage_maintenance_possible: Literal[True]
    capabilities: list[CapabilityEntry]


def _entry(
    capability_id: str,
    *commands: str,
    surface: Surface = "planner",
    cache_only: bool = True,
    network_required: bool = False,
    explicit_local_state_mutation: bool = False,
    mutates_steam: bool = False,
    providers: tuple[tuple[str, SupportLevel], ...] = (),
    requirements: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> CapabilityEntry:
    return {
        "id": capability_id,
        "surface": surface,
        "status": "implemented",
        "commands": list(commands),
        "cache_only": cache_only,
        "network_required": network_required,
        "explicit_local_state_mutation": explicit_local_state_mutation,
        "mutates_steam": mutates_steam,
        "providers": [
            {"provider": provider, "support_level": support_level}
            for provider, support_level in providers
        ],
        "requirements": list(requirements),
        "limitations": list(limitations),
    }


def capability_catalog() -> CapabilityCatalog:
    """Return a fresh command index, without inspecting host readiness.

    Explicit mutation describes a command family's requested effects. Cache
    reads may also maintain retention or ledger expiry in writable local stores;
    cache-only means they acquire no provider evidence over the network.
    """

    local: tuple[tuple[str, SupportLevel], ...] = (
        ("local_steam", "local_observation"),
    )
    owned: tuple[tuple[str, SupportLevel], ...] = (("steam_web_api", "official"),)
    wishlist: tuple[tuple[str, SupportLevel], ...] = (
        ("steam_web_api", "official_undocumented_provisional"),
    )
    declared: tuple[tuple[str, SupportLevel], ...] = (
        ("steam_store", "provisional"),
    )
    derived: tuple[tuple[str, SupportLevel], ...] = (("local_cache", "derived"),)
    configured = ("configured account",)
    provider_sync = (
        "configured account",
        "provider credentials where required",
        "explicit local-retention acknowledgment where required",
    )
    entries = [
        _entry(
            "installed.read",
            "steam-agent games query --scope installed --machine MACHINE",
            providers=local,
            limitations=("installed does not establish ownership",),
        ),
        _entry(
            "installed.sync",
            "steam-agent sync installed --machine MACHINE",
            cache_only=False,
            explicit_local_state_mutation=True,
            providers=local,
            limitations=("partial scans preserve the last complete projection",),
        ),
        _entry(
            "owned.read",
            "steam-agent games query --scope owned --account ACCOUNT",
            providers=owned,
            requirements=configured,
            limitations=("visible-owned inventory can omit private titles",),
        ),
        _entry(
            "owned.sync",
            "steam-agent sync owned --account ACCOUNT",
            cache_only=False,
            network_required=True,
            explicit_local_state_mutation=True,
            providers=owned,
            requirements=provider_sync,
        ),
        _entry(
            "wishlist.read",
            "steam-agent games query --scope wishlist --account ACCOUNT",
            "steam-agent games query --scope wishlist --account ACCOUNT --appid APPID --include-names",
            providers=wishlist,
            requirements=configured,
            limitations=("wishlist membership does not establish ownership",),
        ),
        _entry(
            "wishlist.plan",
            "steam-agent wishlist plan --account ACCOUNT --appid APPID --action add",
            "steam-agent wishlist plan --account ACCOUNT --appid APPID --action remove",
            "steam-agent wishlist plan --account ACCOUNT --appid APPID --action reprioritize --priority 0",
            providers=wishlist,
            requirements=configured,
            limitations=("inert proposal; a fresh matching cached baseline is required", "does not mutate the Steam wishlist",),
        ),
        _entry(
            "purchase.plan",
            "steam-agent deals plan --account ACCOUNT --appid APPID --country COUNTRY",
            providers=derived,
            requirements=configured,
            limitations=("cached decision card; licenses, editions and checkout total remain unknown", "checkout is human-only",),
        ),
        _entry(
            "wishlist.sync",
            "steam-agent sync wishlist --account ACCOUNT",
            cache_only=False,
            network_required=True,
            explicit_local_state_mutation=True,
            providers=wishlist,
            requirements=provider_sync,
        ),
        _entry(
            "library.read",
            "steam-agent games query --scope library --account ACCOUNT --machine MACHINE",
            providers=derived,
            requirements=configured,
            limitations=("joins cached visible-owned and installed evidence",),
        ),
        _entry(
            "catalog.sync",
            "steam-agent sync catalog --account ACCOUNT --machine MACHINE",
            cache_only=False,
            network_required=True,
            explicit_local_state_mutation=True,
            providers=(("steam_store_api", "official"),),
            requirements=provider_sync,
            limitations=("bounded application classification, not a full store mirror",),
        ),
        _entry(
            "prices.sync",
            "steam-agent sync prices --scope wishlist --account ACCOUNT --country COUNTRY",
            cache_only=False,
            network_required=True,
            explicit_local_state_mutation=True,
            providers=(
                ("gg-deals", "contractual_third_party"),
                ("cheapshark", "contractual_third_party"),
            ),
            requirements=provider_sync,
            limitations=("provider and regional coverage are bounded",),
        ),
        _entry(
            "deals.rank",
            "steam-agent deals query --scope wishlist --account ACCOUNT --country COUNTRY",
            providers=derived,
            requirements=configured,
            limitations=("missing prices remain unknown",),
        ),
        _entry(
            "activity.sync",
            "steam-agent sync activity --account ACCOUNT",
            "steam-agent sync achievements --account ACCOUNT --appid APPID",
            cache_only=False,
            network_required=True,
            explicit_local_state_mutation=True,
            providers=owned,
            requirements=provider_sync,
        ),
        _entry(
            "activity.read",
            "steam-agent activity query --account ACCOUNT",
            "steam-agent achievements query --account ACCOUNT",
            providers=owned,
            requirements=configured,
        ),
        _entry(
            "reviews.sync",
            "steam-agent sync reviews --scope wishlist --account ACCOUNT",
            cache_only=False,
            network_required=True,
            explicit_local_state_mutation=True,
            providers=(("steam-store-reviews", "official"),),
            requirements=provider_sync,
            limitations=("aggregate reviews do not establish personal preference",),
        ),
        _entry(
            "recommendations.rank",
            "steam-agent recommendations query --account ACCOUNT --machine MACHINE --recipe RECIPE",
            "steam-agent recommendations wishlist --account ACCOUNT --country COUNTRY",
            providers=derived,
            requirements=configured,
            limitations=("subjective scores remain separate from hard eligibility",),
        ),
        _entry(
            "system.sync",
            "steam-agent sync system --machine MACHINE",
            cache_only=False,
            explicit_local_state_mutation=True,
            providers=(("local_system", "local_observation"),),
        ),
        _entry(
            "system.read",
            "steam-agent system query --machine MACHINE",
            providers=(("local_system", "local_observation"),),
        ),
        _entry(
            "declared_facts.sync",
            "steam-agent sync app-facts --scope appids --appid APPID --account ACCOUNT --machine MACHINE --country COUNTRY --language LANGUAGE",
            "steam-agent sync compatibility --scope library --account ACCOUNT --machine MACHINE --country COUNTRY --language LANGUAGE",
            cache_only=False,
            network_required=True,
            explicit_local_state_mutation=True,
            providers=declared,
            requirements=provider_sync,
        ),
        _entry(
            "discovery.read",
            "steam-agent discovery query --scope appids --appid APPID --limit LIMIT --account ACCOUNT --machine MACHINE --country COUNTRY --language LANGUAGE",
            providers=declared,
            requirements=configured,
            limitations=("bounded known candidates, not store-wide discovery",),
        ),
        _entry(
            "compatibility.assess",
            "steam-agent compatibility assess APPID --target machine:MACHINE --account ACCOUNT --country COUNTRY --language LANGUAGE",
            providers=derived,
            requirements=configured,
            limitations=("declared compatibility does not prove playability or performance",),
        ),
        _entry(
            "groups.read",
            "steam-agent group ownership APPID --member MEMBER --account ACCOUNT --machine MACHINE --country COUNTRY --language LANGUAGE",
            "steam-agent group eligibility APPID --member MEMBER --account ACCOUNT --machine MACHINE --country COUNTRY --language LANGUAGE --mode MODE",
            "steam-agent group recommend --scope appids --appid APPID --limit LIMIT --member MEMBER --context-account ACCOUNT --context-machine MACHINE --country COUNTRY --language LANGUAGE --mode MODE --objective OBJECTIVE",
            "steam-agent profiles list",
            providers=derived,
            requirements=configured,
            limitations=("unknown ownership does not establish available copies",),
        ),
        _entry(
            "groups.configure",
            "steam-agent profiles create PROFILE",
            "steam-agent profiles delete PROFILE",
            "steam-agent profiles clear-account PROFILE",
            "steam-agent ownership set MEMBER APPID STATE",
            "steam-agent ownership clear MEMBER APPID",
            "steam-agent family set RECIPIENT APPID STATE --source SOURCE",
            "steam-agent family clear RECIPIENT APPID --source SOURCE",
            "steam-agent fact set MEMBER APPID FACT VALUE",
            "steam-agent fact clear MEMBER APPID FACT",
            explicit_local_state_mutation=True,
            providers=(("user", "user_asserted"),),
        ),
        _entry(
            "operations.observe",
            "steam-agent operations observe --machine MACHINE",
            providers=derived,
            limitations=("runtime, queue, saves and update currency remain unobserved",),
        ),
        _entry(
            "storage.rank",
            "steam-agent storage rank --recipe reclaim-space/0.1 --machine MACHINE --target-bytes BYTES --limit LIMIT",
            "steam-agent storage rank --recipe travel-install/0.1 --account ACCOUNT --machine MACHINE --country COUNTRY --language LANGUAGE --budget-bytes BYTES --limit LIMIT",
            providers=derived,
            limitations=("ranking does not prove safe uninstall or download completion",),
        ),
        _entry(
            "operations.plan",
            "steam-agent operations plan OPERATION APPID --account ACCOUNT --machine MACHINE",
            providers=derived,
            requirements=configured,
            limitations=(
                "inert plans never authorize or execute actions",
                "uninstall, move and backup remain human steps",
            ),
        ),
        _entry(
            "accounts.configure",
            "steam-agent accounts discover",
            "steam-agent accounts configure --from-local-most-recent --alias ACCOUNT",
            "steam-agent accounts configure --steam-id64 STEAMID --alias ACCOUNT",
            "steam-agent accounts status --alias ACCOUNT",
            "steam-agent owned capability --account ACCOUNT",
            "steam-agent accounts remove --alias ACCOUNT",
            cache_only=False,
            explicit_local_state_mutation=True,
            providers=(("local_steam", "local_observation"), ("user", "user_asserted")),
            limitations=("explicit account configuration does not verify account existence, login or API access",),
        ),
        _entry(
            "auth.configure",
            "steam-agent auth set PROVIDER",
            "steam-agent auth status PROVIDER",
            "steam-agent auth remove PROVIDER",
            explicit_local_state_mutation=True,
            limitations=("no passwords, Guard codes or session cookies accepted",),
        ),
        _entry(
            "providers.probe",
            "steam-agent auth probe PROVIDER",
            "steam-agent owned probe --account ACCOUNT",
            cache_only=False,
            network_required=True,
            requirements=configured,
        ),
        _entry(
            "preferences.configure",
            "steam-agent feedback rate APPID --value RATING --account ACCOUNT",
            "steam-agent feedback finish APPID --account ACCOUNT",
            "steam-agent feedback abandon APPID --account ACCOUNT",
            "steam-agent feedback resume APPID --account ACCOUNT",
            "steam-agent feedback clear-state APPID --account ACCOUNT",
            "steam-agent feedback snooze APPID --until TIMESTAMP --account ACCOUNT",
            "steam-agent feedback estimate APPID --remaining-minutes MINUTES --account ACCOUNT",
            "steam-agent feedback trait APPID --trait TRAIT --value TRAIT_STATE --account ACCOUNT",
            "steam-agent feedback query --account ACCOUNT",
            "steam-agent preferences rule set --trait TRAIT --kind KIND --strength STRENGTH --weight WEIGHT --account ACCOUNT",
            "steam-agent preferences rule list --account ACCOUNT",
            "steam-agent preferences rule remove --trait TRAIT --account ACCOUNT",
            explicit_local_state_mutation=True,
            providers=(("user", "user_asserted"),),
            requirements=configured,
        ),
        _entry(
            "data.delete",
            "steam-agent data delete --provider PROVIDER --account ACCOUNT --yes",
            explicit_local_state_mutation=True,
            limitations=("deletes tool-retained data, not Steam content or credentials at providers",),
        ),
        _entry(
            "diagnostics.read",
            "steam-agent status",
            "steam-agent capabilities",
            "steam-agent capabilities --all",
            "steam-agent doctor --offline",
            cache_only=False,
            providers=local,
            limitations=("legacy diagnostics cover narrow installed-read prerequisites",),
        ),
    ]
    for operation in ("install", "verify", "launch", "stop"):
        requirements = ["separately provisioned broker", f"{operation} policy grant"]
        limitations = ["host readiness and live outcomes are not checked by this catalog"]
        if operation in {"launch", "stop"}:
            requirements.extend((
                "per-AppID allowlist",
                "Windows requires the owner's interactive desktop session",
            ))
        else:
            requirements.extend((
                "qualified Linux content execution",
                "configured SteamCMD",
                "explicit account alias to Steam login mapping",
            ))
        if operation == "launch":
            limitations.append("success means dispatched; playability is not established")
        elif operation == "stop":
            requirements.append("--force additionally requires allow_force")
            limitations.append("stop targets one game, not the Steam client")
        elif operation == "install":
            limitations.append("install includes update; first-run setup remains unverified")
        else:
            limitations.append("verify can replace locally modified official files")
        entries.append(
            _entry(
                f"execution.{operation}",
                f"steam-agent-broker {operation} APPID --request-id ID",
                surface="broker",
                mutates_steam=True,
                cache_only=False,
                network_required=operation in {"install", "verify"},
                explicit_local_state_mutation=True,
                providers=(("steam", "policy_gated"),),
                requirements=tuple(requirements),
                limitations=tuple(limitations),
            )
        )
    entries.append(
        _entry(
            "execution.manage",
            "steam-agent-broker init --library LIBRARY",
            "steam-agent-broker init --library LIBRARY --steamcmd STEAMCMD --account ACCOUNT --steam-login LOGIN",
            "steam-agent-broker request --account ACCOUNT",
            "steam-agent-broker confirm NONCE --actor ACTOR",
            "steam-agent-broker run --operation-id ID",
            "steam-agent-broker reconcile",
            surface="broker",
            mutates_steam=True,
            cache_only=False,
            explicit_local_state_mutation=True,
            requirements=("separately provisioned broker",),
            limitations=("reconcile may restore the Steam client and adoption state",),
        )
    )
    entries.append(
        _entry(
            "broker.read",
            "steam-agent-broker runtime --appid APPID",
            "steam-agent-broker inspect ID",
            "steam-agent-broker inspect --request-id REQUEST_ID",
            "steam-agent-broker preflight launch APPID",
            "steam-agent-broker desktop-jobs",
            "steam-agent-broker desktop-collect JOB_ID",
            "steam-agent-broker capabilities",
            "steam-agent-broker status",
            "steam-agent-broker policy",
            surface="broker",
            cache_only=False,
            mutates_steam=False,
            limitations=(
                "runtime process evidence does not prove playability or saved state",
                "capabilities reports implementation and configuration, not live qualification",
            ),
        )
    )
    return {
        "catalog_schema": "capability-catalog/0.1",
        "runtime_checked": False,
        "storage_maintenance_possible": True,
        "capabilities": sorted(entries, key=lambda entry: entry["id"]),
    }
