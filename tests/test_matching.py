from bk_crowns.matching import match_reward, match_rewards, normalize_name
from bk_crowns.models import Catalog, Category, Menu, MenuSize, Product, Reward


def _reward(
    name: str,
    *,
    kind: str = "produit",
    size: str | None = None,
) -> Reward:
    return Reward(
        crowns=40,
        name=name,
        kind=kind,
        menu_size=size,
        seasonal=False,
        available=True,
        source_url="https://public.example/rewards",
    )


def _product(
    name: str,
    price: int,
    *,
    route: str,
    item_id: str = "1",
    categories: tuple[str, ...] = ("normal",),
    active: bool = True,
    available: bool = True,
    kind: str = "produit",
) -> Product:
    return Product(
        id=item_id,
        plu=item_id,
        name=name,
        route_id=route,
        available=available,
        active=active,
        prices={"alone": price},
        category_ids=categories,
        kind=kind,
    )


def _catalog(*, products: tuple[Product, ...] = (), menus: tuple[Menu, ...] = ()) -> Catalog:
    return Catalog(
        categories=(Category("normal", "Catalogue normal"),),
        products=products,
        menus=menus,
    )


def test_normalize_name_removes_accents_marks_punctuation_and_extra_spaces() -> None:
    assert normalize_name("  KÍNG   Nuggets® — M&M's ! ") == "king nuggets m m s"


def test_versioned_alias_matches_quantity_variant_before_other_methods() -> None:
    catalog = _catalog(products=(_product("King Nuggets® (4)", 350, route="nuggets-4"),))

    result = match_reward(_reward("4 King Nuggets®"), catalog)

    assert result.status == "matched"
    assert result.method == "alias-v1"
    assert result.price_cents == 350


def test_known_route_is_resolved_before_exact_name() -> None:
    catalog = _catalog(products=(_product("Cheeseburger", 295, route="cheeseburger-cc"),))

    result = match_reward(_reward("cheeseburger-cc"), catalog)

    assert result.status == "matched"
    assert result.method == "route"


def test_exact_normalized_product_name_matches() -> None:
    catalog = _catalog(products=(_product("Whopper®", 650, route="whopper-product"),))

    result = match_reward(_reward("Whopper"), catalog)

    assert result.status == "matched"
    assert result.method == "exact"
    assert result.price_cents == 650


def test_regular_and_king_size_menu_rules_select_m_and_l() -> None:
    menu = Menu(
        id="11",
        plu="11",
        name="Whopper®",
        route_id="whopper",
        available=True,
        active=True,
        sizes=(MenuSize("11-M", "M", 890), MenuSize("11-L", "L", 985)),
        category_ids=("normal",),
    )
    catalog = _catalog(menus=(menu,))

    regular = match_reward(_reward("Menu Whopper®", kind="menu", size="M"), catalog)
    king_size = match_reward(_reward("Menu King Size Whopper®", kind="menu", size="L"), catalog)

    assert regular.price_cents == 890
    assert king_size.price_cents == 985


def test_menu_route_without_requested_size_falls_through_to_exact_public_menu() -> None:
    kingdom = Menu(
        id="kingdom",
        plu="kingdom",
        name="[KINGDOM] Menu Double Whopper Cheese",
        route_id="menu-double-whopper-cheese",
        available=True,
        active=True,
        sizes=(MenuSize("kd-M", "M", 1180),),
        category_ids=(),
    )
    public = Menu(
        id="public",
        plu="public",
        name="Double Whopper Cheese",
        route_id="double-whopper-cheese",
        available=True,
        active=True,
        sizes=(MenuSize("public-M", "M", 1180), MenuSize("public-L", "L", 1275)),
        category_ids=("normal",),
    )

    result = match_reward(
        _reward("Menu King Size Double Whopper Cheese", kind="menu", size="L"),
        _catalog(menus=(kingdom, public)),
    )

    assert result.status == "matched"
    assert result.candidate_id == "public"
    assert result.price_cents == 1275


def test_box_uses_product_alone_price() -> None:
    catalog = _catalog(
        products=(_product("King Box Tenders", 1090, route="king-box-tenders", kind="box"),)
    )

    result = match_reward(_reward("King Box Tenders", kind="box"), catalog)

    assert result.price_cents == 1090


def test_candidate_preference_uses_normal_available_active_item_not_highest_price() -> None:
    catalog = _catalog(
        products=(
            _product("Cheeseburger", 700, route="hidden", item_id="hidden", categories=()),
            _product("Cheeseburger", 295, route="normal", item_id="normal"),
            _product(
                "Cheeseburger",
                400,
                route="inactive",
                item_id="inactive",
                active=False,
            ),
        )
    )

    result = match_reward(_reward("Cheeseburger"), catalog)

    assert result.status == "matched"
    assert result.candidate_id == "normal"
    assert result.price_cents == 295


def test_equally_preferred_candidates_with_different_prices_are_ambiguous() -> None:
    catalog = _catalog(
        products=(
            _product("Produit double", 400, route="double-a", item_id="a"),
            _product("Produit double", 500, route="double-b", item_id="b"),
        )
    )

    result = match_reward(_reward("Produit double"), catalog)

    assert result.status == "ambiguous"
    assert result.price_cents is None
    assert "400" in (result.diagnostic or "")
    assert "500" in (result.diagnostic or "")


def test_only_unavailable_catalog_candidate_is_reported_for_badging() -> None:
    catalog = _catalog(
        products=(
            _product(
                "Produit saisonnier",
                450,
                route="saisonnier",
                available=False,
            ),
        )
    )

    result = match_reward(_reward("Produit saisonnier"), catalog)

    assert result.status == "matched"
    assert result.candidate_available is False


def test_fuzzy_similarity_is_diagnostic_only_and_unmatched_is_reported() -> None:
    catalog = _catalog(products=(_product("Whopper Cheese", 700, route="whopper-cheese"),))

    report = match_rewards((_reward("Whopper Cheeze"),), catalog)
    result = report.results[0]

    assert result.status == "unmatched"
    assert result.price_cents is None
    assert result.suggestions == ("Whopper Cheese",)
    assert report.matched_count == 0
    assert report.total_count == 1
    assert report.match_rate == 0.0
