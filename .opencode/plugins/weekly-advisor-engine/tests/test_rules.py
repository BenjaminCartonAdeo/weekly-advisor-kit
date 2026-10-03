"""Declarative rule loader + pipeline (P1) — loader, DSL, extends, test runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from weekly_telemetry_aggregator.rule_loader import (
    Rule,
    RuleError,
    apply_extends,
    load_rule,
    load_rules,
    parse_fenced_blocks,
    parse_frontmatter,
    parse_sections,
    parse_yaml,
)
from weekly_telemetry_aggregator.rule_pipeline import (
    evaluate_rule,
    evaluate_rules,
    iter_rule_tests,
    render_template,
    run_rule_test,
)

RULES = load_rules()
RULE_TESTS = [(rule, case) for rule in RULES for case in iter_rule_tests([rule])]


# --------------------------------------------------------------------------- #
# Fixtures limited to this module (no shared helper mutation)
# --------------------------------------------------------------------------- #
def _make_rule(
    rule_id: str = "test-rule",
    *,
    detect: dict,
    severity: str = "low",
    group: str = "test",
    scope: str = "requests",
    thresholds: dict | None = None,
    patterns: dict | None = None,
    file_types: list[str] | None = None,
    description: str = "",
    when_triggered: str = "",
    how_to_improve: str = "",
    tests: list[dict] | None = None,
) -> Rule:
    return Rule(
        id=rule_id,
        name=rule_id,
        group=group,
        severity=severity,
        scope=scope,
        thresholds=dict(thresholds or {}),
        patterns=dict(patterns or {}),
        file_types=list(file_types or []),
        description=description,
        when_triggered=when_triggered,
        how_to_improve=how_to_improve,
        detect=dict(detect),
        tests=list(tests or []),
    )


def _write_rule(directory: Path, filename: str, body: str) -> Path:
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


_BASE_RULE = """---
id: {id}
name: {name}
group: test
severity: {severity}
scope: requests
version: "1"
thresholds:
  min_occurrences: 1
  {threshold_key}: {threshold_value}
---

# Description

base description {{{{count}}}}

```detect
scan: requests
match: contains(text, "base")
examples: first(matched, 1)
check:
  triggered: count(matched) >= 1
```
"""


# --------------------------------------------------------------------------- #
# Mini-YAML / frontmatter / sections
# --------------------------------------------------------------------------- #
def test_parse_frontmatter_scalars_lists_and_nested_maps():
    frontmatter, body = parse_frontmatter(
        "---\nid: x\nsev: high\nflags: [a, b]\ntags:\n  - one\n  - two\n"
        "thresholds:\n  min: 3\n  ratio: 0.5\n---\n\n# Description\nhello\n"
    )
    assert frontmatter["id"] == "x"
    assert frontmatter["sev"] == "high"
    assert frontmatter["flags"] == ["a", "b"]
    assert frontmatter["tags"] == ["one", "two"]
    assert frontmatter["thresholds"] == {"min": 3, "ratio": 0.5}
    assert body.startswith("\n# Description")


def test_parse_frontmatter_absent_returns_whole_body():
    frontmatter, body = parse_frontmatter("# Description\nno frontmatter\n")
    assert frontmatter == {}
    assert body.startswith("# Description")


def test_parse_yaml_quoted_hash_and_inline_mapping():
    parsed = parse_yaml('patterns:\n  ref: ["pr #", ticket]\n  env: ["dev-only"]\n')
    assert parsed == {"patterns": {"ref": ["pr #", "ticket"], "env": ["dev-only"]}}


def test_parse_sections_and_fenced_blocks():
    body = (
        "# Description\ndesc\n\n# When Triggered\ntrig\n\n# How to Improve\nfix\n"
        "```detect\nscan: requests\n```\n```test\nexpect: triggered\n```\n"
        "```test\nexpect: clean\n```\n"
    )
    sections = parse_sections(body)
    assert sections["description"] == "desc"
    assert sections["when triggered"] == "trig"
    assert sections["how to improve"] == "fix"
    blocks = parse_fenced_blocks(body)
    assert len(blocks["test"]) == 2
    assert blocks["detect"] == ["scan: requests"]


# --------------------------------------------------------------------------- #
# Shipped rules (>= 3, real ported detectors)
# --------------------------------------------------------------------------- #
def test_shipped_rules_loaded_and_sorted():
    ids = [rule.id for rule in RULES]
    assert ids == sorted(ids)
    assert len(RULES) >= 3
    assert {"anti-learning", "agent-loop", "daily-spike", "architecture-drift"} <= set(ids)
    for rule in RULES:
        assert rule.detect.get("scan"), f"{rule.id} has no scan"
        assert rule.description, f"{rule.id} has no description"


def test_shipped_rules_port_existing_thresholds():
    by_id = {rule.id: rule for rule in RULES}
    assert by_id["agent-loop"].thresholds["loop_min_repeats"] == 8
    assert by_id["daily-spike"].thresholds["z_cap"] == 10.0
    assert by_id["architecture-drift"].thresholds["drift_runs"] == 2
    assert len(by_id["anti-learning"].patterns) == 5
    assert all(rule.tests for rule in RULES)


def test_load_rule_single_file_shipped():
    rule = load_rule(RULES[0].source_path)
    assert rule.id == RULES[0].id
    assert rule.version == "1"


# --------------------------------------------------------------------------- #
# Parametrized execution of every rule's ```test block
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("rule", "case"),
    RULE_TESTS,
    ids=[f"{case.rule_id}-{case.index}" for _, case in RULE_TESTS],
)
def test_rule_blocks(rule: Rule, case):
    passed, message = run_rule_test(rule, case)
    assert passed, message


# --------------------------------------------------------------------------- #
# DSL evaluation + uniform finding
# --------------------------------------------------------------------------- #
def test_evaluate_rule_returns_uniform_finding_with_placeholders():
    rule = _make_rule(
        detect={
            "scan": "requests",
            "match": 'contains(text, "secret")',
            "aggregate": {"occurrences": "count(matched)", "pct": "ratio(count(matched), total)"},
            "examples": "first(matched, 2)",
            "check": {"triggered": "count(matched) >= thresholds.min_occurrences"},
        },
        thresholds={"min_occurrences": 1},
        description="found {{count}} ({{pct}}) secret(s)",
        when_triggered="when {{count}}",
        how_to_improve="remove secrets",
    )
    context = {
        "requests": [
            {"description": "SECRET token leaked"},
            {"description": "safe"},
            {"description": "another secret leak"},
        ]
    }
    finding = evaluate_rule(rule, context)
    assert finding is not None
    assert set(finding) == {
        "id",
        "severity",
        "group",
        "occurrences",
        "description",
        "suggestion",
        "examples",
        "details",
    }
    assert finding["occurrences"] == 2
    assert finding["description"] == "found 2 (66.7%) secret(s)"
    assert finding["details"]["metrics"]["pct"] == pytest.approx(2 / 3)
    assert finding["details"]["when_triggered"] == "when 2"
    assert finding["examples"] == ["SECRET token leaked", "another secret leak"]


def test_evaluate_rule_clean_returns_none():
    rule = _make_rule(
        detect={"scan": "requests", "match": 'contains(text, "secret")'},
        thresholds={"min_occurrences": 1},
    )
    assert evaluate_rule(rule, {"requests": [{"description": "safe"}]}) is None


def test_dsl_functions_countWhere_someWhere_matches_ratio():
    rule = _make_rule(
        detect={
            "scan": "sessions",
            "match": (
                'countWhere(sessions, "cost_usd >= thresholds.min_cost") >= 1 '
                'and someWhere(sessions, "matches(text, \\"pytest.*fixture\\")")'
            ),
            "aggregate": {"pct": "ratio(count(matched), total)"},
        },
        thresholds={"min_cost": 1.0},
        patterns={},
    )
    context = {
        "sessions": [
            {"description": "run pytest with a fixture", "cost_usd": 2.5},
            {"description": "cheap", "cost_usd": 0.1},
        ]
    }
    assert evaluate_rule(rule, context) is not None


def test_dsl_countAny_and_patterns_port():
    rule = _make_rule(
        detect={
            "scan": "findings",
            "match": "containsAny(text, patterns.secret) or containsAny(text, patterns.ref)",
        },
        patterns={"secret": ["secret", "token"], "ref": ["jira"]},
    )
    context = {"findings": {"findings": [{"description": "see JIRA-42"}]}}
    finding = evaluate_rule(rule, context)
    assert finding is not None
    assert finding["occurrences"] == 1


def test_evaluate_rules_sorted_by_id_and_deterministic():
    rules = [
        _make_rule(
            "b-rule",
            detect={"scan": "requests", "match": "true"},
        ),
        _make_rule(
            "a-rule",
            detect={"scan": "requests", "match": 'contains(text, "x")'},
        ),
    ]
    context = {"requests": [{"description": "x"}]}
    findings = evaluate_rules(rules, context)
    assert [finding["id"] for finding in findings] == ["a-rule", "b-rule"]


def test_file_types_filter_keeps_unknown_or_matching_paths():
    rule = _make_rule(
        detect={"scan": "requests", "match": "true"},
        file_types=["py"],
    )
    context = {
        "requests": [
            {"path": "a.py", "description": "x"},
            {"path": "b.md", "description": "y"},
            {"description": "no path"},
        ]
    }
    finding = evaluate_rule(rule, context)
    assert finding is not None
    assert finding["occurrences"] == 2


def test_severity_override_in_detect():
    rule = _make_rule(
        severity="low", detect={"scan": "requests", "match": "true", "severity": "high"}
    )
    finding = evaluate_rule(rule, {"requests": [{}]})
    assert finding is not None
    assert finding["severity"] == "high"


def test_render_template_count_pct_thresholds_extra():
    rendered = render_template(
        "{{count}} {{pct}} {{thresholds.limit}} {{extra.note}} {{unknown}}",
        occurrences=3,
        metrics={"pct": 0.5},
        thresholds={"limit": 8},
        extra={"note": "ok"},
    )
    assert rendered == "3 50.0% 8 ok "


def test_unknown_dsl_name_raises():
    rule = _make_rule(detect={"scan": "requests", "match": "nope"})
    with pytest.raises(RuleError):
        evaluate_rule(rule, {"requests": [{}]})


# --------------------------------------------------------------------------- #
# extends (+ layering) — governance V1, no trust state
# --------------------------------------------------------------------------- #
def test_apply_extends_plus_prefix_overrides_parent(tmp_path: Path):
    _write_rule(
        tmp_path,
        "base.md",
        _BASE_RULE.format(
            id="base", name="base", severity="low", threshold_key="limit", threshold_value=1
        ),
    )
    _write_rule(
        tmp_path,
        "child.md",
        """---
id: child
name: child
group: test
severity: high
scope: requests
version: "1"
extends: +base
thresholds:
  limit: 9
---

# Description

child description

```detect
scan: requests
match: contains(text, "child")
```
""",
    )
    rules = {rule.id: rule for rule in load_rules(tmp_path)}
    child = rules["child"]
    assert child.severity == "high"
    assert child.thresholds["limit"] == 9  # overridden
    assert child.thresholds["min_occurrences"] == 1  # inherited
    assert child.detect["match"] == 'contains(text, "child")'
    assert child.description == "child description"
    assert child.extends is None  # resolved
    assert rules["base"].thresholds["limit"] == 1


def test_apply_extends_unknown_parent_raises(tmp_path: Path):
    _write_rule(
        tmp_path,
        "orphan.md",
        _BASE_RULE.format(
            id="orphan", name="orphan", severity="low", threshold_key="limit", threshold_value=1
        )
        + "",
    )
    rule = load_rule(tmp_path / "orphan.md")
    rule.extends = "+missing"
    with pytest.raises(RuleError, match="unknown rule"):
        apply_extends([rule])


def test_apply_extends_cycle_raises(tmp_path: Path):
    first = load_rule(
        _write_rule(
            tmp_path,
            "a.md",
            _BASE_RULE.format(
                id="a", name="a", severity="low", threshold_key="limit", threshold_value=1
            ),
        )
    )
    second = load_rule(
        _write_rule(
            tmp_path,
            "b.md",
            _BASE_RULE.format(
                id="b", name="b", severity="low", threshold_key="limit", threshold_value=1
            ),
        )
    )
    first.extends = "+b"
    second.extends = "+a"
    with pytest.raises(RuleError, match="cyclic"):
        apply_extends([first, second])


# --------------------------------------------------------------------------- #
# Loader error handling
# --------------------------------------------------------------------------- #
def test_load_rule_missing_required_field_raises(tmp_path: Path):
    path = _write_rule(
        tmp_path,
        "broken.md",
        "---\nid: broken\n---\n\n```detect\nscan: requests\n```\n",
    )
    with pytest.raises(RuleError, match="missing required"):
        load_rule(path)


def test_load_rule_missing_detect_block_raises(tmp_path: Path):
    path = _write_rule(
        tmp_path,
        "nodetect.md",
        "---\nid: x\nname: x\ngroup: g\nseverity: low\nscope: requests\n---\n\n# Description\nx\n",
    )
    with pytest.raises(RuleError, match="detect"):
        load_rule(path)


def test_iter_rule_tests_rejects_bad_expect(tmp_path: Path):
    path = _write_rule(
        tmp_path,
        "badexpect.md",
        _BASE_RULE.format(
            id="bad", name="bad", severity="low", threshold_key="limit", threshold_value=1
        )
        + "\n```test\ncontext: {}\nexpect: maybe\n```\n",
    )
    rule = load_rule(path)
    with pytest.raises(RuleError, match="expect"):
        iter_rule_tests([rule])
