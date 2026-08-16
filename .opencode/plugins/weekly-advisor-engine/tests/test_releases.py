"""Releases/ecosystem watch (Partie 2 `releases`) with a FakeClient — no network."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from weekly_telemetry_aggregator import releases
from weekly_telemetry_aggregator.config import TelemetryConfig

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
    def __init__(self, payload, status: int = 200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Drop-in httpx-ish client: canned handler responses, dials recorded."""

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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(releases, "_gh_api", no_gh)

    def handler(url, params, headers):
        if url == URL_NPM:
            return FakeResponse({}, status=500)  # retried (backoff patched)
        if url == URL_GITHUB:
            return FakeResponse({}, status=429)  # retried
        if url == URL_MCP:
            return FakeResponse({}, status=404)  # no retry
        if url == URL_RELEASES:
            raise httpx.TransportError("connection reset")  # network error, retried
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(releases, "_gh_api", no_gh)

    def handler(url, params, headers):
        if url == URL_NPM:
            return FakeResponse({}, status=500)
        if url == URL_GITHUB:
            raise httpx.ConnectError("connection refused")
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))

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

    monkeypatch.setattr(releases, "_gh_api", fake_gh)
    handler = make_handler(
        {
            URL_WATCH_INFO: httpx.TransportError("connection reset"),
            URL_WATCH_REL: httpx.TransportError("connection reset"),
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))

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

    monkeypatch.setattr(releases, "_gh_api", fake_gh)
    handler = make_handler(
        {
            URL_GITHUB: httpx.TransportError("connection reset"),  # HTTP échoue
            URL_RELEASES: httpx.TransportError("connection reset"),
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))
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

    monkeypatch.setattr(releases, "_gh_api", fake_gh)
    client = FakeClient(lambda url, p, h: (_ for _ in ()).throw(httpx.TransportError("reset")))
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
    monkeypatch.setattr(releases, "_BACKOFF", (0.0, 0.0))

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

    monkeypatch.setattr(releases, "_gh_api", fake_gh)
    client = FakeClient(lambda url, p, h: (_ for _ in ()).throw(httpx.TransportError("reset")))
    items = releases._fetch_github_topics(
        client, "claude-code", _dt(2026, 8, 1), _dt(2026, 8, 14), 5
    )
    assert len(items) == 1
    assert items[0]["name"] == "acme/claude-tool"
    assert items[0]["found_via"] == ["github:topic:claude-code"]
    assert items[0]["new_repo"] is True
