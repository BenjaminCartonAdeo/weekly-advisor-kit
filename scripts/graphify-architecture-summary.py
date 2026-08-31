#!/usr/bin/env python3
"""Print or write a deterministic summary of an out-of-band Graphify graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from weekly_telemetry_aggregator.graphify_summary import load_and_summarize


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path, nargs="?", default=Path("graphify-out/graph.json"))
    parser.add_argument(
        "--output", type=Path, help="write summary here; raw graph is never touched"
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if not args.graph.is_file():
        parser.error(f"Graphify artifact not found: {args.graph}")
    if args.output is not None and args.output.resolve() == args.graph.resolve():
        parser.error("--output must not overwrite the input Graphify artifact")
    summary = load_and_summarize(args.graph, project_root=args.project_root.resolve())
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
