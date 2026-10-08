"""Couche de parsing des transcripts JSONL Claude Code (extrait de ``claude_code.py``).

Module purement fonctionnel : aucune dépendance à un autre module du paquet, uniquement
de la stdlib. Il contient la vue plate d'une session (``_JsonlSession`` / ``_Line``) et
les helpers qui transforment un fichier ``<cwd-mungé>/<sessionId>.jsonl`` en lignes
horodatées, tokens, coûts et sessions fusionnées. ``claude_code.py`` ré-importe tous
ces noms (les privés inclus) pour préserver les références historiques.
"""

from __future__ import annotations

import json
import warnings
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

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
    cost: float | None = None

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
        return "\n".join(t for t in (_block_text(b) for b in value if isinstance(b, dict)) if t)
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
        return float(value) if isinstance(value, int | float) and value > 0 else 0.0

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


def _record_cost(
    costs: dict[str, float], entry: dict, session_id: str | None, fallback_sid: str
) -> None:
    """Track the max ``totalCostUSD`` per session id (id from row, else running/fallback)."""
    raw_cost = entry.get("totalCostUSD")
    if not isinstance(raw_cost, int | float) or raw_cost < 0:
        return
    raw_sid = entry.get("sessionId")
    cost_sid = raw_sid if isinstance(raw_sid, str) and raw_sid else (session_id or fallback_sid)
    prev = costs.get(cost_sid)
    val = float(raw_cost)
    if prev is None or val > prev:
        costs[cost_sid] = val


def _read_jsonl(path: Path, costs: dict[str, float]) -> tuple[list[_Line], str | None]:
    """Parse one ``<sid>.jsonl``; cost-state rows land in ``costs``.

    Returns ``(turns, session_id)``; a partially unreadable file warns but still
    yields whatever parsed (fail-soft).
    """
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
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type")
            if etype == "cost-state":
                _record_cost(costs, entry, session_id, path.stem)
                continue
            if etype not in ("user", "assistant"):
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
    return lines, session_id


def _merge_session(sessions: dict[str, _JsonlSession], sid: str, lines: list[_Line]) -> None:
    """Create the session, or merge chronologically (same sid across directories)."""
    existing = sessions.get(sid)
    if existing is None:
        sessions[sid] = _JsonlSession(session_id=sid, lines=sorted(lines, key=lambda ln: ln.ms))
        return
    existing.lines.extend(lines)
    existing.lines.sort(key=lambda ln: ln.ms)


def _load_sessions(projects_dir: Path) -> dict[str, _JsonlSession]:
    """Scan fail-soft `<projects_dir>/*/<sid>.jsonl` ; ids depuis lignes sinon stem."""
    sessions: dict[str, _JsonlSession] = {}
    costs: dict[str, float] = {}
    for path in sorted(projects_dir.glob("*/*.jsonl")):
        lines, session_id = _read_jsonl(path, costs)
        if lines:
            _merge_session(sessions, session_id or path.stem, lines)
    for sid, val in costs.items():
        sess = sessions.get(sid)
        if sess is not None:
            sess.cost = val if sess.cost is None or val > sess.cost else sess.cost
    return sessions


def _windowed(lines: list[_Line], start_ms: int, end_ms: int) -> list[_Line]:
    return [line for line in lines if start_ms <= line.ms <= end_ms]
