"""Every model a shipped config asks for must be served by its paired proxy.

Prudence's suggestion on PR #113, and the only test here earned by a pattern
rather than a single bug. Across three review rounds of that PR the failure was
never the rename — it was **config that must agree with other config, verified
against neither**:

  round 1 — checked the regex pattern, not the file list
  round 2 — checked the table changed and the tables deliberately unchanged,
            never the table in between
  round 3 — checked that every persona resolves against PERSONA_REGISTRY,
            never that its model is routable

Each check was correct and adjacent. The live instance: `wright` asks for
`anthropic/claude-sonnet-5` (its own DEFAULT_MODEL and its implementer's), and
neither proxy list served it. cabal-core runs strictly in proxy mode — no
ANTHROPIC_API_KEY in its k8s env — so an unserved model is not a fallback, it
is LLMInvalidRequestError. One shared LiteLLMClient owns one CircuitBreaker
(failure_threshold=3 / 30s), so three such dispatches open it for EVERY
persona; a single primus plan with three concurrent subordinate legs reaches
that alone.

The two pairs ship together and must agree:
    k8s/configmap.yaml   identity.toml  <-> the same file's litellm config.yaml
    identity.toml.example               <-> compose/litellm-config.yaml
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]


def _served(litellm_cfg: dict) -> set[str]:
    return {m["model_name"] for m in litellm_cfg.get("model_list", [])}


def _requested(identity: dict) -> set[str]:
    """Every model an identity.toml can dispatch to.

    Includes the DEFAULT_MODEL fallback: a primus that declares no
    `model_override` still dispatches, using its persona module's default —
    which is exactly how `wright` slipped through, since its override was
    absent rather than wrong.
    """
    from cabal.agents.personas.mason import DEFAULT_MODEL as MASON_DEFAULT
    from cabal.agents.personas.wright import DEFAULT_MODEL as WRIGHT_DEFAULT

    defaults = {"mason": MASON_DEFAULT, "wright": WRIGHT_DEFAULT}
    wanted: set[str] = set()
    for name, cfg in (identity.get("personas") or {}).items():
        override = cfg.get("model_override")
        if override:
            wanted.add(override)
        elif name in defaults:
            wanted.add(defaults[name])
        for sub in (cfg.get("subordinates") or {}).values():
            if sub.get("model"):
                wanted.add(sub["model"])
    return wanted


def _k8s_docs() -> list[dict]:
    return [d for d in yaml.safe_load_all((REPO / "k8s" / "configmap.yaml").read_text()) if d]


def _k8s_pair() -> tuple[dict, dict]:
    identity = litellm = None
    for doc in _k8s_docs():
        data = doc.get("data", {})
        if "identity.toml" in data:
            identity = tomllib.loads(data["identity.toml"])
        if "config.yaml" in data:
            litellm = yaml.safe_load(data["config.yaml"])
    assert identity is not None, "k8s/configmap.yaml has no identity.toml document"
    assert litellm is not None, "k8s/configmap.yaml has no litellm config.yaml document"
    return identity, litellm


def test_k8s_configmap_models_are_all_served() -> None:
    identity, litellm = _k8s_pair()
    missing = _requested(identity) - _served(litellm)
    assert not missing, (
        f"k8s/configmap.yaml asks for {sorted(missing)} but its litellm model_list "
        f"serves only {sorted(_served(litellm))}. cabal-core has no direct provider "
        "key, so this is LLMInvalidRequestError, and the shared CircuitBreaker makes "
        "it every persona's outage."
    )


def test_compose_example_models_are_all_served() -> None:
    identity = tomllib.loads((REPO / "identity.toml.example").read_text())
    litellm = yaml.safe_load((REPO / "compose" / "litellm-config.yaml").read_text())
    missing = _requested(identity) - _served(litellm)
    assert not missing, (
        f"identity.toml.example asks for {sorted(missing)} but "
        f"compose/litellm-config.yaml serves only {sorted(_served(litellm))}."
    )


@pytest.mark.parametrize("persona", ["mason", "wright"])
def test_every_k8s_primus_resolves_in_the_registry(persona: str) -> None:
    """The check that DID pass in round 3 — kept, because it is still necessary,
    just not sufficient. Routability is the sibling assertion above."""
    from cabal.agents.personas import PERSONA_REGISTRY

    identity, _ = _k8s_pair()
    assert persona in identity.get("personas", {}), f"{persona} absent from k8s identity.toml"
    assert persona in PERSONA_REGISTRY
