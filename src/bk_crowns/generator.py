"""Autoescaped, autonomous static-site generation."""

from __future__ import annotations

import json
from datetime import UTC
from importlib import resources
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .coordinator import AdmissionDecision
from .models import Restaurant, SharedData, SnapshotData

STATIC_ASSETS = frozenset({"app.css", "app.js", "search.js", "waiting.js"})
PUBLIC_SOURCE_LINKS = (
    "https://ecoceabkstorageprdnorth.blob.core.windows.net/static/restaurants.json",
    "https://www.burgerking.fr/paliers-kingdom",
    "https://www.burgerking.fr/page/cgu-fidelite",
)


def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("bk_crowns", "templates"),
        autoescape=select_autoescape(("html", "xml"), default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_site(data: SnapshotData, destination: Path) -> None:
    """Write the complete local-only site and its displayed JSON data."""

    destination.mkdir(parents=True, exist_ok=True)
    (destination / "index.html").write_bytes(render_comparator(data, "fresh"))
    (destination / "data.json").write_text(
        json.dumps(data.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def render_comparator(data: SnapshotData, status_state: str) -> bytes:
    """Render one immutable dataset with its current local freshness state."""

    tiers = sorted({row.reward.crowns for row in data.report.rows})
    kinds = sorted({row.reward.kind for row in data.report.rows})
    if status_state in {"stale", "queued_stale", "refreshing_stale", "stale_backoff"}:
        status_label = "Données anciennes"
        status_class = "stale"
    elif status_state == "error":
        status_label = "Dernier refresh en erreur"
        status_class = "error"
    else:
        status_label = "Données fraîches"
        status_class = "fresh"
    rendered = (
        _environment()
        .get_template("index.html.j2")
        .render(
            data=data,
            tiers=tiers,
            kinds=kinds,
            refreshed_label=data.refreshed_at.astimezone(UTC).strftime("%d/%m/%Y à %H:%M UTC"),
            status_state=status_state,
            status_label=status_label,
            status_class=status_class,
            status_url=f"/api/restaurants/{data.restaurant.id}/status",
        )
    )
    return rendered.encode("utf-8")


def render_home(shared: SharedData | None, status: dict[str, object]) -> bytes:
    """Render the neutral selector without listing or selecting a restaurant."""

    refreshed_label = None
    if shared is not None:
        refreshed_label = shared.refreshed_at.astimezone(UTC).strftime("%d/%m/%Y à %H:%M UTC")
    rendered = (
        _environment()
        .get_template("home.html.j2")
        .render(
            ready=bool(status.get("ready")),
            state=str(status.get("state", "initializing")),
            refreshed_label=refreshed_label,
            restaurant_count=len(shared.restaurants) if shared is not None else 0,
            source_urls=PUBLIC_SOURCE_LINKS,
        )
    )
    return rendered.encode("utf-8")


def render_restaurant_waiting(
    restaurant: Restaurant,
    decision: AdmissionDecision,
    status_url: str,
) -> bytes:
    """Render a safe cold-cache/deferred page polling only local status."""

    rendered = (
        _environment()
        .get_template("restaurant_waiting.html.j2")
        .render(
            restaurant=restaurant,
            decision=decision,
            status_url=status_url,
        )
    )
    return rendered.encode("utf-8")


def static_asset(name: str) -> bytes:
    """Read one packaged static asset through an exact allow-list."""

    if name not in STATIC_ASSETS:
        raise KeyError(name)
    return resources.files("bk_crowns").joinpath("static", name).read_bytes()
