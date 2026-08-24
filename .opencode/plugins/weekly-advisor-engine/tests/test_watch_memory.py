"""Mémoire inter-run de la veille (watch-memory.jsonl append-only)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from weekly_telemetry_aggregator import watch_memory as wm

NOW = datetime(2026, 8, 20)


def _line(
    eid: str,
    *,
    week: str = "2026-W10",
    status: str = "seen",
    occurrences: int = 1,
    sig: dict | None = None,
    note: str = "",
) -> dict:
    return {
        "id": eid,
        "name": eid,
        "first_seen_week": week,
        "last_seen_week": week,
        "occurrences": occurrences,
        "history": [{"week": week, "status": status}],
        "last_signature": sig or {},
        "note": note,
    }


def _write_lines(path: Path, *entries: dict) -> None:
    path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- normalize_id


def test_normalize_id_prefers_npm_then_repo_then_url():
    assert wm.normalize_id("x", "@v/x", "https://github.com/v/x") == "npm:@v/x"
    assert wm.normalize_id("x", None, "https://github.com/v/x") == "gh:v/x"
    assert (
        wm.normalize_id("Some Tool", None, "https://example.com/a") == "url:https://example.com/a"
    )


def test_normalize_id_handles_git_suffix_case_and_name_fallback():
    assert wm.normalize_id("x", None, "git+https://github.com/Acme/Tool.git") == "gh:acme/tool"
    assert wm.normalize_id("Some Tool", None, None) == "url:some tool"


# ---------------------------------------------------------------------- load


def test_load_skips_malformed_lines_with_warning(tmp_path: Path):
    p = tmp_path / "watch-memory.jsonl"
    p.write_text(
        '{"id":"a"}\nnot-json\n{"id":"b","name":"b","first_seen_week":"2026-W1","last_seen_week":"2026-W1","occurrences":1,"history":[],"last_signature":{}}\n',
        encoding="utf-8",
    )
    entries, warnings = wm.load_memory(p)
    assert set(entries) == {"a", "b"}
    assert len(warnings) == 1


def test_missing_file_loads_empty_without_warning(tmp_path: Path):
    entries, warnings = wm.load_memory(tmp_path / "absent.jsonl")
    assert entries == {}
    assert warnings == []


def test_purge_old_ignored_keeps_recommended(tmp_path: Path):
    # entrée ignorée vue 40 sem → purgée ; recommended 40 sem → gardée
    p = tmp_path / "watch-memory.jsonl"
    _write_lines(
        p,
        _line("gone", week="2025-W20", status="ignored"),
        _line("kept", week="2025-W20", status="recommended"),
        _line("blocked", week="2025-W20", status="blocked-security"),
        _line("recent", week="2026-W33", status="ignored"),
    )
    entries, warnings = wm.load_memory(p, now=NOW)
    assert warnings == []
    assert set(entries) == {"kept", "blocked", "recent"}


# ------------------------------------------------------- signatures / filtre


def test_entry_from_item_shapes_new_line():
    item = {
        "name": "Tool",
        "repo_url": "https://github.com/acme/tool",
        "version": "2.1.0",
        "published_at": "2026-08-01T00:00:00Z",
    }
    e = wm.entry_from_item(item, "2026-W34")
    assert e["id"] == "gh:acme/tool"
    assert e["first_seen_week"] == e["last_seen_week"] == "2026-W34"
    assert e["occurrences"] == 1
    assert e["last_signature"] == {"version": "2.1.0", "published_at": "2026-08-01T00:00:00Z"}
    assert e["history"] == [{"week": "2026-W34", "status": "seen"}]


def test_signature_changed_on_version_or_newer_published_at():
    entry = {"last_signature": {"version": "1.0.0", "published_at": "2026-06-01T00:00:00Z"}}
    assert wm.signature_changed(entry, {"version": "2.0.0"})
    assert not wm.signature_changed(entry, {"version": "1.0.0"})
    assert wm.signature_changed(entry, {"version": "1.0.0", "published_at": "2026-07-01T00:00:00Z"})
    assert not wm.signature_changed(
        entry, {"version": "1.0.0", "published_at": "2026-01-01T00:00:00Z"}
    )


def test_signature_unknown_when_no_stored_signature():
    assert not wm.signature_changed({}, {"version": "9.9.9"})


def test_filter_drops_ignored_with_unchanged_signature_but_resurfaces_version_bump():
    memory = {
        "npm:pkg": _line(
            "npm:pkg",
            week="2026-W33",
            status="ignored",
            sig={"version": "1.0.0", "published_at": "2026-06-01T00:00:00Z"},
        )
    }
    same = {"name": "pkg", "npm_package": "pkg", "version": "1.0.0"}
    bumped = {"name": "pkg", "npm_package": "pkg", "version": "2.0.0"}

    kept, dropped = wm.filter_items([same, bumped], memory, "2026-W34")

    assert dropped == [{"id": "npm:pkg", "reason": "ignored-unchanged"}]
    assert [item["id"] for item in kept] == ["npm:pkg"]
    assert kept[0]["version"] == "2.0.0"


def test_filter_flags_stale_seen_within_four_weeks():
    memory = {
        "npm:fresh": _line("npm:fresh", week="2026-W32", status="candidate"),
        "npm:old": _line("npm:old", week="2026-W20", status="candidate"),
    }
    items = [
        {"name": "fresh", "npm_package": "fresh"},
        {"name": "old", "npm_package": "old"},
        {"name": "brand-new"},
    ]

    kept, dropped = wm.filter_items(items, memory, "2026-W34")

    assert dropped == []
    flags = {item["id"]: item["_stale_seen"] for item in kept}
    assert flags == {"npm:fresh": True, "npm:old": False, "url:brand-new": False}


# -------------------------------------------------------------------- append


def test_append_merges_existing_id_and_keeps_append_only_store(tmp_path: Path):
    p = tmp_path / "watch-memory.jsonl"
    first = wm.entry_from_item(
        {"name": "tool", "npm_package": "tool", "version": "1.0.0"}, "2026-W30"
    )

    assert wm.append_entries(p, [first]) == []
    warnings = wm.append_entries(
        p,
        [
            {
                "id": "npm:tool",
                "week": "2026-W34",
                "status": "recommended",
                "signature": {"version": "1.0.0"},
            },
            {"name": "pas-d-id"},
        ],
    )
    assert len(warnings) == 1

    entries, load_warnings = wm.load_memory(p)
    assert load_warnings == []
    entry = entries["npm:tool"]
    assert entry["occurrences"] == 2
    assert entry["first_seen_week"] == "2026-W30"
    assert entry["last_seen_week"] == "2026-W34"
    assert entry["history"][-1] == {"week": "2026-W34", "status": "recommended"}
    # append-only : une ligne par update, l'historique brut reste intact
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2


def test_append_snapshot_each_week_increments_occurrences(tmp_path: Path):
    p = tmp_path / "watch-memory.jsonl"
    for week in ("2026-W30", "2026-W31", "2026-W32"):
        wm.append_entries(p, [wm.entry_from_item({"name": "tool", "npm_package": "tool"}, week)])

    entries, warnings = wm.load_memory(p)
    assert warnings == []
    entry = entries["npm:tool"]
    assert entry["occurrences"] == 3
    assert entry["history"] == [
        {"week": "2026-W30", "status": "seen"},
        {"week": "2026-W31", "status": "seen"},
        {"week": "2026-W32", "status": "seen"},
    ]
    assert wm.build_digest(entries, "2026-W34")["recurrents"] == ["npm:tool"]

    # même semaine re-persistée : pas de double comptage
    wm.append_entries(p, [wm.entry_from_item({"name": "tool", "npm_package": "tool"}, "2026-W32")])
    entries, _ = wm.load_memory(p)
    assert entries["npm:tool"]["occurrences"] == 3


# -------------------------------------------------------------------- digest


def test_digest_bounds_and_recurrents():
    memory: dict[str, dict] = {}
    for i in range(25):
        eid = f"ig{i:02d}"
        memory[eid] = _line(eid, week="2026-W30", status="ignored", note=f"n{i}")
    for i in range(35):
        eid = f"rec{i:02d}"
        entry = _line(eid, week="2026-W31", status="seen")
        entry["history"].append({"week": "2026-W33", "status": "recommended"})
        memory[eid] = entry
    memory["big"] = _line("big", week="2026-W33", status="seen", occurrences=3)

    digest = wm.build_digest(memory, "2026-W34")

    assert len(digest["recently_ignored"]) == 20
    assert set(digest["recently_ignored"][0]) == {"id", "week", "note"}
    assert [row["id"] for row in digest["recently_ignored"]] == [f"ig{i:02d}" for i in range(20)]
    assert len(digest["previously_recommended"]) == 30
    assert digest["recurrents"] == ["big"]
