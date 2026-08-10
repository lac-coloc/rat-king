import json
import os
from pathlib import Path

import pytest

from bk_crowns.catalog_parser import parse_catalog
from bk_crowns.config import Config
from bk_crowns.fetch import PublicFetcher
from bk_crowns.models import CacheMetadata
from bk_crowns.restaurants import parse_restaurant_directory
from bk_crowns.rewards_parser import RewardParseError, choose_reward_source

pytestmark = pytest.mark.skipif(
    os.getenv("BK_LIVE_TEST") != "1",
    reason="test réseau public désactivé ; utiliser BK_LIVE_TEST=1",
)


def _first_orderable_restaurant(content: bytes) -> str:
    document = json.loads(content)
    restaurants = document["restaurants"]
    entries = restaurants.values() if isinstance(restaurants, dict) else restaurants
    candidates = sorted(
        str(entry["id"])
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("closed") is not True
        and "CLICK_AND_COLLECT" in entry.get("services", ())
    )
    if not candidates:
        pytest.fail("aucun restaurant publiquement indiqué comme commandable")
    return candidates[0]


def test_live_public_sources_with_deterministic_directory_entry(tmp_path: Path) -> None:
    config = Config(state_dir=tmp_path, output_dir=tmp_path / "site")
    fetcher = PublicFetcher(config)

    restaurants = fetcher.get("restaurants", CacheMetadata()).content
    directory = parse_restaurant_directory(restaurants)
    selected = directory.get(_first_orderable_restaurant(restaurants))
    assert selected is not None

    catalogue = fetcher.get(
        "catalogue", CacheMetadata(), restaurant_id=selected.id
    ).content.decode()
    parsed_catalogue = parse_catalog(catalogue)
    assert len(parsed_catalogue.products) + len(parsed_catalogue.menus) >= 10

    tiers = fetcher.get("tiers", CacheMetadata()).content
    try:
        rewards = choose_reward_source(tiers, None)
    except RewardParseError:
        cgu = fetcher.get("cgu", CacheMetadata()).content
        rewards = choose_reward_source(tiers, cgu)
    assert len(rewards.rewards) >= 10
    assert len({reward.crowns for reward in rewards.rewards}) >= 3
