"""Primitives partagées des sources watch : liens markdown, RSS/Atom, plafond items.

Créé à l'extraction du radar (`_radar` consomme `_fetch_rss`,
`_extract_markdown_links` et `WATCH_ITEMS_CAP` ; les laisser dans `__init__`
imposerait au sous-module d'importer son propre parent). Complété en F5 avec les
fetchers github topics / mcp / releases / watch-list. `WATCH_ITEMS_CAP` est
défini ici parce que ce module en est le seul propriétaire à venir.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

from ..util import parse_iso_ts
from ._http import SourceError

#: max items emitted per list/web source per run (anti-explosion, v5.30).
WATCH_ITEMS_CAP = 50


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
