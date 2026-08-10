"""Durable least-recently-used policy for restaurant snapshots."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import validate_restaurant_id
from .state import StateError, StateRepository, atomic_write_json


@dataclass(frozen=True, slots=True)
class CacheEntry:
    restaurant_id: str
    last_access: datetime


class RestaurantCache:
    """Persist access times and evict only exact, unprotected cache roots."""

    def __init__(
        self,
        repository: StateRepository,
        maximum: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if maximum <= 0:
            raise ValueError("cache maximum must be positive")
        self.repository = repository
        self.maximum = maximum
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entries = self._load()

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        if not isinstance(value, str):
            raise StateError("index de cache invalide")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise StateError("index de cache invalide") from exc
        if parsed.tzinfo is None:
            raise StateError("index de cache sans fuseau")
        return parsed.astimezone(UTC)

    def _load(self) -> dict[str, CacheEntry]:
        path = self.repository.cache_index_path
        changed = not path.exists()
        parsed: dict[str, CacheEntry] = {}
        if path.exists():
            try:
                document = json.loads(path.read_text("utf-8"))
                raw_entries = document["entries"]
                if not isinstance(raw_entries, dict):
                    raise StateError("index de cache invalide")
                for raw_id, raw_entry in raw_entries.items():
                    restaurant_id = validate_restaurant_id(str(raw_id))
                    if not isinstance(raw_entry, dict):
                        raise StateError("index de cache invalide")
                    parsed[restaurant_id] = CacheEntry(
                        restaurant_id,
                        self._parse_datetime(raw_entry.get("last_access")),
                    )
            except StateError:
                raise
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise StateError("index de cache illisible") from exc

        valid_ids = set(self.repository.cached_restaurant_ids())
        for restaurant_id in tuple(parsed):
            if restaurant_id not in valid_ids:
                parsed.pop(restaurant_id)
                changed = True
        for restaurant_id in sorted(valid_ids - set(parsed)):
            store = self.repository.restaurant(restaurant_id)
            status = store.load_status()
            fallback = status.last_success or datetime.fromtimestamp(
                store.root.stat().st_mtime, tz=UTC
            )
            parsed[restaurant_id] = CacheEntry(restaurant_id, fallback)
            changed = True
        if changed:
            self._entries = parsed
            self._save()
        return parsed

    def _save(self) -> None:
        atomic_write_json(
            self.repository.cache_index_path,
            {
                "entries": {
                    restaurant_id: {"last_access": entry.last_access.isoformat()}
                    for restaurant_id, entry in sorted(self._entries.items())
                }
            },
        )

    def entries(self) -> dict[str, CacheEntry]:
        return dict(self._entries)

    def touch(self, restaurant_id: str) -> None:
        safe_id = validate_restaurant_id(restaurant_id)
        if safe_id not in self.repository.cached_restaurant_ids():
            raise StateError(f"snapshot absent du cache : {safe_id}")
        accessed_at = self._clock()
        if accessed_at.tzinfo is None:
            raise StateError("horloge de cache sans fuseau")
        self._entries[safe_id] = CacheEntry(safe_id, accessed_at.astimezone(UTC))
        self._save()

    def evict(self, protected: set[str]) -> tuple[str, ...]:
        protected_ids = {validate_restaurant_id(item) for item in protected}
        evicted: list[str] = []
        while len(self._entries) > self.maximum:
            candidates = sorted(
                (
                    entry
                    for entry in self._entries.values()
                    if entry.restaurant_id not in protected_ids
                ),
                key=lambda entry: (entry.last_access, entry.restaurant_id),
            )
            if not candidates:
                break
            selected = candidates[0]
            self.repository.remove_restaurant(selected.restaurant_id)
            self._entries.pop(selected.restaurant_id, None)
            evicted.append(selected.restaurant_id)
        if evicted:
            self._save()
        return tuple(evicted)
