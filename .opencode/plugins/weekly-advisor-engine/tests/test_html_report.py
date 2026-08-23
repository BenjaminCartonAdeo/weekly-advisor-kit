"""Rapport HTML autonome (v6.1) — render_html_report + template self-contained."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from helpers import tzutc

from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.html_report import open_html_report, render_html_report

DATE = "2026-08-12"
PAYLOAD_RE = re.compile(r'<script type="application/json" id="weekly-payload">(.*?)</script>', re.S)


def _cfg(tmp_path: Path, **overrides) -> TelemetryConfig:
    cfg = TelemetryConfig()
    cfg.project_root = tmp_path
    cfg.html_report_dir = str(tmp_path / "html")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _ctx() -> dict:
    top_session = {
        "session_id": "ses_abc",
        "title_or_topic": "Refactor report",
        "cost_usd": 0.9,
        "total_tokens": 30000,
        "duration_seconds": 1200,
        "active_time_seconds": 600,
        "cost_per_active_minute": 0.09,
        "api_call_count": 42,
        "includes_subagents": False,
    }
    return {
        "date": DATE,
        "engine_version": "0.4.1",
        "period": {"start": f"{DATE}T00:00:00+00:00", "end": f"{DATE}T23:59:59+00:00"},
        "summary": {
            "totals": {
                "session_count": 3,
                "total_cost_usd": 1.25,
                "total_tokens": 45678,
                "cache_hit_rate": 0.75,
                "cache_read_tokens": 3000,
                "cache_write_tokens": 1000,
            },
            "selection": {"window_touched": 0},
            "warnings": [],
            "tool_usage": [
                {"tool": "read", "call_count": 12, "estimated_input_tokens": 480},
                {"tool": "edit", "call_count": 4, "estimated_input_tokens": 120},
            ],
            "skill_usage": [{"skill": "graphify", "load_count": 2, "sessions_used_in": 1}],
            "command_usage": [{"command": "optimize", "call_count": 3, "sessions_used_in": 2}],
            "cost_outliers": [],
            "top_sessions_by_cost": [top_session],
            "subagent_totals": {
                "child_session_count": 2,
                "total_cost_usd": 0.35,
                "by_agent_type": [],
            },
        },
        "daily_totals": [
            {"date": "2026-08-11", "cost_usd": 0.5, "total_tokens": 20000, "cache_hit_rate": 0.7},
            {"date": DATE, "cost_usd": 0.75, "total_tokens": 25678, "cache_hit_rate": 0.8},
        ],
        "models_top": [
            {
                "model": "claude-opus",
                "session_count": 2,
                "total_tokens": 40000,
                "total_cost_usd": 1.1,
            },
            {
                "model": "qwen3-coder",
                "session_count": 1,
                "total_tokens": 5678,
                "total_cost_usd": 0.15,
            },
        ],
        "top_sessions": [top_session],
        # set réel du ctx report_prep → exerce la sérialisation JSON défensive.
        "outliers": {"ses_outlier"},
        "insights": {
            "alerts": [
                {
                    "severity": "warning",
                    "rule": "weekly_budget_usd",
                    "threshold": 10,
                    "observed": 12,
                    "unit": "USD",
                },
                {
                    "severity": "info",
                    "rule": "cache_hit_rate_min",
                    "threshold": 0.5,
                    "observed": 0.75,
                },
            ],
            "maintenance": {"findings": []},
            "deltas": {},
        },
    }


def _payload(html: str) -> dict:
    match = PAYLOAD_RE.search(html)
    assert match, "payload JSON embarqué absent"
    return json.loads(match.group(1))


def test_render_writes_dated_and_latest_with_parsable_payload(tmp_path: Path):
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=_ctx(), quality_block=None)
    assert dated == tmp_path / "html" / f"weekly-report-{DATE}.html"
    latest = tmp_path / "html" / "weekly-report-latest.html"
    assert dated.exists() and latest.exists()
    assert dated.read_text(encoding="utf-8") == latest.read_text(encoding="utf-8")

    html = dated.read_text(encoding="utf-8")
    payload = _payload(html)
    assert payload["date"] == DATE
    assert payload["summary"]["totals"]["total_cost_usd"] == 1.25
    # set du ctx → liste triée dans le payload
    assert payload["outliers"] == ["ses_outlier"]
    # sections et libellés français présents
    for section_id in (
        "synthese",
        "vue-ensemble",
        "couts",
        "telemetrie",
        "qualitatif",
        "sante",
        "veille",
        "alertes",
    ):
        assert f'id="{section_id}"' in html
    assert "Sommaire" in html or "sommaire" in html.lower()


def test_render_disabled_empty_string_returns_none(tmp_path: Path):
    out = render_html_report(
        _cfg(tmp_path, html_report_dir="", project_root=None),
        anchor=DATE,
        ctx=_ctx(),
        quality_block=None,
    )
    assert out is None
    assert not (tmp_path / "html").exists()
    assert not (tmp_path / "reports").exists()


def test_render_default_dir_under_project_root(tmp_path: Path):
    cfg = _cfg(tmp_path, html_report_dir=None)
    dated = render_html_report(cfg, anchor=DATE, ctx=_ctx(), quality_block=None)
    assert dated == tmp_path / "reports" / "html" / f"weekly-report-{DATE}.html"
    assert dated.exists()


def test_render_without_project_root_warns_and_returns_none(tmp_path: Path, caplog):
    cfg = _cfg(tmp_path, html_report_dir=None, project_root=None)
    with caplog.at_level(logging.WARNING, logger="weekly_telemetry_aggregator.html_report"):
        out = render_html_report(cfg, anchor=DATE, ctx=_ctx(), quality_block=None)
    assert out is None
    assert any("project_root" in record.message for record in caplog.records)


def test_render_explicit_dir_expands_tilde(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = _cfg(tmp_path, html_report_dir="~/wa-html-test", project_root=None)
    dated = render_html_report(cfg, anchor=DATE, ctx=_ctx(), quality_block=None)
    assert dated == tmp_path / "wa-html-test" / f"weekly-report-{DATE}.html"
    assert dated.exists()


def test_render_iso_anchor_yields_date_filename(tmp_path: Path):
    anchor = tzutc(2026, 8, 12).isoformat()
    dated = render_html_report(_cfg(tmp_path), anchor=anchor, ctx=_ctx(), quality_block=None)
    assert dated is not None and dated.name == f"weekly-report-{DATE}.html"


def test_quality_block_xss_is_escaped(tmp_path: Path):
    block = "<script>alert(1)</script>\n\nTexte <img src=x onerror=alert(2)> suite"
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=_ctx(), quality_block=block)
    html = dated.read_text(encoding="utf-8")
    assert "<script>alert(1)" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # le bloc est paragraphisé
    assert "<p>" in html


def test_quality_block_tokens_become_badges(tmp_path: Path):
    block = "[F:ses_1#coût] Session coûteuse détectée.\n\n[M:claude-opus] et [A:revoir le budget]."
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=_ctx(), quality_block=block)
    html = dated.read_text(encoding="utf-8")
    assert '<span class="tag tag-f">F:ses_1#coût</span>' in html
    assert '<span class="tag tag-m">M:claude-opus</span>' in html
    assert '<span class="tag tag-a">A:revoir le budget</span>' in html


def test_ctx_xss_is_escaped_in_body_and_payload_stays_parsable(tmp_path: Path):
    ctx = _ctx()
    # titre malveillant : rendu dans le corps (table top_sessions) ET embarqué
    # dans le payload JSON via summary.top_sessions_by_cost.
    malicious_title = "<script>alert(1)</script>"
    ctx["top_sessions"] = [dict(ctx["top_sessions"][0], title_or_topic=malicious_title)]
    ctx["summary"]["top_sessions_by_cost"] = [
        dict(ctx["summary"]["top_sessions_by_cost"][0], title_or_topic=malicious_title)
    ]
    # item de veille (donnée GitHub externe) injecté dans le corps
    # (le template ne rend watch_items que si ecosystem est présent)
    ctx["ecosystem"] = {"new_items": [], "watch_repos": ["owner/repo"]}
    ctx["watch_items"] = [
        {
            "name": "<img src=x onerror=alert(2)>",
            "category": "veille",
            "description": "desc",
            "found_via": [],
        },
    ]
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=ctx, quality_block=None)
    assert dated is not None
    html = dated.read_text(encoding="utf-8")
    # le payload JSON embarque volontairement le ctx brut (</ neutralisé) :
    # l'exclure pour juger du corps rendu.
    body = PAYLOAD_RE.sub("", html)
    assert "<script>alert(1)" not in body
    assert "<img src=x" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "&lt;img src=x onerror=alert(2)&gt;" in body
    # intégrité des données : le payload reste du JSON fidèle au ctx
    payload = _payload(html)
    assert payload["summary"]["top_sessions_by_cost"][0]["title_or_topic"] == malicious_title


def test_render_oserror_returns_none_and_warns(tmp_path: Path, caplog):
    # un FILE à l'emplacement du dossier attendu → mkdir lève FileExistsError (OSError)
    (tmp_path / "html").write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="weekly_telemetry_aggregator.html_report"):
        out = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=_ctx(), quality_block=None)
    assert out is None
    assert any("non généré" in record.getMessage() for record in caplog.records)


def test_open_html_report_opens_browser_uri(monkeypatch, tmp_path: Path):
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda uri: opened.append(uri) or True)
    monkeypatch.delenv("WEEKLY_NO_BROWSER", raising=False)
    path = tmp_path / "weekly-report-latest.html"
    assert open_html_report(_cfg(tmp_path), path) is True
    assert opened == [path.as_uri()]


def test_open_html_report_guards(monkeypatch, tmp_path: Path):
    """Env WEEKLY_NO_BROWSER=1, open_browser=False ou path None → no-op."""
    called: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda uri: called.append(uri))
    monkeypatch.setenv("WEEKLY_NO_BROWSER", "1")
    assert open_html_report(_cfg(tmp_path), tmp_path / "x.html") is False
    monkeypatch.delenv("WEEKLY_NO_BROWSER")
    assert open_html_report(_cfg(tmp_path, open_browser=False), tmp_path / "x.html") is False
    assert open_html_report(_cfg(tmp_path), None) is False  # rendu précédent en échec
    assert called == []


def test_open_html_report_never_fatal(monkeypatch, tmp_path: Path, caplog):
    def boom(uri: str) -> bool:
        raise RuntimeError("no display")

    monkeypatch.setattr("webbrowser.open", boom)
    monkeypatch.delenv("WEEKLY_NO_BROWSER", raising=False)
    with caplog.at_level(logging.WARNING, logger="weekly_telemetry_aggregator.html_report"):
        assert open_html_report(_cfg(tmp_path), tmp_path / "x.html") is False
    assert any("navigateur" in record.getMessage() for record in caplog.records)
