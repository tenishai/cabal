"""Sync identity.toml declared personas into the bulletin board.

Runs on daemon startup AFTER the signing key is bootstrapped. For each
persona declared in identity.toml:
    - Idempotently insert into `personas` table.
    - If no active agent row exists for "<persona>-primus" (for role=primus
      personas) or "<persona>" (for singletons), create one with a freshly
      signed AgentCard.

For Phase 1 the AgentCards are minimal stubs. Phase 2 will hand persona
modules a hook to refine them.
"""

from __future__ import annotations

import structlog

from cabal.config.identity import IdentityConfig, PersonaConfig
from cabal.core.a2a.card import sign_card
from cabal.core.a2a.types import AgentCapabilities, AgentCard, Skill
from cabal.core.auth.keys import StoredKey
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Persona

log = structlog.get_logger(__name__)


def agent_name_for(persona_name: str, persona: PersonaConfig) -> str:
    """The bulletin agent name a persona is reached under.

    PUBLIC because cabal#154's edge map must resolve a declared persona to the
    agent name that actually arrives in `cabal_target_agent`, and a second copy
    of this one-line rule in app.py would be a second definition free to drift
    from this one.
    """
    return f"{persona_name}-primus" if persona.role == "primus" else persona_name



def _build_agent_card(
    agent_name: str, persona_name: str, persona: PersonaConfig, generation: int
) -> AgentCard:
    return AgentCard(
        name=agent_name,
        description=persona.description,
        url=f"local://{agent_name}",
        version="0.1.0",
        skills=[Skill(id=agent_name, name=agent_name)],
        capabilities=AgentCapabilities(streaming=False),
        metadata={
            "cabal_persona": persona_name,
            "cabal_generation": generation,
            "cabal_safety_tier": persona.safety_tier,
            "cabal_role": persona.role,
        },
    )


async def sync_personas(
    sessionmaker, identity: IdentityConfig, signing_key: StoredKey
) -> None:
    """Idempotently mirror identity.toml personas into the DB."""
    for persona_name, persona_cfg in identity.personas.items():
        async with open_session(sessionmaker) as session:
            existing = await session.get(Persona, persona_name)
            if existing is None:
                session.add(
                    Persona(
                        name=persona_name,
                        config_json=persona_cfg.model_dump(),
                    )
                )
                await session.flush()

            agent_name = agent_name_for(persona_name, persona_cfg)
            current = await bulletin_store.get_active(session, agent_name)
            if current is not None:
                continue

            card = _build_agent_card(agent_name, persona_name, persona_cfg, 1)
            signed = sign_card(card, signing_key)
            await bulletin_store.register_agent(
                session,
                name=agent_name,
                persona_name=persona_name,
                role=persona_cfg.role,
                endpoint=f"local://{agent_name}",
                capabilities={"skills": [s.model_dump() for s in card.skills]},
                safety_tier=persona_cfg.safety_tier,
                agent_card_json={"jws": signed, "card": card.model_dump()},
                agent_card_kid=signing_key.kid,
            )
            log.info(
                "identity_sync.registered_agent",
                persona=persona_name,
                agent=agent_name,
                role=persona_cfg.role,
            )
