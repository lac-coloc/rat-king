import json
from datetime import UTC, datetime
from pathlib import Path

from bk_crowns import __version__
from bk_crowns.cli import build_parser, main, resolve_serve_config
from bk_crowns.config import Config
from bk_crowns.generator import render_home
from bk_crowns.models import Restaurant, Reward, SharedData, Status
from bk_crowns.state import StateRepository


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "BK_STATE_DIR": str(tmp_path),
        "BK_OUTPUT_DIR": str(tmp_path / "site"),
        "BK_REFRESH_INTERVAL_SECONDS": "21600",
        "BK_HTTP_TIMEOUT_SECONDS": "30",
        "BK_CACHE_MAX_RESTAURANTS": "20",
        "BK_REFRESH_QUEUE_MAX": "10",
        "BK_COLD_LOADS_PER_HOUR": "6",
        "BK_SEARCH_MAX_RESULTS": "20",
        "PORT": "8080",
    }


def _publish_shared(tmp_path: Path) -> None:
    config = Config(state_dir=tmp_path, output_dir=tmp_path / "site")
    repository = StateRepository(config)
    data = SharedData(
        restaurants=(
            Restaurant(
                "K2001",
                "VILLE EXEMPLE CENTRE",
                "10 RUE DU TEST - 12345 VILLE EXEMPLE",
                "VILLE EXEMPLE",
                "12345",
            ),
        ),
        rewards=tuple(
            Reward(
                crowns=(40, 80, 120)[index % 3],
                name=f"Récompense exemple {index}",
                kind="produit",
                menu_size=None,
                seasonal=False,
                available=True,
                source_url="https://example.test/rewards",
            )
            for index in range(10)
        ),
        refreshed_at=datetime.now(UTC),
        source_urls=("https://example.test/public",),
    )
    staging = repository.shared.create_staging()
    (staging / "raw").mkdir()
    (staging / "raw" / "restaurants.json").write_text("{}")
    (staging / "normalized.json").write_text(json.dumps(data.to_dict()))
    (staging / "site").mkdir()
    (staging / "site" / "index.html").write_bytes(
        render_home(data, {"ready": True, "state": "fresh"})
    )
    (staging / "site" / "data.json").write_text("{}")
    now = datetime.now(UTC)
    repository.shared.publish(staging, Status(now, now, None, "shared-1"))


def _set_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BK_RESTAURANT_ID", raising=False)
    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)


def test_package_reports_multi_restaurant_release() -> None:
    assert __version__ == "0.2.2"


def test_refresh_parser_accepts_optional_strict_restaurant() -> None:
    args = build_parser().parse_args(["refresh", "--restaurant", "K2001"])

    assert args.restaurant == "K2001"


def test_check_is_offline_and_fails_without_shared_snapshot(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_environment(tmp_path, monkeypatch)

    assert main(["check"]) == 1
    assert "snapshot" in capsys.readouterr().err


def test_check_validates_shared_state_without_network(tmp_path: Path, monkeypatch, capsys) -> None:
    _set_environment(tmp_path, monkeypatch)
    _publish_shared(tmp_path)

    assert main(["check"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "ready": True,
        "restaurant_count": 0,
        "shared_snapshot": "shared-1",
    }


def test_refresh_rejects_unsafe_id_before_any_network(tmp_path: Path, monkeypatch, capsys) -> None:
    _set_environment(tmp_path, monkeypatch)

    assert main(["refresh", "--restaurant", "https://evil.example"]) == 2
    assert "identifiant restaurant" in capsys.readouterr().err


def test_serve_cli_arguments_override_environment_config() -> None:
    parser = build_parser()
    assert parser.prog == "rat-king"
    args = parser.parse_args(
        ["serve", "--host", "0.0.0.0", "--port", "9090", "--refresh-interval", "3600"]
    )

    config, host = resolve_serve_config(Config(), args)

    assert host == "0.0.0.0"
    assert config.port == 9090
    assert config.refresh_interval_seconds == 3600


def test_cli_rejects_invalid_serve_port(capsys, monkeypatch) -> None:
    monkeypatch.delenv("BK_RESTAURANT_ID", raising=False)

    assert main(["serve", "--port", "70000"]) == 2
    assert "PORT" in capsys.readouterr().err
