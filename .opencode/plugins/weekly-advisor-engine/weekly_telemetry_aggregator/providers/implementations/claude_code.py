"""Provider Claude Code — lecture des transcripts JSONL de ``~/.claude/projects``.

Layout : ``<projects_dir>/<cwd-mungé>/<sessionId>.jsonl`` — un fichier = une
session ; le munging du répertoire projet (séparateurs/points → tirets) n'est
JAMAIS inversé : les sous-répertoires sont listés exhaustivement. Chaque ligne
est un objet ``{type: user|assistant, sessionId, timestamp ISO, cwd,
message:{role, model, content[], usage{...}}}`` ; les lignes illisibles ou non
conversationnelles sont ignorées avec au plus UN avertissement par fichier.

Claude Code ne journalise aucun prix ni agrégat vie-telle-enregistrée :
``cost`` est toujours None (l'estimation downstream s'applique via
``main.py``/taux par défaut) et ``session_aggregates`` renvoie None.
"""

from __future__ import annotations

import json
import warnings
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...models import StepFinish, canonical_session_id
from ...sqlite_reader import PartRecord, SchemaError
from ..base import HarnessSession

if TYPE_CHECKING:
    from ...config import TelemetryConfig

#: Identifiant de harnais (préfixe des ids canoniques).
HARNESS_CLAUDE_CODE = "claude-code"

PROVIDER_TYPE = HARNESS_CLAUDE_CODE

#: Répertoire des transcripts par défaut si `projects_dir` non fourni.
_DEFAULT_PROJECTS_DIR = "~/.claude/projects"

#: Clés de contexte alignées sur l'adaptateur OpenCode existant.
_CONTEXT_KEYS = ("file", "tool_result", "text", "reasoning")

#: Longueur max du titre heuristique (premier tour utilisateur tronqué).
_TITLE_MAX_CHARS = 100


@dataclass(slots=True)
class _Line:
    """Une ligne JSONL conversationnelle déjà validée (timestamp parsable)."""

    ts: datetime
    ms: int
    entry: dict


@dataclass(slots=True)
class _JsonlSession:
    """Vue plate d'un fichier `<cwd-mungé>/<sessionId>.jsonl`."""

    session_id: str
    lines: list[_Line] = field(default_factory=list)

    @property
    def first_ms(self) -> int:
        return min(line.ms for line in self.lines)

    @property
    def last_ms(self) -> int:
        return max(line.ms for line in self.lines)

    @property
    def directory(self) -> str | None:
        """Cwd majoritaire des lignes de la session (None si aucune)."""
        counter = Counter(
            cwd for line in self.lines if isinstance(cwd := line.entry.get("cwd"), str) and cwd
        )
        return counter.most_common(1)[0][0] if counter else None

    @property
    def title(self) -> str | None:
        """Premier texte utilisateur, whitespace aplati et tronqué."""
        for line in self.lines:
            text = _user_text(line.entry)
            if text and text.strip():
                return " ".join(text.split())[:_TITLE_MAX_CHARS]
        return None


def _parse_ts(raw: object) -> datetime | None:
    """Timestamp ISO (``...Z`` toléré) → datetime tz-aware ; None sinon."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _blocks(entry: dict) -> list[dict]:
    """Blocks du message ; contenu string normalisé en un block texte unique."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _block_text(block: dict) -> str:
    text = block.get("text")
    return text if isinstance(text, str) else ""


def _user_text(entry: dict) -> str | None:
    """Texte d'un tour utilisateur (blocks texte uniquement, tool_result exclus)."""
    parts = [_block_text(b) for b in _blocks(entry) if b.get("type") == "text"]
    joined = "\n".join(p for p in parts if p.strip())
    return joined or None


def _stringify(value: object) -> str:
    """Contenu tool_result (str | blocks | autre) rendu en texte brut."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            t for t in (_block_text(b) for b in value if isinstance(b, dict)) if t
        )
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _usage_tokens(message: object) -> tuple[float, float, float, float, float] | None:
    """(in, out, reasoning, cache_read, cache_write) ; None sans usage exploitable."""
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None

    def _num(key: str) -> float:
        value = usage.get(key)
        return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0

    tin, tout = _num("input_tokens"), _num("output_tokens")
    if tin <= 0 and tout <= 0:
        return None
    return (
        tin,
        tout,
        0.0,  # pas de comptage reasoning séparé côté Claude Code
        _num("cache_read_input_tokens"),
        _num("cache_creation_input_tokens"),
    )


def _model_key(message: object) -> str:
    raw = message.get("model") if isinstance(message, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return "unknown/unknown"
    base = raw.split("[", 1)[0].strip() or raw.strip()  # suffixe variant "[slurm]" écarté
    return base if "/" in base else f"anthropic/{base}"


def _load_sessions(projects_dir: Path) -> dict[str, _JsonlSession]:
    """Scan fail-soft `<projects_dir>/*/<sid>.jsonl` ; ids depuis lignes sinon stem."""
    sessions: dict[str, _JsonlSession] = {}
    for path in sorted(projects_dir.glob("*/*.jsonl")):
        broken = 0
        lines: list[_Line] = []
        session_id: str | None = None
        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    broken += 1
                    continue
                if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant"):
                    continue
                ts = _parse_ts(entry.get("timestamp"))
                if ts is None:
                    continue  # ligne sans horodatage exploitable : ignorée
                if isinstance(entry.get("sessionId"), str) and entry["sessionId"]:
                    session_id = session_id or entry["sessionId"]
                lines.append(_Line(ts=ts, ms=int(ts.timestamp() * 1000), entry=entry))
        if broken:
            warnings.warn(
                f"JSONL partiellement illisible ({broken} ligne(s)) : {path.name}", stacklevel=2
            )
        sid = session_id or path.stem
        if lines:
            existing = sessions.get(sid)
            if existing is None:
                sessions[sid] = _JsonlSession(
                    session_id=sid, lines=sorted(lines, key=lambda ln: ln.ms)
                )
            else:  # même sessionId vu sous plusieurs répertoires : fusion chronologique
                existing.lines.extend(lines)
                existing.lines.sort(key=lambda ln: ln.ms)
    return sessions


def _windowed(lines: list[_Line], start_ms: int, end_ms: int) -> list[_Line]:
    return [line for line in lines if start_ms <= line.ms <= end_ms]


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
            (_model_key(line.entry.get("message")) for line in session.lines if _usage_tokens(line.entry.get("message"))),
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
            cost=None,  # aucun prix journalisé par Claude Code
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
            _usage_tokens(line.entry.get("message")) is not None for line in session.lines  # type: ignore[union-attr]
        )

    def session_steps(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[StepFinish]:
        session = self._get(session_id)
        if session is None:
            return []
        steps: list[StepFinish] = []
        for line in _windowed(session.lines, start_ms, end_ms):
            message = line.entry.get("message")
            if line.entry.get("type") != "assistant":
                continue
            if (tokens := _usage_tokens(message)) is None:
                continue
            tin, tout, treas, tread, twrite = tokens
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
                    cost=None,  # estimation downstream (taux par défaut), jamais ici
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

    def session_context_chars(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> dict[str, int]:
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
        self._get(session_id)  # tolérance id brut/canonique
        return None  # aucun agrégat vie-telle-enregistrée dans les JSONL

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
                    parts.append(PartRecord(ts=line.ts, kind=role or "assistant", text=_block_text(block)))
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
