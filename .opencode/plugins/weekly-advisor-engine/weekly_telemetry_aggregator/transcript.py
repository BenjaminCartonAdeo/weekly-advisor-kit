"""Human-readable session transcript rendering for show-session (Partie 0 §3).

One entry per turn (tool called + key args + result summary, user text,
files, reasoning). Repetitive sequences (N >= 3 consecutive similar calls of
the same tool) are mechanically compacted — deduplication, never synthesis.

Multi-harnais : `render_session` prend une liste de providers (protocol
`SessionProvider`) et route vers celui dont le harnais correspond au préfixe
canonique ``"<harness>:<session_id>"`` de l'id demandé ; un id brut est toléré
et routé vers la première source.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .models import split_canonical_session_id
from .util import descendants_by_parent

if TYPE_CHECKING:
    from .providers.base import SessionProvider

#: Compaction threshold: N >= 3 similar consecutive calls of the same tool (Partie 0 §3).
COMPACT_MIN = 3
#: Similarity ratio for two tool calls to be considered "similar" (args + error result).
COMPACT_SIMILARITY = 0.9
#: Rendering caps — keep the extract bounded.
ARGS_CAP = 200
OUTPUT_CAP = 300
TEXT_CAP = 400
#: Hard ceiling on rendered transcript bytes. Giant sessions once OOM-killed the
#: step-3 audit (observed exit=137, 2026-08-24 07:41). `render_session` truncates
#: past this with a clear marker instead of accumulating an unbounded string.
MAX_EXTRACT_BYTES = 2_000_000


def _canonical_id(provider: SessionProvider, session_id: str) -> str:
    """Id canonique pour ce provider : préfixe du harnais ajouté si absent."""
    prefix = f"{provider.harness}:"
    return session_id if session_id.startswith(prefix) else prefix + session_id


def _children_ids(provider: SessionProvider, session_id: str) -> list[str]:
    """All descendant session ids (indexed BFS shared with aggregator, util).

    Les metas des providers exposent un `parent_id` brut : il est re-préfixé
    avec le harnais du provider pour rester comparable aux ids canoniques.
    """
    canonical = _canonical_id(provider, session_id)
    prefix = f"{provider.harness}:"

    def _canonical_parent(parent: str | None) -> str | None:
        if not parent or parent.startswith(prefix):
            return parent
        return prefix + parent

    return descendants_by_parent(
        ((m.session_id, _canonical_parent(m.parent_id)) for m in provider.list_sessions(0)),
        canonical,
    )


def _select_provider(providers: Sequence[SessionProvider], session_id: str) -> SessionProvider:
    """Route vers le provider du harnais déduit de l'id ; première source sinon."""
    harness, _raw = split_canonical_session_id(session_id)
    for provider in providers:
        if provider.harness == harness:
            return provider
    return providers[0]  # id brut ou harnais inconnu → première source (tolérance)


def _render_part(part) -> str:
    if part.kind == "user":
        text = " ".join((part.text or "").split())
        return f"user: {text[:TEXT_CAP]}" if text else ""
    if part.kind == "tool":
        args = (part.tool_input or "").replace("\n", " ")[:ARGS_CAP]
        output = " ".join((part.tool_output or "").split())[:OUTPUT_CAP]
        line = f"tool: {part.tool_name}"
        if args:
            line += f" args={{{args}}}"
        if output:
            line += f" → {output}"
        return line
    if part.kind == "file":
        return f"file: {part.text}"
    if part.kind == "reasoning":
        text = " ".join((part.text or "").split())
        return f"reasoning: {text[:200]}" if text else ""
    return f"{part.kind}: {(part.text or '')[:200]}"


def _compact(entries: list[str]) -> list[str]:
    """Collapse consecutive similar tool-call runs into first + masked + last."""
    out: list[str] = []
    i = 0
    n = len(entries)
    while i < n:
        first = entries[i]
        j = i + 1
        if first.startswith("tool:"):
            head = first.split(" → ")[0].split(" args=")[0]
            while (
                j < n
                and entries[j].startswith("tool:")
                and entries[j].split(" → ")[0].split(" args=")[0] == head
                and difflib.SequenceMatcher(None, first, entries[j]).ratio() >= COMPACT_SIMILARITY
            ):
                j += 1
        else:
            while j < n and entries[j] == first:
                j += 1
        run_len = j - i
        if run_len >= COMPACT_MIN:
            out.append(first)
            out.append(
                f"[... {run_len - 2} appels similaires du même outil masqués (compaction) ...]"
            )
            out.append(entries[j - 1])
        else:
            out.extend(entries[i:j])
        i = j
    return out


def render_session(
    providers: SessionProvider | Sequence[SessionProvider],
    session_id: str,
    *,
    include_children: bool = False,
    max_extract_bytes: int | None = MAX_EXTRACT_BYTES,
) -> str:
    """Render a session transcript (with optional subagent children) to readable text.

    Multi-harnais : `providers` est une liste (ou un provider seul) ; la source
    est choisie d'après le préfixe canonique de `session_id`.

    `max_extract_bytes` borne la taille du texte rendu (défaut `MAX_EXTRACT_BYTES`).
    Au-delà, le corps est tronqué et un marqueur ``[truncated: N bytes omitted]``
    est ajouté — évite l'OOM (exit=137) sur les sessions géantes. `None` désactive
    la borne (comportement historique, non borné).
    """
    sources: list[SessionProvider] = (
        list(providers) if isinstance(providers, (list, tuple)) else [providers]
    )
    provider = _select_provider(sources, session_id)
    canonical = _canonical_id(provider, session_id)
    ids = [canonical]
    if include_children:
        ids.extend(_children_ids(provider, canonical))
    records = []
    for sid in ids:
        records.extend(provider.session_parts(sid))
    records.sort(key=lambda r: r.ts)

    lines = [f"# Session {session_id}  —  {len(ids)} session(s)", ""]
    entries = [ln for rec in records if (ln := _render_part(rec))]
    lines.extend(_compact(entries))
    text = "\n".join(lines) + "\n"

    if max_extract_bytes is not None:
        full_bytes = len(text.encode("utf-8"))
        if full_bytes > max_extract_bytes:
            marker = f"\n… [truncated: {full_bytes - max_extract_bytes} bytes omitted]\n"
            # Réserve la place du marqueur pour rester sous la borne.
            cap = max_extract_bytes - len(marker.encode("utf-8"))
            cap = cap if cap >= 0 else 0
            truncated = text.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
            text = truncated + marker
    return text
