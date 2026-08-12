"""Everything that talks to something outside this process.

Each provider is independently constructible from config and exposes a
``ping()`` for the Settings page's Test button. Only ``plex`` and ``llm`` are
required; the rest return ``configured == False`` and are skipped.
"""

from .llm import LLM, LLMError
from .plex import Collection, Item, Play, Plex, Section
from .plextv import PlexAuth, PlexTv, SharedUser
from .tautulli import Tautulli

__all__ = ["LLM", "LLMError", "Plex", "PlexAuth", "PlexTv", "Tautulli", "Item", "Play",
           "Section", "Collection", "SharedUser"]
