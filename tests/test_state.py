import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bk_crowns.config import Config, ConfigError
from bk_crowns.models import (
    CalculationReport,
    Restaurant,
    SharedData,
    SnapshotData,
    Status,
)
from bk_crowns.state import SnapshotStore, StateError, StateRepository


def _status(snapshot_id: str, when: datetime | None = None) -> Status:
    timestamp = when or datetime.now(UTC)
    return Status(
        last_attempt=timestamp,
        last_success=timestamp,
        snapshot_id=snapshot_id,
    )


def _staging(store: SnapshotStore, marker: str, *, complete: bool = True) -> Path:
    staging = store.create_staging()
    (staging / "raw").mkdir()
    (staging / "raw" / "source").write_text(marker)
    (staging / "normalized.json").write_text(json.dumps({"marker": marker}))
    (staging / "site").mkdir()
    (staging / "site" / "index.html").write_text(marker)
    if complete:
        (staging / "site" / "data.json").write_text("{}")
    return staging


def _store(tmp_path: Path, *, public: bool = False) -> SnapshotStore:
    return SnapshotStore(
        tmp_path / "shared",
        ("raw", "normalized.json", "site/index.html", "site/data.json"),
        public_link=tmp_path / "site" if public else None,
    )


def test_snapshot_store_atomically_switches_current_pointer(tmp_path: Path) -> None:
    store = _store(tmp_path, public=True)

    store.publish(_staging(store, "first"), _status("first"))
    store.publish(_staging(store, "second"), _status("second"))

    assert json.loads((store.root / "current.json").read_text()) == {"snapshot_id": "second"}
    assert store.current_path() == store.snapshots_dir / "second"
    assert (tmp_path / "site").is_symlink()
    assert (tmp_path / "site" / "index.html").read_text() == "second"
    assert (store.snapshots_dir / "first" / "site" / "index.html").read_text() == "first"


def test_snapshot_store_retains_only_two_valid_generations(tmp_path: Path) -> None:
    store = _store(tmp_path)

    for snapshot_id in ("one", "two", "three"):
        store.publish(_staging(store, snapshot_id), _status(snapshot_id))

    assert sorted(path.name for path in store.snapshots_dir.iterdir()) == ["three", "two"]
    assert store.current_path().name == "three"


def test_failed_publication_preserves_previous_current_pointer(tmp_path: Path) -> None:
    store = _store(tmp_path, public=True)
    store.publish(_staging(store, "valid"), _status("valid"))

    with pytest.raises(StateError, match="incomplet"):
        store.publish(_staging(store, "broken", complete=False), _status("broken"))

    assert store.current_path().name == "valid"
    assert (tmp_path / "site" / "index.html").read_text() == "valid"
    assert store.load_status().snapshot_id == "valid"


def test_status_failure_rolls_back_pointer_and_public_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, public=True)
    store.publish(_staging(store, "first"), _status("first"))

    def fail_status_write(status: Status) -> None:
        raise OSError("disque indisponible")

    monkeypatch.setattr(store, "write_status", fail_status_write)

    with pytest.raises(OSError, match="disque"):
        store.publish(_staging(store, "second"), _status("second"))

    assert store.current_path().name == "first"
    assert (tmp_path / "site" / "index.html").read_text() == "first"
    assert not (store.snapshots_dir / "second").exists()


def test_readiness_requires_pointer_status_and_files_to_agree(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish(_staging(store, "valid"), _status("valid"))
    assert store.is_ready()

    (store.root / "current.json").write_text('{"snapshot_id":"other"}')

    assert store.is_ready() is False


def test_corrupt_pointer_is_reported_without_deleting_snapshots(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish(_staging(store, "valid"), _status("valid"))
    (store.root / "current.json").write_text("not-json")

    with pytest.raises(StateError, match="pointeur"):
        store.current_path()

    assert (store.snapshots_dir / "valid").is_dir()


def test_record_failure_preserves_success_and_sanitizes_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    successful = _status("valid", datetime.now(UTC) - timedelta(minutes=5))
    store.publish(_staging(store, "valid"), successful)

    store.record_failure(RuntimeError("source indisponible\ndétail interne"))

    status = store.load_status()
    assert status.snapshot_id == "valid"
    assert status.last_success == successful.last_success
    assert status.last_error == "source indisponible détail interne"
    assert status.consecutive_failures == 1
    assert store.is_ready()
    assert store.status_payload(21_600)["state"] == "error"


def _publish_shared(repository: StateRepository, snapshot_id: str = "shared-1") -> None:
    data = SharedData(
        restaurants=(
            Restaurant("K2001", "VILLE EXEMPLE", "10 RUE TEST", "VILLE EXEMPLE", "12345"),
            Restaurant("K2002", "CITÉ DÉMO", "2 AVENUE TEST", "CITÉ DÉMO", "54321"),
        ),
        rewards=(),
        refreshed_at=datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
        source_urls=("https://example.test/restaurants",),
    )
    staging = repository.shared.create_staging()
    (staging / "raw").mkdir()
    (staging / "raw" / "restaurants.json").write_text("{}")
    (staging / "normalized.json").write_text(json.dumps(data.to_dict()))
    (staging / "site").mkdir()
    (staging / "site" / "index.html").write_text("accueil")
    (staging / "site" / "data.json").write_text("{}")
    repository.shared.publish(staging, _status(snapshot_id))


def _publish_restaurant(repository: StateRepository, restaurant_id: str, snapshot_id: str) -> None:
    data = SnapshotData(
        restaurant=Restaurant(
            restaurant_id,
            f"RESTAURANT {restaurant_id}",
            "ADRESSE NEUTRE",
            "VILLE EXEMPLE",
            "12345",
        ),
        refreshed_at=datetime(2026, 8, 10, 8, 5, tzinfo=UTC),
        source_urls=("https://example.test/catalogue",),
        report=CalculationReport((), (), 0.0),
        reward_count=0,
        matched_count=0,
        match_rate=0.0,
        shared_snapshot_id="shared-1",
    )
    store = repository.restaurant(restaurant_id)
    staging = store.create_staging()
    (staging / "raw").mkdir()
    (staging / "raw" / "catalogue.html").write_text("catalogue")
    (staging / "normalized.json").write_text(json.dumps(data.to_dict()))
    (staging / "site").mkdir()
    (staging / "site" / "index.html").write_text(restaurant_id)
    (staging / "site" / "data.json").write_text("{}")
    status = _status(snapshot_id)
    status = Status(
        last_attempt=status.last_attempt,
        last_success=status.last_success,
        snapshot_id=snapshot_id,
        shared_snapshot_id="shared-1",
    )
    store.publish(staging, status)


def _repository(tmp_path: Path) -> StateRepository:
    return StateRepository(Config(state_dir=tmp_path, output_dir=tmp_path / "site"))


def test_repository_uses_independent_safe_restaurant_roots(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    assert repository.restaurant("K2001").root == tmp_path / "restaurants" / "K2001"
    with pytest.raises(ConfigError):
        repository.restaurant("../status")


def test_repository_lists_only_valid_published_restaurants(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _publish_shared(repository)
    _publish_restaurant(repository, "K2001", "restaurant-1")
    _publish_restaurant(repository, "K2002", "restaurant-2")
    (repository.restaurants_root / "not-an-id").mkdir()

    assert repository.cached_restaurant_ids() == ("K2001", "K2002")


def test_repository_deserializes_typed_shared_and_restaurant_data(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _publish_shared(repository)
    _publish_restaurant(repository, "K2001", "restaurant-1")

    shared = repository.load_shared_data()
    selected = repository.load_restaurant_data("K2001")

    assert isinstance(shared, SharedData)
    assert shared.restaurants[0].city == "VILLE EXEMPLE"
    assert isinstance(selected, SnapshotData)
    assert selected.restaurant.id == "K2001"
    assert selected.shared_snapshot_id == "shared-1"


def test_repository_check_is_offline_and_covers_every_current_snapshot(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _publish_shared(repository)
    _publish_restaurant(repository, "K2001", "restaurant-1")

    result = repository.check()

    assert result == {"ready": True, "shared_snapshot": "shared-1", "restaurant_count": 1}


def test_repository_removes_only_a_strict_restaurant_directory(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _publish_restaurant(repository, "K2001", "restaurant-1")
    _publish_restaurant(repository, "K2002", "restaurant-2")

    repository.remove_restaurant("K2001")

    assert not (repository.restaurants_root / "K2001").exists()
    assert (repository.restaurants_root / "K2002").exists()
    with pytest.raises(ConfigError):
        repository.remove_restaurant("../shared")
