"""Transport bas niveau du watch `releases` : client stdlib, retry/backoff, fallback gh.

Aucun import depuis le reste du paquet — ce module est une feuille de l'arbre
d'imports (les constantes retry y sont définies, jamais dans `__init__`).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from json import dumps as _dumps
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

#: Retry/backoff — total attempts, backoff between attempts (spec: 1s, 2s).
#: Module-level constant so tests can shorten it without hitting the network.
_RETRIES = 3
_BACKOFF: tuple[float, ...] = (1.0, 2.0)


class SourceError(Exception):
    """One watch source ultimately failed; the run continues (warning, non-fatal)."""


class _Response:
    """Réponse HTTP minimale : status, corps texte, headers (interface stable)."""

    def __init__(self, status: int, body: bytes, headers):
        self.status_code = status
        self.text = body.decode("utf-8", errors="replace")
        self.headers = headers

    def json(self):
        return json.loads(self.text)


class _HttpClient:
    """Client HTTP synchrone minimal sur stdlib urllib — zéro dépendance réseau.

    Couvre exactement la surface du watch : GET JSON paginé (params encodés
    en UTF-8 : espace ``%20``, ``+`` en ``%2B``), GET RSS, POST JSON-RPC
    MCP avec session header. Les statuts 4xx/5xx sont retournés en réponse
    (pas d'exception) — la politique retry/échec appartient aux appelants.
    """

    def __init__(self, timeout: int = 15) -> None:
        self._timeout = timeout

    def _open(self, url: str, data: bytes | None, headers: dict | None, timeout: int) -> _Response:
        request = Request(url, data=data, headers=headers or {})
        try:
            with urlopen(request, timeout=timeout) as resp:
                return _Response(resp.status, resp.read(), resp.headers)
        except HTTPError as exc:  # 4xx/5xx : le corps reste lisible
            return _Response(exc.code, exc.read(), exc.headers)

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: int | None = None,
    ) -> _Response:
        if params:
            url = f"{url}?{urlencode(params, quote_via=quote)}"
        return self._open(url, None, headers, timeout if timeout is not None else self._timeout)

    def post(self, url: str, *, json: dict | None = None, headers: dict | None = None) -> _Response:
        data = None if json is None else _dumps(json)
        return self._open(url, data, headers, self._timeout)


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
            # timeout réseau hérité du client (_HttpClient(timeout=15)) — borne
            # explicite C9 (v6.0.p), jamais désactivée.
            resp = client.get(url, params=params, headers=headers)
        except (URLError, OSError, TimeoutError) as exc:
            # réseau : DNS, connexion refusée/reset, timeout → retry.
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
            encoding="utf-8",
            errors="replace",
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
