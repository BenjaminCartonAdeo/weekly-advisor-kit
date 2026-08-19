"""Écosystème watch — Partie 2 `releases` (veille minimale, schema_version 2).

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
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import apply_lookback_override
from .util import iso as _iso
from .util import parse_anchor as _parse_anchor
from .util import parse_iso_ts

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

#: Retry/backoff — total attempts, backoff between attempts (spec: 1s, 2s).
#: Module-level constant so tests can shorten it without hitting the network.
_RETRIES = 3
_BACKOFF: tuple[float, ...] = (1.0, 2.0)

#: npm search pagination (registry caps size at 250 rows per page).
NPM_PAGE_SIZE = 250
NPM_MAX_ROWS = 1000
NPM_QUERY = "keywords:opencode-plugin,opencode"
RELEASES_PER_PAGE = 100

# URLs (one per source, resolved per run).
URL_NPM = "https://registry.npmjs.org/-/v1/search"
URL_GITHUB_TOPICS = "https://api.github.com/search/repositories"
URL_MCP = "https://registry.modelcontextprotocol.io/v0.1/servers"
URL_RELEASES = "https://api.github.com/repos/anomalyco/opencode/releases"


class SourceError(Exception):
    """One watch source ultimately failed; the run continues (warning, non-fatal)."""


def _get_json(client, url: str, *, params: dict | None = None, headers: dict | None = None):
    """GET JSON with retry/backoff on {429, 5xx} and network errors.

    Other 4xx (404/401/403) are not retried. Raises :class:`SourceError` when
    the source ultimately fails after ``_RETRIES`` attempts.
    """
    last: Exception | None = None
    for attempt in range(_RETRIES):
        if attempt:
            time.sleep(_BACKOFF[attempt - 1])
        try:
            resp = client.get(url, params=params, headers=headers, timeout=None)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 or exc.response.status_code >= 500:
                last = exc
                continue
            raise SourceError(f"{url}: HTTP {exc.response.status_code}") from exc
        except httpx.TransportError as exc:
            last = exc  # network: ConnectError, TimeoutException, read/write errors…
            continue
        except httpx.HTTPError as exc:  # any other transport-level failure
            last = exc
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last = RuntimeError(f"HTTP {resp.status_code}")
            continue
        if resp.status_code >= 400:
            raise SourceError(f"{url}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except (TypeError, ValueError) as exc:  # includes json.JSONDecodeError
            last = exc
            continue
    raise SourceError(f"{url}: failed after {_RETRIES} attempts ({last})")


def _github_headers() -> dict | None:
    """Optional `Authorization: Bearer $GITHUB_TOKEN` when the env token is set."""
    token = os.environ.get("GITHUB_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else None


def _github_json(client, url: str, *, params: dict | None = None):
    """GitHub API call with authenticated fallback (v5.28 K3).

    Tries plain HTTP (+ GITHUB_TOKEN env) first; on failure (private repo,
    rename 404, anonymous rate-limit) falls back to the authenticated ``gh``
    CLI so every GitHub source works in the same way as the watch.
    """
    try:
        return _get_json(client, url, params=params, headers=_github_headers())
    except SourceError:
        path = url.removeprefix("https://api.github.com/")
        if params:
            # safe="+": le + est un espace dans les requêtes GitHub (search q=...),
            # quote() l'encoderait en %2B et casserait la query (v5.30).
            path = (
                path + "?" + "&".join(f"{k}={quote(str(v), safe='+')}" for k, v in params.items())
            )
        return _gh_api(path)


# ---------------------------------------------------------------- npm -------
def _npm_category(package: dict) -> str:
    """Heuristic: "skill" when the name or keywords suggest a skill, else "plugin"."""
    name = str(package.get("name") or "").lower()
    keywords = package.get("keywords")
    joined = " ".join(str(k).lower() for k in keywords) if isinstance(keywords, list) else ""
    if "skill" in name or "skill" in joined:
        return "skill"
    return "plugin"


def _fetch_npm(client, start: datetime, end: datetime) -> list[dict]:
    """npm search, paginated up to NPM_MAX_ROWS when `total > 250`."""
    params: dict = {"text": NPM_QUERY, "size": NPM_PAGE_SIZE}
    payload = _get_json(client, URL_NPM, params=params)
    total = int(payload.get("total") or 0) if isinstance(payload, dict) else 0
    objects: list = payload.get("objects") if isinstance(payload, dict) else []
    if not isinstance(objects, list):
        objects = []
    offset = NPM_PAGE_SIZE
    while offset < total and offset < NPM_MAX_ROWS:
        page = _get_json(
            client, URL_NPM, params={"text": NPM_QUERY, "size": NPM_PAGE_SIZE, "from": offset}
        )
        page_objects = page.get("objects") if isinstance(page, dict) else []
        if not isinstance(page_objects, list) or not page_objects:
            break
        objects.extend(page_objects)
        offset += NPM_PAGE_SIZE

    items: list[dict] = []
    for obj in objects:
        package = obj.get("package") if isinstance(obj, dict) else None
        if not isinstance(package, dict):
            continue
        published = parse_iso_ts(package.get("date"))
        if published is None or not (start <= published <= end):
            continue
        links = package.get("links")
        repo_url = links.get("repository") if isinstance(links, dict) else ""
        name = str(package.get("name") or "")
        items.append(
            {
                "name": name,
                "category": _npm_category(package),
                "repo_url": str(repo_url or "") if repo_url else "",
                "npm_package": name or None,
                "description": str(package.get("description") or ""),
                "published_at": published,
                "found_via": [SOURCE_NPM],
                "new_repo": False,
            }
        )
    return items


# ---------------------------------------------------------------- github -----
def _fetch_github_topics(
    client, topic: str, start: datetime, end: datetime, min_stars: int
) -> list[dict]:
    """GitHub topic search (type topic, v5.30) — API datée (pushed_at/created_at).

    Une seule implémentation pour tous les topics (y compris opencode-plugin,
    le topic historique de la Partie 2). Query pré-construite : le + serait
    encodé en %2B par httpx/quote et GitHub le chercherait comme terme littéral.
    """
    query = f"q=topic:{quote(topic)}%20stars:%3E{min_stars}&sort=updated&per_page=50"
    payload = _github_json(client, f"{URL_GITHUB_TOPICS}?{query}")
    repos = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(repos, list):
        repos = []
    items: list[dict] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        pushed = parse_iso_ts(repo.get("pushed_at"))
        if pushed is None or not (start <= pushed <= end):
            continue
        created = parse_iso_ts(repo.get("created_at"))
        items.append(
            {
                "name": str(repo.get("full_name") or ""),
                "category": "plugin",
                "repo_url": str(repo.get("html_url") or ""),
                "npm_package": None,
                "description": str(repo.get("description") or ""),
                "published_at": pushed,
                "found_via": [f"github:topic:{topic}"],
                "new_repo": bool(created is not None and start <= created <= end),
            }
        )
    return items


# ---------------------------------------------------------------- mcp --------
def _mcp_repo_url(inner: dict) -> str:
    """First remote URL: direct keys first, then the `remotes` list."""
    for key in ("repository", "githubUrl", "homepage", "sourceUrl", "repositoryUrl"):
        value = inner.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    remotes = inner.get("remotes")
    if isinstance(remotes, list):
        for remote in remotes:
            url = remote.get("url") if isinstance(remote, dict) else remote
            if isinstance(url, str) and url.startswith("http"):
                return url
    return ""


def _fetch_mcp(client, start: datetime, end: datetime) -> list[dict]:
    """MCP registry, filtered client-side: deleted, non-latest, out-of-window."""
    payload = _get_json(
        client,
        URL_MCP,
        params={"updated_since": _iso(start), "version": "latest"},
    )
    servers = payload.get("servers") if isinstance(payload, dict) else []
    if not isinstance(servers, list):
        servers = []

    items: list[dict] = []
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        outer_meta = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else {}
        official = outer_meta.get("io.modelcontextprotocol.registry/official")
        if not isinstance(official, dict):
            official = {}
        # Defensive guard: skip non-latest revisions of an official server.
        if official.get("isLatest") is False:
            continue
        inner = entry.get("server") if isinstance(entry.get("server"), dict) else entry
        if not isinstance(inner, dict):
            continue
        status = str(inner.get("status") or official.get("status") or "")
        if status == "deleted":
            continue
        published = parse_iso_ts(official.get("publishedAt") or inner.get("publishedAt"))
        if published is None or not (start <= published <= end):
            continue
        items.append(
            {
                "name": str(inner.get("name") or inner.get("title") or ""),
                "category": "mcp-server",
                "repo_url": _mcp_repo_url(inner),
                "npm_package": None,
                "description": str(inner.get("description") or ""),
                "published_at": published,
                "found_via": [SOURCE_MCP],
                "new_repo": False,
            }
        )
    return items


# ---------------------------------------------------------------- releases ---
def _release_summary(body: str) -> str:
    """First ~5 non-empty lines joined with " · ", capped at 400 chars."""
    lines: list[str] = []
    for line in body.splitlines():
        text = line.strip().lstrip("-* ").strip()
        if text:
            lines.append(text)
        if len(lines) >= 5:
            break
    joined = " · ".join(lines)
    if len(joined) > 400:
        joined = joined[:400].rstrip() + "…"
    return joined


def _fetch_releases(
    client, start: datetime, end: datetime, keywords: list[str]
) -> tuple[list[dict], int]:
    """OpenCode releases → (changes, in_window_count).

    Only in-window releases with ≥1 keyword match become `core_changes` (0-keyword
    releases are still counted by source, per spec), so the source count is the
    number of in-window releases regardless of keyword matches.
    """
    payload = _github_json(client, URL_RELEASES, params={"per_page": RELEASES_PER_PAGE})
    releases = payload if isinstance(payload, list) else []

    changes: list[dict] = []
    in_window = 0
    for release in releases:
        if not isinstance(release, dict):
            continue
        published = parse_iso_ts(release.get("published_at"))
        if published is None or not (start <= published <= end):
            continue
        in_window += 1
        body = str(release.get("body") or "")
        matched = [kw for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", body, re.IGNORECASE)]
        if not matched:  # 0-keyword release: counted by source, not emitted
            continue
        changes.append(
            {
                "version": str(release.get("tag_name") or ""),
                "date": published.strftime("%Y-%m-%d"),
                "summary": _release_summary(body),
                "matched_keywords": matched,
                "relevance_flag": "high" if len(matched) >= 2 else "medium",
            }
        )
    return changes, in_window


def _gh_api(endpoint: str) -> dict | list:
    """Query the GitHub API through the authenticated ``gh`` CLI.

    Used as a fallback for private/renamed repos when plain HTTP fails (404/403).
    Runs ``gh api <endpoint>`` and parses its JSON output; any failure raises
    :class:`SourceError`.
    """
    try:
        proc = subprocess.run(
            ["gh", "api", endpoint, "--paginate"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # binary absent / hang
        raise SourceError(f"gh api {endpoint}: {exc}") from exc
    if proc.returncode != 0:
        raise SourceError(f"gh api {endpoint}: {(proc.stderr or '').strip()[:180]}")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise SourceError(f"gh api {endpoint}: sortie JSON invalide") from exc


#: max items emitted per list/web source per run (anti-explosion, v5.30).
WATCH_ITEMS_CAP = 50


def _extract_markdown_links(text: str) -> list[tuple[str, str]]:
    """[(titre, url)] des liens markdown `[titre](https://...)` (listes type awesome-*)."""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text or ""):
        title = re.sub(r"[*_`]", "", m.group(1)).strip()
        url = m.group(2).rstrip(".,;")
        if title and url.startswith("http"):
            out.append((title[:120], url))
    return out


def _link_category(url: str) -> str:
    """Heuristique de catégorie pour un lien de veille (défaut plugin)."""
    low = url.lower()
    if "mcp" in low:
        return "mcp-server"
    if "skill" in low:
        return "skill"
    if "agent" in low:
        return "agent"
    return "plugin"


def _snapshot_path(state_dir: Path, key: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{key}.txt"


def _load_snapshot(path: Path) -> set[str]:
    try:
        return {
            line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        }
    except OSError:
        return set()


def _save_snapshot(path: Path, urls: set[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(sorted(urls)), encoding="utf-8")
    tmp.replace(path)


def _fetch_watch_list(client, repo: str, end: datetime, state_dir: Path) -> list[dict]:
    """README diff d'un repo de curation (type list, v5.30).

    Les nouveautés = liens markdown présents dans le README actuel et absents du
    snapshot précédent. Premier run = baseline silencieuse (snapshot initialisé,
    zéro item) pour ne pas inonder le rapport de la liste complète.
    """
    owner, name = _split_repo(repo)
    payload = _github_json(client, f"https://api.github.com/repos/{owner}/{name}/readme")
    content = payload.get("content") if isinstance(payload, dict) else None
    if not content:
        return []
    try:
        readme = base64.b64decode(content).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return []
    links = _extract_markdown_links(readme)
    urls_now = {url for _t, url in links}
    path = _snapshot_path(state_dir, f"list-{owner}-{name}")
    previous = _load_snapshot(path)
    _save_snapshot(path, urls_now)
    if not previous:  # baseline : premier run silencieux
        return []
    fresh = [(t, u) for t, u in links if u not in previous][:WATCH_ITEMS_CAP]
    return [
        {
            "name": title or url,
            "category": _link_category(url),
            "repo_url": url,
            "npm_package": None,
            "description": f"Nouveau lien dans la liste {repo}",
            "published_at": end,
            "found_via": [f"watch:list:{repo}"],
            "new_repo": False,
        }
        for title, url in fresh
    ]


def _rss_date(value: str | None):
    """Date d'un flux RSS/Atom : RFC822 (pubDate) ou ISO (updated/published)."""
    if not value:
        return None
    parsed = parse_iso_ts(value)
    if parsed is not None:
        return parsed
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _fetch_rss(client, url: str, start: datetime, end: datetime) -> list[dict]:
    """Flux RSS/Atom (type rss, v5.30) : items datés dans la fenêtre.

    Parse défensif stdlib (ElementTree) : Atom (entry/updated) et RSS 2.0
    (item/pubDate). Les items sans date sont ignorés (pas de datation fiable).
    """
    try:
        resp = client.get(url, timeout=15)
    except Exception as exc:  # noqa: BLE001
        raise SourceError(f"rss {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise SourceError(f"rss {url}: HTTP {resp.status_code}")
    try:
        root = ET.fromstring(resp.text or "")
    except ET.ParseError as exc:
        raise SourceError(f"rss {url}: XML invalide") from exc

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    items: list[dict] = []
    for node in root.iter():
        if _local(node.tag) not in ("entry", "item"):
            continue
        title = link = published = None
        for child in node.iter():
            name = _local(child.tag)
            if name == "title" and title is None:
                title = " ".join((child.text or "").split())[:160]
            elif name == "link" and link is None:
                link = (child.get("href") or (child.text or "")).strip()
            elif name in ("updated", "published", "pubDate", "date") and published is None:
                published = _rss_date((child.text or "").strip())
        if published is not None and start <= published <= end and title and link:
            items.append(
                {
                    "name": title,
                    "category": "article",
                    "repo_url": link,
                    "npm_package": None,
                    "description": f"Article publié le {published:%Y-%m-%d}",
                    "published_at": published,
                    "found_via": [f"rss:{url}"],
                    "new_repo": False,
                }
            )
    return items


# ---------------------------------------------------------------- watch ------
def _split_repo(repo: str) -> tuple[str, str]:
    """`owner/name` → (owner, name); case kept for display, slugified for the URL."""
    parts = str(repo or "").strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"watch_repos doit être 'owner/name', reçu: {repo!r}")

    return quote(parts[0]), quote(parts[1])


def _fetch_watch_repos(
    client, watch_repos: list[str], start: datetime, end: datetime
) -> list[dict]:
    """Arbitrary-repo watch: per-repo latest release(s) in window, else last push.

    A repo with zero in-window activity emits nothing (veille = ce qui bouge cette
    semaine). If *every* repo fails, a :class:`SourceError` is raised so the caller
    records one warning instead of silently succeeding.
    """
    items: list[dict] = []
    if not watch_repos:
        return items
    failures = 0
    for repo in watch_repos:
        try:
            owner, name = _split_repo(repo)
        except ValueError:
            failures += 1
            continue
        try:
            info = _github_json(client, f"https://api.github.com/repos/{owner}/{name}")
            releases = _github_json(
                client,
                f"https://api.github.com/repos/{owner}/{name}/releases",
                params={"per_page": 10},
            )
        except SourceError:
            failures += 1
            continue
        full_name = str(info.get("full_name") or repo) if isinstance(info, dict) else repo
        display_name = full_name if "/" in full_name else repo
        description = str(info.get("description") or "") if isinstance(info, dict) else ""
        if display_name.lower() != repo.lower():
            if description:
                description = f"Renommé de {repo} ; {description}"
            else:
                description = f"Renommé de {repo}"
        html_url = (
            str(info.get("html_url") or f"https://github.com/{full_name}")
            if isinstance(info, dict)
            else f"https://github.com/{full_name}"
        )
        emitted = False
        for release in releases if isinstance(releases, list) else []:
            if not isinstance(release, dict):
                continue
            published = parse_iso_ts(release.get("published_at"))
            if published is None or not (start <= published <= end):
                continue
            tag = str(release.get("tag_name") or release.get("name") or "")
            emitted = True
            items.append(
                {
                    "name": f"{display_name} {tag}" if tag else f"{display_name} (release)",
                    "category": "repo",
                    "repo_url": html_url,
                    "npm_package": None,
                    "description": _release_summary(str(release.get("body") or "")) or description,
                    "published_at": published,
                    "found_via": [SOURCE_WATCH],
                    "new_repo": False,
                }
            )
        if not emitted:
            publish = parse_iso_ts(info.get("pushed_at")) if isinstance(info, dict) else None
            if publish is not None and start <= publish <= end:
                items.append(
                    {
                        "name": display_name,
                        "category": "repo",
                        "repo_url": html_url,
                        "npm_package": None,
                        "description": (
                            f"Activité du dépôt (dernier push {publish:%Y-%m-%d}) — {description}"
                        )[:200],
                        "published_at": publish,
                        "found_via": [SOURCE_WATCH],
                        "new_repo": False,
                    }
                )
            else:
                # v5.30 (3) : le dernier push peut être post-clôture alors que le repo a
                # travaillé DANS la fenêtre — fallback sur les commits de la fenêtre.
                try:
                    commits = _github_json(
                        client,
                        f"https://api.github.com/repos/{owner}/{name}/commits",
                        params={"since": _iso(start), "until": _iso(end), "per_page": 5},
                    )
                except SourceError:
                    commits = []
                in_window = [
                    c
                    for c in commits
                    if isinstance(c, dict)
                    and parse_iso_ts(((c.get("commit") or {}).get("author") or {}).get("date"))
                    is not None
                ]
                if in_window:
                    latest = max(
                        in_window,
                        key=lambda c: parse_iso_ts(c["commit"]["author"]["date"]),
                    )
                    last_commit = parse_iso_ts(latest["commit"]["author"]["date"])
                    items.append(
                        {
                            "name": display_name,
                            "category": "repo",
                            "repo_url": html_url,
                            "npm_package": None,
                            "description": (
                                f"Activité du dépôt ({len(in_window)} commit(s) dans la fenêtre, "
                                f"dernier le {last_commit:%Y-%m-%d}) — {description}"
                            )[:200],
                            "published_at": last_commit,
                            "found_via": [SOURCE_WATCH],
                            "new_repo": False,
                        }
                    )
    if failures and failures == len(watch_repos):
        raise SourceError("github:watch-repos — tous les repos suivis ont échoué (API GitHub)")
    return items


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
        repos = [w["name"] for w in watch_entries if w.get("type", "repo") == "repo"]
        lists = [w["name"] for w in watch_entries if w.get("type") == "list"]
        topics = [w["name"] for w in watch_entries if w.get("type") == "topic"]
        rss_urls = [w["name"] for w in watch_entries if w.get("type") == "rss"]
        state_dir = cfg.output_dir / "watch-state"
        if repos:
            run_source(
                SOURCE_WATCH, lambda: _fetch_watch_repos(client, repos, start, end), sink_item
            )
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

    try:
        changes, in_window_count = _fetch_releases(client, start, end, cfg.release_keywords)
        ok_sources += 1
        counts_by_source[SOURCE_RELEASES] += in_window_count
    except Exception as exc:  # noqa: BLE001 - one failing source never kills the run
        warnings.append({"source": SOURCE_RELEASES, "message": _fail_message(exc)})

    new_items = _finalize_items(items)
    core_changes = sorted(
        changes, key=lambda c: (-datetime.fromisoformat(c["date"]).timestamp(), c["version"])
    )
    counts_by_category = {"plugin": 0, "skill": 0, "agent": 0, "mcp-server": 0, "repo": 0}
    for item in new_items:
        cat = item.get("category")
        if cat in counts_by_category:
            counts_by_category[cat] += 1

    ecosystem = {
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
    return ecosystem, 0 if ok_sources >= 1 else 1


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

    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=15)
    try:
        return _collect(cfg, client, start, run_time)
    finally:
        if own_client:
            client.close()
