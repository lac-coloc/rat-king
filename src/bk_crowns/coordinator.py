"""Bounded admission and single-worker catalog refresh coordination."""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta

from .cache import RestaurantCache
from .config import Config, ConfigError, validate_restaurant_id
from .models import Status
from .state import StateRepository

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    state: str
    accepted: bool
    queue_position: int | None = None
    retry_after: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after.astimezone(UTC).isoformat()
        return payload


class RefreshCoordinator:
    """Deduplicate and rate-limit catalog work behind one background thread."""

    def __init__(
        self,
        config: Config,
        repository: StateRepository,
        cache: RestaurantCache,
        refresh_one: Callable[[str], object],
        *,
        is_known: Callable[[str], bool],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.cache = cache
        self.refresh_one = refresh_one
        self.is_known = is_known
        self._clock = clock or (lambda: datetime.now(UTC))
        self._queue: queue.Queue[str | None] = queue.Queue(config.refresh_queue_max)
        self._lock = threading.RLock()
        self._queued: set[str] = set()
        self._queued_stale: dict[str, bool] = {}
        self._pending_order: list[str] = []
        self._active_id: str | None = None
        self._cold_admissions: deque[datetime] = deque()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._accepting = True

    @staticmethod
    def failure_delay(consecutive_failures: int) -> float:
        return min(30.0 * (2 ** max(0, consecutive_failures - 1)), 900.0)

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("coordinator clock must include a timezone")
        return current.astimezone(UTC)

    def _fresh(self, status: Status, now: datetime) -> bool:
        if status.last_success is None:
            return False
        age = (now - status.last_success).total_seconds()
        if age > self.config.refresh_interval_seconds:
            return False
        shared_snapshot = self.repository.shared.load_status().snapshot_id
        return shared_snapshot is None or status.shared_snapshot_id == shared_snapshot

    def _queue_position(self, restaurant_id: str) -> int | None:
        try:
            return self._pending_order.index(restaurant_id) + 1
        except ValueError:
            return None

    def request(self, restaurant_id: str) -> AdmissionDecision:
        try:
            safe_id = validate_restaurant_id(restaurant_id)
        except ConfigError:
            return AdmissionDecision("not_found", False)
        with self._lock:
            if not self._accepting:
                return AdmissionDecision("stopping", False)
            if not self.is_known(safe_id):
                return AdmissionDecision("not_found", False)

            store = self.repository.restaurant(safe_id)
            has_snapshot = store.is_ready()
            status = store.load_status()
            now = self._now()
            if has_snapshot and self._fresh(status, now):
                self.cache.touch(safe_id)
                return AdmissionDecision("fresh", False)

            if safe_id in self._queued:
                state = "queued_stale" if self._queued_stale[safe_id] else "queued"
                return AdmissionDecision(
                    state,
                    False,
                    queue_position=self._queue_position(safe_id),
                )

            if status.retry_after is not None and status.retry_after > now:
                state = "stale_backoff" if has_snapshot else "backoff"
                return AdmissionDecision(state, False, retry_after=status.retry_after)

            if self._queue.full():
                return AdmissionDecision("queue_full", False)

            if not has_snapshot:
                threshold = now - timedelta(hours=1)
                while self._cold_admissions and self._cold_admissions[0] <= threshold:
                    self._cold_admissions.popleft()
                if len(self._cold_admissions) >= self.config.cold_loads_per_hour:
                    retry_after = self._cold_admissions[0] + timedelta(hours=1)
                    return AdmissionDecision("rate_limited", False, retry_after=retry_after)
                self._cold_admissions.append(now)

            self._queued.add(safe_id)
            self._queued_stale[safe_id] = has_snapshot
            self._pending_order.append(safe_id)
            self._queue.put_nowait(safe_id)
            return AdmissionDecision(
                "queued_stale" if has_snapshot else "queued",
                True,
                queue_position=len(self._pending_order),
            )

    def pending_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._pending_order)

    def protected_ids(self) -> set[str]:
        with self._lock:
            protected = set(self._queued)
            if self._active_id is not None:
                protected.add(self._active_id)
            return protected

    def status(self, restaurant_id: str) -> dict[str, object]:
        try:
            safe_id = validate_restaurant_id(restaurant_id)
        except ConfigError:
            return {"ready": False, "state": "not_found"}
        if not self.is_known(safe_id):
            return {"ready": False, "state": "not_found"}
        store = self.repository.restaurant(safe_id)
        payload = store.status_payload(self.config.refresh_interval_seconds)
        with self._lock:
            if self._active_id == safe_id:
                payload["state"] = "refreshing_stale" if store.is_ready() else "refreshing"
            elif safe_id in self._queued:
                payload["state"] = "queued_stale" if self._queued_stale[safe_id] else "queued"
                payload["queue_position"] = self._queue_position(safe_id)
        payload.pop("cache", None)
        return payload

    def _record_worker_failure(self, restaurant_id: str, error: Exception, before: Status) -> None:
        store = self.repository.restaurant(restaurant_id)
        after = store.load_status()
        if after.consecutive_failures <= before.consecutive_failures:
            store.record_failure(error)
            after = store.load_status()
        failures = max(1, after.consecutive_failures)
        retry_after = self._now() + timedelta(seconds=self.failure_delay(failures))
        store.write_status(replace(after, retry_after=retry_after))
        LOGGER.exception("Un refresh public de catalogue a échoué")

    def _run(self) -> None:
        while True:
            if self._stop_event.is_set():
                return
            try:
                restaurant_id = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if restaurant_id is None:
                self._queue.task_done()
                return
            with self._lock:
                self._active_id = restaurant_id
            store = self.repository.restaurant(restaurant_id)
            before = store.load_status()
            try:
                self.refresh_one(restaurant_id)
                if store.is_ready():
                    with self._lock:
                        self.cache.touch(restaurant_id)
            except Exception as exc:
                self._record_worker_failure(restaurant_id, exc, before)
            finally:
                with self._lock:
                    self._active_id = None
                    self._queued.discard(restaurant_id)
                    self._queued_stale.pop(restaurant_id, None)
                    if restaurant_id in self._pending_order:
                        self._pending_order.remove(restaurant_id)
                    self.cache.evict(self.protected_ids())
                self._queue.task_done()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._accepting:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="rat-king-catalogue-worker",
                daemon=True,
            )
            self._thread.start()

    def request_stop(self) -> None:
        with self._lock:
            self._accepting = False
            self._stop_event.set()
            with suppress(queue.Full):
                self._queue.put_nowait(None)

    def stop(self, timeout: float = 5.0) -> None:
        self.request_stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            self._queued.clear()
            self._queued_stale.clear()
            self._pending_order.clear()
            if thread is None or not thread.is_alive():
                self._active_id = None
