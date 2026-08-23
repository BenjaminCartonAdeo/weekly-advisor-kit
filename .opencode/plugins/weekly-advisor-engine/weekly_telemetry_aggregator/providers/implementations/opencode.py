"""Provider OpenCode — délégation 1:1 à `detect_db()` / `OpenCodeAdapter`.

Zéro changement de comportement : la détection de base, l'adaptateur V1/V2 et
la lecture read-only existants sont réutilisés tels quels. Le provider se
contente de namespacer les identifiants (`"opencode:<session_id>"`), de poser
le tag harnais sur les objets exposés, et d'accepter en entrée aussi bien un
id canonique qu'un id brut (tolérance).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...models import StepFinish, canonical_session_id
from ...sqlite_reader import (
    DataSourceError,
    PartRecord,
    SchemaAdapter,
    SessionMeta,
    detect_db,
)
from ..base import HARNESS_OPENCODE, HarnessSession

if TYPE_CHECKING:
    from ...config import TelemetryConfig

PROVIDER_TYPE = HARNESS_OPENCODE


class OpenCodeSessionProvider:
    """Sessions OpenCode (V1 + V2) via l'adaptateur existant, ids canoniques."""

    harness = HARNESS_OPENCODE

    def __init__(self, db_path: Path, adapter: SchemaAdapter) -> None:
        self.db_path = db_path
        self._adapter = adapter

    @classmethod
    def open(cls, cfg: TelemetryConfig) -> OpenCodeSessionProvider | None:
        """Détecte la base (`cfg.opencode_db_path`) ; None si indisponible."""
        try:
            path, adapter = detect_db(cfg.opencode_db_path)
        except DataSourceError:
            return None  # fail-soft : le registry émet l'avertissement
        return cls(path, adapter)

    # --- helpers de namespacing -------------------------------------------

    def _unwrap(self, session_id: str) -> str:
        """Retire le préfixe canonique ; id brut toléré tel quel."""
        prefix = f"{self.harness}:"
        return session_id[len(prefix) :] if session_id.startswith(prefix) else session_id

    def _wrap_meta(self, meta: SessionMeta | None) -> HarnessSession | None:
        return None if meta is None else HarnessSession.from_meta(self.harness, meta)

    def _wrap_steps(self, steps: list[StepFinish]) -> list[StepFinish]:
        for step in steps:
            step.harness = self.harness
            step.session_id = canonical_session_id(self.harness, step.session_id)
        return steps

    # --- Protocol SessionProvider -----------------------------------------

    def check_schema(self) -> None:
        self._adapter.check_schema()

    def list_sessions(self, since_ms: int) -> list[HarnessSession]:
        sessions = [
            s
            for s in (self._wrap_meta(m) for m in self._adapter.list_sessions(since_ms))
            if s is not None
        ]
        return sessions

    def has_telemetry_rows(self, session_id: str) -> bool:
        return self._adapter.has_telemetry_rows(self._unwrap(session_id))

    def session_steps(self, session_id: str, start_ms: int, end_ms: int) -> list[StepFinish]:
        return self._wrap_steps(
            self._adapter.session_steps(self._unwrap(session_id), start_ms, end_ms)
        )

    def session_tools(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        return self._adapter.session_tools(self._unwrap(session_id), start_ms, end_ms)

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        return self._adapter.session_user_turns(self._unwrap(session_id), start_ms, end_ms)

    def session_context_chars(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> dict[str, int]:
        return self._adapter.session_context_chars(self._unwrap(session_id), start_ms, end_ms)

    def session_aggregates(self, session_id: str) -> dict | None:
        return self._adapter.session_aggregates(self._unwrap(session_id))

    def session_parts(self, session_id: str) -> list[PartRecord]:
        return self._adapter.session_parts(self._unwrap(session_id))

    def find_session_by_title(self, title: str) -> HarnessSession | None:
        return self._wrap_meta(self._adapter.find_session_by_title(title))

    def close(self) -> None:
        self._adapter.conn.close()


def build_provider(_source_cfg: dict, cfg: TelemetryConfig) -> OpenCodeSessionProvider | None:
    """Factory registry : ouvre la base OpenCode détectée, None si absente."""
    return OpenCodeSessionProvider.open(cfg)
