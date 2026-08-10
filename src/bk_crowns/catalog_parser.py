"""Parser for the public restaurant catalogue embedded in Next.js SSR."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .models import Catalog, Category, Menu, MenuSize, Product
from .rsc import RSCParseError, decode_next_chunks, iter_balanced_json

MAX_PRICE_CENTS = 100_000


class CatalogParseError(ValueError):
    """Raised when the public SSR does not contain a coherent catalogue."""


def _nested_dicts(value: object) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _nested_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nested_dicts(item)


def _mapping_values(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        candidates = value.values()
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    return [item for item in candidates if isinstance(item, Mapping)]


def _catalog_object(chunks: list[str]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for decoded in iter_balanced_json("".join(chunks)):
        for item in _nested_dicts(decoded):
            if isinstance(item.get("products"), (dict, list)) and isinstance(
                item.get("menus"), (dict, list)
            ):
                candidates.append(item)
    if not candidates:
        raise CatalogParseError("aucun objet de catalogue cohérent dans le SSR public")
    return max(
        candidates,
        key=lambda item: (
            len(_mapping_values(item["products"])) + len(_mapping_values(item["menus"]))
        ),
    )


def _price(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_PRICE_CENTS:
        raise CatalogParseError(f"prix invalide pour {label}")
    return value


def _category_ids(item: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(category["id"])
        for category in _mapping_values(item.get("categories", []))
        if category.get("id") is not None
    )


def _kind(name: str, category_ids: tuple[str, ...], categories: dict[str, str]) -> str:
    haystack = " ".join([name, *(categories.get(item, "") for item in category_ids)]).lower()
    if "burger" in haystack or "whopper" in haystack:
        return "burger"
    if "dessert" in haystack or any(word in haystack for word in ("sundae", "cookie", "donut")):
        return "dessert"
    if "box" in haystack:
        return "box"
    return "produit"


def parse_catalog(html: str) -> Catalog:
    """Parse and validate products and menus from a public catalogue page."""

    try:
        source = _catalog_object(decode_next_chunks(html))
    except RSCParseError as exc:
        raise CatalogParseError(f"SSR public invalide : {exc}") from exc

    category_items = _mapping_values(source.get("categories", []))
    categories = tuple(
        Category(id=str(item["id"]), name=str(item.get("name", "")))
        for item in category_items
        if item.get("id") is not None
    )
    category_names = {item.id: item.name for item in categories}

    products: list[Product] = []
    for item in _mapping_values(source["products"]):
        category_ids = _category_ids(item)
        prices = item.get("prices")
        if not isinstance(prices, Mapping) or "alone" not in prices:
            if not category_ids:
                continue
            raise CatalogParseError(f"prix seul absent pour {item.get('name', 'produit inconnu')}")
        try:
            alone = _price(prices["alone"], str(item.get("name", "produit")))
        except CatalogParseError:
            if not category_ids:
                continue
            raise
        products.append(
            Product(
                id=str(item.get("id", item.get("plu", ""))),
                plu=str(item.get("plu", "")),
                name=str(item.get("name", "")).strip(),
                route_id=str(item.get("routeId", "")),
                available=item.get("available") is True,
                active=item.get("active") is True,
                prices={"alone": alone},
                category_ids=category_ids,
                kind=_kind(str(item.get("name", "")), category_ids, category_names),
            )
        )

    menus: list[Menu] = []
    for item in _mapping_values(source["menus"]):
        sizes = tuple(
            MenuSize(
                plu=str(size.get("plu", "")),
                size=str(size.get("size", "")).upper(),
                price=_price(
                    size.get("price"), f"{item.get('name', 'menu')} {size.get('size', '')}"
                ),
            )
            for size in _mapping_values(item.get("sizes", {}))
        )
        if not sizes:
            raise CatalogParseError(f"tailles absentes pour {item.get('name', 'menu inconnu')}")
        menus.append(
            Menu(
                id=str(item.get("id", item.get("plu", ""))),
                plu=str(item.get("plu", "")),
                name=str(item.get("name", "")).strip(),
                route_id=str(item.get("routeId", "")),
                available=item.get("available") is True,
                active=item.get("active") is True,
                sizes=sizes,
                category_ids=_category_ids(item),
            )
        )

    if not products and not menus:
        raise CatalogParseError("catalogue public vide")
    return Catalog(categories=categories, products=tuple(products), menus=tuple(menus))
