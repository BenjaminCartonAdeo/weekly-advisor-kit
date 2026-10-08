"""Sources watch : github topics, MCP registry, releases OpenCode, listes, RSS/Atom.

Possède les URLs de source (`URL_GITHUB_TOPICS`, `URL_MCP`, `URL_RELEASES`),
`RELEASES_PER_PAGE` et `WATCH_ITEMS_CAP` : les seuls consommateurs sont les
fetchers ci-dessous, plus `_radar` et `_watch_repos` pour le plafond d'items.
Aucun module en amont n'importe ce module — les constantes y sont donc définies,
conformément à la règle d'acyclicité.
"""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

from ..util import iso as _iso
from ..util import parse_iso_ts
from ._http import SourceError, _get_json, _github_json
from ._npm import _mcp_map_entry


def _rss_local(tag: str) -> str:
    """Local XML name without namespace."""
    return tag.rsplit("}", 1)[-1]


def _rss_map_node(node, url: str, start: datetime, end: datetime) -> dict | None:
    """One RSS/Atom entry/item node → article, or None when dateless/out-of-window."""
    if _rss_local(node.tag) not in ("entry", "item"):
        return None
    title = link = published = None
    for child in node.iter():
        name = _rss_local(child.tag)
        if name == "title" and title is None:
            title = " ".join((child.text or "").split())[:160]
        elif name == "link" and link is None:
            link = (child.get("href") or (child.text or "")).strip()
        elif name in ("updated", "published", "pubDate", "date") and published is None:
            published = _rss_date((child.text or "").strip())
    if published is not None and start <= published <= end and title and link:
        return {
            "name": title,
            "category": "article",
            "repo_url": link,
            "npm_package": None,
            "description": f"Article publié le {published:%Y-%m-%d}",
            "published_at": published,
            "found_via": [f"rss:{url}"],
            "new_repo": False,
        }
    return None


def _extract_markdown_links(text: str) -> list[tuple[str, str]]:
    """[(titre, url)] des liens markdown `[titre](https://...)` (listes type awesome-*)."""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text or ""):
        title = re.sub(r"[*_`]", "", m.group(1)).strip()
        url = m.group(2).rstrip(".,;")
        if title and url.startswith("http"):
            out.append((title[:120], url))
    return out


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

    items: list[dict] = []
    for node in root.iter():
        mapped = _rss_map_node(node, url, start, end)
        if mapped is not None:
            items.append(mapped)
    return items


#: max items emitted per list/web source per run (anti-explosion, v5.30).
WATCH_ITEMS_CAP = 50

RELEASES_PER_PAGE = 100

# URLs (one per source, resolved per run).
URL_GITHUB_TOPICS = "https://api.github.com/search/repositories"
URL_MCP = "https://registry.modelcontextprotocol.io/v0.1/servers"
URL_RELEASES = "https://api.github.com/repos/anomalyco/opencode/releases"


def _fetch_github_topics(
    client, topic: str, start: datetime, end: datetime, min_stars: int
) -> list[dict]:
    """GitHub topic search (type topic, v5.30) — API datée (pushed_at/created_at).

    Une seule implémentation pour tous les topics (y compris opencode-plugin,
    le topic historique de la Partie 2). Query pré-construite : le + serait
    encodé en %2B par urlencode/quote et GitHub le chercherait comme terme littéral.
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
        mapped = _mcp_map_entry(entry, start, end)
        if mapped is not None:
            items.append(mapped)
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


def _split_repo(repo: str) -> tuple[str, str]:
    """`owner/name` → (owner, name); case kept for display, slugified for the URL."""
    parts = str(repo or "").strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"watch_repos doit être 'owner/name', reçu: {repo!r}")

    return quote(parts[0]), quote(parts[1])
