import json

import pytest

from bk_crowns.restaurants import (
    RestaurantDirectoryError,
    normalize_search_text,
    parse_restaurant_directory,
)

DIRECTORY = {
    "restaurants": [
        {
            "id": "K2001",
            "name": "BURGER KING VILLE EXEMPLE CENTRE",
            "address": "10 RUE DU TEST - 12345 VILLE EXEMPLE",
        },
        {
            "id": "K2002",
            "name": "BURGER KING CITÉ DÉMO GARE",
            "address": "2 AVENUE NEUTRE - 54321 CITÉ DÉMO",
        },
    ]
}


def _content(document: object = DIRECTORY) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode()


def test_directory_parses_list_and_derives_postal_city() -> None:
    directory = parse_restaurant_directory(_content())

    selected = directory.get("K2001")

    assert selected is not None
    assert (selected.postal_code, selected.city) == ("12345", "VILLE EXEMPLE")


def test_directory_accepts_mapping_keyed_by_public_id() -> None:
    mapping = {item["id"]: item for item in DIRECTORY["restaurants"]}

    directory = parse_restaurant_directory(_content({"restaurants": mapping}))

    assert tuple(item.id for item in directory.restaurants) == ("K2001", "K2002")


def test_search_prioritizes_id_city_name_then_address() -> None:
    directory = parse_restaurant_directory(_content())

    assert directory.search("K2002", 20)[0].id == "K2002"
    assert directory.search("cite demo", 20)[0].id == "K2002"
    assert directory.search("gare", 20)[0].id == "K2002"
    assert directory.search("avenue neutre", 20)[0].id == "K2002"


def test_search_accepts_one_missing_letter_in_a_long_city() -> None:
    directory = parse_restaurant_directory(_content())

    assert directory.search("vile exemple", 20)[0].id == "K2001"


@pytest.mark.parametrize("query", ["v", "xx", "ville sans rapport"])
def test_search_does_not_offer_unrelated_fuzzy_results(query: str) -> None:
    directory = parse_restaurant_directory(_content())

    assert directory.search(query, 20) == ()


def test_search_limit_and_order_are_stable() -> None:
    directory = parse_restaurant_directory(_content())

    assert [item.id for item in directory.search("burger king", 1)] == ["K2002"]


def test_name_normalization_removes_accents_symbols_and_extra_spaces() -> None:
    assert normalize_search_text("  Cité®  —  DÉMO! ") == "cite demo"


def test_directory_rejects_duplicate_ids() -> None:
    duplicate = {"restaurants": [DIRECTORY["restaurants"][0]] * 2}

    with pytest.raises(RestaurantDirectoryError, match="dupliqué"):
        parse_restaurant_directory(_content(duplicate))


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"restaurants": []},
        {"restaurants": [{"id": "../x", "name": "Test", "address": "Adresse"}]},
        {"restaurants": [{"id": "K2001", "name": "", "address": "Adresse"}]},
    ],
)
def test_directory_rejects_empty_or_malformed_public_data(document: object) -> None:
    with pytest.raises(RestaurantDirectoryError):
        parse_restaurant_directory(_content(document))


def test_search_rejects_invalid_limits() -> None:
    directory = parse_restaurant_directory(_content())

    with pytest.raises(ValueError, match="limit"):
        directory.search("ville", 0)
