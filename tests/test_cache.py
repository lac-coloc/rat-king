from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bk_crowns.cache import RestaurantCache
from bk_crowns.config import Config
from bk_crowns.models import Status
from bk_crowns.state import StateError, StateRepository


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int = 1) -> None:
        self.value += timedelta(seconds=seconds)


def _repository(tmp_path: Path) -> StateRepository:
    return StateRepository(Config(state_dir=tmp_path, output_dir=tmp_path / "site"))


def _publish(repository: StateRepository, restaurant_id: str) -> None:
    store = repository.restaurant(restaurant_id)
    staging = store.create_staging()
    (staging / "raw").mkdir()
    (staging / "normalized.json").write_text("{}")
    (staging / "site").mkdir()
    (staging / "site" / "index.html").write_text(restaurant_id)
    (staging / "site" / "data.json").write_text("{}")
    now = datetime.now(UTC)
    store.publish(
        staging,
        Status(
            last_attempt=now,
            last_success=now,
            snapshot_id=f"snapshot-{restaurant_id}",
        ),
    )


def test_touch_is_persisted_atomically(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _publish(repository, "K2001")
    clock = MutableClock()

    RestaurantCache(repository, maximum=20, clock=clock).touch("K2001")
    reloaded = RestaurantCache(repository, maximum=20, clock=clock)

    assert reloaded.entries()["K2001"].last_access == clock()


def test_missing_index_discovers_existing_valid_snapshots(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _publish(repository, "K2001")

    cache = RestaurantCache(repository, maximum=20)

    assert tuple(cache.entries()) == ("K2001",)
    assert repository.cache_index_path.is_file()


def test_evicts_least_recently_used_but_never_protected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    clock = MutableClock()
    for restaurant_id in ("K2001", "K2002", "K2003"):
        _publish(repository, restaurant_id)
    cache = RestaurantCache(repository, maximum=2, clock=clock)
    for restaurant_id in ("K2001", "K2002", "K2003"):
        cache.touch(restaurant_id)
        clock.advance()

    evicted = cache.evict(protected={"K2001"})

    assert evicted == ("K2002",)
    assert repository.restaurant("K2001").is_ready()
    assert not (repository.restaurants_root / "K2002").exists()
    assert repository.restaurant("K2003").is_ready()


def test_all_protected_entries_can_temporarily_exceed_limit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    for restaurant_id in ("K2001", "K2002"):
        _publish(repository, restaurant_id)
    cache = RestaurantCache(repository, maximum=1)

    assert cache.evict(protected={"K2001", "K2002"}) == ()
    assert set(cache.entries()) == {"K2001", "K2002"}


def test_touch_rejects_absent_snapshot(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    cache = RestaurantCache(repository, maximum=20)

    with pytest.raises(StateError, match="snapshot"):
        cache.touch("K2001")


def test_corrupt_cache_index_does_not_delete_snapshots(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _publish(repository, "K2001")
    repository.cache_index_path.write_text("not-json")

    with pytest.raises(StateError, match="cache"):
        RestaurantCache(repository, maximum=20)

    assert repository.restaurant("K2001").is_ready()


def test_cache_index_ignores_entries_whose_snapshot_was_removed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _publish(repository, "K2001")
    cache = RestaurantCache(repository, maximum=20)
    cache.touch("K2001")
    repository.remove_restaurant("K2001")

    reloaded = RestaurantCache(repository, maximum=20)

    assert reloaded.entries() == {}
