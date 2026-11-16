"""Rendu Jinja du rapport markdown (extrait de report.py)."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import TelemetryConfig


def render_report_draft(ctx: dict, out: Path, date: str) -> Path:
    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
        autoescape=select_autoescape(("html",)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template("report_template.md.j2")
    rendered = template.render(**ctx)

    draft = out / f"weekly-report-draft-{date}.md"
    draft.write_text(rendered, encoding="utf-8")
    return draft
