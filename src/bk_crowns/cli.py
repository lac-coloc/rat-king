"""Single `rat-king` command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from .config import Config, ConfigError, validate_restaurant_id
from .fetch import FetchError
from .refresh import RefreshError, RestaurantRefreshService, SharedRefreshService
from .restaurants import RestaurantDirectory
from .server import run_http_service
from .state import StateError, StateRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rat-king",
        description="Comparateur public des récompenses Kingdom Burger King France",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    refresh = subparsers.add_parser(
        "refresh", help="Rafraîchir les sources publiques et les snapshots locaux"
    )
    refresh.add_argument(
        "--restaurant",
        metavar="KNNNN",
        help="Rafraîchir explicitement ce restaurant public après validation de l'annuaire",
    )
    subparsers.add_parser("check", help="Vérifier hors ligne tout l'état local")
    serve = subparsers.add_parser("serve", help="Servir le site et planifier les refreshes")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int)
    serve.add_argument("--refresh-interval", type=int)
    return parser


def resolve_serve_config(config: Config, args: argparse.Namespace) -> tuple[Config, str]:
    resolved = replace(
        config,
        port=config.port if args.port is None else args.port,
        refresh_interval_seconds=(
            config.refresh_interval_seconds
            if args.refresh_interval is None
            else args.refresh_interval
        ),
    )
    resolved.validate()
    return resolved, args.host


def _check(config: Config) -> dict[str, object]:
    config.validate()
    repository = StateRepository(config)
    return repository.check()


def _needs_refresh(
    repository: StateRepository,
    restaurant_id: str,
    shared_snapshot_id: str,
    refresh_interval_seconds: int,
) -> bool:
    status = repository.restaurant(restaurant_id).load_status()
    if status.last_success is None or status.shared_snapshot_id != shared_snapshot_id:
        return True
    age = (datetime.now(UTC) - status.last_success).total_seconds()
    return age > refresh_interval_seconds


def _refresh(config: Config, restaurant_id: str | None) -> dict[str, object]:
    selected = validate_restaurant_id(restaurant_id) if restaurant_id is not None else None
    repository = StateRepository(config)
    cached_before = repository.cached_restaurant_ids()
    shared = SharedRefreshService(config, repository).run()
    directory = RestaurantDirectory(shared.data.restaurants)
    refreshed: list[dict[str, object]] = []

    if selected is not None:
        if directory.get(selected) is None:
            raise RefreshError(f"restaurant {selected} absent de la liste publique")
        targets = (selected,)
    else:
        targets = tuple(
            item
            for item in cached_before
            if _needs_refresh(
                repository,
                item,
                str(shared.status.snapshot_id),
                config.refresh_interval_seconds,
            )
        )

    service = RestaurantRefreshService(config, repository)
    for target in targets:
        outcome = service.run(target)
        refreshed.append(
            {
                "restaurant_id": outcome.data.restaurant.id,
                "snapshot": outcome.status.snapshot_id,
                "reward_count": outcome.data.reward_count,
                "matched_count": outcome.data.matched_count,
                "match_rate": outcome.data.match_rate,
            }
        )
    return {
        "shared_snapshot": shared.status.snapshot_id,
        "restaurant_count": len(directory.restaurants),
        "refreshed_restaurants": refreshed,
        "site": str(config.output_dir),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        if args.command == "check":
            print(json.dumps(_check(config), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "refresh":
            print(
                json.dumps(
                    _refresh(config, args.restaurant),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        config, host = resolve_serve_config(config, args)
        run_http_service(config, host)
        return 0
    except ConfigError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2
    except (StateError, RefreshError, FetchError, OSError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
