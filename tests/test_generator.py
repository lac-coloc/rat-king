import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bk_crowns.coordinator import AdmissionDecision
from bk_crowns.generator import (
    render_comparator,
    render_home,
    render_restaurant_waiting,
    render_site,
    static_asset,
)
from bk_crowns.models import (
    CalculationReport,
    MatchResult,
    RankedReward,
    Restaurant,
    Reward,
    SharedData,
    SnapshotData,
)


def _reward() -> Reward:
    return Reward(
        crowns=40,
        name="4 King Nuggets® <script>alert(1)</script>",
        kind="produit",
        menu_size=None,
        seasonal=True,
        available=False,
        source_url="https://www.burgerking.fr/page/cgu-fidelite",
    )


def _snapshot() -> SnapshotData:
    reward = _reward()
    row = RankedReward(
        reward=reward,
        candidate_name="King Nuggets® (4)",
        candidate_kind="produit",
        price_cents=350,
        price_euros=3.5,
        euros_per_crown=0.0875,
        required_spend=20.0,
        yield_percent=17.5,
        catalog_available=False,
        catalog_active=True,
        match_method="alias-v1",
        dominated=True,
        domination_note="Deux retraits nécessitent 2 commandes.",
    )
    diagnostic_reward = Reward(
        80,
        "Produit ambigu <b>externe</b>",
        "dessert",
        None,
        False,
        True,
        "https://www.burgerking.fr/paliers-kingdom",
    )
    diagnostic = MatchResult(
        diagnostic_reward,
        "ambiguous",
        diagnostic="deux prix publics",
        suggestions=("Produit proche",),
    )
    return SnapshotData(
        restaurant=Restaurant(
            "K2001",
            "Ville Exemple <Centre>",
            "10 rue <script>externe</script>",
            "Ville Exemple",
            "12345",
        ),
        refreshed_at=datetime(2026, 8, 10, 8, 30, tzinfo=UTC),
        source_urls=(
            "https://ecoceabkstorageprdnorth.blob.core.windows.net/static/restaurants.json",
            "https://www.burgerking.fr/commande/K2001/a-emporter/catalogue/home",
            "https://www.burgerking.fr/page/cgu-fidelite",
        ),
        report=CalculationReport((row,), (diagnostic,), 0.5),
        reward_count=2,
        matched_count=1,
        match_rate=0.5,
        shared_snapshot_id="shared-1",
    )


def _shared() -> SharedData:
    return SharedData(
        restaurants=(
            Restaurant(
                "K2001",
                "Restaurant qui ne doit pas être présélectionné",
                "Adresse non affichée",
                "Ville Exemple",
                "12345",
            ),
        ),
        rewards=(_reward(),),
        refreshed_at=datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
        source_urls=(
            "https://ecoceabkstorageprdnorth.blob.core.windows.net/static/restaurants.json",
            "https://www.burgerking.fr/paliers-kingdom",
        ),
    )


def test_home_is_neutral_and_contains_local_search_controls() -> None:
    html = render_home(_shared(), {"ready": True, "state": "fresh"}).decode()

    assert 'id="restaurant-search"' in html
    assert 'data-search-url="/api/restaurants"' in html
    assert "Restaurant qui ne doit pas être présélectionné" not in html
    assert "Adresse non affichée" not in html
    assert "navigator.geolocation" not in html
    assert "localStorage" not in html
    assert 'src="/assets/search.js"' in html


def test_home_explains_shared_initialization_without_revealing_a_default() -> None:
    html = render_home(None, {"ready": False, "state": "initializing"}).decode()

    assert "sources publiques" in html
    assert "initialisation" in html.casefold()
    assert "K2001" not in html


def test_search_script_uses_safe_dom_nodes_and_local_api() -> None:
    script = static_asset("search.js").decode()

    assert "textContent" in script
    assert "createElement" in script
    assert "innerHTML" not in script
    assert "fetch(" in script
    assert "burgerking.fr" not in script


def test_waiting_page_polls_only_local_status_and_escapes_external_data() -> None:
    restaurant = Restaurant(
        "K2001",
        "Nom <script>alert(1)</script>",
        "Adresse <b>publique</b>",
        "Ville Exemple",
        "12345",
    )

    html = render_restaurant_waiting(
        restaurant,
        AdmissionDecision("queued", True, 1, None),
        "/api/restaurants/K2001/status",
    ).decode()

    assert 'data-status-url="/api/restaurants/K2001/status"' in html
    assert "Nom &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Adresse &lt;b&gt;publique&lt;/b&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert 'src="/assets/waiting.js"' in html
    assert "burgerking.fr" not in html


def test_generator_escapes_external_html_and_writes_json(tmp_path: Path) -> None:
    render_site(_snapshot(), tmp_path)

    html = (tmp_path / "index.html").read_text()
    data = json.loads((tmp_path / "data.json").read_text())
    assert "Ville Exemple &lt;Centre&gt;" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "Produit ambigu &lt;b&gt;externe&lt;/b&gt;" in html
    assert data["restaurant"]["id"] == "K2001"


def test_generator_contains_controls_columns_badges_and_back_link(tmp_path: Path) -> None:
    render_site(_snapshot(), tmp_path)

    html = (tmp_path / "index.html").read_text()
    for expected in (
        "Rat King",
        "Filtrer par palier",
        "Filtrer par type",
        "Rechercher",
        "Prix public",
        "Couronnes",
        "€ / Couronne",
        "Rendement",
        "Dépense nécessaire",
        "dominé",
        "saisonnier",
        "indisponible",
        "Récompenses non appariées ou ambiguës",
        "Taux d\u2019appariement",
    ):
        assert expected in html
    assert 'data-sort="yield"' in html
    assert 'data-tier="40"' in html
    assert 'data-kind="produit"' in html
    assert 'href="/"' in html


def test_comparator_uses_only_absolute_local_loaded_assets(tmp_path: Path) -> None:
    render_site(_snapshot(), tmp_path)

    html = (tmp_path / "index.html").read_text()
    assert '<link rel="stylesheet" href="/assets/app.css">' in html
    assert '<script src="/assets/app.js" defer></script>' in html
    assert 'data-status-url="/api/restaurants/K2001/status"' in html
    assert not re.search(r'<(?:script|link|img)[^>]+(?:src|href)="https?://', html)
    assert 'href="https://www.burgerking.fr/page/cgu-fidelite"' in html


def test_comparator_can_render_visible_stale_and_error_states() -> None:
    stale = render_comparator(_snapshot(), "stale").decode()
    error = render_comparator(_snapshot(), "error").decode()

    assert "Données anciennes" in stale
    assert 'class="status status--stale"' in stale
    assert "Dernier refresh en erreur" in error
    assert 'class="status status--error"' in error


def test_static_assets_are_allowlisted() -> None:
    assert static_asset("app.css")
    assert static_asset("app.js")
    assert static_asset("waiting.js")
    with pytest.raises(KeyError):
        static_asset("../templates/index.html.j2")
