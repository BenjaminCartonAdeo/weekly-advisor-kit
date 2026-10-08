"""Releases/ecosystem watch (Partie 2 `releases`) with a FakeClient — no network."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from weekly_telemetry_aggregator import releases
from weekly_telemetry_aggregator.config import TelemetryConfig, load_config

ANCHOR_ISO = "2026-08-10T06:00:00Z"
PERIOD_START = datetime(2026, 8, 3, 6, 0, 0, tzinfo=UTC)
PERIOD_END = datetime(2026, 8, 10, 6, 0, 0, tzinfo=UTC)

URL_NPM = "https://registry.npmjs.org/-/v1/search"
URL_GITHUB = "https://api.github.com/search/repositories"
URL_MCP = "https://registry.modelcontextprotocol.io/v0.1/servers"
URL_RELEASES = "https://api.github.com/repos/anomalyco/opencode/releases"

S_NPM = "npm:keywords:opencode-plugin"
S_GITHUB = "github:topic:opencode-plugin"
S_MCP = "mcp-registry"
S_RELEASES = "github:releases:opencode"


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class FakeResponse:
    def __init__(
        self, payload, status: int = 200, text: str | None = None, headers: dict | None = None
    ):
        self.payload = payload
        self.status_code = status
        self._text = text
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self._text if self._text is not None else json.dumps(self.payload)

    def json(self):
        return json.loads(self._text) if self._text is not None else self.payload


class FakeClient:
    """Drop-in client-shaped fake: canned handler responses, dials recorded."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        timeout=None,
        **kwargs,
    ):
        self.calls.append((url, params, headers))
        return self.handler(url, params, headers)

    def close(self):
        pass


def make_cfg(**kw) -> TelemetryConfig:
    base = dict(
        project_root=None,
        lookback_days=7,
        release_keywords=["skill", "cache", "context", "compaction"],
        github_min_stars=5,
    )
    base.update(kw)
    return TelemetryConfig(**base)


def empty_payload():
    return {"empty": True}


def make_handler(url_payloads: dict):
    """Map url → FakeResponse or exception; unknown urls fail hard.

    v5.30 : les URLs GitHub search portent désormais leur query (fix %2B) —
    le match se fait par préfixe (clés les plus longues d'abord) pour couvrir
    les appels avec query tout en gardant la précision.
    """

    def handler(url, params, headers):
        for key in sorted(url_payloads, key=len, reverse=True):
            if url.startswith(key):
                inner = url_payloads[key]
                if isinstance(inner, Exception):
                    raise inner
                if isinstance(inner, FakeResponse):
                    return inner
                return FakeResponse(inner)
        raise AssertionError(f"unexpected url: {url}")

    return handler


# -----------------------------------------------------------------------------
def npm_payload(*packages):
    return {"total": len(packages), "objects": [{"package": p} for p in packages]}


def test_happy_path_all_sources(monkeypatch):
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    handler = make_handler(
        {
            URL_NPM: npm_payload(
                {
                    "name": "oc-skill-pack",
                    "description": "skill bundle",
                    "date": "2026-08-05T10:00:00Z",
                    "keywords": ["opencode", "skill"],
                    "links": {"repository": "https://github.com/acme/oc-skill-pack"},
                },
                {
                    "name": "old-npm",
                    "description": "old",
                    "date": "2026-01-01T00:00:00Z",
                    "keywords": ["opencode-plugin"],
                    "links": {"repository": "https://github.com/acme/old-npm"},
                },
            ),
            URL_GITHUB: {
                "items": [
                    {
                        "full_name": "acme/gh-plugin",
                        "html_url": "https://github.com/acme/gh-plugin",
                        "description": "a plugin",
                        "created_at": "2026-08-01T00:00:00Z",
                        "pushed_at": "2026-08-06T09:00:00Z",
                    },
                    {
                        "full_name": "acme/gh-old",
                        "html_url": "https://github.com/acme/gh-old",
                        "description": "x",
                        "created_at": "2025-01-01T00:00:00Z",
                        "pushed_at": "2026-01-02T00:00:00Z",
                    },
                ]
            },
            URL_MCP: {
                "servers": [
                    {
                        "server": {
                            "name": "acme/mcp",
                            "title": "Acme MCP",
                            "description": "mcp thing",
                            "repository": "https://github.com/acme/mcp-server",
                        },
                        "_meta": {
                            "io.modelcontextprotocol.registry/official": {
                                "status": "active",
                                "isLatest": True,
                                "publishedAt": "2026-08-04T12:00:00Z",
                            }
                        },
                    },
                    {
                        "server": {
                            "name": "acme/old-mcp",
                            "title": "Old",
                            "description": "x",
                            "repository": "",
                        },
                        "_meta": {
                            "io.modelcontextprotocol.registry/official": {
                                "status": "active",
                                "isLatest": True,
                                "publishedAt": "2026-01-01T00:00:00Z",
                            }
                        },
                    },
                    {
                        "server": {"name": "acme/deleted", "title": "Del", "description": "x"},
                        "_meta": {
                            "io.modelcontextprotocol.registry/official": {
                                "status": "deleted",
                                "isLatest": True,
                                "publishedAt": "2026-08-05T00:00:00Z",
                            }
                        },
                    },
                ]
            },
            URL_RELEASES: [
                {
                    "tag_name": "v1.18.14",
                    "name": "Release 14",
                    "published_at": "2026-08-08T00:00:00Z",
                    "body": "improve skill loading\ncache invalidation fix\n",
                },
                {
                    "tag_name": "v1.18.13",
                    "name": "Release 13",
                    "published_at": "2026-08-07T00:00:00Z",
                    "body": "fixes typo in context window sizing\n",
                },
                {
                    "tag_name": "v1.18.12",
                    "name": "Release 12",
                    "published_at": "2026-08-06T00:00:00Z",
                    "body": "nothing relevant\n",
                },
            ],
        }
    )
    client = FakeClient(handler)
    data, exit_code = releases.run(make_cfg(), anchor=ANCHOR_ISO, client=client)

    assert exit_code == 0
    assert data["schema_version"] == 2
    assert data["period"] == {"start": _iso(PERIOD_START), "end": _iso(PERIOD_END)}
    assert data["generated_at"] == _iso(PERIOD_END)
    assert data["warnings"] == []

    # new_items: published_at DESC, then name ASC (windows only).
    names = [i["name"] for i in data["new_items"]]
    assert names == ["acme/gh-plugin", "oc-skill-pack", "acme/mcp"]
    npm_item = next(i for i in data["new_items"] if i["name"] == "oc-skill-pack")
    assert npm_item["category"] == "skill"
    assert npm_item["found_via"] == [S_NPM]
    assert npm_item["new_repo"] is False
    gh_item = next(i for i in data["new_items"] if i["name"] == "acme/gh-plugin")
    assert gh_item["category"] == "plugin"
    assert gh_item["new_repo"] is False  # created 2026-08-01 < period start
    mcp_item = next(i for i in data["new_items"] if i["name"] == "acme/mcp")
    assert mcp_item["category"] == "mcp-server"
    assert mcp_item["repo_url"] == "https://github.com/acme/mcp-server"

    # core_changes: high (skill+cache) then medium (context), 0-keyword omitted.
    assert [c["version"] for c in data["core_changes"]] == ["v1.18.14", "v1.18.13"]
    assert data["core_changes"][0]["relevance_flag"] == "high"
    assert data["core_changes"][0]["matched_keywords"] == ["skill", "cache"]
    assert data["core_changes"][1]["relevance_flag"] == "medium"
    assert data["core_changes"][1]["matched_keywords"] == ["context"]

    assert data["counts_by_category"] == {
        "plugin": 1,
        "skill": 1,
        "agent": 0,
        "mcp-server": 1,
        "repo": 0,
    }
    assert data["counts_by_source"] == {S_NPM: 1, S_GITHUB: 1, S_MCP: 1, S_RELEASES: 3, S_WATCH: 0}


# -----------------------------------------------------------------------------
def test_run_lookback_days_override_widens_sources(monkeypatch):
    """v6.0.b : releases.run(lookback_days=21) élargit la fenêtre.

    Paquet daté 2026-07-25 : hors fenêtre 7 j (start 08-03), inclus dans 21 j
    (start 07-20). La config n'est pas modifiée sur disque.
    """
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    handler = make_handler(
        {
            URL_NPM: npm_payload(
                {
                    "name": "oc-late-pack",
                    "description": "paru dans la fenêtre élargie",
                    "date": "2026-07-25T00:00:00Z",
                    "keywords": ["opencode", "skill"],
                    "links": {"repository": "https://github.com/acme/oc-late-pack"},
                },
            ),
            URL_GITHUB: {"items": []},
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )
    client = FakeClient(handler)
    cfg = make_cfg()
    data, rc = releases.run(cfg, anchor=ANCHOR_ISO, client=client, lookback_days=21)
    assert rc == 0
    assert [i["name"] for i in data["new_items"]] == ["oc-late-pack"]
    assert cfg.lookback_days == 21  # mutation en mémoire seulement
    # contre-preuve : sans override, le même paquet est hors fenêtre 7 j
    data7, rc7 = releases.run(make_cfg(), anchor=ANCHOR_ISO, client=FakeClient(handler))
    assert rc7 == 0
    assert [i["name"] for i in data7["new_items"]] == []


# -----------------------------------------------------------------------------
def test_intra_run_dedup_npm_and_github(monkeypatch):
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    handler = make_handler(
        {
            URL_NPM: npm_payload(
                {
                    "name": "dup",
                    "description": "from npm",
                    "date": "2026-08-05T10:00:00Z",
                    "keywords": ["opencode-plugin"],
                    "links": {"repository": "https://github.com/o/dup"},
                },
            ),
            URL_GITHUB: {
                "items": [
                    {
                        "full_name": "o/dup",
                        "html_url": "https://github.com/o/dup",
                        "description": "",
                        "created_at": "2026-08-01T00:00:00Z",
                        "pushed_at": "2026-08-06T09:00:00Z",
                    },
                ]
            },
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )
    data, exit_code = releases.run(make_cfg(), anchor=ANCHOR_ISO, client=FakeClient(handler))

    assert exit_code == 0
    assert len(data["new_items"]) == 1
    item = data["new_items"][0]
    # same repo_url from npm + github → single item, found_via concatenated canonical order.
    assert item["repo_url"] == "https://github.com/o/dup"
    assert item["name"] == "dup"  # winning (npm) record's name survives the merge
    assert item["found_via"] == [S_NPM, S_GITHUB]
    # most complete record wins the description (npm's non-empty), earliest published_at.
    assert item["description"] == "from npm"
    assert item["published_at"] == "2026-08-05T10:00:00Z"
    assert item["new_repo"] is False
    # counts still increment per source hit (dedup only affects new_items).
    assert data["counts_by_source"][S_NPM] == 1
    assert data["counts_by_source"][S_GITHUB] == 1
    assert data["counts_by_category"]["plugin"] == 1


def test_merge_prefers_github_description_when_npm_empty():
    handler = make_handler(
        {
            URL_NPM: npm_payload(
                {
                    "name": "dup",
                    "description": "",
                    "date": "2026-08-05T10:00:00Z",
                    "keywords": ["opencode-plugin"],
                    "links": {"repository": "https://github.com/o/dup2"},
                },
            ),
            URL_GITHUB: {
                "items": [
                    {
                        "full_name": "o/dup2",
                        "html_url": "https://github.com/o/dup2",
                        "description": "from github",
                        "created_at": "2026-08-01T00:00:00Z",
                        "pushed_at": "2026-08-06T09:00:00Z",
                    },
                ]
            },
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )
    data, _ = releases.run(make_cfg(), anchor=ANCHOR_ISO, client=FakeClient(handler))
    assert len(data["new_items"]) == 1
    assert data["new_items"][0]["description"] == "from github"


# -----------------------------------------------------------------------------
def test_all_sources_failing_exit_1_warnings_filled(monkeypatch):
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(releases._http, "_gh_api", no_gh)

    def handler(url, params, headers):
        if url == URL_NPM:
            return FakeResponse({}, status=500)  # retried (backoff patched)
        if url == URL_GITHUB:
            return FakeResponse({}, status=429)  # retried
        if url == URL_MCP:
            return FakeResponse({}, status=404)  # no retry
        if url == URL_RELEASES:
            raise urllib.error.URLError("connection reset")  # network error, retried
        raise AssertionError(url)

    data, exit_code = releases.run(make_cfg(), anchor=ANCHOR_ISO, client=FakeClient(handler))

    assert exit_code == 1
    assert data["new_items"] == []
    assert data["core_changes"] == []
    assert data["counts_by_source"] == {S_NPM: 0, S_GITHUB: 0, S_MCP: 0, S_RELEASES: 0, S_WATCH: 0}
    assert len(data["warnings"]) == 4
    sources = [w["source"] for w in data["warnings"]]
    assert sources == [S_NPM, S_GITHUB, S_MCP, S_RELEASES]
    for w in data["warnings"]:
        assert "source ignorée pour ce run" in w["message"]


# -----------------------------------------------------------------------------
def test_releases_keyword_relevance_0_1_2(monkeypatch):
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    handler = make_handler(
        {
            URL_NPM: npm_payload(),
            URL_GITHUB: {"items": []},
            URL_MCP: {"servers": []},
            URL_RELEASES: [
                {
                    "tag_name": "v2.0.0",
                    "name": "v2",
                    "published_at": "2026-08-09T00:00:00Z",
                    "body": "skill loading plus cache compaction overhaul\n",
                },
                {
                    "tag_name": "v1.9.9",
                    "name": "v1.9.9",
                    "published_at": "2026-08-08T00:00:00Z",
                    "body": "only a context fix\n",
                },
                {
                    "tag_name": "v1.9.8",
                    "name": "v1.9.8",
                    "published_at": "2026-08-07T00:00:00Z",
                    "body": "housekeeping, no keywords\n",
                },
            ],
        }
    )
    data, exit_code = releases.run(make_cfg(), anchor=ANCHOR_ISO, client=FakeClient(handler))

    assert exit_code == 0
    # 2 matches → high, 1 → medium, 0 → omitted from core_changes but counted.
    assert [c["version"] for c in data["core_changes"]] == ["v2.0.0", "v1.9.9"]
    assert data["core_changes"][0]["relevance_flag"] == "high"
    assert data["core_changes"][1]["relevance_flag"] == "medium"
    assert data["counts_by_source"][S_RELEASES] == 3  # 0-keyword release still counted


# -----------------------------------------------------------------------------
def test_period_filtering_excludes_items_outside_window(monkeypatch):
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    handler = make_handler(
        {
            URL_NPM: npm_payload(
                {
                    "name": "old-npm",
                    "description": "old",
                    "date": "2026-01-01T00:00:00Z",
                    "keywords": ["opencode-plugin"],
                    "links": {"repository": "https://github.com/acme/old-npm"},
                },
            ),
            URL_GITHUB: {
                "items": [
                    {
                        "full_name": "acme/old-gh",
                        "html_url": "https://github.com/acme/old-gh",
                        "description": "x",
                        "created_at": "2025-01-01T00:00:00Z",
                        "pushed_at": "2026-01-02T00:00:00Z",
                    },
                ]
            },
            URL_MCP: {
                "servers": [
                    {
                        "server": {"name": "old-mcp", "description": "x"},
                        "_meta": {
                            "io.modelcontextprotocol.registry/official": {
                                "status": "active",
                                "isLatest": True,
                                "publishedAt": "2026-01-01T00:00:00Z",
                            }
                        },
                    },
                ]
            },
            URL_RELEASES: [
                {
                    "tag_name": "v1.0.0",
                    "name": "old",
                    "published_at": "2026-01-01T00:00:00Z",
                    "body": "skill cache context compaction all",
                },
            ],
        }
    )
    data, exit_code = releases.run(make_cfg(), anchor=ANCHOR_ISO, client=FakeClient(handler))

    assert exit_code == 0  # sources succeeded — zero items still "ok"
    assert data["new_items"] == []
    assert data["core_changes"] == []
    assert data["counts_by_source"] == {S_NPM: 0, S_GITHUB: 0, S_MCP: 0, S_RELEASES: 0, S_WATCH: 0}
    assert data["warnings"] == []


# -----------------------------------------------------------------------------
def test_partial_source_failure_exit_0(monkeypatch):
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(releases._http, "_gh_api", no_gh)

    def handler(url, params, headers):
        if url == URL_NPM:
            return FakeResponse({}, status=500)
        if url == URL_GITHUB:
            raise urllib.error.URLError("connection refused")
        if url == URL_MCP:
            return FakeResponse(
                {
                    "servers": [
                        {
                            "server": {
                                "name": "acme/mcp",
                                "title": "Acme MCP",
                                "description": "mcp thing",
                                "repository": "https://github.com/acme/mcp-server",
                            },
                            "_meta": {
                                "io.modelcontextprotocol.registry/official": {
                                    "status": "active",
                                    "isLatest": True,
                                    "publishedAt": "2026-08-04T12:00:00Z",
                                }
                            },
                        },
                    ]
                }
            )
        if url == URL_RELEASES:
            return FakeResponse(
                [
                    {
                        "tag_name": "v1.18.14",
                        "name": "R14",
                        "published_at": "2026-08-08T00:00:00Z",
                        "body": "skill loading + cache fix\n",
                    },
                ]
            )
        raise AssertionError(url)

    data, exit_code = releases.run(make_cfg(), anchor=ANCHOR_ISO, client=FakeClient(handler))

    assert exit_code == 0  # mcp + releases ok despite npm/github failures
    assert len(data["new_items"]) == 1
    assert data["new_items"][0]["name"] == "acme/mcp"
    assert len(data["core_changes"]) == 1
    warn_sources = [w["source"] for w in data["warnings"]]
    assert warn_sources == [S_NPM, S_GITHUB]
    assert data["counts_by_source"][S_MCP] == 1
    assert data["counts_by_source"][S_RELEASES] == 1


# convenience ISO helper (mirrors releases._iso without importing internals)
def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================ v5.28 (watch repos)


URL_WATCH_INFO = "https://api.github.com/repos/adeo/ai-skills"
URL_WATCH_REL = "https://api.github.com/repos/adeo/ai-skills/releases"
S_WATCH = "github:watch-repos"
URL_WATCH_COMMITS = "https://api.github.com/repos/adeo/ai-skills/commits"


def no_gh(endpoint: str):
    """Test-only: make the gh fallback fail (hermetic failure-path tests)."""
    raise releases.SourceError("gh indisponible (test hermétique)")


def test_watch_repos_emits_release_in_window(monkeypatch):
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    handler = make_handler(
        {
            URL_WATCH_INFO: {
                "full_name": "adeo/ai-skills",
                "pushed_at": "2026-08-08T00:00:00Z",
                "html_url": "https://github.com/adeo/ai-skills",
                "description": "Skills internes ADEO",
            },
            URL_WATCH_REL: [
                {
                    "tag_name": "v1.1.0",
                    "published_at": "2026-08-05T00:00:00Z",
                    "body": "nouvelle règle harness\nmaj skills",
                }
            ],
            URL_NPM: npm_payload(),
            URL_GITHUB: {"items": []},
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )
    cfg = make_cfg(watch_repos=["adeo/ai-skills"])
    data, rc = releases.run(cfg, anchor=ANCHOR_ISO, client=FakeClient(handler))
    assert rc == 0
    assert data["watch_repos"] == ["adeo/ai-skills"]
    assert data["counts_by_source"][S_WATCH] >= 1
    watch_items = [i for i in data["new_items"] if i["repo_url"].endswith("adeo/ai-skills")]
    assert watch_items
    assert "adeo/ai-skills v1.1.0" in watch_items[0]["name"]
    assert "github:watch-repos" in watch_items[0]["found_via"]
    assert watch_items[0]["category"] == "repo"
    assert data["counts_by_category"]["repo"] == 1


def test_watch_repos_silent_when_no_window_activity(monkeypatch):
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    handler = make_handler(
        {
            URL_WATCH_INFO: {
                "full_name": "adeo/ai-skills",
                "pushed_at": "2026-01-01T00:00:00Z",  # hors fenêtre
                "html_url": "https://github.com/adeo/ai-skills",
                "description": "Skills internes ADEO",
            },
            URL_WATCH_REL: [{"tag_name": "v0.9.0", "published_at": "2026-01-02T00:00:00Z"}],
            URL_WATCH_COMMITS: [],  # aucun commit dans la fenêtre
            URL_NPM: npm_payload(),
            URL_GITHUB: {"items": []},
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )
    cfg = make_cfg(watch_repos=["adeo/ai-skills"])
    data, rc = releases.run(cfg, anchor=ANCHOR_ISO, client=FakeClient(handler))
    assert rc == 0
    assert data["watch_repos"] == ["adeo/ai-skills"]
    assert data["counts_by_source"][S_WATCH] == 0
    assert not any("adeo/ai-skills" in i["name"] for i in data["new_items"])


def test_watch_repos_falls_back_to_gh(monkeypatch):
    """Plain HTTP 404 (repo privé/renommé) → fallback authentifié via `gh api`."""
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))

    def fake_gh(endpoint: str):
        if endpoint.startswith("repos/adeo/ai-skills/releases"):
            return []
        if endpoint.startswith("repos/adeo/ai-skills"):
            return {
                "full_name": "adeo/applied-ai",
                "html_url": "https://github.com/adeo/applied-ai",
                "pushed_at": "2026-08-06T10:00:00Z",  # dans la période (03→10/08)
                "description": "Two Claude code toolkits",
                "private": True,
            }
        raise AssertionError(f"endpoint gh inattendu: {endpoint}")

    monkeypatch.setattr(releases._http, "_gh_api", fake_gh)
    handler = make_handler(
        {
            URL_WATCH_INFO: urllib.error.URLError("connection reset"),
            URL_WATCH_REL: urllib.error.URLError("connection reset"),
            URL_NPM: npm_payload(),
            URL_GITHUB: {"items": []},
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )
    cfg = make_cfg(watch_repos=["adeo/ai-skills"])
    data, rc = releases.run(cfg, anchor=ANCHOR_ISO, client=FakeClient(handler))
    assert rc == 0
    names = [i["name"] for i in data["new_items"]]
    assert "adeo/applied-ai" in names  # alias adeo/ai-skills résolu après renommage
    wi = [i for i in data["new_items"] if "github:watch-repos" in (i.get("found_via") or [])]
    assert wi
    assert "Renommé de adeo/ai-skills" in wi[0]["description"]
    assert wi[0]["repo_url"].endswith("adeo/applied-ai")
    assert data["counts_by_source"][S_WATCH] == 1


def test_github_topic_search_falls_back_to_gh(monkeypatch):
    """K3: le topic search utilise le fallback gh quand l'HTTP anonyme échoue."""
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))

    def fake_gh(endpoint):
        if endpoint.startswith("search/repositories"):
            return {
                "items": [
                    {
                        "full_name": "acme/gh-via-cli",
                        "html_url": "https://github.com/acme/gh-via-cli",
                        "description": "found via gh",
                        "created_at": "2026-08-01T00:00:00Z",
                        "pushed_at": "2026-08-06T09:00:00Z",
                    }
                ]
            }
        if endpoint.startswith("repos/anomalyco/opencode/releases"):
            return []
        raise AssertionError(f"endpoint gh inattendu: {endpoint}")

    monkeypatch.setattr(releases._http, "_gh_api", fake_gh)
    handler = make_handler(
        {
            URL_GITHUB: urllib.error.URLError("connection reset"),  # HTTP échoue
            URL_RELEASES: urllib.error.URLError("connection reset"),
            URL_NPM: npm_payload(),
            URL_MCP: {"servers": []},
        }
    )
    data, rc = releases.run(make_cfg(), anchor=ANCHOR_ISO, client=FakeClient(handler))
    assert rc == 0
    assert any(i["name"] == "acme/gh-via-cli" for i in data["new_items"])
    assert data["warnings"] == []  # pas de warning : le fallback a réussi


def test_watch_repos_commits_fallback_in_window(monkeypatch):
    """v5.30 (3) : dernier push post-clôture mais commits dans la fenêtre → item émis."""
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    handler = make_handler(
        {
            URL_WATCH_INFO: {
                "full_name": "adeo/applied-ai",
                "pushed_at": "2026-08-10T23:00:00Z",  # hors fenêtre (post-clôture)
                "html_url": "https://github.com/adeo/applied-ai",
                "description": "Skills internes ADEO",
            },
            URL_WATCH_REL: [],
            URL_WATCH_COMMITS: [
                {"commit": {"author": {"date": "2026-08-06T09:00:00Z"}}},  # dans la fenêtre
                {"commit": {"author": {"date": "2026-08-05T09:00:00Z"}}},
            ],
            URL_NPM: npm_payload(),
            URL_GITHUB: {"items": []},
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )
    cfg = make_cfg(watch_repos=["adeo/ai-skills"])
    data, rc = releases.run(cfg, anchor=ANCHOR_ISO, client=FakeClient(handler))
    assert rc == 0
    wi = [i for i in data["new_items"] if "github:watch-repos" in (i.get("found_via") or [])]
    assert wi, "item watch attendu via fallback commits"
    assert wi[0]["published_at"] == "2026-08-06T09:00:00Z"  # dernier commit de la fenêtre
    assert "2 commit(s)" in wi[0]["description"]
    assert data["counts_by_source"][S_WATCH] == 1


# ============================================================ v5.30 (veille étendue : list + web)


class _FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


def test_extract_markdown_links():
    md = "[Plugin X](https://github.com/a/b) et [Skill Y](https://github.com/c/d)."
    assert ("Plugin X", "https://github.com/a/b") in releases._extract_markdown_links(md)
    assert releases._link_category("https://github.com/x/mcp-server") == "mcp-server"
    assert releases._link_category("https://github.com/x/awesome-skill") == "skill"


def test_watch_list_baseline_then_diff(monkeypatch, tmp_path):
    """type list : premier run = baseline silencieuse, 2e run = nouveaux liens émis."""
    import base64 as b64

    readme = "# Awesome\n- [Plugin A](https://github.com/acme/a)\n- [Plugin B](https://github.com/acme/b)\n"
    state = tmp_path / "watch-state"

    def fake_gh(endpoint):
        if endpoint.startswith("repos/awesome-opencode/awesome-opencode/readme"):
            return {"content": b64.b64encode(readme.encode()).decode()}
        raise AssertionError(endpoint)

    monkeypatch.setattr(releases._http, "_gh_api", fake_gh)
    client = FakeClient(lambda url, p, h: (_ for _ in ()).throw(urllib.error.URLError("reset")))
    end = _dt(2026, 8, 14)

    first = releases._fetch_watch_list(client, "awesome-opencode/awesome-opencode", end, state)
    assert first == []  # baseline silencieuse
    assert (state / "list-awesome-opencode-awesome-opencode.txt").exists()

    # le README gagne un lien
    readme = readme + "- [Plugin C](https://github.com/acme/c)\n"
    second = releases._fetch_watch_list(client, "awesome-opencode/awesome-opencode", end, state)
    assert len(second) == 1
    assert second[0]["name"] == "Plugin C"
    assert second[0]["found_via"] == ["watch:list:awesome-opencode/awesome-opencode"]
    assert second[0]["published_at"] == end


ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Nouvel article</title>
    <link href="https://simonwillison.net/2026/08/10/x/"/>
    <updated>2026-08-10T09:00:00Z</updated></entry>
  <entry><title>Vieux article</title>
    <link href="https://simonwillison.net/2026/01/01/x/"/>
    <updated>2026-01-01T09:00:00Z</updated></entry>
</feed>"""

RSS2 = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Post RSS</title><link>https://www.anthropic.com/news/post</link>
    <pubDate>Fri, 08 Aug 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_fetch_rss_atom_and_rss2():

    end = _dt(2026, 8, 14)
    start = _dt(2026, 8, 1)

    client = FakeClient(lambda url, p, h: _FakeResp(ATOM))
    items = releases._fetch_rss(client, "https://simonwillison.net/atom/everything/", start, end)
    assert len(items) == 1
    assert items[0]["name"] == "Nouvel article"
    assert items[0]["found_via"] == ["rss:https://simonwillison.net/atom/everything/"]
    assert items[0]["category"] == "article"

    client2 = FakeClient(lambda url, p, h: _FakeResp(RSS2))
    items2 = releases._fetch_rss(client2, "https://www.anthropic.com/news/rss", start, end)
    assert len(items2) == 1
    assert items2[0]["name"] == "Post RSS"


def test_fetch_github_topics(monkeypatch):
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))

    def fake_gh(endpoint):
        if endpoint.startswith("search/repositories"):
            return {
                "items": [
                    {
                        "full_name": "acme/claude-tool",
                        "html_url": "https://github.com/acme/claude-tool",
                        "description": "un outil",
                        "created_at": "2026-08-01T00:00:00Z",
                        "pushed_at": "2026-08-09T00:00:00Z",
                    },
                    {
                        "full_name": "acme/old",
                        "html_url": "https://github.com/acme/old",
                        "description": "x",
                        "created_at": "2025-01-01T00:00:00Z",
                        "pushed_at": "2026-01-02T00:00:00Z",
                    },
                ]
            }
        raise AssertionError(endpoint)

    monkeypatch.setattr(releases._http, "_gh_api", fake_gh)
    client = FakeClient(lambda url, p, h: (_ for _ in ()).throw(urllib.error.URLError("reset")))
    items = releases._fetch_github_topics(
        client, "claude-code", _dt(2026, 8, 1), _dt(2026, 8, 14), 5
    )
    assert len(items) == 1
    assert items[0]["name"] == "acme/claude-tool"
    assert items[0]["found_via"] == ["github:topic:claude-code"]
    assert items[0]["new_repo"] is True


# ============================================================ Task 4 (radar MCP + version passthrough)


def test_finalize_items_preserves_version():
    """Le champ `version` (core_changes/watch) survit à la finalisation → signature mémoire."""
    record = {
        "name": "some-repo v1.2.3",
        "category": "repo",
        "repo_url": "https://github.com/acme/some-repo",
        "npm_package": None,
        "description": "d",
        "published_at": PERIOD_END,
        "found_via": ["radar"],
        "new_repo": False,
        "version": "v1.2.3",
    }
    items = releases._finalize_items({"k": dict(record)})
    assert len(items) == 1
    assert items[0]["version"] == "v1.2.3"

    del record["version"]
    items_no_version = releases._finalize_items({"k": dict(record)})
    assert items_no_version[0]["version"] is None


RADAR_URL = "https://radar.test/mcp"
RADAR_FEED_URL = "https://radar.test/feed.xml"
RADAR_ENTRY = {
    "type": "radar",
    "name": "agents-radar",
    "tool": "get_latest",
    "window_days": 7,
    "rss_fallback": RADAR_FEED_URL,
}

RADAR_MARKDOWN = (
    "# Digest\n"
    "- [Tool A](https://github.com/acme/tool-a) — 2026-08-05\n"
    "- [Tool B](https://github.com/acme/tool-b) (2026-08-08)\n"
    "- [Hors fenêtre](https://github.com/acme/old) — 2026-01-01\n"
    "- [Non daté](https://github.com/acme/undated)\n"
)

SSE_TOOLS_CALL = (
    'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"content":'
    '[{"type":"text","text":"- [SSE Tool](https://github.com/acme/sse-tool) 2026-08-06"}]}}\n\n'
)


def _radar_opencode_json(tmp_path, mcp=None):
    doc = {"$schema": "https://opencode.ai/config.json"}
    if mcp is not None:
        doc["mcp"] = mcp
    (tmp_path / "opencode.json").write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path


class RadarFake:
    """Fake client radar MCP : initialize → session, tools/call piloté, GET RSS piloté."""

    def __init__(self, tools_call, *, rss_status: int = 503, rss_text: str = ""):
        self.tools_call = tools_call
        self.rss_status = rss_status
        self.rss_text = rss_text
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def post(self, url, *, json=None, headers=None):
        self.posts.append(json or {})
        method = (json or {}).get("method")
        if method == "initialize":
            assert headers["Accept"] == "application/json, text/event-stream"
            assert headers["Content-Type"].startswith("application/json")
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "agents-radar"},
                    },
                },
                200,
                headers={"mcp-session-id": "sess-42"},
            )
        if method == "tools/call":
            assert headers.get("mcp-session-id") == "sess-42", (
                "la session initialize doit être reprise dans tools/call"
            )
            assert json["params"]["name"] == RADAR_ENTRY["tool"]
            return self.tools_call(json)
        return FakeResponse({}, 404)

    def get(self, url, *, timeout=None):
        self.gets.append(url)
        return FakeResponse(None, self.rss_status, text=self.rss_text)

    def close(self):
        pass


def test_fetch_radar_happy_path_mcp(tmp_path):
    """initialize + tools/call → liens markdown datés ; non datés et hors fenêtre exclus."""
    root = _radar_opencode_json(
        tmp_path, {RADAR_ENTRY["name"]: {"type": "remote", "url": RADAR_URL}}
    )

    def tools_call(_body):
        return FakeResponse(
            None,
            200,
            text=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"content": [{"type": "text", "text": RADAR_MARKDOWN}]},
                }
            ),
        )

    client = RadarFake(tools_call)
    items = releases._fetch_radar(client, RADAR_ENTRY, PERIOD_START, PERIOD_END, project_root=root)

    names = [i["name"] for i in items]
    assert names == ["Tool A", "Tool B"]  # undated exclu, hors fenêtre exclu
    first = items[0]
    assert first["category"] == "repo"
    assert first["repo_url"] == "https://github.com/acme/tool-a"
    assert first["npm_package"] is None
    assert first["found_via"] == ["radar"]
    assert first["new_repo"] is False
    assert first["published_at"] == datetime(2026, 8, 5, tzinfo=UTC)


def test_fetch_radar_parses_sse_tools_call(tmp_path):
    """Réponse tools/call en flux SSE (`event:`/`data:`) → dernière ligne data parsée."""
    root = _radar_opencode_json(
        tmp_path, {RADAR_ENTRY["name"]: {"type": "remote", "url": RADAR_URL}}
    )

    def tools_call(_body):
        return FakeResponse(
            None, 200, text=SSE_TOOLS_CALL, headers={"content-type": "text/event-stream"}
        )

    client = RadarFake(tools_call)
    items = releases._fetch_radar(client, RADAR_ENTRY, PERIOD_START, PERIOD_END, project_root=root)
    assert [i["name"] for i in items] == ["SSE Tool"]
    assert items[0]["published_at"] == datetime(2026, 8, 6, tzinfo=UTC)


def test_fetch_radar_falls_back_to_rss_when_tools_call_500(tmp_path):
    """tools/call en 500 → repli RSS déclaré dans l'entrée radar."""
    root = _radar_opencode_json(
        tmp_path, {RADAR_ENTRY["name"]: {"type": "remote", "url": RADAR_URL}}
    )
    fallback_atom = (
        '<?xml version="1.0"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "  <entry><title>Fallback Item</title>"
        '<link href="https://github.com/acme/fallback"/>'
        "<updated>2026-08-05T09:00:00Z</updated></entry>\n</feed>"
    )

    def tools_call(_body):
        return FakeResponse(None, 500)

    client = RadarFake(tools_call, rss_status=200, rss_text=fallback_atom)
    items = releases._fetch_radar(client, RADAR_ENTRY, PERIOD_START, PERIOD_END, project_root=root)
    assert [i["name"] for i in items] == ["Fallback Item"]
    assert items[0]["found_via"] == [f"rss:{RADAR_FEED_URL}"]
    assert client.gets == [RADAR_FEED_URL]


def test_fetch_radar_both_dead_raises_source_error(tmp_path):
    """MCP mort ET flux RSS mort → SourceError (warning source, run inchangé)."""
    root = _radar_opencode_json(
        tmp_path, {RADAR_ENTRY["name"]: {"type": "remote", "url": RADAR_URL}}
    )

    def tools_call(_body):
        return FakeResponse(None, 500)

    client = RadarFake(tools_call)  # RSS mort aussi (503 par défaut)
    with pytest.raises(releases.SourceError, match="feed.xml"):
        releases._fetch_radar(client, RADAR_ENTRY, PERIOD_START, PERIOD_END, project_root=root)


def test_fetch_radar_missing_mcp_url_raises_clear_error(tmp_path):
    """URL absente de opencode.json → SourceError nommant la clé manquante."""
    root = _radar_opencode_json(tmp_path, None)  # pas de clé mcp du tout
    client = RadarFake(lambda _body: FakeResponse(None, 200, text="{}"))
    with pytest.raises(releases.SourceError, match=r"mcp\.agents-radar\.url.*opencode\.json"):
        releases._fetch_radar(client, RADAR_ENTRY, PERIOD_START, PERIOD_END, project_root=root)


def test_collect_routes_radar_entries(monkeypatch, tmp_path):
    """_collect route le type radar : source_id `radar:<name>`, project_root transmis."""
    monkeypatch.setattr(releases._http, "_BACKOFF", (0.0, 0.0))
    seen: dict = {}

    def fake_radar(client, entry, start, end, *, project_root):
        seen.update(entry=dict(entry), project_root=project_root, start=start, end=end)
        return [
            {
                "name": "Radar Item",
                "category": "repo",
                "repo_url": "https://github.com/acme/radar-item",
                "npm_package": None,
                "description": "d",
                "published_at": PERIOD_END - timedelta(days=1),
                "found_via": ["radar"],
                "new_repo": False,
            }
        ]

    monkeypatch.setattr(releases, "_fetch_radar", fake_radar)
    handler = make_handler(
        {
            URL_NPM: npm_payload(),
            URL_GITHUB: {"items": []},
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )
    cfg = make_cfg(watch=[dict(RADAR_ENTRY)], project_root=tmp_path)
    data, rc = releases.run(cfg, anchor=ANCHOR_ISO, client=FakeClient(handler))

    assert rc == 0
    assert seen["entry"]["tool"] == "get_latest"
    assert seen["entry"]["rss_fallback"] == RADAR_FEED_URL
    assert seen["project_root"] == tmp_path
    assert seen["start"] == PERIOD_START and seen["end"] == PERIOD_END
    assert data["counts_by_source"]["radar:agents-radar"] == 1
    assert [i["name"] for i in data["new_items"]] == ["Radar Item"]
    assert data["warnings"] == []


def test_config_parses_radar_watch_entry(tmp_path):
    """Le parseur config préserve les clés radar (tool/window_days/rss_fallback)."""
    cfg_file = tmp_path / "weekly-telemetry-config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "watch": [
                    {"type": "repo", "name": "openai/codex"},
                    {
                        "type": "radar",
                        "name": "agents-radar",
                        "tool": "get_latest",
                        "window_days": 7,
                        "rss_fallback": RADAR_FEED_URL,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    radar = next(w for w in cfg.watch if w["type"] == "radar")
    assert radar == {
        "type": "radar",
        "name": "agents-radar",
        "tool": "get_latest",
        "window_days": 7,
        "rss_fallback": RADAR_FEED_URL,
    }
    repo = next(w for w in cfg.watch if w["type"] == "repo")
    assert repo == {"type": "repo", "name": "openai/codex"}  # pas de clés fantômes


# ============================================================ F1: HTTP client + dedup fan-in
# Caractérisation (pas de code neuf) du cœur retry et du fan-in dedup de `releases`.


class _UrllibRecorder:
    """Stub de `releases._http.urlopen` : enregistre (url, timeout, data), rend un contexte."""

    def __init__(self, status: int = 200, body: bytes = b"{}", headers: dict | None = None):
        self.status = status
        self.body = body
        self.headers = headers if headers is not None else {"content-type": "application/json"}
        self.calls: list[tuple[str, object, bytes | None]] = []

    def __call__(self, request, timeout=None):
        self.calls.append((request.full_url, timeout, request.data))
        recorder = self

        class _Ctx:
            status = recorder.status
            headers = recorder.headers

            @staticmethod
            def read() -> bytes:
                return recorder.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Ctx()


def test_response_decodes_utf8_replace_and_json():
    """`_Response` : texte en errors=replace, `.json()` décode le texte (pas le payload)."""
    resp = releases._Response(200, b'{"k": "caf\xe9"}', {"h": "v"})
    assert resp.status_code == 200
    assert resp.headers == {"h": "v"}
    assert "�" in resp.text  # octet invalide remplacé, pas d'exception
    assert resp.json() == {"k": "caf�"}


def test_http_client_get_encodes_params_and_timeout(monkeypatch):
    """GET : params encodés UTF-8 (%20 pour l'espace, %2B pour `+`), timeout du client."""
    rec = _UrllibRecorder(body=b'{"ok": true}')
    monkeypatch.setattr(releases._http, "urlopen", rec)
    client = releases._HttpClient(timeout=7)

    resp = client.get("https://x.test/s", params={"q": "a b+c", "per_page": 50})

    assert json.loads(resp.text) == {"ok": True}
    url, timeout, data = rec.calls[0]
    assert url == "https://x.test/s?q=a%20b%2Bc&per_page=50"
    assert timeout == 7
    assert data is None


def test_http_client_get_timeout_override_and_no_params(monkeypatch):
    """Timeout par appel wins ; sans params, aucune query string n'est ajoutée."""
    rec = _UrllibRecorder(body=b"[]")
    monkeypatch.setattr(releases._http, "urlopen", rec)
    client = releases._HttpClient(timeout=15)

    client.get("https://x.test/s", timeout=1)
    client.get("https://x.test/s")

    assert [c[1] for c in rec.calls] == [1, 15]
    assert rec.calls[1][0] == "https://x.test/s"


def test_http_client_post_serializes_json(monkeypatch):
    """POST : corps JSON sérialisé, aucun timeout par appel (celui du client)."""
    rec = _UrllibRecorder(body=b'{"result": 1}')
    monkeypatch.setattr(releases._http, "urlopen", rec)

    releases._HttpClient(timeout=3).post(
        "https://x.test/mcp", json={"method": "initialize"}, headers={"mcp-session-id": "s"}
    )

    url, timeout, data = rec.calls[0]
    assert url == "https://x.test/mcp"
    assert timeout == 3
    assert json.loads(data) == {"method": "initialize"}


def test_http_client_post_without_body_sends_no_data(monkeypatch):
    """POST sans json → data None (le corps est porté par json=..., pas par data)."""
    rec = _UrllibRecorder(body=b"")
    monkeypatch.setattr(releases._http, "urlopen", rec)

    releases._HttpClient().post("https://x.test/mcp")

    assert rec.calls[0][2] is None


def test_http_client_http_error_is_returned_not_raised(monkeypatch):
    """HTTPError 4xx/5xx : corps lisible renvoyé en `_Response` (pas d'exception)."""

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b"missing"))

    monkeypatch.setattr(releases._http, "urlopen", boom)
    resp = releases._HttpClient().get("https://x.test/s")

    assert resp.status_code == 404
    assert resp.text == "missing"


def test_http_client_network_error_propagates(monkeypatch):
    """URLError n'est pas capturé par le client : la politique retry est chez _get_json."""

    def boom(request, timeout=None):
        raise urllib.error.URLError("dns")

    monkeypatch.setattr(releases._http, "urlopen", boom)
    with pytest.raises(urllib.error.URLError):
        releases._HttpClient().get("https://x.test/s")


# --------------------------------------------------------- _get_json (retry core)
class _CountingClient:
    """Client scripté : renvoie une réponse/exception par appel, compte les tentatives."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        return step

    def close(self):
        pass


def _no_sleep(monkeypatch) -> list[float]:
    """Neutralise time.sleep et enregistre les durées de backoff demandées."""
    slept: list[float] = []
    monkeypatch.setattr(releases._http.time, "sleep", slept.append)
    return slept


def test_get_json_returns_payload_on_200_without_retry():
    """200 : un seul appel, pas de backoff."""
    client = _CountingClient([FakeResponse({"v": 1})])
    assert releases._get_json(client, "https://x.test/a", params={"p": 1}) == {"v": 1}
    assert len(client.calls) == 1
    assert client.calls[0] == ("https://x.test/a", {"p": 1}, None)


def test_get_json_passes_through_non_error_statuses():
    """2xx/3xx : le corps est retourné tel quel — la politique erreur est >= 400."""
    for status in (200, 201, 204, 301, 399):
        client = _CountingClient([FakeResponse({"s": status}, status=status)])
        assert releases._get_json(client, "https://x.test/a") == {"s": status}
        assert len(client.calls) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_get_json_retries_retryable_statuses_then_raises(monkeypatch, status):
    """{429, 5xx} : `_RETRIES` tentatives, backoff _BACKOFF, puis SourceError."""
    slept = _no_sleep(monkeypatch)
    client = _CountingClient([FakeResponse({}, status=status)])

    with pytest.raises(
        releases.SourceError, match=f"failed after {releases._http._RETRIES} attempts"
    ) as ei:
        releases._get_json(client, "https://x.test/a")

    assert len(client.calls) == releases._http._RETRIES
    assert slept == list(releases._http._BACKOFF)
    assert f"HTTP {status}" in str(ei.value)  # la dernière cause est citée


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_get_json_does_not_retry_client_4xx(monkeypatch, status):
    """Tout 4xx : SourceError immédiat, un seul appel, aucun backoff (non retriable)."""
    slept = _no_sleep(monkeypatch)
    client = _CountingClient([FakeResponse({}, status=status)])

    with pytest.raises(releases.SourceError, match=rf"https://x\.test/a: HTTP {status}"):
        releases._get_json(client, "https://x.test/a")

    assert len(client.calls) == 1
    assert slept == []


def test_get_json_recovers_on_second_attempt(monkeypatch):
    """503 puis 200 : la 2e tentative renvoie le payload (retry non destructif)."""
    slept = _no_sleep(monkeypatch)
    client = _CountingClient([FakeResponse({}, status=503), FakeResponse({"ok": True})])

    assert releases._get_json(client, "https://x.test/a") == {"ok": True}
    assert len(client.calls) == 2
    assert slept == [releases._http._BACKOFF[0]]


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("dns"),
        ConnectionResetError("reset"),
        TimeoutError("timed out"),
        OSError("io"),
    ],
)
def test_get_json_retries_network_errors(monkeypatch, exc):
    """URLError/OSError/TimeoutError : retriés jusqu'à `_RETRIES` puis SourceError."""
    slept = _no_sleep(monkeypatch)
    client = _CountingClient([exc])

    with pytest.raises(releases.SourceError, match="failed after 3 attempts"):
        releases._get_json(client, "https://x.test/a")

    assert len(client.calls) == releases._http._RETRIES
    assert slept == list(releases._http._BACKOFF)


def test_get_json_retries_undecodable_body(monkeypatch):
    """Corps non-JSON : retrié comme une erreur réseau, puis SourceError."""
    _no_sleep(monkeypatch)
    client = _CountingClient([FakeResponse(None, text="<html>oops</html>")])

    with pytest.raises(releases.SourceError, match="failed after 3 attempts"):
        releases._get_json(client, "https://x.test/a")
    assert len(client.calls) == releases._http._RETRIES


def test_get_json_uses_client_default_timeout_only(monkeypatch):
    """`_get_json` ne passe pas de timeout : la borne reste celle du client (15 s)."""
    rec = _UrllibRecorder(body=b"{}")
    monkeypatch.setattr(releases._http, "urlopen", rec)

    releases._get_json(releases._HttpClient(timeout=15), "https://x.test/a")

    assert rec.calls[0][1] == 15


# --------------------------------------------------------- _github_headers / _github_json
def test_github_headers_absent_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert releases._github_headers() is None


def test_github_headers_bearer_when_token_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    assert releases._github_headers() == {"Authorization": "Bearer ghp_x"}


def test_github_json_sends_token_header(monkeypatch):
    """Le token d'env est transmis à `_get_json` (pas d'import client)."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    seen: list[dict | None] = []

    class Client:
        def get(self, url, params=None, headers=None):
            seen.append(headers)
            return FakeResponse({"ok": True})

    assert releases._github_json(Client(), "https://api.github.com/x") == {"ok": True}
    assert seen == [{"Authorization": "Bearer ghp_x"}]


def test_github_json_falls_back_to_gh_with_encoded_params(monkeypatch):
    """Échec HTTP → `_gh_api(path?params)`, `+` préservé (safe='+'), espace en %20."""
    seen: list[str] = []

    def fake_gh(endpoint):
        seen.append(endpoint)
        return {"items": []}

    monkeypatch.setattr(releases._http, "_gh_api", fake_gh)
    client = _CountingClient([FakeResponse({}, status=404)])

    assert releases._github_json(
        client,
        "https://api.github.com/search/repositories",
        params={"q": "topic:oc stars:>5", "page": 2},
    ) == {"items": []}

    assert seen == ["search/repositories?q=topic%3Aoc%20stars%3A%3E5&page=2"]


def test_github_json_falls_back_without_params(monkeypatch):
    """Sans params, le endpoint gh est le path nu (pas de `?`)."""
    seen: list[str] = []
    monkeypatch.setattr(releases._http, "_gh_api", lambda ep: seen.append(ep) or [])
    client = _CountingClient([FakeResponse({}, status=403)])

    releases._github_json(client, "https://api.github.com/repos/anomalyco/opencode/releases")

    assert seen == ["repos/anomalyco/opencode/releases"]


def test_github_json_propagates_gh_failure(monkeypatch):
    """Si le fallback gh échoue aussi, son SourceError remonte au run_source."""
    monkeypatch.setattr(releases._http, "_gh_api", no_gh)
    client = _CountingClient([FakeResponse({}, status=404)])

    with pytest.raises(releases.SourceError, match="gh indisponible"):
        releases._github_json(client, "https://api.github.com/x")


# ------------------------------------------------------------------ _gh_api (gh CLI)
class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_gh_api_parses_json_stdout(monkeypatch):
    """`gh api <endpoint> --paginate` : stdout parsé en JSON, timeout=30."""
    seen: list[tuple] = []

    def fake_run(argv, **kw):
        seen.append((argv, kw))
        return _Proc(stdout='{"full_name": "a/b"}')

    monkeypatch.setattr(releases._http.subprocess, "run", fake_run)

    assert releases._http._gh_api("repos/a/b") == {"full_name": "a/b"}
    argv, kw = seen[0]
    assert argv == ["gh", "api", "repos/a/b", "--paginate"]
    assert kw["capture_output"] is True
    assert kw["timeout"] == 30


def test_gh_api_non_zero_exit_truncates_stderr(monkeypatch):
    """rc != 0 : SourceError avec stderr tronqué à 180 caractères."""
    monkeypatch.setattr(
        releases._http.subprocess, "run", lambda argv, **kw: _Proc(returncode=1, stderr="E" * 300)
    )
    with pytest.raises(releases.SourceError) as excinfo:
        releases._http._gh_api("repos/a/b")
    assert "E" * 180 in str(excinfo.value)
    assert "E" * 181 not in str(excinfo.value)


def test_gh_api_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(
        releases._http.subprocess, "run", lambda argv, **kw: _Proc(stdout="not json")
    )
    with pytest.raises(releases.SourceError, match="sortie JSON invalide"):
        releases._http._gh_api("repos/a/b")


@pytest.mark.parametrize("exc", [OSError("gh absent"), subprocess.TimeoutExpired("gh", 30)])
def test_gh_api_transport_failure_raises(monkeypatch, exc):
    """Binaire absent ou timeout → SourceError (jamais d'exception subprocess brute)."""
    monkeypatch.setattr(releases._http.subprocess, "run", _raise(exc))
    with pytest.raises(releases.SourceError, match="gh api repos/a/b"):
        releases._http._gh_api("repos/a/b")


def _raise(exc):
    def boom(argv, **kw):
        raise exc

    return boom


# --------------------------------------------------------------------- dedup fan-in
def _item(name="n", **kw) -> dict:
    base = {
        "name": name,
        "category": "plugin",
        "repo_url": "",
        "npm_package": None,
        "description": "",
        "published_at": PERIOD_END,
        "found_via": [],
        "new_repo": False,
    }
    base.update(kw)
    return base


def test_dedup_key_precedence():
    """repo_url > npm_package > name > chaîne vide."""
    assert releases._dedup_key(_item(repo_url="https://u")) == "https://u"
    assert releases._dedup_key(_item(npm_package="pkg")) == "pkg"
    assert releases._dedup_key(_item(name="only-name")) == "only-name"
    assert releases._dedup_key({"name": ""}) == ""


def test_preferred_record_prefers_non_empty_description():
    a = _item(description="from npm")
    b = _item(description="")
    assert releases._preferred_record(a, b) is a
    assert releases._preferred_record(b, a) is a


def test_preferred_record_earliest_published_at_when_both_described():
    """Descriptions égales → le published_at le plus ancien gagne."""
    a = _item(description="d", published_at=PERIOD_END)
    b = _item(description="d", published_at=PERIOD_START)
    assert releases._preferred_record(a, b) is b
    assert releases._preferred_record(b, a) is b


def test_preferred_record_dated_beats_undated():
    dated = _item(description="d", published_at=PERIOD_START)
    undated = _item(description="d", published_at=None)
    assert releases._preferred_record(undated, dated) is dated
    assert releases._preferred_record(dated, undated) is dated


def test_merge_into_keeps_existing_identity_and_unions_found_via():
    """Survivre = l'enregistrement préféré ; repo_url/npm_package/found_via sont cumulés."""
    existing = _item(
        name="npm-name",
        description="from npm",
        repo_url="https://u",
        npm_package=None,
        found_via=[S_NPM],
    )
    incoming = _item(
        name="gh-name",
        description="",
        repo_url="",
        npm_package="pkg",
        published_at=PERIOD_START,
        found_via=[S_GITHUB, S_NPM],  # doublon : ne doit pas être dupliqué
        new_repo=True,
    )

    merged = releases._merge_into(existing, incoming)

    assert merged["name"] == "npm-name"  # description non vide ⇒ existing survit
    assert merged["description"] == "from npm"
    assert merged["repo_url"] == "https://u"  # existing gagne, sinon incoming
    assert merged["npm_package"] == "pkg"  # existing vide ⇒ repris depuis incoming
    assert merged["found_via"] == [S_NPM, S_GITHUB]  # union, ordre d'insertion
    assert merged["new_repo"] is True  # OU logique
    assert merged["published_at"] == PERIOD_END  # enregistrement préféré = existing


def test_merge_into_falls_back_to_incoming_repo_url():
    existing = _item(description="x", repo_url="", found_via=[S_NPM])
    incoming = _item(description="", repo_url="https://u2", found_via=[S_GITHUB])
    merged = releases._merge_into(existing, incoming)
    assert merged["repo_url"] == "https://u2"
    assert merged["description"] == "x"


def test_add_item_inserts_then_merges():
    """Première occurrence : copie défensive. Seconde : fusion sur la même clé."""
    items: dict[str, dict] = {}
    first = _item(name="a", repo_url="https://u", found_via=[S_NPM])
    releases._add_item(items, first)

    first["name"] = "mutated"  # la copie stockée ne suit pas
    assert items["https://u"]["name"] == "a"

    releases._add_item(items, _item(name="b", repo_url="https://u", found_via=[S_MCP]))
    assert list(items) == ["https://u"]
    assert items["https://u"]["found_via"] == [S_NPM, S_MCP]


def test_canonical_found_via_reorders_to_canonical_sequence():
    assert releases._canonical_found_via([S_MCP, S_GITHUB, S_NPM]) == [S_NPM, S_GITHUB, S_MCP]
    # une seule source canonique suffit à basculer sur le filtre canonique :
    # les sources hors séquence (radar/rss) sont alors abandonnées.
    assert releases._canonical_found_via(["radar", S_NPM]) == [S_NPM]
    # aucune source canonique : la liste est renvoyée telle quelle
    assert releases._canonical_found_via(["rss:https://f", "radar"]) == ["rss:https://f", "radar"]


def test_finalize_items_sorting_and_defaults():
    """Tri published_at DESC puis name ASC ; défauts name/category/published_at."""
    items = releases._finalize_items(
        {
            "k1": _item(name="bbb", published_at=PERIOD_START),
            "k2": _item(name="aaa", published_at=PERIOD_START),
            "k3": _item(name="ccc", published_at=PERIOD_END),
            "k4": {"name": "zzz"},  # aucun champ optionnel
        }
    )
    assert [i["name"] for i in items] == ["ccc", "aaa", "bbb", "zzz"]
    last = items[-1]
    assert last["category"] == "plugin"
    assert last["repo_url"] == ""
    assert last["npm_package"] is None
    assert last["description"] == ""
    assert last["published_at"] == "1970-01-01T00:00:00Z"  # epoch, pas None
    assert last["found_via"] == []
    assert last["new_repo"] is False


def test_finalize_items_canonicalizes_found_via_at_the_edge():
    """La canonicalisation n'est appliquée qu'au final, pas au dedup interne."""
    items = releases._finalize_items({"k": _item(found_via=[S_MCP, S_NPM])})
    assert items[0]["found_via"] == [S_NPM, S_MCP]


def test_build_ecosystem_counts_only_known_categories():
    """`counts_by_category` ne compte que les 5 catégories du schéma (clé figée)."""
    items = {
        "a": _item(name="p", category="plugin"),
        "b": _item(name="s", category="skill"),
        "c": _item(name="x", category="article"),  # non compté
        "d": _item(name="m", category="mcp-server"),
    }
    payload = releases._build_ecosystem(
        make_cfg(watch_repos=["a/b"]), PERIOD_START, PERIOD_END, items, [], {S_NPM: 4}, []
    )
    assert payload["counts_by_category"] == {
        "plugin": 1,
        "skill": 1,
        "agent": 0,
        "mcp-server": 1,
        "repo": 0,
    }
    assert payload["schema_version"] == 2
    assert payload["period"] == {"start": "2026-08-03T06:00:00Z", "end": "2026-08-10T06:00:00Z"}
    assert payload["generated_at"] == "2026-08-10T06:00:00Z"
    assert payload["watch_repos"] == ["a/b"]
    assert payload["counts_by_source"] == {S_NPM: 4}


def test_build_ecosystem_sorts_core_changes_by_date_desc_then_version():
    changes = [
        {
            "version": "v1.0.0",
            "date": "2026-08-05",
            "summary": "",
            "matched_keywords": [],
            "relevance_flag": "medium",
        },
        {
            "version": "v2.0.0",
            "date": "2026-08-09",
            "summary": "",
            "matched_keywords": [],
            "relevance_flag": "medium",
        },
        {
            "version": "v0.9.0",
            "date": "2026-08-05",
            "summary": "",
            "matched_keywords": [],
            "relevance_flag": "medium",
        },
    ]
    payload = releases._build_ecosystem(make_cfg(), PERIOD_START, PERIOD_END, {}, changes, {}, [])
    assert [c["version"] for c in payload["core_changes"]] == ["v2.0.0", "v0.9.0", "v1.0.0"]


def test_collect_seeds_counts_for_every_canonical_source():
    """`_collect` pré-remplit les 5 sources canoniques à 0, même sans config watch."""
    handler = make_handler(
        {
            URL_NPM: npm_payload(),
            URL_GITHUB: {"items": []},
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )
    cfg = make_cfg()
    payload, rc = releases._collect(cfg, FakeClient(handler), PERIOD_START, PERIOD_END)

    assert rc == 0
    assert payload["counts_by_source"] == {
        S_NPM: 0,
        S_GITHUB: 0,
        S_MCP: 0,
        S_RELEASES: 0,
        S_WATCH: 0,
    }
    assert payload["warnings"] == []


def test_collect_exit_1_when_every_source_fails(monkeypatch):
    """rc=1 seulement si AUCUNE source n'a réussi (0 item mais source ok ⇒ rc=0)."""
    monkeypatch.setattr(releases._http, "_gh_api", no_gh)
    _no_sleep(monkeypatch)

    def handler(url, params, headers):
        raise urllib.error.URLError("down")

    payload, rc = releases._collect(make_cfg(), FakeClient(handler), PERIOD_START, PERIOD_END)

    assert rc == 1
    assert [w["source"] for w in payload["warnings"]] == [S_NPM, S_GITHUB, S_MCP, S_RELEASES]
    assert payload["new_items"] == []


def test_collect_watch_repos_fallback_to_watch_entries(monkeypatch):
    """`watch` vide + `watch_repos` non vide → des entrées repo sont synthétisées."""
    _no_sleep(monkeypatch)
    seen: dict = {}

    def fake_watch_repos(client, repos, start, end):
        seen["repos"] = repos
        return []

    monkeypatch.setattr(releases, "_fetch_watch_repos", fake_watch_repos)
    handler = make_handler(
        {
            URL_NPM: npm_payload(),
            URL_GITHUB: {"items": []},
            URL_MCP: {"servers": []},
            URL_RELEASES: [],
        }
    )

    payload, rc = releases._collect(
        make_cfg(watch_repos=["a/b", "c/d"]), FakeClient(handler), PERIOD_START, PERIOD_END
    )

    assert seen["repos"] == ["a/b", "c/d"]
    assert rc == 0
    assert payload["warnings"] == []


def test_collect_empty_watch_does_not_inflate_ok_sources(monkeypatch):
    """Pas d'entrée watch ⇒ pas de source no-op : un échec réseau seul donne rc=1."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(releases._http, "_gh_api", no_gh)
    called: list[int] = []
    monkeypatch.setattr(releases, "_collect_watch_sources", lambda *a, **k: called.append(1))

    def handler(url, params, headers):
        raise urllib.error.URLError("down")

    _, rc = releases._collect(make_cfg(), FakeClient(handler), PERIOD_START, PERIOD_END)

    assert called == []
    assert rc == 1


def test_fail_message_truncates_long_text():
    """Le message d'échec est préfixé et borné à 120 caractères + `…`."""
    short = releases._fail_message(ValueError("boom"))
    assert short.startswith("API indisponible / rate-limitated; source ignorée pour ce run (boom)")
    long = releases._fail_message(ValueError("x" * 300))
    assert long.endswith("…)")
    assert "x" * 120 in long
    assert "x" * 121 not in long


def test_fail_message_uses_exception_class_when_message_empty():
    assert releases._fail_message(ValueError()).endswith("(ValueError)")


# ============================================================ F2: npm / MCP mapping
# Caractérisation du mapping objet → item : valeurs exactes, normalisation, fallbacks.


def _npm_pkg(name="pkg", date="2026-08-05T10:00:00Z", **kw) -> dict:
    base = {"name": name, "description": "d", "date": date, "keywords": ["opencode"]}
    base.update(kw)
    return base


def _mcp_entry(server=None, official=None, wrap=True) -> dict:
    entry: dict = {}
    if wrap:
        entry["server"] = server or {}
    elif server:
        entry.update(server)
    if official is not None:
        entry["_meta"] = {"io.modelcontextprotocol.registry/official": official}
    return entry


def _official(**kw) -> dict:
    base = {"status": "active", "isLatest": True, "publishedAt": "2026-08-04T12:00:00Z"}
    base.update(kw)
    return base


# ------------------------------------------------------------------- _npm_category
@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ({"name": "opencode-skill-kit"}, "skill"),
        ({"name": "oc-plugin", "keywords": ["Skill", "OpenCode"]}, "skill"),
        ({"name": "OC-SKILL"}, "skill"),  # name lowercased avant recherche
        ({"name": "oc-plugin", "keywords": ["skill"]}, "skill"),
        ({"name": "oc-plugin", "keywords": ["opencode-plugin"]}, "plugin"),
        ({"name": "oc-plugin"}, "plugin"),  # keywords absent ⇒ pas de heuristique
        ({"name": "oc-plugin", "keywords": "skill"}, "plugin"),  # non-list ⇒ ignoré
        ({"name": "", "keywords": ["skill"]}, "skill"),
    ],
)
def test_npm_category_heuristic(package, expected):
    assert releases._npm_category(package) == expected


# ----------------------------------------------------------------- _npm_map_object
def test_npm_map_object_exact_item():
    """Objet npm search → item : valeurs exactes, `found_via` unique, pas de version."""
    item = releases._npm_map_object(
        {"package": _npm_pkg(links={"repository": "https://github.com/acme/p"})},
        PERIOD_START,
        PERIOD_END,
    )
    assert item == {
        "name": "pkg",
        "category": "plugin",
        "repo_url": "https://github.com/acme/p",
        "npm_package": "pkg",
        "description": "d",
        "published_at": datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        "found_via": [S_NPM],
        "new_repo": False,
    }


def test_npm_map_object_window_is_inclusive():
    """Les deux bornes de la fenêtre sont incluses (start <= published <= end)."""
    at_start = releases._npm_map_object(
        {"package": _npm_pkg(date="2026-08-03T06:00:00Z")}, PERIOD_START, PERIOD_END
    )
    at_end = releases._npm_map_object(
        {"package": _npm_pkg(date="2026-08-10T06:00:00Z")}, PERIOD_START, PERIOD_END
    )
    before = releases._npm_map_object(
        {"package": _npm_pkg(date="2026-08-03T05:59:59Z")}, PERIOD_START, PERIOD_END
    )
    after = releases._npm_map_object(
        {"package": _npm_pkg(date="2026-08-10T06:00:01Z")}, PERIOD_START, PERIOD_END
    )
    assert at_start is not None and at_end is not None
    assert before is None and after is None


@pytest.mark.parametrize(
    "obj",
    [
        {},  # pas de package
        {"package": "not-a-dict"},
        {"package": _npm_pkg(date="")},  # date vide
        {"package": _npm_pkg(date="pas-une-date")},
        {"package": _npm_pkg(date=None)},
    ],
)
def test_npm_map_object_rejects_malformed(obj):
    assert releases._npm_map_object(obj, PERIOD_START, PERIOD_END) is None


def test_npm_map_object_absent_fields_fall_back():
    """Champs absents : repo_url "", description "", npm_package=None (name vide)."""
    item = releases._npm_map_object(
        {"package": {"date": "2026-08-05T10:00:00Z"}}, PERIOD_START, PERIOD_END
    )
    assert item["name"] == ""
    assert item["repo_url"] == ""
    assert item["description"] == ""
    assert item["npm_package"] is None
    assert item["category"] == "plugin"


def test_npm_map_object_links_fallback():
    """`links` absent ou non-dict ⇒ repo_url vide, jamais une KeyError."""
    assert (
        releases._npm_map_object(
            {"package": _npm_pkg(links="https://github.com/x")}, PERIOD_START, PERIOD_END
        )["repo_url"]
        == ""
    )
    assert (
        releases._npm_map_object(
            {"package": _npm_pkg(links={"repository": None})}, PERIOD_START, PERIOD_END
        )["repo_url"]
        == ""
    )


def test_npm_map_object_ignores_prerelease_marker_in_date():
    """Une date ISO avec offset est normalisée en UTC avant le filtre de fenêtre."""
    item = releases._npm_map_object(
        {"package": _npm_pkg(date="2026-08-05T12:00:00+02:00")}, PERIOD_START, PERIOD_END
    )
    assert item["published_at"] == datetime(2026, 8, 5, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------- _npm_fetch_pages
class _PagingClient:
    """Client npm paginé : enregistre les offsets demandés, sert les pages."""

    def __init__(self, total: int, page_sizes: list[int]):
        self.total = total
        self.page_sizes = page_sizes
        self.offsets: list[int | None] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.offsets.append((params or {}).get("from"))
        offset = (params or {}).get("from")
        if offset is None:
            objects = [
                {"package": _npm_pkg(name=f"p{offset}")} for offset in range(self.page_sizes[0])
            ]
            return FakeResponse({"total": self.total, "objects": objects})
        n = self.page_sizes[min(self.offsets.index(offset), len(self.page_sizes) - 1)]
        return FakeResponse(
            {"objects": [{"package": _npm_pkg(name=f"p{offset}-{i}")} for i in range(n)]}
        )


def test_npm_fetch_pages_single_page_when_total_fits():
    """total <= NPM_PAGE_SIZE : une seule requête, pas de `from`."""
    client = _PagingClient(total=10, page_sizes=[10])
    objects = releases._npm_fetch_pages(client)
    assert len(objects) == 10
    assert client.offsets == [None]


def test_npm_fetch_pages_paginates_while_total_exceeds():
    """total > page : des pages `from=250, 500, ...` jusqu'à épuisement du total."""
    client = _PagingClient(total=800, page_sizes=[3, 3, 3])
    objects = releases._npm_fetch_pages(client)
    assert client.offsets == [None, 250, 500, 750]
    assert len(objects) == 12  # 3 (page 1) + 3 pages de 3


def test_npm_fetch_pages_stops_on_empty_page():
    """Une page vide interrompt la pagination (pas de boucle infinie)."""
    client = _PagingClient(total=5000, page_sizes=[3, 0])
    objects = releases._npm_fetch_pages(client)
    assert client.offsets == [None, 250]
    assert len(objects) == 3


def test_npm_fetch_pages_never_exceeds_max_rows():
    """La pagination s'arrête à NPM_MAX_ROWS même si `total` l'annonce plus grand."""
    client = _PagingClient(total=10_000, page_sizes=[1])
    releases._npm_fetch_pages(client)
    last = max(o for o in client.offsets if o is not None)
    assert last < releases.NPM_MAX_ROWS


def test_npm_fetch_pages_tolerates_malformed_payload():
    """Payload non-dict / objects non-list ⇒ liste vide, pas d'exception."""
    assert releases._npm_fetch_pages(_PagingClient(total=0, page_sizes=[0])) == []

    class Weird:
        def get(self, url, params=None, headers=None, timeout=None):
            return FakeResponse({"total": "12", "objects": "not-a-list"})

    assert releases._npm_fetch_pages(Weird()) == []


def test_npm_fetch_pages_query_constants():
    client = _PagingClient(total=0, page_sizes=[0])
    releases._npm_fetch_pages(client)
    assert client.offsets == [None]
    assert releases.NPM_PAGE_SIZE == 250
    assert releases.NPM_MAX_ROWS == 1000
    assert releases.NPM_QUERY == "keywords:opencode-plugin,opencode"
    assert releases.URL_NPM == "https://registry.npmjs.org/-/v1/search"


# --------------------------------------------------------------------- _fetch_npm
def test_fetch_npm_maps_only_in_window_objects(monkeypatch):
    """`_fetch_npm` filtre via `_npm_map_object` et saute les None."""
    objects = [
        {"package": _npm_pkg(name="in", date="2026-08-05T10:00:00Z")},
        {"package": _npm_pkg(name="out", date="2026-01-01T00:00:00Z")},
        {"bad": "object"},
        {"package": _npm_pkg(name="in2", date="2026-08-06T10:00:00Z")},
    ]
    monkeypatch.setattr(releases, "_npm_fetch_pages", lambda client: objects)

    items = releases._fetch_npm(None, PERIOD_START, PERIOD_END)

    assert [i["name"] for i in items] == ["in", "in2"]
    assert all(i["found_via"] == [S_NPM] for i in items)


# ------------------------------------------------------------------ _mcp_repo_url
@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ({"repository": "https://github.com/a/b"}, "https://github.com/a/b"),
        ({"githubUrl": "https://github.com/a/b"}, "https://github.com/a/b"),
        ({"homepage": "https://ex.test/p"}, "https://ex.test/p"),
        ({"sourceUrl": "https://ex.test/s"}, "https://ex.test/s"),
        ({"repositoryUrl": "https://ex.test/r"}, "https://ex.test/r"),
        # priorité : la première clé directe qui vaut http gagne
        (
            {"homepage": "https://h.test", "githubUrl": "https://gh.test"},
            "https://gh.test",
        ),
        # valeur non-http ignorée, on continue la liste
        ({"repository": "ftp://x", "homepage": "https://h.test"}, "https://h.test"),
        ({"remotes": [{"url": "https://r1.test"}, {"url": "https://r2.test"}]}, "https://r1.test"),
        ({"remotes": ["https://raw.test"]}, "https://raw.test"),  # remote = str
        ({"remotes": [{"url": "ftp://x"}, "https://ok.test"]}, "https://ok.test"),
        ({"remotes": "not-a-list"}, ""),
        ({"repository": 123}, ""),
        ({}, ""),
    ],
)
def test_mcp_repo_url_resolution(server, expected):
    assert releases._mcp_repo_url(server) == expected


def test_mcp_repo_url_direct_key_wins_over_remotes():
    assert (
        releases._mcp_repo_url(
            {"repository": "https://direct.test", "remotes": [{"url": "https://remote.test"}]}
        )
        == "https://direct.test"
    )


# ----------------------------------------------------------------- _mcp_map_entry
def test_mcp_map_entry_exact_item():
    item = releases._mcp_map_entry(
        _mcp_entry(
            {"name": "acme/mcp", "description": "mcp", "repository": "https://github.com/a/b"},
            _official(),
        ),
        PERIOD_START,
        PERIOD_END,
    )
    assert item == {
        "name": "acme/mcp",
        "category": "mcp-server",
        "repo_url": "https://github.com/a/b",
        "npm_package": None,
        "description": "mcp",
        "published_at": datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        "found_via": [S_MCP],
        "new_repo": False,
    }


def test_mcp_map_entry_name_falls_back_to_title():
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"title": "Titre seul"}, _official()), PERIOD_START, PERIOD_END
        )["name"]
        == "Titre seul"
    )
    assert (
        releases._mcp_map_entry(_mcp_entry({}, _official()), PERIOD_START, PERIOD_END)["name"] == ""
    )


def test_mcp_map_entry_reads_published_at_from_server_when_official_lacks_it():
    """publishedAt : `_meta.official` d'abord, puis le serveur lui-même."""
    assert releases._mcp_map_entry(
        _mcp_entry(
            {"name": "s", "publishedAt": "2026-08-06T00:00:00Z"}, _official(publishedAt=None)
        ),
        PERIOD_START,
        PERIOD_END,
    )["published_at"] == datetime(2026, 8, 6, tzinfo=UTC)


def test_mcp_map_entry_rejects_non_latest_revision():
    """isLatest False (révision non-latest) ⇒ entrée ignorée."""
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s"}, _official(isLatest=False)), PERIOD_START, PERIOD_END
        )
        is None
    )


def test_mcp_map_entry_accepts_latest_true_and_missing_flag():
    """isLatest True ou absent ⇒ conservé (seul `is False` rejette)."""
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s"}, _official(isLatest=True)), PERIOD_START, PERIOD_END
        )
        is not None
    )
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s"}, {"publishedAt": "2026-08-05T00:00:00Z"}),
            PERIOD_START,
            PERIOD_END,
        )
        is not None
    )


def test_mcp_map_entry_rejects_deleted_status():
    """Statut `deleted` — comparaison exacte, sensible à la casse."""
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s"}, _official(status="deleted")), PERIOD_START, PERIOD_END
        )
        is None
    )
    # comportement caractérisé : "DELETED" n'est PAS filtré (casse non normalisée)
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s"}, _official(status="DELETED")), PERIOD_START, PERIOD_END
        )
        is not None
    )


def test_mcp_map_entry_deleted_status_read_from_server_or_official():
    """Le statut `deleted` est lu sur le serveur comme sur `_meta.official`."""
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s", "status": "deleted"}, _official()), PERIOD_START, PERIOD_END
        )
        is None
    )
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s"}, _official(status="deleted")), PERIOD_START, PERIOD_END
        )
        is None
    )


def test_mcp_map_entry_out_of_window_and_undated():
    """Hors fenêtre (avant start) ou sans date exploitable ⇒ entrée ignorée."""
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s"}, _official(publishedAt="2026-08-03T05:59:59Z")),
            PERIOD_START,
            PERIOD_END,
        )
        is None
    )
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s"}, _official(publishedAt="2026-08-10T06:00:01Z")),
            PERIOD_START,
            PERIOD_END,
        )
        is None
    )
    assert (
        releases._mcp_map_entry(
            _mcp_entry({"name": "s"}, _official(publishedAt=None)),
            PERIOD_START,
            PERIOD_END,
        )
        is None
    )


def test_mcp_map_entry_tolerates_non_dict_shapes():
    assert releases._mcp_map_entry("not-a-dict", PERIOD_START, PERIOD_END) is None
    assert releases._mcp_map_entry({}, PERIOD_START, PERIOD_END) is None
    # entrée plate (pas de wrapper `server`) : l'entrée sert de source
    flat = {"name": "flat", "publishedAt": "2026-08-05T00:00:00Z"}
    assert releases._mcp_map_entry(flat, PERIOD_START, PERIOD_END)["name"] == "flat"
    # `_meta` non-dict : traité comme absent
    assert (
        releases._mcp_map_entry(
            {"server": {"name": "s"}, "_meta": "not-a-dict"},
            PERIOD_START,
            PERIOD_END,
        )
        is None
    )


def test_mcp_map_entry_repo_url_through_remotes():
    item = releases._mcp_map_entry(
        _mcp_entry({"name": "s", "remotes": [{"url": "https://gh.test/r"}]}, _official()),
        PERIOD_START,
        PERIOD_END,
    )
    assert item["repo_url"] == "https://gh.test/r"


# --------------------------------------------------------------------- _fetch_mcp
def test_fetch_mcp_filters_and_maps(monkeypatch):
    """`_fetch_mcp` : query figée, mapping + suppression des entrées filtrées."""
    seen: list[tuple] = []

    class Client:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.append((url, params))
            return FakeResponse(
                {
                    "servers": [
                        _mcp_entry({"name": "keep"}, _official()),
                        _mcp_entry({"name": "old"}, _official(publishedAt="2026-01-01T00:00:00Z")),
                        _mcp_entry({"name": "gone"}, _official(status="deleted")),
                        _garbage,
                    ]
                }
            )

    _garbage = "pas un objet"
    items = releases._fetch_mcp(Client(), PERIOD_START, PERIOD_END)

    assert seen == [
        (
            "https://registry.modelcontextprotocol.io/v0.1/servers",
            {"updated_since": "2026-08-03T06:00:00Z", "version": "latest"},
        )
    ]
    assert [i["name"] for i in items] == ["keep"]


def test_fetch_mcp_tolerates_malformed_payload():
    class Client:
        def get(self, url, params=None, headers=None, timeout=None):
            return FakeResponse({"servers": "not-a-list"})

    assert releases._fetch_mcp(Client(), PERIOD_START, PERIOD_END) == []

    class DictClient:
        def get(self, url, params=None, headers=None, timeout=None):
            return FakeResponse({"unexpected": True})

    assert releases._fetch_mcp(DictClient(), PERIOD_START, PERIOD_END) == []


def test_mcp_registry_constants():
    assert releases.URL_MCP == "https://registry.modelcontextprotocol.io/v0.1/servers"
