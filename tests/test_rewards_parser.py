import json
from pathlib import Path

import pytest

from bk_crowns.rewards_parser import (
    CGU_URL,
    RewardParseError,
    choose_reward_source,
    parse_rewards,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_rewards_parser_extracts_dynamic_tiers_and_products() -> None:
    rewards = parse_rewards((FIXTURES / "rewards-cgu-reduced.html").read_text(), CGU_URL)

    assert {reward.crowns for reward in rewards} == {35, 75, 125, 155}
    assert len(rewards) == 15
    assert (
        next(reward for reward in rewards if reward.name.startswith("4 King Nuggets")).crowns == 35
    )


def test_rewards_parser_extracts_availability_seasonality_and_types() -> None:
    rewards = parse_rewards((FIXTURES / "rewards-cgu-reduced.html").read_text(), CGU_URL)

    cheeseburger = next(reward for reward in rewards if reward.name == "Cheeseburger")
    seasonal = next(reward for reward in rewards if reward.name.startswith("Burger éphémère"))
    donut = next(reward for reward in rewards if reward.name.startswith("Donut"))
    box = next(reward for reward in rewards if reward.name.startswith("Box"))

    assert cheeseburger.available is False
    assert seasonal.seasonal is True
    assert donut.kind == "dessert"
    assert box.kind == "box"


def test_rewards_parser_applies_documented_menu_size_defaults() -> None:
    rewards = parse_rewards((FIXTURES / "rewards-cgu-reduced.html").read_text(), CGU_URL)

    regular = next(
        reward
        for reward in rewards
        if reward.name.startswith("Menu Whopper") and "King Size" not in reward.name
    )
    king_size = next(reward for reward in rewards if "King Size" in reward.name)

    assert regular.kind == "menu"
    assert regular.menu_size == "M"
    assert king_size.menu_size == "L"


def test_rewards_parser_decodes_html_nested_in_rsc_json() -> None:
    embedded = "<ul><li><strong>44 Couronnes</strong><br>Un Hamburger ou Un Cookie.</li></ul>"
    chunk = "8:" + json.dumps({"content": embedded})
    html = f"<script>self.__next_f.push({json.dumps([1, chunk])})</script>"

    rewards = parse_rewards(html, "https://public.example/rewards")

    assert [(reward.crowns, reward.name) for reward in rewards] == [
        (44, "Hamburger"),
        (44, "Cookie"),
    ]


def test_rewards_parser_reassembles_rsc_fragments_split_inside_nested_html() -> None:
    embedded = "<ul><li><strong>44 Couronnes</strong><br>Un Hamburger ou Un Cookie.</li></ul>"
    chunk = "8:" + json.dumps({"content": embedded})
    split_at = chunk.index("Hamburger") + 4
    html = "".join(
        f"<script>self.__next_f.push({json.dumps([1, fragment])})</script>"
        for fragment in (chunk[:split_at], chunk[split_at:])
    )

    rewards = parse_rewards(html, "https://public.example/rewards")

    assert [reward.name for reward in rewards] == ["Hamburger", "Cookie"]


def test_reward_source_falls_back_when_primary_has_no_complete_tiers() -> None:
    primary = (FIXTURES / "rewards-primary-reduced.html").read_bytes()
    fallback = (FIXTURES / "rewards-cgu-reduced.html").read_bytes()

    result = choose_reward_source(primary, fallback)

    assert result.source_url == CGU_URL
    assert result.used_fallback is True
    assert len(result.rewards) == 15
    assert result.warning is not None


def test_reward_source_never_accepts_an_incomplete_ranking() -> None:
    primary = b"<ul><li>40 Couronnes<br>Un Hamburger.</li></ul>"

    with pytest.raises(RewardParseError, match="incomplet"):
        choose_reward_source(primary, None)


def test_rewards_parser_ignores_crown_earning_rules_inside_list_body() -> None:
    html = """
    <ol>
      <li>Le programme attribue 2 Couronnes par euro.
        <ul><li><strong>40 Couronnes — Palier</strong><br>Un Hamburger.</li></ul>
      </li>
    </ol>
    """

    rewards = parse_rewards(html, CGU_URL)

    assert [(reward.crowns, reward.name) for reward in rewards] == [(40, "Hamburger")]


def test_rewards_parser_splits_missing_comma_between_sized_drinks() -> None:
    html = """
    <ul><li><strong>40 Couronnes</strong><br>
      Un Coca-Cola (25cl) Un Fanta sans sucres (25cl).
    </li></ul>
    """

    rewards = parse_rewards(html, CGU_URL)

    assert [reward.name for reward in rewards] == [
        "Coca-Cola (25cl)",
        "Fanta sans sucres (25cl)",
    ]
