"""The dev image must carry liminal when the dev identity points at it.

compose/identity.dev.toml sets `[liminal] url`, and startup fails fast with
"the liminal package is not installed" (cabal#126) when the image lacks it, so
the container never goes healthy. The image gets liminal through a named
build context, the way techne arrives. This pins the three places that must
agree: the Dockerfile stage, and the cabal service of each compose file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _liminal_url(identity: Path) -> str | None:
    return tomllib.loads(identity.read_text()).get("liminal", {}).get("url")


def test_dockerfile_installs_liminal_from_a_named_context():
    text = (ROOT / "Dockerfile").read_text()
    assert "FROM scratch AS liminal" in text
    assert "COPY --from=liminal / /build/liminal" in text
    assert "pip install --no-cache-dir /build/liminal" in text


def test_compose_files_pass_a_liminal_context_when_their_identity_sets_a_url():
    cases = {
        "docker-compose.dev.yml": ("cabal", ROOT / "compose" / "identity.dev.toml"),
        "docker-compose.yml": ("cabal-core", ROOT / "identity.toml.example"),
    }
    for compose_name, (service, identity) in cases.items():
        assert _liminal_url(identity), f"{identity.name} no longer sets [liminal] url; drop this case"
        compose = yaml.safe_load((ROOT / compose_name).read_text())
        contexts = compose["services"][service]["build"]["additional_contexts"]
        assert contexts.get("liminal") == "../liminal", compose_name
