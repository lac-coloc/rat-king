"""Extraction of Kingdom rewards from public loyalty pages only."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from .models import Reward
from .rsc import RSCParseError, decode_next_chunks, iter_balanced_json

TIERS_URL = "https://www.burgerking.fr/paliers-kingdom"
CGU_URL = "https://www.burgerking.fr/page/cgu-fidelite"
MIN_LEVELS = 3
MIN_REWARDS = 10

_CROWNS = re.compile(r"(?P<crowns>[0-9]{1,4})\s+Couronnes?\b", re.IGNORECASE)
_SEPARATOR = re.compile(r"\s*(?:,|;|\bou\b)\s*", re.IGNORECASE)
_DETERMINER = re.compile(r"^(?:un|une|la|le|les)\s+", re.IGNORECASE)
_WRITTEN_NUMBER = re.compile(r"\b[A-Za-zÀ-ÿ'-]+\s*\(([0-9]+)\)")
_MISSING_SEPARATOR = re.compile(r"(?<=\))\s*(?=(?:Un|Une)\s)")
_BREAK = "\x1e"


class RewardParseError(ValueError):
    """Raised when no complete public reward list can be extracted."""


@dataclass(frozen=True, slots=True)
class RewardParseResult:
    rewards: tuple[Reward, ...]
    source_url: str
    used_fallback: bool
    warning: str | None = None


class _ListItemCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[list[str]] = []
        self.items: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered == "li":
            self._stack.append([])
        elif lowered == "br" and self._stack:
            self._stack[-1].append(_BREAK)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "li" or not self._stack:
            return
        parts = self._stack.pop()
        segments = [" ".join(segment.split()) for segment in "".join(parts).split(_BREAK)]
        self.items.append(_BREAK.join(segment for segment in segments if segment))

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1].append(data)


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    return []


def _list_items(html: str) -> list[str]:
    documents = [html]
    try:
        chunks = decode_next_chunks(html)
    except RSCParseError as exc:
        raise RewardParseError(f"données RSC de récompenses invalides : {exc}") from exc
    rsc_stream = "".join(chunks)
    documents.append(rsc_stream)
    for value in iter_balanced_json(rsc_stream):
        documents.extend(_string_values(value))

    items: list[str] = []
    seen: set[str] = set()
    for document in documents:
        if "<li" not in document.lower():
            continue
        collector = _ListItemCollector()
        collector.feed(document)
        for item in collector.items:
            if item not in seen:
                seen.add(item)
                items.append(item)
    return items


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).lower()


def _classify(name: str) -> str:
    plain = _plain(name)
    if plain.startswith("menu "):
        return "menu"
    if "box" in plain or "festin" in plain:
        return "box"
    if any(word in plain for word in ("fusion", "sundae", "donut", "cookie", "muffin")):
        return "dessert"
    if any(word in plain for word in ("burger", "whopper", "steakhouse", "big king", "king fish")):
        return "burger"
    return "produit"


def _clean_name(raw_name: str) -> tuple[str, bool, bool]:
    available = "*" not in raw_name and "indisponibl" not in _plain(raw_name)
    seasonal = "saisonn" in _plain(raw_name) or "ephem" in _plain(raw_name)
    name = _WRITTEN_NUMBER.sub(r"\1", raw_name)
    name = _DETERMINER.sub("", name.strip())
    name = re.sub(r"\s*\*+\s*", " ", name)
    name = re.sub(r"\s*\((?:temporairement\s+)?indisponible\)\s*", " ", name, flags=re.I)
    name = re.sub(r"\s*\(saisonnier(?:e)?\)\s*", " ", name, flags=re.I)
    name = name.strip(" .:-\u2013\u2014\t\r\n")
    name = " ".join(name.split())
    return name, available, seasonal


def _parse_item(item: str, source_url: str) -> list[Reward]:
    tier = _CROWNS.search(item)
    if tier is None or tier.start() > 80:
        return []
    tail = item[tier.end() :]
    if _BREAK not in tail:
        return []
    tail = tail.split(_BREAK, 1)[1].replace(_BREAK, ", ")
    tail = _MISSING_SEPARATOR.sub(", ", tail)

    rewards: list[Reward] = []
    for token in _SEPARATOR.split(tail):
        name, available, seasonal = _clean_name(token)
        if not name:
            continue
        kind = _classify(name)
        menu_size = "L" if kind == "menu" and "king size" in _plain(name) else None
        if kind == "menu" and menu_size is None:
            menu_size = "M"
        rewards.append(
            Reward(
                crowns=int(tier.group("crowns")),
                name=name,
                kind=kind,
                menu_size=menu_size,
                seasonal=seasonal,
                available=available,
                source_url=source_url,
            )
        )
    return rewards


def parse_rewards(html: str, source_url: str) -> list[Reward]:
    """Extract all reward choices from tier list items in a public page."""

    rewards = [reward for item in _list_items(html) for reward in _parse_item(item, source_url)]
    unique: list[Reward] = []
    seen: set[tuple[int, str]] = set()
    for reward in rewards:
        key = (reward.crowns, _plain(reward.name))
        if key not in seen:
            seen.add(key)
            unique.append(reward)
    if not unique:
        raise RewardParseError("aucune récompense publique trouvée")
    return unique


def _complete(rewards: list[Reward]) -> bool:
    return len({reward.crowns for reward in rewards}) >= MIN_LEVELS and len(rewards) >= MIN_REWARDS


def choose_reward_source(primary: bytes, fallback: bytes | None) -> RewardParseResult:
    """Prefer the tiers page, falling back to the public loyalty terms."""

    try:
        primary_rewards = parse_rewards(primary.decode("utf-8"), TIERS_URL)
    except (RewardParseError, UnicodeDecodeError):
        primary_rewards = []
    if _complete(primary_rewards):
        return RewardParseResult(tuple(primary_rewards), TIERS_URL, False)

    if fallback is not None:
        try:
            fallback_rewards = parse_rewards(fallback.decode("utf-8"), CGU_URL)
        except (RewardParseError, UnicodeDecodeError):
            fallback_rewards = []
        if _complete(fallback_rewards):
            return RewardParseResult(
                tuple(fallback_rewards),
                CGU_URL,
                True,
                "La page des paliers était incomplète ; les CGU publiques ont été utilisées.",
            )

    raise RewardParseError(
        "classement incomplet : plusieurs paliers et au moins dix récompenses sont requis"
    )
