from bk_crowns.calculator import calculate
from bk_crowns.models import MatchReport, MatchResult, Reward


def _reward(name: str, crowns: int, *, kind: str = "produit") -> Reward:
    return Reward(
        crowns=crowns,
        name=name,
        kind=kind,
        menu_size="M" if kind == "menu" else None,
        seasonal=False,
        available=True,
        source_url="https://public.example/rewards",
    )


def _matched(
    name: str,
    crowns: int,
    price_cents: int,
    *,
    kind: str = "produit",
    catalog_available: bool = True,
    catalog_active: bool = True,
    reward_available: bool = True,
) -> MatchResult:
    reward = _reward(name, crowns, kind=kind)
    if not reward_available:
        reward = Reward(
            crowns=reward.crowns,
            name=reward.name,
            kind=reward.kind,
            menu_size=reward.menu_size,
            seasonal=reward.seasonal,
            available=False,
            source_url=reward.source_url,
        )
    return MatchResult(
        reward=reward,
        status="matched",
        candidate_name=name,
        candidate_id=name,
        candidate_kind=kind,
        price_cents=price_cents,
        method="exact",
        candidate_available=catalog_available,
        candidate_active=catalog_active,
    )


def _report(*results: MatchResult) -> MatchReport:
    matched = sum(result.status == "matched" for result in results)
    return MatchReport(tuple(results), matched, len(results), matched / len(results))


def test_calculator_applies_all_documented_formulas() -> None:
    calculated = calculate(_report(_matched("4 King Nuggets", 40, 350)))

    row = calculated.rows[0]
    assert row.price_euros == 3.5
    assert row.euros_per_crown == 0.0875
    assert row.required_spend == 20.0
    assert row.yield_percent == 17.5


def test_calculator_sorts_by_descending_euros_per_crown() -> None:
    calculated = calculate(
        _report(
            _matched("Valeur faible", 80, 400),
            _matched("Valeur haute", 40, 350),
            _matched("Valeur moyenne", 40, 300),
        )
    )

    assert [row.reward.name for row in calculated.rows] == [
        "Valeur haute",
        "Valeur moyenne",
        "Valeur faible",
    ]


def test_calculator_keeps_unmatched_and_ambiguous_diagnostics() -> None:
    unmatched = MatchResult(_reward("Inconnu", 40), "unmatched", suggestions=("Connu",))
    ambiguous = MatchResult(_reward("Double", 80), "ambiguous", diagnostic="deux prix")

    calculated = calculate(_report(_matched("Valide", 40, 300), unmatched, ambiguous))

    assert [item.status for item in calculated.diagnostics] == ["unmatched", "ambiguous"]
    assert calculated.match_rate == 1 / 3


def test_calculator_propagates_catalogue_unavailability_for_badges() -> None:
    calculated = calculate(_report(_matched("Saisonnier", 40, 450, catalog_available=False)))

    assert calculated.rows[0].catalog_available is False


def test_larger_onion_ring_reward_is_dominated_by_two_smaller_rewards() -> None:
    calculated = calculate(
        _report(
            _matched("6 Onion Rings", 40, 295),
            _matched("9 Onion Rings", 80, 330),
        )
    )

    target = next(row for row in calculated.rows if row.reward.name == "9 Onion Rings")
    assert target.dominated is True
    assert "12 Onion Rings" in (target.domination_note or "")
    assert "2 commandes" in (target.domination_note or "")
    assert "un avantage par commande" in (target.domination_note or "")


def test_nugget_portion_can_be_structurally_dominated() -> None:
    calculated = calculate(
        _report(
            _matched("4 King Nuggets", 40, 350),
            _matched("6 King Nuggets", 80, 500),
        )
    )

    target = next(row for row in calculated.rows if row.reward.name == "6 King Nuggets")
    assert target.dominated is True


def test_different_foods_and_menus_are_never_treated_as_substitutes() -> None:
    calculated = calculate(
        _report(
            _matched("6 Onion Rings", 40, 295),
            _matched("6 King Nuggets", 80, 500),
            _matched("Menu King Nuggets de 7 nuggets", 150, 790, kind="menu"),
        )
    )

    assert all(row.dominated is False for row in calculated.rows)


def test_equal_domination_alternatives_are_resolved_deterministically() -> None:
    calculated = calculate(
        _report(
            _matched("4 King Nuggets classique", 40, 350),
            _matched("4 King Nuggets épicé", 40, 350),
            _matched("12 King Nuggets", 120, 900),
        )
    )

    target = next(row for row in calculated.rows if row.reward.name == "12 King Nuggets")
    assert target.dominated is True
    assert "4 King Nuggets classique" in (target.domination_note or "")


def test_unavailable_or_inactive_rewards_never_dominate_an_available_reward() -> None:
    calculated = calculate(
        _report(
            _matched("4 King Nuggets indisponibles", 40, 350, reward_available=False),
            _matched("4 King Nuggets inactifs", 40, 350, catalog_active=False),
            _matched("4 King Nuggets catalogue indisponible", 40, 350, catalog_available=False),
            _matched("6 King Nuggets", 80, 500),
        )
    )

    target = next(row for row in calculated.rows if row.reward.name == "6 King Nuggets")
    assert target.dominated is False
