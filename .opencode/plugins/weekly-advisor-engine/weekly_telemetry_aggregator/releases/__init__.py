"""Écosystème watch — Partie 2 `releases` (veille minimale, schema_version 2).

Point d'entrée du paquet : il garde l'orchestration (`run`, `_collect`,
`_build_ecosystem`, dispatch des sources watch) et réexporte les noms historiques
pour que `from .releases import X` et `releases.X` restent valides.

Stateless observer of the OpenCode plugin ecosystem. Every run re-fetches four
live sources within the sliding period window and filters client-side by date
fields (no persistent dedup file, no DB):

1. npm search (`npm:keywords:opencode-plugin`)
2. GitHub topic search (`github:topic:opencode-plugin`)
3. MCP registry (`mcp-registry`)
4. Official OpenCode GitHub releases (`github:releases:opencode`)

Output is the exact Part 2 JSON schema as a plain dict (the CLI serializes it).
Intra-run dedup only: the same repo found by several sources appears once in
`new_items` (with concatenated `found_via`), while `counts_by_source` increments
for every source hit. ``run`` returns ``(ecosystem_dict, exit_code)`` with
``exit_code`` 0 when at least one source succeeded (even with zero items) and 1
when all sources failed.

Sous-modules :

* `_http`     — client HTTP, retry/backoff, fallback `gh api`
* `dedup`     — clé de dedup, fusion, `FOUND_VIA_ORDER`, `_finalize_items`
* `_npm`      — mapping npm search (extrait à l'étape suivante)
* `_sources`  — fetchers GitHub/MCP/RSS/watch-list
* `_watch_repos` — repos suivis
* `_radar`    — radars MCP
"""

from __future__ import annotations

import base64
import json as json
import os as os
import re
import subprocess as subprocess
import time as time
import xml.etree.ElementTree as ET
from collections.abc import Mapping as Mapping
from datetime import UTC as UTC
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime as parsedate_to_datetime
from pathlib import Path

# stdlib names were reachable on the old single module (`releases.urlopen`, …).
# Redundant aliases mark them as deliberate re-exports (PEP 484 style).
from urllib.error import HTTPError as HTTPError
from urllib.error import URLError as URLError
from urllib.parse import quote
from urllib.parse import urlencode as urlencode
from urllib.request import Request as Request
from urllib.request import urlopen as urlopen

from .. import __version__
from ..config import apply_lookback_override
from ..util import iso as _iso
from ..util import parse_anchor as _parse_anchor
from ..util import parse_iso_ts
from ._http import (
    _BACKOFF,
    _RETRIES,
    SourceError,
    _dumps,
    _get_json,
    _gh_api,
    _github_headers,
    _github_json,
    _HttpClient,
    _Response,
)
from ._npm import (
    NPM_MAX_ROWS,
    NPM_PAGE_SIZE,
    NPM_QUERY,
    URL_NPM,
    _fetch_npm,
    _mcp_map_entry,
    _mcp_repo_url,
    _npm_category,
    _npm_fetch_pages,
    _npm_map_object,
)
from ._radar import (
    _RADAR_DATE_RE,
    _RADAR_HEADERS,
    MCP_PROTOCOL_VERSION,
    _decode_mcp_body,
    _fetch_radar,
    _radar_markdown,
    _radar_mcp_url,
    _radar_post,
)
from ._sources import (
    RELEASES_PER_PAGE,
    URL_GITHUB_TOPICS,
    URL_MCP,
    URL_RELEASES,
    WATCH_ITEMS_CAP,
    _extract_markdown_links,
    _fetch_github_topics,
    _fetch_mcp,
    _fetch_releases,
    _fetch_rss,
    _fetch_watch_list,
    _link_category,
    _load_snapshot,
    _release_summary,
    _rss_date,
    _rss_local,
    _rss_map_node,
    _save_snapshot,
    _snapshot_path,
    _split_repo,
)
from ._watch_repos import (
    _fetch_watch_repos,
    _process_watch_repo,
    _watch_repo_activity_fallback,
    _watch_repo_display_fields,
    _watch_repo_release_items,
)
from .dedup import (
    FOUND_VIA_ORDER,
    SOURCE_GITHUB,
    SOURCE_MCP,
    SOURCE_NPM,
    SOURCE_RELEASES,
    SOURCE_WATCH,
    _add_item,
    _canonical_found_via,
    _dedup_key,
    _finalize_items,
    _merge_into,
    _preferred_record,
)


def _partition_watch_entries(watch_entries: list) -> tuple[list, list, list, list, list]:
    """Split watch entries by type → (repos, lists, topics, rss_urls, radars)."""
    repos = [w["name"] for w in watch_entries if w.get("type", "repo") == "repo"]
    lists = [w["name"] for w in watch_entries if w.get("type") == "list"]
    topics = [w["name"] for w in watch_entries if w.get("type") == "topic"]
    rss_urls = [w["name"] for w in watch_entries if w.get("type") == "rss"]
    radars = [w for w in watch_entries if w.get("type") == "radar"]
    return repos, lists, topics, rss_urls, radars


def _collect_watch_sources(
    cfg, client, start, end, watch_entries, run_source, sink_item, counts_by_source
) -> None:
    """Extended watch dispatch (v5.30) : repo / list / topic / rss / radar entries."""
    state_dir = cfg.output_dir / "watch-state"
    repos, lists, topics, rss_urls, radars = _partition_watch_entries(watch_entries)
    if repos:
        run_source(SOURCE_WATCH, lambda: _fetch_watch_repos(client, repos, start, end), sink_item)
    for repo in lists:
        source_id = f"watch:list:{repo}"
        counts_by_source.setdefault(source_id, 0)
        run_source(
            source_id,
            lambda r=repo: _fetch_watch_list(client, r, end, state_dir),
            sink_item,
        )
    for topic in topics:
        source_id = f"github:topic:{topic}"
        counts_by_source.setdefault(source_id, 0)
        run_source(
            source_id,
            lambda t=topic: _fetch_github_topics(client, t, start, end, cfg.github_min_stars),
            sink_item,
        )
    for url in rss_urls:
        source_id = f"rss:{url}"
        counts_by_source.setdefault(source_id, 0)
        run_source(
            source_id,
            lambda u=url: _fetch_rss(client, u, start, end),
            sink_item,
        )
    # radars MCP (Task 4) : URL résolue depuis <project_root>/opencode.json,
    # repli RSS par entrée ; un radar mort reste un warning toléré.
    for r in radars:
        source_id = f"radar:{r['name']}"
        counts_by_source.setdefault(source_id, 0)
        run_source(
            source_id,
            lambda e=r: _fetch_radar(client, e, start, end, project_root=cfg.project_root),
            sink_item,
        )


def _collect_release_changes(cfg, client, start, end) -> tuple[list, int, Exception | None]:
    """Core release changes — (changes, count, None), or ([], 0, exc) on source failure."""
    try:
        changes, in_window_count = _fetch_releases(client, start, end, cfg.release_keywords)
        return changes, in_window_count, None
    except Exception as exc:  # noqa: BLE001 - one failing source never kills the run
        return [], 0, exc


def _build_ecosystem(cfg, start, end, items, changes, counts_by_source, warnings) -> dict:
    """Final ecosystem payload : finalized items + core changes + counts."""
    new_items = _finalize_items(items)
    core_changes = sorted(
        changes, key=lambda c: (-datetime.fromisoformat(c["date"]).timestamp(), c["version"])
    )
    counts_by_category = {"plugin": 0, "skill": 0, "agent": 0, "mcp-server": 0, "repo": 0}
    for item in new_items:
        cat = item.get("category")
        if cat in counts_by_category:
            counts_by_category[cat] += 1
    return {
        "schema_version": 2,
        "period": {"start": _iso(start), "end": _iso(end)},
        "generated_at": _iso(end),
        "new_items": new_items,
        "core_changes": core_changes,
        "counts_by_category": counts_by_category,
        "counts_by_source": counts_by_source,
        "watch_repos": list(cfg.watch_repos),
        "warnings": warnings,
    }


def _collect(cfg, client, start: datetime, end: datetime) -> tuple[dict, int]:
    counts_by_source = {source: 0 for source in FOUND_VIA_ORDER}
    warnings: list[dict] = []
    items: dict[str, dict] = {}
    changes: list[dict] = []
    ok_sources = 0

    def run_source(source_id: str, fetcher, sink) -> None:
        nonlocal ok_sources
        try:
            records = fetcher()
            ok_sources += 1
            counts_by_source[source_id] += len(records)
            for record in records:
                sink(record)
        except Exception as exc:  # noqa: BLE001 - one failing source never kills the run
            warnings.append({"source": source_id, "message": _fail_message(exc)})

    def sink_item(item: dict) -> None:
        _add_item(items, item)

    run_source(SOURCE_NPM, lambda: _fetch_npm(client, start, end), sink_item)
    run_source(
        SOURCE_GITHUB,
        lambda: _fetch_github_topics(client, "opencode-plugin", start, end, cfg.github_min_stars),
        sink_item,
    )
    run_source(SOURCE_MCP, lambda: _fetch_mcp(client, start, end), sink_item)

    # veille étendue (v5.30) : entrées typées — repo (fenêtre) / list (README diff) / web (diff HTML).
    watch_entries = list(getattr(cfg, "watch", None) or [])
    if not watch_entries and cfg.watch_repos:
        watch_entries = [{"type": "repo", "name": r} for r in cfg.watch_repos]
    if watch_entries:  # no-op source when nothing to watch must not inflate ok_sources
        _collect_watch_sources(
            cfg, client, start, end, watch_entries, run_source, sink_item, counts_by_source
        )

    changes, in_window_count, release_exc = _collect_release_changes(cfg, client, start, end)
    if release_exc is None:
        ok_sources += 1
        counts_by_source[SOURCE_RELEASES] += in_window_count
    else:  # noqa: BLE001 - one failing source never kills the run
        warnings.append({"source": SOURCE_RELEASES, "message": _fail_message(release_exc)})

    return _build_ecosystem(cfg, start, end, items, changes, counts_by_source, warnings), (
        0 if ok_sources >= 1 else 1
    )


def _fail_message(exc: Exception) -> str:
    short = str(exc) or type(exc).__name__
    if len(short) > 120:
        short = short[:120] + "…"
    return f"API indisponible / rate-limitated; source ignorée pour ce run ({short})"


def run(
    cfg,
    *,
    anchor: str | None = None,
    client=None,
    lookback_days: int | None = None,
) -> tuple[dict, int]:
    """Run the ecosystem watch. Returns (ecosystem_dict, exit_code).

    exit_code: 0 = complete (>=1 source ok), 1 = all sources failed.
    """
    apply_lookback_override(cfg, lookback_days)
    run_time = _parse_anchor(anchor)
    window = timedelta(
        hours=cfg.window_hours() if hasattr(cfg, "window_hours") else cfg.lookback_days * 24.0
    )
    start = run_time - window

    if client is None:
        client = _HttpClient(timeout=15)
    return _collect(cfg, client, start, run_time)


__all__ = [
    "FOUND_VIA_ORDER",
    "_BACKOFF",
    "_RETRIES",
    "_dumps",
    "SOURCE_GITHUB",
    "SOURCE_MCP",
    "SOURCE_NPM",
    "SOURCE_RELEASES",
    "SOURCE_WATCH",
    "SourceError",
    "_HttpClient",
    "_Response",
    "_add_item",
    "_canonical_found_via",
    "_dedup_key",
    "_finalize_items",
    "_gh_api",
    "_get_json",
    "_github_headers",
    "_github_json",
    "_merge_into",
    "_preferred_record",
    "run",
]


__all__ = [
    "__version__",
    "_add_item",
    "_build_ecosystem",
    "_canonical_found_via",
    "_collect",
    "_collect_release_changes",
    "_collect_watch_sources",
    "_decode_mcp_body",
    "_dedup_key",
    "_dumps",
    "_extract_markdown_links",
    "_fail_message",
    "_fetch_github_topics",
    "_fetch_mcp",
    "_fetch_npm",
    "_fetch_radar",
    "_fetch_releases",
    "_fetch_rss",
    "_fetch_watch_list",
    "_fetch_watch_repos",
    "_finalize_items",
    "_get_json",
    "_gh_api",
    "_github_headers",
    "_github_json",
    "_HttpClient",
    "_iso",
    "_link_category",
    "_load_snapshot",
    "_mcp_map_entry",
    "_mcp_repo_url",
    "_merge_into",
    "_npm_category",
    "_npm_fetch_pages",
    "_npm_map_object",
    "_parse_anchor",
    "_partition_watch_entries",
    "_preferred_record",
    "_process_watch_repo",
    "_RADAR_DATE_RE",
    "_RADAR_HEADERS",
    "_radar_markdown",
    "_radar_mcp_url",
    "_radar_post",
    "_release_summary",
    "_Response",
    "_RETRIES",
    "_rss_date",
    "_rss_local",
    "_rss_map_node",
    "_save_snapshot",
    "_snapshot_path",
    "_split_repo",
    "_watch_repo_activity_fallback",
    "_watch_repo_display_fields",
    "_watch_repo_release_items",
    "annotations",
    "apply_lookback_override",
    "base64",
    "datetime",
    "ET",
    "FOUND_VIA_ORDER",
    "HTTPError",
    "json",
    "Mapping",
    "MCP_PROTOCOL_VERSION",
    "NPM_MAX_ROWS",
    "NPM_PAGE_SIZE",
    "NPM_QUERY",
    "os",
    "parse_iso_ts",
    "parsedate_to_datetime",
    "Path",
    "quote",
    "re",
    "RELEASES_PER_PAGE",
    "Request",
    "run",
    "SOURCE_GITHUB",
    "SOURCE_MCP",
    "SOURCE_NPM",
    "SOURCE_RELEASES",
    "SOURCE_WATCH",
    "SourceError",
    "subprocess",
    "time",
    "timedelta",
    "URL_GITHUB_TOPICS",
    "URL_MCP",
    "URL_NPM",
    "URL_RELEASES",
    "urlencode",
    "URLError",
    "urlopen",
    "UTC",
    "WATCH_ITEMS_CAP",
]
