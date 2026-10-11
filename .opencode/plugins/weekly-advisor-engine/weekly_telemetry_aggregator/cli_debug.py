"""Debug-rule playground (read-only) — extrait de `cli.py` (split cli, Task 2).

Handlers `evaluate` / `fields` / `distributions`. Aucune dépendance à `cli` :
le playground ne lit que le `weekly-summary` daté et le jeu de règles local.
Les helpers `_debug_*` sont appelés par `_cmd_debug_rule` dans ce même module.
"""

from __future__ import annotations

import inspect
import json
import sys

# ------------------------------------------------------------ debug-rule (read-only playground)


def _debug_type_name(value: object) -> str:
    """JSON-ish type label used by the debug-rule playground."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _debug_collect_paths(context: object) -> dict[str, dict[str, object]]:
    """Typed catalogue of every value path reachable in the summary document.

    List items expand to a ``path[]`` segment, so a list of objects yields
    ``field[].sub`` entries — the shape the rule DSL scans. Scalar types are
    unioned per path (an optional field may be ``int`` on one run, ``null`` on
    another), which is exactly what surfaces an upstream parser drift.
    """
    paths: dict[str, dict[str, object]] = {}

    def record(path: str, kind: str, value: object) -> None:
        entry = paths.setdefault(path, {"path": path, "kind": kind, "types": []})
        label = _debug_type_name(value)
        types = entry["types"]
        if isinstance(types, list) and label not in types:
            types.append(label)

    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            record(path or "$", "object", value)
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            item_path = f"{path}[]" if path else "[]"
            record(path or "$", "list", value)
            for item in value:
                walk(item, item_path)
        else:
            record(path, "scalar", value)

    walk(context, "")
    return paths


def _debug_collect_values(context: object) -> dict[str, list[object]]:
    """Scalar leaf values grouped by path (list items share their ``path[]``)."""
    values: dict[str, list[object]] = {}

    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            item_path = f"{path}[]" if path else "[]"
            for item in value:
                walk(item, item_path)
        else:
            values.setdefault(path, []).append(value)

    walk(context, "")
    return values


def _debug_dsl_functions() -> list[dict[str, object]]:
    """DSL function catalogue with arity, derived from the live evaluator.

    Read-only import of the pipeline evaluator keeps the catalogue honest: a new
    helper added to ``rule_pipeline`` shows up here without a second registry.
    """
    from .rule_pipeline import _Evaluator

    catalogue: list[dict[str, object]] = []
    for name, func in _Evaluator([])._functions().items():
        signature = inspect.signature(func)
        required = 0
        optional = 0
        for parameter in signature.parameters.values():
            if parameter.kind not in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                continue
            if parameter.default is inspect.Parameter.empty:
                required += 1
            else:
                optional += 1
        catalogue.append(
            {
                "name": name,
                "arity": str(required) if optional == 0 else f"{required}..{required + optional}",
                "required": required,
                "optional": optional,
            }
        )
    return sorted(catalogue, key=lambda item: str(item["name"]))


def _debug_context_finding(target: str, context: dict) -> dict[str, object]:
    """Uniform finding for a direct DSL expression evaluated on the summary.

    The pipeline only evaluates expressions per scanned entry; the playground
    binds the whole summary as the root scope so expressions such as
    ``count(session_classifications) > 3`` or ``avg(totals.cost_usd)`` are
    inspectable without authoring a rule file.
    """
    from .rule_pipeline import _Evaluator

    result = _Evaluator([context], entry=None).eval_expr(target)
    if isinstance(result, (list, tuple, set, dict)):
        occurrences = len(result)
    elif isinstance(result, (bool, int, float)):
        occurrences = int(result)
    elif isinstance(result, str):
        occurrences = len(result)
    else:
        occurrences = 0
    return {
        "id": f"expr:{target}",
        "severity": "info",
        "group": "debug-rule",
        "occurrences": occurrences,
        "description": f"{target} => {json.dumps(result, ensure_ascii=False, default=str)}",
        "suggestion": "",
        "examples": [],
        "details": {"expression": target, "result": result, "truthy": bool(result)},
    }


def _debug_rule_fields(context: dict, summary_path) -> int:
    """``fields`` action: typed field catalogue plus live DSL function arities."""
    catalogue = sorted(_debug_collect_paths(context).values(), key=lambda item: str(item["path"]))
    payload = {
        "action": "fields",
        "summary": str(summary_path),
        "schema_version": context.get("schema_version"),
        "fields": catalogue,
        "dsl_functions": _debug_dsl_functions(),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), flush=True)
    return 0


def _debug_ranked_values(non_null: list, top_n: int) -> list[list[object]]:
    """Count distinct JSON values, keep the ``top_n`` most frequent as pairs."""
    counts: dict[str, int] = {}
    display: dict[str, object] = {}
    for value in non_null:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        counts[key] = counts.get(key, 0) + 1
        display.setdefault(key, value)
    ranked = sorted(display, key=lambda key: (-counts[key], key))[:top_n]
    return [[display[key], counts[key]] for key in ranked]


def _debug_distribution_row(path: str, values: list, top_n: int) -> dict[str, object]:
    """Per-field sample size / null count / min-max / top values (drift probe)."""
    non_null = [value for value in values if value is not None]
    numeric = [
        value
        for value in non_null
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    all_numeric = bool(non_null) and len(numeric) == len(non_null)
    return {
        "path": path,
        "sample_size": len(non_null),
        "null_count": len(values) - len(non_null),
        "numeric": all_numeric,
        "min": min(numeric) if all_numeric else None,
        "max": max(numeric) if all_numeric else None,
        "top": _debug_ranked_values(non_null, top_n),
    }


def _debug_rule_distributions(context: dict, summary_path, target, top_n: int) -> int:
    """``distributions`` action: one row per (optionally filtered) field path."""
    collected = _debug_collect_values(context)
    rows: list[dict[str, object]] = []
    for path in sorted(collected):
        if target and target not in path:
            continue
        rows.append(_debug_distribution_row(path, collected[path], top_n))
    payload = {
        "action": "distributions",
        "summary": str(summary_path),
        "filter": target,
        "top": top_n,
        "fields": rows,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), flush=True)
    return 0


def _debug_rule_evaluate(context: dict, target, rules_dir) -> int:
    """``evaluate`` action: a rule id or a direct DSL expression over the summary."""
    from .rule_loader import RuleError, load_rules
    from .rule_pipeline import evaluate_rule

    if not target:
        print("debug-rule evaluate: <rule-id|expression> requis", file=sys.stderr)
        return 2
    try:
        rules = load_rules(rules_dir)
    except RuleError as exc:
        print(f"debug-rule: {exc}", file=sys.stderr)
        return 2
    by_id = {rule.id: rule for rule in rules}
    if target in by_id:
        try:
            finding = evaluate_rule(by_id[target], context)
        except RuleError as exc:
            print(f"debug-rule: {exc}", file=sys.stderr)
            return 2
        mode = "rule"
    else:
        try:
            finding = _debug_context_finding(target, context)
        except RuleError as exc:
            print(f"debug-rule: {exc}", file=sys.stderr)
            return 2
        mode = "expression"
    payload = {"action": "evaluate", "mode": mode, "target": target, "finding": finding}
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), flush=True)
    return 0


def _cmd_debug_rule(args, cfg) -> int:
    """Read-only rule playground: ``evaluate`` / ``fields`` / ``distributions``.

    Resolves the active run dir (``run_state.resolve_active_run_dir`` — never a
    guessed path), reads the dated ``weekly-summary`` and the local rule set.
    No network, no writes. ``evaluate`` accepts a rule id or a direct DSL
    expression; ``fields`` prints the typed field catalogue plus DSL function
    arities; ``distributions`` prints per-field top values / min-max with the
    sample size (a parser-drift probe). ``rc 2`` when the active run summary is
    missing (same convention as the other dated subcommands), ``rc 0`` on
    success.
    """
    from .run_state import resolve_active_run_dir
    from .util import parse_anchor

    action = getattr(args, "action", None)
    target = getattr(args, "target", None)
    rules_dir = getattr(args, "rules_dir", None)
    top_n = int(getattr(args, "top", 10) or 10)

    run_time = parse_anchor(getattr(args, "anchor", None))
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)
    summary_path = out / f"weekly-summary-{date}.json"
    if not summary_path.is_file():
        print(f"debug-rule: summary inexistante: {summary_path}", file=sys.stderr)
        return 2
    context = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(context, dict):
        print(f"debug-rule: summary invalide (objet attendu): {summary_path}", file=sys.stderr)
        return 2

    if action == "fields":
        return _debug_rule_fields(context, summary_path)
    if action == "distributions":
        return _debug_rule_distributions(context, summary_path, target, top_n)
    return _debug_rule_evaluate(context, target, rules_dir)
