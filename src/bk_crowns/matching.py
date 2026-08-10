"""Deterministic reward-to-catalogue matching and centralized price rules."""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from .models import Catalog, MatchReport, MatchResult, Menu, Product, Reward

_MARKS = str.maketrans({"®": "", "™": "", "©": ""})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(value: str) -> str:
    """Return a conservative comparison key for external product names."""

    value = value.translate(_MARKS).casefold()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    return " ".join(_NON_ALNUM.sub(" ", value).split())


@dataclass(frozen=True, slots=True)
class _Candidate:
    item: Product | Menu

    @property
    def id(self) -> str:
        return self.item.id

    @property
    def name(self) -> str:
        return self.item.name

    @property
    def route_id(self) -> str:
        return self.item.route_id

    @property
    def plu(self) -> str:
        return self.item.plu

    @property
    def kind(self) -> str:
        return "menu" if isinstance(self.item, Menu) else self.item.kind

    @property
    def category_ids(self) -> tuple[str, ...]:
        return self.item.category_ids

    @property
    def active(self) -> bool:
        return self.item.active

    @property
    def available(self) -> bool:
        return self.item.available


@lru_cache(maxsize=1)
def _aliases() -> dict[str, dict[str, object]]:
    raw = resources.files("bk_crowns.data").joinpath("aliases.v1.json").read_text("utf-8")
    document = json.loads(raw)
    if document.get("version") != 1 or not isinstance(document.get("aliases"), dict):
        raise RuntimeError("ressource d'alias v1 invalide")
    return {normalize_name(key): value for key, value in document["aliases"].items()}


def _candidates(reward: Reward, catalog: Catalog) -> list[_Candidate]:
    if reward.kind == "menu":
        return [_Candidate(menu) for menu in catalog.menus]
    return [_Candidate(product) for product in catalog.products]


def _reward_key(reward: Reward) -> str:
    value = reward.name
    if reward.kind == "menu":
        value = re.sub(r"^menu\s+king\s+size\s+", "Menu ", value, flags=re.IGNORECASE)
    return normalize_name(value)


def _candidate_keys(candidate: _Candidate) -> set[str]:
    keys = {normalize_name(candidate.name)}
    if candidate.kind == "menu":
        keys.add(normalize_name("Menu " + candidate.name))
    return keys


def select_price(reward: Reward, candidate: Product | Menu) -> int:
    """Apply product/box alone and menu M/L rules in one place."""

    if isinstance(candidate, Product):
        return candidate.prices["alone"]
    requested_size = (reward.menu_size or "M").upper()
    for size in candidate.sizes:
        if size.size.upper() == requested_size:
            return size.price
    raise ValueError(f"taille {requested_size} absente pour {candidate.name}")


def _resolve(reward: Reward, candidates: list[_Candidate], method: str) -> MatchResult:
    priced: list[tuple[_Candidate, int]] = []
    price_errors: list[str] = []
    for candidate in candidates:
        try:
            priced.append((candidate, select_price(reward, candidate.item)))
        except (KeyError, ValueError) as exc:
            price_errors.append(str(exc))
    if not priced:
        return MatchResult(
            reward=reward,
            status="unmatched",
            method=method,
            diagnostic="; ".join(price_errors) or "aucun prix public utilisable",
        )

    def preference(value: tuple[_Candidate, int]) -> tuple[bool, bool, bool]:
        candidate, _ = value
        return candidate.active, candidate.available, bool(candidate.category_ids)

    best_score = max(preference(value) for value in priced)
    preferred = [value for value in priced if preference(value) == best_score]
    prices = {price for _, price in preferred}
    if len(prices) > 1:
        price_list = ", ".join(str(price) for price in sorted(prices))
        names = ", ".join(f"{candidate.name} [{candidate.id}]" for candidate, _ in preferred)
        return MatchResult(
            reward=reward,
            status="ambiguous",
            method=method,
            diagnostic=f"candidats équivalents ({names}) aux prix {price_list} centimes",
        )

    candidate, price = min(preferred, key=lambda value: (value[0].id, value[0].route_id))
    return MatchResult(
        reward=reward,
        status="matched",
        candidate_name=candidate.name,
        candidate_id=candidate.id,
        candidate_kind=candidate.kind,
        price_cents=price,
        method=method,
        candidate_available=candidate.available,
        candidate_active=candidate.active,
    )


def match_reward(reward: Reward, catalog: Catalog) -> MatchResult:
    """Match one reward without ever accepting a fuzzy suggestion."""

    candidates = _candidates(reward, catalog)
    reward_key = _reward_key(reward)
    failed_resolution: MatchResult | None = None

    alias = _aliases().get(normalize_name(reward.name))
    if alias is not None:
        routes = alias.get("routes", [])
        alias_kind = alias.get("kind")
        matching = [
            candidate
            for candidate in candidates
            if candidate.route_id in routes and (alias_kind is None or candidate.kind == alias_kind)
        ]
        if matching:
            result = _resolve(reward, matching, "alias-v1")
            if result.status != "unmatched":
                return result
            failed_resolution = result

    route_matches = [
        candidate
        for candidate in candidates
        if reward_key == normalize_name(candidate.route_id) or reward.name == candidate.plu
    ]
    if route_matches:
        result = _resolve(reward, route_matches, "route")
        if result.status != "unmatched":
            return result
        failed_resolution = result

    exact_matches = [
        candidate for candidate in candidates if reward_key in _candidate_keys(candidate)
    ]
    if exact_matches:
        return _resolve(reward, exact_matches, "exact")

    if failed_resolution is not None:
        return failed_resolution

    key_to_names: dict[str, list[str]] = {}
    for candidate in candidates:
        for key in _candidate_keys(candidate):
            key_to_names.setdefault(key, []).append(candidate.name)
    close_keys = difflib.get_close_matches(reward_key, key_to_names, n=3, cutoff=0.65)
    suggestions = tuple(dict.fromkeys(name for key in close_keys for name in key_to_names[key]))
    return MatchResult(
        reward=reward,
        status="unmatched",
        diagnostic="aucune correspondance exacte",
        suggestions=suggestions,
    )


def match_rewards(rewards: tuple[Reward, ...], catalog: Catalog) -> MatchReport:
    results = tuple(match_reward(reward, catalog) for reward in rewards)
    matched_count = sum(result.status == "matched" for result in results)
    total_count = len(results)
    return MatchReport(
        results=results,
        matched_count=matched_count,
        total_count=total_count,
        match_rate=matched_count / total_count if total_count else 0.0,
    )
