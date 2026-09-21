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
    RULE_ERROR_CATEGORY,
    evaluate_rule,
    evaluate_rules,
    is_ignored,
    is_rule_ignored,
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
    sink: str | None = None,
    ignore: list[str] | None = None,
    emit: dict | None = None,
    alert: dict | None = None,
) -> Rule:
    rule = Rule(
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
    if sink is not None:
        rule.sink = sink
    if ignore is not None:
        rule.ignore = list(ignore)
    if emit is not None:
        rule.emit = dict(emit)
    if alert is not None:
        rule.alert = dict(alert)
    return rule


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
    # agent-loop scans tool arguments since the wiring epic bumped its version:
    # its semantics changed from `user_prompt_repeats` to `tool_argument_loops`,
    # so a "1" here would be a false provenance on every emitted finding.
    assert RULES[0].id == "agent-loop"
    # ... and again to "3" when it moved from the window total to the
    # intra-session peak.
    assert rule.version == "3"


# --------------------------------------------------------------------------- #
# agent-loop matches on the intra-session peak, not the window total
# --------------------------------------------------------------------------- #
def _agent_loop() -> Rule:
    return next(rule for rule in RULES if rule.id == "agent-loop")


def _loop(tool: str, total: int, peak: int) -> dict:
    return {
        "tool": tool,
        "total": total,
        "peak": peak,
        "task_threshold": 3 if tool == "task" else 8,
    }


def test_shipped_agent_loop_matches_on_peak_not_on_total():
    """13 identical calls, one per cron run: the shipped rule must stay SILENT.

    This is the whole point of the peak: the window total cannot tell "13 cron
    runs" from "13 identical calls inside one stuck session". Both are `total=13`;
    only `peak` (13 vs 1) tells them apart.
    """
    context = {"tool_argument_loops": [_loop("weekly_run", total=13, peak=1)]}
    assert evaluate_rule(_agent_loop(), context) is None


def test_shipped_agent_loop_still_fires_on_a_real_peak_and_publishes_both_numbers():
    """A real loop fires, and the evidence carries the discriminating pair."""
    context = {"tool_argument_loops": [_loop("bash", total=30, peak=27)]}
    finding = evaluate_rule(_agent_loop(), context)
    assert finding is not None
    metrics = finding["details"]["metrics"]
    assert metrics["max_peak"] == 27
    assert metrics["max_total"] == 30
    # the pair is what a reader needs: total == peak would read as a loop
    assert metrics["max_total"] != metrics["max_peak"]
    assert "27" in finding["description"] and "30" in finding["description"]


def test_shipped_agent_loop_task_threshold_applies_to_peak():
    """`task` stays at 3 repeats — of the peak, so the two thresholds both still bite."""
    assert evaluate_rule(_agent_loop(), {"tool_argument_loops": [_loop("task", 2, 2)]}) is None
    assert (
        evaluate_rule(_agent_loop(), {"tool_argument_loops": [_loop("task", total=20, peak=4)]})
        is not None
    )


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


# --------------------------------------------------------------------------- #
# min/max/sum/avg — collection-only: a scalar first argument is a loud error
# --------------------------------------------------------------------------- #
# (collection, selector) is the whole contract.  `max(a, b)` used to bind `b` to
# `selector` — evaluated against the whole rule scope, not against a second
# operand — so a scalar call answered with a wrong number and no complaint.
@pytest.mark.parametrize("function", ["max", "min", "sum", "avg"])
def test_dsl_reducer_rejects_scalar_first_argument(function: str):
    rule = _make_rule(
        detect={
            "scan": "sessions",
            "match": "true",
            "aggregate": {"worst": f"{function}(1, 2)"},
        },
    )
    with pytest.raises(RuleError, match=rf"{function}\(\) expects a collection"):
        evaluate_rule(rule, {"sessions": [{"description": "x"}]})


@pytest.mark.parametrize("function", ["max", "min", "sum", "avg"])
def test_dsl_reducer_rejects_scalar_selector_misread(function: str):
    """The silent case: a string second argument resolved as a field selector.

    Before the guard `max(3, "count(matched)")` returned the *match count* and
    `min(thresholds.n, "thresholds.cap")` returned the cap — a wrong answer, no
    exception.  The selector is evaluated against the full rule scope, so any
    resolvable expression was accepted.
    """
    rule = _make_rule(
        detect={
            "scan": "sessions",
            "match": "true",
            "aggregate": {
                "wrong": f'{function}(count(matched), "count(scanned)")',
            },
        },
    )
    with pytest.raises(RuleError, match=rf"{function}\(\) expects a collection"):
        evaluate_rule(rule, {"sessions": [{"description": "x"}]})


def test_dsl_reducer_scalar_error_names_function_type_and_arguments():
    rule = _make_rule(
        detect={"scan": "sessions", "match": "true", "aggregate": {"worst": "max(1, 2)"}},
    )
    with pytest.raises(RuleError) as excinfo:
        evaluate_rule(rule, {"sessions": [{"description": "x"}]})
    message = str(excinfo.value)
    assert "max() expects a collection" in message  # the function
    assert "got int" in message  # the offending type
    assert "(1, 2)" in message  # both offending arguments
    assert "no two-operand max()" in message  # and how to write it instead


@pytest.mark.parametrize(
    ("function", "expected"),
    [("max", 9.0), ("min", 1.0), ("sum", 10.0), ("avg", 5.0)],
)
def test_dsl_reducer_keeps_collection_selector_form(function: str, expected: float):
    """The legitimate shape is untouched: reduce a collection through a selector."""
    rule = _make_rule(
        detect={
            "scan": "sessions",
            "match": "true",
            "aggregate": {"worst": f'{function}(matched, "raw_z")'},
        },
    )
    context = {"sessions": [{"raw_z": 9.0}, {"raw_z": 1.0}]}
    finding = evaluate_rule(rule, context)
    assert finding is not None
    assert finding["details"]["metrics"]["worst"] == pytest.approx(expected)


def test_dsl_reducer_keeps_collection_without_selector():
    """`f(matched)` — no selector — still reduces a collection of scalars."""
    rule = _make_rule(
        detect={"scan": "sessions", "match": "true", "aggregate": {"total": "sum(matched)"}},
    )
    finding = evaluate_rule(rule, {"sessions": [1, 2]})
    assert finding is not None
    assert finding["details"]["metrics"]["total"] == 3


def test_dsl_reducer_still_soft_fails_when_selector_matches_nothing():
    """An empty projection is not misuse: `max` over zero values stays `None`."""
    rule = _make_rule(
        detect={
            "scan": "sessions",
            "match": "true",
            "aggregate": {"worst": 'max(matched, "absent")'},
        },
    )
    finding = evaluate_rule(rule, {"sessions": [{"raw_z": 9.0}]})
    assert finding is not None
    assert finding["details"]["metrics"]["worst"] is None


def test_shipped_daily_spike_still_reduces_its_collection_selector():
    """End-to-end on the shipped `max(matched, "raw_z")` from daily-spike."""
    spike = {rule.id: rule for rule in RULES}["daily-spike"]
    finding = evaluate_rule(spike, {"daily_spikes": [{"day": "2026-09-10", "raw_z": 11.5}]})
    assert finding is not None
    assert finding["details"]["metrics"]["max_raw_z"] == pytest.approx(11.5)
    assert finding["details"]["version"] == spike.version


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


# --------------------------------------------------------------------------- #
# Batch isolation — `evaluate_rules` is the single batch entry point
#
# The engine layer is where the guarantee lives: a rule that raises degrades to one
# `rule-error` finding and the loop keeps going. `tests/test_insights.py` pins the
# *rendering* of that finding in the artifact; these tests pin the batch contract
# itself (which findings come out, sorted, and what the diagnostic carries).
# --------------------------------------------------------------------------- #
def _broken_rule(rule_id: str = "aaa-broken") -> Rule:
    """A rule whose `aggregate` calls an unknown DSL function → `RuleError`."""
    return _make_rule(
        rule_id,
        detect={
            "scan": "requests",
            "match": "true",
            "aggregate": {"worst": "nope(1)"},
            "check": {"worst": "> 0"},
        },
    )


def _typing_broken_rule(rule_id: str = "aaa-typed") -> Rule:
    """A rule whose expression raises a raw `TypeError`, NOT a `RuleError`.

    This is not a contrived injection: `_eval_BinOp` computes `left + right`
    unguarded, so `text() + 1` on a string scope raises
    ``TypeError: can only concatenate str (not "int") to str`` straight out of
    ``evaluate_rule``. The DSL normalises `TypeError` only inside `_eval_Call`
    (a mistyped *function*), not inside arithmetic — so a plain `.md` typo reaches
    the batch as a non-`RuleError`. That gap is exactly why the isolation catches
    `Exception` and not `RuleError`.
    """
    return _make_rule(
        rule_id,
        detect={
            "scan": "requests",
            "match": "true",
            "aggregate": {"worst": "text() + 1"},
            "check": {"worst": "> 0"},
        },
    )


def test_the_typing_broken_rule_really_raises_a_non_rule_error():
    """Guard for the fixture above: it must raise `TypeError`, not `RuleError`.

    Without this, `_typing_broken_rule` could silently start raising `RuleError`
    (a DSL guard added upstream) and every isolation test below would keep passing
    while testing nothing about `except Exception`.
    """
    with pytest.raises(TypeError):
        evaluate_rule(_typing_broken_rule(), {"requests": [{"text": "a"}]})


def test_a_rule_raising_rule_error_becomes_one_rule_error_finding():
    rule = _broken_rule()
    findings = evaluate_rules([rule], {"requests": [{"description": "x"}]})

    assert len(findings) == 1
    error = findings[0]
    assert error["id"] == RULE_ERROR_CATEGORY
    assert error["severity"] == "low"
    assert error["details"]["sink"] == "findings"
    assert error["details"]["metrics"]["rule_id"] == "aaa-broken"
    assert "nope" in error["details"]["metrics"]["error"]


def test_a_rule_raising_a_non_rule_error_is_still_isolated():
    """The point of `except Exception`: a non-`RuleError` no longer kills the batch.

    `TypeError` from `text() + 1` used to escape `evaluate_rules` entirely and abort
    the caller — for production that meant the whole `insights` step, i.e. no weekly
    report at all. Now it is one `rule-error` diagnostic and the rules around it are
    evaluated as usual.
    """
    findings = evaluate_rules([_typing_broken_rule()], {"requests": [{"text": "a"}]})

    assert len(findings) == 1
    assert findings[0]["id"] == RULE_ERROR_CATEGORY
    assert "concatenate" in findings[0]["description"]


def test_the_diagnostic_reports_the_exception_type_next_to_the_message():
    """`error_type` is what makes a `TypeError` tellable apart from a `RuleError`.

    The DSL normalises most operator mistakes into `RuleError`, so the message alone
    cannot say whether the operator has a typo in their `.md` or the evaluator has a
    bug. One segment in the metrics settles it.
    """
    typed = evaluate_rules([_typing_broken_rule()], {"requests": [{"text": "a"}]})[0]
    rule_error = evaluate_rules([_broken_rule()], {"requests": [{"description": "x"}]})[0]

    assert typed["details"]["metrics"]["error_type"] == "TypeError"
    assert rule_error["details"]["metrics"]["error_type"] == "RuleError"


def test_a_rule_that_raises_does_not_contaminate_the_rules_evaluated_after_it():
    """Non-contamination, including for a non-`RuleError`: the batch keeps going.

    The broken rule is FIRST so the loop must survive it to reach the others; without
    the per-rule `try` the call raises here and the caller gets nothing.
    """
    rules = [
        _typing_broken_rule(),
        _make_rule("zzz-healthy", detect={"scan": "requests", "match": "true"}),
    ]

    findings = evaluate_rules(rules, {"requests": [{"text": "a"}]})

    assert [finding["id"] for finding in findings] == [RULE_ERROR_CATEGORY, "zzz-healthy"]


def test_each_failing_rule_gets_its_own_diagnostic_and_the_batch_stays_sorted():
    """One finding per broken rule, and the batch is still sorted by id."""
    rules = [
        _make_rule("zzz-early", detect={"scan": "requests", "match": "true"}),
        _broken_rule("bbb-broken"),
        _typing_broken_rule("aaa-typed"),
    ]

    findings = evaluate_rules(rules, {"requests": [{"text": "a"}]})

    assert [finding["id"] for finding in findings] == [
        RULE_ERROR_CATEGORY,
        RULE_ERROR_CATEGORY,
        "zzz-early",
    ]
    diagnostics = findings[:2]
    assert [finding["details"]["metrics"]["rule_id"] for finding in diagnostics] == [
        "bbb-broken",
        "aaa-typed",
    ]
    # the sort is exercised on a duplicate id too: the two diagnostics are adjacent
    # and stay distinguishable by their rule id and exception type
    assert diagnostics[0]["details"]["metrics"]["error_type"] == "RuleError"
    assert diagnostics[1]["details"]["metrics"]["error_type"] == "TypeError"


def test_an_ignored_broken_rule_is_never_evaluated_and_emits_no_diagnostic():
    """The ignore filter runs BEFORE evaluation: a suppressed rule costs nothing."""
    findings = evaluate_rules(
        [_typing_broken_rule(), _make_rule("zzz", detect={"scan": "requests", "match": "true"})],
        {"requests": [{"text": "a"}]},
        extra={"ignored": ["aaa-typed"]},
    )

    assert [finding["id"] for finding in findings] == ["zzz"]


def test_base_exceptions_are_not_swallowed_by_the_batch(monkeypatch):
    """`except Exception`, not `BaseException`: an operator Ctrl-C still stops the run.

    A cancelled run must not be reported as a broken rule — turning an abort into a
    `low` diagnostic would be a lie the operator then has to debug. Monkeypatched
    because no DSL expression reaches `BaseException`: `_eval_BinOp` still raises
    plain `TypeError` for the arithmetic case pinned above.
    """
    rule = _make_rule("boom", detect={"scan": "requests", "match": "true"})

    for exc_type in (KeyboardInterrupt, SystemExit):

        def _abort(*_args, _exc_type=exc_type, **_kwargs):
            raise _exc_type()

        monkeypatch.setattr("weekly_telemetry_aggregator.rule_pipeline.evaluate_rule", _abort)
        with pytest.raises(exc_type):
            evaluate_rules([rule], {"requests": [{"description": "x"}]})


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


# --------------------------------------------------------------------------- #
# sink — destination pile, read by the wiring cell as finding["details"]["sink"]
# --------------------------------------------------------------------------- #
def _sink_rule_md(frontmatter_extra: str = "", rule_id: str = "routed") -> str:
    return (
        f"---\nid: {rule_id}\nname: {rule_id}\ngroup: test\nseverity: low\nscope: requests\n"
        'version: "1"\n'
        f"{frontmatter_extra}"
        "---\n\n# Description\n\nrouted finding\n\n"
        '```detect\nscan: requests\nmatch: "true"\n```\n'
    )


def test_sink_defaults_to_findings_and_stays_readable():
    rule = _make_rule(detect={"scan": "requests", "match": "true"})
    assert rule.sink == "findings"
    finding = evaluate_rule(rule, {"requests": [{"description": "x"}]})
    assert finding is not None
    assert finding["details"]["sink"] == "findings"


def test_sink_read_from_frontmatter(tmp_path: Path):
    _write_rule(tmp_path, "routed.md", _sink_rule_md("sink: alerts\n"))
    rule = load_rule(tmp_path / "routed.md")
    assert rule.sink == "alerts"
    finding = evaluate_rule(rule, {"requests": [{"description": "x"}]})
    assert finding is not None
    assert finding["details"]["sink"] == "alerts"


def test_unknown_sink_in_frontmatter_raises(tmp_path: Path):
    _write_rule(tmp_path, "badsink.md", _sink_rule_md("sink: somewhere\n"))
    with pytest.raises(RuleError, match="sink"):
        load_rule(tmp_path / "badsink.md")


def test_sink_does_not_add_top_level_finding_key():
    rule = _make_rule(detect={"scan": "requests", "match": "true"})
    finding = evaluate_rule(rule, {"requests": [{"description": "x"}]})
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


# --------------------------------------------------------------------------- #
# emit — literals merged into the emitted finding
# --------------------------------------------------------------------------- #
def test_emit_merges_literals_into_finding():
    rule = _make_rule(
        detect={"scan": "requests", "match": "true"},
        emit={"recommendation_type": "agent-loop", "impact_order_of_magnitude": "medium"},
    )
    finding = evaluate_rule(rule, {"requests": [{"description": "x"}]})
    assert finding is not None
    assert finding["recommendation_type"] == "agent-loop"
    assert finding["impact_order_of_magnitude"] == "medium"
    # shallow merge: the engine-owned keys survive untouched
    assert finding["id"] == "test-rule"
    assert finding["severity"] == "low"
    assert finding["details"]["metrics"]["occurrences"] == 1


def test_emit_defaults_to_empty_and_cannot_clobber_reserved_keys():
    assert _make_rule(detect={"scan": "requests"}).emit == {}
    rule = _make_rule(
        detect={"scan": "requests", "match": "true"},
        emit={"id": "hijacked", "details": {"nope": 1}, "ok": True},
    )
    finding = evaluate_rule(rule, {"requests": [{"description": "x"}]})
    assert finding is not None
    assert finding["id"] == "test-rule"
    assert finding["details"]["name"] == "test-rule"
    assert finding["ok"] is True


def test_emit_read_from_frontmatter(tmp_path: Path):
    _write_rule(
        tmp_path,
        "routed.md",
        _sink_rule_md(
            "emit:\n  recommendation_type: agent-loop\n  impact_order_of_magnitude: medium\n"
        ),
    )
    rule = load_rule(tmp_path / "routed.md")
    assert rule.emit == {
        "recommendation_type": "agent-loop",
        "impact_order_of_magnitude": "medium",
    }


def test_emit_must_be_a_mapping(tmp_path: Path):
    _write_rule(tmp_path, "bademit.md", _sink_rule_md("emit: nope\n"))
    with pytest.raises(RuleError, match="emit"):
        load_rule(tmp_path / "bademit.md")


# --------------------------------------------------------------------------- #
# alert — rendering block of a `sink: alerts` rule, published in details
# --------------------------------------------------------------------------- #
_ALERT_BLOCK = {
    "signal": "max_raw_z",
    "threshold": "z_min",
    "cap": "z_cap",
    "rows": "spikes",
    "row_signal": "raw_z",
    "row_day": "day",
}


def test_alert_defaults_to_empty():
    assert _make_rule(detect={"scan": "requests"}).alert == {}


def test_alert_read_from_frontmatter(tmp_path: Path):
    _write_rule(
        tmp_path,
        "routed.md",
        _sink_rule_md(
            "sink: alerts\n"
            "alert:\n"
            "  signal: max_raw_z\n"
            "  threshold: z_min\n"
            "  cap: z_cap\n"
            "  rows: spikes\n"
            "  row_signal: raw_z\n"
            "  row_day: day\n"
        ),
    )
    rule = load_rule(tmp_path / "routed.md")
    assert rule.alert == _ALERT_BLOCK


def test_alert_is_published_in_details():
    rule = _make_rule(
        detect={"scan": "requests", "match": "true"},
        sink="alerts",
        alert=_ALERT_BLOCK,
    )
    finding = evaluate_rule(rule, {"requests": [{"description": "x"}]})
    assert finding is not None
    assert finding["details"]["alert"] == _ALERT_BLOCK
    # the top-level finding shape is unchanged: the block lives under `details`
    assert "alert" not in finding


def test_alert_must_be_a_mapping(tmp_path: Path):
    _write_rule(tmp_path, "badalert.md", _sink_rule_md("alert: nope\n"))
    with pytest.raises(RuleError, match="alert"):
        load_rule(tmp_path / "badalert.md")


def test_unknown_alert_key_raises(tmp_path: Path):
    # A typo'd rendering key would silently disable a behaviour (no fan-out, no
    # cap), so it is rejected rather than ignored.
    _write_rule(tmp_path, "badalert.md", _sink_rule_md("alert:\n  row_days: day\n"))
    with pytest.raises(RuleError, match="unknown `alert` key"):
        load_rule(tmp_path / "badalert.md")


def test_alert_name_is_not_a_rendering_key():
    # The alert name is derived from the rule id, never declared: a `name` key
    # would recreate the alias drift this block removes.
    from weekly_telemetry_aggregator.rule_loader import _coerce_alert

    with pytest.raises(RuleError, match="unknown `alert` key"):
        _coerce_alert({"name": "daily_spike_z_min"})


def test_shipped_daily_spike_exposes_its_alert_block():
    rules = {rule.id: rule for rule in load_rules()}
    assert rules["daily-spike"].sink == "alerts"
    assert rules["daily-spike"].alert == _ALERT_BLOCK


# --------------------------------------------------------------------------- #
# ignore — per-rule suppression driven by extra["ignored"]
# --------------------------------------------------------------------------- #
def test_is_ignored_matches_category_target_and_bare_target():
    assert is_ignored(["agent-loop:task"], "agent-loop", "task") is True
    assert is_ignored(["task"], "agent-loop", "task") is True
    assert is_ignored(["agent-loop:other"], "agent-loop", "task") is False
    assert is_ignored([], "agent-loop", "task") is False
    assert is_ignored(None, "agent-loop", "task") is False
    # a bare string is one entry, never a sequence of characters
    assert is_ignored("task", "agent-loop", "task") is True
    assert is_ignored("agent-loop", "agent-loop", "task") is False


def test_is_rule_ignored_defaults_to_the_rule_id_itself():
    rule = _make_rule("agent-loop", detect={"scan": "requests", "match": "true"})
    assert is_rule_ignored(rule, None) is False
    assert is_rule_ignored(rule, ["agent-loop:task"]) is False
    assert is_rule_ignored(rule, ["agent-loop"]) is True


def _loop_rules() -> list[Rule]:
    return [
        _make_rule(
            "agent-loop",
            detect={"scan": "requests", "match": "true"},
            ignore=["task"],
            group="agent-loop",
        ),
        _make_rule(
            "anti-learning",
            detect={"scan": "requests", "match": "true"},
            group="quality",
        ),
    ]


def test_ignore_filter_drops_rule_on_category_target_form():
    context = {"requests": [{"description": "x"}]}
    kept = evaluate_rules(_loop_rules(), context, extra={"ignored": ["agent-loop:task"]})
    assert [finding["id"] for finding in kept] == ["anti-learning"]


def test_ignore_filter_drops_rule_on_bare_target_form():
    context = {"requests": [{"description": "x"}]}
    kept = evaluate_rules(_loop_rules(), context, extra={"ignored": ["task"]})
    assert [finding["id"] for finding in kept] == ["anti-learning"]


def test_ignore_filter_drops_rule_when_its_id_is_ignored():
    context = {"requests": [{"description": "x"}]}
    assert [
        finding["id"]
        for finding in evaluate_rules(_loop_rules(), context, extra={"ignored": ["x"]})
    ] == ["agent-loop", "anti-learning"]
    kept = evaluate_rules(_loop_rules(), context, extra={"ignored": ["anti-learning"]})
    assert [finding["id"] for finding in kept] == ["agent-loop"]


def test_ignore_filter_is_a_noop_without_extra_ignored():
    context = {"requests": [{"description": "x"}]}
    for extra in (None, {}, {"note": "no ignored key"}, {"ignored": []}):
        findings = evaluate_rules(_loop_rules(), context, extra=extra)
        assert [finding["id"] for finding in findings] == ["agent-loop", "anti-learning"]


def test_ignore_from_frontmatter(tmp_path: Path):
    _write_rule(tmp_path, "routed.md", _sink_rule_md("ignore: [task, prompt]\n"))
    rule = load_rule(tmp_path / "routed.md")
    assert rule.ignore == ["task", "prompt"]
    assert rule.sink == "findings"


# --------------------------------------------------------------------------- #
# pluck — the collection -> list extractor the `targets` declaration needs
# --------------------------------------------------------------------------- #
def _pluck_rule(expression: str, *, detect_extra: dict | None = None) -> Rule:
    return _make_rule(
        detect={
            "scan": "requests",
            "match": "true",
            "aggregate": {"tools": expression},
            **(detect_extra or {}),
        }
    )


def _pluck(expression: str) -> object:
    return evaluate_rule(_pluck_rule(expression), {"requests": [{"tool": "read"}]})["details"][
        "metrics"
    ]["tools"]


def test_pluck_lists_every_value_of_the_selector():
    rule = _make_rule(
        detect={
            "scan": "requests",
            "match": "true",
            "aggregate": {"tools": 'pluck(matched, "tool")'},
        }
    )
    finding = evaluate_rule(rule, {"requests": [{"tool": "read"}, {"tool": "grep"}]})
    assert finding["details"]["metrics"]["tools"] == ["read", "grep"]


def test_pluck_is_empty_on_an_empty_collection_and_on_a_selector_no_row_has():
    # collection vide : la règle se déclenche quand même (0 match, `check` vrai)
    empty = _pluck_rule(
        'pluck(matched, "tool")',
        detect_extra={"match": "n > 100", "check": {"triggered": "true"}},
    )
    assert (
        evaluate_rule(empty, {"requests": [{"tool": "read", "n": 1}]})["details"]["metrics"][
            "tools"
        ]
        == []
    )
    # selector absent de toutes les lignes : liste vide, pas d'exception
    assert _pluck('pluck(matched, "absent")') == []


def test_pluck_rejects_a_scalar_first_argument():
    # même garde-fou que min/max/sum/avg : un scalaire en 1er argument est une
    # erreur rapportée, jamais une collection d'un seul élément.
    with pytest.raises(RuleError, match="collection"):
        _pluck('pluck(3, "tool")')


# --------------------------------------------------------------------------- #
# targets — le canal d'ignore PAR CIBLE (contre le canal par règle ci-dessus)
# --------------------------------------------------------------------------- #
def _targeted_rule(rule_id: str, scope: str, *, ignore: list[str] | None = None) -> Rule:
    """Règle qui publie les cibles de son finding (`targets` du bloc ```detect)."""
    return _make_rule(
        rule_id,
        scope=scope,
        detect={
            "scan": scope,
            "match": "true",
            "examples": 'first(matched, 3, "tool")',
            "targets": 'pluck(matched, "tool")',
        },
        ignore=ignore,
    )


def _two_scopes() -> dict:
    """`tools` porte trois cibles, `cron` n'en porte qu'une seule."""
    return {
        "tools": [{"tool": "read"}, {"tool": "weekly_run"}, {"tool": "bash"}],
        "cron": [{"tool": "weekly_run"}],
    }


def test_targets_publishes_the_targets_of_the_finding():
    finding = evaluate_rule(_targeted_rule("agent-loop", "tools"), _two_scopes())
    assert finding["details"]["targets"] == ["read", "weekly_run", "bash"]


def test_ignored_target_drops_the_finding_that_carries_it_and_keeps_the_others():
    """POINT CENTRAL : la détection n'est pas éteinte en bloc.

    Même règle, même entrée d'ignore, deux semaines : celle où la boucle ne porte
    QUE `weekly_run` disparaît, celle qui porte aussi `read` et `bash` est
    conservée. Le préfixe de catégorie compte : `"agent-loop:weekly_run"` ne dit
    rien sur les autres règles.
    """
    multi = _targeted_rule("agent-loop", "tools")
    cron_only = _targeted_rule("agent-loop", "cron")
    extra = {"ignored": ["agent-loop:weekly_run"]}

    kept = evaluate_rules([multi], _two_scopes(), extra=extra)
    dropped = evaluate_rules([cron_only], _two_scopes(), extra=extra)

    assert [finding["id"] for finding in kept] == ["agent-loop"]
    assert dropped == []


def test_multi_target_finding_survives_with_its_examples_reduced():
    findings = evaluate_rules(
        [_targeted_rule("agent-loop", "tools")],
        _two_scopes(),
        extra={"ignored": ["agent-loop:weekly_run"]},
    )

    assert len(findings) == 1
    assert findings[0]["details"]["targets"] == ["read", "bash"]
    assert findings[0]["examples"] == ["read", "bash"]


def test_bare_rule_id_still_disables_the_whole_rule_with_targets():
    assert (
        evaluate_rules(
            [_targeted_rule("agent-loop", "tools")],
            _two_scopes(),
            extra={"ignored": ["agent-loop"]},
        )
        == []
    )


def test_rule_level_ignore_still_wins_over_targets():
    """`ignore: [task]` éteint la règle ENTIÈRE, même si elle déclare ses cibles."""
    rule = _targeted_rule("agent-loop", "tools", ignore=["task"])

    findings = evaluate_rules([rule], _two_scopes(), extra={"ignored": ["agent-loop:task"]})

    assert findings == []


def test_rule_without_targets_behaves_exactly_as_before():
    """NON-RÉGRESSION : pas de `targets` déclaré = pas de filtre par cible.

    Règle identique à `_targeted_rule` moins la ligne `targets` : les exemples
    restent intacts, la cible ignorée n'y touche rien.
    """
    rule = _make_rule(
        "agent-loop",
        detect={
            "scan": "tools",
            "match": "true",
            "examples": 'first(matched, 3, "tool")',
        },
    )

    finding = evaluate_rules([rule], _two_scopes(), extra={"ignored": ["agent-loop:weekly_run"]})[0]

    assert "targets" not in finding["details"]
    assert finding["examples"] == ["read", "weekly_run", "bash"]


def test_shipped_agent_loop_declares_its_tool_targets():
    """La règle livrée publie ses cibles ET se filtre par cible."""
    rule = next(rule for rule in RULES if rule.id == "agent-loop")
    context = {
        "tool_argument_loops": [
            {"tool": "read", "total": 17, "peak": 17, "task_threshold": 8},
            {"tool": "weekly_run", "total": 13, "peak": 13, "task_threshold": 8},
            {"tool": "skill", "total": 20, "peak": 9, "task_threshold": 8},
        ]
    }

    kept = evaluate_rule(rule, context)
    muted = evaluate_rule(rule, context, extra={"ignored": ["agent-loop:weekly_run"]})

    assert kept["details"]["targets"] == ["read", "weekly_run", "skill"]
    assert muted is not None
    assert muted["details"]["targets"] == ["read", "skill"]
    assert muted["examples"] == ["read", "skill"]
    # le seuil n'a pas bougé : les trois outils sont toujours détectés
    assert muted["details"]["metrics"]["occurrences"] == 3


def test_targets_must_be_an_expression_string():
    rule = _make_rule(
        detect={"scan": "tools", "match": "true", "targets": ["read", "bash"]},
    )
    with pytest.raises(RuleError, match="targets"):
        evaluate_rule(rule, _two_scopes())


# --------------------------------------------------------------------------- #
# load_rules(overrides=) — threshold / field overrides without touching the .md
# --------------------------------------------------------------------------- #
def _override_rule_md(rule_id: str) -> str:
    return (
        f"---\nid: {rule_id}\nname: {rule_id}\ngroup: test\nseverity: low\nscope: requests\n"
        'version: "1"\nthresholds:\n  min_occurrences: 1\n  nested:\n    a: 1\n    b: 2\n'
        '---\n\n# Description\n\ndesc\n\n```detect\nscan: requests\nmatch: "true"\n```\n'
    )


def test_load_rules_overrides_deep_merge_thresholds(tmp_path: Path):
    _write_rule(tmp_path, "base.md", _override_rule_md("base"))
    rules = load_rules(
        tmp_path,
        overrides={"base": {"thresholds": {"nested": {"b": 20}, "limit": 5}}},
    )
    thresholds = rules[0].thresholds
    assert thresholds["min_occurrences"] == 1  # untouched sibling key
    assert thresholds["limit"] == 5  # added key
    assert thresholds["nested"] == {"a": 1, "b": 20}  # recursive merge, `a` kept


def test_load_rules_overrides_patch_other_fields_and_skip_unknown_keys(tmp_path: Path):
    _write_rule(tmp_path, "base.md", _override_rule_md("base"))
    rules = load_rules(
        tmp_path,
        overrides={
            "base": {
                "severity": "HIGH",
                "sink": "alerts",
                "ignore": ["task"],
                "emit": {"recommendation_type": "agent-loop"},
                "not_a_rule_field": 42,
            }
        },
    )
    rule = rules[0]
    assert rule.severity == "high"
    assert rule.sink == "alerts"
    assert rule.ignore == ["task"]
    assert rule.emit == {"recommendation_type": "agent-loop"}


def test_load_rules_overrides_ignore_unknown_id_and_invalid_values(tmp_path: Path):
    _write_rule(tmp_path, "base.md", _override_rule_md("base"))
    rules = load_rules(tmp_path, overrides={"ghost-rule": {"thresholds": {"x": 1}}})
    assert [rule.id for rule in rules] == ["base"]
    assert rules[0].thresholds == {"min_occurrences": 1, "nested": {"a": 1, "b": 2}}
    with pytest.raises(RuleError, match="sink"):
        load_rules(tmp_path, overrides={"base": {"sink": "nowhere"}})


def test_load_rules_overrides_do_not_mutate_the_source_rules(tmp_path: Path):
    _write_rule(tmp_path, "base.md", _override_rule_md("base"))
    load_rules(tmp_path, overrides={"base": {"thresholds": {"nested": {"b": 99}}}})
    assert load_rules(tmp_path)[0].thresholds == {"min_occurrences": 1, "nested": {"a": 1, "b": 2}}


def test_load_rules_default_directory_and_no_override_unchanged():
    assert load_rules() == RULES
    assert load_rules(overrides=None) == RULES
    assert load_rules(overrides={}) == RULES


def test_extends_carries_sink_ignore_and_emit(tmp_path: Path):
    _write_rule(
        tmp_path,
        "base.md",
        _sink_rule_md(
            "sink: findings\nignore: [base]\nemit:\n  from_base: 1\n  shared: base\n",
            rule_id="base",
        ),
    )
    _write_rule(
        tmp_path,
        "child.md",
        "---\nid: child\nname: child\ngroup: test\nseverity: low\nscope: requests\n"
        'version: "1"\nextends: +base\nsink: alerts\nignore: [task]\n'
        "emit:\n  shared: child\n  from_child: 2\n"
        '---\n\n# Description\n\nchild\n\n```detect\nscan: requests\nmatch: "true"\n```\n',
    )
    rules = {rule.id: rule for rule in load_rules(tmp_path)}
    assert rules["child"].sink == "alerts"
    assert rules["child"].ignore == ["task"]
    assert rules["child"].emit == {"from_base": 1, "shared": "child", "from_child": 2}
    assert rules["base"].sink == "findings"
    assert rules["base"].ignore == ["base"]
