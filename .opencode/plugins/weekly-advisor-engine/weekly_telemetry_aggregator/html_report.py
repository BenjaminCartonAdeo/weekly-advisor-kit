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

# Bandes [N:19 findings] → chiffre arabe. Validées par `report.validate_llm_blocks`
# (C2 : le nombre doit résoudre vers un scalaire des artefacts) puis rendues ici
# sous forme de chiffre nu, sinon le lecteur verrait le marqueur `[N:…]`.
# Groupe 1 = le nombre, groupe 2 = l'unité (éventuelle) : la résolution ne
# réécrit QUE ces deux groupes, jamais le reste de la ligne, donc elle
# n'introduit aucun caractère HTML — `escape()` reste la seule sanitisation.
_NUMBER_BAND_RE = re.compile(r"\[N:\s*(\d+(?:[.,]\d+)?)((?:\s+[^\[\]]*)?)\]")

# Segment de code markdown `` `foo` `` → `<code>foo</code>` (C10/R2). Une seule
# paire de backticks par segment, pas de fence : le bloc qualitatif est de la
# prose, un fence n'y a pas de sens. Résolu AVANT `escape()` comme les bandes
# `[N:…]`, via un sentinelle : le contenu du segment est échappé à la
# réinjection, donc `escape()` reste l'unique sanitisation et un `` `<script>` ``
# reste inerte (`&lt;script&gt;` dans la balise).
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_CODE_SENTINEL_RE = re.compile("\ue000(\\d+)\ue001")

# Titre markdown dans le bloc qualitatif (C10) : le brouillon automatique
# `report_blocks_draft` émet volontairement des `###` (ses `###` ne sont pas
# validés par `validate_llm_blocks`), et ils fuyaient tels quels dans le HTML.
# Ils deviennent un vrai `<hN>` ; l'offset +2 les rentrent sous le `<h2>` de la
# section, sans jamais concurrencer le titre de la page.
_MD_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(\S.*)$")
_HEADING_OFFSET = 2


def resolve_number_bands(text: str) -> str:
    """`[N:19 findings]` → `19 findings` (chiffre arabe, marqueur retiré).

    Idempotente et conservatrice : une bande dont la charge utile n'est pas un
    nombre brut est laissée telle quelle (le validateur l'a déjà rejetée ; on ne
    réécrit pas un texte qu'on ne sait pas lire).
    """
    return _NUMBER_BAND_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}", text)


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


def _quality_chunk_html(chunk: str) -> str:
    """Un paragraphe du bloc qualité : `<hN>` pour un titre, `<p>` sinon.

    Le texte est DÉJÀ échappé à l'appel : seules des balises statiques sont
    ajoutées ici. Une ligne simple reste un `<br>` (comportement historique).
    """
    out: list[str] = []
    para: list[str] = []
    for raw in chunk.split("\n"):
        line = raw.strip()
        if not line:
            continue
        heading = _MD_HEADING_RE.match(raw)
        if heading:
            if para:
                out.append("<p>" + "<br>".join(para) + "</p>")
                para = []
            level = min(6, len(heading.group(1)) + _HEADING_OFFSET)
            # `group(2)` = le TITRE seul : émettre la ligne entière réintroduirait
            # le marqueur `###` dans la balise — c'est précisément le fuite à supprimer.
            out.append(f"<h{level}>{heading.group(2).strip()}</h{level}>")
            continue
        para.append(line)
    if para:
        out.append("<p>" + "<br>".join(para) + "</p>")
    return "\n".join(out)


def _render_quality_block(block: str | None) -> Markup:
    """Bloc qualitatif §4 : markdown inline → HTML, après `escape()`.

    Pipeline, dans cet ordre :

    1. `` `code` `` → sentinelle (le contenu part dans une table, hors du texte) ;
    2. bandes `[N:19 findings]` → `19 findings` (chiffre arabe, C3) ;
    3. `escape()` — **unique** sanitisation du contenu agent ;
    4. badges `[F:…]`/`[M:…]`/`[A:…]` → `<span class="tag …">` (après escape :
       la valeur du badge est déjà inerte) ;
    5. réinjection `<code>escape(contenu)</code>` ;
    6. paragraphisation + titres `###` → `<hN>`.

    Retourne un Markup vide si absent.
    """
    if not block or not block.strip():
        return Markup()

    spans: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        spans.append(m.group(1))
        return f"\ue000{len(spans) - 1}\ue001"

    def _badge(m: re.Match[str]) -> str:
        kind, value = m.group(1), m.group(2)
        return f'<span class="tag tag-{kind.lower()}">{kind}:{value}</span>'

    prepared = _CODE_SPAN_RE.sub(_stash, resolve_number_bands(block))
    badged = _TOKEN_RE.sub(_badge, escape(prepared))
    badged = _CODE_SENTINEL_RE.sub(
        lambda m: f"<code>{escape(spans[int(m.group(1))])}</code>", badged
    )

    paragraphs = [_quality_chunk_html(chunk) for chunk in re.split(r"\n[ \t]*\n", badged)]
    return Markup("\n".join(html for html in paragraphs if html))


_SECURITY_PARAPHRASE = {
    "mcp-tool-poisoning": "Contenu outil non fiable — revue humaine avant usage.",
    "unbounded-delegation": "Délégation trop large — restreindre le périmètre.",
    "memory-write-unscoped": "Écriture mémoire hors périmètre — revue requise.",
}


def _parse_security_counts(security: dict | None) -> tuple[str, int, int, list[str]]:
    """Normalise status/counts/rules (top-5, préfixe security/)."""
    status, critical_count, blocking_count, rules = _parse_security_head(security)
    return status, critical_count, blocking_count, rules


def _parse_security_head(security: dict | None) -> tuple[str, int, int, list[str]]:
    """status/critical/blocking/rules — socle commun aux deux rendus."""
    status = "pass"
    critical_count = 0
    blocking_count = 0
    rules: list[str] = []
    if not isinstance(security, dict):
        return status, critical_count, blocking_count, rules
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
    return status, critical_count, blocking_count, rules


def _security_int(value: object) -> int | None:
    """int ≥ 0 depuis un JSON, ou None si absent/illisible (jamais 0 par défaut)."""
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _security_rule_counts(security: dict | None, key: str) -> dict[str, int]:
    """`by_rule` / `blocking_by_rule` du gate, ordre déterministe conservé."""
    out: dict[str, int] = {}
    if not isinstance(security, dict):
        return out
    raw = security.get(key)
    if not isinstance(raw, dict):
        return out
    for rule, count in raw.items():
        coerced = _security_int(count)
        if coerced is not None:
            out[str(rule)] = coerced
    return out


# FIXME 8 : allowlist bloquante du gate. Les trois règles sont nommées
# explicitement dans le rendu (MD comme HTML) pour que le lecteur puisse
# vérifier d'où vient `blocking_count`.
_BLOCKING_RULES_HTML = ("mcp-tool-poisoning", "memory-write-unscoped", "unbounded-delegation")


def _security_ratio_paragraph(
    critical_count: int,
    blocking_count: int,
    findings_raw: int | None,
    findings_unique: int | None,
) -> str:
    """Ligne expliquant pourquoi les trois comptes ne sont pas comparables.

    Parité stricte avec la version markdown : `critical_count` est un parcours
    récursif de tout le digest, `findings_raw`/`findings_unique` viennent de
    harness-eval après déduplication, `blocking_count` applique l'allowlist.

    Les seules valeurs interpolées sont des entiers déjà formatés : le balisage
    ci-dessous est statique et n'est donc pas échappé.
    """
    items = [
        f"<li><code>critical_count</code> = <b>{critical_count}</b> : parcours récursif de "
        f"tout le digest (y compris <code>inspection.*.findings</code>, même quand le tableau "
        f"<code>findings</code> de premier niveau est vide).</li>"
    ]
    if findings_raw is not None or findings_unique is not None:
        raw_txt = "n/a" if findings_raw is None else str(findings_raw)
        uniq_txt = "n/a" if findings_unique is None else str(findings_unique)
        items.append(
            f"<li>digest <code>findings_raw</code> = <b>{raw_txt}</b> · "
            f"<code>findings_unique</code> = <b>{uniq_txt}</b> : même population après "
            f"déduplication harness-eval.</li>"
        )
    items.append(
        f"<li><code>blocking_count</code> = <b>{blocking_count}</b> : après application de "
        f"l'allowlist des {len(_BLOCKING_RULES_HTML)} règles bloquantes.</li>"
    )
    return (
        "<p><b>Comptage</b> — les trois chiffres ci-dessous ne sont pas comparables :</p>"
        "<ul>" + "".join(items) + "</ul>"
        "<p><i>Ne pas en déduire un delta : les trois sources ne comptent pas les mêmes "
        "objets.</i></p>"
    )


def _security_rules_list(rules: list[str]) -> str:
    """Bloc <ul> des paraphrases génériques (≤200 caractères par ligne)."""
    items = ["<ul>"]
    for rule in rules:
        short = str(rule).strip().lower().removeprefix("security/")
        para = _SECURITY_PARAPHRASE.get(short, "Signal de sécurité — revue humaine requise.")
        row = f"<li><code>{escape(rule)}</code> — {escape(para)}</li>"
        items.append(row[:200] if len(row) > 200 else row)
    items.append("</ul>")
    return "\n".join(items)


def _security_exact_rules_paragraph() -> str:
    """Les 3 règles bloquantes nommées explicitement (parité markdown)."""
    return (
        "<p>Règles bloquantes exactes (allowlist du gate) : "
        + ", ".join(f"<code>{escape(r)}</code>" for r in _BLOCKING_RULES_HTML)
        + ".</p>"
    )


def _render_security_section(security: dict | None) -> Markup:
    """Section Sécurité repliée (HTML) — counts/rules only, warn-only.

    Entrées Task1 uniquement : ``security.{status,critical_count,
    blocking_count,blocking_rules}`` via ``ctx["gate_status"]["security"]``.
    ``_critical``/``_blocking_security_findings`` inchangés : aucun finding
    brut affiché, paraphrases génériques (≤200 caractères, top-5 max).
    Retourne un ``<details id="security">`` replié par défaut (jamais open).
    """
    # <!-- ponytail: counts-only — aucun finding brut, paraphrases génériques -->
    status, critical_count, blocking_count, rules = _parse_security_counts(security)
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
        parts.append(
            _security_ratio_paragraph(
                critical_count,
                blocking_count,
                _security_int(security.get("digest_findings_raw"))
                if isinstance(security, dict)
                else None,
                _security_int(security.get("digest_findings_unique"))
                if isinstance(security, dict)
                else None,
            )
        )
        by_rule = _security_rule_counts(security, "by_rule")
        if by_rule:
            parts.append("<p>Répartition des findings critical par règle (top 5) :</p>")
            parts.append(
                "<ul>"
                + "".join(
                    f"<li><code>{escape(rule)}</code> — {count}</li>"
                    for rule, count in list(by_rule.items())[:5]
                )
                + "</ul>"
            )
        blocking_by_rule = _security_rule_counts(security, "blocking_by_rule")
        if blocking_by_rule:
            parts.append("<p>Répartition des findings bloquants par règle :</p>")
            parts.append(
                "<ul>"
                + "".join(
                    f"<li><code>{escape(rule)}</code> — {count}</li>"
                    for rule, count in list(blocking_by_rule.items())[:5]
                )
                + "</ul>"
            )
        if rules:
            parts.append(_security_rules_list(rules))
        parts.append(_security_exact_rules_paragraph())
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
