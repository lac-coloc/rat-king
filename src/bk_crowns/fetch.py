"""Polite, fixed-source HTTP client for public Burger King pages."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from urllib.parse import urljoin, urlsplit

import httpx

from . import __version__
from .config import Config, validate_restaurant_id
from .models import CacheMetadata

PUBLIC_URLS = {
    "restaurants": (
        "https://ecoceabkstorageprdnorth.blob.core.windows.net/static/restaurants.json"
    ),
    "catalogue": ("https://www.burgerking.fr/commande/{restaurant_id}/a-emporter/catalogue/home"),
    "tiers": "https://www.burgerking.fr/paliers-kingdom",
    "cgu": "https://www.burgerking.fr/page/cgu-fidelite",
}
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class FetchError(RuntimeError):
    """Raised when a fixed public source cannot be downloaded politely."""


class FetchCancelled(FetchError):
    """Raised when process shutdown cancels an in-progress refresh."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    resource: str
    url: str
    content: bytes
    metadata: CacheMetadata
    not_modified: bool


class PublicFetcher:
    """Fetch only allow-listed public resources without durable cookies."""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        stop_event: Event | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.sleep = sleep
        self.stop_event = stop_event

    def _url(self, resource: str, restaurant_id: str | None) -> str:
        template = PUBLIC_URLS[resource]
        if resource == "catalogue":
            if restaurant_id is None:
                raise FetchError("identifiant requis pour le catalogue public")
            return template.format(restaurant_id=validate_restaurant_id(restaurant_id))
        if restaurant_id is not None:
            raise FetchError("identifiant inattendu pour une source publique partagée")
        return template

    def _ensure_running(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise FetchCancelled("arrêt du refresh demandé")

    def _backoff(self, delay: float) -> None:
        if self.stop_event is None:
            self.sleep(delay)
        elif self.stop_event.wait(delay):
            raise FetchCancelled("arrêt du refresh demandé")

    @staticmethod
    def _redirect_is_safe(source_url: str, next_url: str) -> bool:
        source = urlsplit(source_url)
        target = urlsplit(next_url)
        try:
            target_port = target.port
        except ValueError:
            return False
        target_segments = {segment.casefold() for segment in target.path.split("/")}
        return (
            target.scheme == "https"
            and target.hostname == source.hostname
            and target_port in (None, 443)
            and target.username is None
            and target.password is None
            and target.path.rstrip("/") == source.path.rstrip("/")
            and "kingdom" not in target_segments
        )

    def _request(self, client: httpx.Client, url: str) -> httpx.Response:
        current_url = url
        for _redirect in range(4):
            self._ensure_running()
            response = client.get(current_url)
            client.cookies.clear()
            self._ensure_running()
            if response.status_code not in REDIRECT_STATUSES:
                return response
            location = response.headers.get("Location")
            next_url = urljoin(str(response.url), location or "")
            if not location or not self._redirect_is_safe(url, next_url):
                raise FetchError("redirection hors des sources publiques autorisées")
            current_url = next_url
        raise FetchError("trop de redirections pour une source publique")

    def get(
        self,
        resource: str,
        cache: CacheMetadata,
        *,
        restaurant_id: str | None = None,
    ) -> FetchResult:
        self._ensure_running()
        url = self._url(resource, restaurant_id)
        headers = {
            "User-Agent": f"rat-king/{__version__} (+public-data; no-auth)",
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
        }
        if cache.etag:
            headers["If-None-Match"] = cache.etag
        if cache.last_modified:
            headers["If-Modified-Since"] = cache.last_modified

        timeout = httpx.Timeout(self.config.http_timeout_seconds)
        last_error: Exception | None = None
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=self.transport,
            headers=headers,
        ) as client:
            for attempt in range(3):
                try:
                    response = self._request(client, url)
                except httpx.RequestError as exc:
                    last_error = exc
                    if attempt < 2:
                        self._backoff(float(2**attempt))
                        continue
                    break
                finally:
                    client.cookies.clear()

                if response.status_code == 304:
                    if not cache.raw_path or not Path(cache.raw_path).is_file():
                        raise FetchError(f"304 reçu sans copie locale pour {resource}")
                    return FetchResult(
                        resource, url, Path(cache.raw_path).read_bytes(), cache, True
                    )

                if response.status_code in TRANSIENT_STATUSES:
                    last_error = FetchError(f"HTTP {response.status_code} pour {resource}")
                    if attempt < 2:
                        self._backoff(float(2**attempt))
                        continue
                    break

                if response.status_code != 200:
                    raise FetchError(
                        f"HTTP {response.status_code} pour la source publique {resource}"
                    )
                if not response.content:
                    raise FetchError(f"réponse vide pour la source publique {resource}")
                metadata = CacheMetadata(
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    raw_path=cache.raw_path,
                )
                return FetchResult(resource, url, response.content, metadata, False)

        detail = f" : {last_error}" if last_error else ""
        raise FetchError(f"échec de {resource} après 3 tentatives{detail}")
