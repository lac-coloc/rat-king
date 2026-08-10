"""Application configuration with strict, SSRF-safe validation."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

RESTAURANT_ID_PATTERN = re.compile(r"^K[0-9]{4}$")


class ConfigError(ValueError):
    """Raised when an environment setting is unsafe or invalid."""


def validate_restaurant_id(value: str) -> str:
    """Return a strict BK identifier or reject unsafe path/URL input."""

    if not RESTAURANT_ID_PATTERN.fullmatch(value):
        raise ConfigError(
            "l'identifiant restaurant doit respecter le format K suivi de quatre chiffres"
        )
    return value


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} doit être un entier positif") from exc
    if value <= 0:
        raise ConfigError(f"{key} doit être un entier positif")
    return value


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} doit être un nombre positif") from exc
    if value <= 0:
        raise ConfigError(f"{key} doit être un nombre positif")
    return value


def _bounded_int(env: Mapping[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    value = _positive_int(env, key, default)
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} doit être compris entre {minimum} et {maximum}")
    return value


def _bounded_float(
    env: Mapping[str, str], key: str, default: float, minimum: float, maximum: float
) -> float:
    value = _positive_float(env, key, default)
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} doit être compris entre {minimum:g} et {maximum:g}")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    """Validated runtime settings."""

    refresh_interval_seconds: int = 21_600
    output_dir: Path = Path("/data/site")
    state_dir: Path = Path("/data")
    http_timeout_seconds: float = 30.0
    port: int = 8080
    cache_max_restaurants: int = 20
    refresh_queue_max: int = 10
    cold_loads_per_hour: int = 6
    search_max_results: int = 20

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        values = os.environ if env is None else env
        if "BK_RESTAURANT_ID" in values:
            raise ConfigError(
                "BK_RESTAURANT_ID n'est plus accepté : choisissez le restaurant dans l'interface"
            )
        config = cls(
            refresh_interval_seconds=_bounded_int(
                values, "BK_REFRESH_INTERVAL_SECONDS", 21_600, 900, 86_400
            ),
            output_dir=Path(values.get("BK_OUTPUT_DIR", "/data/site")),
            state_dir=Path(values.get("BK_STATE_DIR", "/data")),
            http_timeout_seconds=_bounded_float(
                values, "BK_HTTP_TIMEOUT_SECONDS", 30.0, 0.001, 120.0
            ),
            port=_bounded_int(values, "PORT", 8080, 1, 65_535),
            cache_max_restaurants=_bounded_int(values, "BK_CACHE_MAX_RESTAURANTS", 20, 1, 100),
            refresh_queue_max=_bounded_int(values, "BK_REFRESH_QUEUE_MAX", 10, 1, 50),
            cold_loads_per_hour=_bounded_int(values, "BK_COLD_LOADS_PER_HOUR", 6, 1, 24),
            search_max_results=_bounded_int(values, "BK_SEARCH_MAX_RESULTS", 20, 1, 50),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not 900 <= self.refresh_interval_seconds <= 86_400:
            raise ConfigError("BK_REFRESH_INTERVAL_SECONDS doit être compris entre 900 et 86400")
        if self.http_timeout_seconds <= 0 or self.http_timeout_seconds > 120:
            raise ConfigError("BK_HTTP_TIMEOUT_SECONDS doit être compris entre 0 et 120")
        if not 1 <= self.port <= 65_535:
            raise ConfigError("PORT doit être compris entre 1 et 65535")
        bounds = (
            ("BK_CACHE_MAX_RESTAURANTS", self.cache_max_restaurants, 1, 100),
            ("BK_REFRESH_QUEUE_MAX", self.refresh_queue_max, 1, 50),
            ("BK_COLD_LOADS_PER_HOUR", self.cold_loads_per_hour, 1, 24),
            ("BK_SEARCH_MAX_RESULTS", self.search_max_results, 1, 50),
        )
        for key, value, minimum, maximum in bounds:
            if not minimum <= value <= maximum:
                raise ConfigError(f"{key} doit être compris entre {minimum} et {maximum}")

        state = self.state_dir.resolve(strict=False)
        output = self.output_dir.resolve(strict=False)
        if output == state or not output.is_relative_to(state):
            raise ConfigError("BK_OUTPUT_DIR doit être un sous-répertoire de BK_STATE_DIR")
