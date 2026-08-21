"""Configuration loading (Part 1 §6) — weekly-telemetry-config.json.

Default location: `<project_root>/weekly-telemetry-config.json`; since a config
defines project_root itself, the implicit default is `<cwd>/weekly-telemetry-config.json`
(missing → built-in defaults). Explicit `--config <path>` raises on missing file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import OUTLIER_MIN_SESSIONS

DEFAULT_CONFIG_NAME = "weekly-telemetry-config.json"

DEFAULT_HARNESS_INCLUDE_PROFILES: dict[str, tuple[str, ...]] = {
    "strict": (
        ".opencode/AGENTS.md",
        ".opencode/agents/**/*.md",
        ".opencode/commands/**/*.md",
        ".opencode/plugins/*.{js,ts,mjs,cjs}",
        ".opencode/opencode.json",
        ".opencode/opencode.jsonc",
        ".opencode/context/**/*.md",
        ".opencode/*.json",
    ),
    # Advisory is deliberately a superset: policy surfaces plus the
    # documentation surfaces that influence skill loading and guidance.
    "advisory": (
        ".opencode/AGENTS.md",
        ".opencode/agents/**/*.md",
        ".opencode/commands/**/*.md",
        ".opencode/plugins/*.{js,ts,mjs,cjs}",
        ".opencode/opencode.json",
        ".opencode/opencode.jsonc",
        ".opencode/skills/**/SKILL.md",
        ".opencode/skills/**/references/**/*.md",
        ".opencode/skills/**/examples/**/*",
        ".opencode/context/**/*.md",
        ".opencode/*.json",
    ),
}

DEFAULT_HARNESS_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".opencode/node_modules/**",
    ".opencode/plugins/weekly-advisor-engine/**",
    "**/.venv/**",
    "**/.git/**",
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/.mypy_cache/**",
    "**/.cache/**",
    "**/dist/**",
    "**/build/**",
    "**/coverage/**",
)


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
    #: part des surfaces .opencode/ hors allowlist tolérée avant alerte (v6.0.n).
    lint_coverage_min: float = 0.70
    never_loaded_runs_threshold: int = 4
    #: consecutive runs with cache_write_tokens == 0 before alerting (v5.28).
    cache_write_zero_runs: int = 2


@dataclass(slots=True)
class HarnessIncludeConfig:
    """Allowlist profiles used to build the temporary harness projection."""

    #: ``advisory`` is the safe default for this kit: policy + documentation.
    default_profile: str = "advisory"
    profiles: dict[str, list[str]] = field(
        default_factory=lambda: {
            name: list(patterns) for name, patterns in DEFAULT_HARNESS_INCLUDE_PROFILES.items()
        }
    )
    #: Extra exclusions; mandatory generated/vendor exclusions are always merged in.
    exclude_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_HARNESS_EXCLUDE_PATTERNS)
    )


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
    outlier_min_sessions: int = OUTLIER_MIN_SESSIONS
    #: relative tolerance for the parts-vs-session_v2 cross-check (v5.28).
    cross_check_tolerance_pct: float = 0.25
    #: harness-eval MINIMUM requis (v6.1.a — plancher et non pin exact : les
    #: versions supérieures sont acceptées ; la compatibilité du digest est
    #: validée par structure au chargement, spec §7).
    harness_eval_version: str = "7.9.0"
    #: harness rules excluded from top-rules/lint counts (noise reduction, v5.28).
    harness_ignored_rules: list[str] = field(default_factory=list)
    #: rules that may pass the deterministic harness remediation gate.
    harness_auto_fix_rules: list[str] = field(default_factory=list)
    #: maximum number of distinct files changed by one remediation run.
    harness_auto_fix_max_files: int = 1
    #: harness-eval input allowlist; projection is temporary and project-relative.
    harness_include: HarnessIncludeConfig = field(default_factory=HarnessIncludeConfig)
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
    #: rapport HTML autonome (v6.1) — None → défaut `<project_root>/reports/html` ;
    #: "" → génération HTML désactivée. Même sémantique que l'ancien report_dir.
    html_report_dir: str | None = None
    #: ouverture automatique du rapport HTML dans le navigateur après assemble (v6.1).
    open_browser: bool = True
    #: worktree du kit (distribution) — sync best-effort des drafts auto-rédigés (v6.0.l).
    kit_root: Path | None = None
    release_keywords: list[str] = field(
        default_factory=lambda: ["skill", "cache", "context", "compaction"]
    )
    github_min_stars: int = 5
    #: repos arbitrarily watched (owner/name), surfaced in the veille (v5.28).
    watch_repos: list[str] = field(default_factory=list)
    #: veille étendue — entrées typées (v5.30) :
    #:   {"type": "repo", "name": "owner/repo"}  → releases + commits (fenêtre)
    #:   {"type": "list", "name": "owner/repo"}  → diff du README (nouveaux liens)
    #:   {"type": "topic", "name": "topic"}      → GitHub topic search
    #:   {"type": "rss", "name": "https://..."}  → items datés d'un flux RSS/Atom
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


def apply_lookback_override(cfg: TelemetryConfig, lookback_days: int | None) -> TelemetryConfig:
    """Override de run `--lookback-days` (v6.0.b) : mutation en mémoire seulement.

    La config JSON n'est **jamais** réécrite — l'override est déduit du prompt
    utilisateur et porté par le CLI (ou l'appel API direct), jamais persisté.
    """
    if lookback_days is None:
        return cfg
    if lookback_days < 1:
        raise ValueError(f"lookback_days doit être >= 1 (reçu {lookback_days})")
    cfg.lookback_days = lookback_days
    return cfg


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

    def _get(key: str, default, cast):
        return cast(raw.get(key, default))

    cfg.lookback_days = _get("lookback_days", cfg.lookback_days, int)
    cfg.top_sessions_limit = _get("top_sessions_limit", cfg.top_sessions_limit, int)
    cfg.audit_max_sessions = _get("audit_max_sessions", cfg.audit_max_sessions, int)
    cfg.skill_similarity_min = _get("skill_similarity_min", cfg.skill_similarity_min, float)
    cfg.review_window_weeks = _get("review_window_weeks", cfg.review_window_weeks, int)
    cfg.session_outlier_z = _get("session_outlier_z", cfg.session_outlier_z, float)
    cfg.session_outlier_min_cost_usd = _get(
        "session_outlier_min_cost_usd", cfg.session_outlier_min_cost_usd, float
    )
    cfg.outlier_min_sessions = _get("outlier_min_sessions", cfg.outlier_min_sessions, int)
    cfg.cross_check_tolerance_pct = _get(
        "cross_check_tolerance_pct", cfg.cross_check_tolerance_pct, float
    )
    cfg.harness_eval_version = str(raw.get("harness_eval_version", cfg.harness_eval_version))
    cfg.harness_ignored_rules = [
        str(x) for x in raw.get("harness_ignored_rules", cfg.harness_ignored_rules)
    ]
    auto_fix_rules = raw.get("harness_auto_fix_rules", cfg.harness_auto_fix_rules)
    if isinstance(auto_fix_rules, list):
        cfg.harness_auto_fix_rules = [str(x) for x in auto_fix_rules]
    auto_fix_max_files = raw.get("harness_auto_fix_max_files", cfg.harness_auto_fix_max_files)
    if isinstance(auto_fix_max_files, int) and not isinstance(auto_fix_max_files, bool):
        cfg.harness_auto_fix_max_files = max(0, auto_fix_max_files)
    include_raw = raw.get("harness_include")
    if isinstance(include_raw, str):
        cfg.harness_include.default_profile = include_raw
    elif isinstance(include_raw, list):
        cfg.harness_include.profiles[cfg.harness_include.default_profile] = [
            str(x) for x in include_raw
        ]
    elif isinstance(include_raw, dict):
        profile = include_raw.get("default_profile")
        if isinstance(profile, str) and profile:
            cfg.harness_include.default_profile = profile
        profiles = include_raw.get("profiles")
        if isinstance(profiles, dict):
            for name, patterns in profiles.items():
                if isinstance(name, str) and isinstance(patterns, list):
                    cfg.harness_include.profiles[name] = [str(x) for x in patterns]
        patterns = include_raw.get("patterns")
        if isinstance(patterns, list):
            cfg.harness_include.profiles[cfg.harness_include.default_profile] = [
                str(x) for x in patterns
            ]
        excludes = include_raw.get("exclude_patterns", include_raw.get("excludes"))
        if isinstance(excludes, list):
            cfg.harness_include.exclude_patterns = [str(x) for x in excludes]
    _bsv = raw.get("baseline_summary_path")
    cfg.baseline_summary_path = str(_bsv) if _bsv else None
    cfg.blocks_min_words = _get("blocks_min_words", cfg.blocks_min_words, int)
    cfg.max_candidates_per_run = _get("max_candidates_per_run", cfg.max_candidates_per_run, int)
    cfg.include_subagents = _get("include_subagents", cfg.include_subagents, bool)
    cfg.fail_on_missing_telemetry = _get(
        "fail_on_missing_telemetry", cfg.fail_on_missing_telemetry, bool
    )
    cfg.exclude_active_sessions = _get("exclude_active_sessions", cfg.exclude_active_sessions, bool)
    cfg.user_prompt_repeat_min = _get("user_prompt_repeat_min", cfg.user_prompt_repeat_min, int)
    cfg.user_prompt_repeat_similarity = _get(
        "user_prompt_repeat_similarity", cfg.user_prompt_repeat_similarity, float
    )
    cfg.user_prompt_repeat_min_chars = _get(
        "user_prompt_repeat_min_chars", cfg.user_prompt_repeat_min_chars, int
    )
    cfg.github_min_stars = _get("github_min_stars", cfg.github_min_stars, int)
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
    if "html_report_dir" in raw:
        # None/absent → défaut <project_root>/reports/html ; "" explicite → désactivé.
        v = raw["html_report_dir"]
        cfg.html_report_dir = None if v is None else str(v)
    if "open_browser" in raw:
        cfg.open_browser = bool(raw["open_browser"])
    if raw.get("kit_root"):
        cfg.kit_root = _expand(raw["kit_root"])

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
    cfg.insights.lint_coverage_min = float(
        ins.get("lint_coverage_min", cfg.insights.lint_coverage_min)
    )
    cfg.insights.never_loaded_runs_threshold = int(
        ins.get("never_loaded_runs_threshold", cfg.insights.never_loaded_runs_threshold)
    )
    cfg.insights.cache_write_zero_runs = int(
        ins.get("cache_write_zero_runs", cfg.insights.cache_write_zero_runs)
    )
    return cfg
