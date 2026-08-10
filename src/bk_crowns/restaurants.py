"""Validated public restaurant directory and local-only search."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from .config import ConfigError, validate_restaurant_id
from .models import Restaurant

_POSTAL_CITY = re.compile(r"\b(?P<postal>[0-9]{5})\s+(?P<city>[^,;]+?)\s*$")


class RestaurantDirectoryError(ValueError):
    """Raised when the public restaurant index is empty or incoherent."""


def normalize_search_text(value: str) -> str:
    """Normalize public labels and user queries without retaining either."""

    decomposed = unicodedata.normalize("NFKD", value.casefold())
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", unaccented).split())


def _public_text(item: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _location(item: dict[str, object], address: str) -> tuple[str, str]:
    postal_code = _public_text(item, "postalCode", "postal_code", "zipCode", "zipcode")
    city = _public_text(item, "city", "town")
    match = _POSTAL_CITY.search(address)
    if not postal_code and match:
        postal_code = match.group("postal")
    if not city and match:
        city = match.group("city").strip(" -")
    return city, postal_code


@dataclass(frozen=True, slots=True)
class _SearchEntry:
    restaurant: Restaurant
    restaurant_id: str
    name: str
    city: str
    postal_code: str
    address: str


class RestaurantDirectory:
    """Immutable validated directory with deterministic local search."""

    def __init__(self, restaurants: tuple[Restaurant, ...]) -> None:
        if not restaurants:
            raise RestaurantDirectoryError("annuaire public des restaurants vide")
        self.restaurants = tuple(sorted(restaurants, key=lambda item: item.id))
        self._by_id = {restaurant.id: restaurant for restaurant in self.restaurants}
        self._search = tuple(
            _SearchEntry(
                restaurant=restaurant,
                restaurant_id=normalize_search_text(restaurant.id),
                name=normalize_search_text(restaurant.name),
                city=normalize_search_text(restaurant.city),
                postal_code=normalize_search_text(restaurant.postal_code),
                address=normalize_search_text(restaurant.address),
            )
            for restaurant in self.restaurants
        )

    def get(self, restaurant_id: str) -> Restaurant | None:
        return self._by_id.get(restaurant_id)

    @staticmethod
    def _stable_key(entry: _SearchEntry) -> tuple[str, str, str]:
        return (entry.city, entry.name, entry.restaurant.id)

    def search(self, query: str, limit: int) -> tuple[Restaurant, ...]:
        if limit <= 0:
            raise ValueError("search limit must be positive")
        normalized = normalize_search_text(query[:80])
        if len(normalized) < 2:
            return ()

        ranked: list[tuple[int, tuple[str, str, str], Restaurant]] = []
        for entry in self._search:
            exact_fields = (entry.city, entry.postal_code, entry.name)
            prefix_fields = (entry.city, entry.postal_code, entry.name, entry.restaurant_id)
            if normalized == entry.restaurant_id:
                score = 0
            elif normalized in exact_fields:
                score = 1
            elif any(field.startswith(normalized) for field in prefix_fields if field):
                score = 2
            elif normalized in entry.name or normalized in entry.address:
                score = 3
            else:
                continue
            ranked.append((score, self._stable_key(entry), entry.restaurant))

        if ranked:
            ranked.sort(key=lambda item: (item[0], item[1]))
            return tuple(item[2] for item in ranked[:limit])

        if len(normalized) < 4:
            return ()
        fuzzy = [
            entry
            for entry in self._search
            if entry.city and _edit_distance_at_most_one(normalized, entry.city)
        ]
        fuzzy.sort(key=self._stable_key)
        return tuple(entry.restaurant for entry in fuzzy[:limit])


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    """Return whether two strings differ by at most one edit, in linear time."""

    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    left_index = 0
    right_index = 0
    edits = 0
    while left_index < len(left) and right_index < len(right):
        if left[left_index] == right[right_index]:
            left_index += 1
            right_index += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(left) == len(right):
            left_index += 1
        right_index += 1
    return edits + (right_index < len(right) or left_index < len(left)) <= 1


def parse_restaurant_directory(content: bytes) -> RestaurantDirectory:
    """Parse and validate list or ID-keyed public index shapes."""

    try:
        document = json.loads(content)
        raw_restaurants = document["restaurants"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RestaurantDirectoryError("liste publique des restaurants invalide") from exc

    keyed_items: list[tuple[str | None, object]]
    if isinstance(raw_restaurants, list):
        keyed_items = [(None, item) for item in raw_restaurants]
    elif isinstance(raw_restaurants, dict):
        keyed_items = [(str(key), item) for key, item in raw_restaurants.items()]
    else:
        raise RestaurantDirectoryError("liste publique des restaurants invalide")

    restaurants: list[Restaurant] = []
    seen: set[str] = set()
    for mapping_id, raw_item in keyed_items:
        if not isinstance(raw_item, dict):
            raise RestaurantDirectoryError("entrée de restaurant invalide")
        restaurant_id = _public_text(raw_item, "id") or mapping_id or ""
        try:
            restaurant_id = validate_restaurant_id(restaurant_id)
        except ConfigError as exc:
            raise RestaurantDirectoryError("identifiant public de restaurant invalide") from exc
        if mapping_id is not None and mapping_id != restaurant_id:
            raise RestaurantDirectoryError("clé et identifiant public incohérents")
        if restaurant_id in seen:
            raise RestaurantDirectoryError(f"identifiant public dupliqué : {restaurant_id}")
        name = _public_text(raw_item, "name")
        address = _public_text(raw_item, "address")
        if not name or not address:
            raise RestaurantDirectoryError(f"identité publique incomplète : {restaurant_id}")
        city, postal_code = _location(raw_item, address)
        restaurants.append(Restaurant(restaurant_id, name, address, city, postal_code))
        seen.add(restaurant_id)

    return RestaurantDirectory(tuple(restaurants))
