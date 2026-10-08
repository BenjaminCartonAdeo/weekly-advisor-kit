"""Dedup intra-run du watch `releases` : clé, fusion, canonicalisation `found_via`.

Détient le vocabulaire `FOUND_VIA_ORDER` / `SOURCE_*` : c'est le module qui
possède la règle d'ordre canonique, donc les consommateurs (`_npm`, `_sources`,
`_watch_repos`, `__init__`) l'importent ici — jamais l'inverse.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..util import iso as _iso
from ..util import parse_iso_ts

#: Canonical found_via ordering — also the processing order of the new-item sources.
FOUND_VIA_ORDER = [
    "npm:keywords:opencode-plugin",
    "github:topic:opencode-plugin",
    "mcp-registry",
    "github:releases:opencode",
    "github:watch-repos",
]
SOURCE_NPM = FOUND_VIA_ORDER[0]
SOURCE_GITHUB = FOUND_VIA_ORDER[1]
SOURCE_MCP = FOUND_VIA_ORDER[2]
SOURCE_RELEASES = FOUND_VIA_ORDER[3]
SOURCE_WATCH = FOUND_VIA_ORDER[4]


# ---------------------------------------------------------------- dedup -------
def _dedup_key(item: dict) -> str:
    """repo_url if non-empty, else npm_package (name as last-resort key)."""
    return item.get("repo_url") or item.get("npm_package") or item.get("name") or ""


def _preferred_record(a: dict, b: dict) -> dict:
    """Most complete record: non-empty description, then earliest published_at."""
    a_desc, b_desc = a.get("description") or "", b.get("description") or ""
    if a_desc and not b_desc:
        return a
    if b_desc and not a_desc:
        return b
    a_ts, b_ts = a.get("published_at"), b.get("published_at")
    if a_ts and b_ts:
        return a if a_ts <= b_ts else b
    return b if b_ts and not a_ts else a


def _merge_into(existing: dict, item: dict) -> dict:
    """Merge `item` (same dedup key) into `existing`; return the surviving record."""
    record = _preferred_record(existing, item)
    merged = dict(record)
    merged["repo_url"] = existing["repo_url"] or item["repo_url"] or ""
    merged["npm_package"] = existing.get("npm_package") or item.get("npm_package")
    merged["found_via"] = list(existing["found_via"])
    for source in item.get("found_via") or []:
        if source not in merged["found_via"]:
            merged["found_via"].append(source)
    merged["new_repo"] = bool(existing.get("new_repo") or item.get("new_repo"))
    return merged


def _add_item(items: dict[str, dict], item: dict) -> None:
    key = _dedup_key(item)
    existing = items.get(key)
    items[key] = _merge_into(existing, item) if existing is not None else dict(item)


def _canonical_found_via(found_via: list[str]) -> list[str]:
    """Re-order found_via to the canonical npm, github, mcp, releases sequence."""
    ordered = [source for source in FOUND_VIA_ORDER if source in found_via]
    return ordered or list(found_via)


# ---------------------------------------------------------------- orchestration


def _finalize_items(items: dict[str, dict]) -> list[dict]:
    result: list[dict] = []
    for record in items.values():
        published = record.get("published_at") or datetime(1970, 1, 1, tzinfo=UTC)
        result.append(
            {
                "name": record.get("name") or "",
                "category": record.get("category") or "plugin",
                "repo_url": record.get("repo_url") or "",
                "npm_package": record.get("npm_package"),
                # passthrough (Task 4) : alimente signature.version de la mémoire.
                "version": record.get("version"),
                "description": record.get("description") or "",
                "published_at": _iso(published),
                "found_via": _canonical_found_via(record.get("found_via") or []),
                "new_repo": bool(record.get("new_repo")),
            }
        )
    result.sort(
        key=lambda item: (
            -(parse_iso_ts(item["published_at"]) or datetime(1970, 1, 1, tzinfo=UTC)).timestamp(),
            item["name"],
        )
    )
    return result
