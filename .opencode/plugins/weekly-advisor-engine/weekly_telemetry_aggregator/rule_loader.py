"""Declarative rule loader (P1) — frontmatter + sections + DSL blocks.

Rules are plain ``.md`` files kept under ``rules/`` and versioned in git.  The
loader is deliberately dependency-free: the frontmatter and the ``detect`` /
``test`` fenced blocks use a small, deterministic YAML subset (mappings,
sequences, inline ``[a, b]`` / ``{k: v}``, quoted strings, numbers, booleans).

Governance V1: there is no trust/approve/reload state machine.  Personal or
project overrides are declared with a ``+`` prefix on ``extends`` (same layering
idea as ``harness_scope``): the child rule inherits the parent and overrides the
fields it declares — purely local, never a network fetch.  The same layering is
available programmatically via ``load_rules(overrides=...)`` for threshold tuning
without touching the versioned ``.md``.

Production wiring (three optional frontmatter capabilities):

``sink``
    Destination pile of the emitted finding, ``"findings"`` (default) or
    ``"alerts"``.  Surfaced to consumers as ``finding["details"]["sink"]`` so the
    caller can route the dict without knowing the rule.
``ignore``
    Targets this rule may be suppressed for **as a whole**, at rule level.  A rule
    is dropped when one of its targets is ignored for its id, or when the id
    itself is ignored (see :func:`rule_pipeline.is_rule_ignored`).
``targets`` (`` ```detect `` block)
    Expression listing what a single finding covers — ``targets: pluck(matched,
    "tool")`` publishes ``details["targets"]`` so the finer
    ``"<rule>:<target>"`` form of the ignore list drops only the findings carrying
    that target, and trims the examples of the ones that carry others (see
    :func:`rule_pipeline.apply_finding_ignore`).  A rule that declares none is
    never dropped by that channel.
``emit``
    Literals shallow-merged into the emitted finding, e.g.
    ``emit: {recommendation_type: agent-loop}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

#: Default location of the versioned rules directory.
DEFAULT_RULES_DIR = Path(__file__).resolve().parent / "rules"

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$", re.MULTILINE)
_FENCE_RE = re.compile(r"```([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
#: DSL fences removed from section prose (they are code, not presentation text).
_DSL_FENCE_RE = re.compile(r"```(?:detect|test)\b.*?```", re.DOTALL)
_INT_RE = re.compile(r"[+-]?\d+\Z")
_FLOAT_RE = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+[eE][+-]?\d+|\d+\.\d*[eE][+-]?\d+)\Z")

#: Canonical section names exposed on :class:`Rule`.
SECTION_DESCRIPTION = "description"
SECTION_WHEN = "when triggered"
SECTION_IMPROVE = "how to improve"
SECTION_EXAMPLES = "examples"

#: Default destination pile of an emitted finding.
DEFAULT_SINK = "findings"
#: Supported ``sink`` values.
SINK_VALUES = ("findings", "alerts")


class RuleError(ValueError):
    """Raised when a rule file is malformed or cannot be resolved."""


# --------------------------------------------------------------------------- #
# Minimal YAML subset
# --------------------------------------------------------------------------- #
def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, honouring single/double quotes."""
    quote: str | None = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"':
                continue
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _split_key(content: str) -> tuple[str, str] | None:
    """Split ``key: value`` outside quotes; ``None`` when not a mapping entry."""
    quote: str | None = None
    for index, char in enumerate(content):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == ":" and (index + 1 == len(content) or content[index + 1] in " \t"):
            key = content[:index].strip()
            return (key, content[index + 1 :].strip()) if key else None
    return None


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == "'":
            return inner.replace("''", "'")
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return value


def _split_flow_items(inner: str) -> list[str]:
    """Split a flow collection body on top-level commas (quote/nesting aware)."""
    items: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for char in inner:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
        elif char in "[{":
            depth += 1
            current.append(char)
        elif char in "]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return [item for item in items if item != ""]


def parse_scalar(raw: str) -> Any:
    """Parse an inline scalar / flow collection from the YAML subset."""
    value = raw.strip()
    if value == "":
        return None
    if value[0] in ("'", '"') and value[-1] == value[0] and len(value) >= 2:
        return _strip_quotes(value)
    if value.startswith("[") and value.endswith("]"):
        return [parse_scalar(item) for item in _split_flow_items(value[1:-1])]
    if value.startswith("{") and value.endswith("}"):
        mapping: dict[str, Any] = {}
        for item in _split_flow_items(value[1:-1]):
            pair = _split_key(item)
            if pair is None:
                raise RuleError(f"invalid inline mapping entry: {item!r}")
            mapping[pair[0]] = parse_scalar(pair[1])
        return mapping
    low = value.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    if _INT_RE.match(value):
        return int(value)
    if _FLOAT_RE.match(value):
        return float(value)
    return _strip_quotes(value)


def _prepare_lines(text: str) -> list[tuple[int, str]]:
    prepared: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise RuleError("tabs are not allowed for YAML indentation")
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        prepared.append((indent, stripped[indent:]))
    return prepared


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return None, index
    if lines[index][1] == "-" or lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise RuleError(f"unexpected indentation at line {index + 1}")
        if content == "-" or content.startswith("- "):
            break
        pair = _split_key(content)
        if pair is None:
            raise RuleError(f"expected 'key: value', got {content!r}")
        key, rest = pair
        index += 1
        if rest != "":
            result[key] = parse_scalar(rest)
            continue
        if index < len(lines) and lines[index][0] > indent:
            result[key], index = _parse_block(lines, index, lines[index][0])
        elif (
            index < len(lines)
            and lines[index][0] == indent
            and (lines[index][1] == "-" or lines[index][1].startswith("- "))
        ):
            result[key], index = _parse_sequence(lines, index, indent)
        else:
            result[key] = None
    return result, index


def _parse_sequence(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent != indent or not (content == "-" or content.startswith("- ")):
            break
        item_text = content[1:].strip()
        if item_text == "":
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0])
            else:
                value = None
            result.append(value)
            continue
        if _split_key(item_text) is not None:
            item_indent = indent + 2
            sub_lines = [(item_indent, item_text), *lines[index + 1 :]]
            value, consumed = _parse_block(sub_lines, 0, item_indent)
            result.append(value)
            index += consumed
            continue
        result.append(parse_scalar(item_text))
        index += 1
    return result, index


def parse_yaml(text: str) -> Any:
    """Parse the supported YAML subset into plain Python objects."""
    lines = _prepare_lines(text)
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0, lines[0][0])
    return value


# --------------------------------------------------------------------------- #
# Markdown parsing
# --------------------------------------------------------------------------- #
def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter mapping, body)``; empty mapping when absent."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    parsed = parse_yaml(match.group(1))
    if not isinstance(parsed, dict):
        raise RuleError("frontmatter must be a mapping")
    return parsed, text[match.end() :]


def parse_sections(body: str) -> dict[str, str]:
    """Map lower-cased ``# Heading`` titles to their trimmed section body.

    ``detect`` / ``test`` fenced blocks are removed first so presentation
    sections never leak the DSL code.
    """
    cleaned = _DSL_FENCE_RE.sub("", body)
    matches = list(_HEADING_RE.finditer(cleaned))
    sections: dict[str, str] = {}
    for position, heading in enumerate(matches):
        title = " ".join(heading.group(2).split()).lower()
        start = heading.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(cleaned)
        sections[title] = cleaned[start:end].strip()
    return sections


def parse_fenced_blocks(body: str) -> dict[str, list[str]]:
    """Return fenced code blocks grouped by info string (``detect``, ``test``…)."""
    blocks: dict[str, list[str]] = {}
    for match in _FENCE_RE.finditer(body):
        lang = (match.group(1) or "").strip().lower()
        blocks.setdefault(lang, []).append(match.group(2).strip())
    return blocks


# --------------------------------------------------------------------------- #
# Rule model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Rule:
    """One declarative rule, parsed and ready to be evaluated."""

    id: str
    name: str
    group: str
    severity: str
    scope: str
    version: str = "1"
    extends: str | None = None
    thresholds: dict[str, Any] = field(default_factory=dict)
    patterns: dict[str, list[str]] = field(default_factory=dict)
    file_types: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    sink: str = DEFAULT_SINK
    ignore: list[str] = field(default_factory=list)
    emit: dict[str, Any] = field(default_factory=dict)
    alert: dict[str, str] = field(default_factory=dict)
    description: str = ""
    when_triggered: str = ""
    how_to_improve: str = ""
    examples_section: str = ""
    detect: dict[str, Any] = field(default_factory=dict)
    tests: list[dict[str, Any]] = field(default_factory=list)
    source_path: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Stable serialization (used by tests / future report wiring)."""
        return {
            "id": self.id,
            "name": self.name,
            "group": self.group,
            "severity": self.severity,
            "scope": self.scope,
            "version": self.version,
            "extends": self.extends,
            "thresholds": dict(self.thresholds),
            "patterns": {key: list(val) for key, val in self.patterns.items()},
            "file_types": list(self.file_types),
            "tags": list(self.tags),
            "sink": self.sink,
            "ignore": list(self.ignore),
            "emit": dict(self.emit),
            "alert": dict(self.alert),
            "source_path": self.source_path,
        }


_REQUIRED_FIELDS = ("id", "name", "group", "severity", "scope")
#: Fields a ``load_rules(overrides=...)`` patch may never touch (rule identity).
_OVERRIDE_IMMUTABLE_FIELDS = frozenset({"id", "source_path"})
#: Fields an override replaces wholesale as string lists. Other list fields
#: (``tests`` is a ``list[dict]``) must keep their structured shape.
_OVERRIDE_STR_LIST_FIELDS = frozenset({"tags", "file_types", "ignore"})
#: Frontmatter mappings merged key-by-key by the ``extends`` layering.
_MERGED_RAW_KEYS = ("thresholds", "patterns", "emit", "alert")
#: Fixed vocabulary of the ``alert`` rendering block (read by name in the insights
#: adapter). Unlike ``emit`` (free literals), an unknown key here would silently
#: disable a behaviour (e.g. a typo'd ``rows`` loses the per-day fan-out), so it is
#: rejected rather than ignored.
_ALERT_KEYS = frozenset({"signal", "threshold", "cap", "rows", "row_signal", "row_day"})


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _coerce_sink(value: Any) -> str:
    """Validate the destination pile (``findings`` / ``alerts``)."""
    if value is None:
        return DEFAULT_SINK
    sink = str(value).strip().lower()
    if sink not in SINK_VALUES:
        raise RuleError(f"invalid `sink` {sink!r}: expected one of {', '.join(SINK_VALUES)}")
    return sink


def _coerce_emit(value: Any) -> dict[str, Any]:
    """Validate the ``emit`` literals mapping."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuleError("`emit` must be a mapping of literal key -> value")
    return {str(key): item for key, item in value.items()}


def _coerce_alert(value: Any) -> dict[str, str]:
    """Validate the ``alert`` rendering block (shape of a ``sink: alerts`` output).

    The keys are a fixed vocabulary read *by name* by the insights alert renderer
    (``signal``/``threshold``/``cap``/``rows``/``row_signal``/``row_day``). A typo
    would silently no-op (e.g. a wrong ``rows`` falls back to the single aggregated
    alert, losing the per-day fan-out), so unknown keys raise instead of being
    ignored. Values are metric/threshold field names, coerced to ``str``.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuleError("`alert` must be a mapping of rendering key -> value")
    unknown = sorted(str(key) for key in value if str(key) not in _ALERT_KEYS)
    if unknown:
        raise RuleError(
            f"unknown `alert` key(s): {', '.join(unknown)} — "
            f"expected one of {', '.join(sorted(_ALERT_KEYS))}"
        )
    keys = {str(key) for key in value}
    if "rows" in keys and not {"row_signal", "row_day"} <= keys:
        raise RuleError(
            "`alert.rows` requires both `alert.row_signal` and `alert.row_day` — "
            "without them the per-row fan-out would fabricate an observed value"
        )
    return {str(key): str(item) for key, item in value.items()}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into ``base`` key-by-key, recursing into nested mappings."""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _coerce_patterns(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuleError("`patterns` must be a mapping of group -> list")
    patterns: dict[str, list[str]] = {}
    for key, items in value.items():
        patterns[str(key)] = _coerce_str_list(items)
    return patterns


def _coerce_tests(blocks: dict[str, list[str]], path: Path) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    for raw_block in blocks.get("test", []):
        parsed = parse_yaml(raw_block)
        if parsed is None:
            continue
        if isinstance(parsed, list):
            cases = parsed
        elif isinstance(parsed, dict):
            cases = [parsed]
        else:
            raise RuleError(f"{path}: a `test` block must be a mapping or a list")
        for case in cases:
            if not isinstance(case, dict):
                raise RuleError(f"{path}: each test case must be a mapping")
            tests.append(case)
    return tests


def load_rule(path: str | Path) -> Rule:
    """Parse a single ``.md`` rule file."""
    rule_path = Path(path)
    text = rule_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    sections = parse_sections(body)
    blocks = parse_fenced_blocks(body)

    missing = [name for name in _REQUIRED_FIELDS if not frontmatter.get(name)]
    if missing:
        raise RuleError(f"{rule_path}: missing required frontmatter field(s): {', '.join(missing)}")

    detect_blocks = blocks.get("detect", [])
    if len(detect_blocks) != 1:
        raise RuleError(f"{rule_path}: expected exactly one ```detect block")
    detect = parse_yaml(detect_blocks[0])
    if not isinstance(detect, dict):
        raise RuleError(f"{rule_path}: the ```detect block must be a mapping")

    extends = frontmatter.get("extends")
    return Rule(
        id=str(frontmatter["id"]),
        name=str(frontmatter["name"]),
        group=str(frontmatter["group"]),
        severity=str(frontmatter["severity"]).lower(),
        scope=str(frontmatter["scope"]),
        version=str(frontmatter.get("version", "1")),
        extends=str(extends).strip() if extends else None,
        thresholds=dict(frontmatter.get("thresholds") or {}),
        patterns=_coerce_patterns(frontmatter.get("patterns")),
        file_types=_coerce_str_list(frontmatter.get("file_types")),
        tags=_coerce_str_list(frontmatter.get("tags")),
        sink=_coerce_sink(frontmatter.get("sink")),
        ignore=_coerce_str_list(frontmatter.get("ignore")),
        emit=_coerce_emit(frontmatter.get("emit")),
        alert=_coerce_alert(frontmatter.get("alert")),
        description=sections.get(SECTION_DESCRIPTION, ""),
        when_triggered=sections.get(SECTION_WHEN, ""),
        how_to_improve=sections.get(SECTION_IMPROVE, ""),
        examples_section=sections.get(SECTION_EXAMPLES, ""),
        detect=detect,
        tests=_coerce_tests(blocks, rule_path),
        source_path=str(rule_path),
        raw=frontmatter,
    )


def load_rules(
    directory: str | Path | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[Rule]:
    """Load and resolve every ``.md`` rule under ``directory``, sorted by id.

    ``overrides`` patches loaded rules by id — ``{"<rule-id>": {...}}``.  Mapping
    fields (``thresholds``, ``patterns``, ``emit``, ``alert``) are deep-merged so one
    threshold can be retuned without re-declaring its siblings; list fields (``tags``,
    ``file_types``, ``ignore``) are replaced; ``null`` values and unknown keys are
    skipped.  Overrides are applied *after* ``extends`` resolution, so they win
    over the versioned ``.md``.  An override id that matches no rule is ignored:
    a stale config key must never break a run.
    """
    rules_dir = Path(directory) if directory is not None else DEFAULT_RULES_DIR
    if not rules_dir.is_dir():
        raise RuleError(f"rules directory not found: {rules_dir}")
    rules = [load_rule(path) for path in sorted(rules_dir.glob("*.md"))]
    ids = [rule.id for rule in rules]
    duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
    if duplicates:
        raise RuleError(f"duplicate rule id(s): {', '.join(duplicates)}")
    return apply_overrides(apply_extends(rules), overrides)


def apply_overrides(
    rules: list[Rule],
    overrides: dict[str, dict[str, Any]] | None,
) -> list[Rule]:
    """Return ``rules`` with per-id field overrides applied (order preserved)."""
    if not overrides:
        return rules
    patched: list[Rule] = []
    for rule in rules:
        patch = overrides.get(rule.id)
        patched.append(override_rule(rule, patch) if isinstance(patch, dict) else rule)
    return patched


def override_rule(rule: Rule, patch: dict[str, Any]) -> Rule:
    """Return a copy of ``rule`` with ``patch`` applied field-by-field.

    Only names that exist on :class:`Rule` and are not identity fields are
    honoured; anything else is skipped silently.  The input rule is never
    mutated, so a stale override cannot leak into the next call.
    """
    values: dict[str, Any] = {spec.name: getattr(rule, spec.name) for spec in fields(Rule)}
    for key, value in patch.items():
        if key in _OVERRIDE_IMMUTABLE_FIELDS or key not in values or value is None:
            continue
        current = values[key]
        if isinstance(current, dict) and isinstance(value, dict):
            values[key] = _deep_merge(current, value)
        elif key in _OVERRIDE_STR_LIST_FIELDS:
            values[key] = _coerce_str_list(value)
        elif isinstance(current, str):
            values[key] = str(value)
        else:
            values[key] = value
    values["severity"] = values["severity"].lower()
    values["sink"] = _coerce_sink(values["sink"])
    values["emit"] = _coerce_emit(values["emit"])
    values["alert"] = _coerce_alert(values["alert"])
    values["patterns"] = _coerce_patterns(values["patterns"])
    return Rule(**values)


def _merge_rules(parent: Rule, child: Rule) -> Rule:
    """Child overrides parent field-by-field (the ``+`` layering semantics).

    ``thresholds``, ``patterns`` and ``emit`` are merged key-by-key so a local
    override can tighten one threshold without re-declaring the whole mapping.
    """
    merged_raw = dict(parent.raw)
    for key, value in child.raw.items():
        if key in _MERGED_RAW_KEYS and isinstance(value, dict):
            base = merged_raw.get(key)
            merged_raw[key] = _deep_merge(base if isinstance(base, dict) else {}, value)
        else:
            merged_raw[key] = value
    return Rule(
        id=child.id,
        name=child.name,
        group=child.group,
        severity=child.severity,
        scope=child.scope,
        version=child.version,
        extends=None,
        thresholds=dict(merged_raw.get("thresholds") or {}),
        patterns=_coerce_patterns(merged_raw.get("patterns")),
        file_types=_coerce_str_list(merged_raw.get("file_types")),
        tags=_coerce_str_list(merged_raw.get("tags")),
        sink=_coerce_sink(merged_raw.get("sink")),
        ignore=_coerce_str_list(merged_raw.get("ignore")),
        emit=_coerce_emit(merged_raw.get("emit")),
        alert=_coerce_alert(merged_raw.get("alert")),
        description=child.description or parent.description,
        when_triggered=child.when_triggered or parent.when_triggered,
        how_to_improve=child.how_to_improve or parent.how_to_improve,
        examples_section=child.examples_section or parent.examples_section,
        detect=child.detect or parent.detect,
        tests=child.tests or parent.tests,
        source_path=child.source_path,
        raw=merged_raw,
    )


def apply_extends(rules: list[Rule]) -> list[Rule]:
    """Resolve ``extends`` chains with cycle detection, then sort by id.

    A ``+`` prefix on ``extends`` marks a personal/project override layer but is
    functionally identical: the child inherits the parent and overrides what it
    declares.  Resolution is local only — no network.
    """
    by_id = {rule.id: rule for rule in rules}
    memo: dict[str, Rule] = {}
    resolving: set[str] = set()

    def resolve(rule_id: str) -> Rule:
        if rule_id in memo:
            return memo[rule_id]
        if rule_id in resolving:
            raise RuleError(f"cyclic `extends` involving rule {rule_id!r}")
        resolving.add(rule_id)
        rule = by_id[rule_id]
        target: Rule | None = None
        if rule.extends:
            parent_id = rule.extends[1:].strip() if rule.extends.startswith("+") else rule.extends
            if parent_id not in by_id:
                raise RuleError(f"rule {rule_id!r} extends unknown rule {parent_id!r}")
            target = _merge_rules(resolve(parent_id), rule)
        else:
            target = rule
        resolving.discard(rule_id)
        memo[rule_id] = target
        return target

    resolved = [resolve(rule.id) for rule in rules]
    return sorted(resolved, key=lambda rule: rule.id)
