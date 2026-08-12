import re
import tomllib
from pathlib import Path

PUBLISHABLE_CONFIGURATION = (
    Path("README.md"),
    Path("Dockerfile"),
    Path("deploy/k8s/deployment.yaml"),
    Path("deploy/k8s/service.yaml"),
)


def test_deployment_artifacts_have_no_global_restaurant_setting() -> None:
    for path in PUBLISHABLE_CONFIGURATION:
        content = path.read_text()
        assert "BK_RESTAURANT_ID" not in content
        assert not re.search(
            r"(?:value|default|défaut)\s*[:=]?\s*K[0-9]{4}", content, re.IGNORECASE
        )


def test_live_smoke_does_not_pin_a_real_restaurant() -> None:
    content = Path("tests/test_live.py").read_text()

    assert not re.search(r"K[0-9]{4}", content)


def test_container_publish_is_limited_to_version_tags() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text()

    assert "packages: write" in workflow
    assert "startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "--password-stdin" in workflow


def test_project_does_not_claim_a_license() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]

    assert "license" not in project
    assert not any(Path(".").glob("LICENSE*"))
