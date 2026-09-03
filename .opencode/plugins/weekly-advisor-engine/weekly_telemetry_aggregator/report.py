"""Deterministic report rendering — report-prep + report-assemble (Partie 7).

`report-prep` renders every section that comes from JSON artefacts + git log
(deterministic, no LLM) into a draft; section 4 (qualitative findings) is left
as `<!-- QUALITY_BLOCK -->` for the agent. `report-assemble` injects the LLM
block file (`weekly-report-blocks-<date>.md`) into the draft and produces the
final `weekly-report-<date>.md`. Missing blocks => explicit placeholder, never
a silent gap.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__
from .config import TelemetryConfig
from .harness_scope import harness_digest_problems
from .html_report import open_html_report, render_html_report
from .insights import flatten_harness_findings
from .run_state import active_run_meta, resolve_active_run_dir
from .util import iso as _iso
from .util import load_json as _load_json
from .util import parse_anchor as _parse_anchor
from .util import parse_iso_ts
from .util import read_text as _load_text


def _git_log_raw(project_root: Path, *args: str) -> list[str]:
    """Lines of `git log --grep=auto-rédigé, revue hebdo <args>`; [] on any failure."""
    if project_root is None or not (project_root / ".git").exists():
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "log", "--grep=auto-rédigé, revue hebdo", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


def _git_log(project_root: Path, since_iso: str, until_iso: str | None = None) -> list[dict]:
    """Auto-redige commits inside the window: [{hash, date, subject}] (v6.0.l, E1).

    Two v6.0.l fixes: the window is now bounded by ``--until`` (previously the
    log ran from window start to *now*, so commits of later runs — even the
    current run's own draft — were counted "sur la fenêtre"); and the match is
    enforced on the **subject** in Python because ``--grep`` matches full commit
    messages (a spec/doc commit whose body mentions the phrase was counted).
    """
    args = ["--since=" + since_iso, "--format=%h|%ad|%s", "--date=short"]
    if until_iso:
        args.append("--until=" + until_iso)
    rows = []
    for line in _git_log_raw(project_root, *args):
        parts = line.split("|", 2)
        if len(parts) == 3 and "auto-rédigé, revue hebdo" in parts[2]:
            rows.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return rows


def _pending_auto_commits(project_root: Path, cutoff_iso: str) -> int:
    return len(
        [
            line
            for line in _git_log_raw(project_root, "--before=" + cutoff_iso, "--format=%H")
            if line.strip()
        ]
    )


def _self_cost_value(cfg: TelemetryConfig) -> dict | None:
    """Advisor session info {cost, tokens} for the report; None when undetectable."""
    from .costing import advisor_cost
    from .sqlite_reader import DataSourceError

    try:
        return advisor_cost(cfg)
    except DataSourceError:
        return None


def _top_models(summary: dict, limit: int = 3) -> list[dict]:
    """Top-N by cost with <5% models fused into 'autres' (spec: by code, not LLM)."""
    # v6.0.l (E9) : les lignes 0 token ET 0 coût sont des fantômes de sélection
    # (sessions aux steps vides) — elles n'apportent aucun signal au top modèles.
    models = [
        m
        for m in summary.get("by_model", [])
        if m.get("total_tokens", 0) > 0 or m.get("total_cost_usd", 0.0) > 0
    ]
    models.sort(key=lambda m: (-m.get("total_cost_usd", 0.0), m.get("model", "")))
    total_cost = sum(m.get("total_cost_usd", 0.0) for m in models)
    if total_cost <= 0:
        return models
    top, others_cost = [], 0.0
    others_tokens, others_sessions = 0, 0
    for m in models:
        share = m.get("total_cost_usd", 0.0) / total_cost
        if len(top) < limit or share >= 0.05:
            top.append(m)
        else:
            others_cost += m.get("total_cost_usd", 0.0)
            others_tokens += m.get("total_tokens", 0)
            others_sessions += m.get("session_count", 0)
    if len(models) > len(top):
        top.append(
            {
                "model": "autres",
                "total_cost_usd": round(others_cost, 6),
                "total_tokens": others_tokens,
                "session_count": others_sessions,
            }
        )
    return top


def _complete_daily(period: dict, daily: list[dict]) -> list[dict]:
    """Tous les jours de la fenêtre, zéro explicite (v5.30, 10).

    Le bucketing ne produit que les jours avec activité — le lecteur pouvait croire
    à des trous de données. On complète la période avec des entrées à zéro.
    """
    start = parse_iso_ts(period.get("start"))
    end = parse_iso_ts(period.get("end"))
    if start is None or end is None:
        return daily
    start = start.date()
    end = end.date()
    by_date = {d.get("date"): d for d in daily}
    out: list[dict] = []
    cur = start
    while cur <= end:
        out.append(
            by_date.get(
                cur.isoformat(),
                {
                    "date": cur.isoformat(),
                    "cost_usd": 0.0,
                    "total_tokens": 0,
                    "cache_hit_rate": None,
                },
            )
        )
        cur += timedelta(days=1)
    return out


def _group_warnings(warnings: list[dict]) -> list[dict]:
    """Regroupe les warnings identiques (message) — annexe lisible (v5.30, F)."""
    grouped: dict[str, dict] = {}
    for w in warnings:
        msg = w.get("message", "")
        entry = grouped.setdefault(msg, {"message": msg, "count": 0, "session_ids": []})
        entry["count"] += 1
        sid = w.get("session_id") or "global"
        if sid not in entry["session_ids"]:
            entry["session_ids"].append(sid)
    return list(grouped.values())


def _top_harness_rules(
    digest: dict | None, ignored_rules: list[str], n: int = 5
) -> list[tuple[str, int]]:
    """Top-N most-violated harness rules, ignored rules excluded."""
    ignored = set(ignored_rules)
    return Counter(
        f["rule"] for f in flatten_harness_findings(digest) if f.get("rule") not in ignored
    ).most_common(n)


def _critical_security_findings(digest: object) -> list[dict]:
    """Return critical security findings, preserving deterministic provenance."""
    if not isinstance(digest, dict):
        return []
    # Keep the source records untouched when the compact top-level schema is
    # supplied; nested harness digests still require the canonical flattening.
    top_level = digest.get("findings")
    if isinstance(top_level, list):
        findings = [finding for finding in top_level if isinstance(finding, dict)]
    else:
        findings = flatten_harness_findings(digest)
    return [
        finding
        for finding in findings
        if (
            str(finding.get("severity") or "").lower() == "critical"
            and str(finding.get("rule") or "").lower().startswith("security/")
        )
        or _is_blocking_security_rule(finding.get("rule"))
    ]


def _artifact_provenance(out: Path, date: str) -> dict[str, dict[str, object]]:
    """Describe report inputs without relying on mutable process state."""
    return validate_required_artifacts(out, date)["artifacts"]


def validate_required_artifacts(
    out: Path,
    date: str,
    *,
    html_enabled: bool = False,
    html_path: Path | None = None,
) -> dict[str, object]:
    """Validate report inputs once, with deterministic required/optional gates.

    JSON inputs are read exactly once each.  Optional upstream artefacts remain
    visible when absent, but cannot make an otherwise usable report fail.  HTML
    is a conditional artefact: callers can pass ``html_path`` after rendering
    to distinguish a renderer failure from a missing or unreadable output.
    """
    names = {
        "required": ("weekly-summary",),
        "optional": (
            "weekly-insights",
            "weekly-harness-digest",
            "weekly-ecosystem",
            "weekly-quality-findings",
            "weekly-coherence-findings",
            "skill-curate",
            "weekly-audit-candidates",
        ),
    }

    def check_json(name: str) -> dict[str, object]:
        path = out / f"{name}-{date}.json"
        status = "absent"
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
                data = json.loads(text) if text.strip() else None
                status = "present" if isinstance(data, dict) else "ill_readable"
            except (OSError, UnicodeError, json.JSONDecodeError):
                status = "ill_readable"
        return {"path": str(path), "present": status == "present", "status": status}

    required = {f"{name}-{date}.json": check_json(name) for name in names["required"]}
    optional = {f"{name}-{date}.json": check_json(name) for name in names["optional"]}
    html: dict[str, object] = {"status": "disabled", "path": None}
    if html_enabled:
        path = html_path
        html = {"status": "absent", "path": str(path) if path else None}
        if path is not None:
            try:
                if not path.is_file():
                    html["status"] = "absent"
                elif not path.read_text(encoding="utf-8").strip():
                    html["status"] = "ill_readable"
                else:
                    html["status"] = "present"
            except (OSError, UnicodeError):
                html["status"] = "ill_readable"
    required_status = "pass" if all(a["status"] == "present" for a in required.values()) else "incomplete"
    return {
        "required": required,
        "optional": optional,
        "artifacts": {**required, **optional},
        "status": required_status,
        "html": html,
    }


_BLOCKING_SECURITY_RULES = {
    "security/mcp-tool-poisoning",
    "security/unbounded-delegation",
    "security/memory-write-unscoped",
}


def _is_blocking_security_rule(rule: object) -> bool:
    """Match canonical rule ids and harness ``security/`` qualified ids."""
    normalized = str(rule or "").strip().lower()
    return normalized in _BLOCKING_SECURITY_RULES or normalized.removeprefix("security/") in {
        item.removeprefix("security/") for item in _BLOCKING_SECURITY_RULES
    }


def _gate_status(provenance: dict[str, dict[str, object]]) -> dict[str, object]:
    """Machine-readable artifact gate; missing optional inputs remain explicit."""
    artifacts = list(provenance.values())
    required = {
        name: artifact
        for name, artifact in provenance.items()
        if name.startswith("weekly-summary-")
    }
    optional = {name: artifact for name, artifact in provenance.items() if name not in required}
    return {
        "required": required,
        "optional": optional,
        "artifacts": {
            "status": "pass" if all(a["status"] == "present" for a in required.values()) else "incomplete",
            "missing": [a["path"] for a in artifacts if a["status"] == "absent"],
            "ill_readable": [a["path"] for a in artifacts if a["status"] == "ill_readable"],
        },
        "prose": {"status": "not_validated"},
        "html": {"status": "not_run"},
        "blocking_rules": sorted(_BLOCKING_SECURITY_RULES),
    }


_CURATION_TAG_ACTIONS = {"archive", "merge", "pin", "reference", "delete", "recalibrate"}


def _coherence_has_curation_signal(coherence: object) -> bool:
    """Vrai si les findings de cohérence portent ≥1 action de curation.

    Accepte un dict (champ ``curation_signal`` ou ``findings[]``) ou une liste
    (findings bruts). Défensif : toute entrée illisible est ignorée.
    """
    if not coherence:
        return False
    if isinstance(coherence, dict):
        sig = coherence.get("curation_signal")
        if isinstance(sig, list) and sig:
            return True
        # R4 emits a mapping (rather than a list) for archive candidates.  Any
        # non-empty mapping is an actionable signal; do not require a specific
        # producer shape here so report gating remains forward-compatible.
        if isinstance(sig, dict) and sig:
            return True
        findings = coherence.get("findings") or []
    elif isinstance(coherence, list):
        findings = coherence
    else:
        return False
    return any(
        isinstance(f, dict) and f.get("tag_action") in _CURATION_TAG_ACTIONS for f in findings
    )


def _coherence_findings(coherence: object) -> list[dict]:
    """Return coherence findings in one deterministic shape for all reports."""
    if isinstance(coherence, list):
        return [finding for finding in coherence if isinstance(finding, dict)]
    if isinstance(coherence, dict):
        findings = coherence.get("findings")
        if isinstance(findings, list):
            return [finding for finding in findings if isinstance(finding, dict)]
    return []


def _curation_manifest_detail(manifest: object) -> dict:
    """Normalize curation v1/v2 manifests without changing their contracts."""
    if not isinstance(manifest, dict):
        return {"decisions": [], "skipped_details": [], "by_action": {}, "mode": None}
    decisions = manifest.get("decisions")
    skipped = manifest.get("skipped_details")
    summary = manifest.get("summary")
    normalized_decisions = (
        [item for item in decisions if isinstance(item, dict)]
        if isinstance(decisions, list)
        else []
    )
    decision_skips = [item for item in normalized_decisions if item.get("status") == "skipped"]
    raw_skips = (
        [item for item in skipped if isinstance(item, dict)]
        if isinstance(skipped, list)
        else decision_skips
    )
    # v2 carries skipped decisions in both arrays. Keep one rendered row.
    seen_skips: set[tuple[object, ...]] = set()
    skipped_details = []
    for item in raw_skips:
        key = tuple(
            item.get(field) for field in ("skill_id", "action", "source", "reason", "status")
        )
        if key not in seen_skips:
            seen_skips.add(key)
            skipped_details.append(item)
    by_action = summary.get("by_action") if isinstance(summary, dict) else None
    if not isinstance(by_action, dict):
        by_action = dict(
            sorted(Counter(str(item.get("action") or "") for item in normalized_decisions).items())
        )
    return {
        "decisions": normalized_decisions,
        "skipped_details": skipped_details,
        "by_action": by_action,
        "mode": manifest.get("mode"),
        "dry_run": manifest.get("dry_run"),
    }


def build_report_context(cfg: TelemetryConfig, *, anchor: str | None = None) -> dict | None:
    """Construit le ctx Jinja du rapport (v6.1) — partagé par prep et assemble.

    Reconstruit intégralement depuis les artefacts JSON du run actif à chaque
    appel : prep et assemble tournent comme sous-commandes CLI séparées, il n'y
    a donc aucune persistance inter-process. Retourne None si la summary du run
    est absente (le rapport HTML est alors silencieusement ignoré).
    """
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)

    summary = _load_json(out / f"weekly-summary-{date}.json")
    if summary is None:
        return None

    insights = _load_json(out / f"weekly-insights-{date}.json")
    digest = _load_json(out / f"weekly-harness-digest-{date}.json")
    for digest_problem in harness_digest_problems(digest):
        print(f"report: WARNING: {digest_problem}", file=sys.stderr, flush=True)
        digest = None
    ecosystem = _load_json(out / f"weekly-ecosystem-{date}.json")
    findings = _load_json(out / f"weekly-quality-findings-{date}.json")
    coherence_findings = _load_json(out / f"weekly-coherence-findings-{date}.json")
    skill_curate = _load_json(out / f"skill-curate-{date}.json")

    git_commits = _git_log(
        cfg.project_root,
        _iso(run_time - timedelta(hours=cfg.window_hours())),
        _iso(run_time),
    )
    pending = _pending_auto_commits(
        cfg.project_root,
        _iso(run_time - timedelta(weeks=cfg.review_window_weeks)),
    )
    # v6.0.l (E11) : delta par règle vs run précédent (null en first-run).
    lint_delta = ((insights or {}).get("deltas") or {}).get("lint_violations_delta_by_rule") or {}

    provenance = {
        "anchor": run_time.isoformat(),
        "artifact_inputs": _artifact_provenance(out, date),
        "run_provenance": summary.get("run_provenance"),
    }
    ctx = {
        "date": date,
        "engine_version": __version__,
        "period": summary.get("period", {}),
        "summary": summary,
        "insights": insights,
        "digest": digest,
        "ecosystem": ecosystem,
        "findings": findings,
        "models_top": _top_models(summary),
        "top_sessions": summary.get("top_sessions_by_cost", []),
        "harness_ignored_rules": list(cfg.harness_ignored_rules),
        "harness_top_rules": [
            {
                "rule": rule,
                "count": count,
                "delta": lint_delta.get(rule),
            }
            for rule, count in _top_harness_rules(digest, cfg.harness_ignored_rules)
        ],
        "cost_outliers_state": summary.get("cost_outliers_state", "computed"),
        "outliers": {o["session_id"] for o in summary.get("cost_outliers", [])},
        "audit_candidates": _load_json(out / f"weekly-audit-candidates-{date}.json"),
        "audit_worker_statuses": (
            (_load_json(out / f"weekly-audit-candidates-{date}.json") or {}).get("worker_statuses", [])
        ),
        "watch_findings": _load_json(out / f"weekly-watch-findings-{date}.json"),
        "coherence_findings": coherence_findings,
        "coherence_items": _coherence_findings(coherence_findings),
        "skill_curate": skill_curate,
        "curation_detail": _curation_manifest_detail(skill_curate),
        "coherence_curation_signal": _coherence_has_curation_signal(coherence_findings),
        "graphify_state": _load_json(out / f"weekly-graphify-state-{date}.json"),
        "harness_budget": (digest or {}).get("budget"),
        "harness_triggers": (digest or {}).get("triggers"),
        "harness_dependencies": (digest or {}).get("dependencies"),
        "harness_scope": (digest or {}).get("harness_scope")
        or (digest or {}).get("harness_include"),
        "harness_counts": (digest or {}).get("harness_counts"),
        "run_dir": (active_run_meta(cfg.output_dir, date) or {}).get("run_dir"),
        "provenance": provenance,
        "gate_status": _gate_status(provenance["artifact_inputs"]),
        "harness_remediation": _load_json(out / f"weekly-harness-remediation-{date}.json"),
        "warnings_grouped": _group_warnings(summary.get("warnings", [])),
        "watch_warned": any(
            w.get("source") == "github:watch-repos" for w in (ecosystem or {}).get("warnings", [])
        ),
        "watch_items": [
            i
            for i in (ecosystem or {}).get("new_items", [])
            if any(
                fv == "github:watch-repos" or fv.startswith("watch:")
                for fv in (i.get("found_via") or [])
            )
        ],
        "daily_totals": _complete_daily(summary.get("period", {}), summary.get("daily_totals", [])),
        "auto_commits": git_commits,
        "pending_auto_commits": pending,
        "self_cost": info["cost"] if (info := _self_cost_value(cfg)) else None,
        "self_cost_tokens": (info or {}).get("tokens"),
    }
    return ctx


def report_prep(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, dict | None]:
    """Render deterministic sections into `weekly-report-draft-<date>.md`."""
    ctx = build_report_context(cfg, anchor=anchor)
    if ctx is None:
        return None, None

    date = ctx["date"]
    out = resolve_active_run_dir(cfg.output_dir, date)
    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
        autoescape=select_autoescape(("html",)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template("report_template.md.j2")
    rendered = template.render(**ctx)

    draft = out / f"weekly-report-draft-{date}.md"
    draft.write_text(rendered, encoding="utf-8")
    return draft, ctx


def report_blocks_draft(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, list[str], int]:
    """Deterministic draft of the section-4 blocks (v5.28, P5.1) — zero LLM.

    Writes `weekly-report-blocks-<date>.md` from alerts, maintenance findings,
    notable sessions and top harness rules. Explicitly flagged as an automatic
    draft requiring human review; `report-assemble` validates it like any block.
    """
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)

    summary = _load_json(out / f"weekly-summary-{date}.json")
    if summary is None:
        return None, ["summary inexistante — lancer run d'abord"], 2
    insights = _load_json(out / f"weekly-insights-{date}.json")
    digest = _load_json(out / f"weekly-harness-digest-{date}.json")
    for digest_problem in harness_digest_problems(digest):
        print(f"report: WARNING: {digest_problem}", file=sys.stderr, flush=True)
        digest = None
    quality_findings = _load_json(out / f"weekly-quality-findings-{date}.json")

    lines = [
        "# Constats qualitatifs (brouillon automatique — revue humaine requise)",
        "",
        f"*Généré le {date} par `report-blocks-draft` (déterministe, zéro LLM). "
        "Éditer puis relancer `report-assemble`, ou conserver tel quel : "
        "la section 4 du rapport restera marquée comme brouillon automatique.*",
        "",
    ]
    if insights and insights.get("alerts"):
        lines += ["## Alertes", ""]
        for a in insights["alerts"]:
            lines += [
                f"- **`{a['rule']}`** ({a['severity']}) : observé {a.get('observed')} vs seuil {a.get('threshold')}"
                f"{(' ' + str(a.get('unit'))) if a.get('unit') else ''}"
                f"{(' — ' + str(a.get('note'))) if a.get('note') else ''}",
                "",
            ]
    findings = (insights or {}).get("maintenance", {}).get("findings", [])
    if findings:
        lines += ["## Constats de maintenance", ""]
        for f in findings:
            lines += [
                f"- [{f.get('severity')}] {f.get('category')} — {f.get('description')} "
                f"{f.get('recommendation')}",
                "",
            ]
    for t in summary.get("top_sessions_by_cost", [])[:2]:
        lines += [
            f"- **{t.get('title_or_topic') or t['session_id']}** — ${t.get('cost_usd', 0.0):.4f}, "
            f"{t.get('total_tokens', 0):,} tokens, cache eff. {t.get('cache_efficiency')}, "
            f"{t.get('api_call_count')} appels API",
            "",
        ]
    if quality_findings and quality_findings.get("findings"):
        lines += ["## Constats de l'audit qualitatif (Partie 3)", ""]
        for f in quality_findings["findings"]:
            lines += [
                f"- [{f.get('severity', 'low').upper()}] {f.get('category', '?')} — "
                f"{f.get('description', '')} → {f.get('recommendation', '')}"
                f"{(' *(repris de ' + str(f.get('carried_from')) + ')*') if f.get('source') == 'carried' else ''}",
                "",
            ]
    flat = flatten_harness_findings(digest)
    if flat:
        lines += ["## Règles harness les plus violées", ""]
        for rule, count in _top_harness_rules(digest, cfg.harness_ignored_rules):
            lines += [f"- `{rule}` : {count} violation(s)", ""]
    lines += ["## Recommandations", ""]
    lines += [
        "- Revoir les alertes et constats ci-dessus ; corriger les violations harness en priorité (R4).",
        "",
    ]

    # v5.29/7b-hybride : le brouillon déterministe vit dans -auto- (filet de sécurité) ;
    # le fichier weekly-report-blocks-<date>.md est réservé à la prose LLM de l'agent.
    blocks_path = out / f"weekly-report-blocks-auto-{date}.md"
    blocks_path.write_text("\n".join(lines), encoding="utf-8")
    return blocks_path, [f"brouillon de blocs généré ({len(lines)} lignes) — à éditer si besoin"], 0


_BLOC_DETERMINISTE_SIGNATURE = "# Constats qualitatifs (brouillon automatique"


def validate_llm_blocks(text: str, findings: dict | None, insights: dict | None):
    """Garde-fous anti-hallucination du bloc 7b LLM (v5.29, hybride).

    Retourne (violations, coverage_warnings) :
    - violations → le bloc est REJETÉ, report-assemble bascule sur le brouillon
      déterministe (fallback) ;
    - coverage_warnings → le bloc est accepté mais l'annexe signale les constats
      high non cités (omission ≠ hallucination).
    Checks : zéro chiffre (spec : le bloc ne cite que catégories/sévérités) ;
    balises de source [F:ses_xxx#cat] / [M:cat] / [A:rule] résolues dans les
    entrées ; taille ≤ 60 lignes ; tout finding high doit être cité.
    """
    import re

    violations: list[str] = []
    coverage: list[str] = []

    # les balises [F:...]/[M:...]/[A:...] portent des ids/session_ids (chiffres) —
    # le check chiffres ne s'applique qu'au texte visible (hors balises).
    text_no_tags = re.sub(r"\[[FMA]:[^\]]+\]", "", text)
    # chiffres autorisés hors balise : dates ISO, pourcentages, numéros de version.
    # la spec interdit toujours les chiffres « libres » (coûts, décomptes, durées).
    _ALLOWED_NUM = re.compile(
        r"\b\d{4}-\d{2}-\d{2}\b"  # date ISO
        r"|\b\d{1,3}([.,]\d+)?%"  # pourcentage
        r"|\bv?\d+\.\d+(\.\d+)?\b"  # version sémantique
    )
    text_no_allowed = _ALLOWED_NUM.sub(" ", text_no_tags)
    illegal_digits = re.findall(r"\d+", text_no_allowed)
    if illegal_digits:
        snippet = ", ".join(illegal_digits[:5])
        first_digit = re.search(r"\d+", text_no_allowed)
        line = text_no_allowed.count("\n", 0, first_digit.start()) + 1 if first_digit else 1
        violations.append(
            "chiffres interdits dans le bloc LLM (spec : catégories/sévérités "
            f"uniquement) — ligne {line}, chiffres hors date/pourcentage/version : {snippet}"
        )

    n_lines = len(text.splitlines())
    if n_lines > 60:
        violations.append(f"bloc trop long (ligne {n_lines}, {n_lines} lignes > 60)")

    findings_list = (findings or {}).get("findings", []) if findings else []
    alerts = (insights or {}).get("alerts", []) if insights else []
    maint = (insights or {}).get("maintenance", {}).get("findings", []) if insights else []

    f_refs = {f"{f.get('session_id')}#{f.get('category')}" for f in findings_list}
    m_refs = {f.get("category") for f in maint}
    a_refs = {a.get("rule") for a in alerts}
    seen_f: set[str] = set()

    # Empty source references are not merely unknown: report them as malformed so
    # the author can repair the exact traceability marker instead of guessing.
    for match in re.finditer(r"\[([FMA]):([^\]]*)\]", text):
        kind, ref = match.group(1), match.group(2)
        line = text.count("\n", 0, match.start()) + 1
        if not ref.strip():
            violations.append(f"balise de source mal formée [{kind}:] — ligne {line}")
            continue
        if kind == "F":
            if ref not in f_refs:
                violations.append(
                    f"balise inconnue [F:{ref}] — ligne {line}, aucun finding correspondant"
                )
            else:
                seen_f.add(ref)
        elif kind == "M":
            if ref not in m_refs:
                violations.append(
                    f"balise inconnue [M:{ref}] — ligne {line}, aucun constat de maintenance"
                )
        elif kind == "A" and ref not in a_refs:
            violations.append(f"balise inconnue [A:{ref}] — ligne {line}, aucune alerte")

    for f in findings_list:
        if f.get("severity") == "high":
            ref = f"{f.get('session_id')}#{f.get('category')}"
            if ref not in seen_f:
                coverage.append(f"constat high non couvert par le bloc : [F:{ref}]")

    return violations, coverage


def report_assemble(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, list[str], int]:
    """Inject the LLM blocks file into the draft → final report."""
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)
    warnings: list[str] = []
    rc = 0
    summary_for_rc = _load_json(out / f"weekly-summary-{date}.json")
    if isinstance(summary_for_rc, dict):
        try:
            rc = max(rc, int(summary_for_rc.get("rc", 0) or 0))
        except (TypeError, ValueError):
            rc = max(rc, 1)

    # Phase 4 (gate déterministe) : WAVE 2.5 REQUIRED. Si les findings de cohérence
    # portent des actions de curation mais le manifeste skill-curate est absent ->
    # alerte P0 + rc=1 (partiel, jamais fatal). Le détail P0 est rendu dans le
    # rapport via le contexte (coherence_curation_signal + skill_curate).
    coherence = _load_json(out / f"weekly-coherence-findings-{date}.json")
    # Preserve deterministic failures from upstream gates; assembling a report
    # must not turn an apply refusal into an apparent success.
    curation_manifest = _load_json(out / f"skill-curate-{date}.json")
    critical_security = _critical_security_findings(_load_json(out / f"weekly-harness-digest-{date}.json"))
    if critical_security:
        warnings.append("⚠ findings security/critical présents — rapport marqué en échec déterministe")
        rc = 1
    if isinstance(curation_manifest, dict):
        try:
            upstream_rc = int(curation_manifest.get("rc", 0) or 0)
        except (TypeError, ValueError):
            upstream_rc = 1
        rc = max(rc, upstream_rc)
    if (
        _coherence_has_curation_signal(coherence)
        and curation_manifest is None
    ):
        warnings.append(
            f"⚠ WAVE 2.5 (curation) REQUIRED : findings de cohérence porte(nt) des "
            f"actions de curation mais skill-curate-{date}.json est absent — "
            f"exécuter `weekly_skill_curate --apply` puis regénérer le rapport (P0)."
        )
        rc = 1

    draft = out / f"weekly-report-draft-{date}.md"
    text = _load_text(draft)
    if text is None:
        return (
            None,
            [
                f"draft inexistant {draft} — un assemble précédent l'a consommé/supprimé : "
                "relancer report-prep d'abord"
            ],
            2,
        )

    marker = "<!-- QUALITY_BLOCK -->"
    if marker not in text:
        return None, ["marqueur QUALITY_BLOCK absent du draft — gabarit incohérent"], 2

    # v5.29 hybride : brouillon déterministe (-auto-) toujours disponible ;
    # le fichier weekly-report-blocks-<date>.md est la prose LLM (7b), validée.
    auto_path = out / f"weekly-report-blocks-auto-{date}.md"
    llm_path = out / f"weekly-report-blocks-{date}.md"
    auto_text = _load_text(auto_path)
    llm_text = _load_text(llm_path)

    replacement: str | None = None
    status = "non disponible (placeholder)"
    if llm_text is not None:
        word_count = len(llm_text.split())
        if word_count < cfg.blocks_min_words:
            violation = (
                f"bloc LLM trop court ({word_count} mots < {cfg.blocks_min_words}) — "
                f"revoir {llm_path.name}"
            )
            status = f"brouillon automatique (bloc LLM rejeté : {violation}) — auto_draft_fallback; never validated"
            warnings.append(f"bloc LLM rejeté — fallback brouillon automatique : {violation}")
            replacement = (
                auto_text
                if auto_text is not None
                else (
                    "*Section 4 non disponible (bloc LLM rejeté et brouillon automatique absent — "
                    "lancer report-blocks-draft).*\n"
                )
            )
        else:
            findings = _load_json(out / f"weekly-quality-findings-{date}.json")
            insights = _load_json(out / f"weekly-insights-{date}.json")
            violations, coverage = validate_llm_blocks(llm_text, findings, insights)
            warnings.extend(f"bloc LLM : {v}" for v in coverage)
            if violations:
                status = f"brouillon automatique (bloc LLM rejeté : {' ; '.join(violations)}) — auto_draft_fallback; never validated"
                warnings.append(
                    f"bloc LLM rejeté — fallback brouillon automatique ({len(violations)} violation(s))"
                )
                replacement = (
                    auto_text
                    if auto_text is not None
                    else (
                        "*Section 4 non disponible (bloc LLM rejeté et brouillon automatique absent — "
                        "lancer report-blocks-draft).*\n"
                    )
                )
            else:
                status = "prose agent (7b LLM)"
                replacement = llm_text
    elif auto_text is not None:
        status = "brouillon automatique (report-blocks-draft) — auto_draft_fallback; prose absente; never validated"
        replacement = auto_text
    else:
        replacement = (
            "*Section 4 non disponible (bloc de constats absent — ni l'agent (7b) ni "
            "report-blocks-draft n'ont produit de bloc).*\n\n"
            "Lancer report-blocks-draft, ou coller les constats dans "
            "`weekly-report-blocks-<date>.md` puis relancer report-assemble."
        )
        warnings.append("bloc de constats absent — section 4 remplacée par un placeholder")

    final_text = text.replace(marker, replacement) + f"\n---\n*Statut section 4 : {status}*\n"
    final_path = out / f"weekly-report-{date}.md"
    final_path.write_text(final_text, encoding="utf-8")
    if replacement is not None:
        final_path.with_name(f"weekly-report-draft-{date}.md").unlink(missing_ok=True)

    # v6.1 : rapport HTML autonome, best-effort (échec → warning + None, jamais
    # fatal). Le ctx est reconstruit depuis les artefacts — prep et assemble
    # tournent comme sous-commandes CLI séparées — et le bloc qualité injecté
    # ci-dessus (prose LLM validée ou fallback auto) alimente la section 4.
    ctx = build_report_context(cfg, anchor=anchor)
    if ctx is not None:
        html_enabled = bool(cfg.html_report_dir)
        render_error: Exception | None = None
        try:
            html_path = render_html_report(cfg, anchor=anchor, ctx=ctx, quality_block=replacement)
        except Exception as exc:  # renderer is best-effort; gate remains deterministic
            html_path = None
            render_error = exc
        artifact_gate = validate_required_artifacts(
            out, date, html_enabled=html_enabled, html_path=html_path
        )
        ctx["gate_status"]["required"] = artifact_gate["required"]
        ctx["gate_status"]["optional"] = artifact_gate["optional"]
        ctx["gate_status"]["artifacts"] = {
            "status": artifact_gate["status"],
            "missing": [
                artifact["path"]
                for artifact in artifact_gate["artifacts"].values()
                if artifact["status"] == "absent"
            ],
            "ill_readable": [
                artifact["path"]
                for artifact in artifact_gate["artifacts"].values()
                if artifact["status"] == "ill_readable"
            ],
        }
        ctx["gate_status"]["html"] = artifact_gate["html"]
        if render_error is not None:
            ctx["gate_status"]["html"] = {
                "status": "failure",
                "path": None,
                "error": type(render_error).__name__,
            }
            warnings.append("HTML renderer failed; report artifact unavailable")
            rc = max(rc, 1)
        elif html_enabled and artifact_gate["html"]["status"] != "present":
            warnings.append(
                "HTML enabled but report artifact "
                f"{artifact_gate['html']['status']}"
            )
            rc = max(rc, 1)
        if html_path:
            open_html_report(cfg, html_path)

    # Persist machine-readable gate state alongside final report metadata.
    if ctx is not None:
        ctx["gate_status"]["prose"] = {
            "status": "validated" if status == "prose agent (7b LLM)" else "auto_draft_fallback",
            "validated": status == "prose agent (7b LLM)",
        }
        ctx["gate_status"]["blocking_rules"] = sorted(_BLOCKING_SECURITY_RULES)
        import json
        (out / f"weekly-report-gates-{date}.json").write_text(
            json.dumps(ctx["gate_status"], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return final_path, warnings, rc
