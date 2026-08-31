"""Rapport HTML autonome (v6.1) — rendu Jinja2 self-contained depuis le ctx de report_prep.

Contrat : `render_html_report(cfg, *, anchor, ctx, quality_block) -> Path | None`.
Best-effort total : toute OSError ou TemplateError → warning log + None (le run
cron ne casse jamais, y compris en cas d'erreur de rendu Jinja). Branché en fin
de `report_assemble`, suivi de `open_html_report` (ouverture navigateur,
elle aussi best-effort et désactivable par env `WEEKLY_NO_BROWSER=1`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import webbrowser
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, TemplateError
from markupsafe import Markup, escape

from .config import TelemetryConfig
from .util import parse_anchor

logger = logging.getLogger(__name__)

_TEMPLATE_NAME = "report_template.html.j2"

# Tokens [F:ses_xxx#cat] / [M:...] / [A:...] du bloc qualitatif → badges inline.
_TOKEN_RE = re.compile(r"\[([FMA]):([^\[\]]+)\]")


def _resolve_html_dir(cfg: TelemetryConfig) -> Path | None:
    """Résolution du dossier cible (contrat verrouillé par la cellule 1).

    - `""`  → désactivé (None) ;
    - valeur explicite → `Path(v).expanduser()` (l'expansion ~ est du ressort
      du consommateur) ;
    - None → `<project_root>/reports/html` si `project_root` défini, sinon
      warning + None.
    """
    if cfg.html_report_dir == "":
        return None
    if cfg.html_report_dir is not None:
        return Path(cfg.html_report_dir).expanduser()
    if cfg.project_root is None:
        logger.warning("html_report_dir non défini et project_root absent — rapport HTML ignoré")
        return None
    return cfg.project_root / "reports" / "html"


def _json_default(obj: Any) -> Any:
    """Sérialisation défensive du ctx (sets du ctx réel → listes triées)."""
    if isinstance(obj, set | frozenset):
        return sorted(obj)
    return str(obj)


def _payload_json(ctx: dict) -> str:
    """ctx → JSON embarqué dans `<script type="application/json">`.

    Sécurisation anti-breakout : `</` → séquence JSON `\\/` (échappement valide,
    neutralise la balise fermante) et `<!--` → `<\\u0021--` (neutralise l'état
    "escaped" du parser HTML).
    """
    text = json.dumps(ctx, ensure_ascii=False, sort_keys=True, default=_json_default)
    return text.replace("</", "<\\/").replace("<!--", "<\\u0021--")


def _render_quality_block(block: str | None) -> Markup:
    """Bloc qualitatif §4 : échapper le HTML puis paragraphiser.

    Les tokens `[F:ses_xxx#cat]`, `[M:…]`, `[A:…]` deviennent des badges
    `<span class="tag tag-f|tag-m|tag-a">`. Retourne un Markup vide si absent.
    """
    if not block or not block.strip():
        return Markup()

    def _badge(m: re.Match[str]) -> str:
        kind, value = m.group(1), m.group(2)
        return f'<span class="tag tag-{kind.lower()}">{kind}:{value}</span>'

    badged = _TOKEN_RE.sub(_badge, escape(block))
    paragraphs = []
    for chunk in re.split(r"\n[ \t]*\n", badged):
        text = chunk.strip()
        if not text:
            continue
        paragraphs.append(f"<p>{text.replace(chr(10), '<br>')}</p>")
    return Markup("\n".join(paragraphs))


def render_html_report(
    cfg: TelemetryConfig,
    *,
    anchor: str | None,
    ctx: dict,
    quality_block: str | None,
) -> Path | None:
    """Rend le rapport hebdo en page HTML autonome (un seul fichier, zéro CDN).

    Écrit `weekly-report-<date>.html` et `weekly-report-latest.html` (même
    contenu) dans le dossier résolu, retourne le chemin daté. Best-effort :
    OSError ou TemplateError → warning + None ; désactivé
    (`html_report_dir=""`) → None.
    """
    try:
        out_dir = _resolve_html_dir(cfg)
        if out_dir is None:
            return None

        date = parse_anchor(anchor).strftime("%Y-%m-%d")
        env = Environment(
            loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
            autoescape=True,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        template = env.get_template(_TEMPLATE_NAME)
        rendered = template.render(
            **{
                **ctx,
                "date": date,
                "payload_json": _payload_json(ctx),
                "quality_html": _render_quality_block(quality_block),
            }
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        dated = out_dir / f"weekly-report-{date}.html"
        latest = out_dir / "weekly-report-latest.html"
        dated.write_text(rendered, encoding="utf-8")
        latest.write_text(rendered, encoding="utf-8")
        return dated
    except (OSError, TemplateError) as exc:
        logger.warning("Rapport HTML non généré (%s) — ignoré", exc)
        return None


def open_html_report(cfg: TelemetryConfig, path: Path | None) -> bool:
    """Ouvre le rapport rendu dans le navigateur (best-effort, jamais fatal).

    No-op si `path` est None, si `cfg.open_browser` est False, ou si la variable
    d'environnement `WEEKLY_NO_BROWSER` vaut "1" (cron headless). Toute
    exception (pas de display, navigateur absent…) → warning + False.
    """
    if path is None or not cfg.open_browser:
        return False
    if os.environ.get("WEEKLY_NO_BROWSER") == "1":
        return False
    try:
        return bool(webbrowser.open(path.as_uri()))
    except Exception as exc:  # jamais fatal — headless, display absent, etc.
        logger.warning("Ouverture du rapport dans le navigateur impossible (%s) — ignoré", exc)
        return False
