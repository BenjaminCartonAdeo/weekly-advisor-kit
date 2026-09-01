from __future__ import annotations

import json
from pathlib import Path

from weekly_telemetry_aggregator.cli import main
from weekly_telemetry_aggregator.graphify_summary import graphify_state


def test_graphify_state_unavailable_does_not_create_output(tmp_path: Path) -> None:
    state = graphify_state(tmp_path)
    assert state["status"] == "unavailable"
    assert state["reason"] == "graph.json absent"
    assert not (tmp_path / "graphify-out").exists()


def test_graphify_state_stale_flag_reads_graph_and_report(tmp_path: Path) -> None:
    output = tmp_path / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_text(json.dumps({
        "built_at_commit": "old",
        "nodes": [{"id": "a", "label": "alpha", "source_file": "a.py"}],
        "links": [],
    }), encoding="utf-8")
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (output / "GRAPH_REPORT.md").write_text("report", encoding="utf-8")
    state = graphify_state(tmp_path, head_commit="new")
    assert state["status"] == "ok"
    assert state["stale"] is True
    assert state["summary"]["node_count"] == 1


def test_graphify_state_cli_writes_degraded_artifact(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"output_dir": str(tmp_path), "project_root": str(tmp_path)}), encoding="utf-8")
    rc = main(["graphify-state", "--config", str(config), "--anchor", "2026-08-12T00:00:00Z"])
    artifact = tmp_path / "weekly-graphify-state-2026-08-12.json"
    assert rc == 1
    assert json.loads(artifact.read_text(encoding="utf-8"))["status"] == "unavailable"
