"""Deterministic report rendering — report-prep + report-assemble (Partie 7).

`report-prep` renders every section that comes from JSON artefacts + git log
(deterministic, no LLM) into a draft; section 4 (qualitative findings) is left
as `<!-- QUALITY_BLOCK -->` for the agent. `report-assemble` injects the LLM
block file (`weekly-report-blocks-<date>.md`) into the draft and produces the
final `weekly-report-<date>.md`. Missing blocks => explicit placeholder, never
a silent gap.
"""

from __future__ import annotations

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
        "watch_findings": _load_json(out / f"weekly-watch-findings-{date}.json"),
        "coherence_findings": _load_json(out / f"weekly-coherence-findings-{date}.json"),
        "harness_budget": (digest or {}).get("budget"),
        "harness_triggers": (digest or {}).get("triggers"),
        "harness_dependencies": (digest or {}).get("dependencies"),
        "harness_scope": (digest or {}).get("harness_scope")
        or (digest or {}).get("harness_include"),
        "harness_counts": (digest or {}).get("harness_counts"),
        "run_dir": (active_run_meta(cfg.output_dir, date) or {}).get("run_dir"),
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
        violations.append(
            "chiffres interdits dans le bloc LLM (spec : catégories/sévérités "
            f"uniquement) — chiffres hors date/pourcentage/version : {snippet}"
        )

    n_lines = len(text.splitlines())
    if n_lines > 60:
        violations.append(f"bloc trop long ({n_lines} lignes > 60)")

    findings_list = (findings or {}).get("findings", []) if findings else []
    alerts = (insights or {}).get("alerts", []) if insights else []
    maint = (insights or {}).get("maintenance", {}).get("findings", []) if insights else []

    f_refs = {f"{f.get('session_id')}#{f.get('category')}" for f in findings_list}
    m_refs = {f.get("category") for f in maint}
    a_refs = {a.get("rule") for a in alerts}
    seen_f: set[str] = set()

    for kind, ref in re.findall(r"\[([FMA]):([^\]]+)\]", text):
        if kind == "F":
            if ref not in f_refs:
                violations.append(f"balise inconnue [F:{ref}] — aucun finding correspondant")
            else:
                seen_f.add(ref)
        elif kind == "M":
            if ref not in m_refs:
                violations.append(f"balise inconnue [M:{ref}] — aucun constat de maintenance")
        elif kind == "A" and ref not in a_refs:
            violations.append(f"balise inconnue [A:{ref}] — aucune alerte")

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
            return (
                None,
                [
                    f"bloc de constats trop court ({word_count} mots < {cfg.blocks_min_words}) — "
                    f"revoir {llm_path.name}"
                ],
                2,
            )
        findings = _load_json(out / f"weekly-quality-findings-{date}.json")
        insights = _load_json(out / f"weekly-insights-{date}.json")
        violations, coverage = validate_llm_blocks(llm_text, findings, insights)
        warnings.extend(f"bloc LLM : {v}" for v in coverage)
        if violations:
            status = f"brouillon automatique (bloc LLM rejeté : {' ; '.join(violations)})"
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
        status = "brouillon automatique (report-blocks-draft)"
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
        html_path = render_html_report(cfg, anchor=anchor, ctx=ctx, quality_block=replacement)
        open_html_report(cfg, html_path)
    return final_path, warnings, 0
