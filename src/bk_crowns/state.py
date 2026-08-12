"""Atomic shared and per-restaurant snapshot persistence."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config, validate_restaurant_id
from .models import (
    CacheMetadata,
    CalculationReport,
    MatchResult,
    RankedReward,
    Restaurant,
    Reward,
    SharedData,
    SnapshotData,
    Status,
)

_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9._-]+$")
SNAPSHOT_RETENTION = 2
MIN_REWARD_COUNT = 10
MIN_TIER_COUNT = 3
MIN_MATCH_COUNT = 5
MIN_MATCH_RATE = 0.20
MAX_CROWNS = 10_000
MAX_PRICE_CENTS = 100_000
_KINDS = {"produit", "burger", "dessert", "menu", "box"}
_MATCH_STATUSES = {"matched", "unmatched", "ambiguous"}


class StateError(RuntimeError):
    """Raised when durable state cannot be trusted."""


def _datetime(value: object, *, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise StateError("date requise absente")
        return None
    if not isinstance(value, str):
        raise StateError("date de statut invalide")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StateError("date de statut invalide") from exc
    if parsed.tzinfo is None:
        raise StateError("date de statut sans fuseau")
    return parsed.astimezone(UTC)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise StateError(f"{label} doit être une liste")
    return value


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise StateError(f"{label} invalide")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise StateError(f"{label} doit être un booléen")
    return value


def _integer(value: object, label: str, *, minimum: int = 0, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StateError(f"{label} invalide")
    return value


def _number(
    value: object,
    label: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateError(f"{label} invalide")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < minimum or (maximum is not None and parsed > maximum):
        raise StateError(f"{label} invalide")
    return parsed


def _source_urls(value: object) -> tuple[str, ...]:
    urls = tuple(_text(item, "URL source") for item in _list(value, "sources"))
    if not urls or any(not url.startswith("https://") for url in urls):
        raise StateError("URLs sources publiques invalides")
    return urls


def atomic_write_json(path: Path, payload: object) -> None:
    """Durably replace a small JSON file within its existing filesystem."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SnapshotStore:
    """Publish immutable generations through an atomic JSON pointer."""

    def __init__(
        self,
        root: Path,
        required_paths: tuple[str, ...],
        public_link: Path | None = None,
    ) -> None:
        self.root = root
        self.state_dir = root
        self.required_paths = tuple(Path(item) for item in required_paths)
        if any(path.is_absolute() or ".." in path.parts for path in self.required_paths):
            raise StateError("chemin requis de snapshot invalide")
        self.public_link = public_link
        self.output_dir = public_link
        self.snapshots_dir = root / "snapshots"
        self.status_path = root / "status.json"
        self.current_path_file = root / "current.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def create_staging(self) -> Path:
        return Path(tempfile.mkdtemp(prefix=".refresh-", dir=self.root))

    def _validate_staging(self, staging: Path, status: Status) -> None:
        if status.snapshot_id is None or not _SNAPSHOT_ID.fullmatch(status.snapshot_id):
            raise StateError("identifiant de snapshot invalide")
        try:
            staging.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise StateError("répertoire temporaire hors du stockage") from exc
        missing_required = any(not (staging / path).exists() for path in self.required_paths)
        if staging.is_symlink() or missing_required:
            raise StateError("snapshot temporaire incomplet")
        if (
            self.public_link is not None
            and os.path.lexists(self.public_link)
            and not self.public_link.is_symlink()
        ):
            raise StateError("le chemin du site public n'est pas géré par Rat King")

    def current_path(self) -> Path:
        try:
            document = json.loads(self.current_path_file.read_text("utf-8"))
            snapshot_id = document["snapshot_id"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateError("pointeur de snapshot illisible") from exc
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise StateError("pointeur de snapshot invalide")
        destination = self.snapshots_dir / snapshot_id
        if not destination.is_dir() or destination.is_symlink():
            raise StateError("pointeur vers un snapshot absent")
        return destination

    def _replace_public_link(self, target: Path) -> None:
        if self.public_link is None:
            return
        self.public_link.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.public_link.parent / f".site-link-{uuid.uuid4().hex}"
        try:
            os.symlink(target, temporary, target_is_directory=True)
            os.replace(temporary, self.public_link)
        finally:
            if os.path.lexists(temporary):
                temporary.unlink()

    def _restore_pointer(self, previous: Path | None) -> None:
        if previous is None:
            self.current_path_file.unlink(missing_ok=True)
            if self.public_link is not None and os.path.lexists(self.public_link):
                self.public_link.unlink()
            return
        atomic_write_json(self.current_path_file, {"snapshot_id": previous.name})
        self._replace_public_link(previous / "site")

    def _prune(self) -> None:
        try:
            current = self.current_path()
        except StateError:
            current = None
        snapshots = sorted(
            (
                path
                for path in self.snapshots_dir.iterdir()
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: path.name,
            reverse=True,
        )
        retained: set[Path] = set()
        if current is not None:
            retained.add(current)
        for snapshot in snapshots:
            if len(retained) >= SNAPSHOT_RETENTION:
                break
            retained.add(snapshot)
        for snapshot in snapshots:
            if snapshot not in retained:
                shutil.rmtree(snapshot)

    def publish(self, staging: Path, status: Status) -> Path:
        """Expose a complete generation while preserving rollback state."""

        self._validate_staging(staging, status)
        destination = self.snapshots_dir / str(status.snapshot_id)
        if destination.exists():
            raise StateError("un snapshot portant cet identifiant existe déjà")
        try:
            previous = self.current_path()
        except StateError:
            previous = None
        os.replace(staging, destination)
        try:
            atomic_write_json(self.current_path_file, {"snapshot_id": status.snapshot_id})
            self._replace_public_link(destination / "site")
            self.write_status(status)
        except Exception:
            try:
                self._restore_pointer(previous)
            except OSError as rollback_error:
                raise StateError("échec du rollback après publication") from rollback_error
            shutil.rmtree(destination, ignore_errors=True)
            raise
        self._prune()
        return destination

    def write_status(self, status: Status) -> None:
        atomic_write_json(self.status_path, status.to_dict())

    def load_status(self) -> Status:
        if not self.status_path.exists():
            return Status()
        try:
            document = json.loads(self.status_path.read_text("utf-8"))
            cache = {
                str(key): CacheMetadata(**value)
                for key, value in document.get("cache", {}).items()
                if isinstance(value, dict)
            }
            failures = document.get("consecutive_failures", 0)
            if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
                raise StateError("compteur d'échecs invalide")
            return Status(
                last_attempt=_datetime(document.get("last_attempt")),
                last_success=_datetime(document.get("last_success")),
                last_error=document.get("last_error"),
                snapshot_id=document.get("snapshot_id"),
                cache=cache,
                shared_snapshot_id=document.get("shared_snapshot_id"),
                retry_after=_datetime(document.get("retry_after")),
                consecutive_failures=failures,
            )
        except StateError:
            raise
        except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
            raise StateError("fichier de statut illisible") from exc

    def record_failure(self, error: Exception, *, retry_after: datetime | None = None) -> None:
        try:
            previous = self.load_status()
        except StateError:
            previous = Status()
        message = " ".join(str(error).split())[:500] or error.__class__.__name__
        self.write_status(
            Status(
                last_attempt=datetime.now(UTC),
                last_success=previous.last_success,
                last_error=message,
                snapshot_id=previous.snapshot_id,
                cache=previous.cache,
                shared_snapshot_id=previous.shared_snapshot_id,
                retry_after=retry_after,
                consecutive_failures=previous.consecutive_failures + 1,
            )
        )

    def is_ready(self) -> bool:
        try:
            current = self.current_path()
            status = self.load_status()
        except StateError:
            return False
        return (
            status.last_success is not None
            and status.snapshot_id == current.name
            and all((current / path).exists() for path in self.required_paths)
        )

    def status_payload(self, refresh_interval_seconds: int) -> dict[str, object]:
        try:
            status = self.load_status()
        except StateError as exc:
            return {"ready": False, "state": "error", "last_error": str(exc)}
        ready = self.is_ready()
        if status.last_error and (
            status.last_success is None
            or (status.last_attempt is not None and status.last_attempt > status.last_success)
        ):
            state = "error"
        elif not ready or status.last_success is None:
            state = "initializing"
        elif (datetime.now(UTC) - status.last_success).total_seconds() > refresh_interval_seconds:
            state = "stale"
        else:
            state = "fresh"
        return {**status.to_dict(), "ready": ready, "state": state}


class StateRepository:
    """Resolve shared and strict per-restaurant stores below one state root."""

    _SHARED_REQUIRED = ("raw", "normalized.json", "site/index.html", "site/data.json")
    _RESTAURANT_REQUIRED = ("raw", "normalized.json", "site/index.html", "site/data.json")

    def __init__(self, config: Config) -> None:
        self.config = config
        self.state_dir = config.state_dir
        self.restaurants_root = self.state_dir / "restaurants"
        self.cache_index_path = self.state_dir / "cache-index.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.restaurants_root.mkdir(parents=True, exist_ok=True)
        self.shared = SnapshotStore(
            self.state_dir / "shared",
            self._SHARED_REQUIRED,
            public_link=config.output_dir,
        )

    def restaurant(self, restaurant_id: str) -> SnapshotStore:
        safe_id = validate_restaurant_id(restaurant_id)
        return SnapshotStore(self.restaurants_root / safe_id, self._RESTAURANT_REQUIRED)

    @staticmethod
    def _normalized_document(store: SnapshotStore) -> dict[str, Any]:
        if not store.is_ready():
            raise StateError("aucun snapshot valide n'est disponible")
        try:
            document = json.loads((store.current_path() / "normalized.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError("snapshot normalisé illisible") from exc
        if not isinstance(document, dict):
            raise StateError("snapshot normalisé incohérent")
        return document

    def load_shared_data(self) -> SharedData:
        document = self._normalized_document(self.shared)
        try:
            restaurants = tuple(
                _restaurant(item) for item in _list(document["restaurants"], "restaurants")
            )
            rewards = tuple(_reward(item) for item in _list(document["rewards"], "récompenses"))
            refreshed_at = _datetime(document["refreshed_at"], required=True)
            source_urls = _source_urls(document["source_urls"])
        except (KeyError, TypeError) as exc:
            raise StateError("snapshot partagé incohérent") from exc
        assert refreshed_at is not None
        if not restaurants or len({item.id for item in restaurants}) != len(restaurants):
            raise StateError("annuaire de restaurants vide ou dupliqué")
        return SharedData(restaurants, rewards, refreshed_at, source_urls)

    def load_restaurant_data(self, restaurant_id: str) -> SnapshotData:
        document = self._normalized_document(self.restaurant(restaurant_id))
        try:
            refreshed_at = _datetime(document["refreshed_at"], required=True)
            report_document = document["report"]
            if not isinstance(report_document, dict):
                raise StateError("rapport de restaurant invalide")
            report = CalculationReport(
                rows=tuple(
                    _ranked_reward(item) for item in _list(report_document["rows"], "classement")
                ),
                diagnostics=tuple(
                    _match_result(item)
                    for item in _list(report_document["diagnostics"], "diagnostics")
                ),
                match_rate=_number(
                    report_document["match_rate"], "taux d'appariement du rapport", maximum=1.0
                ),
            )
            assert refreshed_at is not None
            data = SnapshotData(
                restaurant=_restaurant(document["restaurant"]),
                refreshed_at=refreshed_at,
                source_urls=_source_urls(document["source_urls"]),
                report=report,
                reward_count=_integer(
                    document["reward_count"], "nombre de récompenses", maximum=10_000
                ),
                matched_count=_integer(
                    document["matched_count"], "nombre d'appariements", maximum=10_000
                ),
                match_rate=_number(document["match_rate"], "taux d'appariement", maximum=1.0),
                shared_snapshot_id=_text(
                    document["shared_snapshot_id"], "identifiant du snapshot partagé"
                ),
            )
            _validate_snapshot_counts(data)
            return data
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError("snapshot de restaurant incohérent") from exc

    def cached_restaurant_ids(self) -> tuple[str, ...]:
        identifiers: list[str] = []
        for path in self.restaurants_root.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                safe_id = validate_restaurant_id(path.name)
            except ValueError:
                continue
            if self.restaurant(safe_id).is_ready():
                identifiers.append(safe_id)
        return tuple(sorted(identifiers))

    def remove_restaurant(self, restaurant_id: str) -> None:
        safe_id = validate_restaurant_id(restaurant_id)
        target = self.restaurants_root / safe_id
        if target.is_symlink():
            raise StateError("refus de supprimer un lien de cache")
        if target.exists():
            shutil.rmtree(target)

    def check(self) -> dict[str, object]:
        shared = self.load_shared_data()
        if len(shared.rewards) < MIN_REWARD_COUNT:
            raise StateError("nombre de récompenses publiques insuffisant")
        if len({reward.crowns for reward in shared.rewards}) < MIN_TIER_COUNT:
            raise StateError("nombre de paliers publics insuffisant")
        identifiers = self.cached_restaurant_ids()
        for restaurant_id in identifiers:
            data = self.load_restaurant_data(restaurant_id)
            if data.restaurant.id != restaurant_id:
                raise StateError("restaurant incohérent avec son répertoire de cache")
            if data.matched_count < MIN_MATCH_COUNT or data.match_rate < MIN_MATCH_RATE:
                raise StateError("taux d'appariement du snapshot insuffisant")
        return {
            "ready": True,
            "shared_snapshot": self.shared.load_status().snapshot_id,
            "restaurant_count": len(identifiers),
        }


def _restaurant(document: object) -> Restaurant:
    if not isinstance(document, dict):
        raise StateError("restaurant normalisé invalide")
    try:
        restaurant_id = validate_restaurant_id(_text(document["id"], "identifiant restaurant"))
        return Restaurant(
            id=restaurant_id,
            name=_text(document["name"], "nom du restaurant"),
            address=_text(document["address"], "adresse du restaurant"),
            city=_text(document.get("city", ""), "ville", allow_empty=True),
            postal_code=_text(document.get("postal_code", ""), "code postal", allow_empty=True),
        )
    except (KeyError, ValueError) as exc:
        raise StateError("restaurant normalisé incomplet") from exc


def _reward(document: object) -> Reward:
    if not isinstance(document, dict):
        raise StateError("récompense normalisée invalide")
    try:
        crowns = _integer(document["crowns"], "nombre de Couronnes", minimum=1, maximum=MAX_CROWNS)
        kind = _text(document["kind"], "type de récompense")
        if kind not in _KINDS:
            raise StateError("type de récompense inconnu")
        menu_size = _optional_text(document.get("menu_size"), "taille de menu")
        if (kind == "menu" and menu_size not in {"M", "L"}) or (
            kind != "menu" and menu_size is not None
        ):
            raise StateError("taille de récompense incohérente")
        return Reward(
            crowns=crowns,
            name=_text(document["name"], "nom de récompense"),
            kind=kind,
            menu_size=menu_size,
            seasonal=_boolean(document["seasonal"], "indicateur saisonnier"),
            available=_boolean(document["available"], "disponibilité de récompense"),
            source_url=_source_urls([document["source_url"]])[0],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("récompense normalisée incomplète") from exc


def _match_result(document: object) -> MatchResult:
    if not isinstance(document, dict):
        raise StateError("appariement normalisé invalide")
    try:
        status = _text(document["status"], "état d'appariement")
        if status not in _MATCH_STATUSES:
            raise StateError("état d'appariement inconnu")
        price = document.get("price_cents")
        if price is not None:
            price = _integer(price, "prix apparié", minimum=1, maximum=MAX_PRICE_CENTS)
        candidate_available = document.get("candidate_available")
        if candidate_available is not None:
            candidate_available = _boolean(candidate_available, "disponibilité du candidat")
        candidate_active = document.get("candidate_active")
        if candidate_active is not None:
            candidate_active = _boolean(candidate_active, "activité du candidat")
        return MatchResult(
            reward=_reward(document["reward"]),
            status=status,
            candidate_name=_optional_text(document.get("candidate_name"), "nom du candidat"),
            candidate_id=_optional_text(document.get("candidate_id"), "identifiant du candidat"),
            candidate_kind=_optional_text(document.get("candidate_kind"), "type du candidat"),
            price_cents=price,
            method=_optional_text(document.get("method"), "méthode d'appariement"),
            diagnostic=_optional_text(document.get("diagnostic"), "diagnostic"),
            suggestions=tuple(
                _text(item, "suggestion")
                for item in _list(document.get("suggestions", []), "suggestions")
            ),
            candidate_available=candidate_available,
            candidate_active=candidate_active,
        )
    except KeyError as exc:
        raise StateError("appariement normalisé incomplet") from exc


def _ranked_reward(document: object) -> RankedReward:
    if not isinstance(document, dict):
        raise StateError("classement normalisé invalide")
    try:
        reward = _reward(document["reward"])
        row = RankedReward(
            reward=reward,
            candidate_name=_text(document["candidate_name"], "nom classé"),
            candidate_kind=_text(document["candidate_kind"], "type classé"),
            price_cents=_integer(
                document["price_cents"], "prix public", minimum=1, maximum=MAX_PRICE_CENTS
            ),
            price_euros=_number(document["price_euros"], "prix en euros"),
            euros_per_crown=_number(document["euros_per_crown"], "euros par Couronne"),
            required_spend=_number(document["required_spend"], "dépense nécessaire"),
            yield_percent=_number(document["yield_percent"], "rendement"),
            catalog_available=_boolean(
                document.get("catalog_available", True), "disponibilité catalogue"
            ),
            catalog_active=_boolean(document.get("catalog_active", True), "activité catalogue"),
            match_method=_optional_text(document.get("match_method"), "méthode de classement"),
            dominated=_boolean(document.get("dominated", False), "indicateur de domination"),
            domination_note=_optional_text(document.get("domination_note"), "note de domination"),
        )
        expected = (
            ("prix en euros", row.price_euros, row.price_cents / 100),
            ("euros par Couronne", row.euros_per_crown, row.price_euros / reward.crowns),
            ("dépense nécessaire", row.required_spend, reward.crowns / 2),
            ("rendement", row.yield_percent, 200 * row.price_euros / reward.crowns),
        )
        for label, actual, calculated in expected:
            if not math.isclose(actual, calculated, rel_tol=1e-9, abs_tol=1e-9):
                raise StateError(f"formule incohérente pour {label}")
        return row
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("classement normalisé incomplet") from exc


def _validate_snapshot_counts(data: SnapshotData) -> None:
    row_count = len(data.report.rows)
    diagnostic_count = len(data.report.diagnostics)
    if (
        data.matched_count != row_count
        or data.reward_count != row_count + diagnostic_count
        or data.matched_count > data.reward_count
    ):
        raise StateError("comptage des récompenses incohérent")
    expected_rate = data.matched_count / data.reward_count if data.reward_count else 0.0
    if not math.isclose(data.match_rate, expected_rate, rel_tol=1e-9, abs_tol=1e-9):
        raise StateError("taux d'appariement incohérent avec le comptage")
    if not math.isclose(data.report.match_rate, data.match_rate, rel_tol=1e-9, abs_tol=1e-9):
        raise StateError("taux d'appariement du rapport incohérent")
