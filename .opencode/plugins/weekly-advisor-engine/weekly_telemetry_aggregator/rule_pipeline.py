"""Declarative rule pipeline (P1) — DSL evaluation + uniform findings.

The DSL is intentionally small and side-effect free.  ``match`` / ``aggregate`` /
``check`` / ``examples`` expressions are parsed with the stdlib :mod:`ast` and
evaluated against a whitelist of nodes and functions; no ``eval`` on raw text and
no I/O.  Output is deterministic: findings are emitted sorted by rule id, scan
order is preserved, examples keep their first-occurrence order.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .rule_loader import Rule, RuleError

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_EXPECT_VALUES = ("triggered", "clean")


# --------------------------------------------------------------------------- #
# Tiny expression evaluator (stdlib ast, whitelisted)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=256)
def _compile_expr(expression: str) -> ast.Expression:
    try:
        return ast.parse(expression, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - surfaced as RuleError
        raise RuleError(f"invalid DSL expression {expression!r}: {exc.msg}") from exc


def _get_attr(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        if name in value:
            return value[name]
        raise RuleError(f"unknown field {name!r}")
    try:
        return getattr(value, name)
    except AttributeError as exc:
        raise RuleError(f"unknown attribute {name!r}") from exc


def _iterable(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, str):
        return [value]
    return [value]


class _Evaluator:
    """Evaluates one DSL expression against a chain of scopes."""

    def __init__(self, scopes: list[dict[str, Any]], entry: Any = None) -> None:
        self.scopes = scopes
        self.entry = entry

    # -- public API -------------------------------------------------------- #
    def eval_expr(self, expression: str) -> Any:
        tree = _compile_expr(expression)
        return self.eval_node(tree.body)

    def eval_node(self, node: ast.AST) -> Any:
        handler = getattr(self, f"_eval_{type(node).__name__}", None)
        if handler is None:
            raise RuleError(f"unsupported DSL syntax: {type(node).__name__}")
        return handler(node)

    # -- literals / names -------------------------------------------------- #
    def _eval_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def _eval_List(self, node: ast.List) -> list[Any]:
        return [self.eval_node(item) for item in node.elts]

    def _eval_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        return tuple(self.eval_node(item) for item in node.elts)

    def _eval_Name(self, node: ast.Name) -> Any:
        return self._resolve_name(node.id)

    def _eval_Attribute(self, node: ast.Attribute) -> Any:
        return _get_attr(self.eval_node(node.value), node.attr)

    def _eval_Subscript(self, node: ast.Subscript) -> Any:
        container = self.eval_node(node.value)
        key = self.eval_node(node.slice)
        try:
            return container[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuleError(f"invalid subscript {key!r}") from exc

    def _resolve_name(self, name: str) -> Any:
        if name in ("true", "True"):
            return True
        if name in ("false", "False"):
            return False
        if name in ("null", "None", "none"):
            return None
        for scope in reversed(self.scopes):
            if isinstance(scope, dict) and name in scope:
                return scope[name]
        if name == "text":
            return _entry_text(self.entry)
        if name in self._functions():
            return self._functions()[name]
        raise RuleError(f"unknown DSL name {name!r}")

    # -- operators --------------------------------------------------------- #
    def _eval_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.eval_node(value)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = self.eval_node(value)
            if result:
                return result
        return result

    def _eval_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.eval_node(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise RuleError(f"unsupported unary operator {type(node.op).__name__}")

    def _eval_BinOp(self, node: ast.BinOp) -> Any:
        left = self.eval_node(node.left)
        right = self.eval_node(node.right)
        op = type(node.op).__name__
        if op == "Add":
            return left + right
        if op == "Sub":
            return left - right
        if op == "Mult":
            return left * right
        if op in ("Div", "FloorDiv") and not right:
            raise RuleError("division by zero in DSL expression")
        if op == "Div":
            return left / right
        if op == "FloorDiv":
            return left // right
        if op == "Mod":
            return left % right
        raise RuleError(f"unsupported binary operator {op}")

    def _eval_Compare(self, node: ast.Compare) -> bool:
        left = self.eval_node(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.eval_node(comparator)
            if not _compare(op, left, right):
                return False
            left = right
        return True

    def _eval_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise RuleError("DSL only supports direct function calls")
        name = node.func.id
        functions = self._functions()
        if name not in functions:
            raise RuleError(f"unknown DSL function {name!r}")
        if node.keywords:
            raise RuleError(f"function {name!r} does not accept keyword arguments")
        args = [self.eval_node(arg) for arg in node.args]
        try:
            return functions[name](*args)
        except RuleError:
            raise
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise RuleError(f"function {name!r} failed: {exc}") from exc

    # -- helpers ----------------------------------------------------------- #
    def eval_predicate(self, expression: str, item: Any) -> Any:
        """Evaluate a per-item predicate string with ``item`` bound for it."""
        scope: dict[str, Any] = {"item": item}
        if isinstance(item, dict):
            scope.update(item)
        child = _Evaluator([*self.scopes, scope], entry=item if item is not None else self.entry)
        return child.eval_expr(expression)

    def _functions(self) -> dict[str, Callable[..., Any]]:
        return {
            "contains": _fn_contains,
            "containsAny": _fn_contains_any,
            "countAny": _fn_count_any,
            "matches": _fn_matches,
            "length": _fn_length,
            "count": _fn_count,
            "countWhere": self._fn_count_where,
            "someWhere": self._fn_some_where,
            "everyWhere": self._fn_every_where,
            "ratio": _fn_ratio,
            "sum": self._fn_sum,
            "avg": self._fn_avg,
            "min": self._fn_min,
            "max": self._fn_max,
            "first": self._fn_first,
            "text": lambda value=None: _entry_text(value if value is not None else self.entry),
            "field": lambda value, name: _get_attr(value, name),
        }

    def _select(self, selector: str, item: Any) -> Any:
        """Evaluate a per-item selector, tolerating items missing the field."""
        try:
            return self.eval_predicate(selector, item)
        except RuleError:
            return None

    def _reduce(
        self, collection: Any, selector: str | None, reducer: Callable[[list], Any], default: Any
    ) -> Any:
        items = _iterable(collection)
        if selector is None:
            values = list(items)
        else:
            values = [self._select(selector, item) for item in items]
        values = [value for value in values if value is not None]
        return reducer(values) if values else default

    def _fn_count_where(self, collection: Any, expression: str) -> int:
        return sum(1 for item in _iterable(collection) if self.eval_predicate(expression, item))

    def _fn_some_where(self, collection: Any, expression: str) -> bool:
        return any(self.eval_predicate(expression, item) for item in _iterable(collection))

    def _fn_every_where(self, collection: Any, expression: str) -> bool:
        items = _iterable(collection)
        return all(self.eval_predicate(expression, item) for item in items) if items else False

    def _fn_sum(self, collection: Any, selector: str | None = None) -> Any:
        return self._reduce(collection, selector, lambda values: builtins_sum(values), 0)

    def _fn_avg(self, collection: Any, selector: str | None = None) -> Any:
        return self._reduce(
            collection, selector, lambda values: builtins_sum(values) / len(values), 0
        )

    def _fn_min(self, collection: Any, selector: str | None = None) -> Any:
        return self._reduce(collection, selector, builtins_min, None)

    def _fn_max(self, collection: Any, selector: str | None = None) -> Any:
        return self._reduce(collection, selector, builtins_max, None)

    def _fn_first(self, collection: Any, limit: int = 5, selector: str | None = None) -> list[Any]:
        items = _iterable(collection)[: max(0, int(limit))]
        if selector is None:
            return items
        return [
            value for value in (self._select(selector, item) for item in items) if value is not None
        ]


builtins_sum = sum
builtins_min = min
builtins_max = max


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    name = type(op).__name__
    try:
        if name == "Eq":
            return left == right
        if name == "NotEq":
            return left != right
        if name == "Lt":
            return left < right
        if name == "LtE":
            return left <= right
        if name == "Gt":
            return left > right
        if name == "GtE":
            return left >= right
        if name == "In":
            return left in right
        if name == "NotIn":
            return left not in right
    except TypeError as exc:
        raise RuleError(f"cannot compare {left!r} and {right!r}: {exc}") from exc
    raise RuleError(f"unsupported comparison operator {name}")


def _fn_contains(haystack: Any, needle: Any) -> bool:
    return str(needle).lower() in str(haystack).lower()


def _fn_contains_any(haystack: Any, needles: Any) -> bool:
    """True when at least one needle substring (case-insensitive) is present."""
    return any(_fn_contains(haystack, needle) for needle in _iterable(needles))


def _fn_count_any(haystack: Any, needles: Any) -> int:
    """Number of needle substrings found in the haystack (deterministic count)."""
    return sum(1 for needle in _iterable(needles) if _fn_contains(haystack, needle))


def _fn_matches(value: Any, pattern: Any) -> bool:
    try:
        return re.search(str(pattern), str(value), re.IGNORECASE) is not None
    except re.error as exc:
        raise RuleError(f"invalid regex {pattern!r}: {exc}") from exc


def _fn_length(value: Any) -> int:
    try:
        return len(value)
    except TypeError:
        return 0


def _fn_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, set, dict, str)):
        return len(value)
    return 1


def _fn_ratio(part: Any, whole: Any) -> float:
    try:
        numerator = float(part)
        denominator = float(whole)
    except (TypeError, ValueError):
        return 0.0
    return numerator / denominator if denominator else 0.0


def _entry_text(entry: Any) -> str:
    """Concatenate the scalar values of an entry (the ``text`` pseudo-field)."""
    if entry is None:
        return ""
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return str(entry)
    parts: list[str] = []
    for value in entry.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (int, float, bool)) and not isinstance(value, bool):
            parts.append(str(value))
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Scan / aggregate / render
# --------------------------------------------------------------------------- #
def _resolve_scan(context: dict[str, Any], scope: str) -> list[Any]:
    if not scope:
        raise RuleError("the rule has no `scan` (and no frontmatter `scope`)")
    value: Any = context
    for part in scope.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return []
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        inner = value.get("findings")
        return list(inner) if isinstance(inner, list) else [value]
    return [value]


def _filter_file_types(entries: list[Any], file_types: Sequence[str]) -> list[Any]:
    if not file_types:
        return list(entries)
    wanted = tuple(f".{suffix.lstrip('.').lower()}" for suffix in file_types)
    kept: list[Any] = []
    for entry in entries:
        path = ""
        if isinstance(entry, dict):
            path = str(entry.get("file") or entry.get("path") or "")
        if path and not path.lower().endswith(wanted):
            continue
        kept.append(entry)
    return kept


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return str(value)


def _format_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{ratio * 100:.1f}%"


def render_template(
    text: str,
    *,
    occurrences: int,
    metrics: dict[str, Any],
    extra: dict[str, Any] | None = None,
    thresholds: dict[str, Any] | None = None,
) -> str:
    """Resolve ``{{count}}``, ``{{pct}}``, ``{{thresholds.k}}`` and ``{{extra.k}}``."""
    extras = extra or {}
    limits = thresholds or {}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "count":
            return str(occurrences)
        if key == "pct":
            return _format_pct(metrics.get("pct"))
        if key.startswith("extra."):
            return _format_value(extras.get(key[len("extra.") :], ""))
        if key.startswith("thresholds."):
            return _format_value(limits.get(key[len("thresholds.") :], ""))
        if key in metrics:
            return _format_value(metrics[key])
        if key in extras:
            return _format_value(extras[key])
        return ""

    return _PLACEHOLDER_RE.sub(_replace, text)


def _example_text(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in ("description", "evidence", "summary", "text", "message"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
    return str(entry)


def _base_scope(
    matched: list[Any],
    scanned: list[Any],
    rule: Rule,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "matched": matched,
        "scanned": scanned,
        "total": len(scanned),
        "thresholds": dict(rule.thresholds),
        "patterns": {key: list(value) for key, value in rule.patterns.items()},
        "extra": dict(extra or {}),
    }


def evaluate_rule(
    rule: Rule,
    context: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Evaluate one rule; return the uniform finding, or ``None`` when clean."""
    detect = rule.detect
    scope_name = str(detect.get("scan") or rule.scope)
    scanned = _filter_file_types(_resolve_scan(context, scope_name), rule.file_types)
    scan_scope = _base_scope(scanned, scanned, rule, extra)
    if scope_name.isidentifier():
        scan_scope[scope_name] = scanned  # ergonomic alias: scan: sessions -> `sessions`

    match_expr = detect.get("match")
    if match_expr is None:
        matched = list(scanned)
    elif isinstance(match_expr, str):
        evaluator = _Evaluator([scan_scope], entry=None)
        matched = [entry for entry in scanned if evaluator.eval_predicate(match_expr, entry)]
    else:
        raise RuleError(f"rule {rule.id!r}: `match` must be an expression string")

    metrics: dict[str, Any] = {
        "occurrences": len(matched),
        "pct": _fn_ratio(len(matched), len(scanned)),
    }
    base = _base_scope(matched, scanned, rule, extra)
    aggregate = detect.get("aggregate") or {}
    if not isinstance(aggregate, dict):
        raise RuleError(f"rule {rule.id!r}: `aggregate` must be a mapping")
    for name, expression in aggregate.items():
        if isinstance(expression, str):
            metrics[str(name)] = _Evaluator([base], entry=None).eval_expr(expression)
        else:
            metrics[str(name)] = expression

    occurrences = int(metrics.get("occurrences", len(matched)))

    check = detect.get("check") or {}
    if not isinstance(check, dict):
        raise RuleError(f"rule {rule.id!r}: `check` must be a mapping")
    check_scope = {**base, **metrics}
    if check:
        evaluator = _Evaluator([check_scope], entry=None)
        for name, expression in check.items():
            if not isinstance(expression, str):
                raise RuleError(f"rule {rule.id!r}: check {name!r} must be an expression")
            if not evaluator.eval_expr(expression):
                return None
    elif not matched:
        return None

    examples: list[str] = []
    examples_expr = detect.get("examples")
    if isinstance(examples_expr, str) and examples_expr.strip():
        extracted = _Evaluator([check_scope], entry=None).eval_expr(examples_expr)
        examples = [_example_text(item) for item in _iterable(extracted)]
    elif isinstance(examples_expr, list):
        examples = [_example_text(item) for item in examples_expr]

    severity = str(detect.get("severity") or rule.severity).lower()
    rendered_metrics = {key: _jsonable(value) for key, value in metrics.items()}
    details: dict[str, Any] = {
        "name": rule.name,
        "scope": scope_name,
        "version": rule.version,
        "tags": list(rule.tags),
        "when_triggered": render_template(
            rule.when_triggered,
            occurrences=occurrences,
            metrics=metrics,
            extra=extra,
            thresholds=rule.thresholds,
        ),
        "metrics": rendered_metrics,
        "thresholds": dict(rule.thresholds),
        "matched": occurrences,
        "scanned": len(scanned),
    }
    if rule.examples_section:
        details["examples_guide"] = rule.examples_section

    return {
        "id": rule.id,
        "severity": severity,
        "group": rule.group,
        "occurrences": occurrences,
        "description": render_template(
            rule.description,
            occurrences=occurrences,
            metrics=metrics,
            extra=extra,
            thresholds=rule.thresholds,
        ),
        "suggestion": render_template(
            rule.how_to_improve,
            occurrences=occurrences,
            metrics=metrics,
            extra=extra,
            thresholds=rule.thresholds,
        ),
        "examples": examples,
        "details": details,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def evaluate_rules(
    rules: Iterable[Rule],
    context: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate all rules and return triggered findings sorted by id."""
    findings = [
        finding
        for rule in rules
        if (finding := evaluate_rule(rule, context, extra=extra)) is not None
    ]
    return sorted(findings, key=lambda finding: finding["id"])


# --------------------------------------------------------------------------- #
# Test-block runner (used by the parametrized pytest)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RuleTestCase:
    """One `` ```test `` case extracted from a rule file."""

    rule_id: str
    index: int
    context: dict[str, Any]
    expect: str
    extra: dict[str, Any] = field(default_factory=dict)


def iter_rule_tests(rules: Iterable[Rule]) -> list[RuleTestCase]:
    """Flatten every rule's test blocks into runnable cases (stable order)."""
    cases: list[RuleTestCase] = []
    for rule in rules:
        for index, case in enumerate(rule.tests):
            context = case.get("context", case.get("input"))
            if not isinstance(context, dict):
                raise RuleError(f"rule {rule.id!r}: test #{index} needs a `context` mapping")
            expect = str(case.get("expect", "triggered")).strip().lower()
            if expect not in _EXPECT_VALUES:
                raise RuleError(
                    f"rule {rule.id!r}: test #{index} `expect` must be one of {_EXPECT_VALUES}"
                )
            extra = case.get("extra") or {}
            if not isinstance(extra, dict):
                raise RuleError(f"rule {rule.id!r}: test #{index} `extra` must be a mapping")
            cases.append(
                RuleTestCase(
                    rule_id=rule.id, index=index, context=context, expect=expect, extra=extra
                )
            )
    return cases


def run_rule_test(rule: Rule, case: RuleTestCase) -> tuple[bool, str]:
    """Return ``(passed, message)`` for one extracted test case."""
    finding = evaluate_rule(rule, case.context, extra=case.extra)
    triggered = finding is not None
    expected = case.expect == "triggered"
    if triggered == expected:
        return True, f"{rule.id} test #{case.index}: {case.expect} as expected"
    state = "triggered" if triggered else "clean"
    if triggered:
        return False, f"{rule.id} test #{case.index}: expected {case.expect}, got {state}"
    return False, f"{rule.id} test #{case.index}: expected {case.expect}, got {state}"


def run_rule_tests(rules: Iterable[Rule]) -> list[tuple[RuleTestCase, bool, str]]:
    """Run every test case of every rule (convenience for callers/tests)."""
    by_id = {rule.id: rule for rule in rules}
    results: list[tuple[RuleTestCase, bool, str]] = []
    for case in iter_rule_tests(rules):
        rule = by_id[case.rule_id]
        passed, message = run_rule_test(rule, case)
        results.append((case, passed, message))
    return results
