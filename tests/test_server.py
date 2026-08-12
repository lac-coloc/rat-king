import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from bk_crowns.config import Config
from bk_crowns.coordinator import AdmissionDecision
from bk_crowns.generator import render_home, render_site
from bk_crowns.models import CalculationReport, Restaurant, SharedData, SnapshotData, Status
from bk_crowns.restaurants import RestaurantDirectory
from bk_crowns.server import (
    CLIENT_SOCKET_TIMEOUT_SECONDS,
    MAX_REQUEST_THREADS,
    CrownsHTTPServer,
    CrownsRequestHandler,
    RefreshScheduler,
)
from bk_crowns.state import StateRepository


def _config(tmp_path: Path) -> Config:
    return Config(state_dir=tmp_path, output_dir=tmp_path / "site", port=8080)


def _shared_data() -> SharedData:
    return SharedData(
        restaurants=(
            Restaurant(
                "K2001",
                "BURGER KING VILLE EXEMPLE CENTRE",
                "10 RUE DU TEST - 12345 VILLE EXEMPLE",
                "VILLE EXEMPLE",
                "12345",
            ),
            Restaurant(
                "K2002",
                "BURGER KING CITÉ DÉMO GARE",
                "2 AVENUE NEUTRE - 54321 CITÉ DÉMO",
                "CITÉ DÉMO",
                "54321",
            ),
        ),
        rewards=(),
        refreshed_at=datetime.now(UTC),
        source_urls=("https://example.test/public",),
    )


def _publish_shared(repository: StateRepository) -> None:
    data = _shared_data()
    store = repository.shared
    staging = store.create_staging()
    (staging / "raw").mkdir()
    (staging / "raw" / "restaurants.json").write_text("{}")
    (staging / "normalized.json").write_text(json.dumps(data.to_dict()))
    (staging / "site").mkdir()
    (staging / "site" / "index.html").write_bytes(
        render_home(data, {"ready": True, "state": "fresh"})
    )
    (staging / "site" / "data.json").write_text("{}")
    now = datetime.now(UTC)
    store.publish(staging, Status(now, now, None, "shared-1"))


def _publish_restaurant(
    repository: StateRepository,
    restaurant_id: str,
    *,
    stale: bool = False,
) -> None:
    restaurant = next(item for item in _shared_data().restaurants if item.id == restaurant_id)
    refreshed_at = datetime.now(UTC)
    if stale:
        refreshed_at -= timedelta(seconds=21_601)
    data = SnapshotData(
        restaurant=restaurant,
        refreshed_at=refreshed_at,
        source_urls=("https://example.test/public",),
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
    render_site(data, staging / "site")
    store.publish(
        staging,
        Status(
            last_attempt=refreshed_at,
            last_success=refreshed_at,
            snapshot_id=f"snapshot-{restaurant_id}",
            shared_snapshot_id="shared-1",
        ),
    )


class FakeCoordinator:
    def __init__(self, repository: StateRepository, config: Config) -> None:
        self.repository = repository
        self.config = config
        self.requests: list[str] = []
        self.actual_admissions: list[str] = []
        self._admitted: set[str] = set()

    def request(self, restaurant_id: str) -> AdmissionDecision:
        self.requests.append(restaurant_id)
        store = self.repository.restaurant(restaurant_id)
        payload = store.status_payload(self.config.refresh_interval_seconds)
        if payload["state"] == "fresh":
            return AdmissionDecision("fresh", False)
        stale = store.is_ready()
        if restaurant_id in self._admitted:
            return AdmissionDecision("queued_stale" if stale else "queued", False, 1)
        self._admitted.add(restaurant_id)
        self.actual_admissions.append(restaurant_id)
        return AdmissionDecision("queued_stale" if stale else "queued", True, 1)

    def status(self, restaurant_id: str) -> dict[str, object]:
        store = self.repository.restaurant(restaurant_id)
        payload = store.status_payload(self.config.refresh_interval_seconds)
        if restaurant_id in self._admitted and not store.is_ready():
            payload["state"] = "queued"
        return payload

    def pending_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._admitted))


class DirectResponse:
    def __init__(self, raw: bytes) -> None:
        head, self.content = raw.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        self.status_code = int(lines[0].split()[1])
        self.headers = {
            key.strip(): value.strip()
            for key, value in (line.split(":", 1) for line in lines[1:] if ":" in line)
        }

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> object:
        return json.loads(self.content)


class DirectClient:
    def __init__(self, server: object) -> None:
        self.server = server

    def _request(self, method: str, path: str) -> DirectResponse:
        handler = object.__new__(CrownsRequestHandler)
        handler.server = self.server
        handler.path = path
        handler.command = method
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.wfile = BytesIO()
        handler._headers_buffer = []
        getattr(handler, f"do_{method}")()
        return DirectResponse(handler.wfile.getvalue())

    def get(self, path: str, params: dict[str, str] | None = None) -> DirectResponse:
        if params:
            path = f"{path}?{urlencode(params)}"
        return self._request("GET", path)

    def post(self, path: str) -> DirectResponse:
        return self._request("POST", path)


@contextmanager
def _running_server(
    tmp_path: Path,
    *,
    shared: bool = True,
    restaurant: str | None = None,
    stale: bool = False,
):
    config = _config(tmp_path)
    repository = StateRepository(config)
    if shared:
        _publish_shared(repository)
    if restaurant is not None:
        _publish_restaurant(repository, restaurant, stale=stale)
    coordinator = FakeCoordinator(repository, config)

    def directory_provider() -> RestaurantDirectory:
        return RestaurantDirectory(repository.load_shared_data().restaurants)

    server = SimpleNamespace(
        config=config,
        repository=repository,
        coordinator=coordinator,
        directory_provider=directory_provider,
    )
    client = DirectClient(server)
    try:
        yield SimpleNamespace(
            config=config,
            repository=repository,
            coordinator=coordinator,
            server=server,
            client=client,
        )
    finally:
        pass


def test_process_is_healthy_but_not_ready_before_shared_snapshot(tmp_path: Path) -> None:
    with _running_server(tmp_path, shared=False) as runtime:
        assert runtime.client.get("/healthz").json() == {"status": "ok"}
        assert runtime.client.get("/readyz").status_code == 503
        home = runtime.client.get("/")
        assert home.status_code == 200
        assert "Initialisation des sources publiques" in home.text
        assert "restaurant-search" in home.text
        assert runtime.client.get("/api/restaurants", params={"q": "ville"}).status_code == 503
        assert runtime.client.get("/api/status").json()["state"] == "initializing"


def test_home_searches_locally_by_city_and_typo_without_admission(tmp_path: Path) -> None:
    with _running_server(tmp_path) as runtime:
        response = runtime.client.get("/api/restaurants", params={"q": "vile exemple"})

        assert response.status_code == 200
        assert response.json()["restaurants"][0] == {
            "id": "K2001",
            "name": "BURGER KING VILLE EXEMPLE CENTRE",
            "address": "10 RUE DU TEST - 12345 VILLE EXEMPLE",
            "city": "VILLE EXEMPLE",
            "postal_code": "12345",
            "url": "/restaurants/K2001",
        }
        assert runtime.coordinator.requests == []


def test_cold_selection_enqueues_once_and_returns_waiting_page(tmp_path: Path) -> None:
    with _running_server(tmp_path) as runtime:
        first = runtime.client.get("/restaurants/K2001")
        second = runtime.client.get("/restaurants/K2001")

        assert first.status_code == second.status_code == 200
        assert "Le comparateur se prépare" in first.text
        assert runtime.coordinator.requests == ["K2001", "K2001"]
        assert runtime.coordinator.actual_admissions == ["K2001"]


def test_valid_selection_serves_comparator_data_and_fixed_assets(tmp_path: Path) -> None:
    with _running_server(tmp_path, restaurant="K2001") as runtime:
        page = runtime.client.get("/restaurants/K2001")
        data = runtime.client.get("/restaurants/K2001/data.json")
        asset = runtime.client.get("/assets/app.css")

        assert page.status_code == 200
        assert "Classement général" in page.text
        assert data.status_code == 200
        assert data.json()["restaurant"]["id"] == "K2001"
        assert asset.status_code == 200
        assert "--cream" in asset.text


def test_stale_snapshot_is_served_while_refresh_is_admitted(tmp_path: Path) -> None:
    with _running_server(tmp_path, restaurant="K2001", stale=True) as runtime:
        response = runtime.client.get("/restaurants/K2001")

        assert response.status_code == 200
        assert "Données anciennes" in response.text
        assert runtime.coordinator.actual_admissions == ["K2001"]


def test_status_and_data_reads_never_enqueue(tmp_path: Path) -> None:
    with _running_server(tmp_path, restaurant="K2001") as runtime:
        assert runtime.client.get("/api/restaurants/K2001/status").status_code == 200
        assert runtime.client.get("/restaurants/K2001/data.json").status_code == 200

        assert runtime.coordinator.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "/restaurants/https:%2F%2Fevil.example",
        "/restaurants/%2e%2e",
        "/restaurants/K2999",
        "/restaurants/K2001/../../shared/status.json",
    ],
)
def test_invalid_or_unknown_selection_never_enqueues(tmp_path: Path, path: str) -> None:
    with _running_server(tmp_path) as runtime:
        assert runtime.client.get(path).status_code == 404
        assert runtime.coordinator.requests == []


def test_request_log_omits_ip_query_and_selected_restaurant(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="bk_crowns.server")
    with _running_server(tmp_path) as runtime:
        runtime.client.get("/api/restaurants", params={"q": "terme privé"})
        runtime.client.get("/restaurants/K2001")

    assert "terme privé" not in caplog.text
    assert "K2001" not in caplog.text
    assert "127.0.0.1" not in caplog.text


def test_unknown_assets_and_mutating_methods_are_rejected(tmp_path: Path) -> None:
    with _running_server(tmp_path) as runtime:
        assert runtime.client.get("/assets/../templates/home.html.j2").status_code == 404
        assert runtime.client.post("/restaurants/K2001").status_code == 405
        assert runtime.client.get("/refresh").status_code == 404


def test_http_server_refuses_work_above_its_thread_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = object.__new__(CrownsHTTPServer)
    server._request_slots = threading.BoundedSemaphore(1)
    accepted: list[object] = []
    rejected: list[object] = []
    monkeypatch.setattr(
        ThreadingHTTPServer,
        "process_request",
        lambda _server, request, _address: accepted.append(request),
    )
    monkeypatch.setattr(
        ThreadingHTTPServer,
        "shutdown_request",
        lambda _server, request: rejected.append(request),
    )

    first = object()
    second = object()
    server.process_request(first, ("127.0.0.1", 1))
    server.process_request(second, ("127.0.0.1", 2))

    assert MAX_REQUEST_THREADS == 32
    assert accepted == [first]
    assert rejected == [second]


def test_http_server_sets_a_timeout_on_accepted_client_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocket:
        timeout: float | None = None

        def settimeout(self, value: float) -> None:
            self.timeout = value

    client = FakeSocket()
    server = object.__new__(CrownsHTTPServer)
    monkeypatch.setattr(
        ThreadingHTTPServer,
        "get_request",
        lambda _server: (client, ("127.0.0.1", 1)),
    )

    returned, _address = server.get_request()

    assert returned is client
    assert client.timeout == CLIENT_SOCKET_TIMEOUT_SECONDS == 10.0


class _ImmediateRefresher:
    def __init__(self, *, failures: int = 0) -> None:
        self.calls = 0
        self.failures = failures
        self.called = threading.Event()

    def run(self) -> None:
        self.calls += 1
        self.called.set()
        if self.calls <= self.failures:
            raise RuntimeError("indisponible")


def test_scheduler_runs_refresh_immediately_on_startup() -> None:
    refresher = _ImmediateRefresher()
    scheduler = RefreshScheduler(refresher, refresh_interval_seconds=21_600)

    scheduler.start()
    try:
        assert refresher.called.wait(1)
        assert refresher.calls == 1
    finally:
        scheduler.stop()


def test_scheduler_uses_bounded_exponential_backoff_then_normal_interval() -> None:
    refresher = _ImmediateRefresher(failures=2)
    scheduler = RefreshScheduler(refresher, refresh_interval_seconds=21_600)
    delays: list[float] = []

    def controlled_wait(delay: float) -> bool:
        delays.append(delay)
        return len(delays) < 3

    scheduler._wait = controlled_wait  # type: ignore[method-assign]
    scheduler.run_forever()

    assert delays == [30.0, 60.0, 21_600.0]
    assert refresher.calls == 3


def test_scheduler_failure_backoff_is_capped() -> None:
    assert RefreshScheduler.failure_delay(1) == 30.0
    assert RefreshScheduler.failure_delay(2) == 60.0
    assert RefreshScheduler.failure_delay(10) == 900.0


def test_scheduler_shares_shutdown_event_with_active_refresher() -> None:
    stop_event = threading.Event()
    started = threading.Event()
    finished = threading.Event()

    class CancellableRefresher:
        def run(self) -> None:
            started.set()
            stop_event.wait(2)
            finished.set()

    scheduler = RefreshScheduler(
        CancellableRefresher(),
        refresh_interval_seconds=21_600,
        stop_event=stop_event,
    )
    scheduler.start()
    assert started.wait(1)

    scheduler.stop(timeout=1)

    assert finished.is_set()
