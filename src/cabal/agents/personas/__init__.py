"""Built-in personas.

The registry maps persona names declared in identity.toml to LLMPersona
subclass constructors. Personas not in the registry register their
bulletin-board row from identity_sync but have no LocalHandler — dispatch
to them returns a clear "no handler" error rather than spinning on HTTP.
"""

from __future__ import annotations

from collections.abc import Callable

from cabal.agents.llm_persona import LLMPersona
from cabal.agents.personas.archivist import ArchivistPersona
from cabal.agents.personas.architect import ArchitectPersona
from cabal.agents.personas.librarian import LibrarianPersona


# name → factory that takes (llm_client, persona_cfg) and returns an LLMPersona
PERSONA_REGISTRY: dict[str, Callable[..., LLMPersona]] = {
    "archivist": ArchivistPersona,
    "architect": ArchitectPersona,
    "librarian": LibrarianPersona,
}


__all__ = [
    "ArchitectPersona",
    "ArchivistPersona",
    "LibrarianPersona",
    "PERSONA_REGISTRY",
]
