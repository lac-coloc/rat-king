"""Independent shared-source and per-restaurant refresh transactions."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Protocol

from .calculator import calculate
from .catalog_parser import CatalogParseError, parse_catalog
from .config import Config, validate_restaurant_id
from .fetch import FetchCancelled, FetchResult, PublicFetcher
from .generator import render_home, render_site
from .matching import match_rewards
from .models import CacheMetadata, SharedData, SnapshotData, Status
from .restaurants import RestaurantDirectory, RestaurantDirectoryError, parse_restaurant_directory
from .rewards_parser import RewardParseError, choose_reward_source
from .state import StateRepository

RAW_FILENAMES = {
    "restaurants": "restaurants.json",
    "catalogue": "catalogue.html",
    "tiers": "rewards-tiers.html",
    "cgu": "rewards-cgu.html",
}


class Fetcher(Protocol):
    def get(
        self,
        resource: str,
        cache: CacheMetadata,
        *,
        restaurant_id: str | None = None,
    ) -> FetchResult: ...


class RefreshError(RuntimeError):
    """Raised when a refresh is incomplete and must not be published."""


@dataclass(frozen=True, slots=True)
class SharedRefreshOutcome:
    snapshot_path: Path
    data: SharedData
    status: Status


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    snapshot_path: Path
    data: SnapshotData
    status: Status


class _RefreshTransaction:
    def __init__(self, cancel_event: Event | None) -> None:
        self.cancel_event = cancel_event

    def ensure_running(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise FetchCancelled("arrêt du refresh demandé")

    @staticmethod
    def cleanup(staging: Path) -> None:
        if staging.exists():
            shutil.rmtree(staging)


class SharedRefreshService(_RefreshTransaction):
    """Refresh the restaurant index and public Kingdom rewards atomically."""

    def __init__(
        self,
        config: Config,
        repository: StateRepository,
        fetcher: Fetcher | None = None,
        *,
        min_reward_count: int = 10,
        min_tier_count: int = 3,
        cancel_event: Event | None = None,
    ) -> None:
        super().__init__(cancel_event)
        self.config = config
        self.repository = repository
        self.fetcher = fetcher or PublicFetcher(config, stop_event=cancel_event)
        self.min_reward_count = min_reward_count
        self.min_tier_count = min_tier_count

    def run(self) -> SharedRefreshOutcome:
        attempted_at = datetime.now(UTC)
        snapshot_id = attempted_at.strftime("shared-%Y%m%dT%H%M%S%fZ")
        store = self.repository.shared
        staging = store.create_staging()
        raw_dir = staging / "raw"
        raw_dir.mkdir()
        previous = store.load_status()
        cache = dict(previous.cache)
        source_urls: list[str] = []

        def fetch(resource: str) -> FetchResult:
            self.ensure_running()
            result = self.fetcher.get(resource, cache.get(resource, CacheMetadata()))
            self.ensure_running()
            filename = RAW_FILENAMES[resource]
            (raw_dir / filename).write_bytes(result.content)
            cache[resource] = CacheMetadata(
                etag=result.metadata.etag,
                last_modified=result.metadata.last_modified,
                raw_path=str(store.snapshots_dir / snapshot_id / "raw" / filename),
            )
            source_urls.append(result.url)
            return result

        try:
            try:
                directory = parse_restaurant_directory(fetch("restaurants").content)
            except RestaurantDirectoryError as exc:
                raise RefreshError(f"liste publique des restaurants invalide : {exc}") from exc

            tiers_result = fetch("tiers")
            try:
                reward_result = choose_reward_source(tiers_result.content, None)
            except RewardParseError:
                cgu_result = fetch("cgu")
                try:
                    reward_result = choose_reward_source(tiers_result.content, cgu_result.content)
                except RewardParseError as exc:
                    raise RefreshError(f"récompenses publiques incomplètes : {exc}") from exc

            rewards = reward_result.rewards
            tier_count = len({reward.crowns for reward in rewards})
            if len(rewards) < self.min_reward_count or tier_count < self.min_tier_count:
                raise RefreshError(
                    "récompenses publiques insuffisantes : "
                    f"{len(rewards)} récompenses et {tier_count} paliers"
                )

            self.ensure_running()
            completed_at = datetime.now(UTC)
            data = SharedData(
                restaurants=directory.restaurants,
                rewards=rewards,
                refreshed_at=completed_at,
                source_urls=tuple(dict.fromkeys(source_urls)),
            )
            (staging / "normalized.json").write_text(
                json.dumps(data.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            site = staging / "site"
            site.mkdir()
            (site / "index.html").write_bytes(render_home(data, {"ready": True, "state": "fresh"}))
            (site / "data.json").write_text(
                json.dumps(data.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            status = Status(
                last_attempt=attempted_at,
                last_success=completed_at,
                snapshot_id=snapshot_id,
                cache=cache,
            )
            snapshot_path = store.publish(staging, status)
            return SharedRefreshOutcome(snapshot_path, data, status)
        except FetchCancelled:
            self.cleanup(staging)
            raise
        except Exception as exc:
            self.cleanup(staging)
            store.record_failure(exc)
            if isinstance(exc, RefreshError):
                raise
            raise RefreshError(str(exc)) from exc


class RestaurantRefreshService(_RefreshTransaction):
    """Refresh one validated restaurant catalog using current shared rewards."""

    def __init__(
        self,
        config: Config,
        repository: StateRepository,
        fetcher: Fetcher | None = None,
        *,
        min_catalog_items: int = 10,
        min_match_count: int = 5,
        min_match_rate: float = 0.20,
        cancel_event: Event | None = None,
    ) -> None:
        super().__init__(cancel_event)
        self.config = config
        self.repository = repository
        self.fetcher = fetcher or PublicFetcher(config, stop_event=cancel_event)
        self.min_catalog_items = min_catalog_items
        self.min_match_count = min_match_count
        self.min_match_rate = min_match_rate

    def run(self, restaurant_id: str) -> RefreshOutcome:
        self.ensure_running()
        safe_id = validate_restaurant_id(restaurant_id)
        shared = self.repository.load_shared_data()
        directory = RestaurantDirectory(shared.restaurants)
        restaurant = directory.get(safe_id)
        if restaurant is None:
            raise RefreshError(f"restaurant {safe_id} absent de la liste publique")

        shared_snapshot_id = self.repository.shared.load_status().snapshot_id
        if shared_snapshot_id is None:
            raise RefreshError("snapshot partagé sans identifiant")
        attempted_at = datetime.now(UTC)
        snapshot_id = attempted_at.strftime(f"{safe_id}-%Y%m%dT%H%M%S%fZ")
        store = self.repository.restaurant(safe_id)
        staging = store.create_staging()
        raw_dir = staging / "raw"
        raw_dir.mkdir()
        previous = store.load_status()
        cache = dict(previous.cache)

        try:
            result = self.fetcher.get(
                "catalogue",
                cache.get("catalogue", CacheMetadata()),
                restaurant_id=safe_id,
            )
            self.ensure_running()
            filename = RAW_FILENAMES["catalogue"]
            (raw_dir / filename).write_bytes(result.content)
            cache["catalogue"] = CacheMetadata(
                etag=result.metadata.etag,
                last_modified=result.metadata.last_modified,
                raw_path=str(store.snapshots_dir / snapshot_id / "raw" / filename),
            )
            try:
                catalog = parse_catalog(result.content.decode("utf-8"))
            except (UnicodeDecodeError, CatalogParseError) as exc:
                raise RefreshError(f"catalogue public invalide : {exc}") from exc
            catalog_count = len(catalog.products) + len(catalog.menus)
            if catalog_count < self.min_catalog_items:
                raise RefreshError(
                    f"catalogue public trop petit : {catalog_count} articles, "
                    f"minimum {self.min_catalog_items}"
                )

            match_report = match_rewards(shared.rewards, catalog)
            if (
                match_report.matched_count < self.min_match_count
                or match_report.match_rate < self.min_match_rate
            ):
                raise RefreshError(
                    "taux d'appariement insuffisant : "
                    f"{match_report.matched_count}/{match_report.total_count} "
                    f"({match_report.match_rate:.1%})"
                )

            calculation = calculate(match_report)
            self.ensure_running()
            completed_at = datetime.now(UTC)
            data = SnapshotData(
                restaurant=restaurant,
                refreshed_at=completed_at,
                source_urls=tuple(dict.fromkeys((*shared.source_urls, result.url))),
                report=calculation,
                reward_count=match_report.total_count,
                matched_count=match_report.matched_count,
                match_rate=match_report.match_rate,
                shared_snapshot_id=shared_snapshot_id,
            )
            (staging / "normalized.json").write_text(
                json.dumps(data.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            render_site(data, staging / "site")
            self.ensure_running()
            status = Status(
                last_attempt=attempted_at,
                last_success=completed_at,
                snapshot_id=snapshot_id,
                cache=cache,
                shared_snapshot_id=shared_snapshot_id,
            )
            snapshot_path = store.publish(staging, status)
            return RefreshOutcome(snapshot_path, data, status)
        except FetchCancelled:
            self.cleanup(staging)
            raise
        except Exception as exc:
            self.cleanup(staging)
            store.record_failure(exc)
            if isinstance(exc, RefreshError):
                raise
            raise RefreshError(str(exc)) from exc
