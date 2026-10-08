"""Provider Claude Code — lecture des transcripts JSONL de ``~/.claude/projects``.

Layout : ``<projects_dir>/<cwd-mungé>/<sessionId>.jsonl`` — un fichier = une
session ; le munging du répertoire projet (séparateurs/points → tirets) n'est
JAMAIS inversé : les sous-répertoires sont listés exhaustivement. Chaque ligne
est un objet ``{type: user|assistant|cost-state, sessionId, timestamp ISO, cwd,
message:{role, model, content[], usage{...}}}`` pour ``user|assistant`` et
``{type: cost-state, totalCostUSD}`` pour les coûts ; les lignes illisibles ou
non conversationnelles (hors ``cost-state``) sont ignorées avec au plus UN
avertissement par fichier.

Claude Code journalise le coût réel dans les lignes ``type: cost-state``
(``totalCostUSD``) : ``cost`` reflète ce total (max par session) et
``session_aggregates`` expose ``{"cost": total}`` ; à défaut l'estimation
downstream (``main.py``/taux par défaut) s'applique.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...models import StepFinish, canonical_session_id, round6
from ...sqlite_reader import PartRecord, SchemaError
from ..base import HarnessSession

# Ré-export de la couche de parsing JSONL : les noms privés restent atteignables
# depuis `claude_code` pour les appelants et tests historiques. Les noms marqués
# `noqa` ne sont plus utilisés dans ce module (parsant pur) : ils restent importés
# pour que toute référence historique continue de résoudre.
from ._jsonl import (
    _TITLE_MAX_CHARS,  # noqa: F401
    _block_text,
    _blocks,
    _JsonlSession,
    _Line,  # noqa: F401
    _load_sessions,
    _merge_session,  # noqa: F401
    _model_key,
    _parse_ts,  # noqa: F401
    _read_jsonl,  # noqa: F401
    _record_cost,  # noqa: F401
    _stringify,
    _usage_tokens,
    _user_text,
    _windowed,
)

if TYPE_CHECKING:
    from ...config import TelemetryConfig

#: Identifiant de harnais (préfixe des ids canoniques).
HARNESS_CLAUDE_CODE = "claude-code"

PROVIDER_TYPE = HARNESS_CLAUDE_CODE

#: Répertoire des transcripts par défaut si `projects_dir` non fourni.
_DEFAULT_PROJECTS_DIR = "~/.claude/projects"

#: Clés de contexte alignées sur l'adaptateur OpenCode existant.
_CONTEXT_KEYS = ("file", "tool_result", "text", "reasoning")


class ClaudeCodeSessionProvider:
    """Sessions Claude Code via leurs transcripts JSONL, ids canoniques."""

    harness = HARNESS_CLAUDE_CODE

    def __init__(self, projects_dir: Path) -> None:
        #: chemin source (enrichit la section doctor `[claude-code] OK (...)`).
        self.db_path = projects_dir
        self._sessions = _load_sessions(projects_dir)

    # --- helpers ----------------------------------------------------------

    def _unwrap(self, session_id: str) -> str:
        prefix = f"{self.harness}:"
        return session_id[len(prefix) :] if session_id.startswith(prefix) else session_id

    def _get(self, session_id: str) -> _JsonlSession | None:
        return self._sessions.get(self._unwrap(session_id))

    def _to_harness_session(self, session: _JsonlSession) -> HarnessSession:
        tokens = [t for line in session.lines if (t := _usage_tokens(line.entry.get("message")))]
        model_key = next(
            (
                _model_key(line.entry.get("message"))
                for line in session.lines
                if _usage_tokens(line.entry.get("message"))
            ),
            "unknown/unknown",
        )
        return HarnessSession(
            harness=self.harness,
            session_id=canonical_session_id(self.harness, session.session_id),
            title=session.title,
            parent_id=None,
            model_key=model_key,
            agent=None,
            directory=session.directory,
            cost=session.cost,
            tokens_input=sum(t[0] for t in tokens),
            tokens_output=sum(t[1] for t in tokens),
            tokens_reasoning=sum(t[2] for t in tokens),
            tokens_cache_read=sum(t[3] for t in tokens),
            tokens_cache_write=sum(t[4] for t in tokens),
            time_created=datetime.fromtimestamp(session.first_ms / 1000, tz=UTC),
            time_updated=datetime.fromtimestamp(session.last_ms / 1000, tz=UTC),
        )

    # --- Protocol SessionProvider -------------------------------------------

    def check_schema(self) -> None:
        if not self.db_path.is_dir():
            raise SchemaError(f"répertoire projects introuvable : {self.db_path}")
        if not self._sessions:
            raise SchemaError(f"aucun transcript JSONL parsable sous {self.db_path}")

    def list_sessions(self, since_ms: int) -> list[HarnessSession]:
        sessions = [
            self._to_harness_session(s) for s in self._sessions.values() if s.last_ms >= since_ms
        ]
        return sorted(
            sessions,
            key=lambda s: s.time_updated or s.time_created or datetime.min.replace(tzinfo=UTC),
        )

    def has_telemetry_rows(self, session_id: str) -> bool:
        session = self._get(session_id)
        return bool(session) and any(
            _usage_tokens(line.entry.get("message")) is not None
            for line in session.lines  # type: ignore[union-attr]
        )

    def session_steps(self, session_id: str, start_ms: int, end_ms: int) -> list[StepFinish]:
        session = self._get(session_id)
        if session is None:
            return []
        session_total_tokens: float | None = None
        per_token_cost: float | None = None
        if session.cost is not None:
            total = 0.0
            for line in session.lines:
                tok = _usage_tokens(line.entry.get("message"))
                if tok is not None:
                    tin, tout, treas, _, _ = tok
                    total += float(tin + tout + treas)
            if total > 0:
                session_total_tokens = total
                per_token_cost = float(session.cost) / total
        steps: list[StepFinish] = []
        for line in _windowed(session.lines, start_ms, end_ms):
            message = line.entry.get("message")
            if line.entry.get("type") != "assistant":
                continue
            if (tokens := _usage_tokens(message)) is None:
                continue
            tin, tout, treas, tread, twrite = tokens
            step_cost: float | None = None
            if per_token_cost is not None and session_total_tokens is not None:
                step_cost = round6(per_token_cost * float(tin + tout + treas))
            steps.append(
                StepFinish(
                    session_id=canonical_session_id(self.harness, session.session_id),
                    timestamp=line.ts,
                    model=_model_key(message),
                    tokens_input=tin,
                    tokens_output=tout,
                    tokens_reasoning=treas,
                    tokens_cache_read=tread,
                    tokens_cache_write=twrite,
                    cost=step_cost,
                    harness=self.harness,
                )
            )
        return steps

    def session_tools(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        session = self._get(session_id)
        if session is None:
            return {}, {}, {}
        tool_calls: dict[str, int] = {}
        tool_arg_chars: dict[str, int] = {}
        for line in _windowed(session.lines, start_ms, end_ms):
            for block in _blocks(line.entry):
                if block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "unknown")
                tool_calls[name] = tool_calls.get(name, 0) + 1
                arg_chars = len(json.dumps(block.get("input"), ensure_ascii=False))
                tool_arg_chars[name] = tool_arg_chars.get(name, 0) + arg_chars
        return tool_calls, tool_arg_chars, {}  # skills_loaded : sans objet hors CLI

    def session_tool_fingerprints(
        self, _session_id: str, _start_ms: int, _end_ms: int
    ) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
        """Payloads bruts non exposés par ce harnais : dicts vides."""
        return {}, {}

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        session = self._get(session_id)
        if session is None:
            return []
        turns = []
        for line in _windowed(session.lines, start_ms, end_ms):
            if line.entry.get("type") != "user":
                continue
            text = _user_text(line.entry)
            if text and text.strip():
                turns.append(text)
        return turns

    def session_context_chars(self, session_id: str, start_ms: int, end_ms: int) -> dict[str, int]:
        session = self._get(session_id)
        if session is None:
            return dict.fromkeys(_CONTEXT_KEYS, 0)
        counts = dict.fromkeys(_CONTEXT_KEYS, 0)
        for line in _windowed(session.lines, start_ms, end_ms):
            for block in _blocks(line.entry):
                kind = block.get("type")
                if kind == "text":
                    counts["text"] += len(_block_text(block))
                elif kind == "thinking":
                    reasoning = block.get("thinking")
                    counts["reasoning"] += len(reasoning) if isinstance(reasoning, str) else 0
                elif kind == "tool_result":
                    counts["tool_result"] += len(_stringify(block.get("content")))
        return counts

    def session_aggregates(self, session_id: str) -> dict | None:
        sess = self._get(session_id)  # tolérance id brut/canonique
        if sess is None or sess.cost is None:
            return None
        return {"cost": sess.cost}

    def session_parts(self, session_id: str) -> list[PartRecord]:
        session = self._get(session_id)
        if session is None:
            return []
        parts: list[PartRecord] = []
        for line in session.lines:
            role = line.entry.get("type")
            for block in _blocks(line.entry):
                kind = block.get("type")
                if role == "user" and kind == "tool_result":
                    output = _stringify(block.get("content"))
                    parts.append(PartRecord(ts=line.ts, kind="tool", tool_output=output or None))
                elif kind == "text":
                    parts.append(
                        PartRecord(ts=line.ts, kind=role or "assistant", text=_block_text(block))
                    )
                elif kind == "thinking":
                    thinking = block.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        parts.append(PartRecord(ts=line.ts, kind="reasoning", text=thinking))
                elif kind == "tool_use":
                    parts.append(
                        PartRecord(
                            ts=line.ts,
                            kind="tool",
                            tool_name=str(block.get("name") or "unknown"),
                            tool_input=json.dumps(block.get("input"), ensure_ascii=False),
                        )
                    )
        return parts

    def find_session_by_title(self, title: str) -> HarnessSession | None:
        for session in self._sessions.values():
            if session.title == title:
                return self._to_harness_session(session)
        return None

    def close(self) -> None:
        """Aucune ressource persistante (lectures ponctuelles) — no-op idempotent."""


def build_provider(source_cfg: dict, _cfg: TelemetryConfig) -> ClaudeCodeSessionProvider | None:
    """Factory registry : None si `projects_dir` absent (source indisponible propre)."""
    raw_dir = source_cfg.get("projects_dir") if isinstance(source_cfg, dict) else None
    projects_dir = Path(
        raw_dir if isinstance(raw_dir, str) and raw_dir else _DEFAULT_PROJECTS_DIR
    ).expanduser()
    if not projects_dir.is_dir():
        return None
    return ClaudeCodeSessionProvider(projects_dir)
