import json
from pathlib import Path

import pytest

from bk_crowns.catalog_parser import CatalogParseError, parse_catalog

FIXTURES = Path(__file__).parent / "fixtures"


def _catalog_html(payload: dict[str, object]) -> str:
    chunk = f"13:{json.dumps(payload)}"
    return f"<script>self.__next_f.push({json.dumps([1, chunk])})</script>"


def _split_rsc_html(chunk: str, split_at: int) -> str:
    return "".join(
        f"<script>self.__next_f.push({json.dumps([1, fragment])})</script>"
        for fragment in (chunk[:split_at], chunk[split_at:])
    )


def test_catalog_parser_reads_products_prices_and_availability() -> None:
    catalog = parse_catalog((FIXTURES / "catalogue-reduced.html").read_text())

    whopper = next(product for product in catalog.products if product.route_id == "whopper")
    assert whopper.name == "Whopper®"
    assert whopper.prices["alone"] == 650
    assert whopper.available is True
    assert whopper.active is True
    assert whopper.category_ids == ("burgers",)


def test_catalog_parser_reads_each_menu_size_price() -> None:
    catalog = parse_catalog((FIXTURES / "catalogue-reduced.html").read_text())

    menu = catalog.menus[0]
    assert {size.size: size.price for size in menu.sizes} == {"M": 890, "L": 990}


def test_catalog_parser_rejects_empty_catalogue() -> None:
    with pytest.raises(CatalogParseError, match="catalogue"):
        parse_catalog('<script>self.__next_f.push([1,"0:{\\"page\\":1}"])</script>')


def test_catalog_parser_ignores_uncategorized_zero_price_menu_components() -> None:
    payload = {
        "categories": [{"id": "burgers", "name": "Burgers"}],
        "products": {
            "valid": {
                "id": "1",
                "plu": "1",
                "name": "Burger test",
                "routeId": "burger-test",
                "available": True,
                "active": True,
                "prices": {"alone": 500},
                "categories": [{"id": "burgers"}],
            },
            "component": {
                "id": "2",
                "plu": "2",
                "name": "Rien du tout",
                "routeId": "sans-jouet",
                "available": True,
                "active": True,
                "prices": {"alone": 0, "m": -30},
            },
        },
        "menus": {},
    }

    catalog = parse_catalog(_catalog_html(payload))

    assert [product.name for product in catalog.products] == ["Burger test"]


def test_catalog_parser_reassembles_rsc_fragments_split_inside_a_json_string() -> None:
    payload = {
        "categories": [{"id": "burgers", "name": "Burgers"}],
        "products": {
            "1": {
                "id": "1",
                "plu": "1",
                "name": "Burger test",
                "routeId": "burger-test",
                "available": True,
                "active": True,
                "prices": {"alone": 500},
                "categories": [{"id": "burgers"}],
            }
        },
        "menus": {},
    }
    chunk = "13:" + json.dumps(payload)
    split_at = chunk.index("Burger test") + 6

    catalog = parse_catalog(_split_rsc_html(chunk, split_at))

    assert catalog.products[0].name == "Burger test"


@pytest.mark.parametrize("price", [0, -1, 100_001, 3.5, "650"])
def test_catalog_parser_rejects_invalid_positive_centime_prices(price: object) -> None:
    payload = {
        "categories": [{"id": "burgers", "name": "Burgers"}],
        "products": {
            "1": {
                "id": "1",
                "plu": "1",
                "name": "Test",
                "routeId": "test",
                "available": True,
                "active": True,
                "prices": {"alone": price},
                "categories": [{"id": "burgers"}],
            }
        },
        "menus": {},
    }

    with pytest.raises(CatalogParseError, match="prix"):
        parse_catalog(_catalog_html(payload))
