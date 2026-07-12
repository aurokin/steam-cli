"""Argument parsing and process boundary for the M1 CLI."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import errno
import getpass
import hashlib
import os
from pathlib import Path
import re
import sys
import sqlite3
import stat
import time
from typing import Any, Iterator, Mapping, Sequence
import unicodedata
import warnings

from steam_agent import __version__
from steam_agent.application import (
    default_credential_dir,
    default_database_path,
    discover_steam_root,
    installed_item,
    machine_for,
    sync_installed,
    usable_steam_root,
)
from steam_agent.owned_library import (
    OWNED_DISCLOSURE_VERSION,
    OwnedSyncError,
    owned_item,
    sync_owned,
)
from steam_agent.catalog_inventory import CatalogSyncError, sync_catalog
from steam_agent.credentials import (
    CredentialError,
    CredentialRef,
    NativeKeyringStore,
    ProtectedFileStore,
    SecretValue,
)
from steam_agent.contracts import (
    CompletenessStatus,
    ErrorCode,
    ErrorRecord,
    WarningRecord,
    completeness,
    encode_json,
    error_envelope,
    success_envelope,
)
from steam_agent.storage import (
    MAX_DECLARED_APP_DEMAND,
    AccountConflict,
    Storage,
    StorageError,
)
from steam_agent.local_accounts import (
    AmbiguousLocalAccounts,
    LocalAccountError,
    LocalAccountRegistryUnavailable,
    MalformedLocalAccountRegistry,
    NoLocalAccount,
    discover_local_accounts,
    select_primary_local_account,
)
from steam_agent.steam_web_api import SteamApiError, SteamWebApiClient
from steam_agent.steam_wishlist import SteamWishlistClient
from steam_agent.wishlist_library import (
    WISHLIST_DISCLOSURE_VERSION,
    WishlistSyncError,
    sync_wishlist,
)
from steam_agent.steam_store_catalog import (
    CatalogApiError,
    SteamStoreCatalogClient,
)
from steam_agent.provider_auth import ProviderAuthClient, ProviderAuthError
from steam_agent.gg_deals import GgDealsClient, GgDealsError
from steam_agent.cheapshark import CheapSharkClient, CheapSharkError
from steam_agent.price_library import PriceSyncError, sync_wishlist_prices
from steam_agent.deal_query import build_deal_query_from_snapshot
from steam_agent.feedback import FeedbackService
from steam_agent.activity import (
    ACTIVITY_DISCLOSURE_VERSION,
    ActivitySyncError,
    query_activity,
    query_achievements,
    sync_activity,
    sync_achievements,
)
from steam_agent.recommendations import ConstraintOverride, Requirement
from steam_agent.recommendation_query import build_recommendation_query
from steam_agent.steam_activity_api import SteamActivityApiClient
from steam_agent.steam_reviews import SteamReviewClient
from steam_agent.review_library import (
    REVIEW_DISCLOSURE_VERSION,
    ReviewSyncError,
    sync_wishlist_reviews,
)
from steam_agent.wishlist_recommendations import GateOverride
from steam_agent.wishlist_recommendation_query import (
    build_wishlist_recommendation_query,
)
from steam_agent.system_profile import (
    SYSTEM_PROFILE_DISCLOSURE_VERSION,
    SystemProfileError,
    canonical_architecture,
    collect_system_profile,
    query_system_profile,
    sync_system_profile,
)
from steam_agent.steam_declared_facts import (
    DECLARED_FACTS_DISCLOSURE_VERSION,
    MULTIPLAYER_CATEGORY_SLUGS,
    SteamDeclaredFactsClient,
    SteamDeclaredFactsError,
    SteamDeclaredFactsRequestContext,
    declared_discovery_facts,
    declared_facts_payload,
)
from steam_agent.compatibility import (
    MAX_COMPONENTS,
    CompatibilityTarget,
    FeatureRequirement,
    GateOverride as CompatibilityGateOverride,
    validate_compatibility_request,
)
from steam_agent.compatibility_adapter import (
    assess_compatibility_snapshot,
    compatibility_query_data,
)


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNAVAILABLE = 3
SECRET_FLAGS = frozenset(
    {"--api-key", "--token", "--password", "--cookie", "--client-secret"}
)
_SAFE_WARNING_SOURCE = re.compile(r"(?:libraryfolders\.vdf|appmanifest_\d+\.acf)\Z")
_OWNED_CAPABILITY = "owned.visible.read"
_OWNED_PROBE_FRESHNESS_SECONDS = 24 * 60 * 60
_OWNED_SYNC_FRESHNESS_SECONDS = 24 * 60 * 60
_WISHLIST_SYNC_FRESHNESS_SECONDS = 24 * 60 * 60
_CATALOG_SYNC_FRESHNESS_SECONDS = 24 * 60 * 60
_SYNC_ABANDONED_SECONDS = 15 * 60
_PROVIDER_MINIMUM_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class _CredentialProviderSpec:
    cli_name: str
    storage_provider: str
    kind: str
    prompt_label: str
    display_label: str
    missing_capability: str
    dependent_capability: str | None = None


_CREDENTIAL_PROVIDERS = {
    spec.cli_name: spec
    for spec in (
        _CredentialProviderSpec(
            "steam-web-api",
            "steam",
            "web-api-key",
            "Steam Web API key",
            "Steam Web API user key",
            "credential:steam_web_api_user_key",
            _OWNED_CAPABILITY,
        ),
        _CredentialProviderSpec(
            "isthereanydeal",
            "isthereanydeal",
            "api-key",
            "IsThereAnyDeal API key",
            "IsThereAnyDeal API key",
            "credential:isthereanydeal_api_key",
        ),
        _CredentialProviderSpec(
            "steamgriddb",
            "steamgriddb",
            "api-key",
            "SteamGridDB API key",
            "SteamGridDB API key",
            "credential:steamgriddb_api_key",
        ),
        _CredentialProviderSpec(
            "gg-deals",
            "gg-deals",
            "api-key",
            "GG.deals API key",
            "GG.deals API key",
            "credential:gg_deals_api_key",
        ),
    )
}
_AUTH_PROVIDER_NAMES = tuple(_CREDENTIAL_PROVIDERS)
_AUTH_PROBE_PROVIDER_NAMES = ("steamgriddb", "gg-deals")
_AUTH_PROBE_INTERVAL_SECONDS = {
    "isthereanydeal": 3.0,
    "steamgriddb": 1.0,
    "gg-deals": 1.0,
}


class CliUsageError(ValueError):
    """An argparse failure safe to serialize without a traceback."""


class AgentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = AgentArgumentParser(
        prog="steam-agent",
        description="Local-first Steam evidence and operations for agents.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--data-dir", type=Path, help="Override the local data directory."
    )
    parser.add_argument("--format", choices=("json", "table"), default="json")
    commands = parser.add_subparsers(dest="command", required=True)

    status_parser = commands.add_parser(
        "status", help="Show local data and M1 readiness."
    )
    _add_leaf_format(status_parser)
    capabilities_parser = commands.add_parser(
        "capabilities", help="Show available M1 capabilities."
    )
    _add_leaf_format(capabilities_parser)
    doctor = commands.add_parser("doctor", help="Check local M1 prerequisites.")
    _add_leaf_format(doctor)
    doctor.add_argument(
        "--offline", action="store_true", help="Do not use the network."
    )

    sync = commands.add_parser("sync", help="Synchronize a capability.")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    installed = sync_commands.add_parser(
        "installed", help="Scan installed Steam games."
    )
    _add_leaf_format(installed)
    installed.add_argument("--machine", default="local")
    installed.add_argument("--steam-root", type=Path)
    owned_sync = sync_commands.add_parser(
        "owned", help="Synchronize the visible owned library."
    )
    _add_leaf_format(owned_sync)
    owned_sync.add_argument("--account", default="primary")
    owned_sync.add_argument(
        "--acknowledge-local-storage",
        action="store_true",
        help="Accept the versioned local storage and backup disclosure.",
    )
    catalog_sync = sync_commands.add_parser(
        "catalog", help="Synchronize bounded catalog evidence for observed AppIDs."
    )
    _add_leaf_format(catalog_sync)
    catalog_sync.add_argument("--account", default="primary")
    catalog_sync.add_argument("--machine", default="local")
    wishlist_sync = sync_commands.add_parser(
        "wishlist", help="Synchronize the provisional Steam wishlist."
    )
    _add_leaf_format(wishlist_sync)
    wishlist_sync.add_argument("--account", default="primary")
    wishlist_sync.add_argument(
        "--acknowledge-local-storage",
        action="store_true",
        help="Accept the versioned wishlist storage and backup disclosure.",
    )
    prices_sync = sync_commands.add_parser(
        "prices", help="Synchronize current and historical-low wishlist evidence."
    )
    _add_leaf_format(prices_sync)
    prices_sync.add_argument("--scope", choices=("wishlist",), required=True)
    prices_sync.add_argument("--account", default="primary")
    prices_sync.add_argument("--country", required=True)
    prices_sync.add_argument(
        "--provider", choices=("auto", "gg-deals", "cheapshark"), default="auto"
    )
    prices_sync.add_argument("--max-items", type=int)
    activity_sync = sync_commands.add_parser(
        "activity", help="Synchronize normalized owned and recent activity."
    )
    _add_leaf_format(activity_sync)
    activity_sync.add_argument("--account", default="primary")
    activity_sync.add_argument("--acknowledge-local-storage", action="store_true")
    achievements_sync = sync_commands.add_parser(
        "achievements", help="Synchronize bounded achievement evidence."
    )
    _add_leaf_format(achievements_sync)
    achievements_sync.add_argument("--account", default="primary")
    achievements_sync.add_argument(
        "--scope", choices=("recent", "installed", "owned"), default="recent"
    )
    achievements_sync.add_argument("--appid", type=int, action="append", default=[])
    achievements_sync.add_argument("--max-items", type=int, default=20)
    achievements_sync.add_argument("--acknowledge-local-storage", action="store_true")
    reviews_sync = sync_commands.add_parser(
        "reviews", help="Synchronize bounded public aggregate wishlist reviews."
    )
    _add_leaf_format(reviews_sync)
    reviews_sync.add_argument("--scope", choices=("wishlist",), required=True)
    reviews_sync.add_argument("--account", default="primary")
    reviews_sync.add_argument("--max-items", type=int)
    reviews_sync.add_argument("--acknowledge-local-storage", action="store_true")
    system_sync = sync_commands.add_parser(
        "system", help="Synchronize a privacy-bounded local system profile."
    )
    _add_leaf_format(system_sync)
    system_sync.add_argument("--machine", default="local")
    system_sync.add_argument("--acknowledge-local-storage", action="store_true")
    compatibility_sync = sync_commands.add_parser(
        "compatibility",
        help="Synchronize provisional Steam-declared compatibility facts.",
    )
    _add_leaf_format(compatibility_sync)
    compatibility_sync.add_argument("--scope", choices=("library",), required=True)
    compatibility_sync.add_argument("--appid", action="append", type=int, default=[])
    compatibility_sync.add_argument("--account", default="primary")
    compatibility_sync.add_argument("--machine", default="local")
    compatibility_sync.add_argument("--country", required=True)
    compatibility_sync.add_argument("--language", required=True)
    compatibility_sync.add_argument("--max-items", type=int, default=20)
    compatibility_sync.add_argument("--acknowledge-local-storage", action="store_true")
    app_facts_sync = sync_commands.add_parser(
        "app-facts",
        help="Synchronize bounded provisional declared store facts.",
    )
    _add_leaf_format(app_facts_sync)
    app_facts_sync.add_argument(
        "--scope",
        choices=("known", "library", "wishlist", "installed", "appids"),
        required=True,
    )
    app_facts_sync.add_argument("--appid", action="append", type=int, default=[])
    app_facts_sync.add_argument("--account", default="primary")
    app_facts_sync.add_argument("--machine", default="local")
    app_facts_sync.add_argument("--country", required=True)
    app_facts_sync.add_argument("--language", required=True)
    app_facts_sync.add_argument("--max-items", type=int, default=20)
    app_facts_sync.add_argument("--acknowledge-local-storage", action="store_true")

    games = commands.add_parser("games", help="Query normalized games.")
    game_commands = games.add_subparsers(dest="games_command", required=True)
    query = game_commands.add_parser("query", help="Query games in a scope.")
    _add_leaf_format(query)
    query.add_argument(
        "--scope", choices=("installed", "owned", "wishlist", "library"), required=True
    )
    query.add_argument("--machine", default="local")
    query.add_argument("--account", default="primary")
    query.add_argument("--include-paths", action="store_true")

    discovery = commands.add_parser(
        "discovery", help="Query bounded cached declared discovery evidence."
    )
    discovery_commands = discovery.add_subparsers(
        dest="discovery_command", required=True
    )
    discovery_query = discovery_commands.add_parser(
        "query", help="Query declared facts without network access."
    )
    _add_leaf_format(discovery_query)
    discovery_query.add_argument(
        "--scope",
        choices=("known", "library", "wishlist", "installed", "appids"),
        required=True,
    )
    discovery_query.add_argument("--appid", action="append", type=int, default=[])
    discovery_query.add_argument("--limit", type=int, required=True)
    discovery_query.add_argument("--account", default="primary")
    discovery_query.add_argument("--machine", default="local")
    discovery_query.add_argument("--country", required=True)
    discovery_query.add_argument("--language", required=True)
    discovery_query.add_argument("--require-mode")

    deals = commands.add_parser("deals", help="Query cached wishlist deal evidence.")
    deal_commands = deals.add_subparsers(dest="deals_command", required=True)
    deal_query = deal_commands.add_parser(
        "query", help="Rank cached deal evidence for a wishlist."
    )
    _add_leaf_format(deal_query)
    deal_query.add_argument("--scope", choices=("wishlist",), required=True)
    deal_query.add_argument("--account", required=True)
    deal_query.add_argument("--country", required=True)
    deal_query.add_argument(
        "--store-class",
        choices=("official", "keyshop", "unknown"),
        default="official",
    )

    recommendations = commands.add_parser(
        "recommendations", help="Query deterministic cached play recommendations."
    )
    recommendation_commands = recommendations.add_subparsers(
        dest="recommendations_command", required=True
    )
    recommendation_query = recommendation_commands.add_parser(
        "query", help="Rank visible-owned games from one cached evidence snapshot."
    )
    _add_leaf_format(recommendation_query)
    recommendation_query.add_argument("--account", required=True)
    recommendation_query.add_argument("--machine", default="local")
    recommendation_query.add_argument("--scope", choices=("owned",), default="owned")
    recommendation_query.add_argument(
        "--recipe",
        choices=("resume/0.1", "finishability/0.1", "preference-fit/0.1"),
        required=True,
    )
    recommendation_query.add_argument("--time-minutes", type=int)
    recommendation_query.add_argument("--require", action="append", default=[])
    recommendation_query.add_argument(
        "--unknown", choices=("include", "exclude"), default="exclude"
    )
    recommendation_query.add_argument("--override", action="append", default=[])
    recommendation_query.add_argument("--explain", action="store_true")
    wishlist_recommendation = recommendation_commands.add_parser(
        "wishlist", help="Rank wishlist fit from one cached evidence snapshot."
    )
    _add_leaf_format(wishlist_recommendation)
    wishlist_recommendation.add_argument("--account", required=True)
    wishlist_recommendation.add_argument("--country", required=True)
    wishlist_recommendation.add_argument(
        "--store-class", choices=("official", "keyshop", "unknown"), default="official"
    )
    wishlist_recommendation.add_argument(
        "--unknown", choices=("include", "exclude"), default="exclude"
    )
    wishlist_recommendation.add_argument("--override", action="append", default=[])

    activity = commands.add_parser("activity", help="Query cached activity evidence.")
    activity_commands = activity.add_subparsers(dest="activity_command", required=True)
    activity_query = activity_commands.add_parser(
        "query", help="Query cached activity."
    )
    _add_leaf_format(activity_query)
    activity_query.add_argument("--account", default="primary")
    activity_query.add_argument("--appid", type=int)

    achievements = commands.add_parser(
        "achievements", help="Query cached achievement evidence."
    )
    achievement_commands = achievements.add_subparsers(
        dest="achievements_command", required=True
    )
    achievement_query = achievement_commands.add_parser(
        "query", help="Query cached achievement summaries."
    )
    _add_leaf_format(achievement_query)
    achievement_query.add_argument("--account", default="primary")
    achievement_query.add_argument("--appid", type=int)

    system_command = commands.add_parser(
        "system", help="Query cached local system compatibility facts."
    )
    system_commands = system_command.add_subparsers(
        dest="system_command", required=True
    )
    system_query = system_commands.add_parser(
        "query", help="Query one cached system profile."
    )
    _add_leaf_format(system_query)
    system_query.add_argument("--machine", default="local")

    compatibility = commands.add_parser(
        "compatibility", help="Assess cached compatibility evidence."
    )
    compatibility_commands = compatibility.add_subparsers(
        dest="compatibility_command", required=True
    )
    compatibility_assess = compatibility_commands.add_parser(
        "assess", help="Assess explicit AppIDs against one explicit target."
    )
    _add_leaf_format(compatibility_assess)
    compatibility_assess.add_argument("appids", metavar="APPID", nargs="+", type=int)
    compatibility_assess.add_argument("--account", required=True)
    compatibility_assess.add_argument("--target", required=True)
    compatibility_assess.add_argument(
        "--context-machine",
        help="Machine alias carrying declared-fact lineage for non-machine targets.",
    )
    compatibility_assess.add_argument("--country", required=True)
    compatibility_assess.add_argument("--language", required=True)
    compatibility_assess.add_argument("--require", action="append", default=[])
    compatibility_assess.add_argument("--override", action="append", default=[])
    compatibility_assess.add_argument("--explain", action="store_true")

    feedback = commands.add_parser(
        "feedback", help="Manage explicit local game feedback."
    )
    feedback_commands = feedback.add_subparsers(dest="feedback_command", required=True)
    rate = feedback_commands.add_parser("rate", help="Set an explicit game rating.")
    _add_leaf_format(rate)
    rate.add_argument("appid", type=int)
    rate.add_argument("--account", default="primary")
    rate_choice = rate.add_mutually_exclusive_group(required=True)
    rate_choice.add_argument("--value", choices=("liked", "disliked", "neutral"))
    rate_choice.add_argument("--clear", action="store_true")
    for command_name in ("finish", "abandon", "resume"):
        state = feedback_commands.add_parser(
            command_name, help=f"Mark a game as {command_name}."
        )
        _add_leaf_format(state)
        state.add_argument("appid", type=int)
        state.add_argument("--account", default="primary")
    clear_state = feedback_commands.add_parser(
        "clear-state", help="Clear the explicit play state."
    )
    _add_leaf_format(clear_state)
    clear_state.add_argument("appid", type=int)
    clear_state.add_argument("--account", default="primary")
    snooze = feedback_commands.add_parser(
        "snooze", help="Set or clear a temporary snooze."
    )
    _add_leaf_format(snooze)
    snooze.add_argument("appid", type=int)
    snooze.add_argument("--account", default="primary")
    snooze_choice = snooze.add_mutually_exclusive_group(required=True)
    snooze_choice.add_argument("--until")
    snooze_choice.add_argument("--clear", action="store_true")
    estimate = feedback_commands.add_parser(
        "estimate", help="Set or clear explicit time estimates."
    )
    _add_leaf_format(estimate)
    estimate.add_argument("appid", type=int)
    estimate.add_argument("--account", default="primary")
    estimate.add_argument("--minimum-session-minutes", type=int)
    estimate.add_argument("--remaining-minutes", type=int)
    estimate.add_argument("--clear-minimum-session-minutes", action="store_true")
    estimate.add_argument("--clear-remaining-minutes", action="store_true")
    trait = feedback_commands.add_parser(
        "trait", help="Set an explicit user-namespaced trait."
    )
    _add_leaf_format(trait)
    trait.add_argument("appid", type=int)
    trait.add_argument("--account", default="primary")
    trait.add_argument("--trait", required=True)
    trait_choice = trait.add_mutually_exclusive_group(required=True)
    trait_choice.add_argument("--value", choices=("present", "absent", "unknown"))
    trait_choice.add_argument("--clear", action="store_true")
    feedback_query = feedback_commands.add_parser(
        "query", help="Query cached explicit feedback."
    )
    _add_leaf_format(feedback_query)
    feedback_query.add_argument("--account", default="primary")
    feedback_query.add_argument("--appid", type=int)

    preferences = commands.add_parser(
        "preferences", help="Manage explicit profile preference rules."
    )
    preference_commands = preferences.add_subparsers(
        dest="preferences_command", required=True
    )
    rules = preference_commands.add_parser(
        "rule", help="Manage trait preference rules."
    )
    rule_commands = rules.add_subparsers(dest="rule_command", required=True)
    rule_set = rule_commands.add_parser("set", help="Set a trait preference rule.")
    _add_leaf_format(rule_set)
    rule_set.add_argument("--account", default="primary")
    rule_set.add_argument("--trait", required=True)
    rule_set.add_argument(
        "--kind", choices=("prefer", "avoid", "require"), required=True
    )
    rule_set.add_argument("--strength", choices=("soft", "hard"), required=True)
    rule_set.add_argument("--weight", type=int, required=True)
    rule_list = rule_commands.add_parser("list", help="List trait preference rules.")
    _add_leaf_format(rule_list)
    rule_list.add_argument("--account", default="primary")
    rule_remove = rule_commands.add_parser(
        "remove", help="Remove a trait preference rule."
    )
    _add_leaf_format(rule_remove)
    rule_remove.add_argument("--account", default="primary")
    rule_remove.add_argument("--trait", required=True)

    accounts = commands.add_parser(
        "accounts", help="Configure Steam account identities."
    )
    account_commands = accounts.add_subparsers(dest="accounts_command", required=True)
    discover_accounts = account_commands.add_parser(
        "discover",
        help="Inspect local Steam account candidates without exposing identifiers.",
    )
    _add_leaf_format(discover_accounts)
    discover_accounts.add_argument("--steam-root", type=Path)
    discover_accounts.add_argument("--include-identifiers", action="store_true")
    configure_account = account_commands.add_parser(
        "configure", help="Persist an explicitly selected local account alias."
    )
    _add_leaf_format(configure_account)
    configure_account.add_argument("--alias", default="primary")
    selection = configure_account.add_mutually_exclusive_group(required=True)
    selection.add_argument("--from-local-most-recent", action="store_true")
    selection.add_argument("--steam-id64")
    configure_account.add_argument("--steam-root", type=Path)
    account_status = account_commands.add_parser(
        "status", help="Show redacted configured-account status."
    )
    _add_leaf_format(account_status)
    account_status.add_argument("--alias", default="primary")
    account_status.add_argument("--include-identifiers", action="store_true")
    remove_account = account_commands.add_parser(
        "remove",
        help=(
            "Remove an account alias and all account-scoped Steam data while "
            "preserving the shared API key."
        ),
    )
    _add_leaf_format(remove_account)
    remove_account.add_argument("--alias", default="primary")
    remove_account.add_argument("--yes", action="store_true")

    auth = commands.add_parser(
        "auth", help="Manage provider credentials without argv secrets."
    )
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_set = auth_commands.add_parser(
        "set", help="Store a credential from hidden input."
    )
    _add_leaf_format(auth_set)
    auth_set.add_argument("provider", choices=_AUTH_PROVIDER_NAMES)
    auth_set.add_argument("--backend", choices=("os", "file"), default="os")
    auth_set.add_argument("--yes-file-risk", action="store_true")
    auth_status = auth_commands.add_parser(
        "status", help="Show redacted credential status."
    )
    _add_leaf_format(auth_status)
    auth_status.add_argument("provider", choices=_AUTH_PROVIDER_NAMES)
    auth_probe = auth_commands.add_parser(
        "probe",
        help="Explicitly validate a third-party credential without retaining a body.",
    )
    _add_leaf_format(auth_probe)
    auth_probe.add_argument("provider", choices=_AUTH_PROBE_PROVIDER_NAMES)
    auth_remove = auth_commands.add_parser(
        "remove", help="Remove a locally stored credential."
    )
    _add_leaf_format(auth_remove)
    auth_remove.add_argument("provider", choices=_AUTH_PROVIDER_NAMES)
    auth_remove.add_argument("--yes", action="store_true")

    owned = commands.add_parser("owned", help="Inspect visible-owned capability state.")
    owned_commands = owned.add_subparsers(dest="owned_command", required=True)
    owned_capability = owned_commands.add_parser(
        "capability",
        help="Show account, credential, and probe state without network access.",
    )
    _add_leaf_format(owned_capability)
    owned_capability.add_argument("--account", default="primary")
    owned_probe = owned_commands.add_parser(
        "probe",
        help="Explicitly probe visible-owned access without retaining the payload.",
    )
    _add_leaf_format(owned_probe)
    owned_probe.add_argument("--account", default="primary")

    data = commands.add_parser("data", help="Delete locally retained provider data.")
    data_commands = data.add_subparsers(dest="data_command", required=True)
    delete_data = data_commands.add_parser(
        "delete", help="Delete retained Steam Web API account data."
    )
    _add_leaf_format(delete_data)
    delete_data.add_argument(
        "--provider",
        choices=(
            "steam-web-api",
            "steam-store-reviews",
            "steam-store-appdetails",
            "gg-deals",
            "cheapshark",
            "local-system",
        ),
        required=True,
    )
    target = delete_data.add_mutually_exclusive_group(required=True)
    target.add_argument("--account")
    target.add_argument("--machine")
    target.add_argument("--all", action="store_true")
    delete_data.add_argument("--yes", action="store_true")
    return parser


def _add_leaf_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("json", "table"),
        default=argparse.SUPPRESS,
        help="Override output format for this command.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    if any(argument.split("=", 1)[0] in SECRET_FLAGS for argument in effective_argv):
        namespace = argparse.Namespace(format=_parse_error_format(effective_argv))
        return _emit_error(
            namespace,
            command="cli",
            code=ErrorCode.SECRET_ON_ARGV,
            message="Secrets are not accepted as command-line arguments.",
            remediation="Use the future hidden auth prompt or a documented secret file.",
            exit_code=2,
        )
    parser = build_parser()
    try:
        args = parser.parse_args(effective_argv)
    except CliUsageError:
        namespace = argparse.Namespace(format=_parse_error_format(effective_argv))
        return _emit_error(
            namespace,
            command="cli",
            code=ErrorCode.INVALID_ARGUMENT,
            message="The command arguments are invalid.",
            remediation="Run steam-agent --help for supported commands and options.",
            exit_code=2,
        )
    database_path = (
        args.data_dir.expanduser() / "steam-agent.sqlite3"
        if args.data_dir
        else default_database_path()
    )
    try:
        return _dispatch(args, database_path)
    except KeyboardInterrupt:
        return _emit_error(
            args,
            command=_command_name(args),
            code=ErrorCode.INTERNAL_ERROR,
            message="Operation canceled.",
            retryable=True,
        )
    except CredentialError as exc:
        return _emit_error(
            args,
            command=_command_name(args),
            code=exc.code,
            message=str(exc),
            retryable=False,
        )
    except LocalAccountError as exc:
        return _emit_error(
            args,
            command=_command_name(args),
            code=_local_account_error_code(exc),
            message="The local Steam account selection is unavailable.",
            retryable=False,
        )
    except AccountConflict:
        return _emit_error(
            args,
            command=_command_name(args),
            code=ErrorCode.ACCOUNT_CONFLICT,
            message="The account alias or Steam identity is already configured differently.",
            remediation="Remove the conflicting alias before configuring a different identity.",
            retryable=False,
        )
    except (sqlite3.DatabaseError, StorageError, OSError):
        return _emit_error(
            args,
            command=_command_name(args),
            code=ErrorCode.DATABASE_ERROR,
            message="The local data store is unavailable or corrupt.",
            retryable=False,
        )
    except Exception as exc:
        print(f"steam-agent: {type(exc).__name__}", file=sys.stderr)
        return _emit_error(
            args,
            command=_command_name(args),
            code=ErrorCode.INTERNAL_ERROR,
            message="The command failed unexpectedly.",
            retryable=False,
        )


def _dispatch(args: argparse.Namespace, database_path: Path) -> int:
    if args.command == "status":
        count = 0
        if database_path.exists():
            with Storage(database_path) as storage:
                count = len(storage.list_installed("local"))
        return _emit_success(
            args,
            command="status",
            data={
                "version": __version__,
                "database_initialized": database_path.exists(),
                "installed_count": count,
            },
        )
    if args.command == "capabilities":
        root = discover_steam_root()
        capability_completeness = _installed_read_completeness(root)
        return _emit_success(
            args,
            command="capabilities",
            completeness_value=capability_completeness,
            data={
                "capabilities": [
                    {
                        "name": "installed.read",
                        "state": "ready" if root else "unavailable",
                        "auth_scope": "local_os_user",
                        "network_required": False,
                        "interface_status": "unofficial_local_read_only",
                    }
                ]
            },
        )
    if args.command == "doctor":
        root = discover_steam_root()
        return _emit_success(
            args,
            command="doctor",
            data={
                "offline": bool(args.offline),
                "installed_read": "ready" if root else "unavailable",
            },
            completeness_value=_installed_read_completeness(root),
        )
    if args.command in {"feedback", "preferences"}:
        return _dispatch_feedback(args, database_path)
    if args.command == "sync" and args.sync_command == "owned":
        return _dispatch_sync_owned(args, database_path)
    if args.command == "sync" and args.sync_command == "catalog":
        return _dispatch_sync_catalog(args, database_path)
    if args.command == "sync" and args.sync_command == "wishlist":
        return _dispatch_sync_wishlist(args, database_path)
    if args.command == "sync" and args.sync_command == "prices":
        return _dispatch_sync_prices(args, database_path)
    if args.command == "sync" and args.sync_command in {"activity", "achievements"}:
        return _dispatch_activity(args, database_path)
    if args.command == "sync" and args.sync_command == "reviews":
        return _dispatch_sync_reviews(args, database_path)
    if args.command == "sync" and args.sync_command == "system":
        return _dispatch_system(args, database_path)
    if args.command == "sync" and args.sync_command in {"compatibility", "app-facts"}:
        return _dispatch_sync_compatibility(args, database_path)
    if args.command == "sync" and args.sync_command == "installed":
        root = args.steam_root or discover_steam_root()
        if root is None:
            invalid_override = bool(os.environ.get("STEAM_AGENT_STEAM_ROOT"))
            return _emit_error(
                args,
                command="sync.installed",
                code=(
                    ErrorCode.STEAM_ROOT_INACCESSIBLE
                    if invalid_override
                    else ErrorCode.STEAM_NOT_FOUND
                ),
                message=(
                    "The configured Steam root is missing or inaccessible."
                    if invalid_override
                    else "No Steam installation was found."
                ),
                remediation="Pass --steam-root with the Steam installation directory.",
            )
        if not usable_steam_root(root):
            return _emit_error(
                args,
                command="sync.installed",
                code=ErrorCode.STEAM_ROOT_INACCESSIBLE,
                message="The configured Steam root is missing or inaccessible.",
                remediation="Pass --steam-root with a readable Steam installation directory.",
            )
        with Storage(database_path) as storage:
            result = sync_installed(
                storage,
                steam_root=root,
                machine_id=args.machine,
            )
        warnings = [
            WarningRecord(
                code=warning.code,
                message=warning.message,
                source=_warning_source(warning.path),
            )
            for warning in result.scan.warnings
        ]
        status = (
            CompletenessStatus.COMPLETE
            if result.run.status == "complete"
            else CompletenessStatus.PARTIAL
        )
        return _emit_success(
            args,
            command="sync.installed",
            context={"machine_id": args.machine},
            completeness_value=completeness(status, warnings=warnings),
            data={
                "sync_run_id": result.run.id,
                "sync_status": result.run.status,
                "records_seen": result.run.records_seen,
                "recorded_appids": list(result.recorded_appids),
                "skipped_appids": list(result.skipped_appids),
                "parser_version": result.scan.parser_version,
            },
        )
    if args.command == "games" and args.games_command == "query":
        if args.scope == "wishlist":
            return _dispatch_wishlist_games_query(args, database_path)
        if args.scope in ("owned", "library"):
            return _dispatch_account_games_query(args, database_path)
        with Storage(database_path) as storage:
            installed_snapshot = storage.read_installed_snapshot(args.machine)
        games = installed_snapshot.games
        latest = installed_snapshot.latest
        latest_complete = installed_snapshot.latest_complete
        query_completeness: dict[str, Any]
        snapshot: dict[str, Any]
        if latest is None:
            query_completeness = completeness(
                CompletenessStatus.UNAVAILABLE,
                missing_capabilities=["installed.read"],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.NOT_SYNCED,
                        message="Installed games have not been synchronized for this machine.",
                    )
                ],
            )
            snapshot = {"last_attempt_status": None, "last_successful_sync_at": None}
        elif latest.status == "complete":
            query_completeness = completeness(CompletenessStatus.COMPLETE)
            snapshot = {
                "last_attempt_status": latest.status,
                "last_successful_sync_at": latest.completed_at,
            }
        elif latest.status == "running":
            running_warning = WarningRecord(
                code=ErrorCode.SYNC_IN_PROGRESS,
                message="An installed-games synchronization is currently in progress.",
            )
            if latest_complete is None:
                query_completeness = completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["installed.read"],
                    warnings=[running_warning],
                )
                successful_at = None
            else:
                query_completeness = completeness(
                    CompletenessStatus.COMPLETE,
                    warnings=[running_warning],
                )
                successful_at = latest_complete.completed_at
            snapshot = {
                "last_attempt_status": latest.status,
                "last_successful_sync_at": successful_at,
            }
        elif latest_complete is None:
            query_completeness = completeness(
                CompletenessStatus.UNAVAILABLE,
                missing_capabilities=["installed.read"],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.PARTIAL_SCAN,
                        message="The latest scan was incomplete and no last-good snapshot exists.",
                    )
                ],
            )
            snapshot = {
                "last_attempt_status": latest.status,
                "last_successful_sync_at": None,
            }
        else:
            query_completeness = completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=["installed.read"],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message="The latest scan was incomplete; results use the last-good snapshot.",
                    )
                ],
            )
            snapshot = {
                "last_attempt_status": latest.status,
                "last_successful_sync_at": latest_complete.completed_at,
            }
        return _emit_success(
            args,
            command="games.query",
            context={"machine_id": args.machine, "scopes": ["installed"]},
            completeness_value=query_completeness,
            data={
                "items": [
                    installed_item(game, include_paths=args.include_paths)
                    for game in games
                ],
                "next_cursor": None,
                "snapshot": snapshot,
            },
        )
    if args.command == "system":
        return _dispatch_system(args, database_path)
    if args.command == "compatibility":
        return _dispatch_compatibility(args, database_path)
    if args.command == "discovery":
        return _dispatch_discovery(args, database_path)
    if args.command == "deals" and args.deals_command == "query":
        return _dispatch_deals_query(args, database_path)
    if args.command == "recommendations" and args.recommendations_command == "wishlist":
        return _dispatch_wishlist_recommendations(args, database_path)
    if args.command == "recommendations":
        return _dispatch_recommendations_query(args, database_path)
    if args.command in {"activity", "achievements"}:
        return _dispatch_activity(args, database_path)
    if args.command == "accounts":
        return _dispatch_accounts(args, database_path)
    if args.command == "auth":
        return _dispatch_auth(args, database_path)
    if args.command == "owned":
        return _dispatch_owned(args, database_path)
    if args.command == "data":
        return _dispatch_data(args, database_path)
    raise AssertionError("argparse accepted an unhandled command")


def _dispatch_activity(args: argparse.Namespace, database_path: Path) -> int:
    command = _command_name(args)
    if args.command in {"activity", "achievements"}:
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                account = None
            if account is None:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            try:
                result = (
                    query_activity(
                        storage,
                        account_id=account.id,
                        appid=args.appid,
                        clock=_utc_now,
                    )
                    if args.command == "activity"
                    else query_achievements(
                        storage,
                        account_id=account.id,
                        appid=args.appid,
                        clock=_utc_now,
                    )
                )
            except ValueError:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The query arguments are invalid.",
                    exit_code=2,
                )
        query_completeness = _activity_query_completeness(args.command, result)
        return _emit_success(
            args,
            command=command,
            context={"account_alias": account.alias, "identifiers_included": False},
            completeness_value=query_completeness,
            data=result,
        )

    with _credential_operation_lock(database_path):
        credential_ref = _steam_credential_ref(database_path)
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                account = None
            if account is None:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            consent = storage.get_activity_data_consent(account.id)
            if (
                consent is None
                or consent.disclosure_version != ACTIVITY_DISCLOSURE_VERSION
            ):
                if not args.acknowledge_local_storage:
                    return _emit_error(
                        args,
                        command=command,
                        code=ErrorCode.DATA_POLICY_ACKNOWLEDGMENT_REQUIRED,
                        message=(
                            "Steam activity and bounded achievement evidence will be stored "
                            "locally: AppIDs, normalized playtime and recent-window fields, "
                            "last-played times, achievement state and public schema text, "
                            "plus coarse attempt metadata. Recent play is not a session log, "
                            "achievement percentage is not completion truth, and activity does "
                            "not imply preference. Locked hidden achievement text is suppressed "
                            "from normal output. The selected filesystem, replicas, snapshots, "
                            "and user-controlled backups may retain copies after local deletion."
                        ),
                        remediation=(
                            "Rerun with --acknowledge-local-storage to accept this "
                            "versioned local-storage policy."
                        ),
                    )
                storage.record_activity_data_consent(
                    account_id=account.id,
                    disclosure_version=ACTIVITY_DISCLOSURE_VERSION,
                    accepted_at=_utc_now(),
                    backups_acknowledged=True,
                )
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            if metadata is None:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.AUTH_REQUIRED,
                    message="A Steam Web API user key has not been configured.",
                )
            resolved = _resolve_credential(metadata, credential_ref)
            if resolved["state"] != "configured":
                return _emit_error(
                    args,
                    command=command,
                    code=_credential_error_code(resolved["state"]),
                    message="The Steam Web API credential is unavailable.",
                )

            def request_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "steam-web-api", _utc_now(), _PROVIDER_MINIMUM_INTERVAL_SECONDS
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise ActivitySyncError("REQUEST_THROTTLED", retryable=True)

            try:
                if args.sync_command == "activity":
                    result = sync_activity(
                        storage,
                        account_id=account.id,
                        steamid=account.provider_account_id,
                        api_key=resolved["secret"],
                        client=_steam_activity_client(),
                        request_gate=request_gate,
                        clock=_utc_now,
                    )
                    data = {
                        "sync_run_id": result.run.id,
                        "sync_status": result.run.status,
                        "owned_count": result.owned_count,
                        "recent_count": result.recent_count,
                        "disclosure_version": ACTIVITY_DISCLOSURE_VERSION,
                    }
                else:
                    result = sync_achievements(
                        storage,
                        account_id=account.id,
                        steamid=account.provider_account_id,
                        api_key=resolved["secret"],
                        scope=args.scope,
                        explicit_appids=tuple(args.appid),
                        max_items=args.max_items,
                        client=_steam_activity_client(),
                        request_gate=request_gate,
                        clock=_utc_now,
                    )
                    data = {
                        "sync_run_id": result.run.id,
                        "sync_status": result.run.status,
                        "candidate_count": result.candidate_count,
                        "targeted_count": result.targeted_count,
                        "state_counts": result.state_counts,
                        "disclosure_version": ACTIVITY_DISCLOSURE_VERSION,
                    }
            except ActivitySyncError as exc:
                return _emit_error(
                    args,
                    command=command,
                    code=exc.code,
                    message="The Steam activity synchronization did not complete.",
                    retryable=exc.retryable,
                )
            except ValueError:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The synchronization arguments are invalid.",
                    exit_code=2,
                )
        return _emit_success(
            args,
            command=command,
            context={"account_alias": account.alias, "identifiers_included": False},
            data=data,
        )


def _dispatch_system(args: argparse.Namespace, database_path: Path) -> int:
    command = _command_name(args)
    machine_id = args.machine
    if (
        not isinstance(machine_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", machine_id) is None
    ):
        return _emit_error(
            args,
            command=command,
            code=ErrorCode.INVALID_ARGUMENT,
            message="The machine alias is invalid.",
            exit_code=2,
        )
    with Storage(database_path) as storage:
        if args.command == "system":
            result = query_system_profile(
                storage, machine_id=machine_id, clock=_utc_now
            )
            snapshot = result["snapshot"]
            latest_status = snapshot["last_attempt_status"]
            warnings: list[WarningRecord] = []
            missing: list[str] = []
            stale: list[str] = []
            status = CompletenessStatus.COMPLETE
            if latest_status is None or result["profile"] is None:
                status = CompletenessStatus.UNAVAILABLE
                missing.append("system_profile.read")
                warnings.append(
                    WarningRecord(
                        code=ErrorCode.NOT_SYNCED,
                        message="A system profile has not been synchronized for this machine.",
                    )
                )
            elif latest_status == "running":
                status = CompletenessStatus.PARTIAL
                warnings.append(
                    WarningRecord(
                        code=ErrorCode.SYNC_IN_PROGRESS,
                        message="System-profile synchronization is in progress; results use the last-good snapshot.",
                    )
                )
            elif latest_status != "complete":
                status = CompletenessStatus.PARTIAL
                stale.append("system_profile.read")
                warnings.append(
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message="The latest system-profile synchronization was incomplete; results use the last-good snapshot.",
                    )
                )
            if result["profile"] is not None and any(
                value == "stale" for value in result["freshness"].values()
            ):
                status = CompletenessStatus.PARTIAL
                if "system_profile.read" not in stale:
                    stale.append("system_profile.read")
                if not any(
                    warning.code == ErrorCode.STALE_LAST_GOOD for warning in warnings
                ):
                    warnings.append(
                        WarningRecord(
                            code=ErrorCode.STALE_LAST_GOOD,
                            message="Some cached system-profile facts are older than their freshness policy.",
                        )
                    )
            return _emit_success(
                args,
                command=command,
                context={"machine_alias": machine_id, "identifiers_included": False},
                completeness_value=completeness(
                    status,
                    missing_capabilities=missing,
                    stale_capabilities=stale,
                    warnings=warnings,
                ),
                data=result,
            )

        detected_machine = machine_for(machine_id)
        normalized_detected_architecture = canonical_architecture(
            detected_machine.architecture
        )
        if (
            detected_machine.architecture is not None
            and normalized_detected_architecture is None
        ):
            return _emit_error(
                args,
                command=command,
                code="MACHINE_ARCHITECTURE_UNSUPPORTED",
                message="The detected machine architecture is not supported by system-profile/0.1.",
            )
        candidate = type(detected_machine)(
            detected_machine.id,
            detected_machine.name,
            detected_machine.platform,
            normalized_detected_architecture,
        )
        existing = storage.get_machine(machine_id)
        if existing is not None and not _machine_profile_identity_matches(
            existing.platform,
            existing.architecture,
            candidate.platform,
            candidate.architecture,
        ):
            return _emit_error(
                args,
                command=command,
                code="MACHINE_PROFILE_CONFLICT",
                message="The machine alias is already associated with another platform or architecture.",
            )
        if existing is None:
            # Create only the explicit alias needed by the foreign key. Hardware
            # facts are not persisted until the disclosure is acknowledged.
            storage.upsert_machine(
                type(candidate)(machine_id, machine_id, "unknown", None),
                observed_at=_utc_now(),
            )
        consent = storage.get_system_profile_consent(machine_id)
        if (
            consent is None
            or consent.disclosure_version != SYSTEM_PROFILE_DISCLOSURE_VERSION
        ):
            if not args.acknowledge_local_storage:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.DATA_POLICY_ACKNOWLEDGMENT_REQUIRED,
                    message=(
                        "A normalized local system profile will store OS, CPU, memory, "
                        "graphics, coarse storage capacity, and conclusive peripheral "
                        "presence facts. It excludes hostname, username, serial numbers, "
                        "UUIDs, MAC/IP addresses, device nodes, labels, and filesystem paths. "
                        "The selected filesystem, replicas, snapshots, and user-controlled "
                        "backups may retain copies after local deletion."
                    ),
                    remediation="Rerun with --acknowledge-local-storage to accept this versioned local-storage policy.",
                )
            storage.record_system_profile_consent(
                machine_id=machine_id,
                disclosure_version=SYSTEM_PROFILE_DISCLOSURE_VERSION,
                accepted_at=_utc_now(),
                backups_acknowledged=True,
            )
        storage.upsert_machine(
            type(candidate)(
                candidate.id,
                candidate.name if existing is None else existing.name,
                candidate.platform,
                candidate.architecture,
            ),
            observed_at=_utc_now(),
        )
        try:
            result = sync_system_profile(
                storage,
                machine_id=machine_id,
                collector=_system_profile_collector,
                clock=_utc_now,
            )
        except SystemProfileError as exc:
            return _emit_error(
                args,
                command=command,
                code=exc.code,
                message="The local system profile could not be collected.",
                retryable=exc.retryable,
            )
        status = (
            CompletenessStatus.COMPLETE
            if result.run.status == "complete"
            else CompletenessStatus.PARTIAL
        )
        warnings = (
            []
            if result.run.status == "complete"
            else [
                WarningRecord(
                    code=result.run.error_code or ErrorCode.PARTIAL_SCAN,
                    message="Required system facts were unavailable; the last-good profile was preserved.",
                )
            ]
        )
        return _emit_success(
            args,
            command=command,
            context={"machine_alias": machine_id, "identifiers_included": False},
            completeness_value=completeness(
                status,
                stale_capabilities=(
                    ["system_profile.read"] if result.run.status != "complete" else []
                ),
                warnings=warnings,
            ),
            data={
                "sync_run_id": result.run.id,
                "sync_status": result.run.status,
                "promoted": result.run.promoted,
                "schema_id": "system-profile/0.1",
                "disclosure_version": SYSTEM_PROFILE_DISCLOSURE_VERSION,
            },
        )


def _system_profile_collector() -> Any:
    return collect_system_profile()


def _machine_profile_identity_matches(
    stored_platform: str,
    stored_architecture: str | None,
    candidate_platform: str,
    candidate_architecture: str | None,
) -> bool:
    platform_matches = stored_platform == "unknown" or (
        stored_platform.casefold() == candidate_platform.casefold()
    )

    def canonical(value: str | None) -> str | None:
        return canonical_architecture(value)

    stored_arch = canonical(stored_architecture)
    candidate_arch = canonical(candidate_architecture)
    return platform_matches and (
        stored_architecture is None
        or candidate_architecture is None
        or (
            stored_arch is not None
            and candidate_arch is not None
            and stored_arch == candidate_arch
        )
    )


def _dispatch_sync_compatibility(args: argparse.Namespace, database_path: Path) -> int:
    app_facts_command = args.sync_command == "app-facts"
    command = "sync.app-facts" if app_facts_command else "sync.compatibility"
    started_at = _utc_now()
    try:
        # Validate the complete provider/scheduler contract before any
        # dependency-specific early return.  Otherwise an unsynchronized owned
        # library could make malformed arguments appear valid.
        if re.fullmatch(r"[A-Za-z]{2}", args.country) is None:
            raise ValueError("country must be an ASCII alpha-2 code")
        country = args.country.upper()
        SteamDeclaredFactsRequestContext(country, args.language)
        supplied_appids = tuple(args.appid)
        if any(
            isinstance(appid, bool)
            or not isinstance(appid, int)
            or not 1 <= appid <= (1 << 32) - 1
            for appid in supplied_appids
        ):
            raise ValueError("AppIDs must be positive uint32 values")
        demanded_selection = tuple(sorted(set(supplied_appids)))
        if app_facts_command and args.scope == "appids" and not demanded_selection:
            raise ValueError("appids scope requires at least one AppID")
        if len(demanded_selection) > 10_000:
            raise ValueError("declared-fact demand exceeds the bounded maximum")
        if (
            isinstance(args.max_items, bool)
            or not isinstance(args.max_items, int)
            or not 1 <= args.max_items <= 100
        ):
            raise ValueError("max-items must be between 1 and 100")
    except ValueError:
        return _emit_error(
            args,
            command=command,
            code=ErrorCode.INVALID_ARGUMENT,
            message="The declared-fact synchronization arguments are invalid.",
            exit_code=2,
        )
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            account = None
        try:
            machine = storage.get_machine(args.machine)
        except ValueError:
            machine = None
        if account is None:
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                message="The requested Steam account is not configured.",
            )
        if machine is None:
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.INVALID_ARGUMENT,
                message="The requested machine is not configured.",
                exit_code=2,
            )
        owned = storage.read_owned_snapshot(account.id)
        if not app_facts_command and owned.latest_complete is None:
            return _emit_success(
                args,
                command=command,
                context={
                    "account_alias": account.alias,
                    "machine_alias": machine.id,
                    "country": country,
                    "language": args.language,
                    "scope": "library",
                    "identifiers_included": False,
                },
                completeness_value=completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["owned.visible.read"],
                    warnings=[
                        WarningRecord(
                            code=ErrorCode.NOT_SYNCED,
                            message=(
                                "A complete visible-owned library snapshot is required "
                                "before compatibility facts can be synchronized."
                            ),
                        )
                    ],
                ),
                data={
                    "sync_run_id": None,
                    "sync_status": None,
                    "items": [],
                    "demand": [],
                    "schema_id": "declared-app-facts/0.2",
                    "support_level": "provisional",
                },
            )
        owned_appids = {game.appid for game in owned.games}
        if app_facts_command:
            wishlist = storage.read_wishlist_snapshot(account.id)
            installed = storage.read_installed_snapshot(machine.id)
            wishlist_appids = {game.appid for game in wishlist.games}
            installed_appids = {game.appid for game in installed.games}
            scope_appids = {
                "library": owned_appids,
                "wishlist": wishlist_appids,
                "installed": installed_appids,
                "known": owned_appids | wishlist_appids | installed_appids,
                "appids": set(),
            }[args.scope]
            demanded_appids = tuple(sorted(scope_appids | set(demanded_selection)))
            owned_dependency_stale = False
        else:
            owned_reference = (
                owned.latest_complete.completed_at or owned.latest_complete.started_at
            )
            owned_age = (
                started_at
                - datetime.fromisoformat(owned_reference.replace("Z", "+00:00"))
            ).total_seconds()
            owned_dependency_stale = (
                owned.latest is None
                or owned.latest.status != "complete"
                or owned_age < 0
                or owned_age > _OWNED_SYNC_FRESHNESS_SECONDS
            )
            demanded_appids = demanded_selection or tuple(sorted(owned_appids))
        if not app_facts_command and any(
            appid not in owned_appids for appid in demanded_appids
        ):
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.INVALID_ARGUMENT,
                message="Every prioritized AppID must be in the cached visible-owned library.",
                exit_code=2,
            )
        if not demanded_appids:
            empty_status = (
                CompletenessStatus.PARTIAL
                if owned_dependency_stale
                else CompletenessStatus.COMPLETE
            )
            empty_warnings = (
                [
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message=(
                            "Compatibility demand came from a stale or superseded "
                            "last-good visible-owned snapshot."
                        ),
                    )
                ]
                if owned_dependency_stale
                else []
            )
            return _emit_success(
                args,
                command=command,
                context={
                    "account_alias": account.alias,
                    "machine_alias": machine.id,
                    "country": country,
                    "language": args.language,
                    "scope": args.scope,
                    "identifiers_included": False,
                },
                completeness_value=completeness(
                    empty_status,
                    stale_capabilities=(
                        ["owned.visible.read"] if owned_dependency_stale else []
                    ),
                    warnings=empty_warnings,
                ),
                data={
                    "sync_run_id": None,
                    "sync_status": "complete",
                    "items": [],
                    "demand": [],
                    "schema_id": "declared-app-facts/0.2",
                    "support_level": "provisional",
                },
            )
        consent = storage.get_compatibility_data_consent(account.id)
        if (
            consent is None
            or consent.disclosure_version != DECLARED_FACTS_DISCLOSURE_VERSION
        ):
            if not args.acknowledge_local_storage:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.DATA_POLICY_ACKNOWLEDGMENT_REQUIRED,
                    message=(
                        "Declared-fact sync stores normalized public platform, "
                        "requirement, language, controller, accessibility-category, "
                        "genre, release-display, multiplayer-category, account-notice, "
                        "and DRM-notice facts for explicitly bounded AppIDs. Raw "
                        "responses and HTML are not retained. Account and machine "
                        "demand lineage is private; backups may retain deleted copies."
                    ),
                    remediation=(
                        "Rerun with --acknowledge-local-storage to accept this "
                        "versioned local-storage policy."
                    ),
                )
            storage.record_compatibility_data_consent(
                account_id=account.id,
                disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
                accepted_at=started_at,
                backups_acknowledged=True,
            )
        try:
            run, candidates, targeted = storage.begin_declared_app_sync(
                account_id=account.id,
                machine_id=machine.id,
                demanded_appids=demanded_appids,
                country=country,
                language=args.language,
                max_items=args.max_items,
                skip_fresh_terminal=True,
                started_at=started_at,
                disclosure_version=DECLARED_FACTS_DISCLOSURE_VERSION,
            )
        except ValueError:
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.INVALID_ARGUMENT,
                message="The declared-fact synchronization arguments are invalid.",
                exit_code=2,
            )
        client = _declared_facts_client()
        for index, appid in enumerate(targeted):
            try:
                requested_at = _utc_now()
                reserved = storage.reserve_provider_request(
                    provider="steam-store-appdetails",
                    budget_scope="global",
                    requested_at=requested_at,
                    minimum_interval_seconds=_PROVIDER_MINIMUM_INTERVAL_SECONDS,
                )
                if not reserved:
                    time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                    requested_at = _utc_now()
                    reserved = storage.reserve_provider_request(
                        provider="steam-store-appdetails",
                        budget_scope="global",
                        requested_at=requested_at,
                        minimum_interval_seconds=_PROVIDER_MINIMUM_INTERVAL_SECONDS,
                    )
            except BaseException:
                _interrupt_declared_sync(storage, run.id)
                raise
            if not reserved:
                storage.mark_remaining_declared_apps_unevaluated(
                    run.id,
                    observed_at=requested_at,
                    error_code="REQUEST_THROTTLED",
                )
                break
            try:
                result = client.fetch(appid, country=country, language=args.language)
            except SteamDeclaredFactsError as exc:
                try:
                    storage.record_declared_app_result(
                        run.id,
                        account_id=account.id,
                        appid=appid,
                        state="failed",
                        error_code=exc.code,
                        observed_at=_utc_now(),
                    )
                except BaseException:
                    _interrupt_declared_sync(storage, run.id)
                    raise
                retry_after = exc.retry_after_seconds
                if exc.code == "RATE_LIMITED" and retry_after is None:
                    retry_after = 300
                if retry_after is not None:
                    storage.defer_provider_requests(
                        provider="steam-store-appdetails",
                        budget_scope="global",
                        requested_at=_utc_now(),
                        retry_after_seconds=retry_after,
                    )
                if index + 1 < len(targeted):
                    storage.mark_remaining_declared_apps_unevaluated(
                        run.id,
                        observed_at=_utc_now(),
                        error_code=(
                            "PROVIDER_COOLDOWN"
                            if exc.code in {"PROVIDER_RESPONSE_INVALID", "RATE_LIMITED"}
                            else exc.code
                        ),
                    )
                break
            except BaseException:
                _interrupt_declared_sync(storage, run.id)
                raise
            try:
                storage.record_declared_app_result(
                    run.id,
                    account_id=account.id,
                    appid=appid,
                    state=result.state,
                    facts=(
                        None
                        if result.facts is None
                        else declared_facts_payload(result.facts)
                    ),
                    observed_at=_utc_now(),
                )
            except BaseException:
                _interrupt_declared_sync(storage, run.id)
                raise
        finished = storage.finish_declared_app_sync(run.id, completed_at=_utc_now())
        snapshot = storage.read_declared_app_snapshot(
            account_id=account.id,
            machine_id=machine.id,
            country=country,
            language=args.language,
            appids=demanded_appids,
        )
    source_gaps = _declared_sync_source_gaps(snapshot)
    missing_declared = any(item["missing_capabilities"] for item in source_gaps)
    stale_declared = any(item["stale_capabilities"] for item in source_gaps)
    available_declared = any(
        item.get("facts") is not None for item in snapshot["items"]
    )
    if missing_declared and not available_declared:
        status = CompletenessStatus.UNAVAILABLE
    elif missing_declared or stale_declared or finished.status != "complete":
        status = CompletenessStatus.PARTIAL
    else:
        status = CompletenessStatus.COMPLETE
    warnings = []
    if finished.status != "complete":
        warnings.append(
            WarningRecord(
                code=finished.error_code or ErrorCode.PARTIAL_SCAN,
                message=(
                    "Some demanded compatibility facts were not refreshed; "
                    "available last-good facts were preserved."
                ),
            )
        )
    elif missing_declared:
        warnings.append(
            WarningRecord(
                code=ErrorCode.PROVIDER_UNAVAILABLE,
                message=(
                    "Normalized declared facts are unavailable for some demanded "
                    "AppIDs; per-AppID source gaps identify them."
                ),
            )
        )
    if owned_dependency_stale:
        if status == CompletenessStatus.COMPLETE:
            status = CompletenessStatus.PARTIAL
        warnings.append(
            WarningRecord(
                code=ErrorCode.STALE_LAST_GOOD,
                message=(
                    "Compatibility demand came from a stale or superseded "
                    "last-good visible-owned snapshot."
                ),
            )
        )
    return _emit_success(
        args,
        command=command,
        context={
            "account_alias": account.alias,
            "machine_alias": machine.id,
            "country": country,
            "language": args.language,
            "scope": args.scope,
            "identifiers_included": True,
        },
        completeness_value=completeness(
            status,
            missing_capabilities=(
                ["compatibility.declared.read"] if missing_declared else []
            ),
            stale_capabilities=(
                sorted(
                    ({"compatibility.declared.read"} if stale_declared else set())
                    | ({"owned.visible.read"} if owned_dependency_stale else set())
                )
            ),
            warnings=warnings,
        ),
        data={
            "sync_run_id": finished.id,
            "sync_status": finished.status,
            "promoted": finished.promoted,
            "error_code": finished.error_code,
            "candidates": list(candidates),
            "targeted": list(targeted),
            "items": list(snapshot["items"]),
            "demand": list(snapshot["latest_demand"]),
            "source_gaps": source_gaps,
            "schema_id": "declared-app-facts/0.2",
            "support_level": "provisional",
            "disclosure_version": DECLARED_FACTS_DISCLOSURE_VERSION,
        },
    )


def _declared_scope_appids(
    storage: Storage,
    *,
    account_id: int,
    machine_id: str,
    scope: str,
    explicit: Sequence[int],
) -> tuple[int, ...]:
    """Authorize one deterministic candidate set without consulting global demand."""

    owned = {game.appid for game in storage.read_owned_snapshot(account_id).games}
    wishlist = {game.appid for game in storage.read_wishlist_snapshot(account_id).games}
    installed = {
        game.appid for game in storage.read_installed_snapshot(machine_id).games
    }
    scoped = {
        "library": owned,
        "wishlist": wishlist,
        "installed": installed,
        "known": owned | wishlist | installed,
        "appids": set(),
    }[scope]
    return tuple(sorted(scoped | set(explicit)))


def _dispatch_discovery(args: argparse.Namespace, database_path: Path) -> int:
    command = "discovery.query"
    try:
        if re.fullmatch(r"[A-Za-z]{2}", args.country) is None:
            raise ValueError("invalid country")
        country = args.country.upper()
        SteamDeclaredFactsRequestContext(country, args.language)
        explicit = tuple(sorted(set(args.appid)))
        if any(not 1 <= appid <= (1 << 32) - 1 for appid in explicit):
            raise ValueError("invalid AppID")
        if args.scope == "appids" and not explicit:
            raise ValueError("appids scope requires AppIDs")
        if not 1 <= args.limit <= 10_000:
            raise ValueError("invalid limit")
        known_modes = frozenset(MULTIPLAYER_CATEGORY_SLUGS.values())
        if args.require_mode is not None and args.require_mode not in known_modes:
            raise ValueError("invalid mode")
    except (TypeError, ValueError):
        return _emit_error(
            args,
            command=command,
            code=ErrorCode.INVALID_ARGUMENT,
            message="The bounded discovery query arguments are invalid.",
            exit_code=2,
        )
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
            machine = storage.get_machine(args.machine)
        except ValueError:
            account = machine = None
        if account is None:
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                message="The requested Steam account is not configured.",
            )
        if machine is None:
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.INVALID_ARGUMENT,
                message="The requested machine is not configured.",
                exit_code=2,
            )
        candidates = _declared_scope_appids(
            storage,
            account_id=account.id,
            machine_id=machine.id,
            scope=args.scope,
            explicit=explicit,
        )
        bounded = candidates[: args.limit]
        if bounded:
            snapshot = storage.read_declared_app_snapshot(
                account_id=account.id,
                machine_id=machine.id,
                country=country,
                language=args.language,
                appids=bounded,
            )
            raw_items = snapshot["items"]
        else:
            snapshot = {"latest": None, "latest_demand": ()}
            raw_items = ()
    items: list[dict[str, Any]] = []
    missing = 0
    for raw in raw_items:
        payload = raw.get("facts")
        if payload is None:
            missing += 1
            items.append(
                {
                    "appid": raw["appid"],
                    "evidence_state": "unknown",
                    "mode_requirement": (
                        None if args.require_mode is None else "unknown"
                    ),
                    "genres": {"state": "unknown", "items": []},
                    "multiplayer_modes": [],
                    "coming_soon": {
                        "state": "unknown",
                        "localized_date_display": None,
                    },
                    "player_count": {"state": "unsupported"},
                    "health": {"state": "unsupported"},
                    "tags_and_mechanics": {"state": "unsupported"},
                }
            )
            continue
        facts = declared_discovery_facts(payload)
        modes = list(facts.multiplayer_modes)
        items.append(
            {
                "appid": raw["appid"],
                "evidence_state": "declared",
                "schema_id": payload["schema_id"],
                "observed_at": raw.get("observed_at"),
                "category_ids": list(facts.category_ids),
                "multiplayer_modes": modes,
                "mode_requirement": (
                    None
                    if args.require_mode is None
                    else ("pass" if args.require_mode in modes else "unknown")
                ),
                "genres": {
                    "state": facts.genres.state,
                    "items": [asdict(item) for item in facts.genres.items],
                },
                "coming_soon": asdict(facts.coming_soon),
                "player_count": {"state": "unsupported"},
                "health": {"state": "unsupported"},
                "tags_and_mechanics": {"state": "unsupported"},
            }
        )
    status = (
        CompletenessStatus.COMPLETE
        if not missing
        else (
            CompletenessStatus.UNAVAILABLE
            if missing == len(items)
            else CompletenessStatus.PARTIAL
        )
    )
    return _emit_success(
        args,
        command=command,
        context={
            "account_alias": account.alias,
            "machine_alias": machine.id,
            "country": country,
            "language": args.language,
            "scope": args.scope,
            "identifiers_included": True,
        },
        completeness_value=completeness(
            status,
            missing_capabilities=(["discovery.declared.read"] if missing else []),
        ),
        data={
            "items": items,
            "candidate_count": len(candidates),
            "returned_count": len(items),
            "truncated": len(candidates) > len(bounded),
            "limit": args.limit,
            "network_used": False,
            "schema_id": "discovery-query/0.1",
        },
    )


def _declared_sync_source_gaps(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Classify each demanded subject without letting one last-good mask another."""

    gaps: list[dict[str, Any]] = []
    for item, demand in zip(snapshot["items"], snapshot["latest_demand"], strict=True):
        missing: list[str] = []
        stale: list[str] = []
        has_facts = item.get("facts") is not None
        if not has_facts:
            missing.append("compatibility.declared.read")
        elif not (
            demand.get("state") == "ready"
            or demand.get("error_code") == "FRESH_LAST_GOOD"
        ):
            # A newer unresolved, failed, or negative observation supersedes
            # the freshness claim while retaining usable last-good facts.
            stale.append("compatibility.declared.read")
        gaps.append(
            {
                "appid": int(item["appid"]),
                "missing_capabilities": missing,
                "stale_capabilities": stale,
            }
        )
    return gaps


def _dispatch_compatibility(args: argparse.Namespace, database_path: Path) -> int:
    """Assess one atomic cache snapshot without provider or client access."""

    command = "compatibility.assess"
    now = _utc_now()
    # Validate the caller-controlled contract before touching cached evidence so
    # request mistakes remain distinct from malformed persisted projections.
    try:
        if re.fullmatch(r"[A-Za-z]{2}", args.country) is None:
            raise ValueError("country must be an ASCII alpha-2 code")
        country = args.country.upper()
        supplied_appids = tuple(args.appids)
        if not supplied_appids or any(
            isinstance(appid, bool)
            or not isinstance(appid, int)
            or not 1 <= appid <= (1 << 32) - 1
            for appid in supplied_appids
        ):
            raise ValueError("AppIDs must be positive uint32 values")
        # The CLI has historically treated repeated positional AppIDs as one
        # requested subject.  Normalize that surface before applying the pure
        # engine request envelope.
        appids = tuple(sorted(set(supplied_appids)))
        if len(appids) > MAX_DECLARED_APP_DEMAND:
            raise ValueError("compatibility query exceeds the bounded AppID maximum")
        # Compatibility facts use the same closed request-context vocabulary
        # as their provider adapter.  Accepting an arbitrary language slug here
        # would create a cache key the sync boundary can never populate.
        SteamDeclaredFactsRequestContext(country, args.language)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", args.account) is None:
            raise ValueError("account alias is invalid")
        _validate_compatibility_target_syntax(args.target, args.context_machine)
        requirements = tuple(_compatibility_requirement(item) for item in args.require)
        overrides = tuple(
            _compatibility_override(item, requested=set(appids), applied_at=now)
            for item in args.override
        )
        override_keys = tuple((item.appid, item.gate) for item in overrides)
        if len(override_keys) != len(set(override_keys)):
            raise ValueError("compatibility overrides must be unique")
        validate_compatibility_request(
            appids,
            requirements,
            overrides,
            candidate_gate_capacity=2 * MAX_COMPONENTS,
        )
    except ValueError:
        return _emit_error(
            args,
            command=command,
            code=ErrorCode.INVALID_ARGUMENT,
            message="The compatibility assessment arguments are invalid.",
            remediation=(
                "Use valid AppIDs, an explicit configured account/target, country, "
                "language, and documented requirement/override expressions."
            ),
            exit_code=2,
        )

    if not database_path.is_file() and not database_path.is_symlink():
        return _emit_error(
            args,
            command=command,
            code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
            message="The requested account alias is not configured.",
        )
    try:
        with Storage(database_path, readonly=True) as storage:
            account = storage.get_account(args.account)
            if account is None:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            try:
                target, machine_id = _compatibility_target(
                    storage, args.target, args.context_machine
                )
            except ValueError:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The compatibility assessment target is invalid.",
                    remediation=(
                        "Use a configured machine target, or provide an explicit "
                        "configured evidence context for a Steam Deck target."
                    ),
                    exit_code=2,
                )
            snapshot = storage.read_compatibility_snapshot(
                account.id,
                machine_id,
                country,
                args.language,
                appids,
                now,
                include_local_target_evidence=(target.kind == "machine"),
            )
        # Reconstruct once without caller overrides.  That keeps malformed
        # persisted evidence classified as DATABASE_ERROR while giving us the
        # exact evaluated gate set against which to validate request overrides.
        query_without_overrides = assess_compatibility_snapshot(
            snapshot,
            target=target,
            requirements=requirements,
            overrides=(),
        )
    except (sqlite3.DatabaseError, StorageError):
        return _emit_error(
            args,
            command=command,
            code=ErrorCode.DATABASE_ERROR,
            message=(
                "The local data store is unavailable for a cache-only "
                "compatibility assessment."
            ),
            remediation=(
                "Run a writable steam-agent command with the current version "
                "to apply required database migrations and checkpoint pending "
                "writes, then retry the assessment."
            ),
        )
    except ValueError:
        return _emit_error(
            args,
            command=command,
            code=ErrorCode.DATABASE_ERROR,
            message="Cached compatibility evidence is malformed or inconsistent.",
            remediation=(
                "Resync the system profile and declared compatibility facts with "
                "the current steam-agent version, then retry the assessment."
            ),
        )

    evaluated_gates = {
        (result.appid, gate.name)
        for result in query_without_overrides.assessment.results
        for gate in result.gates
    }
    if any((item.appid, item.gate) not in evaluated_gates for item in overrides):
        return _emit_error(
            args,
            command=command,
            code=ErrorCode.INVALID_ARGUMENT,
            message="A compatibility override names a gate that was not evaluated.",
            remediation=(
                "Use --explain without overrides to inspect the gates evaluated "
                "for each requested AppID, then name one of those exact gates."
            ),
            exit_code=2,
        )
    if overrides:
        try:
            query = assess_compatibility_snapshot(
                snapshot,
                target=target,
                requirements=requirements,
                overrides=overrides,
            )
        except ValueError:
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.INVALID_ARGUMENT,
                message="A compatibility override is invalid for this assessment.",
                remediation=(
                    "Use --explain without overrides to inspect the evaluated "
                    "gates, then provide unique documented override expressions."
                ),
                exit_code=2,
            )
    else:
        query = query_without_overrides

    # The process envelope reports completeness for the active M5 query
    # contract.  Keep future ready/operations evidence and Deck-inapplicable
    # local evidence explicit in data.source_completeness without presenting
    # either as a failed M5 synchronization.
    envelope_inapplicable = {"operations.ready.read"}
    if target.kind == "valve_deck":
        envelope_inapplicable.update({"system_profile.read", "library.installed.read"})
    missing = tuple(
        capability
        for capability in query.completeness.missing_capabilities
        if capability not in envelope_inapplicable
    )
    stale = tuple(
        capability
        for capability in query.completeness.stale_capabilities
        if capability not in envelope_inapplicable
    )
    warnings_out: list[WarningRecord] = []
    if missing:
        warnings_out.append(
            WarningRecord(
                code=ErrorCode.NOT_SYNCED,
                message="Some compatibility or ready-now evidence is unavailable.",
            )
        )
    if stale:
        warnings_out.append(
            WarningRecord(
                code=ErrorCode.STALE_LAST_GOOD,
                message="Some compatibility evidence is stale or superseded.",
            )
        )
    completeness_value = completeness(
        CompletenessStatus.PARTIAL
        if (missing or stale)
        else CompletenessStatus.COMPLETE,
        missing_capabilities=missing,
        stale_capabilities=stale,
        warnings=warnings_out,
    )
    return _emit_success(
        args,
        command=command,
        generated_at=query.generated_at,
        context={
            "account_alias": args.account,
            "target": args.target,
            "evidence_machine": machine_id,
            "country": country,
            "language": args.language,
            "cache_only": True,
            "requirements": [f"{item.kind}:{item.name}" for item in requirements],
            "overrides_ephemeral": bool(overrides),
        },
        completeness_value=completeness_value,
        data=compatibility_query_data(query, explain=args.explain),
    )


def _compatibility_target(
    storage: Storage, raw: str, context_machine: str | None
) -> tuple[CompatibilityTarget, str]:
    if raw == "valve:steam-deck":
        # Declared facts are global, while attempt lineage remains scoped to an
        # account and machine.  A sole configured machine is unambiguous; a
        # multi-machine store must name the desired evidence context.
        machine_id = context_machine
        if machine_id is None:
            machines = storage.list_machines()
            if len(machines) == 1:
                machine_id = machines[0].id
            else:
                raise ValueError("Deck assessment requires an explicit machine context")
        if storage.get_machine(machine_id) is None:
            raise ValueError("compatibility context machine is not configured")
        return CompatibilityTarget("valve_deck", "steam-deck", "steamos"), machine_id
    if not raw.startswith("machine:"):
        raise ValueError("target is invalid")
    machine_id = raw.removeprefix("machine:")
    if context_machine is not None and context_machine != machine_id:
        raise ValueError("machine target and evidence context must match")
    machine = storage.get_machine(machine_id)
    if machine is None:
        raise ValueError("compatibility machine is not configured")
    platform_name = {
        "darwin": "macos",
        "mac": "macos",
        "macos": "macos",
        "win32": "windows",
        "windows": "windows",
        "linux": "linux",
    }.get(machine.platform.casefold())
    if platform_name is None:
        raise ValueError("machine platform is unsupported")
    return (
        CompatibilityTarget("machine", machine_id, platform_name),  # type: ignore[arg-type]
        machine_id,
    )


def _validate_compatibility_target_syntax(
    raw: str, context_machine: str | None
) -> None:
    """Validate target identifiers without consulting configured machines."""

    if context_machine is not None:
        CompatibilityTarget("machine", context_machine, "linux")
    if raw == "valve:steam-deck":
        return
    if not isinstance(raw, str) or not raw.startswith("machine:"):
        raise ValueError("target is invalid")
    machine_id = raw.removeprefix("machine:")
    CompatibilityTarget("machine", machine_id, "linux")
    if context_machine is not None and context_machine != machine_id:
        raise ValueError("machine target and evidence context must match")


def _compatibility_requirement(raw: str) -> FeatureRequirement:
    if not isinstance(raw, str) or raw.count(":") != 1:
        raise ValueError("compatibility requirement is invalid")
    kind, name = raw.split(":", 1)
    return FeatureRequirement(kind, name)  # type: ignore[arg-type]


def _compatibility_override(
    raw: str, *, requested: set[int], applied_at: datetime
) -> CompatibilityGateOverride:
    if not isinstance(raw, str):
        raise ValueError("compatibility override is invalid")
    pieces = raw.split(":", 2)
    if len(pieces) != 3 or "=" not in pieces[2]:
        raise ValueError("compatibility override is invalid")
    appid_text, name, gate_state = pieces
    gate, state = gate_state.rsplit("=", 1)
    if not appid_text.isascii() or not appid_text.isdecimal():
        raise ValueError("compatibility override AppID is invalid")
    appid = int(appid_text)
    if appid not in requested:
        raise ValueError("compatibility override AppID was not requested")
    lineage = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return CompatibilityGateOverride(
        name=name,
        appid=appid,
        gate=gate,
        effective=state,  # type: ignore[arg-type]
        evidence_ids=(f"query-override:{lineage}",),
        applied_at=applied_at,
    )


def _declared_facts_client() -> SteamDeclaredFactsClient:
    return SteamDeclaredFactsClient()


def _interrupt_declared_sync(storage: Storage, sync_run_id: int) -> None:
    """Best-effort cleanup without replacing the caller's BaseException."""

    try:
        storage.mark_remaining_declared_apps_unevaluated(
            sync_run_id,
            observed_at=_utc_now(),
            error_code="SYNC_INTERRUPTED",
        )
        if storage.get_sync_run(sync_run_id).status == "running":
            storage.finish_declared_app_sync(sync_run_id, completed_at=_utc_now())
    except BaseException:
        pass


def _dispatch_deals_query(args: argparse.Namespace, database_path: Path) -> int:
    country = args.country.upper()
    if (
        len(country) != 2
        or not country.isascii()
        or not country.isalpha()
        or country != "US"
    ):
        return _emit_error(
            args,
            command="deals.query",
            code=(
                "UNSUPPORTED_COUNTRY"
                if len(country) == 2 and country.isascii() and country.isalpha()
                else ErrorCode.INVALID_ARGUMENT
            ),
            message=(
                "Cached GG.deals and CheapShark evidence is currently US/USD only."
                if len(country) == 2 and country.isascii() and country.isalpha()
                else "Country must be a two-letter code."
            ),
            exit_code=2,
        )
    generated_at = _utc_now()
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command="deals.query",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            return _emit_success(
                args,
                command="deals.query",
                generated_at=generated_at,
                context={
                    "account_alias": args.account,
                    "scopes": ["wishlist", "deals"],
                    "country": country,
                    "currency": "USD",
                    "store_class": args.store_class,
                    "identifiers_included": False,
                },
                completeness_value=completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["account.identity"],
                    warnings=[
                        WarningRecord(
                            code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                            message="The requested account alias is not configured.",
                        )
                    ],
                ),
                data={
                    "items": [],
                    "empty": False,
                    "next_cursor": None,
                    "ranking": {
                        "schema": "deal-evidence/0.1",
                        "deterministic_order": True,
                    },
                    "snapshots": {"wishlist": None, "prices": None},
                    "fallback": {
                        "ladder": [
                            {"rung": 0, "provider": "gg-deals", "mode": "api"},
                            {
                                "rung": 1,
                                "provider": "cheapshark",
                                "mode": "api",
                            },
                            {
                                "rung": 2,
                                "provider": "manual-reference",
                                "mode": "manual_only",
                            },
                        ],
                        "providers_attempted": [],
                        "providers_used": [],
                    },
                    "limitations": [
                        "price evidence is US/USD only",
                        "historical lows are summaries rather than a full event graph",
                        "manual references are never read or fetched by Steam Agent",
                    ],
                },
            )
        spec = _CREDENTIAL_PROVIDERS["gg-deals"]
        credential_ref = _provider_credential_ref(database_path, spec)
        gg_configured = (
            storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            is not None
        )
        snapshot = storage.read_wishlist_deal_snapshot(
            account_id=account.id,
            country=country,
            now=generated_at,
        )
    result = build_deal_query_from_snapshot(
        snapshot,
        account_alias=account.alias,
        country=country,
        store_class=args.store_class,
        generated_at=generated_at,
        gg_credential_configured=gg_configured,
    )
    return _emit_success(
        args,
        command="deals.query",
        generated_at=generated_at,
        context=result["context"],  # type: ignore[arg-type]
        completeness_value=result["completeness"],  # type: ignore[arg-type]
        data=result["data"],  # type: ignore[arg-type]
    )


def _dispatch_sync_reviews(args: argparse.Namespace, database_path: Path) -> int:
    command = "sync.reviews"
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                message="The requested Steam account is not configured.",
            )
        consent = storage.get_review_data_consent(account.id)
        if consent is None or consent.disclosure_version != REVIEW_DISCLOSURE_VERSION:
            if not args.acknowledge_local_storage:
                return _emit_error(
                    args,
                    command=command,
                    code=ErrorCode.DATA_POLICY_ACKNOWLEDGMENT_REQUIRED,
                    message=(
                        "Review sync stores public normalized aggregate counts, "
                        "request context, account-scoped demand, and coarse attempt "
                        "lineage for up to seven days. Review text, reviewers, and raw "
                        "responses are not retained. Backups may retain deleted copies."
                    ),
                    remediation="Rerun with --acknowledge-local-storage.",
                )
            storage.record_review_data_consent(
                account_id=account.id,
                disclosure_version=REVIEW_DISCLOSURE_VERSION,
                accepted_at=_utc_now(),
                backups_acknowledged=True,
            )
        try:
            result = sync_wishlist_reviews(
                storage,
                account_id=account.id,
                max_items=args.max_items,
                client=_steam_review_client(),
                clock=_utc_now,
            )
        except ReviewSyncError as exc:
            return _emit_error(
                args,
                command=command,
                code=exc.code,
                message="Steam aggregate review synchronization stopped early.",
                retryable=exc.retryable,
            )
        except (ValueError, StorageError):
            return _emit_error(
                args,
                command=command,
                code=ErrorCode.INVALID_ARGUMENT,
                message="Review synchronization requires a synchronized, disclosed wishlist.",
                exit_code=2,
            )
    return _emit_success(
        args,
        command=command,
        context={
            "account_alias": account.alias,
            "scope": "wishlist",
            "provider": "steam_store",
            "identifiers_included": False,
        },
        data={
            "sync_run_id": result.run.id,
            "sync_status": result.run.status,
            "candidate_count": result.candidate_count,
            "targeted_count": result.targeted_count,
            "state_counts": result.state_counts,
            "request_context": {
                "filter": "all",
                "language": "all",
                "day_range": 365,
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": 1,
                "off_topic_activity_filtered": True,
            },
            "review_text_retained": False,
            "reviewer_data_retained": False,
            "raw_response_retained": False,
        },
    )


_WISHLIST_OVERRIDE = re.compile(
    r"appid:([1-9][0-9]{0,9}):((?:explicit_hard_exclude|active_snooze|hard_(?:avoid|require):user:[a-z0-9][a-z0-9._-]{0,58}))\Z"
)


def _wishlist_overrides(values: list[str]) -> tuple[GateOverride, ...]:
    if len(values) > 64:
        raise ValueError("too many wishlist overrides")
    result: list[GateOverride] = []
    for index, expression in enumerate(values, start=1):
        match = _WISHLIST_OVERRIDE.fullmatch(expression)
        if match is None or int(match.group(1)) > (1 << 32) - 1:
            raise ValueError("wishlist override is invalid")
        result.append(
            GateOverride(
                f"override:cli-{index}",
                int(match.group(1)),
                match.group(2),
                (f"request:override-{index}",),
            )
        )
    return tuple(result)


def _dispatch_wishlist_recommendations(
    args: argparse.Namespace, database_path: Path
) -> int:
    generated_at = _utc_now().astimezone(timezone.utc).replace(microsecond=0)
    country = args.country.upper()
    if country != "US":
        return _emit_error(
            args,
            command="recommendations.wishlist",
            code="UNSUPPORTED_COUNTRY",
            message="Cached wishlist deal evidence is currently US/USD only.",
            exit_code=2,
        )
    try:
        overrides = _wishlist_overrides(args.override)
    except ValueError:
        return _emit_error(
            args,
            command="recommendations.wishlist",
            code=ErrorCode.INVALID_ARGUMENT,
            message="A wishlist recommendation override is invalid.",
            exit_code=2,
        )
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command="recommendations.wishlist",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            return _emit_success(
                args,
                command="recommendations.wishlist",
                generated_at=generated_at,
                context={
                    "account_alias": args.account,
                    "recipe": "wishlist-fit/0.1",
                    "identifiers_included": False,
                },
                completeness_value=completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["account.identity"],
                ),
                data={
                    "recipe": "wishlist-fit/0.1",
                    "ranked": [],
                    "excluded": [],
                    "purchase_recommendation_supported": False,
                    "empty": False,
                    "next_cursor": None,
                },
            )
        spec = _CREDENTIAL_PROVIDERS["gg-deals"]
        credential_ref = _provider_credential_ref(database_path, spec)
        gg_configured = (
            storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            is not None
        )
        snapshot = storage.read_wishlist_recommendation_snapshot(
            account_id=account.id, country=country, now=generated_at
        )
    try:
        result = build_wishlist_recommendation_query(
            snapshot,
            account_alias=account.alias,
            country=country,
            store_class=args.store_class,
            unknown_policy=args.unknown,
            overrides=overrides,
            generated_at=generated_at,
            gg_credential_configured=gg_configured,
        )
    except ValueError:
        return _emit_error(
            args,
            command="recommendations.wishlist",
            code=ErrorCode.INVALID_ARGUMENT,
            message="The wishlist recommendation request conflicts with cached evidence.",
            exit_code=2,
        )
    return _emit_success(
        args,
        command="recommendations.wishlist",
        generated_at=generated_at,
        context=result["context"],  # type: ignore[arg-type]
        completeness_value=result["completeness"],  # type: ignore[arg-type]
        data=result["data"],  # type: ignore[arg-type]
    )


_RECOMMEND_REQUIREMENT = re.compile(
    r"(installed|user:[a-z0-9](?:[a-z0-9._-]{0,57}[a-z0-9])?)=(true|false)\Z"
)
_RECOMMEND_OVERRIDE = re.compile(
    r"appid:([1-9][0-9]{0,9}):([a-z][a-z0-9:._-]{0,126})=(pass|fail|unknown)\Z"
)
_MAX_RECOMMEND_FILTERS = 32


def _seconds_old(value: str, now: datetime) -> float:
    observed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    return (now - observed).total_seconds()


def _recommendation_filters(
    args: argparse.Namespace,
) -> tuple[tuple[Requirement, ...], tuple[ConstraintOverride, ...]]:
    if args.time_minutes is not None and not 0 <= args.time_minutes <= (1 << 32) - 1:
        raise ValueError("time-minutes is out of range")
    if (
        len(args.require) > _MAX_RECOMMEND_FILTERS
        or len(args.override) > _MAX_RECOMMEND_FILTERS
    ):
        raise ValueError("too many recommendation filters")
    requirements: list[Requirement] = []
    for expression in args.require:
        if (
            len(expression) > 128
            or (match := _RECOMMEND_REQUIREMENT.fullmatch(expression)) is None
        ):
            raise ValueError("requirement expression is invalid")
        requirements.append(Requirement(match.group(1), match.group(2) == "true"))
    overrides: list[ConstraintOverride] = []
    for expression in args.override:
        if (
            len(expression) > 160
            or (match := _RECOMMEND_OVERRIDE.fullmatch(expression)) is None
        ):
            raise ValueError("override expression is invalid")
        appid = int(match.group(1))
        if appid > (1 << 32) - 1:
            raise ValueError("override AppID is out of range")
        overrides.append(ConstraintOverride(appid, match.group(2), match.group(3)))
    if len({item.name for item in requirements}) != len(requirements):
        raise ValueError("duplicate requirements are invalid")
    if len({(item.appid, item.constraint) for item in overrides}) != len(overrides):
        raise ValueError("duplicate overrides are invalid")
    return tuple(requirements), tuple(overrides)


def _dispatch_recommendations_query(
    args: argparse.Namespace, database_path: Path
) -> int:
    generated_at = _utc_now().astimezone(timezone.utc).replace(microsecond=0)
    if (
        not isinstance(args.machine, str)
        or not 1 <= len(args.machine) <= 256
        or any(ord(character) < 32 for character in args.machine)
    ):
        return _emit_error(
            args,
            command="recommendations.query",
            code=ErrorCode.INVALID_ARGUMENT,
            message="The recommendation query arguments are invalid.",
            exit_code=2,
        )
    try:
        requirements, overrides = _recommendation_filters(args)
    except ValueError:
        return _emit_error(
            args,
            command="recommendations.query",
            code=ErrorCode.INVALID_ARGUMENT,
            message="The recommendation query arguments are invalid.",
            exit_code=2,
        )
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command="recommendations.query",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            return _emit_success(
                args,
                command="recommendations.query",
                generated_at=generated_at,
                context={
                    "account_alias": args.account,
                    "machine_id": args.machine,
                    "scopes": ["owned"],
                    "recipe": args.recipe,
                    "identifiers_included": False,
                },
                completeness_value=completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["account.identity"],
                    warnings=[
                        WarningRecord(
                            ErrorCode.ACCOUNT_NOT_CONFIGURED,
                            "The requested account alias is not configured.",
                        )
                    ],
                ),
                data={
                    "schema": "recommendations/0.1",
                    "recipe_version": args.recipe,
                    "results": [],
                    "eligible": [],
                    "conditional": [],
                    "excluded": [],
                    "counts": {
                        "eligible": 0,
                        "conditional": 0,
                        "excluded": 0,
                        "total": 0,
                    },
                    "empty": False,
                },
            )
        snapshot = storage.read_recommendation_snapshot(
            account.id, args.machine, now=generated_at
        )
    if snapshot.owned.latest_complete is None:
        return _emit_success(
            args,
            command="recommendations.query",
            generated_at=generated_at,
            context={
                "account_alias": account.alias,
                "machine_id": args.machine,
                "scopes": ["owned"],
                "recipe": args.recipe,
                "identifiers_included": False,
            },
            completeness_value=completeness(
                CompletenessStatus.UNAVAILABLE,
                missing_capabilities=["owned.visible.read"],
                warnings=[
                    WarningRecord(
                        ErrorCode.NOT_SYNCED,
                        "Visible-owned games have not been synchronized.",
                    )
                ],
            ),
            data={
                "schema": "recommendations/0.1",
                "recipe_version": args.recipe,
                "results": [],
                "eligible": [],
                "conditional": [],
                "excluded": [],
                "counts": {"eligible": 0, "conditional": 0, "excluded": 0, "total": 0},
                "empty": False,
            },
        )
    try:
        ranking = build_recommendation_query(
            snapshot,
            recipe=args.recipe,
            now=generated_at,
            time_minutes=args.time_minutes,
            requirements=requirements,
            unknown_policy=args.unknown,
            overrides=overrides,
            explain=args.explain,
        )
    except ValueError:
        return _emit_error(
            args,
            command="recommendations.query",
            code=ErrorCode.INVALID_ARGUMENT,
            message="A recommendation constraint or override is invalid for this candidate set.",
            exit_code=2,
        )
    missing: set[str] = set()
    stale: set[str] = set()
    warnings: list[WarningRecord] = []
    owned_appids = {item.appid for item in snapshot.owned.games}
    classified = {
        fact.appid: fact
        for fact in snapshot.catalog.facts
        if fact.appid in owned_appids
    }
    if owned_appids and (
        set(classified) != owned_appids
        or any(value.classification == "not_observed" for value in classified.values())
    ):
        missing.add("catalog.classification")
    if any(
        _seconds_old(fact.observed_at, generated_at) > _CATALOG_SYNC_FRESHNESS_SECONDS
        or _seconds_old(fact.observed_at, generated_at) < 0
        for fact in classified.values()
    ):
        stale.add("catalog.classification")
    attempted_catalog_appids = {
        appid for attempt in snapshot.catalog.attempts for appid in attempt.appids
    }
    if owned_appids - attempted_catalog_appids:
        missing.add("catalog.application.read")
    for attempt in snapshot.catalog.attempts:
        run = attempt.run
        if run.status in {"failed", "partial"}:
            stale.add("catalog.application.read")
            warnings.append(
                WarningRecord(
                    ErrorCode.STALE_LAST_GOOD,
                    "A relevant catalog refresh did not replace its last-good classification.",
                )
            )
        elif run.status == "running":
            abandoned = (
                _seconds_old(run.started_at, generated_at) > _SYNC_ABANDONED_SECONDS
            )
            if abandoned:
                stale.add("catalog.application.read")
            warnings.append(
                WarningRecord(
                    ErrorCode.SYNC_ABANDONED
                    if abandoned
                    else ErrorCode.SYNC_IN_PROGRESS,
                    "A relevant catalog refresh appears abandoned."
                    if abandoned
                    else "A relevant catalog refresh is in progress.",
                )
            )
    if (
        owned_appids
        and any(item.name == "installed" for item in requirements)
        and snapshot.installed.latest_complete is None
    ):
        missing.add("installed.read")
    if (
        owned_appids
        and args.recipe in {"resume/0.1", "preference-fit/0.1"}
        and snapshot.activity_latest_complete is None
    ):
        missing.add("activity.read")
    if (
        owned_appids
        and args.recipe in {"resume/0.1", "finishability/0.1"}
        and snapshot.achievement_latest is None
    ):
        missing.add("achievements.read")
    owned_good = snapshot.owned.latest_complete
    if owned_good is not None:
        owned_age = _seconds_old(
            owned_good.completed_at or owned_good.started_at, generated_at
        )
        if owned_age > _OWNED_SYNC_FRESHNESS_SECONDS or owned_age < 0:
            stale.add("owned.visible.read")
    for latest, capability, relevant in (
        (snapshot.owned.latest, "owned.visible.read", True),
        (
            snapshot.activity_latest,
            "activity.read",
            args.recipe in {"resume/0.1", "preference-fit/0.1"},
        ),
        (
            snapshot.installed.latest,
            "installed.read",
            any(item.name == "installed" for item in requirements),
        ),
    ):
        if latest is None or not relevant or latest.status == "complete":
            continue
        if latest.status == "running":
            abandoned = (
                _seconds_old(latest.started_at, generated_at) > _SYNC_ABANDONED_SECONDS
            )
            if abandoned:
                stale.add(capability)
            warnings.append(
                WarningRecord(
                    ErrorCode.SYNC_ABANDONED
                    if abandoned
                    else ErrorCode.SYNC_IN_PROGRESS,
                    f"The {capability} refresh appears abandoned."
                    if abandoned
                    else f"The {capability} refresh is in progress.",
                )
            )
        else:
            stale.add(capability)
            warnings.append(
                WarningRecord(
                    ErrorCode.STALE_LAST_GOOD,
                    f"The {capability} refresh did not replace its last-good snapshot.",
                )
            )
    components = [
        component for item in ranking["results"] for component in item["components"]
    ]
    if any(
        component["evidence_kind"] == "behavioral"
        and component["state"] in {"stale", "expired"}
        for component in components
    ):
        stale.add("activity.read")
    if any(
        component["evidence_kind"] == "achievement"
        and component["state"] in {"stale", "expired"}
        for component in components
    ):
        stale.add("achievements.read")
    status = CompletenessStatus.COMPLETE
    if ranking["completeness"] == "partial" or missing or stale:
        status = CompletenessStatus.PARTIAL
    ranking["empty"] = len(snapshot.owned.games) == 0
    ranking["snapshots"] = {
        "owned_last_attempt_status": snapshot.owned.latest.status
        if snapshot.owned.latest
        else None,
        "owned_last_successful_sync_at": snapshot.owned.latest_complete.completed_at,
        "installed_last_attempt_status": snapshot.installed.latest.status
        if snapshot.installed.latest
        else None,
        "activity_last_attempt_status": snapshot.activity_latest.status
        if snapshot.activity_latest
        else None,
        "achievements_last_attempt_status": snapshot.achievement_latest.status
        if snapshot.achievement_latest
        else None,
    }
    return _emit_success(
        args,
        command="recommendations.query",
        generated_at=generated_at,
        context={
            "account_alias": account.alias,
            "machine_id": args.machine,
            "scopes": ["owned"],
            "recipe": args.recipe,
            "unknown_policy": args.unknown,
            "time_minutes": args.time_minutes,
            "identifiers_included": False,
            "cache_only": True,
        },
        completeness_value=completeness(
            status,
            missing_capabilities=sorted(missing),
            stale_capabilities=sorted(stale),
            warnings=warnings,
        ),
        data=ranking,
    )


def _dispatch_sync_owned(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        credential_ref = _steam_credential_ref(database_path)
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            if account is None:
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            consent = storage.get_owned_data_consent(account.id)
            if (
                consent is None
                or consent.disclosure_version != OWNED_DISCLOSURE_VERSION
            ):
                if not args.acknowledge_local_storage:
                    return _emit_error(
                        args,
                        command="sync.owned",
                        code=ErrorCode.DATA_POLICY_ACKNOWLEDGMENT_REQUIRED,
                        message=(
                            "Valve data is stored as-is in the selected local data "
                            "directory: AppID, optional name, lifetime playtime, "
                            "inclusion basis, provenance, and coarse sync metadata. "
                            "Visible-owned is not complete license truth; individually "
                            "private games and unplayed free entitlements may be omitted, "
                            "and sequential request differences may reflect a concurrent "
                            "library change. Storage countries follow the device, selected "
                            "filesystem, replicas, and user-controlled backups. Account "
                            "deletion preserves the shared key; all-provider deletion "
                            "removes its local key/reference but does not revoke it at "
                            "Valve. SQLite secure deletion cannot erase external backups, "
                            "snapshots, journals, or storage-media remapping."
                        ),
                        remediation=(
                            "Rerun with --acknowledge-local-storage to accept this "
                            "versioned local-storage policy."
                        ),
                    )
                storage.record_owned_data_consent(
                    account_id=account.id,
                    disclosure_version=OWNED_DISCLOSURE_VERSION,
                    accepted_at=_utc_now(),
                    backups_acknowledged=True,
                )
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            if metadata is None:
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=ErrorCode.AUTH_REQUIRED,
                    message="A Steam Web API user key has not been configured.",
                )
            resolved = _resolve_credential(metadata, credential_ref)
            if resolved["state"] != "configured":
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=_credential_error_code(resolved["state"]),
                    message="The Steam Web API credential is unavailable.",
                )

            def request_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "steam-web-api",
                        _utc_now(),
                        _PROVIDER_MINIMUM_INTERVAL_SECONDS,
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise OwnedSyncError("REQUEST_THROTTLED", retryable=True)

            try:
                result = sync_owned(
                    storage,
                    account_id=account.id,
                    steamid=account.provider_account_id,
                    api_key=resolved["secret"],
                    client=_steam_web_api_client(),
                    request_gate=request_gate,
                    clock=_utc_now,
                )
            except OwnedSyncError as exc:
                return _emit_error(
                    args,
                    command="sync.owned",
                    code=exc.code,
                    message="The visible-owned synchronization did not complete.",
                    retryable=exc.retryable,
                )
        return _emit_success(
            args,
            command="sync.owned",
            context={"account_alias": account.alias, "identifiers_included": False},
            data={
                "sync_run_id": result.run.id,
                "sync_status": result.run.status,
                "records_seen": result.run.records_seen,
                "visible_owned_count": result.visible_owned_count,
                "played_free_count": result.played_free_count,
                "disclosure_version": OWNED_DISCLOSURE_VERSION,
                "limitations": [
                    "individually_private_games_may_be_omitted",
                    "unplayed_free_entitlements_are_not_complete",
                    "sequential_request_difference_may_reflect_concurrent_library_change",
                ],
            },
        )


def _credential_error_code(state: str) -> str:
    return {
        "missing": str(ErrorCode.CREDENTIAL_NOT_FOUND),
        "store_locked": str(ErrorCode.CREDENTIAL_STORE_LOCKED),
        "store_unavailable": str(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE),
    }.get(state, str(ErrorCode.CREDENTIAL_READ_FAILED))


def _dispatch_sync_wishlist(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        credential_ref = _steam_credential_ref(database_path)
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            if account is None:
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            consent = storage.get_wishlist_data_consent(account.id)
            if (
                consent is None
                or consent.disclosure_version != WISHLIST_DISCLOSURE_VERSION
            ):
                if not args.acknowledge_local_storage:
                    return _emit_error(
                        args,
                        command="sync.wishlist",
                        code=ErrorCode.DATA_POLICY_ACKNOWLEDGMENT_REQUIRED,
                        message=(
                            "The provisional Steam wishlist stores one last-good local "
                            "projection containing AppID, priority, date added, "
                            "provenance, and coarse sync metadata. It retains no raw "
                            "response body. An inaccessible or authentication-like "
                            "empty response cannot establish an empty wishlist. "
                            "Account deletion removes this projection; external backups "
                            "and storage snapshots remain user-controlled."
                        ),
                        remediation=(
                            "Rerun with --acknowledge-local-storage to accept this "
                            "versioned local-storage policy."
                        ),
                    )
                storage.record_wishlist_data_consent(
                    account_id=account.id,
                    disclosure_version=WISHLIST_DISCLOSURE_VERSION,
                    accepted_at=_utc_now(),
                    backups_acknowledged=True,
                )
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            if metadata is None:
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=ErrorCode.AUTH_REQUIRED,
                    message="A Steam Web API user key has not been configured.",
                )
            resolved = _resolve_credential(metadata, credential_ref)
            if resolved["state"] != "configured":
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=_credential_error_code(resolved["state"]),
                    message="The Steam Web API credential is unavailable.",
                )

            def request_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "steam-web-api",
                        _utc_now(),
                        _PROVIDER_MINIMUM_INTERVAL_SECONDS,
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise WishlistSyncError("REQUEST_THROTTLED", retryable=True)

            try:
                result = sync_wishlist(
                    storage,
                    account_id=account.id,
                    steamid=account.provider_account_id,
                    api_key=resolved["secret"],
                    client=_steam_wishlist_client(),
                    request_gate=request_gate,
                    clock=_utc_now,
                )
            except WishlistSyncError as exc:
                return _emit_error(
                    args,
                    command="sync.wishlist",
                    code=exc.code,
                    message="The wishlist synchronization did not complete.",
                    retryable=exc.retryable,
                )
    return _emit_success(
        args,
        command="sync.wishlist",
        context={"account_alias": account.alias, "identifiers_included": False},
        data={
            "sync_run_id": result.run.id,
            "sync_status": result.run.status,
            "records_seen": result.run.records_seen,
            "wishlist_count": result.item_count,
            "disclosure_version": WISHLIST_DISCLOSURE_VERSION,
            "support_level": "official_undocumented_provisional",
            "limitations": [
                "provider_contract_is_provisional",
                "empty_auth_like_response_is_ambiguous",
                "sequential_pair_may_detect_concurrent_wishlist_change",
            ],
        },
    )


def _dispatch_sync_prices(args: argparse.Namespace, database_path: Path) -> int:
    country = args.country.upper()
    if (
        len(country) != 2
        or not country.isascii()
        or not country.isalpha()
        or country != "US"
    ):
        return _emit_error(
            args,
            command="sync.prices",
            code=(
                "UNSUPPORTED_COUNTRY"
                if len(country) == 2 and country.isascii() and country.isalpha()
                else ErrorCode.INVALID_ARGUMENT
            ),
            message=(
                "GG.deals and CheapShark are currently supported only for US/USD."
                if len(country) == 2 and country.isascii() and country.isalpha()
                else "Country must be a two-letter code."
            ),
            exit_code=2,
        )
    if args.max_items is not None and not 1 <= args.max_items <= 10_000:
        return _emit_error(
            args,
            command="sync.prices",
            code=ErrorCode.INVALID_ARGUMENT,
            message="--max-items must be between 1 and 10000.",
            exit_code=2,
        )
    with _credential_operation_lock(database_path):
        spec = _CREDENTIAL_PROVIDERS["gg-deals"]
        credential_ref = _provider_credential_ref(database_path, spec)
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                return _emit_error(
                    args,
                    command="sync.prices",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            if account is None:
                return _emit_error(
                    args,
                    command="sync.prices",
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            resolved = (
                {"state": "missing", "secret": None}
                if metadata is None
                else _resolve_credential(metadata, credential_ref)
            )
            if args.provider == "gg-deals" and resolved["state"] != "configured":
                return _emit_error(
                    args,
                    command="sync.prices",
                    code=(
                        ErrorCode.AUTH_REQUIRED
                        if resolved["state"] == "missing"
                        else _credential_error_code(resolved["state"])
                    ),
                    message="The GG.deals API credential is unavailable.",
                )

            def gg_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "gg-deals", _utc_now(), _PROVIDER_MINIMUM_INTERVAL_SECONDS
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise GgDealsError("REQUEST_THROTTLED", retryable=True)

            def cheap_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "cheapshark", _utc_now(), _PROVIDER_MINIMUM_INTERVAL_SECONDS
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise CheapSharkError("REQUEST_THROTTLED", retryable=True)

            try:
                result = sync_wishlist_prices(
                    storage,
                    account_id=account.id,
                    country=country,
                    provider=args.provider,
                    gg_api_key=(
                        resolved.get("secret")
                        if resolved["state"] == "configured"
                        else None
                    ),
                    max_items=args.max_items,
                    gg_client=_gg_deals_client(gg_gate),
                    cheapshark_client=_cheapshark_client(cheap_gate),
                    clock=_utc_now,
                )
            except PriceSyncError as exc:
                return _emit_error(
                    args,
                    command="sync.prices",
                    code=exc.code,
                    message="Wishlist price synchronization did not complete.",
                    retryable=exc.retryable,
                )
    warnings = []
    if args.provider == "auto" and resolved["state"] != "configured":
        warnings.append(
            WarningRecord(
                code=_credential_error_code(resolved["state"]),
                message=(
                    "GG.deals was not attempted because its credential is unavailable; "
                    "the bounded CheapShark fallback was used."
                ),
            )
        )
    if result.completeness == "partial":
        warnings.append(
            WarningRecord(
                code=ErrorCode.PARTIAL_SCAN,
                message=(
                    "The requested synchronization did not complete every required "
                    "deal-evidence evaluation."
                ),
            )
        )
    if (
        args.provider == "auto"
        and result.completeness == "complete"
        and any(
            run.provider == "gg-deals" and run.error_code is not None
            for run in result.runs
        )
    ):
        warnings.append(
            WarningRecord(
                code="DEGRADED_FALLBACK",
                message=(
                    "GG.deals did not complete; fresh CheapShark fallback evidence "
                    "completed the requested deal-evidence ladder."
                ),
                source="gg-deals",
            )
        )
    return _emit_success(
        args,
        command="sync.prices",
        context={
            "account_alias": account.alias,
            "country": country,
            "currency": "USD",
            "scope": "wishlist",
            "identifiers_included": False,
        },
        completeness_value=completeness(
            CompletenessStatus(result.completeness), warnings=warnings
        ),
        data={
            "sync_runs": [
                {
                    "id": run.id,
                    "provider": run.provider,
                    "status": run.status,
                    "error_code": run.error_code,
                }
                for run in result.runs
            ],
            "provider_selection": args.provider,
            "providers_used": list(result.providers_used),
            "providers_attempted": list(result.providers_attempted),
            "evaluated_items": result.evaluated_items,
            "total_items": result.total_items,
            "observed_items": result.observed_items,
            "fallback_evaluated": result.fallback_evaluated,
            "fallback_total": result.fallback_total,
            "current_freshness_seconds": 6 * 60 * 60,
            "historical_low_freshness_seconds": 24 * 60 * 60,
            "hard_expiry_seconds": 7 * 24 * 60 * 60,
            "raw_payload_retained": False,
            "limitations": [
                "GG.deals exposes summary lows rather than a price-event graph",
                "CheapShark is USD-only and groups offers at game level",
                "provider links are manual-only and are never followed",
            ],
        },
    )


def _dispatch_sync_catalog(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        credential_ref = _steam_credential_ref(database_path)
        with Storage(database_path) as storage:
            try:
                account = storage.get_account(args.account)
            except ValueError:
                return _emit_error(
                    args,
                    command="sync.catalog",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            if account is None:
                return _emit_error(
                    args,
                    command="sync.catalog",
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="The requested account alias is not configured.",
                )
            # Demand derivation deliberately avoids catalog reads. A malformed
            # retained catalog projection must not prevent an explicit repair.
            demanded = storage.read_catalog_demand(account.id, args.machine)
            secret: SecretValue | None = None
            if demanded:
                metadata = storage.get_credential_reference(
                    provider=credential_ref.provider,
                    kind=credential_ref.kind,
                    profile_id=credential_ref.profile_id,
                )
                if metadata is None:
                    return _emit_error(
                        args,
                        command="sync.catalog",
                        code=ErrorCode.AUTH_REQUIRED,
                        message="A Steam Web API user key has not been configured.",
                    )
                resolved = _resolve_credential(metadata, credential_ref)
                if resolved["state"] != "configured":
                    return _emit_error(
                        args,
                        command="sync.catalog",
                        code=_credential_error_code(resolved["state"]),
                        message="The Steam Web API credential is unavailable.",
                    )
                secret = resolved["secret"]

            def request_gate() -> None:
                for attempt in range(2):
                    if _reserve_provider_request(
                        "steam-web-api",
                        _utc_now(),
                        _PROVIDER_MINIMUM_INTERVAL_SECONDS,
                    ):
                        return
                    if attempt == 0:
                        time.sleep(_PROVIDER_MINIMUM_INTERVAL_SECONDS + 0.05)
                raise CatalogApiError("REQUEST_THROTTLED", retryable=True)

            try:
                result = sync_catalog(
                    storage,
                    account_id=account.id,
                    machine_id=args.machine,
                    demanded_appids=demanded,
                    api_key=secret,
                    client=SteamStoreCatalogClient(request_gate=request_gate),
                    clock=_utc_now,
                )
            except CatalogSyncError as exc:
                return _emit_error(
                    args,
                    command="sync.catalog",
                    code=exc.code,
                    message="The bounded Steam catalog synchronization did not complete.",
                    retryable=exc.retryable,
                )
    return _emit_success(
        args,
        command="sync.catalog",
        context={
            "account_alias": account.alias,
            "machine_id": args.machine,
            "identifiers_included": False,
        },
        data={
            "sync_run_id": result.run.id,
            "sync_status": result.run.status,
            "demanded_count": result.demanded_count,
            "game_count": result.game_count,
            "non_game_count": result.non_game_count,
            "not_observed_count": result.not_observed_count,
            "page_count": result.page_count,
            "persistence_scope": "demanded_appids_only",
            "upstream_scan_scope": "ordered_catalog_through_highest_demanded_appid",
            "identity_limitations": [
                "packages_not_collected",
                "bundles_not_collected",
                "editions_not_collected",
                "non_game_subtype_not_distinguished",
            ],
        },
    )


def _account_snapshot_completeness(
    snapshot: Any, *, capability: str, subject: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    latest = snapshot.latest
    latest_complete = snapshot.latest_complete
    last_good_stale = False
    if (
        capability in ("owned.visible.read", "wishlist.read")
        and latest_complete is not None
    ):
        last_good_at = datetime.fromisoformat(
            latest_complete.completed_at.replace("Z", "+00:00")
        )
        freshness_seconds = (
            _OWNED_SYNC_FRESHNESS_SECONDS
            if capability == "owned.visible.read"
            else _WISHLIST_SYNC_FRESHNESS_SECONDS
        )
        last_good_stale = (
            _utc_now() - last_good_at
        ).total_seconds() > freshness_seconds
    if latest is None:
        return completeness(
            CompletenessStatus.UNAVAILABLE,
            missing_capabilities=[capability],
            warnings=[
                WarningRecord(
                    code=ErrorCode.NOT_SYNCED,
                    message=f"{subject} have not been synchronized.",
                )
            ],
        ), {"last_attempt_status": None, "last_successful_sync_at": None}
    if latest.status == "complete":
        if last_good_stale:
            return completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=[capability],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message=f"The {subject.lower()} snapshot is older than the freshness policy.",
                    )
                ],
            ), {
                "last_attempt_status": "complete",
                "last_successful_sync_at": latest.completed_at,
            }
        return completeness(CompletenessStatus.COMPLETE), {
            "last_attempt_status": "complete",
            "last_successful_sync_at": latest.completed_at,
        }
    if latest.status == "running":
        started_at = datetime.fromisoformat(latest.started_at.replace("Z", "+00:00"))
        abandoned = (_utc_now() - started_at).total_seconds() > _SYNC_ABANDONED_SECONDS
        warning = WarningRecord(
            code=(
                ErrorCode.SYNC_ABANDONED if abandoned else ErrorCode.SYNC_IN_PROGRESS
            ),
            message=(
                f"The last {subject.lower()} synchronization appears abandoned."
                if abandoned
                else f"A {subject.lower()} synchronization is in progress."
            ),
        )
        if latest_complete is None:
            value = completeness(
                CompletenessStatus.UNAVAILABLE,
                missing_capabilities=[capability],
                warnings=[warning],
            )
        elif last_good_stale:
            value = completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=[capability],
                warnings=[
                    warning,
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message=(
                            f"The {subject.lower()} snapshot is older than the "
                            "freshness policy."
                        ),
                    ),
                ],
            )
        else:
            value = completeness(
                CompletenessStatus.PARTIAL
                if abandoned
                else CompletenessStatus.COMPLETE,
                stale_capabilities=[capability] if abandoned else [],
                warnings=[warning],
            )
        return value, {
            "last_attempt_status": "running",
            "last_successful_sync_at": (
                None if latest_complete is None else latest_complete.completed_at
            ),
        }
    has_last_good = latest_complete is not None
    attempt_was_partial = latest.status == "partial"
    warning_code = (
        str(ErrorCode.STALE_LAST_GOOD)
        if has_last_good
        else (latest.error_code or str(ErrorCode.STALE_LAST_GOOD))
    )
    warning = WarningRecord(
        code=warning_code,
        message=(
            (
                f"The latest {subject.lower()} synchronization "
                f"{'was incomplete' if attempt_was_partial else 'failed'}; "
                "the last-good snapshot is preserved."
            )
            if has_last_good
            else (
                f"The latest {subject.lower()} synchronization "
                f"{'was incomplete' if attempt_was_partial else 'failed'} and no "
                "complete snapshot exists."
            )
        ),
    )
    if latest_complete is None:
        value = completeness(
            CompletenessStatus.UNAVAILABLE,
            missing_capabilities=[capability],
            warnings=[warning],
        )
    else:
        value = completeness(
            CompletenessStatus.PARTIAL,
            stale_capabilities=[capability],
            warnings=[warning],
        )
    return value, {
        "last_attempt_status": latest.status,
        "last_error_code": latest.error_code,
        "last_successful_sync_at": (
            None if latest_complete is None else latest_complete.completed_at
        ),
    }


def _owned_provenance(snapshot: Any) -> dict[str, Any] | None:
    provenance = snapshot.latest_complete_provenance
    if provenance is None:
        return None
    return {
        "sync_run_id": provenance.sync_run_id,
        "provider": provenance.provider,
        "support_level": provenance.support_level,
        "include_appinfo": provenance.include_appinfo,
        "base": {
            "include_played_free_games": (provenance.base_include_played_free_games),
            "retrieved_at": provenance.base_retrieved_at,
            "reported_count": provenance.base_reported_count,
        },
        "expanded": {
            "include_played_free_games": (
                provenance.expanded_include_played_free_games
            ),
            "retrieved_at": provenance.expanded_retrieved_at,
            "reported_count": provenance.expanded_reported_count,
        },
        "classification_method": provenance.classification_method,
    }


def _catalog_sources(snapshot: Any) -> list[dict[str, Any]]:
    return [
        {
            "sync_run_id": source.sync_run_id,
            "provider": source.provider,
            "support_level": source.support_level,
            "streams": [
                {
                    "stream": stream.stream,
                    "termination": stream.termination,
                    "scanned_through_appid": stream.scanned_through_appid,
                    "filter_context": dict(stream.filter_context),
                    "pages": [
                        {
                            "page_number": page.page_number,
                            "requested_last_appid": page.requested_last_appid,
                            "first_appid": page.first_appid,
                            "last_appid": page.last_appid,
                            "item_count": page.item_count,
                            "have_more_results": page.have_more_results,
                            "retrieved_at": page.retrieved_at,
                        }
                        for page in stream.pages
                    ],
                }
                for stream in source.streams
            ],
        }
        for source in snapshot.sources
    ]


def _catalog_completeness(
    snapshot: Any, *, demanded_appids: set[int]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not demanded_appids:
        return completeness(CompletenessStatus.COMPLETE), {
            "last_attempt_status": None,
            "last_error_code": None,
            "last_attempt_sync_run_id": None,
            "relevant_attempts": [],
            "freshness_window_seconds": _CATALOG_SYNC_FRESHNESS_SECONDS,
            "sources": [],
            "oldest_fact_observed_at": None,
            "newest_fact_observed_at": None,
            "stale_fact_count": 0,
        }
    observed = {fact.appid for fact in snapshot.facts}
    missing = demanded_appids - observed
    relevant_attempts = tuple(snapshot.attempts)
    if not relevant_attempts and snapshot.latest is not None:
        # Compatibility for callers constructing the pre-aggregate snapshot
        # shape directly; storage-backed scoped reads always provide attempts.
        attempt_values = ((snapshot.latest, tuple(sorted(demanded_appids))),)
    else:
        attempt_values = tuple(
            (attempt.run, attempt.appids) for attempt in relevant_attempts
        )
    attempted_appids = {appid for _, appids in attempt_values for appid in appids}
    missing_attempts = demanded_appids - attempted_appids
    sole_attempt = attempt_values[0][0] if len(attempt_values) == 1 else None
    metadata = {
        "last_attempt_status": None if sole_attempt is None else sole_attempt.status,
        "last_error_code": None if sole_attempt is None else sole_attempt.error_code,
        "last_attempt_sync_run_id": None if sole_attempt is None else sole_attempt.id,
        "relevant_attempts": [
            {
                "sync_run_id": run.id,
                "status": run.status,
                "error_code": run.error_code,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "appids": list(appids),
            }
            for run, appids in attempt_values
        ],
        "freshness_window_seconds": _CATALOG_SYNC_FRESHNESS_SECONDS,
        "sources": _catalog_sources(snapshot),
    }
    observed_times = [
        datetime.fromisoformat(fact.observed_at.replace("Z", "+00:00"))
        for fact in snapshot.facts
    ]
    stale_fact_count = sum(
        (_utc_now() - observed_at).total_seconds() > _CATALOG_SYNC_FRESHNESS_SECONDS
        for observed_at in observed_times
    )
    metadata.update(
        {
            "oldest_fact_observed_at": (
                None
                if not observed_times
                else min(observed_times).isoformat().replace("+00:00", "Z")
            ),
            "newest_fact_observed_at": (
                None
                if not observed_times
                else max(observed_times).isoformat().replace("+00:00", "Z")
            ),
            "stale_fact_count": stale_fact_count,
        }
    )
    failed_attempts = tuple(
        run for run, _ in attempt_values if run.status in ("failed", "partial")
    )
    running_attempts = tuple(
        run for run, _ in attempt_values if run.status == "running"
    )
    abandoned_running = bool(running_attempts) and any(
        (
            _utc_now() - datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))
        ).total_seconds()
        > _SYNC_ABANDONED_SECONDS
        for run in running_attempts
    )
    refresh_warning = (
        None
        if not running_attempts
        else WarningRecord(
            code=(
                ErrorCode.SYNC_ABANDONED
                if abandoned_running
                else ErrorCode.SYNC_IN_PROGRESS
            ),
            message=(
                "The catalog synchronization appears abandoned."
                if abandoned_running
                else "A catalog synchronization is in progress."
            ),
        )
    )
    if missing or missing_attempts:
        warnings = [
            WarningRecord(
                code=ErrorCode.NOT_SYNCED,
                message=(
                    "Catalog facts or scoped synchronization attempts are "
                    "missing for observed application identities."
                ),
            )
        ]
        if refresh_warning is not None:
            warnings.append(refresh_warning)
        if failed_attempts:
            failed_code = next(
                (
                    run.error_code
                    for run in reversed(failed_attempts)
                    if run.error_code is not None
                ),
                str(ErrorCode.STALE_LAST_GOOD),
            )
            warnings.append(
                WarningRecord(
                    code=failed_code,
                    message=(
                        "A relevant catalog synchronization failed or was "
                        "incomplete before it produced a last-good fact."
                    ),
                )
            )
        active_or_failed = bool(running_attempts or failed_attempts)
        return completeness(
            (
                CompletenessStatus.PARTIAL
                if active_or_failed and not missing_attempts
                else CompletenessStatus.UNAVAILABLE
            ),
            missing_capabilities=["catalog.application.read"],
            warnings=warnings,
        ), metadata
    if failed_attempts:
        warnings = [
            WarningRecord(
                code=ErrorCode.STALE_LAST_GOOD,
                message=(
                    "At least one demanded AppID has a newer failed or incomplete "
                    "catalog attempt; retained subject facts remain last-good."
                ),
            )
        ]
        if refresh_warning is not None:
            warnings.append(refresh_warning)
        return completeness(
            CompletenessStatus.PARTIAL,
            stale_capabilities=["catalog.application.read"],
            warnings=warnings,
        ), metadata
    if running_attempts:
        assert refresh_warning is not None
        if stale_fact_count:
            return completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=["catalog.application.read"],
                warnings=[
                    refresh_warning,
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message=(
                            "One or more retained catalog facts are older than "
                            "the 24-hour freshness window."
                        ),
                    ),
                ],
            ), metadata
        return completeness(
            (
                CompletenessStatus.PARTIAL
                if abandoned_running
                else CompletenessStatus.COMPLETE
            ),
            stale_capabilities=(
                ["catalog.application.read"] if abandoned_running else []
            ),
            warnings=[refresh_warning],
        ), metadata
    if stale_fact_count:
        return completeness(
            CompletenessStatus.PARTIAL,
            stale_capabilities=["catalog.application.read"],
            warnings=[
                WarningRecord(
                    code=ErrorCode.STALE_LAST_GOOD,
                    message=(
                        "One or more retained catalog facts are older than the "
                        "24-hour freshness window."
                    ),
                )
            ],
        ), metadata
    return completeness(CompletenessStatus.COMPLETE), metadata


def _dispatch_wishlist_games_query(
    args: argparse.Namespace, database_path: Path
) -> int:
    if args.include_paths:
        return _emit_error(
            args,
            command="games.query",
            code=ErrorCode.INVALID_ARGUMENT,
            message="--include-paths is available only for installed-scope queries.",
            exit_code=2,
        )
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command="games.query",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            return _emit_success(
                args,
                command="games.query",
                context={
                    "account_alias": args.account,
                    "scopes": ["wishlist"],
                    "identifiers_included": False,
                },
                completeness_value=completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["account.identity"],
                    warnings=[
                        WarningRecord(
                            code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                            message="The requested account alias is not configured.",
                        )
                    ],
                ),
                data={
                    "items": [],
                    "empty": False,
                    "next_cursor": None,
                    "source": None,
                    "snapshot": {
                        "last_attempt_status": None,
                        "last_successful_sync_at": None,
                    },
                },
            )
        snapshot = storage.read_wishlist_snapshot(account.id)
    value, metadata = _account_snapshot_completeness(
        snapshot, capability="wishlist.read", subject="Wishlist items"
    )
    stable_ids = dict(snapshot.stable_game_ids_by_appid)
    source = snapshot.latest_complete_provenance
    return _emit_success(
        args,
        command="games.query",
        context={
            "account_alias": account.alias,
            "scopes": ["wishlist"],
            "identifiers_included": False,
        },
        completeness_value=value,
        data={
            "items": [
                {
                    "appid": game.appid,
                    "game_id": f"game:{stable_ids[game.appid]}",
                    "wishlisted": True,
                    "priority": game.priority,
                    "date_added_unix": game.date_added,
                    "observed_at": game.observed_at,
                    "evidence_ids": [game.evidence_id],
                }
                for game in snapshot.games
            ],
            "empty": bool(snapshot.latest_complete is not None and not snapshot.games),
            "next_cursor": None,
            "source": (
                None
                if source is None
                else {
                    "provider": source.provider,
                    "support_level": source.support_level,
                    "validation_method": source.validation_method,
                    "item_list_retrieved_at": source.item_list_retrieved_at,
                    "item_count_retrieved_at": source.item_count_retrieved_at,
                    "reported_count": source.item_count_reported_count,
                }
            ),
            "snapshot": metadata,
            "limitations": [
                "provider_contract_is_provisional",
                "empty_auth_like_response_is_ambiguous",
            ],
        },
    )


def _dispatch_account_games_query(args: argparse.Namespace, database_path: Path) -> int:
    if args.include_paths:
        return _emit_error(
            args,
            command="games.query",
            code=ErrorCode.INVALID_ARGUMENT,
            message="--include-paths is available only for installed-scope queries.",
            exit_code=2,
        )
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command="games.query",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            unavailable_snapshot = {
                "last_attempt_status": None,
                "last_successful_sync_at": None,
            }
            if args.scope == "owned":
                empty_data: dict[str, Any] = {
                    "items": [],
                    "empty": False,
                    "limitations": [
                        "individually_private_games_may_be_omitted",
                        "unplayed_free_entitlements_are_not_complete",
                        "sequential_request_difference_may_reflect_concurrent_library_change",
                    ],
                    "next_cursor": None,
                    "source": None,
                    "snapshot": unavailable_snapshot,
                }
            else:
                empty_data = {
                    "items": [],
                    "limitations": [
                        "individually_private_games_may_be_omitted",
                        "unplayed_free_entitlements_are_not_complete",
                        "sequential_request_difference_may_reflect_concurrent_library_change",
                    ],
                    "next_cursor": None,
                    "snapshots": {
                        "owned": {**unavailable_snapshot, "source": None},
                        "installed": unavailable_snapshot,
                        "catalog": {
                            "last_attempt_status": None,
                            "last_error_code": None,
                            "sources": [],
                        },
                    },
                }
            return _emit_success(
                args,
                command="games.query",
                context={
                    "account_alias": args.account,
                    "scopes": (
                        ["owned", "installed", "catalog"]
                        if args.scope == "library"
                        else ["owned"]
                    ),
                    "identifiers_included": False,
                    **({"machine_id": args.machine} if args.scope == "library" else {}),
                },
                completeness_value=completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["account.identity"],
                    warnings=[
                        WarningRecord(
                            code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                            message="The requested account alias is not configured.",
                        )
                    ],
                ),
                data=empty_data,
            )
        if args.scope == "owned":
            owned_snapshot = storage.read_owned_snapshot(account.id)
            owned_game_ids = dict(owned_snapshot.stable_game_ids_by_appid)
            owned_completeness, metadata = _account_snapshot_completeness(
                owned_snapshot,
                capability="owned.visible.read",
                subject="Owned games",
            )
            return _emit_success(
                args,
                command="games.query",
                context={
                    "account_alias": account.alias,
                    "scopes": ["owned"],
                    "identifiers_included": False,
                },
                completeness_value=owned_completeness,
                data={
                    "items": [
                        {
                            **owned_item(game),
                            "game_id": f"game:{owned_game_ids[game.appid]}",
                        }
                        for game in owned_snapshot.games
                    ],
                    "empty": bool(
                        owned_snapshot.latest_complete is not None
                        and not owned_snapshot.games
                    ),
                    "limitations": [
                        "individually_private_games_may_be_omitted",
                        "unplayed_free_entitlements_are_not_complete",
                        "sequential_request_difference_may_reflect_concurrent_library_change",
                    ],
                    "next_cursor": None,
                    "source": _owned_provenance(owned_snapshot),
                    "snapshot": metadata,
                },
            )
        library = storage.read_library_snapshot(account.id, args.machine)

    owned_completeness, owned_metadata = _account_snapshot_completeness(
        library.owned,
        capability="owned.visible.read",
        subject="Owned games",
    )
    installed_completeness, installed_metadata = _account_snapshot_completeness(
        library.installed,
        capability="installed.read",
        subject="Installed games",
    )
    catalog_completeness, catalog_metadata = _catalog_completeness(
        library.catalog,
        demanded_appids={
            *(game.appid for game in library.owned.games),
            *(game.appid for game in library.installed.games),
        },
    )
    owned_usable = library.owned.latest_complete is not None
    installed_usable = library.installed.latest_complete is not None
    installed_types_by_appid = {
        game.appid: game.app_type for game in library.installed.games
    }
    entity_ids = dict(library.stable_game_ids_by_appid)
    by_appid: dict[int, dict[str, Any]] = {}
    for game in library.owned.games:
        by_appid[game.appid] = {
            **owned_item(game),
            "game_id": f"game:{entity_ids[game.appid]}",
            "installed": False if installed_usable else None,
            "app_type": "unknown",
            "names": {"owned": game.name, "installed": None},
        }
    for game in library.installed.games:
        item = by_appid.setdefault(
            game.appid,
            {
                "appid": game.appid,
                "game_id": f"game:{entity_ids[game.appid]}",
                "name": game.name,
                "visible_in_owned_games": False if owned_usable else None,
                "inclusion_basis": None,
                "playtime_forever_minutes": None,
                "observed_at": game.observed_at,
                "evidence_ids": [],
                "family_available": None,
                "purchasable": None,
                "playable_now": None,
                "names": {"owned": None, "installed": game.name},
            },
        )
        item["installed"] = True
        item["app_type"] = game.app_type
        item["names"]["installed"] = game.name
        if item["name"] is None and game.name is not None:
            item["name"] = game.name
        item["evidence_ids"] = sorted({*item["evidence_ids"], game.evidence_id})
    catalog_by_appid = {fact.appid: fact for fact in library.catalog.facts}
    for appid, item in by_appid.items():
        fact = catalog_by_appid.get(appid)
        item["catalog_classification"] = None if fact is None else fact.classification
        item["catalog_observed_at"] = None if fact is None else fact.observed_at
        item["catalog_evidence_ids"] = [] if fact is None else [fact.evidence_id]
        item["app_types"] = {
            "installed": installed_types_by_appid.get(appid),
            "catalog": None if fact is None else fact.classification,
        }
        if fact is not None and fact.classification in ("game", "non_game"):
            item["app_type"] = fact.classification
        if fact is not None:
            item["evidence_ids"] = sorted({*item["evidence_ids"], fact.evidence_id})
        item["identity"] = {
            "entity_kind": "application",
            "external_identities": [
                {
                    "provider": "steam",
                    "identity_kind": "application_appid",
                    "value": str(appid),
                }
            ],
            "package": None,
            "bundle": None,
            "edition": None,
        }
    warnings = [
        *owned_completeness["warnings"],
        *installed_completeness["warnings"],
        *catalog_completeness["warnings"],
    ]
    missing = sorted(
        {
            *owned_completeness["missing_capabilities"],
            *installed_completeness["missing_capabilities"],
            *catalog_completeness["missing_capabilities"],
        }
    )
    stale = sorted(
        {
            *owned_completeness["stale_capabilities"],
            *installed_completeness["stale_capabilities"],
            *catalog_completeness["stale_capabilities"],
        }
    )
    if missing and not (owned_usable or installed_usable):
        status = CompletenessStatus.UNAVAILABLE
    elif missing:
        status = CompletenessStatus.PARTIAL
    elif stale or any(
        value["status"] == "partial"
        for value in (
            owned_completeness,
            installed_completeness,
            catalog_completeness,
        )
    ):
        status = CompletenessStatus.PARTIAL
    else:
        status = CompletenessStatus.COMPLETE
    return _emit_success(
        args,
        command="games.query",
        context={
            "account_alias": account.alias,
            "machine_id": args.machine,
            "scopes": ["owned", "installed", "catalog"],
            "identifiers_included": False,
        },
        completeness_value=completeness(
            status,
            warnings=warnings,
            missing_capabilities=missing,
            stale_capabilities=stale,
        ),
        data={
            "items": [by_appid[appid] for appid in sorted(by_appid)],
            "limitations": [
                "individually_private_games_may_be_omitted",
                "unplayed_free_entitlements_are_not_complete",
                "sequential_request_difference_may_reflect_concurrent_library_change",
            ],
            "next_cursor": None,
            "snapshots": {
                "owned": {
                    **owned_metadata,
                    "source": _owned_provenance(library.owned),
                },
                "installed": installed_metadata,
                "catalog": catalog_metadata,
            },
        },
    )


def _dispatch_feedback(args: argparse.Namespace, database_path: Path) -> int:
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command=_command_name(args),
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
            )
        if account is None:
            return _emit_error(
                args,
                command=_command_name(args),
                code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                message="The requested account alias is not configured.",
            )
        service = FeedbackService(storage)
        try:
            if args.command == "preferences":
                if args.rule_command == "set":
                    event_id = service.set_rule(
                        account.id,
                        trait=args.trait,
                        kind=args.kind,
                        strength=args.strength,
                        weight=args.weight,
                    )
                    data: dict[str, Any] = {
                        "event_id": event_id,
                        "rule": next(
                            item
                            for item in service.list_rules(account.id)
                            if item["trait"] == args.trait
                        ),
                    }
                elif args.rule_command == "remove":
                    data = {
                        "removed": service.remove_rule(account.id, trait=args.trait),
                        "trait": args.trait,
                    }
                else:
                    data = {"rules": list(service.list_rules(account.id))}
                return _emit_success(
                    args,
                    command=_command_name(args),
                    context={
                        "account_alias": account.alias,
                        "identifiers_included": False,
                    },
                    data=data,
                )

            command = args.feedback_command
            if command == "query":
                return _emit_success(
                    args,
                    command="feedback.query",
                    context={
                        "account_alias": account.alias,
                        "identifiers_included": False,
                    },
                    data={
                        "items": list(service.query(account.id, appid=args.appid)),
                        "next_cursor": None,
                    },
                )
            if command == "rate":
                changes = (
                    service.rate(
                        account.id,
                        args.appid,
                        args.value,
                        clear=args.clear,
                    ),
                )
            elif command in {"finish", "abandon", "resume"}:
                state = {
                    "finish": "finished",
                    "abandon": "user_abandoned",
                    "resume": "active",
                }[command]
                changes = (service.play_state(account.id, args.appid, state),)
            elif command == "clear-state":
                changes = (
                    service.play_state(account.id, args.appid, None, clear=True),
                )
            elif command == "snooze":
                changes = (
                    service.snooze(
                        account.id,
                        args.appid,
                        until=args.until,
                        clear=args.clear,
                    ),
                )
            elif command == "estimate":
                changes = service.estimate(
                    account.id,
                    args.appid,
                    minimum_session_minutes=args.minimum_session_minutes,
                    remaining_minutes=args.remaining_minutes,
                    clear_minimum_session_minutes=args.clear_minimum_session_minutes,
                    clear_remaining_minutes=args.clear_remaining_minutes,
                )
            elif command == "trait":
                changes = (
                    service.trait(
                        account.id,
                        args.appid,
                        args.trait,
                        args.value,
                        clear=args.clear,
                    ),
                )
            else:
                raise AssertionError("unhandled feedback command")
            item = service.query(account.id, appid=args.appid)
            return _emit_success(
                args,
                command=f"feedback.{command}",
                context={"account_alias": account.alias, "identifiers_included": False},
                data={
                    "changes": [asdict(change) for change in changes],
                    "item": None if not item else item[0],
                },
            )
        except ValueError:
            return _emit_error(
                args,
                command=_command_name(args),
                code=ErrorCode.INVALID_ARGUMENT,
                message="The feedback arguments are invalid.",
            )


def _dispatch_data(args: argparse.Namespace, database_path: Path) -> int:
    if args.data_command != "delete":
        raise AssertionError("unhandled data command")
    if not args.yes:
        deletion_subject = (
            "Local system-profile data"
            if args.provider == "local-system"
            else (
                "Steam Store declared compatibility data"
                if args.provider == "steam-store-appdetails"
                else "Steam Web API data"
            )
        )
        return _emit_error(
            args,
            command="data.delete",
            code=ErrorCode.CONFIRMATION_REQUIRED,
            message=f"{deletion_subject} deletion requires --yes.",
        )
    if args.provider == "local-system":
        if args.machine is None or args.account is not None or args.all:
            return _emit_error(
                args,
                command="data.delete",
                code=ErrorCode.INVALID_ARGUMENT,
                message="Local system-profile deletion requires exactly --machine.",
                exit_code=2,
            )
        with Storage(database_path) as storage:
            deletion = storage.delete_system_profile_data(args.machine)
        return _emit_success(
            args,
            command="data.delete",
            data={
                "scope": "local-system-machine",
                "provider": "local-system",
                "machine_alias": args.machine,
                **deletion,
                "machine_preserved": True,
                "installed_data_preserved": True,
                "backup_copies_require_separate_deletion": True,
            },
        )
    if args.machine is not None:
        return _emit_error(
            args,
            command="data.delete",
            code=ErrorCode.INVALID_ARGUMENT,
            message="This provider does not support a machine deletion target.",
            exit_code=2,
        )
    if args.provider in {"gg-deals", "cheapshark"}:
        return _delete_price_provider_data(args, database_path)
    if args.provider == "steam-store-appdetails":
        with Storage(database_path) as storage:
            try:
                account = (
                    None if args.account is None else storage.get_account(args.account)
                )
            except ValueError:
                return _emit_error(
                    args,
                    command="data.delete",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            deletion = (
                {
                    "observations_removed": 0,
                    "demand_removed": 0,
                    "current_removed": 0,
                    "sync_runs_removed": 0,
                    "consents_removed": 0,
                }
                if args.account is not None and account is None
                else storage.delete_declared_app_data(
                    account_id=None if account is None else account.id
                )
            )
        return _emit_success(
            args,
            command="data.delete",
            data={
                "scope": "provider-all" if args.all else "account-provider",
                "provider": args.provider,
                "account_alias": args.account,
                **deletion,
                "global_public_current_preserved": not args.all,
                "account_preserved": True,
                "credential_preserved": True,
                "backup_copies_require_separate_deletion": True,
            },
        )
    if args.provider == "steam-store-reviews":
        with Storage(database_path) as storage:
            try:
                account = (
                    None if args.account is None else storage.get_account(args.account)
                )
            except ValueError:
                return _emit_error(
                    args,
                    command="data.delete",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="The account alias is invalid.",
                    exit_code=2,
                )
            if args.account is not None and account is None:
                deletion = {
                    "observations_removed": 0,
                    "demand_removed": 0,
                    "current_removed": 0,
                    "sync_runs_removed": 0,
                }
            else:
                deletion = storage.delete_review_data(
                    account_id=None if account is None else account.id
                )
        return _emit_success(
            args,
            command="data.delete",
            data={
                "scope": "provider-all" if args.all else "account-provider",
                "provider": args.provider,
                "account_alias": args.account,
                **deletion,
                "account_preserved": True,
                "credential_preserved": True,
                "backup_copies_require_separate_deletion": True,
            },
        )
    with _credential_operation_lock(database_path):
        if args.account is not None:
            with Storage(database_path) as storage:
                try:
                    account = storage.get_account(args.account)
                except ValueError:
                    return _emit_error(
                        args,
                        command="data.delete",
                        code=ErrorCode.INVALID_ARGUMENT,
                        message="The account alias is invalid.",
                        exit_code=2,
                    )
                if account is None:
                    return _emit_success(
                        args,
                        command="data.delete",
                        data={
                            "scope": "account",
                            "account_alias": args.account,
                            "removed": False,
                            "owned_observations_removed": 0,
                            "owned_current_removed": 0,
                            "wishlist_observations_removed": 0,
                            "wishlist_current_removed": 0,
                            "price_observations_removed": 0,
                            "price_current_removed": 0,
                            "price_subjects_removed": 0,
                            "feedback_events_removed": 0,
                            "feedback_current_removed": 0,
                            "feedback_traits_removed": 0,
                            "preference_rule_events_removed": 0,
                            "preference_rules_removed": 0,
                            "activity_observations_removed": 0,
                            "activity_current_removed": 0,
                            "achievement_demand_removed": 0,
                            "achievement_player_observations_removed": 0,
                            "achievement_player_current_removed": 0,
                            "sync_runs_removed": 0,
                            "probes_removed": 0,
                            "consents_removed": 0,
                            "evidence_removed": 0,
                            "orphan_apps_removed": 0,
                            "shared_credential_preserved": True,
                            "backup_copies_require_separate_deletion": True,
                        },
                    )
                result = storage.delete_steam_account_data(account.id)
            return _emit_success(
                args,
                command="data.delete",
                data={
                    "scope": "account",
                    "account_alias": args.account,
                    "removed": result.account_removed,
                    "owned_observations_removed": result.owned_observations_removed,
                    "owned_current_removed": result.owned_current_removed,
                    "wishlist_observations_removed": result.wishlist_observations_removed,
                    "wishlist_current_removed": result.wishlist_current_removed,
                    "price_observations_removed": result.price_observations_removed,
                    "price_current_removed": result.price_current_removed,
                    "price_subjects_removed": result.price_subjects_removed,
                    "feedback_events_removed": result.feedback_events_removed,
                    "feedback_current_removed": result.feedback_current_removed,
                    "feedback_traits_removed": result.feedback_traits_removed,
                    "preference_rule_events_removed": result.preference_rule_events_removed,
                    "preference_rules_removed": result.preference_rules_removed,
                    "activity_observations_removed": result.activity_observations_removed,
                    "activity_current_removed": result.activity_current_removed,
                    "achievement_demand_removed": result.achievement_demand_removed,
                    "achievement_player_observations_removed": result.achievement_player_observations_removed,
                    "achievement_player_current_removed": result.achievement_player_current_removed,
                    "sync_runs_removed": result.sync_runs_removed,
                    "probes_removed": result.probes_removed,
                    "consents_removed": result.consents_removed,
                    "evidence_removed": result.evidence_removed,
                    "orphan_apps_removed": result.orphan_apps_removed,
                    "shared_credential_preserved": True,
                    "backup_copies_require_separate_deletion": True,
                },
            )
        return _delete_all_steam_web_api_data(args, database_path)


def _delete_price_provider_data(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        if args.account is not None:
            with Storage(database_path) as storage:
                try:
                    account = storage.get_account(args.account)
                except ValueError:
                    return _emit_error(
                        args,
                        command="data.delete",
                        code=ErrorCode.INVALID_ARGUMENT,
                        message="The account alias is invalid.",
                        exit_code=2,
                    )
                if account is None:
                    return _emit_success(
                        args,
                        command="data.delete",
                        data={
                            "scope": "account-provider",
                            "provider": args.provider,
                            "account_alias": args.account,
                            "price_observations_removed": 0,
                            "price_current_removed": 0,
                            "price_subjects_removed": 0,
                            "sync_runs_removed": 0,
                            "evidence_removed": 0,
                            "account_preserved": True,
                            "credential_preserved": True,
                            "backup_copies_require_separate_deletion": True,
                        },
                    )
                deletion = storage.delete_price_data(
                    provider=args.provider, account_id=account.id
                )
            return _emit_success(
                args,
                command="data.delete",
                data={
                    "scope": "account-provider",
                    "provider": args.provider,
                    "account_alias": args.account,
                    "price_observations_removed": deletion.observations_removed,
                    "price_current_removed": deletion.current_removed,
                    "price_subjects_removed": deletion.subjects_removed,
                    "sync_runs_removed": deletion.sync_runs_removed,
                    "evidence_removed": deletion.evidence_removed,
                    "account_preserved": True,
                    "credential_preserved": True,
                    "backup_copies_require_separate_deletion": True,
                },
            )

        metadata = None
        credential_ref = None
        store = None
        previous_secret = None
        credential_unreadable = False
        credential_deleted = False
        if args.provider == "gg-deals":
            credential_ref = _provider_credential_ref(
                database_path, _CREDENTIAL_PROVIDERS["gg-deals"]
            )
            with Storage(database_path) as storage:
                metadata = storage.get_credential_reference(
                    provider=credential_ref.provider,
                    kind=credential_ref.kind,
                    profile_id=credential_ref.profile_id,
                )
            if metadata is not None:
                store = _credential_store(metadata.backend, metadata.backend_locator)
                try:
                    previous_secret = store.resolve(credential_ref)
                except CredentialError as exc:
                    if exc.code != "CREDENTIAL_READ_FAILED":
                        raise
                    credential_unreadable = True
        try:
            if store is not None and (
                previous_secret is not None or credential_unreadable
            ):
                if not store.delete(credential_ref):
                    raise CredentialError(str(ErrorCode.CREDENTIAL_DELETE_FAILED))
                credential_deleted = True
            with Storage(database_path) as storage:
                deletion = storage.delete_price_data(
                    provider=args.provider,
                    credential_kind=(None if metadata is None else credential_ref.kind),
                    credential_profile_id=(
                        None if metadata is None else credential_ref.profile_id
                    ),
                )
        except BaseException:
            if store is not None and previous_secret is not None:
                try:
                    store.put(credential_ref, previous_secret)
                except BaseException:
                    return _emit_error(
                        args,
                        command="data.delete",
                        code=ErrorCode.CREDENTIAL_ROLLBACK_FAILED,
                        message="Provider deletion failed and the key could not be restored.",
                    )
            elif credential_deleted and credential_unreadable:
                return _emit_error(
                    args,
                    command="data.delete",
                    code=ErrorCode.CREDENTIAL_ROLLBACK_FAILED,
                    message=(
                        "Provider deletion failed after an unreadable locally managed "
                        "key was removed. The database was retained, but the key could "
                        "not be restored."
                    ),
                )
            raise
        return _emit_success(
            args,
            command="data.delete",
            data={
                "scope": "provider-all",
                "provider": args.provider,
                "price_observations_removed": deletion.observations_removed,
                "price_current_removed": deletion.current_removed,
                "price_subjects_removed": deletion.subjects_removed,
                "sync_runs_removed": deletion.sync_runs_removed,
                "evidence_removed": deletion.evidence_removed,
                "credential_refs_removed": deletion.credential_refs_removed,
                "local_credential_removed": credential_deleted,
                "steam_account_data_preserved": True,
                "other_provider_data_preserved": True,
                "backup_copies_require_separate_deletion": True,
            },
        )


def _delete_all_steam_web_api_data(
    args: argparse.Namespace, database_path: Path
) -> int:
    credential_ref = _steam_credential_ref(database_path)
    with Storage(database_path) as storage:
        metadata = storage.get_credential_reference(
            provider=credential_ref.provider,
            kind=credential_ref.kind,
            profile_id=credential_ref.profile_id,
        )
    store = None
    previous_secret = None
    credential_unreadable = False
    credential_deleted = False
    if metadata is not None:
        store = _credential_store(metadata.backend, metadata.backend_locator)
        try:
            previous_secret = store.resolve(credential_ref)
        except CredentialError as exc:
            if exc.code != "CREDENTIAL_READ_FAILED":
                raise
            credential_unreadable = True
    try:
        if store is not None and (previous_secret is not None or credential_unreadable):
            if not store.delete(credential_ref):
                raise CredentialError(str(ErrorCode.CREDENTIAL_DELETE_FAILED))
            credential_deleted = True
        with Storage(database_path) as storage:
            deletion = storage.delete_all_steam_account_data(
                credential_provider=(
                    None if metadata is None else credential_ref.provider
                ),
                credential_kind=None if metadata is None else credential_ref.kind,
                credential_profile_id=(
                    None if metadata is None else credential_ref.profile_id
                ),
            )
    except BaseException:
        if store is not None and previous_secret is not None:
            try:
                store.put(credential_ref, previous_secret)
            except BaseException:
                return _emit_error(
                    args,
                    command="data.delete",
                    code=ErrorCode.CREDENTIAL_ROLLBACK_FAILED,
                    message=(
                        "Account-data deletion failed and the locally managed key "
                        "could not be restored."
                    ),
                )
        elif credential_deleted and credential_unreadable:
            return _emit_error(
                args,
                command="data.delete",
                code=ErrorCode.CREDENTIAL_ROLLBACK_FAILED,
                message=(
                    "Account-data deletion failed after an unreadable locally "
                    "managed key was removed. The database was retained, but "
                    "the key could not be restored."
                ),
            )
        raise
    return _emit_success(
        args,
        command="data.delete",
        data={
            "scope": "all-steam-web-api",
            "accounts_removed": deletion.accounts_removed,
            "owned_observations_removed": deletion.owned_observations_removed,
            "owned_current_removed": deletion.owned_current_removed,
            "wishlist_observations_removed": deletion.wishlist_observations_removed,
            "wishlist_current_removed": deletion.wishlist_current_removed,
            "price_observations_removed": deletion.price_observations_removed,
            "price_current_removed": deletion.price_current_removed,
            "price_subjects_removed": deletion.price_subjects_removed,
            "feedback_events_removed": deletion.feedback_events_removed,
            "feedback_current_removed": deletion.feedback_current_removed,
            "feedback_traits_removed": deletion.feedback_traits_removed,
            "preference_rule_events_removed": deletion.preference_rule_events_removed,
            "preference_rules_removed": deletion.preference_rules_removed,
            "activity_observations_removed": deletion.activity_observations_removed,
            "activity_current_removed": deletion.activity_current_removed,
            "achievement_demand_removed": deletion.achievement_demand_removed,
            "achievement_player_observations_removed": deletion.achievement_player_observations_removed,
            "achievement_player_current_removed": deletion.achievement_player_current_removed,
            "sync_runs_removed": deletion.sync_runs_removed,
            "probes_removed": deletion.probes_removed,
            "consents_removed": deletion.consents_removed,
            "evidence_removed": deletion.evidence_removed,
            "orphan_apps_removed": deletion.orphan_apps_removed,
            "credential_refs_removed": deletion.credential_refs_removed,
            "catalog_observations_removed": (deletion.catalog_observations_removed),
            "catalog_current_removed": deletion.catalog_current_removed,
            "catalog_sync_runs_removed": deletion.catalog_sync_runs_removed,
            "catalog_metadata_removed": deletion.catalog_metadata_removed,
            "catalog_streams_removed": deletion.catalog_streams_removed,
            "catalog_pages_removed": deletion.catalog_pages_removed,
            "catalog_evidence_removed": deletion.catalog_evidence_removed,
            "shared_credential_preserved": deletion.shared_credential_preserved,
            "local_credential_removed": (
                previous_secret is not None or credential_unreadable
            ),
            "credential_already_absent": (
                metadata is not None
                and previous_secret is None
                and not credential_unreadable
            ),
            "valve_key_revoked": False,
            "backup_copies_require_separate_deletion": True,
        },
    )


def _dispatch_accounts(args: argparse.Namespace, database_path: Path) -> int:
    if args.accounts_command in ("configure", "remove"):
        with _credential_operation_lock(database_path):
            return _dispatch_accounts_locked(args, database_path)
    return _dispatch_accounts_locked(args, database_path)


def _dispatch_accounts_locked(args: argparse.Namespace, database_path: Path) -> int:
    if args.accounts_command == "discover":
        discovery = discover_local_accounts(_account_steam_root(args))
        try:
            select_primary_local_account(discovery)
            selection = "available"
        except NoLocalAccount:
            selection = "none"
        except AmbiguousLocalAccounts:
            selection = "ambiguous"
        status = (
            CompletenessStatus.COMPLETE
            if selection == "available"
            else CompletenessStatus.UNAVAILABLE
        )
        warnings: list[WarningRecord] = []
        if selection == "ambiguous":
            warnings.append(
                WarningRecord(
                    code=ErrorCode.ACCOUNT_AMBIGUOUS,
                    message="Multiple local Steam accounts require explicit selection.",
                )
            )
        elif selection == "none":
            warnings.append(
                WarningRecord(
                    code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                    message="No local Steam account candidate was found.",
                )
            )
        return _emit_success(
            args,
            command="accounts.discover",
            completeness_value=completeness(
                status,
                warnings=warnings,
                missing_capabilities=(
                    ["account.identity"]
                    if status == CompletenessStatus.UNAVAILABLE
                    else []
                ),
            ),
            data={
                "candidate_count": len(discovery.candidates),
                "primary_selection": selection,
                "support_level": discovery.support_level,
                "identifiers_included": bool(args.include_identifiers),
                **(
                    {
                        "candidates": [
                            {
                                "steam_id64": candidate.steam_id64,
                                "most_recent": candidate.most_recent,
                            }
                            for candidate in discovery.candidates
                        ]
                    }
                    if args.include_identifiers
                    else {}
                ),
            },
        )
    if args.accounts_command == "configure":
        discovery = discover_local_accounts(_account_steam_root(args))
        if args.from_local_most_recent:
            selected = select_primary_local_account(discovery)
        else:
            selected = next(
                (
                    candidate
                    for candidate in discovery.candidates
                    if candidate.steam_id64 == args.steam_id64
                ),
                None,
            )
            if selected is None:
                return _emit_error(
                    args,
                    command="accounts.configure",
                    code=ErrorCode.ACCOUNT_SELECTION_NOT_FOUND,
                    message="The selected Steam identity is not present in the local registry.",
                    remediation="Run accounts discover --include-identifiers and choose a listed identity.",
                    exit_code=2,
                )
        try:
            with Storage(database_path) as storage:
                account = storage.configure_steam_account(
                    alias=args.alias,
                    steam_id64=selected.steam_id64,
                    configured_at=_utc_now(),
                )
        except ValueError:
            return _emit_error(
                args,
                command="accounts.configure",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        return _emit_success(
            args,
            command="accounts.configure",
            data={
                "alias": account.alias,
                "provider": account.provider,
                "configured": True,
                "source_kind": account.source_kind,
                "identifier_included": False,
            },
        )
    if args.accounts_command == "status":
        try:
            with Storage(database_path) as storage:
                account = storage.get_account(args.alias)
        except ValueError:
            return _emit_error(
                args,
                command="accounts.status",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        if account is None:
            return _emit_success(
                args,
                command="accounts.status",
                completeness_value=completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=["account.identity"],
                    warnings=[
                        WarningRecord(
                            code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                            message="The requested account alias is not configured.",
                        )
                    ],
                ),
                data={"alias": args.alias, "configured": False},
            )
        data: dict[str, Any] = {
            "alias": account.alias,
            "provider": account.provider,
            "configured": True,
            "source_kind": account.source_kind,
            "identifier_included": bool(args.include_identifiers),
        }
        if args.include_identifiers:
            data["steam_id64"] = account.provider_account_id
        return _emit_success(args, command="accounts.status", data=data)
    if args.accounts_command == "remove":
        if not args.yes:
            return _emit_error(
                args,
                command="accounts.remove",
                code=ErrorCode.CONFIRMATION_REQUIRED,
                message="Account removal requires --yes.",
            )
        try:
            with Storage(database_path) as storage:
                account = storage.get_account(args.alias)
                deletion = (
                    None
                    if account is None
                    else storage.delete_steam_account_data(account.id)
                )
        except ValueError:
            return _emit_error(
                args,
                command="accounts.remove",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        return _emit_success(
            args,
            command="accounts.remove",
            data={
                "alias": args.alias,
                "removed": deletion is not None and deletion.account_removed,
                "owned_observations_removed": (
                    0 if deletion is None else deletion.owned_observations_removed
                ),
                "owned_current_removed": (
                    0 if deletion is None else deletion.owned_current_removed
                ),
                "wishlist_observations_removed": (
                    0 if deletion is None else deletion.wishlist_observations_removed
                ),
                "wishlist_current_removed": (
                    0 if deletion is None else deletion.wishlist_current_removed
                ),
                "price_observations_removed": (
                    0 if deletion is None else deletion.price_observations_removed
                ),
                "price_current_removed": (
                    0 if deletion is None else deletion.price_current_removed
                ),
                "price_subjects_removed": (
                    0 if deletion is None else deletion.price_subjects_removed
                ),
                "feedback_events_removed": (
                    0 if deletion is None else deletion.feedback_events_removed
                ),
                "feedback_current_removed": (
                    0 if deletion is None else deletion.feedback_current_removed
                ),
                "feedback_traits_removed": (
                    0 if deletion is None else deletion.feedback_traits_removed
                ),
                "preference_rule_events_removed": (
                    0 if deletion is None else deletion.preference_rule_events_removed
                ),
                "preference_rules_removed": (
                    0 if deletion is None else deletion.preference_rules_removed
                ),
                "activity_observations_removed": (
                    0 if deletion is None else deletion.activity_observations_removed
                ),
                "activity_current_removed": (
                    0 if deletion is None else deletion.activity_current_removed
                ),
                "achievement_demand_removed": (
                    0 if deletion is None else deletion.achievement_demand_removed
                ),
                "achievement_player_observations_removed": (
                    0
                    if deletion is None
                    else deletion.achievement_player_observations_removed
                ),
                "achievement_player_current_removed": (
                    0
                    if deletion is None
                    else deletion.achievement_player_current_removed
                ),
                "sync_runs_removed": 0
                if deletion is None
                else deletion.sync_runs_removed,
                "probes_removed": 0 if deletion is None else deletion.probes_removed,
                "consents_removed": 0
                if deletion is None
                else deletion.consents_removed,
                "evidence_removed": 0
                if deletion is None
                else deletion.evidence_removed,
                "orphan_apps_removed": (
                    0 if deletion is None else deletion.orphan_apps_removed
                ),
                "shared_credential_preserved": True,
                "backup_copies_require_separate_deletion": True,
            },
        )
    raise AssertionError("unhandled accounts command")


def _dispatch_auth(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        return _dispatch_auth_locked(args, database_path)


def _dispatch_auth_locked(args: argparse.Namespace, database_path: Path) -> int:
    spec = _CREDENTIAL_PROVIDERS[args.provider]
    credential_ref = _provider_credential_ref(database_path, spec)
    if args.auth_command == "set":
        if args.backend == "file" and not args.yes_file_risk:
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.FILE_STORE_NOT_APPROVED,
                message="Protected-file storage requires --yes-file-risk.",
            )
        store = _credential_store(args.backend)
        store_probe = store.probe()
        if not store_probe.available:
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.CREDENTIAL_STORE_UNAVAILABLE,
                message="The selected credential store is unavailable.",
            )
        if not sys.stdin.isatty():
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.INTERACTIVE_INPUT_REQUIRED,
                message="Credential setup requires a terminal with hidden input.",
            )
        try:
            first = _hidden_input(f"{spec.prompt_label}: ")
            second = _hidden_input(f"Confirm {spec.prompt_label}: ")
        except getpass.GetPassWarning:
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.INTERACTIVE_INPUT_REQUIRED,
                message="Hidden credential input is unavailable in this terminal.",
            )
        if first != second or not _valid_secret_input(first):
            return _emit_error(
                args,
                command="auth.set",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The credential was invalid or confirmation did not match.",
                exit_code=2,
            )
        secret = SecretValue(first)
        with Storage(database_path) as storage:
            existing = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            if existing is not None and existing.backend != args.backend:
                return _emit_error(
                    args,
                    command="auth.set",
                    code=ErrorCode.INVALID_ARGUMENT,
                    message="Remove the existing credential before changing backends.",
                    exit_code=2,
                )
            if existing is not None:
                store = _credential_store(existing.backend, existing.backend_locator)
                store_probe = store.probe()
            previous_secret = store.resolve(credential_ref)
            put_completed = False
            try:
                store.put(credential_ref, secret)
                put_completed = True
                storage.upsert_credential_and_clear_probes(
                    provider=credential_ref.provider,
                    kind=credential_ref.kind,
                    profile_id=credential_ref.profile_id,
                    backend=args.backend,
                    backend_locator=store_probe.backend,
                    configured_at=_utc_now(),
                    capability=spec.dependent_capability,
                )
            except BaseException:
                try:
                    if previous_secret is None:
                        deleted = store.delete(credential_ref)
                        if put_completed and not deleted:
                            raise CredentialError("CREDENTIAL_ROLLBACK_FAILED")
                    else:
                        store.put(credential_ref, previous_secret)
                except CredentialError:
                    # A backend may mutate before reporting a failed put. If
                    # compensation cannot establish the prior/absent state,
                    # rollback failure is the only honest result regardless of
                    # the original backend error.
                    raise CredentialError("CREDENTIAL_ROLLBACK_FAILED") from None
                raise
        return _emit_success(
            args,
            command="auth.set",
            data={
                "provider": args.provider,
                "configured": True,
                "backend": args.backend,
                "secret_included": False,
                "validated": False,
            },
        )
    if args.auth_command == "status":
        with Storage(database_path) as storage:
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
        snapshot = _credential_snapshot(metadata, credential_ref)
        status = (
            CompletenessStatus.COMPLETE
            if snapshot["state"] == "configured"
            else CompletenessStatus.UNAVAILABLE
        )
        warnings = _credential_warnings(
            snapshot["state"], credential_label=spec.display_label
        )
        return _emit_success(
            args,
            command="auth.status",
            completeness_value=completeness(
                status,
                warnings=warnings,
                missing_capabilities=(
                    [spec.missing_capability]
                    if status == CompletenessStatus.UNAVAILABLE
                    else []
                ),
            ),
            data={
                "provider": args.provider,
                "configured": snapshot["state"] == "configured",
                "state": snapshot["state"],
                "backend": snapshot["backend"],
                "protection": snapshot["protection"],
                "secret_included": False,
            },
        )
    if args.auth_command == "probe":
        with Storage(database_path) as storage:
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
        resolved = (
            {"state": "missing", "secret": None, "error_code": None}
            if metadata is None
            else _resolve_credential(metadata, credential_ref)
        )
        if resolved["state"] != "configured":
            if resolved["state"] == "store_locked":
                code = ErrorCode.CREDENTIAL_STORE_LOCKED
                message = "The configured credential store is locked."
            elif resolved["state"] == "store_unavailable":
                code = ErrorCode.CREDENTIAL_STORE_UNAVAILABLE
                message = "The configured credential store is unavailable."
            else:
                code = ErrorCode.AUTH_REQUIRED
                message = f"A {spec.display_label} has not been configured."
            return _emit_error(
                args,
                command="auth.probe",
                code=code,
                message=message,
            )
        now = _utc_now()
        if not _reserve_provider_request(
            args.provider,
            now,
            _AUTH_PROBE_INTERVAL_SECONDS[args.provider],
        ):
            return _emit_error(
                args,
                command="auth.probe",
                code=ErrorCode.REQUEST_THROTTLED,
                message="The local provider request interval has not elapsed.",
                retryable=True,
            )
        try:
            result = _provider_auth_client().probe(
                provider=args.provider,
                api_key=resolved["secret"],
            )
        except ProviderAuthError as exc:
            return _emit_error(
                args,
                command="auth.probe",
                code=exc.code,
                message="The provider credential probe did not succeed.",
                retryable=exc.retryable,
            )
        return _emit_success(
            args,
            command="auth.probe",
            data={
                "provider": args.provider,
                "validation_state": result.state,
                "validated": True,
                "retryable": result.retryable,
                "response_retained": False,
                "secret_included": False,
            },
        )
    if args.auth_command == "remove":
        if not args.yes:
            return _emit_error(
                args,
                command="auth.remove",
                code=ErrorCode.CONFIRMATION_REQUIRED,
                message="Credential removal requires --yes.",
            )
        with Storage(database_path) as storage:
            metadata = storage.get_credential_reference(
                provider=credential_ref.provider,
                kind=credential_ref.kind,
                profile_id=credential_ref.profile_id,
            )
            if metadata is None:
                removed = False
            else:
                store = _credential_store(metadata.backend, metadata.backend_locator)
                try:
                    previous_secret = store.resolve(credential_ref)
                except CredentialError as exc:
                    if exc.code != "CREDENTIAL_READ_FAILED":
                        raise
                    # An unsafe file or undecodable OS-store entry must remain
                    # removable without reading it. It cannot be restored on a
                    # later DB failure because its contents were deliberately
                    # not retained.
                    previous_secret = None
                deleted = store.delete(credential_ref)
                if previous_secret is not None and not deleted:
                    raise CredentialError("CREDENTIAL_DELETE_FAILED")
                try:
                    storage.remove_credential_and_clear_probes(
                        provider=credential_ref.provider,
                        kind=credential_ref.kind,
                        profile_id=credential_ref.profile_id,
                        capability=spec.dependent_capability,
                    )
                except BaseException:
                    if previous_secret is not None:
                        try:
                            store.put(credential_ref, previous_secret)
                        except CredentialError:
                            raise CredentialError(
                                "CREDENTIAL_ROLLBACK_FAILED"
                            ) from None
                    else:
                        raise CredentialError("CREDENTIAL_ROLLBACK_FAILED") from None
                    raise
                removed = True
        data = {
            "provider": args.provider,
            "removed": removed,
            "secret_included": False,
        }
        if args.provider == "steam-web-api":
            data["valve_key_revoked"] = False
        return _emit_success(
            args,
            command="auth.remove",
            data=data,
        )
    raise AssertionError("unhandled auth command")


def _dispatch_owned(args: argparse.Namespace, database_path: Path) -> int:
    with _credential_operation_lock(database_path):
        return _dispatch_owned_locked(args, database_path)


def _dispatch_owned_locked(args: argparse.Namespace, database_path: Path) -> int:
    credential_ref = _steam_credential_ref(database_path)
    with Storage(database_path) as storage:
        try:
            account = storage.get_account(args.account)
        except ValueError:
            return _emit_error(
                args,
                command=f"owned.{args.owned_command}",
                code=ErrorCode.INVALID_ARGUMENT,
                message="The account alias is invalid.",
                exit_code=2,
            )
        metadata = storage.get_credential_reference(
            provider=credential_ref.provider,
            kind=credential_ref.kind,
            profile_id=credential_ref.profile_id,
        )
        if (
            args.owned_command == "probe"
            and account is not None
            and metadata is not None
        ):
            credential = _resolve_credential(metadata, credential_ref)
            if credential["state"] == "configured":
                now = _utc_now()
                if not _reserve_provider_request(
                    "steam-web-api", now, _PROVIDER_MINIMUM_INTERVAL_SECONDS
                ):
                    return _emit_error(
                        args,
                        command="owned.probe",
                        code=ErrorCode.REQUEST_THROTTLED,
                        message="The local provider request interval has not elapsed.",
                        retryable=True,
                    )
                try:
                    result = _steam_web_api_client().probe_visible_owned_games(
                        steamid=account.provider_account_id,
                        api_key=credential["secret"],
                    )
                    probe_state = result.probe_state
                    retryable = result.retryable
                except SteamApiError as exc:
                    probe_state = _provider_probe_state(exc.code)
                    retryable = exc.retryable
                storage.save_provider_probe(
                    capability=_OWNED_CAPABILITY,
                    account_alias=account.alias,
                    probe_state=probe_state,
                    checked_at=now,
                    retryable=retryable,
                )
        probe = (
            None
            if account is None
            else storage.get_provider_probe(
                capability=_OWNED_CAPABILITY, account_alias=account.alias
            )
        )
    capability, completeness_value = _owned_capability_snapshot(
        account=account,
        metadata=metadata,
        probe=probe,
        credential_ref=credential_ref,
    )
    return _emit_success(
        args,
        command=f"owned.{args.owned_command}",
        completeness_value=completeness_value,
        data={"capability": capability},
    )


def _account_steam_root(args: argparse.Namespace) -> Path:
    root = getattr(args, "steam_root", None) or discover_steam_root()
    if root is None:
        raise LocalAccountRegistryUnavailable("Steam account registry unavailable")
    return Path(root)


def _provider_credential_ref(
    database_path: Path, spec: _CredentialProviderSpec
) -> CredentialRef:
    """Scope an opaque provider credential to one local data profile."""
    canonical = str(database_path.expanduser().resolve(strict=False)).encode("utf-8")
    profile_id = f"data-{hashlib.sha256(canonical).hexdigest()[:32]}"
    return CredentialRef(spec.storage_provider, spec.kind, profile_id)


def _steam_credential_ref(database_path: Path) -> CredentialRef:
    return _provider_credential_ref(
        database_path, _CREDENTIAL_PROVIDERS["steam-web-api"]
    )


@contextmanager
def _credential_operation_lock(database_path: Path) -> Iterator[None]:
    """Serialize secret-store mutations and their SQLite metadata per profile."""

    parent = database_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        parent_info = parent.lstat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
    lock_path = parent / ".credential-operation.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(lock_path, flags | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(lock_path, flags, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:
            import msvcrt

            if created:
                os.write(descriptor, b"0")
            else:
                initialization_deadline = time.monotonic() + 5.0
                while os.fstat(descriptor).st_size == 0:
                    if time.monotonic() >= initialization_deadline:
                        raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE")
                    time.sleep(0.05)
            lock_deadline = time.monotonic() + 300.0
            while True:
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    winerror = getattr(exc, "winerror", None)
                    if winerror not in (None, 33) or exc.errno not in (
                        None,
                        errno.EACCES,
                        errno.EAGAIN,
                    ):
                        raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE") from None
                    if time.monotonic() >= lock_deadline:
                        raise CredentialError("CREDENTIAL_STORE_LOCKED") from None
                    time.sleep(0.05)
        yield
    finally:
        if os.name == "nt":
            try:
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(descriptor)


def _credential_store(backend: str, backend_locator: str | None = None) -> Any:
    if backend == "os":
        return NativeKeyringStore(backend_id=backend_locator)
    if backend == "file":
        return ProtectedFileStore(default_credential_dir(), approved=True)
    raise ValueError("unsupported credential backend")


def _credential_snapshot(
    metadata: Any, credential_ref: CredentialRef
) -> dict[str, Any]:
    if metadata is None:
        return {"state": "missing", "backend": None, "protection": None}
    resolved = _resolve_credential(metadata, credential_ref)
    return {
        "state": resolved["state"],
        "backend": metadata.backend,
        "protection": "os" if metadata.backend == "os" else "file",
    }


def _resolve_credential(metadata: Any, credential_ref: CredentialRef) -> dict[str, Any]:
    try:
        secret = _credential_store(metadata.backend, metadata.backend_locator).resolve(
            credential_ref
        )
    except CredentialError as exc:
        state = (
            "store_locked"
            if exc.code == "CREDENTIAL_STORE_LOCKED"
            else "store_unavailable"
        )
        return {"state": state, "secret": None, "error_code": exc.code}
    if secret is None:
        return {"state": "missing", "secret": None, "error_code": None}
    return {"state": "configured", "secret": secret, "error_code": None}


def _credential_warnings(
    state: str, *, credential_label: str = "Steam Web API user key"
) -> list[WarningRecord]:
    if state == "configured":
        return []
    if state == "store_locked":
        return [
            WarningRecord(
                code=ErrorCode.CREDENTIAL_STORE_LOCKED,
                message="The configured credential store is locked.",
            )
        ]
    if state == "store_unavailable":
        return [
            WarningRecord(
                code=ErrorCode.CREDENTIAL_STORE_UNAVAILABLE,
                message="The configured credential store is unavailable.",
            )
        ]
    return [
        WarningRecord(
            code=ErrorCode.AUTH_REQUIRED,
            message=f"A {credential_label} has not been configured.",
        )
    ]


def _owned_capability_snapshot(
    *, account: Any, metadata: Any, probe: Any, credential_ref: CredentialRef
) -> tuple[dict[str, Any], dict[str, Any]]:
    credential = _credential_snapshot(metadata, credential_ref)
    identity_state = "configured" if account is not None else "missing"
    probe_state = "not_checked" if probe is None else probe.probe_state
    if probe is not None and _probe_is_stale(probe.checked_at):
        probe_state = "stale"
    warnings: list[WarningRecord] = []
    missing: list[str] = []
    if identity_state == "missing":
        missing.append("account.identity")
        warnings.append(
            WarningRecord(
                code=ErrorCode.ACCOUNT_NOT_CONFIGURED,
                message="The requested account alias is not configured.",
            )
        )
    if credential["state"] != "configured":
        missing.append("credential:steam_web_api_user_key")
        warnings.extend(_credential_warnings(credential["state"]))
    if not missing and probe_state == "not_checked":
        missing.append(_OWNED_CAPABILITY)
        warnings.append(
            WarningRecord(
                code=ErrorCode.CAPABILITY_NOT_PROBED,
                message="Visible-owned access has not been probed.",
            )
        )
    elif probe_state != "ready" and probe_state != "not_checked":
        missing.append(_OWNED_CAPABILITY)
        warnings.append(_probe_warning(probe_state))
    is_ready = not missing and probe_state == "ready"
    capability = {
        "name": _OWNED_CAPABILITY,
        "support": "supported",
        "interface_status": "official_documented",
        "identity": identity_state,
        "credential": credential["state"],
        "credential_backend": credential["backend"],
        "probe": probe_state,
        "last_checked_at": None if probe is None else probe.checked_at,
        "probe_retryable": None if probe is None else probe.retryable,
        "network_required": True,
        "identifiers_included": False,
        "limitations": [
            "individually_private_games_may_be_omitted",
            "unplayed_free_entitlements_are_not_complete",
        ],
    }
    return capability, completeness(
        CompletenessStatus.COMPLETE if is_ready else CompletenessStatus.UNAVAILABLE,
        missing_capabilities=missing,
        warnings=warnings,
    )


def _probe_warning(state: str) -> WarningRecord:
    mapping = {
        "authentication_failed": (
            ErrorCode.AUTHENTICATION_FAILED,
            "Steam rejected the configured API key.",
        ),
        "data_inaccessible": (
            ErrorCode.OWNED_GAMES_INACCESSIBLE_OR_UNKNOWN_ACCOUNT,
            "Owned-game data was inaccessible or the account response was ambiguous.",
        ),
        "rate_limited": (
            ErrorCode.PROVIDER_RATE_LIMITED,
            "Steam rate-limited the capability probe.",
        ),
        "provider_unavailable": (
            ErrorCode.PROVIDER_UNAVAILABLE,
            "Steam was unavailable during the capability probe.",
        ),
        "contract_changed": (
            ErrorCode.PROVIDER_RESPONSE_INVALID,
            "Steam returned an unsupported response shape.",
        ),
        "invalid_request": (
            ErrorCode.PROVIDER_RESPONSE_INVALID,
            "Steam rejected the capability probe request.",
        ),
        "stale": (
            ErrorCode.CAPABILITY_PROBE_STALE,
            "The last visible-owned capability probe is stale.",
        ),
    }
    code, message = mapping.get(
        state,
        (ErrorCode.PROVIDER_RESPONSE_INVALID, "The provider probe did not complete."),
    )
    return WarningRecord(code=code, message=message)


def _provider_probe_state(code: str) -> str:
    return {
        "AUTHENTICATION_FAILED": "authentication_failed",
        "RATE_LIMITED": "rate_limited",
        "PROVIDER_UNAVAILABLE": "provider_unavailable",
        "INVALID_REQUEST": "invalid_request",
        "PROVIDER_RESPONSE_INVALID": "contract_changed",
    }.get(code, "contract_changed")


def _local_account_error_code(exc: LocalAccountError) -> str:
    if isinstance(exc, AmbiguousLocalAccounts):
        return str(ErrorCode.ACCOUNT_AMBIGUOUS)
    if isinstance(exc, MalformedLocalAccountRegistry):
        return str(ErrorCode.ACCOUNT_REGISTRY_MALFORMED)
    if isinstance(exc, LocalAccountRegistryUnavailable):
        return str(ErrorCode.ACCOUNT_REGISTRY_UNAVAILABLE)
    return str(ErrorCode.ACCOUNT_NOT_CONFIGURED)


def _valid_secret_input(value: str) -> bool:
    return (
        16 <= len(value) <= 4096
        and value.isascii()
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _hidden_input(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def _probe_is_stale(checked_at: str) -> bool:
    checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    return (_utc_now() - checked).total_seconds() > _OWNED_PROBE_FRESHNESS_SECONDS


def _steam_web_api_client() -> SteamWebApiClient:
    return SteamWebApiClient()


def _steam_activity_client() -> SteamActivityApiClient:
    return SteamActivityApiClient()


def _steam_wishlist_client() -> SteamWishlistClient:
    return SteamWishlistClient()


def _gg_deals_client(request_gate: Any) -> GgDealsClient:
    return GgDealsClient(request_gate=request_gate)


def _steam_review_client() -> SteamReviewClient:
    return SteamReviewClient()


def _cheapshark_client(request_gate: Any) -> CheapSharkClient:
    return CheapSharkClient(
        request_gate=request_gate, retry_observer=_defer_cheapshark_requests
    )


def _provider_budget_database_path() -> Path:
    """One OS-user-local request budget shared by every data profile."""

    return default_credential_dir().parent / "provider-request-budget.sqlite3"


def _reserve_provider_request(
    provider: str,
    requested_at: datetime,
    minimum_interval_seconds: float,
) -> bool:
    with Storage(_provider_budget_database_path()) as storage:
        return storage.reserve_provider_request(
            provider=provider,
            budget_scope="user-key",
            requested_at=requested_at,
            minimum_interval_seconds=minimum_interval_seconds,
        )


def _defer_cheapshark_requests(retry_after_seconds: int) -> None:
    with Storage(_provider_budget_database_path()) as storage:
        storage.defer_provider_requests(
            provider="cheapshark",
            budget_scope="user-key",
            requested_at=_utc_now(),
            retry_after_seconds=retry_after_seconds,
        )


def _provider_auth_client() -> ProviderAuthClient:
    return ProviderAuthClient()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _emit_success(
    args: argparse.Namespace,
    *,
    command: str,
    data: dict[str, Any],
    context: dict[str, Any] | None = None,
    completeness_value: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> int:
    envelope = success_envelope(
        command=command,
        data=data,
        context=context,
        completeness_value=completeness_value,
        generated_at=generated_at,
    )
    if args.format == "json":
        print(encode_json(envelope))
    else:
        _print_table(command, envelope)
    return EXIT_OK


def _emit_error(
    args: argparse.Namespace,
    *,
    command: str,
    code: str,
    message: str,
    retryable: bool = False,
    remediation: str | None = None,
    exit_code: int | None = None,
) -> int:
    envelope = error_envelope(
        command=command,
        error=ErrorRecord(
            code=str(code),
            message=message,
            retryable=retryable,
            remediation=remediation,
        ),
    )
    if getattr(args, "format", "json") == "json":
        print(encode_json(envelope))
    else:
        print(f"{code}: {message}", file=sys.stderr)
    if exit_code is not None:
        return exit_code
    return (
        EXIT_UNAVAILABLE
        if code in (ErrorCode.STEAM_NOT_FOUND, ErrorCode.STEAM_ROOT_INACCESSIBLE)
        else EXIT_ERROR
    )


def _command_name(args: argparse.Namespace) -> str:
    parts = [getattr(args, "command", "unknown")]
    for name in (
        "sync_command",
        "games_command",
        "deals_command",
        "accounts_command",
        "auth_command",
        "owned_command",
        "data_command",
        "feedback_command",
        "preferences_command",
        "rule_command",
        "activity_command",
        "achievements_command",
        "system_command",
        "recommendations_command",
        "compatibility_command",
    ):
        value = getattr(args, name, None)
        if value:
            parts.append(value)
    return ".".join(parts)


def _parse_error_format(argv: Sequence[str]) -> str:
    """Honor only syntactically valid requests for table-form parse errors."""

    for index, argument in enumerate(argv):
        if argument == "--format" and index + 1 < len(argv):
            if argv[index + 1] == "table":
                return "table"
        elif argument == "--format=table":
            return "table"
    return "json"


def _warning_source(path: Path | None) -> str | None:
    """Expose only recognized Steam metadata filenames, never directories."""

    if path is None:
        return None
    return path.name if _SAFE_WARNING_SOURCE.fullmatch(path.name) else None


def _installed_read_completeness(root: Path | None) -> dict[str, Any]:
    if root is not None:
        return completeness(CompletenessStatus.COMPLETE)
    invalid_override = bool(os.environ.get("STEAM_AGENT_STEAM_ROOT"))
    return completeness(
        CompletenessStatus.UNAVAILABLE,
        missing_capabilities=["installed.read"],
        warnings=[
            WarningRecord(
                code=(
                    ErrorCode.STEAM_ROOT_INACCESSIBLE
                    if invalid_override
                    else ErrorCode.STEAM_NOT_FOUND
                ),
                message=(
                    "The configured Steam root is missing or inaccessible."
                    if invalid_override
                    else "No default Steam installation was found; pass --steam-root when syncing."
                ),
            )
        ],
    )


def _activity_query_completeness(
    subject: str, result: Mapping[str, Any]
) -> dict[str, Any]:
    """Summarize behavioral evidence without hiding subject-level uncertainty."""

    capability = f"{subject}.read"
    snapshot = result["snapshot"]
    status = snapshot["last_attempt_status"]
    if status is None:
        return completeness(
            CompletenessStatus.UNAVAILABLE,
            missing_capabilities=[capability],
            warnings=[
                WarningRecord(
                    code=ErrorCode.NOT_SYNCED,
                    message=f"{subject.title()} have not been synchronized.",
                )
            ],
        )
    if subject == "activity":
        successful_at = snapshot.get("last_successful_sync_at")
        if status == "running":
            return completeness(
                (
                    CompletenessStatus.UNAVAILABLE
                    if successful_at is None
                    else CompletenessStatus.PARTIAL
                ),
                missing_capabilities=[capability] if successful_at is None else [],
                stale_capabilities=[capability] if successful_at is not None else [],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.SYNC_IN_PROGRESS,
                        message=(
                            "Activity synchronization is in progress."
                            if successful_at is None
                            else "Activity synchronization is in progress; results use the last-good snapshot."
                        ),
                    )
                ],
            )
        if status != "complete":
            if successful_at is None:
                return completeness(
                    CompletenessStatus.UNAVAILABLE,
                    missing_capabilities=[capability],
                    warnings=[
                        WarningRecord(
                            code=snapshot.get("last_error_code")
                            or ErrorCode.STALE_LAST_GOOD,
                            message="The latest activity synchronization failed and no last-good snapshot exists.",
                        )
                    ],
                )
            return completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=[capability],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message="The latest activity synchronization failed; results use the last-good snapshot.",
                    )
                ],
            )
        stale = any(
            item["freshness"]["activity"] != "fresh" for item in result["items"]
        )
        if not stale and successful_at is not None:
            completed = datetime.fromisoformat(successful_at.replace("Z", "+00:00"))
            stale = (_utc_now() - completed).total_seconds() > 6 * 60 * 60
        if stale:
            return completeness(
                CompletenessStatus.PARTIAL,
                stale_capabilities=[capability],
                warnings=[
                    WarningRecord(
                        code=ErrorCode.STALE_LAST_GOOD,
                        message="The activity snapshot is older than the freshness policy.",
                    )
                ],
            )
        return completeness(CompletenessStatus.COMPLETE)

    items = result["items"]
    uncertain = [
        item
        for item in items
        if item["state"] in {"failed", "running", "unevaluated", "expired"}
    ]
    stale = [item for item in items if item.get("freshness") == "stale"]
    if uncertain or stale or status != "complete":
        warnings: list[WarningRecord] = []
        if any(item["state"] == "running" for item in uncertain):
            warnings.append(
                WarningRecord(
                    code=ErrorCode.SYNC_IN_PROGRESS,
                    message="Achievement synchronization is still in progress for some subjects.",
                )
            )
        if any(
            item["state"] in {"failed", "unevaluated", "expired"} for item in uncertain
        ):
            warnings.append(
                WarningRecord(
                    code=ErrorCode.PARTIAL_SCAN,
                    message="Achievement evidence is unavailable for some requested subjects.",
                )
            )
        if stale:
            warnings.append(
                WarningRecord(
                    code=ErrorCode.STALE_LAST_GOOD,
                    message="Some achievement evidence is older than the freshness policy.",
                )
            )
        return completeness(
            CompletenessStatus.PARTIAL,
            stale_capabilities=[capability],
            warnings=warnings,
        )
    return completeness(CompletenessStatus.COMPLETE)


def _table_field(value: object) -> str:
    """Render one physical table field without terminal/control injection."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    escaped: list[str] = []
    for character in str(value):
        if character == "\\":
            escaped.append("\\\\")
        elif character == "\t":
            escaped.append("\\t")
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            escaped.append(f"\\u{ord(character):04x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def _print_table_fields(*values: object) -> None:
    print("\t".join(_table_field(value) for value in values))


def _print_table(command: str, envelope: dict[str, Any]) -> None:
    if command == "compatibility.assess":
        query_completeness = envelope["completeness"]
        _print_table_fields("COMPLETENESS", query_completeness["status"])
        for capability in query_completeness["missing_capabilities"]:
            _print_table_fields("MISSING_CAPABILITY", capability)
        for capability in query_completeness["stale_capabilities"]:
            _print_table_fields("STALE_CAPABILITY", capability)
        for warning in query_completeness["warnings"]:
            _print_table_fields("WARNING", warning["code"], warning["message"])
        _print_table_fields(
            "APPID", "COMPATIBILITY", "PLAYABLE_NOW", "COMPLETENESS", "UNKNOWNS"
        )
        for item in envelope["data"]["results"]:
            _print_table_fields(
                item["appid"],
                item["compatibility"],
                item["playable_now"],
                item["completeness"],
                ",".join(item["unknowns"]),
            )
            if envelope["data"]["explain"]:
                for gate in item["gates"]:
                    _print_table_fields(
                        "GATE",
                        item["appid"],
                        gate["name"],
                        gate["original"],
                        gate["effective"],
                        gate["original_freshness"],
                        gate["override_name"],
                    )
        for subject in envelope["data"]["references"]:
            for reference in subject["items"]:
                _print_table_fields(
                    "REFERENCE",
                    subject["appid"],
                    reference["provider"],
                    reference["purpose"],
                    reference["access_mode"],
                    reference["automation_supported"],
                    reference["url"],
                )
        return
    if command == "system.query":
        query_completeness = envelope["completeness"]
        _print_table_fields("COMPLETENESS", query_completeness["status"])
        for warning in query_completeness["warnings"]:
            _print_table_fields("WARNING", warning["code"], warning["message"])
        profile = envelope["data"]["profile"]
        if profile is None:
            return
        _print_table_fields("SECTION", "FACT", "STATE", "VALUE", "FRESHNESS")
        for section in ("os", "cpu", "memory"):
            for name, item in profile[section].items():
                _print_table_fields(
                    section,
                    name,
                    item["state"],
                    item.get("value", ""),
                    envelope["data"]["freshness"].get(section, "unknown"),
                )
        for section in ("graphics", "storage", "gamepad", "vr"):
            item = profile[section]
            section_freshness = envelope["data"]["freshness"].get(section, "unknown")
            if section == "storage":
                storage_states = [
                    envelope["data"]["freshness"].get(name, "unknown")
                    for name in ("storage_capacity", "storage_available")
                ]
                section_freshness = (
                    "stale"
                    if "stale" in storage_states
                    else "unknown"
                    if "unknown" in storage_states
                    else "fresh"
                )
            _print_table_fields(
                section,
                "summary",
                item["state"],
                item.get("value", ""),
                section_freshness,
            )
        return
    if command == "recommendations.wishlist":
        query_completeness = envelope["completeness"]
        _print_table_fields("COMPLETENESS", query_completeness["status"])
        for warning in query_completeness["warnings"]:
            _print_table_fields("WARNING", warning["code"], warning["message"])
        _print_table_fields(
            "APPID", "NAME", "ELIGIBILITY", "PREFERENCE_FIT", "DEAL", "REVIEW_TOTAL"
        )
        for item in envelope["data"]["ranked"]:
            _print_table_fields(
                item["appid"],
                item["name"],
                item["eligibility"]["state"],
                item["preference_fit"]["score"],
                item["deal_value"]["state"],
                "" if item["review"] is None else item["review"]["total"],
            )
        return
    if command == "recommendations.query":
        query_completeness = envelope["completeness"]
        _print_table_fields("COMPLETENESS", query_completeness["status"])
        for capability in query_completeness["missing_capabilities"]:
            _print_table_fields("MISSING_CAPABILITY", capability)
        for capability in query_completeness["stale_capabilities"]:
            _print_table_fields("STALE_CAPABILITY", capability)
        for warning in query_completeness["warnings"]:
            _print_table_fields("WARNING", warning["code"], warning["message"])
        _print_table_fields(
            "APPID", "NAME", "ELIGIBILITY", "SCORE", "CONFIDENCE", "UNKNOWNS"
        )
        for item in envelope["data"]["results"]:
            _print_table_fields(
                item["appid"],
                item["name"],
                item["eligibility"],
                item["score"],
                item["confidence"],
                ",".join(item["unknowns"]),
            )
            if envelope["data"].get("context", {}).get("explain"):
                _print_table_fields(
                    "FACTORS",
                    item["appid"],
                    ",".join(item["positive_factors"]),
                    ",".join(item["negative_factors"]),
                    ",".join(item["tradeoffs"]),
                )
        return
    if command == "activity.query":
        _print_table_fields("COMPLETENESS", envelope["completeness"]["status"])
        for warning in envelope["completeness"]["warnings"]:
            _print_table_fields("WARNING", warning["code"], warning["message"])
        _print_table_fields(
            "APPID",
            "NAME",
            "LIFETIME_MINUTES",
            "RECENT_MINUTES",
            "LAST_PLAYED_AT",
            "FRESHNESS",
        )
        for item in envelope["data"]["items"]:
            _print_table_fields(
                item["appid"],
                item["name"],
                item["playtime"]["lifetime_minutes"],
                item["playtime"]["recent_window_minutes"],
                item["last_played_at"],
                item["freshness"]["activity"],
            )
        return
    if command == "achievements.query":
        _print_table_fields("COMPLETENESS", envelope["completeness"]["status"])
        for warning in envelope["completeness"]["warnings"]:
            _print_table_fields("WARNING", warning["code"], warning["message"])
        _print_table_fields(
            "APPID", "NAME", "STATE", "TARGETED", "UNLOCKED", "TOTAL", "FRESHNESS"
        )
        for item in envelope["data"]["items"]:
            _print_table_fields(
                item["appid"],
                item["name"],
                item["state"],
                item["targeted"],
                item["summary"]["unlocked"],
                item["summary"]["total"],
                item["summary"]["freshness"],
            )
        return
    if command == "feedback.query":
        _print_table_fields(
            "APPID",
            "RATING",
            "PLAY_STATE",
            "SNOOZED_UNTIL",
            "MINIMUM_SESSION_MINUTES",
            "REMAINING_MINUTES",
            "TRAITS",
        )
        for item in envelope["data"]["items"]:
            traits = ",".join(
                f"{trait['trait']}={trait['value']}" for trait in item["traits"]
            )
            _print_table_fields(
                item["appid"],
                item["rating"],
                item["play_state"],
                item["snooze"]["until"],
                item["estimates"]["minimum_session_minutes"],
                item["estimates"]["remaining_minutes"],
                traits,
            )
        return
    if command == "preferences.rule.list":
        _print_table_fields("TRAIT", "KIND", "STRENGTH", "WEIGHT", "UPDATED_AT")
        for item in envelope["data"]["rules"]:
            _print_table_fields(
                item["trait"],
                item["kind"],
                item["strength"],
                item["weight"],
                item["updated_at"],
            )
        return
    if command == "deals.query":
        query_completeness = envelope["completeness"]
        _print_table_fields("COMPLETENESS", query_completeness["status"])
        for capability in query_completeness["missing_capabilities"]:
            _print_table_fields("MISSING_CAPABILITY", capability)
        for capability in query_completeness["stale_capabilities"]:
            _print_table_fields("STALE_CAPABILITY", capability)
        for warning in query_completeness["warnings"]:
            _print_table_fields("WARNING", warning["code"], warning["message"])
        _print_table_fields(
            "APPID",
            "BUCKET",
            "GRADE",
            "CURRENT_MINOR",
            "LOW_MINOR",
            "FALLBACK_RUNG",
        )
        for item in envelope["data"]["items"]:
            deal = item["deal"]
            current = deal["current_offer"]
            low = deal["historical_low"]
            _print_table_fields(
                item["appid"],
                deal["bucket"],
                deal["evidence_grade"],
                "" if current is None else current["price"]["amount_minor"],
                "" if low is None else low["price"]["amount_minor"],
                deal["fallback_rung"],
            )
        return
    if command == "games.query":
        query_completeness = envelope["completeness"]
        _print_table_fields("COMPLETENESS", query_completeness["status"])
        for capability in query_completeness["missing_capabilities"]:
            _print_table_fields("MISSING_CAPABILITY", capability)
        for capability in query_completeness["stale_capabilities"]:
            _print_table_fields("STALE_CAPABILITY", capability)
        for warning in query_completeness["warnings"]:
            fields = ["WARNING", warning["code"], warning["message"]]
            if warning.get("source"):
                fields.append(warning["source"])
            _print_table_fields(*fields)
        scopes = envelope.get("context", {}).get("scopes", [])
        if scopes == ["installed"] or (
            not scopes and all("state" in item for item in envelope["data"]["items"])
        ):
            _print_table_fields("APPID", "NAME", "STATE", "SIZE")
            for item in envelope["data"]["items"]:
                _print_table_fields(
                    item["appid"], item["name"], item["state"], item["size_bytes"]
                )
        elif scopes == ["owned"]:
            _print_table_fields("APPID", "NAME", "VISIBLE", "BASIS", "PLAYTIME")
            for item in envelope["data"]["items"]:
                _print_table_fields(
                    item["appid"],
                    item["name"],
                    item["visible_in_owned_games"],
                    item["inclusion_basis"],
                    item["playtime_forever_minutes"],
                )
        elif scopes == ["wishlist"]:
            _print_table_fields("APPID", "WISHLISTED", "PRIORITY", "DATE_ADDED")
            for item in envelope["data"]["items"]:
                _print_table_fields(
                    item["appid"],
                    item["wishlisted"],
                    item["priority"],
                    item["date_added_unix"],
                )
        else:
            _print_table_fields(
                "APPID", "NAME", "VISIBLE", "INSTALLED", "TYPE", "PLAYTIME"
            )
            for item in envelope["data"]["items"]:
                _print_table_fields(
                    item["appid"],
                    item["name"],
                    item["visible_in_owned_games"],
                    item["installed"],
                    item["app_type"],
                    item["playtime_forever_minutes"],
                )
        return
    for key, value in envelope["data"].items():
        _print_table_fields(key, value)


__all__ = ["build_parser", "main"]
