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
            "skill_usage": [{"skill": "demo-skill", "load_count": 2, "sessions_used_in": 1}],
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


def test_render_html_reports_curation_manifest_and_required_signal(tmp_path: Path):
    """HTML exposes applied decisions, or a P0 when the manifest is absent."""
    from jinja2 import Environment

    template = (
        Path(__file__).resolve().parents[1]
        / "weekly_telemetry_aggregator"
        / "templates"
        / "report_template.html.j2"
    ).read_text(encoding="utf-8")
    start = template.index("{% if skill_curate %}")
    end = template.index("{% set inspection", start)
    block = template[start:end]
    env = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    rendered = env.from_string(block).render(
        date=DATE,
        skill_curate={
            "applied": 1,
            "proposed": 2,
            "skipped": 3,
            "decisions": [
                {
                    "action": "archive",
                    "skill_id": "old/skill",
                    "source": "coherence",
                    "reason": "stale",
                    "status": "moved",
                }
            ],
        },
        coherence_curation_signal=False,
    )
    assert "Curation (WAVE 2.5 — appliquée)" in rendered
    assert "old/skill" in rendered and "moved" in rendered
    assert "Appliquées" in rendered and "Proposées" in rendered and "Ignorées" in rendered

    required = env.from_string(block).render(
        date=DATE, skill_curate=None, coherence_curation_signal=True
    )
    assert "Curation (WAVE 2.5) requise" in required
    assert "weekly_skill_curate --apply" in required


def test_render_html_reports_skipped_decision_once_and_preserves_metadata(tmp_path: Path):
    skipped = {
        "action": "archive",
        "skill_id": "protected-skill",
        "source": "user",
        "reason": "protected",
        "status": "skipped",
    }
    ctx = _ctx()
    ctx["curation_detail"] = {
        "decisions": [skipped],
        "skipped_details": [skipped],
        "by_action": {"archive": 1},
        "mode": "dry-run",
        "dry_run": True,
    }
    ctx["skill_curate"] = {"applied": 0, "proposed": 0, "skipped": 1}
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=ctx, quality_block=None)
    assert dated is not None
    html = dated.read_text(encoding="utf-8")
    body = PAYLOAD_RE.sub("", html)
    assert body.count("protected-skill") == 1
    assert "Curation (WAVE 2.5 — dry-run — propositions)" in body
    assert "archive" in body


def test_render_html_report_end_to_end_curation_and_escaping(tmp_path: Path):
    """Render complete report and inspect its public HTML contract."""
    skipped = {
        "action": "archive",
        "skill_id": "<protected-skill>",
        "source": "user",
        "reason": "<protected>",
        "status": "skipped",
    }
    ctx = _ctx()
    ctx["top_sessions"] = [dict(ctx["top_sessions"][0], title_or_topic="<unsafe-title>")]
    ctx["curation_detail"] = {
        "decisions": [skipped],
        "skipped_details": [skipped],
        "by_action": {"archive": 1},
        "mode": "dry-run",
        "dry_run": True,
    }
    ctx["skill_curate"] = {"applied": 0, "proposed": 1, "skipped": 1}

    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=ctx, quality_block=None)
    assert dated is not None
    html = dated.read_text(encoding="utf-8")
    body = PAYLOAD_RE.sub("", html)

    assert body.count("&lt;protected-skill&gt;") == 1
    assert "<protected-skill>" not in body
    assert "&lt;unsafe-title&gt;" in body
    assert "Curation (WAVE 2.5 — dry-run — propositions)" in body
    assert "archive" in body
    assert _payload(html)["top_sessions"][0]["title_or_topic"] == "<unsafe-title>"


def test_render_explicit_dir_expands_tilde(tmp_path: Path, monkeypatch):
    # expanduser lit HOME (POSIX) mais USERPROFILE d'abord (Windows).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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


# ------------------------------------------------------------------ v6.1 snapshots : exec fold open, annexes collapsed sessionStorage, next-steps Toi/Pipeline/Agent (tag tri), passthrough best-effort


def test_html_exec_fold_open_snapshot(tmp_path: Path):
    """Snapshot exec fold : exactement 1 wrapper ouvert, KPI + next-steps + SVG présents."""
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=_ctx(), quality_block=None)
    html = dated.read_text(encoding="utf-8")
    # template brut contient une seule occurrence avec open
    tpl = (
        Path(__file__).resolve().parents[1]
        / "weekly_telemetry_aggregator"
        / "templates"
        / "report_template.html.j2"
    ).read_text(encoding="utf-8")
    assert tpl.count('<details class="exec-fold" open') == 1
    assert '<details class="exec-fold" open id="exec">' in html
    # une seule occurrence dans le rendu
    assert html.count('class="exec-fold"') == 1
    assert (
        html.count('class="exec-fold" open') == 1
        or html.count('<details class="exec-fold" open') == 1
    )
    # exec-body contenu : KPI + next-steps + daily + Top3/callback
    assert '<div class="exec-body"' in html
    assert "Prochaines actions — Top" in html
    assert "Coût quotidien" in html or "Répartition journalière" in html
    # CSS souple présent
    assert "details.exec-fold" in html
    assert "details.exec-fold[open]" in html


def test_html_annexes_collapsed_sessionStorage_snapshot(tmp_path: Path):
    """Snapshot annexes : 7 détails collapsed par défaut, JS sessionStorage wa-details-state-v2."""
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=_ctx(), quality_block=None)
    html = dated.read_text(encoding="utf-8")
    tpl = (
        Path(__file__).resolve().parents[1]
        / "weekly_telemetry_aggregator"
        / "templates"
        / "report_template.html.j2"
    ).read_text(encoding="utf-8")
    # 7 annexes A-G dans le template, toutes sans open
    assert tpl.count('<details class="annex"') == 7
    assert '<details class="annex" open' not in tpl
    # rendu idem : 7 collapsed
    assert html.count('<details class="annex"') == 7
    assert html.count('<details class="annex" open') == 0
    for annex_id in ("annex-a", "annex-b", "annex-c", "annex-d", "annex-e", "annex-f", "annex-g"):
        assert f'id="{annex_id}"' in html
    # sessionStorage : clé + restore + persist
    assert "wa-details-state-v2" in html
    assert "sessionStorage" in html
    assert "details.exec-fold, details.annex" in html
    assert "getItem" in html and "setItem" in html
    assert 'd.addEventListener("toggle"' in html or "addEventListener" in html
    # annexes-wrapper présent
    assert "annexes-wrapper" in html
    assert "Annexes — détails repliables" in html


def test_html_next_steps_grouped_snapshot(tmp_path: Path):
    """Snapshot next-steps : groupés Toi/Pipeline/Agent, ordre déterministe, fallback."""
    # html_report passthrough best-effort : le template rend dans l'ordre fourni (le tri Toi/Pipeline/Agent est fait côté report.py)
    # on fournit donc déjà trié Toi > Pipeline > Agent et le rendu doit préserver cet ordre
    ctx = _ctx()
    ctx["top_next_steps"] = [
        {
            "actor": "Toi",
            "source": "harness",
            "rule": "security/mcp-tool-poisoning",
            "severity": "high",
            "text": "Corriger `security/mcp-tool-poisoning` — 2 violation(s)",
            "detail": "2 violation(s)",
            "count": 2,
        },
        {
            "actor": "Pipeline",
            "source": "alert",
            "rule": "lint_violations_max",
            "severity": "medium",
            "text": "Alerte `lint_violations_max` — observé 2734 vs seuil 10 (medium)",
            "detail": "seuil 10",
            "count": 3,
        },
        {
            "actor": "Agent",
            "source": "audit",
            "category": "loop",
            "severity": "medium",
            "text": "Agent — loop → réduire bloat",
            "detail": "bloat",
            "count": 1,
        },
    ]
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=ctx, quality_block=None)
    html = dated.read_text(encoding="utf-8")
    # ordre Toi avant Pipeline avant Agent dans le HTML (préservé)
    toi_pos = html.index("<b>Toi</b>")
    pipe_pos = html.index("<b>Pipeline</b>")
    agent_pos = html.index("<b>Agent</b>")
    assert toi_pos < pipe_pos < agent_pos
    assert "security/mcp-tool-poisoning" in html
    assert "lint_violations_max" in html
    assert "loop" in html
    # tag tri : severity badge + source + count× présent
    assert "badge-crit" in html or "badge-warn" in html
    assert "(harness" in html
    assert "×" in html or "2×" in html

    # fallback : tns vide → 3 defaults Toi/Pipeline/Agent
    ctx2 = _ctx()
    ctx2["top_next_steps"] = []
    dated2 = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=ctx2, quality_block=None)
    html2 = PAYLOAD_RE.sub("", dated2.read_text(encoding="utf-8"))
    assert "Prochaines actions — Top 3" in html2
    assert html2.count("<b>Toi</b>") >= 1
    assert html2.count("<b>Pipeline</b>") >= 1
    assert html2.count("<b>Agent</b>") >= 1
    # fallback contient les libellés par défaut
    assert "Vérifier les alertes HIGH" in html2 or "secrets" in html2
    assert "allowlist harness" in html2
    assert "context-bloat" in html2


def test_html_next_steps_tag_tri_order_snapshot(tmp_path: Path):
    """Snapshot tag tri : sévérité > source > count > règle > acteur (déterministe) — passthrough HTML préserve l'ordre report.py."""
    # html_report est best-effort : il préserve l'ordre fourni (tri déjà fait côté report.py). On vérifie le passthrough.
    ctx = _ctx()
    ctx["top_next_steps"] = [
        {
            "actor": "Toi",
            "source": "harness",
            "rule": "security/secret",
            "severity": "high",
            "text": "high Toi",
            "detail": "x",
            "count": 1,
        },
        {
            "actor": "Pipeline",
            "source": "harness",
            "rule": "allowlist/scope",
            "severity": "medium",
            "text": "medium harness Pipeline",
            "detail": "x",
            "count": 10,
        },
        {
            "actor": "Agent",
            "source": "alert",
            "rule": "cost_wow",
            "severity": "medium",
            "text": "medium alert Agent",
            "detail": "x",
            "count": 5,
        },
        {
            "actor": "Agent",
            "source": "audit",
            "category": "context-bloat",
            "severity": "low",
            "text": "low audit",
            "detail": "x",
            "count": 1,
        },
    ]
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=ctx, quality_block=None)
    html = dated.read_text(encoding="utf-8")
    # high Toi doit apparaître avant les medium (ordre fourni trié)
    assert html.index("high Toi") < html.index("medium harness Pipeline")
    assert html.index("high Toi") < html.index("medium alert Agent")
    assert html.index("high Toi") < html.index("low audit")
    # le payload garde l'ordre fourni (le tri est côté report.py, ici on snapshot le passthrough)
    payload = _payload(html)
    assert payload["top_next_steps"][0]["actor"] == "Toi"  # ordre d'entrée préservé côté payload


def test_html_report_passthrough_best_effort_snapshot(tmp_path: Path):
    """Passthrough html_report best-effort : top_next_steps manquant/None/non-dict → template stable, jamais fatal."""
    # 1) ctx sans clé top_next_steps
    ctx_missing = _ctx()
    ctx_missing.pop("top_next_steps", None)
    dated = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=ctx_missing, quality_block=None)
    assert dated is not None and dated.exists()
    html = dated.read_text(encoding="utf-8")
    assert "Prochaines actions — Top" in html
    # le template définit tns = [] quand top_next_steps absent → fallback 3 items
    assert PAYLOAD_RE.sub("", html).count("<b>Toi</b>") >= 1

    # 2) None → normalisé en []
    ctx_none = _ctx()
    ctx_none["top_next_steps"] = None
    dated2 = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=ctx_none, quality_block=None)
    assert dated2 is not None
    assert "Prochaines actions — Top" in dated2.read_text(encoding="utf-8")
    payload2 = _payload(dated2.read_text(encoding="utf-8"))
    assert payload2.get("top_next_steps") is None  # payload garde le ctx brut, le rendu a normalisé

    # 3) [] explicite
    ctx_empty = _ctx()
    ctx_empty["top_next_steps"] = []
    dated3 = render_html_report(_cfg(tmp_path), anchor=DATE, ctx=ctx_empty, quality_block=None)
    assert dated3 is not None

    # 4) ctx non-dict : best-effort, ne doit pas lever
    dated4 = render_html_report(_cfg(tmp_path), anchor=DATE, ctx={}, quality_block=None)  # type: ignore
    assert dated4 is not None

    # 5) vérifie le template passthrough : tns = (top_next_steps if top_next_steps is defined ...) sinon []
    tpl = (
        Path(__file__).resolve().parents[1]
        / "weekly_telemetry_aggregator"
        / "templates"
        / "report_template.html.j2"
    ).read_text(encoding="utf-8")
    assert "top_next_steps if top_next_steps is defined" in tpl
    assert "top_next_steps" in tpl

    # 6) html_report.py best-effort : forward if present else [] to keep template stable
    src = (
        Path(__file__).resolve().parents[1] / "weekly_telemetry_aggregator" / "html_report.py"
    ).read_text(encoding="utf-8")
    assert "top_next_steps = ctx.get" in src
    assert "if top_next_steps is None" in src
