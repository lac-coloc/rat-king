"""HTTP routing and bounded background refresh lifecycle."""

from __future__ import annotations

import json
import logging
import re
import signal
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from .cache import RestaurantCache
from .config import Config
from .coordinator import AdmissionDecision, RefreshCoordinator
from .generator import (
    render_comparator,
    render_home,
    render_restaurant_waiting,
    static_asset,
)
from .refresh import RestaurantRefreshService, SharedRefreshService
from .restaurants import RestaurantDirectory
from .state import StateError, StateRepository

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_THREADS = 32
CLIENT_SOCKET_TIMEOUT_SECONDS = 10.0
_SELECTION = re.compile(r"^/restaurants/(K[0-9]{4})$")
_RESTAURANT_STATUS = re.compile(r"^/api/restaurants/(K[0-9]{4})/status$")
_RESTAURANT_DATA = re.compile(r"^/restaurants/(K[0-9]{4})/data\.json$")
_ASSET = re.compile(r"^/assets/([a-z0-9.]+)$")
_ASSET_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "search.js": "text/javascript; charset=utf-8",
    "waiting.js": "text/javascript; charset=utf-8",
}


class Refresher(Protocol):
    def run(self) -> object: ...


class Coordinator(Protocol):
    def request(self, restaurant_id: str) -> AdmissionDecision: ...

    def status(self, restaurant_id: str) -> dict[str, object]: ...

    def pending_ids(self) -> tuple[str, ...]: ...


class RefreshScheduler:
    """Run shared refresh immediately, then periodically or after backoff."""

    def __init__(
        self,
        refresher: Refresher,
        refresh_interval_seconds: int,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.refresher = refresher
        self.refresh_interval_seconds = refresh_interval_seconds
        self._stop_event = stop_event or threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def failure_delay(consecutive_failures: int) -> float:
        return min(30.0 * (2 ** max(0, consecutive_failures - 1)), 900.0)

    def _wait(self, delay: float) -> bool:
        self._wake_event.wait(delay)
        self._wake_event.clear()
        return not self._stop_event.is_set()

    def run_forever(self) -> None:
        failures = 0
        while not self._stop_event.is_set():
            try:
                self.refresher.run()
                failures = 0
                delay = float(self.refresh_interval_seconds)
            except Exception:
                if self._stop_event.is_set():
                    return
                failures += 1
                delay = self.failure_delay(failures)
                LOGGER.exception("Le refresh des sources publiques a échoué")
            if not self._wait(delay):
                return

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run_forever,
            name="rat-king-shared-refresh",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake_event.set()

    def request_stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def stop(self, timeout: float = 5.0) -> None:
        self.request_stop()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)


class CrownsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = MAX_REQUEST_THREADS

    def __init__(
        self,
        address: tuple[str, int],
        config: Config,
        repository: StateRepository,
        coordinator: Coordinator,
        directory_provider: Callable[[], RestaurantDirectory],
    ) -> None:
        self.config = config
        self.repository = repository
        self.coordinator = coordinator
        self.directory_provider = directory_provider
        self._request_slots = threading.BoundedSemaphore(MAX_REQUEST_THREADS)
        super().__init__(address, CrownsRequestHandler)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(CLIENT_SOCKET_TIMEOUT_SECONDS)
        return request, client_address

    def process_request(self, request, client_address) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class CrownsRequestHandler(BaseHTTPRequestHandler):
    server: CrownsHTTPServer
    server_version = "rat-king"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _send(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(content)
        LOGGER.debug("HTTP response sent with status %d", status)

    def _json(self, status: int, payload: dict[str, object]) -> None:
        content = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
        self._send(status, content, "application/json; charset=utf-8")

    def _method_not_allowed(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed

    def _directory(self) -> RestaurantDirectory | None:
        try:
            return self.server.directory_provider()
        except StateError:
            return None

    def _known_restaurant(self, restaurant_id: str):
        directory = self._directory()
        return None if directory is None else directory.get(restaurant_id)

    def _home(self) -> None:
        status = self.server.repository.shared.status_payload(
            self.server.config.refresh_interval_seconds
        )
        shared = None
        if self.server.repository.shared.is_ready():
            try:
                shared = self.server.repository.load_shared_data()
            except StateError:
                status = {"ready": False, "state": "error"}
        self._send(
            HTTPStatus.OK,
            render_home(shared, status),
            "text/html; charset=utf-8",
        )

    def _search(self, query: str) -> None:
        directory = self._directory()
        if directory is None:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"state": "initializing", "restaurants": []},
            )
            return
        try:
            parameters = parse_qs(query, keep_blank_values=True, max_num_fields=4)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid query"})
            return
        values = parameters.get("q", [])
        if len(values) != 1 or len(values[0]) > 80:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid query"})
            return
        restaurants = [
            {
                **restaurant.to_dict(),
                "url": f"/restaurants/{restaurant.id}",
            }
            for restaurant in directory.search(values[0], self.server.config.search_max_results)
        ]
        self._json(HTTPStatus.OK, {"restaurants": restaurants})

    def _selection(self, restaurant_id: str) -> None:
        restaurant = self._known_restaurant(restaurant_id)
        if restaurant is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        decision = self.server.coordinator.request(restaurant_id)
        store = self.server.repository.restaurant(restaurant_id)
        if store.is_ready():
            try:
                data = self.server.repository.load_restaurant_data(restaurant_id)
                state = str(self.server.coordinator.status(restaurant_id).get("state"))
                content = render_comparator(data, state)
            except StateError:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "data unavailable"})
                return
        else:
            content = render_restaurant_waiting(
                restaurant,
                decision,
                f"/api/restaurants/{restaurant_id}/status",
            )
        self._send(HTTPStatus.OK, content, "text/html; charset=utf-8")

    def _restaurant_status(self, restaurant_id: str) -> None:
        if self._known_restaurant(restaurant_id) is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._json(HTTPStatus.OK, self.server.coordinator.status(restaurant_id))

    def _restaurant_data(self, restaurant_id: str) -> None:
        if self._known_restaurant(restaurant_id) is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        store = self.server.repository.restaurant(restaurant_id)
        if not store.is_ready():
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "data unavailable"})
            return
        path = store.current_path() / "site" / "data.json"
        self._send(HTTPStatus.OK, path.read_bytes(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/readyz":
            ready = self.server.repository.shared.is_ready()
            self._json(
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                {"ready": ready},
            )
            return
        if path == "/api/status":
            payload = self.server.repository.shared.status_payload(
                self.server.config.refresh_interval_seconds
            )
            payload.pop("cache", None)
            payload["cached_restaurant_count"] = len(self.server.repository.cached_restaurant_ids())
            payload["queue_length"] = len(self.server.coordinator.pending_ids())
            self._json(HTTPStatus.OK, payload)
            return
        if path == "/":
            self._home()
            return
        if path == "/api/restaurants":
            self._search(split.query)
            return
        if match := _SELECTION.fullmatch(path):
            self._selection(match.group(1))
            return
        if match := _RESTAURANT_STATUS.fullmatch(path):
            self._restaurant_status(match.group(1))
            return
        if match := _RESTAURANT_DATA.fullmatch(path):
            self._restaurant_data(match.group(1))
            return
        if match := _ASSET.fullmatch(path):
            filename = match.group(1)
            try:
                content = static_asset(filename)
                content_type = _ASSET_TYPES[filename]
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._send(HTTPStatus.OK, content, content_type)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def create_server(
    config: Config,
    repository: StateRepository,
    coordinator: Coordinator,
    directory_provider: Callable[[], RestaurantDirectory],
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> CrownsHTTPServer:
    return CrownsHTTPServer(
        (host, config.port if port is None else port),
        config,
        repository,
        coordinator,
        directory_provider,
    )


def run_http_service(config: Config, host: str) -> None:
    """Bind HTTP first, then refresh shared sources and catalogues politely."""

    repository = StateRepository(config)
    stop_event = threading.Event()
    network_lock = threading.Lock()
    shared_service = SharedRefreshService(config, repository, cancel_event=stop_event)

    class LockedSharedRefresher:
        def run(self) -> object:
            with network_lock:
                return shared_service.run()

    def directory_provider() -> RestaurantDirectory:
        return RestaurantDirectory(repository.load_shared_data().restaurants)

    def refresh_one(restaurant_id: str) -> object:
        with network_lock:
            service = RestaurantRefreshService(
                config,
                repository,
                cancel_event=stop_event,
            )
            return service.run(restaurant_id)

    cache = RestaurantCache(repository, config.cache_max_restaurants)
    coordinator = RefreshCoordinator(
        config,
        repository,
        cache,
        refresh_one,
        is_known=lambda restaurant_id: directory_provider().get(restaurant_id) is not None,
    )
    scheduler = RefreshScheduler(
        LockedSharedRefresher(),
        config.refresh_interval_seconds,
        stop_event,
    )
    server = create_server(
        config,
        repository,
        coordinator,
        directory_provider,
        host=host,
    )
    previous_handlers: dict[signal.Signals, object] = {}

    def terminate(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        scheduler.request_stop()
        coordinator.request_stop()
        threading.Thread(
            target=server.shutdown,
            name="rat-king-shutdown",
            daemon=True,
        ).start()

    def refresh_now(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        scheduler.wake()
        for restaurant_id in repository.cached_restaurant_ids():
            coordinator.request(restaurant_id)

    if threading.current_thread() is threading.main_thread():
        previous_handlers[signal.SIGTERM] = signal.signal(signal.SIGTERM, terminate)
        if hasattr(signal, "SIGUSR1"):
            previous_handlers[signal.SIGUSR1] = signal.signal(signal.SIGUSR1, refresh_now)

    coordinator.start()
    scheduler.start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        scheduler.stop(timeout=config.http_timeout_seconds + 5)
        coordinator.stop(timeout=config.http_timeout_seconds + 5)
        server.server_close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
