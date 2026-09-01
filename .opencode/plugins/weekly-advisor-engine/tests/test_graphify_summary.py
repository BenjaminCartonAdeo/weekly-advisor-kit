import json
import subprocess
import sys
from pathlib import Path

import pytest

from weekly_telemetry_aggregator.graphify_summary import summarize_graph


def test_summary_filters_generic_stale_and_self_loop_without_mutating_graph(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    graph = {
        "built_at_commit": "abc",
        "nodes": [
            {"id": "a", "label": "main", "source_file": "src/main.py", "file_type": "code"},
            {"id": "b", "label": "other", "source_file": "missing.py", "file_type": "code"},
            {"id": "g", "label": "root", "source_file": "src/main.py"},
        ],
        "links": [{"source": "a", "target": "a"}, {"source": "a", "target": "b"}],
    }
    summary = summarize_graph(graph, project_root=tmp_path)
    assert summary["node_count"] == 1
    assert summary["edge_count"] == 0
    assert summary["filtered"] == {"generic_nodes": 1, "stale_nodes": 1, "self_loops": 1}
    assert graph["nodes"][1]["id"] == "b"


def test_summary_is_deterministic_and_groups_files():
    graph = {
        "nodes": [
            {"id": "z", "label": "zeta", "source_file": "z.py"},
            {"id": "a", "label": "alpha", "source_file": "a.py"},
        ],
        "links": [],
    }
    assert summarize_graph(graph) == summarize_graph(graph)
    assert [item["source_file"] for item in summarize_graph(graph)["files"]] == ["a.py", "z.py"]


@pytest.mark.parametrize("links", [None, {"source": "a", "target": "b"}])
def test_summary_treats_non_list_links_as_empty(links: object):
    graph = {"nodes": [], "links": links}

    summary = summarize_graph(graph)

    assert summary["edge_count"] == 0
    assert summary["filtered"]["self_loops"] == 0


def test_cli_rejects_input_output_collision_without_changing_raw_graph(tmp_path: Path):
    graph_path = tmp_path / "graph.json"
    graph_path.write_bytes(json.dumps({"nodes": [], "links": []}).encode())
    raw_graph = graph_path.read_bytes()
    script = Path(__file__).parents[4] / "scripts" / "graphify-architecture-summary.py"

    result = subprocess.run(
        [sys.executable, str(script), str(graph_path), "--output", str(graph_path)],
        cwd=script.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "must not overwrite" in result.stderr
    assert graph_path.read_bytes() == raw_graph


def test_cli_writes_summary_when_invoked_from_repository_root(tmp_path: Path):
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("x", encoding="utf-8")
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "built_at_commit": "abc",
                "nodes": [{"id": "main", "label": "main", "source_file": "src/main.py"}],
                "links": [],
            }
        ),
        encoding="utf-8",
    )
    raw_graph = graph_path.read_bytes()
    output_path = tmp_path / "summary.json"
    script = Path(__file__).parents[4] / "scripts" / "graphify-architecture-summary.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(graph_path),
            "--output",
            str(output_path),
            "--project-root",
            str(tmp_path),
        ],
        cwd=script.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output_path.read_text(encoding="utf-8"))["node_count"] == 1
    assert graph_path.read_bytes() == raw_graph
