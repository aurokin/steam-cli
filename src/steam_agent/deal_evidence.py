"""Provider-neutral, memory-only deal evidence contracts.

These records deliberately separate a Steam application identity from an
external offer.  An offer found by exact AppID can still be only game-level
comparable when the provider does not expose edition, DRM, or region details.
No record carries a raw provider response.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Comparability = Literal["exact_product", "normalized_game", "unknown"]
StoreClass = Literal["official", "keyshop", "unknown"]


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    provider_product_id: str
    steam_appid: int
    product_kind: Literal["app"] = "app"
    mapping: Literal["exact"] = "exact"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.steam_appid, int)
            or isinstance(self.steam_appid, bool)
            or not 1 <= self.steam_appid <= (1 << 32) - 1
        ):
            raise ValueError("steam_appid must be an unsigned 32-bit integer")
        if not self.provider_product_id:
            raise ValueError("provider_product_id must not be empty")


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str
    country: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.amount_minor, int)
            or isinstance(self.amount_minor, bool)
            or self.amount_minor < 0
            or self.amount_minor > (1 << 63) - 1
        ):
            raise ValueError("amount_minor must be a non-negative 64-bit integer")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError("currency must be an uppercase ISO-style code")
        if len(self.country) != 2 or not self.country.isupper():
            raise ValueError("country must be an uppercase country code")


@dataclass(frozen=True, slots=True)
class ManualReference:
    url: str
    purpose: str
    access_mode: Literal["manual_only"] = "manual_only"
    automation_supported: Literal[False] = False

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError("manual reference must use HTTPS")
        if not self.purpose:
            raise ValueError("manual reference purpose must not be empty")


@dataclass(frozen=True, slots=True)
class OfferEvidence:
    provider: str
    product: ProductIdentity
    price: Money
    regular_price: Money | None
    discount_percent: int | None
    store_class: StoreClass
    observed_at: str
    provider_url: ManualReference
    comparability: Comparability


@dataclass(frozen=True, slots=True)
class HistoricalLowSummary:
    provider: str
    product: ProductIdentity
    price: Money
    observed_at: str
    effective_at: str | None
    scope: str
    provider_url: ManualReference
    comparability: Comparability


@dataclass(frozen=True, slots=True)
class DealEvidenceSnapshot:
    provider: str
    product: ProductIdentity
    offers: tuple[OfferEvidence, ...]
    history_lows: tuple[HistoricalLowSummary, ...]
    observed_at: str
    limitations: tuple[str, ...]


__all__ = [
    "Comparability",
    "DealEvidenceSnapshot",
    "HistoricalLowSummary",
    "ManualReference",
    "Money",
    "OfferEvidence",
    "ProductIdentity",
    "StoreClass",
]
