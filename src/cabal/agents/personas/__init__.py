"""Built-in personas.

The registry maps persona names declared in identity.toml to LLMPersona
subclass constructors. Personas not in the registry register their
bulletin-board row from identity_sync but have no LocalHandler — dispatch
to them returns a clear "no handler" error rather than spinning on HTTP.
"""

from __future__ import annotations

from collections.abc import Callable

from cabal.agents.llm_persona import LLMPersona
from cabal.agents.personas.architect import ArchitectPersona
from cabal.agents.personas.archivist import ArchivistPersona
from cabal.agents.personas.librarian import LibrarianPersona
from cabal.agents.personas.mason import MasonPrimusPersona
from cabal.agents.personas.wright import WrightPrimusPersona

# name → factory that takes (llm_client, ...) and returns an LLMPersona
PERSONA_REGISTRY: dict[str, Callable[..., LLMPersona]] = {
    "architect": ArchitectPersona,
    "archivist": ArchivistPersona,
    "librarian": LibrarianPersona,
    "mason": MasonPrimusPersona,
    "wright": WrightPrimusPersona,
}


__all__ = [
    "ArchitectPersona",
    "ArchivistPersona",
    "WrightPrimusPersona",
    "LibrarianPersona",
    "PERSONA_REGISTRY",
    "MasonPrimusPersona",
]
