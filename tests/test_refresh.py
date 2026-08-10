import json
import threading
from pathlib import Path

import pytest

from bk_crowns.config import Config
from bk_crowns.fetch import PUBLIC_URLS, FetchCancelled, FetchError, FetchResult
from bk_crowns.models import CacheMetadata
from bk_crowns.refresh import (
    RefreshError,
    RestaurantRefreshService,
    SharedRefreshService,
)
from bk_crowns.state import StateRepository

FIXTURES = Path(__file__).parent / "fixtures"


class FakeFetcher:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, str | None]] = []
        self.fail_on: str | None = None

    def get(
        self,
        resource: str,
        cache: CacheMetadata,
        *,
        restaurant_id: str | None = None,
    ) -> FetchResult:
        del cache
        self.calls.append((resource, restaurant_id))
        if resource == self.fail_on:
            raise FetchError(f"échec injecté sur {resource}")
        content = self.payloads[resource]
        url = PUBLIC_URLS[resource]
        if restaurant_id is not None:
            url = url.format(restaurant_id=restaurant_id)
        return FetchResult(
            resource=resource,
            url=url,
            content=content,
            metadata=CacheMetadata(etag=f'"{resource}-v1"'),
            not_modified=False,
        )


def _restaurants() -> bytes:
    return json.dumps(
        {
            "restaurants": [
                {
                    "id": "K2001",
                    "name": "BURGER KING VILLE EXEMPLE CENTRE",
                    "address": "10 RUE DU TEST - 12345 VILLE EXEMPLE",
                },
                {
                    "id": "K2002",
                    "name": "BURGER KING CITÉ DÉMO GARE",
                    "address": "2 AVENUE NEUTRE - 54321 CITÉ DÉMO",
                },
            ]
        },
        ensure_ascii=False,
    ).encode()


def _fixture_payloads() -> dict[str, bytes]:
    return {
        "restaurants": _restaurants(),
        "catalogue": (FIXTURES / "catalogue-reduced.html").read_bytes(),
        "tiers": (FIXTURES / "rewards-primary-reduced.html").read_bytes(),
        "cgu": (FIXTURES / "rewards-cgu-reduced.html").read_bytes(),
    }


def _repository(tmp_path: Path) -> tuple[Config, StateRepository]:
    config = Config(state_dir=tmp_path, output_dir=tmp_path / "site")
    return config, StateRepository(config)


def _shared_service(
    config: Config,
    repository: StateRepository,
    fetcher: FakeFetcher,
    *,
    cancel_event: threading.Event | None = None,
) -> SharedRefreshService:
    return SharedRefreshService(
        config,
        repository,
        fetcher,
        min_reward_count=10,
        min_tier_count=3,
        cancel_event=cancel_event,
    )


def _restaurant_service(
    config: Config,
    repository: StateRepository,
    fetcher: FakeFetcher,
    *,
    min_match_rate: float = 0.05,
    cancel_event: threading.Event | None = None,
) -> RestaurantRefreshService:
    return RestaurantRefreshService(
        config,
        repository,
        fetcher,
        min_catalog_items=1,
        min_match_count=1,
        min_match_rate=min_match_rate,
        cancel_event=cancel_event,
    )


def _prepare_shared(config: Config, repository: StateRepository, fetcher: FakeFetcher) -> None:
    _shared_service(config, repository, fetcher).run()
    fetcher.calls.clear()


def test_shared_refresh_publishes_directory_and_rewards_without_catalogue(
    tmp_path: Path,
) -> None:
    config, repository = _repository(tmp_path)
    fetcher = FakeFetcher(_fixture_payloads())

    outcome = _shared_service(config, repository, fetcher).run()

    assert fetcher.calls == [
        ("restaurants", None),
        ("tiers", None),
        ("cgu", None),
    ]
    assert tuple(item.id for item in outcome.data.restaurants) == ("K2001", "K2002")
    assert len(outcome.data.rewards) == 15
    assert repository.shared.is_ready()
    assert (outcome.snapshot_path / "raw" / "restaurants.json").is_file()
    assert not (outcome.snapshot_path / "raw" / "catalogue.html").exists()
    published_home = (outcome.snapshot_path / "site" / "index.html").read_text()
    assert "Sources publiques disponibles" in published_home
    assert 'id="restaurant-search"' in published_home


def test_restaurant_refresh_fetches_only_selected_public_catalogue(tmp_path: Path) -> None:
    config, repository = _repository(tmp_path)
    fetcher = FakeFetcher(_fixture_payloads())
    _prepare_shared(config, repository, fetcher)

    outcome = _restaurant_service(config, repository, fetcher).run("K2002")

    assert fetcher.calls == [("catalogue", "K2002")]
    assert outcome.data.restaurant.id == "K2002"
    assert outcome.data.reward_count == 15
    assert outcome.data.matched_count >= 1
    assert outcome.status.shared_snapshot_id == repository.shared.load_status().snapshot_id
    assert repository.restaurant("K2002").is_ready()


def test_unknown_restaurant_never_calls_catalogue(tmp_path: Path) -> None:
    config, repository = _repository(tmp_path)
    fetcher = FakeFetcher(_fixture_payloads())
    _prepare_shared(config, repository, fetcher)

    with pytest.raises(RefreshError, match="absent"):
        _restaurant_service(config, repository, fetcher).run("K2999")

    assert fetcher.calls == []
    assert not (repository.restaurants_root / "K2999").exists()


def test_failed_shared_refresh_keeps_last_valid_shared_snapshot(tmp_path: Path) -> None:
    config, repository = _repository(tmp_path)
    fetcher = FakeFetcher(_fixture_payloads())
    first = _shared_service(config, repository, fetcher).run()
    fetcher.fail_on = "tiers"

    with pytest.raises(RefreshError, match="tiers"):
        _shared_service(config, repository, fetcher).run()

    assert repository.shared.current_path() == first.snapshot_path
    assert repository.shared.is_ready()
    assert repository.shared.status_payload(21_600)["state"] == "error"


def test_failed_restaurant_refresh_keeps_its_last_valid_snapshot(tmp_path: Path) -> None:
    config, repository = _repository(tmp_path)
    fetcher = FakeFetcher(_fixture_payloads())
    _prepare_shared(config, repository, fetcher)
    first = _restaurant_service(config, repository, fetcher).run("K2001")
    fetcher.fail_on = "catalogue"

    with pytest.raises(RefreshError, match="catalogue"):
        _restaurant_service(config, repository, fetcher).run("K2001")

    store = repository.restaurant("K2001")
    assert store.current_path() == first.snapshot_path
    assert store.is_ready()
    assert store.status_payload(21_600)["state"] == "error"


def test_restaurant_refresh_rejects_low_match_rate_without_publishing(
    tmp_path: Path,
) -> None:
    config, repository = _repository(tmp_path)
    fetcher = FakeFetcher(_fixture_payloads())
    _prepare_shared(config, repository, fetcher)

    with pytest.raises(RefreshError, match="appariement"):
        _restaurant_service(config, repository, fetcher, min_match_rate=0.99).run("K2001")

    assert repository.restaurant("K2001").is_ready() is False


def test_shared_refresh_rejects_empty_directory_without_partial_publication(
    tmp_path: Path,
) -> None:
    config, repository = _repository(tmp_path)
    payloads = _fixture_payloads()
    payloads["restaurants"] = b'{"restaurants":[]}'

    with pytest.raises(RefreshError, match="restaurants"):
        _shared_service(config, repository, FakeFetcher(payloads)).run()

    assert repository.shared.is_ready() is False


def test_cancelled_shared_refresh_cleans_staging_without_recording_error(
    tmp_path: Path,
) -> None:
    config, repository = _repository(tmp_path)
    fetcher = FakeFetcher(_fixture_payloads())
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(FetchCancelled):
        _shared_service(config, repository, fetcher, cancel_event=stop_event).run()

    assert fetcher.calls == []
    assert not repository.shared.status_path.exists()
    assert not list(repository.shared.root.glob(".refresh-*"))
