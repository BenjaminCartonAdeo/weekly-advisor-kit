"""Mapping npm search + MCP registry du watch `releases`.

Feuille du graphe d'imports du paquet : ni `_http` ni `dedup` n'en dépendent.
Possède les constantes npm (`NPM_*`, `URL_NPM`) — ses seuls consommateurs sont
les fonctions de mapping/fetch ci-dessous.
"""

from __future__ import annotations

from datetime import datetime

from ..util import parse_iso_ts
from ._http import _get_json
from .dedup import SOURCE_MCP, SOURCE_NPM

#: npm search pagination (registry caps size at 250 rows per page).
NPM_PAGE_SIZE = 250
NPM_MAX_ROWS = 1000
NPM_QUERY = "keywords:opencode-plugin,opencode"

# URL (resolved per run).
URL_NPM = "https://registry.npmjs.org/-/v1/search"


# ---------------------------------------------------------------- npm -------
def _npm_category(package: dict) -> str:
    """Heuristic: "skill" when the name or keywords suggest a skill, else "plugin"."""
    name = str(package.get("name") or "").lower()
    keywords = package.get("keywords")
    joined = " ".join(str(k).lower() for k in keywords) if isinstance(keywords, list) else ""
    if "skill" in name or "skill" in joined:
        return "skill"
    return "plugin"


def _npm_fetch_pages(client) -> list:
    """npm search pages, up to NPM_MAX_ROWS when `total > 250`."""
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
    return objects


def _npm_map_object(obj, start: datetime, end: datetime) -> dict | None:
    """One npm search object → item, or None when malformed/out-of-window."""
    package = obj.get("package") if isinstance(obj, dict) else None
    if not isinstance(package, dict):
        return None
    published = parse_iso_ts(package.get("date"))
    if published is None or not (start <= published <= end):
        return None
    links = package.get("links")
    repo_url = links.get("repository") if isinstance(links, dict) else ""
    name = str(package.get("name") or "")
    return {
        "name": name,
        "category": _npm_category(package),
        "repo_url": str(repo_url or "") if repo_url else "",
        "npm_package": name or None,
        "description": str(package.get("description") or ""),
        "published_at": published,
        "found_via": [SOURCE_NPM],
        "new_repo": False,
    }


def _mcp_map_entry(entry, start: datetime, end: datetime) -> dict | None:
    """One registry server entry → item, or None when filtered out."""
    if not isinstance(entry, dict):
        return None
    outer_meta = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else {}
    official = outer_meta.get("io.modelcontextprotocol.registry/official")
    if not isinstance(official, dict):
        official = {}
    # Defensive guard: skip non-latest revisions of an official server.
    if official.get("isLatest") is False:
        return None
    inner = entry.get("server") if isinstance(entry.get("server"), dict) else entry
    if not isinstance(inner, dict):
        return None
    status = str(inner.get("status") or official.get("status") or "")
    if status == "deleted":
        return None
    published = parse_iso_ts(official.get("publishedAt") or inner.get("publishedAt"))
    if published is None or not (start <= published <= end):
        return None
    return {
        "name": str(inner.get("name") or inner.get("title") or ""),
        "category": "mcp-server",
        "repo_url": _mcp_repo_url(inner),
        "npm_package": None,
        "description": str(inner.get("description") or ""),
        "published_at": published,
        "found_via": [SOURCE_MCP],
        "new_repo": False,
    }


def _fetch_npm(client, start: datetime, end: datetime) -> list[dict]:
    """npm search, paginated up to NPM_MAX_ROWS when `total > 250`."""
    items: list[dict] = []
    for obj in _npm_fetch_pages(client):
        mapped = _npm_map_object(obj, start, end)
        if mapped is not None:
            items.append(mapped)
    return items


# ---------------------------------------------------------------- github -----


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
