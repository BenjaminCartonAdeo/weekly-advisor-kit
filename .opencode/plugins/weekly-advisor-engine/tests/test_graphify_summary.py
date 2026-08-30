from pathlib import Path

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
