import gzip
import threading
from pathlib import Path

import httpx
import pytest

import bk_crowns.fetch as fetch_module
from bk_crowns.config import Config, ConfigError
from bk_crowns.fetch import FetchCancelled, FetchError, PublicFetcher
from bk_crowns.models import CacheMetadata


def _config(tmp_path: Path) -> Config:
    return Config(
        state_dir=tmp_path,
        output_dir=tmp_path / "site",
        http_timeout_seconds=12.5,
    )


def test_fetcher_uses_fixed_url_user_agent_timeout_and_gzip(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["agent"] = request.headers["User-Agent"]
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(
            200,
            content=gzip.compress(b'{"restaurants": []}'),
            headers={"Content-Encoding": "gzip", "ETag": '"v1"'},
        )

    fetcher = PublicFetcher(_config(tmp_path), transport=httpx.MockTransport(handler))
    result = fetcher.get("restaurants", CacheMetadata())

    assert seen["url"] == (
        "https://ecoceabkstorageprdnorth.blob.core.windows.net/static/restaurants.json"
    )
    assert str(seen["agent"]).startswith("rat-king/")
    assert seen["timeout"] == {"connect": 12.5, "read": 12.5, "write": 12.5, "pool": 12.5}
    assert result.content == b'{"restaurants": []}'
    assert result.metadata.etag == '"v1"'


def test_fetcher_sends_conditionals_and_reuses_cached_body_on_304(tmp_path: Path) -> None:
    cached = tmp_path / "previous.json"
    cached.write_bytes(b"cached-public-data")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["If-None-Match"] == '"old"'
        assert request.headers["If-Modified-Since"] == "Sun, 09 Aug 2026 10:00:00 GMT"
        return httpx.Response(304)

    fetcher = PublicFetcher(_config(tmp_path), transport=httpx.MockTransport(handler))
    result = fetcher.get(
        "restaurants",
        CacheMetadata('"old"', "Sun, 09 Aug 2026 10:00:00 GMT", str(cached)),
    )

    assert result.content == b"cached-public-data"
    assert result.not_modified is True
    assert result.metadata.raw_path == str(cached)


def test_fetcher_retries_transient_errors_without_reusing_response_cookies(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(503, headers={"Set-Cookie": "anonymous=value"})
        return httpx.Response(200, content=b"ok")

    fetcher = PublicFetcher(
        _config(tmp_path), transport=httpx.MockTransport(handler), sleep=delays.append
    )
    result = fetcher.get("restaurants", CacheMetadata())

    assert result.content == b"ok"
    assert len(requests) == 3
    assert delays == [1.0, 2.0]
    assert all("Cookie" not in request.headers for request in requests)


def test_fetcher_does_not_retry_access_blocks_or_accept_caller_urls(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, content=b"blocked")

    fetcher = PublicFetcher(_config(tmp_path), transport=httpx.MockTransport(handler))

    with pytest.raises(FetchError, match="403"):
        fetcher.get("catalogue", CacheMetadata(), restaurant_id="K2001")
    with pytest.raises(KeyError):
        fetcher.get("https://evil.example/", CacheMetadata())
    assert calls == 1


def test_catalogue_url_requires_a_strict_explicit_restaurant_id(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"catalogue")

    fetcher = PublicFetcher(_config(tmp_path), transport=httpx.MockTransport(handler))

    fetcher.get("catalogue", CacheMetadata(), restaurant_id="K2001")

    assert requests[0].url.path == "/commande/K2001/a-emporter/catalogue/home"
    with pytest.raises(ConfigError):
        fetcher.get("catalogue", CacheMetadata(), restaurant_id="https://evil.example")
    with pytest.raises(FetchError, match="identifiant requis"):
        fetcher.get("catalogue", CacheMetadata())


def test_shared_resource_rejects_an_irrelevant_restaurant_id(tmp_path: Path) -> None:
    fetcher = PublicFetcher(
        _config(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"unused")),
    )

    with pytest.raises(FetchError, match="identifiant inattendu"):
        fetcher.get("restaurants", CacheMetadata(), restaurant_id="K2001")


def test_fetcher_refuses_redirects_outside_the_fixed_public_hosts(tmp_path: Path) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(str(request.url.host))
        if request.url.host == "evil.example":
            return httpx.Response(200, content=b"private")
        return httpx.Response(302, headers={"Location": "https://evil.example/private"})

    fetcher = PublicFetcher(_config(tmp_path), transport=httpx.MockTransport(handler))

    with pytest.raises(FetchError, match="redirection"):
        fetcher.get("catalogue", CacheMetadata(), restaurant_id="K2001")
    assert hosts == ["www.burgerking.fr"]


def test_fetcher_allows_cookie_free_redirects_on_a_fixed_public_host(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/home"):
            return httpx.Response(
                302,
                headers={
                    "Location": "/commande/K2001/a-emporter/catalogue/home/",
                    "Set-Cookie": "anon=value",
                },
            )
        assert "Cookie" not in request.headers
        return httpx.Response(200, content=b"catalogue")

    fetcher = PublicFetcher(_config(tmp_path), transport=httpx.MockTransport(handler))

    result = fetcher.get("catalogue", CacheMetadata(), restaurant_id="K2001")

    assert result.content == b"catalogue"
    assert [request.url.host for request in requests] == [
        "www.burgerking.fr",
        "www.burgerking.fr",
    ]


@pytest.mark.parametrize(
    "location",
    [
        "https://www.burgerking.fr/kingdom/private",
        "https://www.burgerking.fr:8443/commande/K2001/a-emporter/catalogue/home",
        "https://www.burgerking.fr/page/cgu-fidelite",
    ],
)
def test_fetcher_refuses_redirects_away_from_the_exact_public_resource(
    tmp_path: Path, location: str
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, content=b"forbidden target")

    fetcher = PublicFetcher(_config(tmp_path), transport=httpx.MockTransport(handler))

    with pytest.raises(FetchError, match="redirection"):
        fetcher.get("catalogue", CacheMetadata(), restaurant_id="K2001")
    assert calls == 1


def test_fetcher_stops_after_three_transient_failures(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    fetcher = PublicFetcher(
        _config(tmp_path), transport=httpx.MockTransport(handler), sleep=lambda _: None
    )

    with pytest.raises(FetchError, match="3 tentatives"):
        fetcher.get("restaurants", CacheMetadata())
    assert calls == 3


def test_fetcher_honours_shutdown_before_starting_another_request(tmp_path: Path) -> None:
    calls = 0
    stop_event = threading.Event()
    stop_event.set()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"unused")

    fetcher = PublicFetcher(
        _config(tmp_path),
        transport=httpx.MockTransport(handler),
        stop_event=stop_event,
    )

    with pytest.raises(FetchCancelled):
        fetcher.get("restaurants", CacheMetadata())
    assert calls == 0


def test_fetcher_rejects_a_decompressed_response_over_the_fixed_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_module, "MAX_RESPONSE_BYTES", 8)
    fetcher = PublicFetcher(
        _config(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"123456789")),
    )

    with pytest.raises(FetchError, match="volumineuse"):
        fetcher.get("restaurants", CacheMetadata())


def test_fetcher_rejects_an_oversized_content_length_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_module, "MAX_RESPONSE_BYTES", 8)
    fetcher = PublicFetcher(
        _config(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"Content-Length": "9"}, content=b"ignored")
        ),
    )

    with pytest.raises(FetchError, match="volumineuse"):
        fetcher.get("restaurants", CacheMetadata())
