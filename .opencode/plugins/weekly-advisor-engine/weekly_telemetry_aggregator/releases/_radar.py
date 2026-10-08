"""Radars MCP du watch `releases` : résolution d'URL, JSON-RPC, digest markdown.

Possède `MCP_PROTOCOL_VERSION`, `_RADAR_HEADERS` et `_RADAR_DATE_RE` : les seuls
consommateurs sont les fonctions radar ci-dessous.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__
from ._http import SourceError
from ._sources import WATCH_ITEMS_CAP, _extract_markdown_links, _fetch_rss

# ---------------------------------------------------------------- radar ------
#: Version de protocole MCP annoncée à l'initialize (streamable-http).
MCP_PROTOCOL_VERSION = "2025-06-18"

_RADAR_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

_RADAR_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")


def _radar_mcp_url(project_root: Path | None, name: str) -> str:
    """URL du serveur MCP ``name`` lue dans ``<project_root>/opencode.json``.

    Le fichier opencode.json du kit est la source unique de vérité : aucune URL
    de radar n'est codée en dur. Lève :class:`SourceError` avec un message nommant
    la clé manquante quand la déclaration est absente ou mal formée.
    """
    if project_root is None:
        raise SourceError(f"radar {name}: project_root non défini — opencode.json introuvable")
    path = Path(project_root) / "opencode.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SourceError(f"radar {name}: {path} illisible ({exc})") from exc
    except ValueError as exc:  # inclut json.JSONDecodeError
        raise SourceError(f"radar {name}: {path} JSON invalide ({exc})") from exc
    url = ""
    if isinstance(raw, dict):
        servers = raw.get("mcp")
        if isinstance(servers, dict):
            conf = servers.get(name)
            if isinstance(conf, dict) and isinstance(conf.get("url"), str):
                url = conf["url"]
    if not url.startswith("http"):
        raise SourceError(
            f"radar {name}: clé mcp.{name}.url absente de opencode.json — "
            "déclarer le serveur MCP dans la configuration du kit"
        )
    return url


def _decode_mcp_body(text: str) -> dict:
    """Corps de réponse JSON-RPC : JSON brut ou flux SSE (dernière ligne ``data:``)."""
    body = (text or "").strip()
    if body.startswith(("event:", "data:", "id:", "retry:")):
        chunks = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        if not chunks:
            raise ValueError("flux SSE sans ligne data:")
        body = chunks[-1]
    payload = json.loads(body or "{}")
    if not isinstance(payload, dict):
        raise ValueError("réponse JSON-RPC non objet")
    return payload


def _radar_post(client, url: str, payload: dict, *, session: str | None) -> tuple[dict, str | None]:
    """POST JSON-RPC streamable-http ; renvoie (réponse décodée, mcp-session-id reçu).

    Toute erreur (transport, HTTP ≥ 400, corps illisible, ``error`` JSON-RPC)
    devient :class:`SourceError` — l'appelant bascule alors sur le repli RSS.
    """
    headers = dict(_RADAR_HEADERS)
    if session:
        headers["mcp-session-id"] = session
    try:
        resp = client.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001 - transport → repli RSS
        raise SourceError(f"{url}: {exc}") from exc
    if resp.status_code >= 400:
        raise SourceError(f"{url}: HTTP {resp.status_code}")
    try:
        decoded = _decode_mcp_body(resp.text or "")
    except ValueError as exc:
        raise SourceError(f"{url}: réponse illisible ({exc})") from exc
    rpc_error = decoded.get("error")
    if isinstance(rpc_error, dict):
        raise SourceError(f"{url}: erreur JSON-RPC {rpc_error.get('message') or rpc_error}")
    session_out = resp.headers.get("mcp-session-id")
    return decoded, session_out


def _radar_markdown(client, entry: Mapping, url: str) -> str:
    """Poignée MCP : ``initialize`` puis ``tools/call`` → texte markdown agrégé."""
    name = str(entry.get("name") or "")
    _init, session = _radar_post(
        client,
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "weekly-advisor-engine", "version": __version__},
            },
        },
        session=None,
    )
    call, _session2 = _radar_post(
        client,
        url,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": str(entry.get("tool") or ""), "arguments": {}},
        },
        session=session,
    )
    result = call.get("result")
    if not isinstance(result, dict):
        raise SourceError(f"radar {name}: réponse tools/call sans résultat")
    content = result.get("content")
    parts = (
        [part.get("text") for part in content if isinstance(part, dict)]
        if isinstance(content, list)
        else []
    )
    text = "\n".join(part for part in parts if isinstance(part, str))
    if not text.strip():
        raise SourceError(f"radar {name}: contenu vide")
    return text


def _fetch_radar(
    client,
    entry: Mapping,
    start: datetime,
    end: datetime,
    *,
    project_root: Path | None,
) -> list[dict]:
    """Radar MCP (type radar) : liens datés du digest, repli RSS configuré.

    1. URL résolue depuis ``<project_root>/opencode.json`` (``mcp[name].url``).
    2. JSON-RPC brut : POST ``initialize`` puis POST ``tools/call``, session
       reprise du header ``mcp-session-id``.
    3. Liens markdown ``[titre](url)`` ; une date ISO sur la ligne fait foi
       (``published_at``), les lignes non datées sont exclues — le digest
       quotidien du radar retombe donc toujours dans la fenêtre hebdomadaire.
    4. Échec MCP → repli ``rss_fallback`` via :func:`_fetch_rss` ; les deux
       morts lèvent :class:`SourceError` (warning par source, run inchangé).
    """
    name = str(entry.get("name") or "")
    fallback = str(entry.get("rss_fallback") or "")
    # Résolution AVANT le try : une déclaration absente est une erreur de config
    # (message clair immédiat), pas une panne MCP justifiant le repli RSS.
    url = _radar_mcp_url(project_root, name)
    try:
        text = _radar_markdown(client, entry, url)
    except Exception as exc:  # noqa: BLE001 - tout échec MCP bascule sur le RSS
        if not fallback:
            raise SourceError(
                f"radar {name}: MCP indisponible et aucun rss_fallback ({exc})"
            ) from exc
        return _fetch_rss(client, fallback, start, end)

    items: list[dict] = []
    for line in text.splitlines():
        links = _extract_markdown_links(line)
        if not links:
            continue
        stamp = _RADAR_DATE_RE.search(line)
        if stamp is None:
            continue  # non daté → exclu (pas de datation fiable)
        published = datetime(int(stamp[1]), int(stamp[2]), int(stamp[3]), tzinfo=UTC)
        if not (start <= published <= end):
            continue
        for title, url in links:
            items.append(
                {
                    "name": title,
                    "category": "repo",
                    "repo_url": url,
                    "npm_package": None,
                    "description": f"Lien partagé par le radar {name} le {published:%Y-%m-%d}",
                    "published_at": published,
                    "found_via": ["radar"],
                    "new_repo": False,
                }
            )
    return items[:WATCH_ITEMS_CAP]
