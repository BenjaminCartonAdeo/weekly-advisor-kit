"""Configuration loading (Part 1 §6) — weekly-telemetry-config.json.

Default location: `<project_root>/weekly-telemetry-config.json`; since a config
defines project_root itself, the implicit default is `<cwd>/weekly-telemetry-config.json`
(missing → built-in defaults). Explicit `--config <path>` raises on missing file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_NAME = "weekly-telemetry-config.json"


def _expand(path: str | Path) -> Path:
    return Path(path).expanduser()


@dataclass(slots=True)
class AuditConfig:
    """Part 3 session-selection thresholds (Part 1 §6)."""

    cost_per_active_minute_min: float = 0.5
    cache_efficiency_gap: float = 0.2


@dataclass(slots=True)
class InsightsConfig:
    """Part 6 alert/maintenance thresholds (Part 1 §6)."""

    weekly_budget_usd: float = 25.0
    monthly_budget_usd: float = 100.0
    daily_spike_z_min: float = 3.0
    cache_hit_rate_min: float = 0.6
    cost_wow_pct_max: float = 15.0
    lint_violations_max: int = 10
    never_loaded_runs_threshold: int = 4
    #: consecutive runs with cache_write_tokens == 0 before alerting (v5.28).
    cache_write_zero_runs: int = 2


@dataclass(slots=True)
class TelemetryConfig:
    project_root: Path | None = None
    #: "auto" → opencode.db then opencode-next.db, the live one wins (v5.24).
    opencode_db_path: str = "auto"
    lookback_days: int = 7
    output_dir: Path = field(default_factory=lambda: _expand("~/opencode-weekly-reports"))
    top_sessions_limit: int = 5
    audit_max_sessions: int = 8
    skill_similarity_min: float = 0.8
    review_window_weeks: int = 4
    session_outlier_z: float = 3.0
    session_outlier_min_cost_usd: float = 0.5
    #: below this root count, cost_outliers are statistically meaningless (v5.23, configurable v5.28).
    outlier_min_sessions: int = 15
    #: relative tolerance for the parts-vs-session_v2 cross-check (v5.28).
    cross_check_tolerance_pct: float = 0.25
    #: pinned harness-eval version (spec §7 — drift → doctor warning, v5.28).
    harness_eval_version: str = "7.9.0"
    #: harness rules excluded from top-rules/lint counts (noise reduction, v5.28).
    harness_ignored_rules: list[str] = field(default_factory=list)
    #: optional explicit previous summary for the first insights run (v5.28).
    baseline_summary_path: str | None = None
    #: minimum words in the LLM blocks file accepted by report-assemble (v5.28).
    blocks_min_words: int = 40
    max_candidates_per_run: int = 3
    include_subagents: bool = True
    fail_on_missing_telemetry: bool = False
    exclude_active_sessions: bool = True
    advisor_run_title: str = "Lance la revue hebdomadaire"
    git_name: str = "Weekly Advisor"
    git_email: str = "weekly-advisor@localhost"
    release_keywords: list[str] = field(
        default_factory=lambda: ["skill", "cache", "context", "compaction"]
    )
    github_min_stars: int = 5
    #: repos arbitrarily watched (owner/name), surfaced in the veille (v5.28).
    watch_repos: list[str] = field(default_factory=list)
    #: veille étendue — entrées typées (v5.30) :
    #:   {"type": "repo", "name": "owner/repo"}  → releases + commits (fenêtre)
    #:   {"type": "list", "name": "owner/repo"}  → diff du README (nouveaux liens)
    #:   {"type": "web",  "name": "https://..."} → diff des liens d'une page
    watch: list[dict] = field(default_factory=list)
    user_prompt_repeat_min: int = 3
    user_prompt_repeat_similarity: float = 0.9
    user_prompt_repeat_min_chars: int = 80
    opencode_version_min: str = "1.18.0"
    ignored_findings: list[str] = field(default_factory=list)
    audit: AuditConfig = field(default_factory=AuditConfig)
    insights: InsightsConfig = field(default_factory=InsightsConfig)

    def window_hours(self) -> float:
        return self.lookback_days * 24.0


def load_config(path: str | Path | None = None) -> TelemetryConfig:
    """Load config from JSON; `path=None` → `<cwd>/weekly-telemetry-config.json` if present."""
    if path is not None:
        p = _expand(path)
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        return _parse(p)
    implicit = Path.cwd() / DEFAULT_CONFIG_NAME
    if implicit.is_file():
        return _parse(implicit)
    return TelemetryConfig()


def _parse(p: Path) -> TelemetryConfig:
    raw = json.loads(p.read_text(encoding="utf-8"))
    cfg = TelemetryConfig()

    def _get_int(key: str, default: int) -> int:
        return int(raw.get(key, default))

    def _get_float(key: str, default: float) -> float:
        return float(raw.get(key, default))

    def _get_bool(key: str, default: bool) -> bool:
        return bool(raw.get(key, default))

    cfg.lookback_days = _get_int("lookback_days", cfg.lookback_days)
    cfg.top_sessions_limit = _get_int("top_sessions_limit", cfg.top_sessions_limit)
    cfg.audit_max_sessions = _get_int("audit_max_sessions", cfg.audit_max_sessions)
    cfg.skill_similarity_min = _get_float("skill_similarity_min", cfg.skill_similarity_min)
    cfg.review_window_weeks = _get_int("review_window_weeks", cfg.review_window_weeks)
    cfg.session_outlier_z = _get_float("session_outlier_z", cfg.session_outlier_z)
    cfg.session_outlier_min_cost_usd = _get_float(
        "session_outlier_min_cost_usd", cfg.session_outlier_min_cost_usd
    )
    cfg.outlier_min_sessions = _get_int("outlier_min_sessions", cfg.outlier_min_sessions)
    cfg.cross_check_tolerance_pct = _get_float(
        "cross_check_tolerance_pct", cfg.cross_check_tolerance_pct
    )
    cfg.harness_eval_version = str(raw.get("harness_eval_version", cfg.harness_eval_version))
    cfg.harness_ignored_rules = [
        str(x) for x in raw.get("harness_ignored_rules", cfg.harness_ignored_rules)
    ]
    _bsv = raw.get("baseline_summary_path")
    cfg.baseline_summary_path = str(_bsv) if _bsv else None
    cfg.blocks_min_words = _get_int("blocks_min_words", cfg.blocks_min_words)
    cfg.max_candidates_per_run = _get_int("max_candidates_per_run", cfg.max_candidates_per_run)
    cfg.include_subagents = _get_bool("include_subagents", cfg.include_subagents)
    cfg.fail_on_missing_telemetry = _get_bool(
        "fail_on_missing_telemetry", cfg.fail_on_missing_telemetry
    )
    cfg.exclude_active_sessions = _get_bool("exclude_active_sessions", cfg.exclude_active_sessions)
    cfg.user_prompt_repeat_min = _get_int("user_prompt_repeat_min", cfg.user_prompt_repeat_min)
    cfg.user_prompt_repeat_similarity = _get_float(
        "user_prompt_repeat_similarity", cfg.user_prompt_repeat_similarity
    )
    cfg.user_prompt_repeat_min_chars = _get_int(
        "user_prompt_repeat_min_chars", cfg.user_prompt_repeat_min_chars
    )
    cfg.github_min_stars = _get_int("github_min_stars", cfg.github_min_stars)
    cfg.opencode_version_min = str(raw.get("opencode_version_min", cfg.opencode_version_min))
    cfg.git_name = str(raw.get("git_name", cfg.git_name))
    cfg.git_email = str(raw.get("git_email", cfg.git_email))
    cfg.advisor_run_title = str(raw.get("advisor_run_title", cfg.advisor_run_title))
    cfg.opencode_db_path = str(raw.get("opencode_db_path", cfg.opencode_db_path))
    cfg.release_keywords = [str(x) for x in raw.get("release_keywords", cfg.release_keywords)]
    cfg.watch_repos = [str(x) for x in raw.get("watch_repos", cfg.watch_repos)]
    cfg.watch = [
        {"type": str(w.get("type", "repo")), "name": str(w.get("name", ""))}
        for w in raw.get("watch", [])
        if isinstance(w, dict) and w.get("name")
    ]
    # rétrocompat : watch_repos → entrées type repo (dédupliquées)
    for repo in cfg.watch_repos:
        if not any(w["name"] == repo for w in cfg.watch):
            cfg.watch.append({"type": "repo", "name": repo})
    cfg.ignored_findings = [str(x) for x in raw.get("ignored_findings", cfg.ignored_findings)]
    if raw.get("project_root"):
        cfg.project_root = _expand(raw["project_root"])
    if raw.get("output_dir"):
        cfg.output_dir = _expand(raw["output_dir"])

    audit = raw.get("audit") or {}
    cfg.audit.cost_per_active_minute_min = float(
        audit.get("cost_per_active_minute_min", cfg.audit.cost_per_active_minute_min)
    )
    cfg.audit.cache_efficiency_gap = float(
        audit.get("cache_efficiency_gap", cfg.audit.cache_efficiency_gap)
    )

    ins = raw.get("insights") or {}
    cfg.insights.weekly_budget_usd = float(
        ins.get("weekly_budget_usd", cfg.insights.weekly_budget_usd)
    )
    cfg.insights.monthly_budget_usd = float(
        ins.get("monthly_budget_usd", cfg.insights.monthly_budget_usd)
    )
    cfg.insights.daily_spike_z_min = float(
        ins.get("daily_spike_z_min", cfg.insights.daily_spike_z_min)
    )
    cfg.insights.cache_hit_rate_min = float(
        ins.get("cache_hit_rate_min", cfg.insights.cache_hit_rate_min)
    )
    cfg.insights.cost_wow_pct_max = float(
        ins.get("cost_wow_pct_max", cfg.insights.cost_wow_pct_max)
    )
    cfg.insights.lint_violations_max = int(
        ins.get("lint_violations_max", cfg.insights.lint_violations_max)
    )
    cfg.insights.never_loaded_runs_threshold = int(
        ins.get("never_loaded_runs_threshold", cfg.insights.never_loaded_runs_threshold)
    )
    cfg.insights.cache_write_zero_runs = int(
        ins.get("cache_write_zero_runs", cfg.insights.cache_write_zero_runs)
    )
    return cfg
