"""Typed domain models shared by parsing, matching, calculation, and output."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


class Serializable:
    """Mixin returning JSON-safe dictionaries for immutable dataclasses."""

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Restaurant(Serializable):
    id: str
    name: str
    address: str
    city: str = ""
    postal_code: str = ""


@dataclass(frozen=True, slots=True)
class Category(Serializable):
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Product(Serializable):
    id: str
    plu: str
    name: str
    route_id: str
    available: bool
    active: bool
    prices: dict[str, int]
    category_ids: tuple[str, ...] = ()
    kind: str = "produit"


@dataclass(frozen=True, slots=True)
class MenuSize(Serializable):
    plu: str
    size: str
    price: int


@dataclass(frozen=True, slots=True)
class Menu(Serializable):
    id: str
    plu: str
    name: str
    route_id: str
    available: bool
    active: bool
    sizes: tuple[MenuSize, ...]
    category_ids: tuple[str, ...] = ()
    kind: str = "menu"


@dataclass(frozen=True, slots=True)
class Catalog(Serializable):
    categories: tuple[Category, ...]
    products: tuple[Product, ...]
    menus: tuple[Menu, ...]


@dataclass(frozen=True, slots=True)
class Reward(Serializable):
    crowns: int
    name: str
    kind: str
    menu_size: str | None
    seasonal: bool
    available: bool
    source_url: str


@dataclass(frozen=True, slots=True)
class SharedData(Serializable):
    restaurants: tuple[Restaurant, ...]
    rewards: tuple[Reward, ...]
    refreshed_at: datetime
    source_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchResult(Serializable):
    reward: Reward
    status: str
    candidate_name: str | None = None
    candidate_id: str | None = None
    candidate_kind: str | None = None
    price_cents: int | None = None
    method: str | None = None
    diagnostic: str | None = None
    suggestions: tuple[str, ...] = ()
    candidate_available: bool | None = None
    candidate_active: bool | None = None


@dataclass(frozen=True, slots=True)
class MatchReport(Serializable):
    results: tuple[MatchResult, ...]
    matched_count: int
    total_count: int
    match_rate: float


@dataclass(slots=True)
class RankedReward(Serializable):
    reward: Reward
    candidate_name: str
    candidate_kind: str
    price_cents: int
    price_euros: float
    euros_per_crown: float
    required_spend: float
    yield_percent: float
    catalog_available: bool = True
    catalog_active: bool = True
    match_method: str | None = None
    dominated: bool = False
    domination_note: str | None = None


@dataclass(frozen=True, slots=True)
class CalculationReport(Serializable):
    rows: tuple[RankedReward, ...]
    diagnostics: tuple[MatchResult, ...]
    match_rate: float


@dataclass(frozen=True, slots=True)
class CacheMetadata(Serializable):
    etag: str | None = None
    last_modified: str | None = None
    raw_path: str | None = None


@dataclass(frozen=True, slots=True)
class Status(Serializable):
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    last_error: str | None = None
    snapshot_id: str | None = None
    cache: dict[str, CacheMetadata] = field(default_factory=dict)
    shared_snapshot_id: str | None = None
    retry_after: datetime | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True)
class SnapshotData(Serializable):
    restaurant: Restaurant
    refreshed_at: datetime
    source_urls: tuple[str, ...]
    report: CalculationReport
    reward_count: int
    matched_count: int
    match_rate: float
    shared_snapshot_id: str = ""
