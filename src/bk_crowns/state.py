"""Atomic shared and per-restaurant snapshot persistence."""

from __future__ import annotations

import json
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
            restaurants = tuple(_restaurant(item) for item in document["restaurants"])
            rewards = tuple(_reward(item) for item in document["rewards"])
            refreshed_at = _datetime(document["refreshed_at"], required=True)
            source_urls = tuple(str(item) for item in document["source_urls"])
        except (KeyError, TypeError) as exc:
            raise StateError("snapshot partagé incohérent") from exc
        assert refreshed_at is not None
        return SharedData(restaurants, rewards, refreshed_at, source_urls)

    def load_restaurant_data(self, restaurant_id: str) -> SnapshotData:
        document = self._normalized_document(self.restaurant(restaurant_id))
        try:
            refreshed_at = _datetime(document["refreshed_at"], required=True)
            report_document = document["report"]
            report = CalculationReport(
                rows=tuple(_ranked_reward(item) for item in report_document["rows"]),
                diagnostics=tuple(_match_result(item) for item in report_document["diagnostics"]),
                match_rate=float(report_document["match_rate"]),
            )
            assert refreshed_at is not None
            return SnapshotData(
                restaurant=_restaurant(document["restaurant"]),
                refreshed_at=refreshed_at,
                source_urls=tuple(str(item) for item in document["source_urls"]),
                report=report,
                reward_count=int(document["reward_count"]),
                matched_count=int(document["matched_count"]),
                match_rate=float(document["match_rate"]),
                shared_snapshot_id=str(document["shared_snapshot_id"]),
            )
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
        self.load_shared_data()
        identifiers = self.cached_restaurant_ids()
        for restaurant_id in identifiers:
            self.load_restaurant_data(restaurant_id)
        return {
            "ready": True,
            "shared_snapshot": self.shared.load_status().snapshot_id,
            "restaurant_count": len(identifiers),
        }


def _restaurant(document: object) -> Restaurant:
    if not isinstance(document, dict):
        raise StateError("restaurant normalisé invalide")
    try:
        return Restaurant(
            id=str(document["id"]),
            name=str(document["name"]),
            address=str(document["address"]),
            city=str(document.get("city", "")),
            postal_code=str(document.get("postal_code", "")),
        )
    except KeyError as exc:
        raise StateError("restaurant normalisé incomplet") from exc


def _reward(document: object) -> Reward:
    if not isinstance(document, dict):
        raise StateError("récompense normalisée invalide")
    try:
        return Reward(
            crowns=int(document["crowns"]),
            name=str(document["name"]),
            kind=str(document["kind"]),
            menu_size=(None if document.get("menu_size") is None else str(document["menu_size"])),
            seasonal=bool(document["seasonal"]),
            available=bool(document["available"]),
            source_url=str(document["source_url"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("récompense normalisée incomplète") from exc


def _match_result(document: object) -> MatchResult:
    if not isinstance(document, dict):
        raise StateError("appariement normalisé invalide")
    try:
        return MatchResult(
            reward=_reward(document["reward"]),
            status=str(document["status"]),
            candidate_name=document.get("candidate_name"),
            candidate_id=document.get("candidate_id"),
            candidate_kind=document.get("candidate_kind"),
            price_cents=document.get("price_cents"),
            method=document.get("method"),
            diagnostic=document.get("diagnostic"),
            suggestions=tuple(str(item) for item in document.get("suggestions", ())),
            candidate_available=document.get("candidate_available"),
            candidate_active=document.get("candidate_active"),
        )
    except KeyError as exc:
        raise StateError("appariement normalisé incomplet") from exc


def _ranked_reward(document: object) -> RankedReward:
    if not isinstance(document, dict):
        raise StateError("classement normalisé invalide")
    try:
        return RankedReward(
            reward=_reward(document["reward"]),
            candidate_name=str(document["candidate_name"]),
            candidate_kind=str(document["candidate_kind"]),
            price_cents=int(document["price_cents"]),
            price_euros=float(document["price_euros"]),
            euros_per_crown=float(document["euros_per_crown"]),
            required_spend=float(document["required_spend"]),
            yield_percent=float(document["yield_percent"]),
            catalog_available=bool(document.get("catalog_available", True)),
            catalog_active=bool(document.get("catalog_active", True)),
            match_method=document.get("match_method"),
            dominated=bool(document.get("dominated", False)),
            domination_note=document.get("domination_note"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("classement normalisé incomplet") from exc
