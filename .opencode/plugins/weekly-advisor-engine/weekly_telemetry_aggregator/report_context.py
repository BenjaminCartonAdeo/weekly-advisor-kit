def build_report_context(cfg, *, anchor=None):
    import weekly_telemetry_aggregator.report as _report

    globals().update(
        {
            key: value
            for key, value in vars(_report).items()
            if not key.startswith("__") or key == "__version__"
        }
    )

    """Construit le ctx Jinja du rapport (v6.1) — partagé par prep et assemble.

    Reconstruit intégralement depuis les artefacts JSON du run actif à chaque
    appel : prep et assemble tournent comme sous-commandes CLI séparées, il n'y
    a donc aucune persistance inter-process. Retourne None si la summary du run
    est absente (le rapport HTML est alors silencieusement ignoré).
    """
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)

    artifacts = _load_report_artifacts(out, date)
    if artifacts is None:
        return None
    summary = artifacts["summary"]
    insights = artifacts["insights"]
    digest = artifacts["digest"]
    ecosystem = artifacts["ecosystem"]
    findings = artifacts["findings"]
    coherence_findings = artifacts["coherence_findings"]
    skill_curate = artifacts["skill_curate"]

    # FIX 6 : les résultats ENREGISTRÉS des appels `commit-draft` sont FUSIONNÉS
    # avec le git log, pas comptés à sa place. Borné par l'ancre de DÉBUT de run,
    # `git log` ne peut structurellement pas voir les commits que ce run vient de
    # drafter (ils sont postérieurs) : le décompte valait 0 à chaque run. Le log
    # reste par ailleurs SANS borne haute (cf. `_auto_commits` / `_pending_auto_commits`).
    timings = _load_json(out / f"weekly-timings-{date}.json")
    git_commits = _auto_commits(
        cfg.project_root,
        _iso(run_time - timedelta(hours=cfg.window_hours())),
        timings,
    )
    pending = _pending_auto_commits(
        cfg.project_root,
        _iso(run_time - timedelta(weeks=cfg.review_window_weeks)),
        timings,
    )
    head_commit = _head_commit(cfg.project_root)
    dirty_files = _dirty_files(cfg.project_root)
    # v6.0.l (E11) : delta par règle vs run précédent (null en first-run).
    lint_delta = ((insights or {}).get("deltas") or {}).get("lint_violations_delta_by_rule") or {}
    # C6 : l'écosystème est projeté (descriptions/résumés déjà tronqués sur frontière
    # de mot) AVANT le contexte, pour que le markdown et le HTML tronquent identiquement.
    eco_view = _ecosystem_view(ecosystem)
    raw_sessions = summary.get("all_sessions", [])
    if not isinstance(raw_sessions, list):
        raw_sessions = []
    all_sessions = [
        {
            **s,
            "title_short": truncate_text(
                s.get("title_or_topic") or s.get("session_id"), _SESSION_TITLE_LIMIT
            ),
        }
        if isinstance(s, dict)
        else s
        for s in raw_sessions
    ]

    provenance = {
        "anchor": run_time.isoformat(),
        "artifact_inputs": _artifact_provenance(out, date),
        "run_provenance": summary.get("run_provenance"),
    }
    # C8 : inventaire réel du run dir, une fois pour les DEUX rendus. Il est
    # dérivé après `provenance` parce qu'il se réconcilie avec les artefacts
    # déclarés par le gate — pas de liste de noms en dur dans un `.j2`.
    run_artifacts = _run_artifact_inventory(out, provenance["artifact_inputs"])
    warnings_grouped = _group_warnings(summary.get("warnings", []))
    ctx = {
        "date": date,
        "engine_version": __version__,
        "period": summary.get("period", {}),
        "summary": summary,
        "insights": insights,
        "digest": digest,
        "ecosystem": eco_view,
        "findings": findings,
        "models_top": _top_models(summary),
        "top_sessions": summary.get("top_sessions_by_cost", []),
        "all_sessions": all_sessions,
        "harness_breakdown": _harness_breakdown(summary),
        "harness_ignored_rules": list(cfg.harness_ignored_rules),
        "harness_top_rules": [
            {
                "rule": rule,
                "count": count,
                "delta": lint_delta.get(rule),
            }
            for rule, count in _top_harness_rules(digest, cfg.harness_ignored_rules)
        ],
        "top_next_steps": _top_next_steps(
            digest, insights, findings, ignored_rules=cfg.harness_ignored_rules
        ),
        "cost_outliers_state": summary.get("cost_outliers_state", "computed"),
        "outliers": {o["session_id"] for o in summary.get("cost_outliers", [])},
        "audit_candidates": artifacts["audit_candidates"],
        "audit_worker_statuses": ((artifacts["audit_candidates"] or {}).get("worker_statuses", [])),
        # C11 : section Drafting — candidats + cible hors périmètre nommée (D5).
        "draft_candidates": artifacts["draft_candidates"],
        "drafting": _drafting_view(artifacts["draft_candidates"], summary, cfg.project_root),
        "drafting_project_root": str(cfg.project_root or ""),
        "watch_findings": artifacts["watch_findings"],
        # C5/C6/C7 : la condensation et la troncature sont faites ICI, une fois pour
        # les deux rendus. Les gabarits ne portent plus ni limite en dur ni
        # déduplication : ils consomment `coherence_items` / `maint_sorted` /
        # `watch_groups` déjà projetés.
        "watch_groups": _group_watch_findings(artifacts["watch_findings"]),
        "coherence_findings": coherence_findings,
        "coherence_items": _dedupe_findings(
            _project_finding(f, evidence_limit=_EVIDENCE_LIMIT)
            for f in _coherence_findings(coherence_findings)
        ),
        "skill_curate": skill_curate,
        "curation_detail": _curation_manifest_detail(skill_curate),
        "coherence_curation_signal": _coherence_has_curation_signal(coherence_findings),
        "harness_budget": (digest or {}).get("budget"),
        "harness_triggers": (digest or {}).get("triggers"),
        "harness_dependencies": (digest or {}).get("dependencies"),
        "harness_scope": (digest or {}).get("harness_scope")
        or (digest or {}).get("harness_include"),
        "harness_counts": (digest or {}).get("harness_counts"),
        "run_dir": (active_run_meta(cfg.output_dir, date) or {}).get("run_dir"),
        "provenance": provenance,
        "gate_status": _gate_status(provenance["artifact_inputs"]),
        "harness_remediation": artifacts["harness_remediation"],
        "warnings_grouped": warnings_grouped,
        # D5 : `summary.warnings` ne porte plus les occurrences mais les ENTITÉS
        # (agrégateur regroupé) — la somme des `count` est le total réel.
        "warnings_occurrences": sum(int(g["count"]) for g in warnings_grouped),
        "run_artifacts": run_artifacts,
        "run_artifacts_total": len(run_artifacts),
        "run_artifacts_shown": min(_RUN_ARTIFACTS_LIST_LIMIT, len(run_artifacts)),
        "maint_sorted": _dedupe_findings(
            _project_finding(f, evidence_limit=_WATCH_EVIDENCE_LIMIT)
            for f in _sort_maintenance_findings(insights)
        ),
        "watch_warned": any(
            w.get("source") == "github:watch-repos" for w in (ecosystem or {}).get("warnings", [])
        ),
        "watch_items": [
            i
            for i in eco_view.get("new_items", [])
            if any(
                fv == "github:watch-repos" or fv.startswith("watch:")
                for fv in (i.get("found_via") or [])
            )
        ],
        "daily_totals": _complete_daily(summary.get("period", {}), summary.get("daily_totals", [])),
        "auto_commits": git_commits,
        "pending_auto_commits": pending,
        "head_commit": head_commit,
        "dirty_files": dirty_files,
        # E4 : une seule valeur de self-cost (artefact post-assemblage s'il
        # existe, sinon mesure live étiquetée « mesuré à l'assemblage »).
        **_self_cost_context(cfg, out, date),
    }
    return ctx
