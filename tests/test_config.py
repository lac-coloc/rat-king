from pathlib import Path

import pytest

from bk_crowns.config import Config, ConfigError


def test_config_uses_multi_restaurant_defaults() -> None:
    config = Config.from_env({})

    assert not hasattr(config, "restaurant_id")
    assert config.refresh_interval_seconds == 21_600
    assert config.output_dir == Path("/data/site")
    assert config.state_dir == Path("/data")
    assert config.http_timeout_seconds == 30.0
    assert config.port == 8080
    assert config.cache_max_restaurants == 20
    assert config.refresh_queue_max == 10
    assert config.cold_loads_per_hour == 6
    assert config.search_max_results == 20


def test_config_reads_documented_environment_variables(tmp_path: Path) -> None:
    config = Config.from_env(
        {
            "BK_REFRESH_INTERVAL_SECONDS": "3600",
            "BK_OUTPUT_DIR": str(tmp_path / "site"),
            "BK_STATE_DIR": str(tmp_path),
            "BK_HTTP_TIMEOUT_SECONDS": "12.5",
            "BK_CACHE_MAX_RESTAURANTS": "30",
            "BK_REFRESH_QUEUE_MAX": "12",
            "BK_COLD_LOADS_PER_HOUR": "8",
            "BK_SEARCH_MAX_RESULTS": "15",
            "PORT": "9090",
        }
    )

    assert config.refresh_interval_seconds == 3600
    assert config.output_dir == tmp_path / "site"
    assert config.state_dir == tmp_path
    assert config.http_timeout_seconds == 12.5
    assert config.cache_max_restaurants == 30
    assert config.refresh_queue_max == 12
    assert config.cold_loads_per_hour == 8
    assert config.search_max_results == 15
    assert config.port == 9090


@pytest.mark.parametrize(
    "restaurant_id",
    ["", "2001", "k2001", "K200", "K20010", "K20/1", "https://example.test"],
)
def test_validate_restaurant_id_rejects_unsafe_values(restaurant_id: str) -> None:
    from bk_crowns.config import validate_restaurant_id

    with pytest.raises(ConfigError, match="identifiant restaurant"):
        validate_restaurant_id(restaurant_id)


def test_validate_restaurant_id_accepts_strict_public_id() -> None:
    from bk_crowns.config import validate_restaurant_id

    assert validate_restaurant_id("K2001") == "K2001"


def test_config_rejects_legacy_global_restaurant_setting() -> None:
    with pytest.raises(ConfigError, match="BK_RESTAURANT_ID"):
        Config.from_env({"BK_RESTAURANT_ID": "K2001"})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BK_REFRESH_INTERVAL_SECONDS", "0"),
        ("BK_REFRESH_INTERVAL_SECONDS", "899"),
        ("BK_REFRESH_INTERVAL_SECONDS", "86401"),
        ("BK_REFRESH_INTERVAL_SECONDS", "fast"),
        ("BK_HTTP_TIMEOUT_SECONDS", "0"),
        ("BK_HTTP_TIMEOUT_SECONDS", "121"),
        ("BK_CACHE_MAX_RESTAURANTS", "0"),
        ("BK_CACHE_MAX_RESTAURANTS", "101"),
        ("BK_REFRESH_QUEUE_MAX", "0"),
        ("BK_REFRESH_QUEUE_MAX", "51"),
        ("BK_COLD_LOADS_PER_HOUR", "0"),
        ("BK_COLD_LOADS_PER_HOUR", "25"),
        ("BK_SEARCH_MAX_RESULTS", "0"),
        ("BK_SEARCH_MAX_RESULTS", "51"),
        ("PORT", "0"),
        ("PORT", "65536"),
    ],
)
def test_config_rejects_invalid_numeric_values(key: str, value: str) -> None:
    with pytest.raises(ConfigError, match=key):
        Config.from_env({key: value})


def test_config_rejects_output_outside_state_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="BK_OUTPUT_DIR"):
        Config.from_env(
            {
                "BK_STATE_DIR": str(tmp_path / "state"),
                "BK_OUTPUT_DIR": str(tmp_path / "elsewhere"),
            }
        )
