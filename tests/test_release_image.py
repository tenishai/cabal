"""The release image: release.yml publishes it from a v* tag, and k8s/ names it by that tag.

An outside user applies k8s/ as written, so the image line must name a tag a release
publishes. Pinning it to pyproject.toml's version makes the release PR's version bump
fail here until the manifest moves with it.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
TEST = ROOT / ".github" / "workflows" / "test.yml"


def _on(doc: dict):
    return doc.get("on", doc.get(True))


def _version() -> str:
    m = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    assert m, "pyproject.toml has no version line"
    return m.group(1)


def test_k8s_image_is_the_release_tag_of_this_version():
    manifest = (ROOT / "k8s" / "cabal-core.yaml").read_text()
    images = re.findall(r"image:\s*(ghcr\.io/tenishai/cabal\S*)", manifest)
    assert images == [f"ghcr.io/tenishai/cabal:v{_version()}"], images
    assert ":latest" not in (ROOT / "k8s" / "README.md").read_text()


def test_release_runs_on_version_tags_after_the_whole_ci():
    doc = yaml.safe_load(RELEASE.read_text())
    assert _on(doc)["push"] == {"tags": ["v*"]}
    assert doc["jobs"]["checks"]["uses"] == "./.github/workflows/test.yml"
    assert doc["jobs"]["release"]["needs"] == "checks"
    assert "workflow_call" in _on(yaml.safe_load(TEST.read_text())), (
        "release.yml calls test.yml, which must accept workflow_call"
    )


def test_release_builds_with_the_techne_the_dockerfile_pins():
    text = RELEASE.read_text()
    assert "s/^ARG TECHNE_VERSION=//p' Dockerfile" in text
    steps = yaml.safe_load(text)["jobs"]["release"]["steps"]
    builds = [s for s in steps if str(s.get("uses", "")).startswith("docker/build-push-action@")]
    assert len(builds) == 2
    for s in builds:
        assert s["with"]["build-contexts"] == "techne=techne"
    published = builds[1]["with"]
    assert published["push"] is True
    assert "latest" not in published["tags"]


def _steps() -> dict:
    steps = yaml.safe_load(RELEASE.read_text())["jobs"]["release"]["steps"]
    return {s.get("name", s.get("uses")): s for s in steps}


def test_a_branch_run_is_a_dry_run_that_publishes_nothing():
    steps = _steps()
    version = steps["The tag names the version the project declares (a branch is a dry run)"]
    assert '"$GITHUB_REF_TYPE" = tag' in version["run"]
    assert 'echo "publish=false"' in version["run"]
    for name in ("Build and publish, both architectures", "The GitHub release"):
        assert "steps.version.outputs.publish == 'true'" in steps[name]["if"], name


def test_a_rerun_does_not_push_a_published_tag_again():
    steps = _steps()
    assert "steps.published.outputs.exists != 'true'" in steps["Build and publish, both architectures"]["if"]
    assert "imagetools inspect" in steps["Is this tag's image already published"]["run"]
    assert "steps.published.outputs.digest" in steps["The GitHub release"]["env"]["DIGEST"]
