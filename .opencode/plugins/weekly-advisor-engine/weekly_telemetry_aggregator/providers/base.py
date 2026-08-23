"""Contrat multi-harnais : Protocol `SessionProvider` + vue session exposée.

Les méthodes du protocol reflètent EXACTEMENT les besoins des call sites
actuels (`main.py`, `costing.py`, `transcript.py`, `cli.py`) vis-à-vis de
`SchemaAdapter` — mêmes noms, mêmes signatures — plus `close()` car le
provider possède le cycle de vie de sa source (les call sites ferment
`adapter.conn` aujourd'hui). Chaque session exposée porte un identifiant
canonique namespacé ``"<harness>:<session_id>"`` et le tag ``harness``.

Cellule cœur : aucun câblage end-to-end ici (cellule suivante).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..models import StepFinish, canonical_session_id
from ..sqlite_reader import PartRecord, SessionMeta

#: Identifiant de harnais du provider OpenCode intégré.
HARNESS_OPENCODE = "opencode"


@dataclass(slots=True)
class HarnessSession:
    """Une session telle qu'exposée par un provider (vue plate, tag harnais).

    Sur-ensemble lisible de `SessionMeta` : tous les champs de la meta y sont
    recopiés à l'identique, avec `session_id` canonique namespacé en plus du
    tag `harness`. Le code consommateur existant (`m.session_id`, `m.title`,
    `m.parent_id`, …) fonctionne sans changement.
    """

    harness: str
    #: canonique : "<harness>:<session_id>"
    session_id: str
    title: str | None = None
    parent_id: str | None = None
    model_key: str = "unknown/unknown"
    agent: str | None = None
    directory: str | None = None
    cost: float | None = None
    tokens_input: float = 0.0
    tokens_output: float = 0.0
    tokens_reasoning: float = 0.0
    tokens_cache_read: float = 0.0
    tokens_cache_write: float = 0.0
    time_created: datetime | None = None
    time_updated: datetime | None = None

    @classmethod
    def from_meta(cls, harness: str, meta: SessionMeta) -> HarnessSession:
        """Enveloppe une meta brute : id namespacé + tag harnais, reste inchangé."""
        data = {f.name: getattr(meta, f.name) for f in dataclasses.fields(meta)}
        raw_id = str(data.pop("session_id"))
        return cls(harness=harness, session_id=canonical_session_id(harness, raw_id), **data)


@runtime_checkable
class SessionProvider(Protocol):
    """Vue lecture-seule uniforme sur les sessions d'un harnais.

    Toutes les méthodes prennent/retournent des identifiants canoniques
    ``"<harness>:<session_id>"`` ; un id brut est toléré en entrée.
    """

    #: Nom court du harnais ("opencode", …) — préfixe des ids canoniques.
    harness: str

    def check_schema(self) -> None:
        """Lève SchemaError si la source est inutilisable (diagnostic doctor)."""
        ...

    def list_sessions(self, since_ms: int) -> list[HarnessSession]:
        """Sessions actives depuis `since_ms` (epoch ms), ids canoniques."""
        ...

    def has_telemetry_rows(self, session_id: str) -> bool:
        """Vrai si la session porte des lignes de télémétrie exploitables."""
        ...

    def session_steps(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[StepFinish]:
        """Step-finish agrégés dans [start_ms, end_ms] (fenêtre, jamais vie totale)."""
        ...

    def session_tools(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        """(tool_calls, tool_arg_chars, skills_loaded) dans la fenêtre."""
        ...

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        """Textes des tours utilisateur dans la fenêtre."""
        ...

    def session_context_chars(self, session_id: str, start_ms: int, end_ms: int) -> dict[str, int]:
        """Caractères de contexte par catégorie dans la fenêtre."""
        ...

    def session_aggregates(self, session_id: str) -> dict | None:
        """Agrégats vie-telle-enregistrée (cross-check coût), None si absents."""
        ...

    def session_parts(self, session_id: str) -> list[PartRecord]:
        """Transcript normalisé complet (show-session)."""
        ...

    def find_session_by_title(self, title: str) -> HarnessSession | None:
        """Recherche par titre exact (lanceur de run), None sinon."""
        ...

    def close(self) -> None:
        """Libère la source sous-jacente (idempotent)."""
        ...
