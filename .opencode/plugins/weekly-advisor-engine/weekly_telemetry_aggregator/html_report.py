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


_SECURITY_PARAPHRASE = {
    "mcp-tool-poisoning": "Contenu outil non fiable — revue humaine avant usage.",
    "unbounded-delegation": "Délégation trop large — restreindre le périmètre.",
    "memory-write-unscoped": "Écriture mémoire hors périmètre — revue requise.",
}


def _render_security_section(security: dict | None) -> Markup:
    """Section Sécurité repliée (HTML) — counts/rules only, warn-only.

    Entrées Task1 uniquement : ``security.{status,critical_count,
    blocking_count,blocking_rules}`` via ``ctx["gate_status"]["security"]``.
    ``_critical``/``_blocking_security_findings`` inchangés : aucun finding
    brut affiché, paraphrases génériques (≤200 caractères, top-5 max).
    Retourne un ``<details id="security">`` replié par défaut (jamais open).
    """
    # <!-- ponytail: counts-only — aucun finding brut, paraphrases génériques -->
    status = "pass"
    critical_count = 0
    blocking_count = 0
    rules: list[str] = []
    if isinstance(security, dict):
        raw = str(security.get("status") or "pass").strip().lower()
        status = raw if raw in {"pass", "warn", "fail"} else "pass"
        try:
            critical_count = max(0, int(security.get("critical_count") or 0))
        except (TypeError, ValueError):
            critical_count = 0
        try:
            blocking_count = max(0, int(security.get("blocking_count") or 0))
        except (TypeError, ValueError):
            blocking_count = 0
        raw_rules = security.get("blocking_rules") or []
        if isinstance(raw_rules, list):
            seen: set[str] = set()
            for r in raw_rules:
                name = str(r or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                rules.append(name if "/" in name else f"security/{name}")
            rules = rules[:5]
    badge = "badge-ok" if status == "pass" else "badge-warn"
    parts = [
        '<details id="security" class="security">',
        f'<summary>Sécurité — <span class="badge {badge}">{escape(status.upper())}</span>'
        f" · {critical_count} critical · {blocking_count} blocking</summary>",
        '<div class="security-body">',
    ]
    if status == "pass" and critical_count == 0 and blocking_count == 0:
        parts.append("<p>Aucun finding security/critical ce run.</p>")
    else:
        parts.append(
            f"<p>Statut : <b>{escape(status)}</b> (warn-only, rapport écrit)."
            f" Findings critical : <b>{critical_count}</b>"
            f" · blocking : <b>{blocking_count}</b>.</p>"
        )
        if rules:
            parts.append("<ul>")
            for rule in rules:
                short = str(rule).strip().lower().removeprefix("security/")
                para = _SECURITY_PARAPHRASE.get(
                    short, "Signal de sécurité — revue humaine requise."
                )
                row = f"<li><code>{escape(rule)}</code> — {escape(para)}</li>"
                parts.append(row[:200] if len(row) > 200 else row)
            parts.append("</ul>")
        parts.append("<p>Revue humaine requise — détails non affichés.</p>")
    parts.append("</div>")
    parts.append("</details>")
    return Markup("\n".join(parts))


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
        # <!-- ponytail: best-effort top_next_steps — forward if present else [] to keep template stable -->
        top_next_steps = ctx.get("top_next_steps", []) if isinstance(ctx, dict) else []
        if top_next_steps is None:
            top_next_steps = []
        rendered = template.render(
            **{
                **ctx,
                "date": date,
                "payload_json": _payload_json(ctx),
                "quality_html": _render_quality_block(quality_block),
                "top_next_steps": top_next_steps,
            }
        )
        # Task2 : section Sécurité repliée (counts-only Task1) sans toucher au gabarit.
        gate = ctx.get("gate_status", {}).get("security") if isinstance(ctx, dict) else None
        sec_html = str(_render_security_section(gate if isinstance(gate, dict) else None))
        if "</main>" in rendered:
            rendered = rendered.replace("</main>", sec_html + "\n</main>", 1)
        else:
            rendered += "\n" + sec_html + "\n"

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
