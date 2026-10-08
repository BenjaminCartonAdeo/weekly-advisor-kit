"""Repos suivis (`cfg.watch_repos`) : releases in-window, repli push/commits.

Feuille du paquet : elle consomme `_release_summary` et `_split_repo` depuis
`_sources`, et rien d'autre — aucune constante n'y est donc définie.
"""

from __future__ import annotations

from datetime import datetime

from ..util import iso as _iso
from ..util import parse_iso_ts
from ._http import SourceError, _github_json
from ._sources import _release_summary, _split_repo
from .dedup import SOURCE_WATCH


def _watch_repo_release_items(
    display_name: str,
    html_url: str,
    description: str,
    releases: object,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Items release in-window d'un repo suivi."""
    items: list[dict] = []
    for release in releases if isinstance(releases, list) else []:
        if not isinstance(release, dict):
            continue
        published = parse_iso_ts(release.get("published_at"))
        if published is None or not (start <= published <= end):
            continue
        tag = str(release.get("tag_name") or release.get("name") or "")
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
    return items


def _watch_repo_activity_fallback(
    client,
    owner: str,
    name: str,
    display_name: str,
    html_url: str,
    description: str,
    info: object,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Repli push/commits quand aucune release in-window — [] si rien ne bouge."""
    publish = parse_iso_ts(info.get("pushed_at")) if isinstance(info, dict) else None
    if publish is not None and start <= publish <= end:
        return [
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
        ]
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
        and parse_iso_ts(((c.get("commit") or {}).get("author") or {}).get("date")) is not None
    ]
    if not in_window:
        return []
    latest = max(
        in_window,
        key=lambda c: parse_iso_ts(c["commit"]["author"]["date"]),
    )
    last_commit = parse_iso_ts(latest["commit"]["author"]["date"])
    return [
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
    ]


def _watch_repo_display_fields(repo: str, info: object) -> tuple[str, str, str]:
    """display_name/description/html_url d'un repo suivi (+ mention rename)."""
    full_name = str(info.get("full_name") or repo) if isinstance(info, dict) else repo
    display_name = full_name if "/" in full_name else repo
    description = str(info.get("description") or "") if isinstance(info, dict) else ""
    if display_name.lower() != repo.lower():
        rename = f"Renommé de {repo}"
        description = f"{rename} ; {description}" if description else rename
    html_url = (
        str(info.get("html_url") or f"https://github.com/{full_name}")
        if isinstance(info, dict)
        else f"https://github.com/{full_name}"
    )
    return display_name, description, html_url


def _process_watch_repo(
    client, repo: str, start: datetime, end: datetime
) -> tuple[list[dict], bool]:
    """Un repo suivi → (items, ok). False = repo en échec (split ou API)."""
    try:
        owner, name = _split_repo(repo)
    except ValueError:
        return [], False
    try:
        info = _github_json(client, f"https://api.github.com/repos/{owner}/{name}")
        releases = _github_json(
            client,
            f"https://api.github.com/repos/{owner}/{name}/releases",
            params={"per_page": 10},
        )
    except SourceError:
        return [], False
    display_name, description, html_url = _watch_repo_display_fields(repo, info)
    repo_items = _watch_repo_release_items(
        display_name, html_url, description, releases, start, end
    )
    if not repo_items:
        # Aucune release émise pour ce repo → repli activité.
        repo_items = _watch_repo_activity_fallback(
            client, owner, name, display_name, html_url, description, info, start, end
        )
    return repo_items, True


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
        repo_items, ok = _process_watch_repo(client, repo, start, end)
        if not ok:
            failures += 1
            continue
        items.extend(repo_items)
    if failures and failures == len(watch_repos):
        raise SourceError("github:watch-repos — tous les repos suivis ont échoué (API GitHub)")
    return items
