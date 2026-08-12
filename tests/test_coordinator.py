import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bk_crowns.cache import RestaurantCache
from bk_crowns.config import Config
from bk_crowns.coordinator import RefreshCoordinator
from bk_crowns.models import Status
from bk_crowns.state import StateRepository


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, restaurant_id: str) -> None:
        self.calls.append(restaurant_id)


def _config(tmp_path: Path, *, capacity: int = 10, cold_limit: int = 6) -> Config:
    return Config(
        state_dir=tmp_path,
        output_dir=tmp_path / "site",
        refresh_queue_max=capacity,
        cold_loads_per_hour=cold_limit,
    )


def _publish(
    repository: StateRepository,
    restaurant_id: str,
    when: datetime,
    *,
    shared_snapshot_id: str = "shared-1",
) -> None:
    store = repository.restaurant(restaurant_id)
    staging = store.create_staging()
    (staging / "raw").mkdir()
    (staging / "normalized.json").write_text("{}")
    (staging / "site").mkdir()
    (staging / "site" / "index.html").write_text(restaurant_id)
    (staging / "site" / "data.json").write_text("{}")
    store.publish(
        staging,
        Status(
            last_attempt=when,
            last_success=when,
            snapshot_id=f"snapshot-{restaurant_id}",
            shared_snapshot_id=shared_snapshot_id,
        ),
    )


def _coordinator(
    tmp_path: Path,
    *,
    known: tuple[str, ...],
    fresh: tuple[str, ...] = (),
    stale: tuple[str, ...] = (),
    capacity: int = 10,
    cold_limit: int = 6,
    clock: MutableClock | None = None,
    refresh_one: Callable[[str], object] | None = None,
) -> tuple[RefreshCoordinator, Recorder, StateRepository, MutableClock]:
    current_clock = clock or MutableClock()
    config = _config(tmp_path, capacity=capacity, cold_limit=cold_limit)
    repository = StateRepository(config)
    for restaurant_id in fresh:
        _publish(repository, restaurant_id, current_clock())
    for restaurant_id in stale:
        _publish(
            repository,
            restaurant_id,
            current_clock() - timedelta(seconds=config.refresh_interval_seconds + 1),
        )
    recorder = Recorder()
    operation = refresh_one or recorder
    cache = RestaurantCache(repository, config.cache_max_restaurants, clock=current_clock)
    coordinator = RefreshCoordinator(
        config,
        repository,
        cache,
        operation,
        is_known=lambda restaurant_id: restaurant_id in known,
        clock=current_clock,
    )
    return coordinator, recorder, repository, current_clock


def test_fresh_snapshot_is_not_enqueued(tmp_path: Path) -> None:
    coordinator, recorder, _repository, _clock = _coordinator(
        tmp_path, known=("K2001",), fresh=("K2001",)
    )

    decision = coordinator.request("K2001")

    assert (decision.state, decision.accepted) == ("fresh", False)
    assert recorder.calls == []
    assert coordinator.pending_ids() == ()


def test_duplicate_requests_share_one_queue_entry(tmp_path: Path) -> None:
    coordinator, _recorder, _repository, _clock = _coordinator(tmp_path, known=("K2001",))

    first = coordinator.request("K2001")
    second = coordinator.request("K2001")

    assert first.accepted is True
    assert second.state == "queued"
    assert second.accepted is False
    assert coordinator.pending_ids() == ("K2001",)


def test_full_queue_refuses_without_calling_refresh(tmp_path: Path) -> None:
    coordinator, recorder, _repository, _clock = _coordinator(
        tmp_path, known=("K2001", "K2002"), capacity=1
    )

    assert coordinator.request("K2001").accepted
    denied = coordinator.request("K2002")

    assert denied.state == "queue_full"
    assert recorder.calls == []


def test_cold_admissions_use_a_rolling_hour_window(tmp_path: Path) -> None:
    identifiers = tuple(f"K{number:04d}" for number in range(2001, 2008))
    clock = MutableClock()
    coordinator, _recorder, _repository, _clock = _coordinator(
        tmp_path,
        known=identifiers,
        cold_limit=6,
        clock=clock,
    )

    assert all(coordinator.request(item).accepted for item in identifiers[:6])
    denied = coordinator.request(identifiers[6])
    assert denied.state == "rate_limited"
    assert denied.retry_after == clock() + timedelta(hours=1)
    status = coordinator.status(identifiers[6])
    assert status["state"] == "rate_limited"
    assert status["retry_after"] == (clock() + timedelta(hours=1)).isoformat()

    clock.advance(3601)

    assert coordinator.request(identifiers[6]).accepted


def test_stale_snapshot_is_served_while_refresh_is_queued(tmp_path: Path) -> None:
    coordinator, _recorder, repository, _clock = _coordinator(
        tmp_path, known=("K2001",), stale=("K2001",)
    )

    decision = coordinator.request("K2001")

    assert decision.state == "queued_stale"
    assert decision.accepted is True
    assert repository.restaurant("K2001").is_ready()


def test_unknown_id_is_rejected_before_creating_cache_state(tmp_path: Path) -> None:
    coordinator, recorder, repository, _clock = _coordinator(tmp_path, known=("K2001",))

    assert coordinator.request("K2999").state == "not_found"

    assert recorder.calls == []
    assert not (repository.restaurants_root / "K2999").exists()


def test_only_one_worker_executes_catalogue_refreshes(tmp_path: Path) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    guard = threading.Lock()
    concurrent = 0
    maximum_concurrent = 0

    def refresh_one(restaurant_id: str) -> None:
        nonlocal concurrent, maximum_concurrent
        with guard:
            concurrent += 1
            maximum_concurrent = max(maximum_concurrent, concurrent)
        if restaurant_id == "K2001":
            first_started.set()
            assert release_first.wait(2)
        else:
            second_started.set()
        with guard:
            concurrent -= 1

    coordinator, _recorder, _repository, _clock = _coordinator(
        tmp_path,
        known=("K2001", "K2002"),
        refresh_one=refresh_one,
    )
    coordinator.start()
    try:
        coordinator.request("K2001")
        coordinator.request("K2002")
        assert first_started.wait(1)
        assert second_started.is_set() is False
        release_first.set()
        assert second_started.wait(1)
    finally:
        coordinator.stop(timeout=2)

    assert maximum_concurrent == 1
    assert coordinator.protected_ids() == set()


def test_failed_refresh_sets_bounded_retry_backoff(tmp_path: Path) -> None:
    called = threading.Event()

    def fail(_restaurant_id: str) -> None:
        called.set()
        raise RuntimeError("source indisponible")

    coordinator, _recorder, repository, clock = _coordinator(
        tmp_path, known=("K2001",), refresh_one=fail
    )
    coordinator.start()
    coordinator.request("K2001")
    assert called.wait(1)
    coordinator.stop(timeout=2)

    status = repository.restaurant("K2001").load_status()
    assert status.last_error == "source indisponible"
    assert status.retry_after == clock() + timedelta(seconds=30)
    assert coordinator.request("K2001").state == "stopping"


def test_failed_cold_refresh_can_be_readmitted_after_backoff(tmp_path: Path) -> None:
    def fail(_restaurant_id: str) -> None:
        raise RuntimeError("source indisponible")

    coordinator, _recorder, _repository, clock = _coordinator(
        tmp_path, known=("K2001",), refresh_one=fail
    )
    coordinator.start()
    try:
        assert coordinator.request("K2001").accepted is True
        coordinator._queue.join()
        deferred = coordinator.request("K2001")
        assert deferred.state == "backoff"
        assert deferred.retry_after == clock() + timedelta(seconds=30)

        clock.advance(31)

        assert coordinator.request("K2001").accepted is True
    finally:
        coordinator.stop(timeout=2)


def test_stop_refuses_new_admissions_and_joins_worker(tmp_path: Path) -> None:
    coordinator, _recorder, _repository, _clock = _coordinator(tmp_path, known=("K2001",))
    coordinator.start()

    coordinator.stop(timeout=1)

    assert coordinator.request("K2001").state == "stopping"


def test_stop_finishes_active_refresh_without_starting_pending_work(tmp_path: Path) -> None:
    active_started = threading.Event()
    release_active = threading.Event()
    pending_started = threading.Event()

    def refresh_one(restaurant_id: str) -> None:
        if restaurant_id == "K2001":
            active_started.set()
            assert release_active.wait(2)
        else:
            pending_started.set()

    coordinator, _recorder, _repository, _clock = _coordinator(
        tmp_path,
        known=("K2001", "K2002"),
        refresh_one=refresh_one,
    )
    coordinator.start()
    coordinator.request("K2001")
    coordinator.request("K2002")
    assert active_started.wait(1)

    coordinator.request_stop()
    release_active.set()
    coordinator.stop(timeout=2)

    assert pending_started.is_set() is False
