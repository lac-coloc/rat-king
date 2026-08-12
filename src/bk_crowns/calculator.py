"""Reward value formulas and same-family structural domination checks."""

from __future__ import annotations

import math
import re

from .matching import normalize_name
from .models import CalculationReport, MatchReport, RankedReward

_FAMILIES = {
    "onion rings": "Onion Rings",
    "king nuggets": "King Nuggets",
    "chili cheese": "Chili Cheese",
}


def _portion(row: RankedReward) -> tuple[int, str, str] | None:
    if row.reward.kind in {"menu", "box"}:
        return None
    name = normalize_name(row.reward.name)
    for key, display in _FAMILIES.items():
        match = re.search(rf"\b([0-9]+)\s+{re.escape(key)}\b", name)
        if match:
            return int(match.group(1)), key, display
    return None


def mark_dominated(rows: list[RankedReward]) -> None:
    """Mark portions replaceable by integer orders of a smaller same-family reward."""

    for target in rows:
        if not target.reward.available or not target.catalog_available or not target.catalog_active:
            continue
        target_portion = _portion(target)
        if target_portion is None:
            continue
        target_quantity, target_family, display_family = target_portion
        alternatives: list[tuple[int, int, int, RankedReward]] = []
        for source in rows:
            if (
                source is target
                or source.reward.crowns >= target.reward.crowns
                or not source.reward.available
                or not source.catalog_available
                or not source.catalog_active
            ):
                continue
            source_portion = _portion(source)
            if source_portion is None or source_portion[1] != target_family:
                continue
            source_quantity = source_portion[0]
            orders = math.ceil(target_quantity / source_quantity)
            crown_cost = orders * source.reward.crowns
            total_quantity = orders * source_quantity
            if crown_cost <= target.reward.crowns and total_quantity >= target_quantity:
                alternatives.append((crown_cost, orders, -total_quantity, source))

        if not alternatives:
            continue
        crown_cost, orders, negative_quantity, source = min(
            alternatives,
            key=lambda alternative: (
                alternative[0],
                alternative[1],
                alternative[2],
                normalize_name(alternative[3].reward.name),
                normalize_name(alternative[3].candidate_name),
            ),
        )
        total_quantity = -negative_quantity
        target.dominated = True
        target.domination_note = (
            f"{orders} retraits de « {source.reward.name} » donnent "
            f"{total_quantity} {display_family} pour {crown_cost} Couronnes. "
            f"Cela nécessite {orders} commandes, BK limitant le retrait à un avantage par "
            "commande."
        )


def calculate(match_report: MatchReport) -> CalculationReport:
    """Calculate all public value metrics and sort best value first."""

    rows: list[RankedReward] = []
    diagnostics = []
    for result in match_report.results:
        if result.status != "matched" or result.price_cents is None:
            diagnostics.append(result)
            continue
        price_euros = result.price_cents / 100
        crowns = result.reward.crowns
        rows.append(
            RankedReward(
                reward=result.reward,
                candidate_name=result.candidate_name or result.reward.name,
                candidate_kind=result.candidate_kind or result.reward.kind,
                price_cents=result.price_cents,
                price_euros=price_euros,
                euros_per_crown=price_euros / crowns,
                required_spend=crowns / 2,
                yield_percent=200 * price_euros / crowns,
                catalog_available=result.candidate_available is not False,
                catalog_active=result.candidate_active is not False,
                match_method=result.method,
            )
        )

    rows.sort(key=lambda row: (-row.euros_per_crown, row.reward.crowns, row.reward.name))
    mark_dominated(rows)
    return CalculationReport(tuple(rows), tuple(diagnostics), match_report.match_rate)
