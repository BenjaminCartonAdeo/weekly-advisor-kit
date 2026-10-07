"""Deterministic report rendering — report-prep + report-assemble (Partie 7).

`report-prep` renders every section that comes from JSON artefacts + git log
(deterministic, no LLM) into a draft; section 4 (qualitative findings) is left
as `<!-- QUALITY_BLOCK -->` for the agent. `report-assemble` injects the LLM
block file (`weekly-report-blocks-<date>.md`) into the draft and produces the
final `weekly-report-<date>.md`. Missing blocks => explicit placeholder, never
a silent gap.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__
from .config import TelemetryConfig
from .harness_scope import harness_digest_problems
from .html_report import open_html_report, render_html_report, resolve_number_bands
from .insights import flatten_harness_findings
from .run_state import (
    active_run_meta,
    record_resilience_event,
    resolve_active_run_dir,
    rollback_current_link,
)
from .security_rules import BLOCKING_RULES, SECURITY_PARAPHRASE
from .util import iso as _iso
from .util import load_json as _load_json
from .util import parse_anchor as _parse_anchor
from .util import parse_iso_ts
from .util import read_text as _load_text


def _git_output(project_root: Path, *args: str) -> list[str] | None:
    """stdout lines of `git <args>`; None on any failure (missing repo, rc != 0, crash)."""
    if project_root is None or not (project_root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.splitlines()


# Marqueur porté par le SUJET des commits produits par `commit-draft`
# (`safe_git_write._draft_commit_message`). Source de vérité unique : le filtre
# sujet ci-dessous et la lecture des résultats enregistrés s'y réfèrent.
_DRAFT_COMMIT_MARKER = "auto-rédigé, revue hebdo"


def _git_log_raw(project_root: Path, *args: str) -> list[str]:
    """Lines of `git log --grep=auto-rédigé, revue hebdo <args>`; [] on any failure."""
    return _git_output(project_root, "log", "--grep=" + _DRAFT_COMMIT_MARKER, *args) or []


def _git_log(project_root: Path, since_iso: str, until_iso: str | None = None) -> list[dict]:
    """Auto-rédigé commits since ``since_iso``: [{hash, date, subject}].

    ``--grep`` matches the FULL commit message (body included), so a spec/doc
    commit merely *mentioning* the phrase in its body was counted. The match is
    therefore re-enforced on the **subject** (first line) in Python.

    ``until_iso`` is an optional upper bound. It must NOT be the run's start
    anchor: the drafts this run is about to report are committed *after* that
    timestamp, so bounding the log at the anchor made `auto_commits` provably
    return 0 at every run. Callers either omit it (bound = now, which covers the
    run) or pass a timestamp that actually ends after the drafts.
    ``since_iso=None`` omits the lower bound (used by the pending backlog query).
    """
    args = ["--format=%h|%ad|%s", "--date=short"]
    if since_iso:
        args.insert(0, "--since=" + since_iso)
    if until_iso:
        args.append("--until=" + until_iso)
    rows = []
    for line in _git_log_raw(project_root, *args):
        parts = line.split("|", 2)
        if len(parts) == 3 and _DRAFT_COMMIT_MARKER in parts[2]:
            rows.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return rows


def _recorded_draft_commits(timings: object) -> list[dict]:
    """Draft commits this run *recorded* in ``weekly-timings-<date>.json``.

    FIX 6 : the report can never count the commits it drafted itself through the
    git log, because those commits do not exist yet at render time — the log
    bounded at the run's start anchor returns 0 by construction. The pipeline,
    on the other hand, knows what `commit-draft` actually committed, and records
    it in the timings artifact at JOIN time. That recorded result is the source
    of truth here.

    The scan is deliberately shape-agnostic (the artifact's exact layout is
    written by the orchestrator, not the engine): any mapping carrying a
    hash-like key AND a subject-like key whose subject contains
    ``_DRAFT_COMMIT_MARKER`` is a recorded draft commit. Requiring the marker in
    the subject keeps the scan self-validating — a record without it is not a
    draft commit and is ignored rather than guessed at.
    """
    if not isinstance(timings, Mapping):
        return []

    hash_keys = ("hash", "sha", "commit", "commit_sha", "head")
    subject_keys = ("subject", "message", "title")
    date_keys = ("date", "committed_at", "at", "timestamp")

    rows: list[dict] = []
    seen: set[int] = set()

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            subject = next(
                (str(value[k]) for k in subject_keys if value.get(k) not in (None, "")),
                "",
            )
            if _DRAFT_COMMIT_MARKER in subject:
                digest = next(
                    (str(value[k]) for k in hash_keys if value.get(k) not in (None, "")),
                    "",
                )
                if digest:
                    rows.append(
                        {
                            "hash": digest,
                            "date": next(
                                (
                                    str(value[k])
                                    for k in date_keys
                                    if value.get(k) not in (None, "")
                                ),
                                "",
                            ),
                            "subject": subject,
                        }
                    )
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(timings)
    return rows


def _merge_draft_commits(*groups: Iterable[Mapping[str, object]]) -> list[dict]:
    """Union de lots de draft commits, dédupliquée par hash, ordre de 1re apparition.

    La clé de dédup est le hash **court** (7 caractères) : `%h` de `_git_log` et
    les 7 premiers caractères d'un SHA complet enregistré par l'orchestrateur
    désignent le même commit. Une ligne sans hash n'est jamais dédupliquée (elle
    reste comptée telle quelle) plutôt que d'être silencieusement absorbée.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for row in group:
            key = str(row.get("hash") or "").strip()[:7]
            if key:
                if key in seen:
                    continue
                seen.add(key)
            rows.append(dict(row))
    return rows


def _auto_commits(project_root: Path, since_iso: str, timings: object = None) -> list[dict]:
    """Draft commits de la fenêtre §8 — UNION (enregistrés ∪ git log), pas un fallback.

    FIX 6 : les résultats ENREGISTRÉS des appels `commit-draft` sont la seule
    source capable de voir les drafts du run courant (ils n'existent pas encore
    dans l'historique au moment du rendu). Le git log, lui, reste borné **par le
    bas seulement** (`since_iso`) et sans borne haute : une borne haute posée sur
    l'ancre de DÉBUT de run rendait ce décompte structurellement nul.

    Choix de fusion (et non de filtrage de `recorded` sur `since_iso`) : les
    enregistrements portent une date au format libre, parfois absente — les
    filtrer risquerait de re-sous-compter les drafts du run, exactement le défaut
    que FIX 6 corrige. Les fusionner comble donc le trou de l'autre côté : si
    l'orchestrateur n'enregistre que les drafts du run courant, les drafts des
    runs PRÉCÉDENTS tombant dans la même fenêtre restent comptés via le log.
    """
    return _merge_draft_commits(
        _recorded_draft_commits(timings),
        _git_log(project_root, since_iso),
    )


def _pending_auto_commits(project_root: Path, cutoff_iso: str, timings: object = None) -> int:
    """Draft commits en attente de revue — backlog CROSS-RUN (union, §8).

    FIX 6 + FIX 7. « En attente de revue » est un backlog qui traverse les runs:
    l'intérêt de la ligne est justement de compter ce qui traîne depuis les runs
    PRÉCÉDENTS. Renvoyer `len(recorded)` dès que l'orchestrateur enregistre
    hash+subject faisait s'effondrer le compteur sur le seul run courant (3 au
    lieu de 40+). D'où l'union dédupliquée par hash des drafts enregistrés du run
    et du git log borné par le haut (`cutoff_iso`, sans borne basse = tout
    l'historique antérieur), le log restant le filet quand `recorded` est vide.

    Le filtre sur le marqueur vit désormais dans `_git_log` (côté SUJET, pas
    corps) : ne pas le re-tester ici, sous peine de suggérer à tort que cette
    fonction filtre encore.
    """
    backlog = _merge_draft_commits(
        _recorded_draft_commits(timings),
        _git_log(project_root, None, cutoff_iso),
    )
    return len(backlog)


def _head_commit(project_root: Path) -> dict | None:
    """HEAD au moment du run : {hash, date, subject} ; None hors git (reco §8 12/09)."""
    lines = _git_output(project_root, "log", "-1", "--format=%H|%ad|%s", "--date=short")
    if not lines:
        return None
    parts = lines[0].strip().split("|", 2)
    if len(parts) != 3:
        return None
    return {"hash": parts[0], "date": parts[1], "subject": parts[2]}


def _dirty_files(project_root: Path, limit: int = 50) -> list[str]:
    """Lignes `git status --short` au moment du run (capées) ; [] hors git (reco §8 12/09)."""
    return (_git_output(project_root, "status", "--short") or [])[:limit]


def _self_cost_value(cfg: TelemetryConfig) -> dict | None:
    """Advisor session info {cost, tokens} for the report; None when undetectable."""
    from .costing import advisor_cost
    from .sqlite_reader import DataSourceError

    try:
        return advisor_cost(cfg)
    except DataSourceError:
        return None


def _self_cost_number(value: object) -> float | None:
    """Cost/token numérique, ou None (artefact absent, tronqué ou non numérique)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _self_cost_context(cfg: TelemetryConfig, out: Path, date: str) -> dict:
    """Self-cost du ctx — INVARIANT E4 : une seule valeur, jamais les deux.

    L'étape 8 (`costing.self_cost`) remesure APRÈS l'assemblage et persiste
    `weekly-self-cost-<date>.json`. L'assemblage ne peut donc pas lire un
    artefact postérieur à lui : on n'inverse PAS l'ordre des étapes.

      * artefact présent → sa valeur seule, phase `post-assemble`, aucun
        libellé d'antériorité (c'est la mesure réconciliée) ;
      * artefact absent (premier run) → mesure live, phase `assemble`, ET le
        gabarit publie « mesuré à l'assemblage » au lieu d'une valeur nue.

    Dans les deux cas le ctx ne contient qu'un seul couple (cost, tokens) :
    l'assemblage ne peut pas afficher 0,0493 $ pendant que l'étape 8 affiche
    0,0718 $ pour le même fait.
    """
    artifact = _load_json(out / f"weekly-self-cost-{date}.json") or {}
    cost = _self_cost_number(artifact.get("cost"))
    tokens = _self_cost_number(artifact.get("tokens"))
    if cost is None:
        info = _self_cost_value(cfg) or {}
        cost = _self_cost_number(info.get("cost"))
        tokens = _self_cost_number(info.get("tokens"))
        phase = "assemble"
    else:
        phase = "post-assemble"
    return {
        "self_cost": cost,
        "self_cost_tokens": int(tokens) if tokens is not None else None,
        "self_cost_phase": phase,
        # FIX 2 : aucune source de télémétrie du run ne publie un compteur
        # d'appels API (`advisor_cost` ne renvoie que cost/session_id/tokens, et
        # `api_calls` n'apparaît nulle part dans les artefacts du run). Le gabarit
        # rendait donc une chaîne vide suivie d'un double espace. On rend
        # explicitement "n/a" plutôt qu'inventer une valeur.
        "self_cost_api_calls": "n/a",
    }


def _top_models(summary: dict, limit: int = 3) -> list[dict]:
    """Top-N by cost with <5% models fused into 'autres' (spec: by code, not LLM)."""
    # v6.0.l (E9) : les lignes 0 token ET 0 coût sont des fantômes de sélection
    # (sessions aux steps vides) — elles n'apportent aucun signal au top modèles.
    models = [
        m
        for m in summary.get("by_model", [])
        if m.get("total_tokens", 0) > 0 or m.get("total_cost_usd", 0.0) > 0
    ]
    models.sort(key=lambda m: (-m.get("total_cost_usd", 0.0), m.get("model", "")))
    total_cost = sum(m.get("total_cost_usd", 0.0) for m in models)
    if total_cost <= 0:
        return models
    top, others_cost = [], 0.0
    others_tokens, others_sessions = 0, 0
    for m in models:
        share = m.get("total_cost_usd", 0.0) / total_cost
        if len(top) < limit or share >= 0.05:
            top.append(m)
        else:
            others_cost += m.get("total_cost_usd", 0.0)
            others_tokens += m.get("total_tokens", 0)
            others_sessions += m.get("session_count", 0)
    if len(models) > len(top):
        top.append(
            {
                "model": "autres",
                "total_cost_usd": round(others_cost, 6),
                "total_tokens": others_tokens,
                "session_count": others_sessions,
            }
        )
    return top


def _harness_breakdown(summary: dict) -> list[dict]:
    """Ventilation sessions/tokens/coût par harnais (miroir de by_model).

    Dérivé de `summary["by_harness"]`, tolérant aux clés absentes (runs
    anciens) : entrées non-dict ignorées, champs manquants → 0, tri
    (-coût, harnais). Jamais de levée sur summary malformé.
    """
    if not isinstance(summary, dict):
        return []
    raw = summary.get("by_harness", [])
    if not isinstance(raw, list):
        return []
    rows = []
    for h in raw:
        if not isinstance(h, dict):
            continue
        try:
            sessions = int(h.get("session_count", 0) or 0)
        except (TypeError, ValueError):
            sessions = 0
        try:
            tokens = int(h.get("total_tokens", 0) or 0)
        except (TypeError, ValueError):
            tokens = 0
        try:
            cost = float(h.get("total_cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        rows.append(
            {
                "harness": h.get("harness", ""),
                "session_count": sessions,
                "total_tokens": tokens,
                "total_cost_usd": cost,
            }
        )
    rows.sort(key=lambda r: (-r["total_cost_usd"], r["harness"]))
    return rows


def _complete_daily(period: dict, daily: list[dict]) -> list[dict]:
    """Tous les jours de la fenêtre, zéro explicite (v5.30, 10).

    Le bucketing ne produit que les jours avec activité — le lecteur pouvait croire
    à des trous de données. On complète la période avec des entrées à zéro.
    """
    start = parse_iso_ts(period.get("start"))
    end = parse_iso_ts(period.get("end"))
    if start is None or end is None:
        return daily
    start = start.date()
    end = end.date()
    by_date = {d.get("date"): d for d in daily}
    out: list[dict] = []
    cur = start
    while cur <= end:
        out.append(
            by_date.get(
                cur.isoformat(),
                {
                    "date": cur.isoformat(),
                    "cost_usd": 0.0,
                    "total_tokens": 0,
                    "cache_hit_rate": None,
                },
            )
        )
        cur += timedelta(days=1)
    return out


def _warning_multiplier(warning: Mapping) -> int:
    """Multiplicateur D5 d'un warning sérialisé — 1 si le champ est absent/illisible.

    `aggregator._cap_warnings` regroupe les messages identiques AVANT le plafond
    et porte le total dans `WarningEntry.count` (50 warnings « session active
    exclue » → 1 entité `count=50`). Compter les LIGNES ici affichait `×1` dans
    l'annexe alors que le summary JSON portait 50 : les deux lectures doivent
    venir du même `count`, sinon le rapport se contredit lui-même.

    Un `count` illisible ou négatif retombe sur 1 : une entité absente ne vaut
    jamais zéro occurrence (elle disparaîtrait de l'annexe sans contrepartie).
    """
    try:
        return max(1, int(warning.get("count", 1)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1


def _group_warnings(warnings: list[dict]) -> list[dict]:
    """Regroupe les warnings identiques (message) — annexe lisible (v5.30, F, D5).

    Le multiplicateur vient de `count` (voir `_warning_multiplier`), pas du
    nombre de lignes : l'agrégateur a DÉJÀ regroupé, donc une ligne = une entité
    porteuse de N occurrences. Les `session_ids` échantillonnés sont conservés
    (ordre d'entrée) et dédupliqués avec le `session_id` propre à l'entité.
    """
    grouped: dict[str, dict] = {}
    for w in warnings:
        msg = w.get("message", "")
        entry = grouped.setdefault(msg, {"message": msg, "count": 0, "session_ids": []})
        entry["count"] += _warning_multiplier(w)
        samples = entry["session_ids"]
        for sid in (w.get("session_id"), *(w.get("session_ids") or ())):
            # Un warning global n'a pas de session : il est déjà compté par
            # l'entité, il n'a pas à peupler l'échantillon d'identifiants.
            if isinstance(sid, str) and sid and sid not in samples:
                samples.append(sid)
    return list(grouped.values())


def _sort_maintenance_findings(insights: object) -> list[dict]:
    """Constats de maintenance triés par (catégorie A-Z, sévérité décroissante) — §7.

    La liste reste PLATE : une ligne par constat, aucune condensation (ni compteur,
    ni top-N, ni « (+N autres) »). Le tri n'est qu'un réordonnancement de l'information
    existante ; la longueur de la section 7 suit le nombre de constats.

    Renvoie une NOUVELLE liste : `insights` est la structure partagée avec
    `report_blocks_draft` (brouillon de repli de la section 4), un tri en place se
    propagerait à ce rendu. `sorted()` est stable — à (catégorie, sévérité) égal, l'ordre
    d'entrée est conservé, donc deux rendus successifs produisent le même texte.

    Le rang de sévérité est celui de `_SEV_RANK` (high/critical avant medium/warning
    avant low/info) : une sévérité inconnue se classe après `low`.

    Le TRI ne condense rien ; la condensation des constats strictement identiques
    est faite en aval, dans `build_report_context` (`_dedupe_findings`), pour que
    cette fonction reste le seul endroit qui décide de l'ordre.
    """
    maint = insights.get("maintenance") if isinstance(insights, dict) else None
    return sorted(
        _coherence_findings(maint),
        key=lambda f: (
            str(f.get("category") or "?").lower(),
            _SEV_RANK.get(str(f.get("severity") or "").lower(), 3),
        ),
    )


# --------------------------------------------------------------------------- rendu texte
#
# Une SEULE implémentation de la troncature pour tout le rendu de texte du rapport.
# Elle coupe sur une frontière de mot : une puce ne doit jamais exposer un nom de
# skill coupé en plein milieu (`loadtest-baseline-man`), c'est exactement ce qui
# rendait les findings de cohérence illisibles.

_EVIDENCE_LIMIT = 110  # section 5 — preuve d'un finding de cohérence (borne historique)
_WATCH_DESC_LIMIT = 160  # section 6 — description d'un finding de veille (md ET HTML)
_WATCH_EVIDENCE_LIMIT = 180
_WATCH_SUMMARY_LIMIT = 200  # section 6 — résumé d'une évolution du cœur
_SESSION_TITLE_LIMIT = 80  # annexe « Toutes les sessions »
_AUDIT_TEXT_LIMIT = 80  # texte court des candidats d'étape C


def truncate_text(text: object, limit: int = _WATCH_SUMMARY_LIMIT) -> str:
    """Texte normalisé (espaces) et tronqué à ``limit`` caractères, sans mot coupé.

    Source UNIQUE de la troncature. `watch_distill.truncate_summary` délègue ici,
    et les gabarits consomment des valeurs DÉJÀ tronquées : une limite écrite dans
    un `.j2` serait un nombre magic dupliqué entre le markdown et le HTML, et le
    contexte ne peut pas porter de callable (il est sérialisé en JSON pour le
    payload HTML).

    Trois sorties, par ordre de préférence :

    1. fin de phrase (``. `` la plus proche de la borne, au moins au tiers de la
       fenêtre) : le texte reste lisible, sans ellipse ;
    2. frontière de mot : la dernière espace de la fenêtre, quand elle conserve au
       moins la moitié de la borne ;
    3. milieu d'un mot insécable (jeton plus long que la borne — chemin, identifiant) :
       coupe franche à ``limit - 1`` + ellipse. La borne historique de
       `main._truncate` est conservée — l'ellipse signale la troncature au lieu de
       la rendre muette.
    """
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    window = normalized[:limit]
    dot = window.rfind(". ")
    if dot == limit - 2:
        # La phrase tombe pile sur la borne : sans ellipse, la ligne paraît entière.
        return window[: dot + 1] + "…"
    if dot >= limit // 3:
        return window[: dot + 1]
    # Frontière de mot : la DERNIÈRE espace de la fenêtre. Garde à la moitié — on ne
    # sacrifie pas plus de la moitié de la fenêtre pour respecter une frontière (le
    # cas « un seul mot plus long que la borne » est traité ci-dessous).
    space = window.rfind(" ")
    if space >= limit // 2:
        return window[:space].rstrip() + "…"
    return window.rstrip()[: limit - 1].rstrip() + "…"


#: `skill 'weekly-foo' jamais chargé…` — nom cité entre guillemets dans la description.
_SKILL_QUOTED_RE = re.compile(r"\bskills?\s+['\"]([^'\"]{3,})['\"]", re.IGNORECASE)
#: `skill weekly-foo jamais chargé` — nom « slug » (doit contenir `-`/`_`/`.`, sinon
#: la forme française « skill jamais chargé » linkerait sur le mot suivant).
_SKILL_SLUG_RE = re.compile(r"\bskills?\s+([A-Za-z0-9_][\w.-]*[-_.][\w.-]+)\b", re.IGNORECASE)
#: `.opencode/skills/loadtest-baseline-management/SKILL.md` — chemin de la recommandation.
_SKILL_PATH_RE = re.compile(r"skills/([A-Za-z0-9][\w.-]*)")


def _finding_subject(finding: Mapping) -> str:
    """Nom du skill/command visé par un finding — ``""`` si le constat n'en nomme aucun.

    Les findings de cohérence répètent une description GÉNÉRIQUE (« Skill jamais
    chargé mais protégé par une politique TTL pin ») et ne nomment la cible que dans
    la preuve — historiquement tronquée en milieu de mot par le gabarit. Douze puces
    ainsi rendues se ressemblent et ne disent pas quoi traiter. Sources, par ordre
    de fiabilité : le champ contractuel `target_skill_id` (cf. `curation.decide_actions`),
    puis le nom cité dans la description, puis le chemin de la recommandation.
    """
    raw = finding.get("target_skill_id") or finding.get("skill_id")
    if isinstance(raw, str) and raw.strip():
        # `target_skill_id` peut être une liste séparée par virgules : le premier
        # nom est celui que la puce met en avant.
        return raw.split(",")[0].strip()
    description = str(finding.get("description") or "")
    for pattern in (_SKILL_QUOTED_RE, _SKILL_SLUG_RE):
        match = pattern.search(description)
        if match:
            return match.group(1).strip()
    path = _SKILL_PATH_RE.search(str(finding.get("recommendation") or ""))
    return path.group(1).strip() if path else ""


def _project_finding(finding: Mapping, *, evidence_limit: int) -> dict:
    """Copie d'un finding enrichie des champs de RENDU (`subject`, `evidence_short`).

    Copie, jamais mutation : `insights` et `coherence_findings` sont partagés avec
    d'autres rendus (`report_blocks_draft`, `weekly_skill_curate`). `subject` n'est
    ajouté que s'il est non vide — pas de clé à tester dans les gabarits.
    """
    projected = dict(finding)
    evidence = finding.get("evidence_summary")
    if evidence:
        projected["evidence_short"] = truncate_text(evidence, evidence_limit)
    subject = _finding_subject(finding)
    if subject:
        projected["subject"] = subject
    return projected


def _dedupe_findings(items: Iterable[Mapping]) -> list[dict]:
    """Findings identiques à l'écran → une puce, porteuse d'un multiplicateur `dup`.

    La clé est le CONTENU RENDU (catégorie, sévérité, sujet, description,
    recommandation, preuve tronquée), pas l'identifiant du constat : deux entrées
    qui ne diffèrent que par leur référence de preuve JSON sont le même constat,
    et le `×N` dit combien de fois la passe en a émis. La recommandation reste
    dans la clé — c'est elle qui nomme le skill à traiter, et l'agréger agrégerait
    des constats qui ne demandent pas la même action (cf. mesure
    `doc/measurements/2026-10-03-section5-duplication.md` §3.1).

    L'ordre d'entrée est conservé (premier exemplaire wins) : le rendu reste stable
    d'un rendu à l'autre.
    """
    merged: dict[tuple[str, ...], dict] = {}
    for item in items:
        key = (
            str(item.get("tag") or item.get("category") or ""),
            str(item.get("severity") or ""),
            str(item.get("subject") or ""),
            str(item.get("description") or ""),
            str(item.get("recommendation") or ""),
            str(item.get("evidence_short") or ""),
        )
        entry = merged.get(key)
        if entry is None:
            merged[key] = {**item, "dup": 1}
        else:
            entry["dup"] += 1
    return list(merged.values())


def _group_watch_findings(findings: object) -> list[dict]:
    """Findings de veille groupés par `category` — une puce de famille + décompte.

    `install-new` / `improve-existing` / `ignore` sont des CATÉGORIES
    (`watch_validation.INPUT_CATEGORIES`), pas des décisions : le gabarit les
    répétait en boucle (26 puces `[LOW] ignore` quasi identiques). Ici chaque
    famille porte une puce unique `[sévérité] category ×N`, la sévérité n'étant
    écrite dessus que si elle est homogène — sinon la composition est recomposée
    (`high×2, low×5`) plutôt que masquée. Tous les membres restent visibles,
    condensés par `_dedupe_findings`.
    """
    raw = findings.get("findings") if isinstance(findings, Mapping) else findings
    if not isinstance(raw, list):
        return []
    items = _dedupe_findings(
        _project_finding(f, evidence_limit=_WATCH_EVIDENCE_LIMIT)
        for f in raw
        if isinstance(f, dict)
    )
    grouped: dict[str, list[dict]] = {}
    for item in items:
        category = str(item.get("category") or "").strip() or "autre"
        grouped.setdefault(category, []).append(item)
    out: list[dict] = []
    for category, members in grouped.items():
        counts = Counter(str(m.get("severity") or "info").lower() for m in members)
        out.append(
            {
                "category": category,
                # `members` et non `items` : dans un `.j2`, `g.items` résout la
                # MÉTHODE `dict.items` (l'attribut gagne la recherche de clé) et la
                # boucle itère une builtin.
                "members": members,
                # `count` = nombre de constats émis par la veille, `distinct` = lignes
                # rendues : l'écart entre les deux est exactement la condensation.
                "count": sum(int(m.get("dup", 1)) for m in members),
                "distinct": len(members),
                "severity": next(iter(counts)) if len(counts) == 1 else None,
                "severity_mix": ", ".join(
                    f"{sev}×{n}"
                    for sev, n in sorted(
                        counts.items(), key=lambda kv: (_SEV_RANK.get(kv[0], 3), kv[0])
                    )
                ),
                # Une famille à sévérités mêlées est classée sur sa sévérité LA PLUS
                # HAUTE, pas sur un « info » de repli : sinon elle passerait après
                # une famille `low` alors qu'elle contient un constat `high`.
                "severity_rank": min(_SEV_RANK.get(sev, 3) for sev in counts),
                "repo_url": next(
                    (
                        (m.get("subject") or {}).get("repo_url")
                        for m in members
                        if isinstance(m.get("subject"), dict)
                        and (m.get("subject") or {}).get("repo_url")
                    ),
                    None,
                ),
            }
        )
    out.sort(key=lambda g: (g["severity_rank"], g["category"]))
    return out


#: §8 — l'inventaire du run est la liste des fichiers RÉELLEMENT présents dans le
#: répertoire du run. Au-delà, la ligne devient un mur illisible : le reste est
#: compté, pas listé.
_RUN_ARTIFACTS_LIST_LIMIT = 24


def _run_artifact_inventory(
    out: Path, gate_artifacts: Mapping[str, object] | None = None
) -> list[dict[str, object]]:
    """§8 (C8) : l'inventaire RÉEL du run dir, réconcilié avec les artefacts déclarés.

    Le gabarit listait 4 noms de fichiers en dur : ils ne correspondaient à rien
    (ni présents, ni produits par ce pipeline — `weekly-ecosystem-*.json` peut
    ne pas exister) et Passingait sous silence les 10 autres artefacts du run.

    Deux sources, un seul rendu :

    1. le contenu réel du répertoire, restreint aux `.json` (fichiers, un niveau —
       pas les sous-dossiers comme `extracts/`) : c'est la vérité de ce qui
       existe. Les `.md` du run (`weekly-report-draft-*.md`,
       `weekly-report-*.md`) sont des SORTIES de rendu, pas des entrées ;
       les inventorier rendrait le §8 non déterministe (le second `report_prep`
       verrait le draft du premier) ;
    2. les artefacts DÉCLARÉS par le gate (`validate_required_artifacts`), y
       compris les `audit-findings-<session>.json` dynamiques : un artefact
       attendu et absent doit rester visible, sinon le lecteur croirait le run
       complet.

    Trié par nom, déterministe. Aucune OSError ne fuite : un run dir illisible
    donne un inventaire réduit aux seuls artefacts déclarés.
    """
    entries: dict[str, dict[str, object]] = {}
    try:
        children = sorted(out.iterdir()) if out.is_dir() else []
    except OSError:
        children = []
    for child in children:
        if child.suffix != ".json":
            continue
        try:
            if not child.is_file():
                continue
        except OSError:
            continue
        entries[child.name] = {"name": child.name, "status": "present", "required": False}
    for name, record in (gate_artifacts or {}).items():
        key = str(name)
        status = "absent"
        required = False
        if isinstance(record, Mapping):
            status = str(record.get("status") or "absent")
            required = bool(record.get("required", False))
        entries.setdefault(key, {"name": key, "status": "present", "required": required})
        entries[key]["status"] = status
        entries[key]["required"] = required
    return [entries[name] for name in sorted(entries)]


def _session_project_paths(summary: Mapping | None) -> dict[str, str]:
    """`session_id` → `project_path`, d'après la summary du run.

    Indexé sous les DEUX formes du même identifiant : la summary porte
    `opencode:ses_xxx` (préfixe harnais) alors que certains artefacts et
    certaines bases ne portent que `ses_xxx`. Sans cette normalisation, une
    cible hors périmètre se lirait « non résolue » au lieu d'être nommée — ce
    qu'elle est précisément le but de la section Drafting (D5).

    Index construit une fois : la section résout une cible par candidat, pas par
    session (les candidats sont plafonnés à quelques unités).
    """
    sessions = (summary or {}).get("all_sessions") if isinstance(summary, Mapping) else None
    if not isinstance(sessions, list):
        return {}
    index: dict[str, str] = {}
    for session in sessions:
        if not isinstance(session, Mapping):
            continue
        sid = session.get("session_id")
        path = session.get("project_path")
        if not isinstance(sid, str) or not sid or not isinstance(path, str) or not path:
            continue
        index.setdefault(sid, path)
        bare = sid.rsplit(":", 1)[-1]
        if bare:
            index.setdefault(bare, path)
    return index


def _drafting_view(
    draft_candidates: object, summary: Mapping | None, project_root: Path | str | None
) -> dict[str, object]:
    """C11 / D5 : les candidats de drafting, et la cible HORS PÉRIMÈTRE nommée.

    Motivation : `weekly-draft-candidates-<date>.json` était produit par l'étape 4
    et jamais lu par le rapport. Un run dont les 3 candidats visent un dépôt que
    le pipeline ne projette pas se lisait « 0 draft, 0 commit » sans jamais dire
    POURQUOI — le travail réel de la semaine devenait invisible (P5).

    Décision D5 : le rapport **nomme** la cible, il n'en crée pas une seconde
    instance. On ne fait donc aucun `fs` : `in_scope` est une VÉRIFICATION
    (le `project_path` de la session candidate est-il sous `project_root` ?), pas
    une autorisation de draft.

    `in_scope` est `None` (inconnu) quand ni le `project_path` ni le
    `project_root` ne permettent de trancher : une cible non résolue ne doit pas
    être comptée « hors périmètre », ni « dans le périmètre » — elle est nommée
    comme telle, et le lecteur tranche.
    """
    payload = draft_candidates if isinstance(draft_candidates, Mapping) else {}
    raw = payload.get("candidates")
    raw = raw if isinstance(raw, list) else []
    projects = _session_project_paths(summary)
    root: Path | None = None
    if isinstance(project_root, (str, Path)) and str(project_root).strip():
        try:
            root = Path(project_root).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            root = None

    rows: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        sid = str(item.get("session_id") or "")
        target = projects.get(sid) or (projects.get(sid.rsplit(":", 1)[-1]) if sid else None) or ""
        in_scope: bool | None = None
        if target and root is not None:
            try:
                candidate = Path(target).expanduser().resolve(strict=False)
                in_scope = candidate == root or root in candidate.parents
            except (OSError, RuntimeError, ValueError):
                in_scope = None
        rows.append(
            {
                "session_id": sid,
                "recommendation_type": str(
                    item.get("recommendation_type") or item.get("category") or "candidat"
                ),
                "severity": str(item.get("severity") or "low"),
                "action": str(item.get("action") or ""),
                "skill_id": str(item.get("skill_id") or ""),
                "description_short": truncate_text(item.get("description"), _AUDIT_TEXT_LIMIT),
                "recommendation_short": truncate_text(
                    item.get("recommendation"), _AUDIT_TEXT_LIMIT
                ),
                "target": target,
                "in_scope": in_scope,
            }
        )

    out_of_scope = [r for r in rows if r["in_scope"] is False]
    unknown_scope = [r for r in rows if r["in_scope"] is None]
    out_of_scope_targets = sorted({str(r["target"]) for r in out_of_scope if r["target"]})
    return {
        "present": isinstance(draft_candidates, Mapping) and bool(raw),
        "limit": payload.get("limit"),
        "candidates": rows,
        "out_of_scope": out_of_scope,
        "unknown_scope": unknown_scope,
        # Nommés explicitement (D5) : c'est la liste que le lecteur doit avoir
        # sous les yeux, pas un compte.
        "out_of_scope_targets": out_of_scope_targets,
        "out_of_scope_count": len(out_of_scope),
        "unknown_scope_count": len(unknown_scope),
    }


#: Marqueurs de titre Markdown dans un texte déjà aplati (` · `). `releases._release_summary`
#: ne dépouille que les puces `-* `, pas les ATX : le rapport affichait
#: « ## Core · ### Bugfixes · … ». Le texte reste de la prose, les marqueurs
#: disparaissent (R3) — même dans le HTML, qui ne les interpréterait de toute façon.
#: Un `#` n'est un marqueur de titre que s'il est suivi d'un espace (ATX) — sinon
#: c'est un caractère ordinaire (« issue #42 ») et le dépouiller mutilerait le texte.
_MD_MARKER_RE = re.compile(r"(?:^|(?<=\s))#{1,6}(?:[ \t]+|$)")


def _plain_release_summary(summary: object) -> str:
    """Résumé de release sans marqueurs Markdown, avant troncature."""
    text = " ".join(str(summary or "").split())
    if not text:
        return ""
    text = _MD_MARKER_RE.sub("", text)
    return text.replace("**", "").replace("`", "").strip()


def _plain_code_ticks(text: object) -> str:
    """Texte jumeau de rendu HTML : les délimiteurs de code Markdown tombent.

    Le HTML n'interprète pas le Markdown — un `` `rule` `` y fuit en clair
    (« Corriger `weekly-report-prose` — 3 violation(s) »). Le texte Markdown,
    lui, est écrit pour ces délimiteurs : on les SUPPRIME côté HTML plutôt que de
    les convertir en `<code>`, le HTML restant ainsi sans sémantique inventée et
    les deux rendus portant les mêmes mots.

    Choix : suppression pure, pas d'appariement de paires. Un délimiteur isolé
    (source amont mal formée) disparaît avec les autres au lieu de laisser un
    caractère parasite ; apparier exigerait de savoir où s'arrête le code span,
    information absente ici.
    """
    return str(text or "").replace("`", "")


def _version_label(version: object) -> str:
    """Version d'une évolution du cœur préfixée d'UN seul `v` (R3).

    `releases._opencode_releases` stocke le `tag_name` GitHub tel quel
    (`v1.18.34`). Le gabarit markdown écrivait `v{{ c.version }}` → `vv1.18.34`.
    La normalisation vit ici, en Python, pour que les deux rendus ne puissent
    plus diverger sur le préfixe.
    """
    text = str(version or "").strip()
    if not text:
        return ""
    return f"v{text.lstrip('vV')}" if text.lstrip("vV") else text


def _ecosystem_view(ecosystem: object) -> dict:
    """Copie de l'artefact écosystème avec les champs de rendu tronqués (C6).

    Les gabarits markdown et HTML lisent les mêmes `new_items` / `core_changes` :
    la troncature est faite ici une fois pour les deux plutôt qu'écrite en dur dans
    chaque `.j2`.
    """
    if not isinstance(ecosystem, dict):
        return {}
    view = dict(ecosystem)

    def _item(raw: object) -> dict:
        item = dict(raw) if isinstance(raw, dict) else {"name": str(raw)}
        description = item.get("description")
        if description:
            short = truncate_text(description, _WATCH_DESC_LIMIT)
            item["description_short"] = short
            # Jumeau HTML : mêmes mots, sans délimiteurs Markdown (le gabarit HTML
            # lit `description_plain`, le markdown garde `description_short`).
            item["description_plain"] = _plain_code_ticks(short)
        return item

    items = ecosystem.get("new_items")
    if isinstance(items, list):
        view["new_items"] = [_item(i) for i in items]
    changes = ecosystem.get("core_changes")
    if isinstance(changes, list):
        view["core_changes"] = [
            {
                **c,
                "summary_short": truncate_text(
                    _plain_release_summary(c.get("summary")), _WATCH_SUMMARY_LIMIT
                ),
                "version_label": _version_label(c.get("version")),
            }
            if isinstance(c, dict)
            else c
            for c in changes
        ]
    return view


def _top_harness_rules(
    digest: dict | None, ignored_rules: list[str], n: int = 5
) -> list[tuple[str, int]]:
    """Top-N most-violated harness rules, ignored rules excluded."""
    ignored = set(ignored_rules)
    return Counter(
        f["rule"] for f in flatten_harness_findings(digest) if f.get("rule") not in ignored
    ).most_common(n)


def _match_actor(text: str, toi_keys: tuple[str, ...], pipeline_keys: tuple[str, ...]) -> str:
    """Matche un texte contre les tables de clés Toi/Pipeline, fallback Agent."""
    t = (text or "").lower()
    if any(k in t for k in toi_keys):
        return "Toi"
    if any(k in t for k in pipeline_keys):
        return "Pipeline"
    return "Agent"


def _actor_for_harness_rule(rule: str) -> str:
    """Mappe une règle harness vers l'acteur propriétaire (Toi/Pipeline/Agent)."""
    return _match_actor(
        rule,
        ("security", "secret", "mcp-tool", "unbounded", "memory-write"),
        ("allowlist", "scope", "budget", "coverage", "lint"),
    )


def _actor_for_alert(rule: str) -> str:
    """Mappe une règle d'alerte vers l'acteur."""
    return _match_actor(rule, ("budget",), ("lint", "coverage", "violation", "spike", "scope"))


def _actor_for_finding(finding: dict) -> str:
    """Mappe un finding d'audit qualité vers l'acteur."""
    cat = str(finding.get("category") or "")
    rtype = str(finding.get("recommendation_type") or "")
    if _match_actor(cat, ("retire", "merge", "security", "adopt"), ()) == "Toi":
        return "Toi"
    if _match_actor(rtype, ("adopt", "merge"), ()) == "Toi":
        return "Toi"
    #: constats guardrail (catégorie `missing-guardrail` ou rec-type `guardrail-check`) :
    #: le kit ne peut pas appliquer un check, l'humain si → « Toi ».
    if _match_actor(cat, ("guardrail",), ()) == "Toi":
        return "Toi"
    if _match_actor(rtype, ("guardrail",), ()) == "Toi":
        return "Toi"
    return _match_actor(cat, (), ("harness", "coverage", "scope", "drift"))


_SEV_RANK = {"high": 0, "critical": 0, "medium": 1, "warning": 1, "low": 2, "info": 2, "ok": 2}


def _harness_step_candidates(digest: dict | None, ignored: set[str]) -> list[dict]:
    """Candidats next-steps depuis les per-rule findings harness (source 1/3)."""
    candidates: list[dict] = []
    if not isinstance(digest, dict):
        return candidates
    try:
        flat = flatten_harness_findings(digest)
    except Exception:
        flat = []
    counter: Counter[str] = Counter()
    for finding in flat:
        rule = finding.get("rule")
        if not isinstance(rule, str) or not rule or rule in ignored:
            continue
        counter[rule] += 1
    # most_common est déjà trié par count desc ; on stabilise par règle
    for rule, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
        actor = _actor_for_harness_rule(rule)
        sev = "high" if "security" in rule.lower() else "medium"
        candidates.append(
            {
                "actor": actor,
                "source": "harness",
                "rule": rule,
                "count": count,
                "severity": sev,
                "text": f"Corriger `{rule}` — {count} violation(s)",
                "detail": f"{count} violation(s) pour {rule}",
            }
        )
    return candidates


def _alert_step_candidates(insights: dict | None) -> list[dict]:
    """Candidats next-steps depuis insights.alerts (source 2/3)."""
    candidates: list[dict] = []
    if not isinstance(insights, dict):
        return candidates
    alerts = insights.get("alerts")
    if not isinstance(alerts, list):
        return candidates
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        rule = str(alert.get("rule") or "").strip()
        if not rule:
            continue
        sev = str(alert.get("severity") or "medium").lower()
        actor = _actor_for_alert(rule)
        observed = alert.get("observed")
        threshold = alert.get("threshold")
        unit = alert.get("unit") or ""
        candidates.append(
            {
                "actor": actor,
                "source": "alert",
                "rule": rule,
                "severity": sev,
                "observed": observed,
                "threshold": threshold,
                "unit": unit,
                "text": f"Alerte `{rule}` — observé {observed} vs seuil {threshold}{(' ' + unit) if unit else ''} ({sev})",
                "detail": f"seuil {threshold}, observé {observed}{(' ' + unit) if unit else ''}",
            }
        )
    return candidates


def _proposed_check_suffix(finding: dict) -> str:
    """Suffixe borné `[check <kind> → <target>]` si le finding porte `proposed_check`.

    Report-only v1 : le payload nomme le check à construire pour que le bloc
    « Prochaines actions » montre QUEL check construire. Absent ou malformé →
    chaîne vide (comportement historique strictement inchangé).
    """
    pc = finding.get("proposed_check")
    if not isinstance(pc, dict):
        return ""
    kind = truncate_text(pc.get("kind"), 24)
    target = truncate_text(pc.get("target"), _AUDIT_TEXT_LIMIT)
    if not kind and not target:
        return ""
    inner = f"{kind} → {target}" if kind and target else (kind or target)
    return f" [check {truncate_text(inner, _AUDIT_TEXT_LIMIT)}]"


def _audit_step_candidates(findings: dict | None) -> list[dict]:
    """Candidats next-steps depuis les audit findings qualitatifs (source 3/3)."""
    candidates: list[dict] = []
    if not isinstance(findings, dict):
        return candidates
    flist = findings.get("findings")
    if not isinstance(flist, list):
        return candidates
    for finding in flist:
        if not isinstance(finding, dict):
            continue
        cat = str(finding.get("category") or "unknown").strip() or "unknown"
        sev = str(finding.get("severity") or "medium").lower()
        actor = _actor_for_finding(finding)
        desc = str(finding.get("description") or "").strip()
        rec = str(finding.get("recommendation") or "").strip()
        # texte concis sans chiffres libres (spec prose) — on garde desc/rec tronqués
        # sur frontière de mot (source unique : `truncate_text`).
        short = (
            f"{cat} — {truncate_text(desc, _AUDIT_TEXT_LIMIT)}"
            f" → {truncate_text(rec, _AUDIT_TEXT_LIMIT)}"
            if desc or rec
            else cat
        )
        #: payload report-only : nomme le check à construire, sans changer le défaut.
        short += _proposed_check_suffix(finding)
        candidates.append(
            {
                "actor": actor,
                "source": "audit",
                "category": cat,
                "severity": sev,
                "description": desc,
                "recommendation": rec,
                "text": short,
                "detail": desc or cat,
            }
        )
    return candidates


def _order_step_candidates(candidates: list[dict], limit: int) -> list[dict]:
    """Déduplique, trie et groupe les candidats (un meilleur par acteur + complément)."""

    def _sort_key(cand: dict) -> tuple[int, int, int, str, str]:
        sev_rank = _SEV_RANK.get(str(cand.get("severity") or "medium").lower(), 3)
        source_rank = {"harness": 0, "alert": 1, "audit": 2}.get(str(cand.get("source") or ""), 3)
        count_rank = -int(cand.get("count", 0)) if isinstance(cand.get("count"), int) else 0
        rule_key = str(cand.get("rule") or cand.get("category") or "")
        actor_key = str(cand.get("actor") or "")
        return (sev_rank, source_rank, count_rank, rule_key, actor_key)

    # Déduplication par (actor, rule/category/text)
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for cand in candidates:
        key_rule = cand.get("rule") or cand.get("category") or cand.get("text") or ""
        key = (cand.get("actor") or "Agent", str(key_rule))
        if key not in seen:
            seen.add(key)
            uniq.append(cand)
    uniq.sort(key=_sort_key)
    # Grouper : un meilleur par acteur d'abord, dans l'ordre Toi/Pipeline/Agent
    by_actor: dict[str, list[dict]] = {"Toi": [], "Pipeline": [], "Agent": []}
    for cand in uniq:
        actor = cand.get("actor")
        if actor not in by_actor:
            actor = "Agent"
            cand = {**cand, "actor": actor}
        by_actor[actor].append(cand)
    ordered: list[dict] = []
    for actor in ("Toi", "Pipeline", "Agent"):
        if by_actor[actor]:
            ordered.append(by_actor[actor][0])
            if len(ordered) >= limit:
                break
    # Compléter jusqu'à limit avec les suivants les plus sévères
    if len(ordered) < limit:
        for cand in uniq:
            if cand not in ordered:
                ordered.append(cand)
                if len(ordered) >= limit:
                    break
    return ordered[:limit]


def _top_next_steps(
    digest: dict | None,
    insights: dict | None,
    findings: dict | None,
    *,
    ignored_rules: list[str] | None = None,
    limit: int = 3,
) -> list[dict]:
    """Dérive déterministe des prochaines actions groupées par acteur.

    Sources (priorité égale, tri final par sévérité) :
    - harness : per-rule findings (``flatten_harness_findings``), comptés par règle ;
    - alerts  : ``insights.alerts`` (seuil dépassé) ;
    - audit   : ``weekly-quality-findings.findings`` (audit qualitatif Partie 3).

    Chaque candidat est assigné à un acteur **Toi** (décision humaine, secret,
    budget), **Pipeline** (infra/harness/allowlist) ou **Agent** (comportement
    d'agent, loop, context-bloat). Le résultat est **déterministe** (tri par
    sévérité, source, compte, règle) et groupé : au plus un par acteur en tête,
    puis complété jusqu'à ``limit``. Fallback ``[]`` si aucune source.
    """
    ignored = set(ignored_rules or [])
    candidates: list[dict] = []
    candidates.extend(_harness_step_candidates(digest, ignored))
    candidates.extend(_alert_step_candidates(insights))
    candidates.extend(_audit_step_candidates(findings))

    if not candidates:
        return []

    # `text` est écrit en Markdown (règles de harness entre `` ` ``) : le HTML
    # consomme le jumeau `text_plain`, seul chemin sans marqueur résiduel.
    return [
        {**step, "text_plain": _plain_code_ticks(step.get("text") or step.get("detail"))}
        for step in _order_step_candidates(candidates, limit)
    ]


#: Fields that identify a mapping as a real harness-eval *occurrence*.
#: ``result`` is deliberately absent: it is the harness evaluation-matrix column
#: (``inspection.*[i].rules[]`` rows are ``{"rule": …, "result": "pass"}``), one
#: per scanned COMPONENT, not one per violation.  Counting it inflated the gate
#: by one finding per component (real 2026-10-04 digest: 1319 "findings" — 453 /
#: 433 / 433 per rule — for 44 real occurrences, and rc=1 on every run).
_FINDING_MARKER_FIELDS = ("severity", "message", "detail")


def _normalized_rule_name(rule: object) -> str:
    """Rule id lowercased and stripped of its ``security/`` prefix.

    Same normalisation as :func:`_is_blocking_security_rule`, so the
    ``harness_ignored_rules`` ignore-list matches whether the producer emits
    ``security/mcp-tool-poisoning`` or ``mcp-tool-poisoning``.
    """
    return str(rule or "").strip().lower().removeprefix("security/")


def _critical_security_findings(
    digest: object, *, ignored_rules: Iterable[str] | None = None
) -> list[dict]:
    """Return critical security findings, preserving deterministic provenance.

    A mapping is a finding only when it carries a rule/id **and** at least one
    real occurrence field (:data:`_FINDING_MARKER_FIELDS`).  That is the same
    detailed-vs-matrix discrimination as ``util.iter_digest_findings``
    (``rec["detailed"]``), re-implemented locally because
    ``insights.flatten_harness_findings`` strips the ``detailed`` flag from its
    output and walks every inspection section — it cannot answer "is this a
    detailed finding?".

    A finding is kept when its rule is ``critical`` AND ``security/``-prefixed,
    or when it is one of the :data:`_BLOCKING_SECURITY_RULES` (blocking warns
    regardless of severity — that policy is unchanged).  Rules listed in
    ``ignored_rules`` (``cfg.harness_ignored_rules``) are dropped.
    """
    ignored = {_normalized_rule_name(rule) for rule in (ignored_rules or ())}
    if not isinstance(digest, dict):
        return []

    # Do not let a present (possibly empty) top-level ``findings`` array hide
    # findings attached to inspection components.  harness-eval has emitted
    # both shapes over time.  The recursive walk is deliberately conservative:
    # a mapping is a finding only when it carries a rule/id and at least one
    # finding field, so metadata such as ``rules`` is not misclassified.
    findings: list[dict] = []
    seen: set[int] = set()

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            rule = value.get("rule") or value.get("id")
            if rule is not None and any(key in value for key in _FINDING_MARKER_FIELDS):
                findings.append(dict(value))
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(digest)
    if not findings:
        # Keep compatibility with the normalized harness shape if a future
        # producer uses a finding record without one of the marker fields.
        findings = flatten_harness_findings(digest)
    return [
        finding
        for finding in findings
        if _normalized_rule_name(finding.get("rule") or finding.get("id")) not in ignored
        and (
            (
                str(finding.get("severity") or "").lower() == "critical"
                and str(finding.get("rule") or finding.get("id") or "")
                .lower()
                .startswith("security/")
            )
            or _is_blocking_security_rule(finding.get("rule") or finding.get("id"))
        )
    ]


_NONBLOCKING_WARNING_MARKERS = (
    "transcript-truncated:",
    "transcript_truncated:",
    "recovered:",
    "recovered ",
    "récupéré:",
    "récupérée:",
    "récupération:",
)
_RECOVERABLE_INPUT_MARKERS = {
    "watch",
    "harness",
    "proposal",
    "proposals",
    "ecosystem",
}
_REFUSAL_STATUSES = {"error", "rejected", "ambiguous", "unverified", "refused", "failure"}


_RECOVERABLE_ARTIFACT_STEMS: dict[str, tuple[str, ...]] = {
    "watch": (
        "weekly-watch-findings",
        "weekly-watch-context",
        "weekly-ecosystem",
        "watch-candidates",
    ),
    "ecosystem": ("weekly-ecosystem",),
    "harness": (
        "weekly-harness-digest",
        "weekly-harness-remediation",
        "weekly-harness-remediation-proposals",
    ),
    "proposal": (
        "weekly-harness-remediation-proposals",
        "weekly-harness-remediation",
    ),
}


def _path_is_outside_worktree(target: object, worktree: object) -> bool:
    """Prove that an absolute target is outside a worktree.

    Permission refusals are report-only only after this check succeeds.  A
    missing, relative or unresolvable path is deliberately *not* considered
    external: fail closed rather than turning an unverified refusal into a
    successful run.
    """
    if not isinstance(target, (str, Path)) or not isinstance(worktree, (str, Path)):
        return False
    target_text = str(target).strip()
    worktree_text = str(worktree).strip()
    if not target_text or not worktree_text:
        return False
    try:
        target_path = Path(target_text).expanduser()
        worktree_path = Path(worktree_text).expanduser()
        if not target_path.is_absolute() or not worktree_path.is_absolute():
            return False
        target_path = target_path.resolve(strict=False)
        worktree_path = worktree_path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    try:
        target_path.relative_to(worktree_path)
    except ValueError:
        return True
    return False


def _record_field(record: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        value = record.get(key)
        if value is not None:
            if isinstance(value, Mapping):
                nested = value.get("path") or value.get("target")
                if nested is not None:
                    return nested
            return value
    return None


def _coerce_rc(value: object, *, default: int | None = None) -> int | None:
    """Return a bounded process status, rejecting malformed values.

    JSON manifests are agent-facing inputs.  Treat booleans, empty strings and
    arbitrary text as malformed instead of accidentally turning them into a
    successful status with ``int(...)``.
    """
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _json_file_state(path: Path) -> tuple[object | None, str]:
    """Read one JSON artifact and distinguish absent from malformed input."""
    if not path.is_file():
        return None, "absent"
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None, "ill_readable"
        value = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "ill_readable"
    return (value, "present") if isinstance(value, dict) else (None, "ill_readable")


def _valid_schema_version(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_date(date: object) -> bool:
    return isinstance(date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is not None


def _valid_weekly_summary(value: Mapping) -> bool:
    period = value.get("period")
    return (
        _valid_schema_version(value.get("schema_version"), 2)
        and isinstance(period, Mapping)
        and _nonempty_text(period.get("start"))
        and _nonempty_text(period.get("end"))
        and _nonempty_text(value.get("generated_at"))
        and isinstance(value.get("totals"), Mapping)
    )


def _valid_weekly_insights(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 1)
        and isinstance(value.get("period"), Mapping)
        and _nonempty_text(value.get("generated_at"))
        and isinstance(value.get("deltas"), Mapping)
        and isinstance(value.get("alerts"), list)
        and isinstance(value.get("maintenance"), Mapping)
    )


def _valid_harness_digest(value: Mapping) -> bool:
    inspection = value.get("inspection")
    return (
        (isinstance(inspection, Mapping) and bool(inspection))
        or (isinstance(value.get("rules"), list) and bool(value["rules"]))
        or (isinstance(value.get("findings"), list) and bool(value["findings"]))
    )


def _valid_ecosystem(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 2)
        and isinstance(value.get("new_items"), list)
        and isinstance(value.get("core_changes"), list)
        and isinstance(value.get("warnings"), list)
    )


def _has_findings_list(value: Mapping) -> bool:
    return isinstance(value.get("findings"), list)


def _valid_coherence(value: Mapping) -> bool:
    return isinstance(value.get("findings"), list) or isinstance(
        value.get("curation_signal"), (list, Mapping)
    )


def _valid_audit_candidates(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 1)
        and isinstance(value.get("audited"), list)
        and isinstance(value.get("unaudited"), list)
        and isinstance(value.get("limit"), int)
        and not isinstance(value.get("limit"), bool)
    )


def _valid_watch_context(value: Mapping) -> bool:
    return _valid_schema_version(value.get("schema_version"), 1) and isinstance(
        value.get("market_matches"), list
    )


def _valid_watch_findings(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 2)
        and isinstance(value.get("findings"), list)
        and isinstance(value.get("validation"), Mapping)
    )


def _valid_watch_candidates(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 1)
        and isinstance(value.get("candidates"), list)
        and isinstance(value.get("security_annex"), list)
    )


def _valid_remediation(value: Mapping) -> bool:
    return isinstance(value.get("summary"), Mapping) and isinstance(value.get("postcheck"), Mapping)


def _valid_timings(value: Mapping) -> bool:
    return isinstance(value.get("branches"), Mapping) or isinstance(
        value.get("steps"), (list, Mapping)
    )


def _valid_remediation_proposals(value: Mapping, date: str | None) -> bool:
    envelope = (
        _valid_date(value.get("date"))
        and (date is None or value.get("date") == date)
        and isinstance(value.get("proposals"), list)
    )
    # The remediation producer currently emits v1. Keep v1 as the
    # canonical proposal contract until the producer is versioned to v2.
    return envelope and (
        _valid_schema_version(value.get("schema_version"), 1)
        or _valid_schema_version(value.get("schema_version"), 2)
    )


def _valid_skill_curate(value: Mapping, date: str | None, allow_legacy_v1: bool) -> bool:
    mode = value.get("mode")
    common = (
        mode in {"dry-run", "dry_run", "apply"}
        and isinstance(value.get("dry_run"), bool)
        and isinstance(value.get("decisions"), list)
        and _coerce_rc(value.get("rc"), default=None) is not None
        and _valid_date(value.get("date"))
        and (date is None or value.get("date") == date)
    )
    if not common:
        return False
    if _valid_schema_version(value.get("schema_version"), 2):
        return (
            _nonempty_text(value.get("generated_at"))
            and _nonempty_text(value.get("anchor"))
            and isinstance(value.get("summary"), Mapping)
            and isinstance(value.get("skipped_details"), list)
        )
    return allow_legacy_v1 and _valid_schema_version(value.get("schema_version"), 1)


#: Artifact name → shape validator (date/legacy variants handled separately).
_ARTIFACT_CONTRACTS: dict[str, Callable[[Mapping], bool]] = {
    "weekly-summary": _valid_weekly_summary,
    "weekly-insights": _valid_weekly_insights,
    "weekly-harness-digest": _valid_harness_digest,
    "weekly-ecosystem": _valid_ecosystem,
    "weekly-quality-findings": _has_findings_list,
    "weekly-watch-findings-raw": _has_findings_list,
    "weekly-coherence-findings": _valid_coherence,
    "weekly-audit-candidates": _valid_audit_candidates,
    "weekly-watch-context": _valid_watch_context,
    "weekly-watch-findings": _valid_watch_findings,
    "watch-candidates": _valid_watch_candidates,
    "weekly-harness-remediation": _valid_remediation,
    "weekly-timings": _valid_timings,
}


def _artifact_contract_valid(
    name: str,
    value: object,
    *,
    date: str | None = None,
    allow_legacy_v1: bool = False,
) -> bool:
    """Validate the exact JSON shape of a required/recovered artifact.

    The report gate must not treat ``{}`` as a successful producer output.  A
    compact contract is kept here rather than importing producer modules so the
    report remains usable when an optional branch is not installed.
    """
    if not isinstance(value, Mapping) or not value:
        return False
    if name == "weekly-harness-remediation-proposals":
        return _valid_remediation_proposals(value, date)
    if name == "skill-curate":
        return _valid_skill_curate(value, date=date, allow_legacy_v1=allow_legacy_v1)
    validator = _ARTIFACT_CONTRACTS.get(name)
    if validator is not None:
        return validator(value)
    # Unknown required names still need a non-empty object.  The path and file
    # name are validated by ``validate_required_artifacts`` below.
    return True


def _text_values(value: object) -> Iterable[str]:
    """Yield searchable text from one warning/manifest record."""
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, (str, int, float, bool)):
                yield f"{key}={item}"
            elif isinstance(item, (Mapping, list)):
                yield from _text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _text_values(item)


def _record_text(record: object) -> str:
    return " ".join(_text_values(record)).casefold()


def _is_report_only_record(record: object, *, project_root: Path | str | None = None) -> bool:
    """Recognize external, report-only permission outcomes.

    A bare ``permission denied`` or a producer-supplied ``report_only`` flag is
    intentionally *not* enough.  The record must identify a permission refusal
    and an absolute target that is provably outside the worktree.
    """
    if not isinstance(record, Mapping) or project_root is None:
        return False
    if record.get("report_only") is not True:
        return False
    if str(record.get("status") or "").casefold() != "report-only":
        return False
    if str(record.get("category") or "").casefold() != "external-permission-refusal":
        return False
    refusal_marker = _record_field(
        record,
        "permission_refused",
        "permission_denied",
        "permission_error",
        "external_permission_refusal",
    )
    if refusal_marker is not True and str(refusal_marker or "").casefold() not in {
        "permission denied",
        "permission refused",
        "external permission refusal",
    }:
        return False
    target = _record_field(
        record,
        "target",
        "target_path",
        "path",
        "destination",
        "output_path",
        "html_report_dir",
    )
    # The configured project root is trusted; worker-supplied roots are not.
    return _path_is_outside_worktree(target, project_root)


def _is_external_permission_failure(cfg: TelemetryConfig, exc: BaseException) -> bool:
    """Whether an HTML/report permission error targets outside the worktree."""
    if not isinstance(exc, PermissionError):
        return False
    project_root = getattr(cfg, "project_root", None)
    configured = getattr(cfg, "html_report_dir", None)
    if not project_root or not configured:
        return False
    try:
        root = Path(project_root).expanduser().resolve(strict=False)
        target = Path(configured).expanduser()
        if not target.is_absolute():
            target = root / target
        target = target.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return _path_is_outside_worktree(target, root)


def _is_recovered_record(record: object) -> bool:
    """Return whether a warning describes a successfully recovered input."""
    if not isinstance(record, Mapping):
        return False
    if record.get("recovered") is True or record.get("recovery") in (True, "ok", "success"):
        return True
    status = str(record.get("status") or "").casefold()
    if status in {"recovered", "recovered-input", "fallback-recovered"}:
        return True
    text = _record_text(record)
    return (
        any(marker in text for marker in _NONBLOCKING_WARNING_MARKERS[2:])
        or "recovered" in text
        or "fallback" in text
        or "récupér" in text
    ) and any(marker in text for marker in _RECOVERABLE_INPUT_MARKERS)


def _artifact_entry_valid(entry: object) -> bool:
    """Check the permissive artifact-input shapes used by joins and reports."""
    if not isinstance(entry, Mapping):
        return False
    status = str(entry.get("status") or "").casefold()
    if status in {"present", "valid", "ok", "recovered"}:
        return entry.get("valid", True) is not False
    return entry.get("present") is True or entry.get("valid") is True


def _summary_artifact_inputs(summary: object) -> Mapping[str, object]:
    if not isinstance(summary, Mapping):
        return {}
    inputs = summary.get("artifact_inputs")
    return inputs if isinstance(inputs, Mapping) else {}


def _audit_envelope_valid(value: object, session_id: str) -> bool:
    """Validate one worker audit envelope at its canonical run-local path."""
    if not isinstance(value, Mapping):
        return False
    rc = _coerce_rc(value.get("rc"), default=None)
    return (
        _valid_schema_version(value.get("schema_version"), 1)
        and value.get("session_id") == session_id
        and _nonempty_text(value.get("summary"))
        and isinstance(value.get("findings"), list)
        and rc in {0, 1}
        and isinstance(value.get("warnings"), list)
    )


#: Miroir des retours non-ok de ``_audit_envelope_reason`` ci-dessous — tout
#: nouveau motif de rejet doit être ajouté ici (comptage résilience Task 6).
_AUDIT_ENVELOPE_REASONS = frozenset(
    {
        "not-mapping",
        "bad-schema-version",
        "sid-mismatch",
        "empty-summary",
        "bad-findings",
        "bad-rc",
        "bad-warnings",
    }
)


def _audit_envelope_reason(value: object, session_id: str) -> str:
    """Return a stable machine-readable cause for an invalid audit envelope."""
    if not isinstance(value, Mapping):
        return "not-mapping"
    if not _valid_schema_version(value.get("schema_version"), 1):
        return "bad-schema-version"
    if value.get("session_id") != session_id:
        return "sid-mismatch"
    if not _nonempty_text(value.get("summary")):
        return "empty-summary"
    if not isinstance(value.get("findings"), list):
        return "bad-findings"
    if _coerce_rc(value.get("rc"), default=None) not in {0, 1}:
        return "bad-rc"
    if not isinstance(value.get("warnings"), list):
        return "bad-warnings"
    return "ok"


def _audit_declaration_values(record: object) -> list[str]:
    """Return structured audit artifact declarations without parsing text."""
    if not isinstance(record, Mapping):
        return []
    values: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Mapping):
            for key in (
                "path",
                "artifact",
                "artifact_path",
                "audit_artifact",
                "audit_artifact_path",
            ):
                if key in value:
                    collect(value[key])
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for key in (
        "audit_artifact",
        "audit_artifact_path",
        "artifact",
        "artifact_path",
        "artifacts",
        "required_artifact",
        "required_artifacts",
    ):
        if key in record:
            collect(record[key])
    return values


def _canonical_audit_path(out: Path, session_id: str) -> Path | None:
    """Build the only accepted audit path for one session id."""
    if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
        return None
    path = out / f"audit-findings-{session_id}.json"
    try:
        if path.resolve(strict=False).parent != out.resolve(strict=False):
            return None
    except (OSError, RuntimeError):
        return None
    return path


def _audit_declaration_matches(out: Path, session_id: str, record: object) -> bool:
    """Require a declaration equal to the exact canonical run-local filename."""
    canonical = _canonical_audit_path(out, session_id)
    if canonical is None:
        return False
    expected_name = canonical.name
    try:
        expected_path = str(canonical.resolve(strict=False))
    except (OSError, RuntimeError):
        return False
    return any(
        declaration in (expected_name, expected_path)
        for declaration in _audit_declaration_values(record)
    )


def _audit_artifact_valid(
    out: Path | None,
    date: str | None,
    session_id: object,
    record: object | None = None,
) -> bool:
    """Validate the declared audit envelope at its exact run-local path."""
    del date  # audit filenames are session-scoped, not date-scoped
    if out is None or not isinstance(session_id, str):
        return False
    path = _canonical_audit_path(out, session_id)
    if path is None or not _audit_declaration_matches(out, session_id, record):
        return False
    data, state = _json_file_state(path)
    return state == "present" and _audit_envelope_valid(data, session_id)


def _recoverable_artifact_valid(
    out: Path | None,
    date: str | None,
    source_text: str,
    artifact: object | None = None,
) -> bool:
    """Validate the named recovered artifact on disk, using its exact schema."""
    if out is None or not _valid_date(date):
        return False
    source = source_text.casefold()
    source_kind = next(
        (kind for kind in ("ecosystem", "watch", "proposal", "harness") if kind in source),
        None,
    )
    if source_kind is None:
        return False
    allowed = set(_RECOVERABLE_ARTIFACT_STEMS[source_kind])
    if isinstance(artifact, list):
        return any(
            _recoverable_artifact_valid(out, date, source_text, candidate) for candidate in artifact
        )
    if not isinstance(artifact, str) or not artifact.strip():
        # Recovery must identify the replacement.  Guessing from any valid
        # artifact in the run lets unrelated branch output mask a missing one.
        return False
    reference = artifact.strip().replace("\\", "/")
    reference_path = Path(reference)
    if reference_path.is_absolute():
        try:
            reference_path = reference_path.resolve(strict=False)
            run_root = out.resolve(strict=False)
            reference_path.relative_to(run_root)
            if reference_path.parent != run_root:
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        filename = reference_path.name
    else:
        if "/" in reference or reference.startswith("."):
            return False
        filename = reference
    if filename.endswith(".json"):
        filename = filename[:-5]
    suffix = f"-{date}"
    if filename.endswith(suffix):
        filename = filename[: -len(suffix)]
    if filename not in allowed:
        return False
    path = out / f"{filename}-{date}.json"
    try:
        if path.resolve(strict=False).parent != out.resolve(strict=False):
            return False
    except (OSError, RuntimeError):
        return False
    value, state = _json_file_state(path)
    return state == "present" and _artifact_contract_valid(filename, value, date=date)


def _truncated_record_valid(
    record: object, summary: object, out: Path | None, date: str | None
) -> bool:
    """Validate a truncated audit via disk or a declared artifact input."""
    del summary
    session_id = record.get("session_id") if isinstance(record, Mapping) else None
    return _audit_artifact_valid(out, date, session_id, record)


def _join_status_records(payload: object) -> list[object]:
    """Extract worker/join status records without imposing one schema version."""
    if not isinstance(payload, Mapping):
        return []
    records: list[object] = []
    for key in ("warnings", "worker_statuses", "statuses", "contracts"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and payload.get("artifacts"):
                    # Keep the exact artifact declaration attached to a string
                    # warning; otherwise recovery validation would have to
                    # guess which branch output was recovered.
                    records.append({"message": item, "artifacts": payload["artifacts"]})
                else:
                    records.append(item)
        elif isinstance(value, Mapping):
            records.append(value)
    for key in ("branches", "workers", "join"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            for record in value.values():
                if isinstance(record, Mapping):
                    records.append(record)
                    records.extend(_join_status_records(record))
    return records


def _audit_artifact_declarations(payload: object) -> list[str]:
    """Collect dynamic audit filenames from structured JOIN declarations."""
    declarations: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key in (
                "audit_artifact",
                "audit_artifact_path",
                "artifact",
                "artifact_path",
                "artifacts",
                "required_artifact",
                "required_artifacts",
            ):
                item = value.get(key)
                if isinstance(item, str):
                    if item.startswith("audit-findings-") or "audit-findings-" in item:
                        declarations.append(item)
                elif isinstance(item, list):
                    for candidate in item:
                        if isinstance(candidate, str) and (
                            candidate.startswith("audit-findings-")
                            or "audit-findings-" in candidate
                        ):
                            declarations.append(candidate)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return declarations


def _declared_run_filename(item: str, out: Path | None = None) -> str | None:
    """Return a declaration filename only when its run-local path is exact."""
    if not item or "\\" in item:
        return None
    path = Path(item)
    if path.is_absolute():
        if out is None:
            return None
        try:
            resolved = path.resolve(strict=False)
            run_root = out.resolve(strict=False)
            if resolved.parent != run_root:
                return None
            return resolved.name
        except (OSError, RuntimeError):
            return None
    if "/" in item or item.startswith("."):
        return None
    return item


def _branch_applicability(
    payload: object, date: str, *, out: Path | None = None
) -> dict[str, bool]:
    """Extract exact branch artifact declarations from a timings/join payload."""
    applicable: dict[str, bool] = {}
    optional_names = {
        "weekly-insights",
        "weekly-harness-digest",
        "weekly-ecosystem",
        "weekly-quality-findings",
        "weekly-coherence-findings",
        "skill-curate",
        "weekly-audit-candidates",
        "weekly-watch-context",
        "weekly-watch-findings",
        "weekly-watch-findings-raw",
        "weekly-harness-remediation",
        "weekly-harness-remediation-proposals",
        "weekly-timings",
    }

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            declared: list[object] = []
            for key in ("artifact", "artifacts", "required_artifact", "required_artifacts"):
                item = value.get(key)
                if isinstance(item, list):
                    declared.extend(item)
                elif item is not None:
                    declared.append(item)
            for item in declared:
                if not isinstance(item, str):
                    continue
                filename = _declared_run_filename(item, out)
                if filename is None:
                    continue
                match = re.fullmatch(
                    r"([A-Za-z0-9][A-Za-z0-9_-]*)-(\d{4}-\d{2}-\d{2})\.json", filename
                )
                if match and match.group(2) == date and match.group(1) in optional_names:
                    applicable[match.group(1)] = True
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return applicable


def _warning_is_nonblocking(
    warning: object,
    *,
    summary: object,
    out: Path | None = None,
    date: str | None = None,
    project_root: Path | str | None = None,
) -> bool:
    """Whether a warning is informational under the final JOIN contract."""
    if isinstance(warning, str):
        # A serialized warning has no trusted structure.  In particular, never
        # downgrade a run by substring-matching report-only fields in text.
        return False
    if not isinstance(warning, Mapping):
        return False
    report_only = _is_report_only_record(warning, project_root=project_root)
    if report_only or _is_recovered_record(warning):
        # Recovered/report-only inputs are only harmless when the replacement
        # input is observable and valid.  Explicit producers can point to it;
        # otherwise require a valid watch/harness/proposal artifact in the run.
        if report_only:
            return True
        source = _record_text(warning)
        artifact = _record_field(warning, "artifact", "artifact_path", "artifacts")
        return _recoverable_artifact_valid(out, date, source, artifact)

    text = _record_text(warning)
    transcript_prefix = next(
        (marker for marker in _NONBLOCKING_WARNING_MARKERS[:2] if marker in text), None
    )
    if transcript_prefix:
        match = text.split(transcript_prefix, 1)
        sid = match[1].split()[0].strip(" ,;)]") if len(match) == 2 else None
        transcript_record = dict(warning)
        transcript_record["session_id"] = sid
        return _truncated_record_valid(transcript_record, summary, out, date)

    # Optional inputs are observable but never required to make a report.  A
    # producer can mark the warning directly or identify the optional artifact.
    if warning.get("optional") is True or warning.get("applicable") is False:
        return True
    for key, value in _summary_artifact_inputs(summary).items():
        if not isinstance(value, Mapping) or value.get("required") is not False:
            continue
        if (
            _artifact_entry_valid(value) or value.get("status") in {"absent", "not_applicable"}
        ) and (str(key).casefold() in text or str(value.get("path", "")).casefold() in text):
            return True
    return False


def _coerce_summary_rc(summary: Mapping, fallback_rc: int | None) -> int:
    """Coerce the summary-level rc/exit (partial=1, never an accidental success)."""
    raw_value = summary.get("rc")
    if raw_value is None:
        raw_value = summary.get("exit")
    if raw_value is None:
        # Generated summaries before the JOIN contract have no rc/exit field.
        # Callers that know the process result must pass it explicitly; direct
        # use without a fallback is partial, never an accidental success.
        raw = _coerce_rc(fallback_rc, default=None)
        if raw is None:
            raw = 1
    else:
        raw = _coerce_rc(raw_value, default=None)
    if raw is None:
        raw = 1
    return raw


def _collect_join_records(summary: Mapping, additional_records: Iterable[object]) -> list[object]:
    """Gather warnings, worker statuses, recovered inputs and join records."""
    warnings = summary.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    warnings = [
        warning
        for warning in warnings
        if not (isinstance(warning, Mapping) and warning.get("partial") is False)
    ]
    recovered_inputs = summary.get("recovered_inputs")
    if not isinstance(recovered_inputs, list):
        recovered_inputs = []
    for key in ("recovered_watch_inputs", "recovered_proposal_inputs"):
        value = summary.get(key)
        if isinstance(value, list):
            recovered_inputs.extend(value)
    report_only_permissions = summary.get("report_only_permissions")
    if not isinstance(report_only_permissions, list):
        report_only_permissions = []
    worker_statuses: list[object] = []
    status_sources = [summary.get("worker_statuses")]
    selection = summary.get("selection")
    if isinstance(selection, Mapping):
        status_sources.append(selection.get("worker_statuses"))
    for source in status_sources:
        if isinstance(source, list):
            worker_statuses.extend(
                item
                for item in source
                if not isinstance(item, Mapping)
                or _coerce_rc(item.get("rc"), default=0) not in (None, 0)
                or item.get("truncated") is True
                or item.get("worker_status") == "truncated"
                or item.get("status") in {"truncated", "error", "missing", "timeout"}
            )
    join_records = [
        record
        for record in additional_records
        if not (
            isinstance(record, Mapping)
            and _coerce_rc(record.get("rc"), default=0) == 0
            and not any(
                key in record
                for key in (
                    "warnings",
                    "truncated",
                    "worker_status",
                    "status",
                    "recovered",
                    "report_only",
                )
            )
        )
    ]
    return [
        *warnings,
        *worker_statuses,
        *recovered_inputs,
        *report_only_permissions,
        *join_records,
    ]


def _empty_records_rc(summary: Mapping, raw: int) -> int:
    """RC when no join records exist: blocking inputs fail, optional-only passes."""
    inputs = _summary_artifact_inputs(summary)
    valid_input_statuses = {"absent", "not_applicable", "present", "valid", "recovered"}
    optional_entries = [
        value
        for value in inputs.values()
        if isinstance(value, Mapping) and value.get("required") is False
    ]
    blocking_inputs = [
        value
        for value in inputs.values()
        if isinstance(value, Mapping)
        and value.get("required") is True
        and str(value.get("status") or "").casefold() not in {"present", "valid", "ok"}
    ]
    optional_only = (
        bool(optional_entries)
        and not blocking_inputs
        and all(
            str(value.get("status") or "").casefold() in valid_input_statuses
            for value in inputs.values()
            if isinstance(value, Mapping)
        )
    )
    if blocking_inputs:
        return 1
    if optional_only:
        return 0
    return 0 if raw == 0 else 1


def _records_all_nonblocking(
    records: list[object],
    summary: object,
    *,
    out: Path | None,
    date: str | None,
    project_root: Path | str | None,
) -> bool:
    """Whether every join record is an informational (non-blocking) fact."""
    return all(
        _warning_is_nonblocking(
            item,
            summary=summary,
            out=out,
            date=date,
            project_root=project_root,
        )
        or (
            isinstance(item, Mapping)
            and (
                item.get("truncated") is True
                or item.get("worker_status") == "truncated"
                or item.get("status") == "truncated"
            )
            and _truncated_record_valid(item, summary, out, date)
        )
        for item in records
    )


def applicable_summary_rc(
    summary: object,
    *,
    out: Path | None = None,
    date: str | None = None,
    additional_records: Iterable[object] = (),
    project_root: Path | str | None = None,
    fallback_rc: int | None = None,
) -> int:
    """Compute the summary process status without counting report-only facts.

    ``weekly_run`` and external joins historically surfaced every partial worker
    warning as ``1``.  The final contract treats valid transcript-truncated,
    recovered optional inputs and external report-only permissions as facts, not
    failures.  Fatal ``2`` is never downgraded.
    """
    if not isinstance(summary, Mapping):
        return 2
    raw = _coerce_summary_rc(summary, fallback_rc)

    records = _collect_join_records(summary, additional_records)
    if raw >= 2 or any(
        isinstance(record, Mapping) and _coerce_rc(record.get("rc"), default=0) >= 2
        for record in records
    ):
        return 2
    if not records:
        return _empty_records_rc(summary, raw)
    return (
        0
        if _records_all_nonblocking(records, summary, out=out, date=date, project_root=project_root)
        else 1
    )


def _artifact_provenance(out: Path, date: str) -> dict[str, dict[str, object]]:
    """Describe report inputs without relying on mutable process state."""
    return validate_required_artifacts(out, date)["artifacts"]


def _check_required_json(
    out: Path,
    name: str,
    date: str,
    required_set: set[str],
    applicable: Mapping[str, bool],
) -> dict[str, object]:
    """État d'un artefact JSON : lu exactement une fois, gate requis/optionnel."""
    path = out / f"{name}-{date}.json"
    status = "absent"
    path_valid = (
        _valid_date(date)
        and isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) is not None
        and path.name == f"{name}-{date}.json"
    )
    data: object | None = None
    schema_valid = False
    if not path_valid:
        status = "ill_readable"
    elif path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else None
            parsed = isinstance(data, dict)
            strict = name in required_set
            schema_valid = parsed and _artifact_contract_valid(name, data, date=date)
            status = "present" if parsed and (schema_valid or not strict) else "ill_readable"
        except (OSError, UnicodeError, json.JSONDecodeError):
            status = "ill_readable"
    applicable_default = name in required_set or status != "absent"
    return {
        "path": str(path),
        "present": status == "present",
        "status": status,
        "required": name in required_set,
        "path_valid": path_valid,
        "schema_valid": schema_valid,
        # Un producteur optionnel absent n'est pas la preuve que sa branche
        # était activée et a échoué ; les fichiers existants sont applicables
        # par définition.
        "applicable": bool(applicable.get(name, applicable_default)),
    }


def _check_dynamic_audit_artifact(
    declaration: object, out: Path, taken: int
) -> tuple[str, dict[str, object]] | None:
    """Un artefact d'audit dynamique : (clé, fiche) — None si clé déjà prise."""
    raw_declaration = str(declaration) if isinstance(declaration, str) else ""
    filename = _declared_run_filename(raw_declaration, out)
    match = re.fullmatch(r"audit-findings-(.+)\.json", filename) if filename is not None else None
    key = filename or f"audit-findings-declaration-{taken}"
    path = out / filename if filename is not None else out / raw_declaration
    sid = match.group(1) if match else ""
    data, state = _json_file_state(path) if match else (None, "ill_readable")
    path_ok = bool(match and _canonical_audit_path(out, sid) == path)
    envelope_reason = _audit_envelope_reason(data, sid) if path_ok and state == "present" else "ok"
    # Un artefact absent/illisible n'est jamais valide : sans le `state ==
    # "present"`, un audit manquant passait la gate en `present` (run 16/09).
    valid = bool(path_ok and state == "present" and envelope_reason == "ok")
    if not path_ok:
        reason = "path-mismatch"
    elif state == "absent":
        reason = "absent"
    elif state != "present":
        reason = "ill-readable"
    else:
        reason = envelope_reason
    return key, {
        "path": str(path),
        "present": valid,
        "status": "present" if valid else ("absent" if state == "absent" else "ill_readable"),
        "required": True,
        "path_valid": path_ok,
        "schema_valid": valid,
        "reason": reason,
        "applicable": True,
    }


def _check_html_artifact(path: Path | None) -> dict[str, object]:
    """État d'un artefact HTML : absent / illisible / présent."""
    result: dict[str, object] = {"status": "absent", "path": str(path) if path else None}
    if path is None:
        return result
    try:
        if not path.is_file():
            result["status"] = "absent"
        elif not path.read_text(encoding="utf-8").strip():
            result["status"] = "ill_readable"
        else:
            result["status"] = "present"
    except (OSError, UnicodeError):
        result["status"] = "ill_readable"
    return result


def _html_gate_status(html_path: Path | None, html_enabled: bool) -> dict[str, object]:
    """Gate HTML : un fichier réellement produit prime sur le flag de config."""
    if html_path is not None:
        return _check_html_artifact(html_path)
    if html_enabled:
        return {"status": "absent", "path": None}
    return {"status": "disabled", "path": None}


def _summarize_artifact_gate(
    required: dict[str, dict[str, object]],
    optional: dict[str, dict[str, object]],
    html: dict[str, object],
) -> dict[str, object]:
    """Verdict final : required pass/incomplete + listes optionnelles manquantes."""
    required_status = (
        "pass"
        if required and all(a["status"] == "present" for a in required.values())
        else "incomplete"
    )
    optional_missing = [
        entry["path"]
        for entry in optional.values()
        if entry["status"] == "absent" and entry.get("applicable", True)
    ]
    optional_ill_readable = [
        entry["path"]
        for entry in optional.values()
        if entry["status"] == "ill_readable" and entry.get("applicable", True)
    ]
    return {
        "required": required,
        "optional": optional,
        "artifacts": {**required, **optional},
        "status": required_status,
        "optional_missing": optional_missing,
        "optional_ill_readable": optional_ill_readable,
        "html": html,
    }


def validate_required_artifacts(
    out: Path,
    date: str,
    *,
    html_enabled: bool = False,
    html_path: Path | None = None,
    required_names: Iterable[str] | None = None,
    applicability: Mapping[str, bool] | None = None,
    dynamic_audit_artifacts: Iterable[object] = (),
) -> dict[str, object]:
    """Validate report inputs once, with deterministic required/optional gates.

    JSON inputs are read exactly once each.  Optional upstream artefacts remain
    visible when absent, but cannot make an otherwise usable report fail.  HTML
    is a conditional artefact: callers can pass ``html_path`` after rendering
    to distinguish a renderer failure from a missing or unreadable output.
    """
    names = {
        "required": tuple(required_names or ("weekly-summary",)),
        "optional": (
            "weekly-insights",
            "weekly-harness-digest",
            "weekly-ecosystem",
            "weekly-quality-findings",
            "weekly-coherence-findings",
            "skill-curate",
            "weekly-audit-candidates",
            "weekly-watch-context",
            "weekly-watch-findings",
            "weekly-watch-findings-raw",
            "weekly-harness-remediation",
            "weekly-harness-remediation-proposals",
            "weekly-timings",
        ),
    }
    explicit_required = {str(name) for name in names["required"]}
    # A branch explicitly marked applicable is required even when its producer
    # is normally optional.  Disabled branches stay visible in ``optional`` and
    # cannot fail the gate merely because they have no output.
    branch_required = {
        str(name) for name, enabled in (applicability or {}).items() if enabled is True
    }
    required_set = explicit_required | branch_required
    optional_names = tuple(name for name in names["optional"] if name not in required_set)
    names["optional"] = optional_names
    applicable = applicability or {}

    required = {
        f"{name}-{date}.json": _check_required_json(out, name, date, required_set, applicable)
        for name in sorted(required_set)
    }

    dynamic_required: dict[str, dict[str, object]] = {}
    for declaration in dynamic_audit_artifacts:
        checked = _check_dynamic_audit_artifact(declaration, out, len(dynamic_required))
        if checked is None:
            continue
        key, record = checked
        if key in dynamic_required:
            continue
        dynamic_required[key] = record
    required.update(dynamic_required)
    optional = {
        f"{name}-{date}.json": _check_required_json(out, name, date, required_set, applicable)
        for name in names["optional"]
    }

    # P0 : un fichier HTML réellement produit prime sur le flag de config —
    # `disabled` uniquement si rendu off ET aucun fichier produit.
    html = _html_gate_status(html_path, html_enabled)
    return _summarize_artifact_gate(required, optional, html)


_BLOCKING_SECURITY_RULES = BLOCKING_RULES


def _is_blocking_security_rule(rule: object) -> bool:
    """Match only the documented critical rule identifiers."""
    normalized = str(rule or "").strip().lower()
    normalized = normalized.removeprefix("security/")
    return normalized in _BLOCKING_SECURITY_RULES


def _blocking_security_findings(
    digest: object, *, ignored_rules: Iterable[str] | None = None
) -> list[dict]:
    """Return only the exact security rules that block final status."""
    return [
        finding
        for finding in _critical_security_findings(digest, ignored_rules=ignored_rules)
        if _is_blocking_security_rule(finding.get("rule") or finding.get("id"))
    ]


_SECURITY_RULE_PARAPHRASE = SECURITY_PARAPHRASE


def _security_rule_paraphrase(rule: object) -> str:
    """Paraphrase générique d'une règle (jamais de verbatim de finding)."""
    short = str(rule or "").strip().lower().removeprefix("security/")
    return _SECURITY_RULE_PARAPHRASE.get(short, "Signal de sécurité — revue humaine requise.")


def _coerce_non_negative_int(value: object) -> int | None:
    """int ≥ 0 depuis un JSON, ou None si absent/illisible (jamais 0 par défaut)."""
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _render_security_section(security: dict | None) -> str:
    """Section Sécurité repliée (MD) — counts/rules only, warn-only.

    Entrées Task1 uniquement : ``security.{status,critical_count,
    blocking_count,blocking_rules}`` (gates JSON). N'accepte jamais de
    findings bruts : aucune procédure de finding n'est recopiée, chaque
    règle est résumée en termes génériques (≤200 caractères, top-5 max).
    Retourne un bloc ``<details id="security">`` replié par défaut.
    """
    # <!-- ponytail: counts-only — aucun finding brut, paraphrases génériques -->
    status = "pass"
    critical_count = 0
    blocking_count = 0
    blocking_rules: list[str] = []
    by_rule: dict[str, int] = {}
    blocking_by_rule: dict[str, int] = {}
    findings_raw: int | None = None
    findings_unique: int | None = None
    if isinstance(security, dict):
        raw_status = str(security.get("status") or "pass").strip().lower()
        status = raw_status if raw_status in {"pass", "warn", "fail"} else "pass"
        try:
            critical_count = max(0, int(security.get("critical_count") or 0))
        except (TypeError, ValueError):
            critical_count = 0
        try:
            blocking_count = max(0, int(security.get("blocking_count") or 0))
        except (TypeError, ValueError):
            blocking_count = 0
        for target, key in ((by_rule, "by_rule"), (blocking_by_rule, "blocking_by_rule")):
            raw_counts = security.get(key)
            if isinstance(raw_counts, Mapping):
                for rule, count in raw_counts.items():
                    try:
                        target[str(rule)] = max(0, int(count))
                    except (TypeError, ValueError):
                        continue
        for key in ("digest_findings_raw", "digest_findings_unique"):
            value = _coerce_non_negative_int(security.get(key))
            if key == "digest_findings_raw":
                findings_raw = value
            else:
                findings_unique = value
        raw_rules = security.get("blocking_rules") or []
        if isinstance(raw_rules, list):
            seen: set[str] = set()
            ordered: list[str] = []
            for r in raw_rules:
                name = str(r or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                ordered.append(name if "/" in name else f"security/{name}")
            blocking_rules = ordered[:5]
    lines = [
        '<details id="security">',
        f"<summary>Sécurité — {status} · {critical_count} critical · {blocking_count} blocking</summary>",
        "",
    ]
    if status == "pass" and critical_count == 0 and blocking_count == 0:
        lines.append("Aucun finding security/critical ce run.")
    else:
        lines.append(f"- Statut : **{status}** (warn-only, rapport écrit).")
        lines.append(
            f"- Findings critical : **{critical_count}** · blocking : **{blocking_count}**."
        )
        # FIX 8 : sans ces lignes, `critical_count = 1316` face à un digest
        # déclarant `findings_raw = 32` / `findings_unique = 29` se lit comme un
        # bug de comptage. Les trois chiffres ne sont pas le même population.
        lines.append("- **Comptage** — les trois chiffres ci-dessous ne sont pas comparables :")
        lines.append(
            f"  - `critical_count` = **{critical_count}** : parcours récursif de tout le "
            f"digest (y compris `inspection.*.findings`, même quand le tableau `findings` "
            f"de premier niveau est vide)."
        )
        if findings_raw is not None or findings_unique is not None:
            raw_txt = "n/a" if findings_raw is None else str(findings_raw)
            uniq_txt = "n/a" if findings_unique is None else str(findings_unique)
            lines.append(
                f"  - digest `findings_raw` = **{raw_txt}** · `findings_unique` = "
                f"**{uniq_txt}** : même population après déduplication harness-eval."
            )
        lines.append(
            f"  - `blocking_count` = **{blocking_count}** : après application de l'allowlist "
            f"des {len(_BLOCKING_SECURITY_RULES)} règles bloquantes."
        )
        lines.append(
            "  - _Ne pas en déduire un delta : les trois sources ne comptent pas les mêmes objets._"
        )
        if by_rule:
            lines.append("- Répartition des findings critical par règle (top 5) :")
            for rule, count in list(by_rule.items())[:5]:
                lines.append(f"  - `{rule}` — {count}")
        if blocking_rules:
            lines.append("- Règles bloquantes (top 5, intitulés seuls) :")
            for rule in blocking_rules:
                row = f"  - `{rule}` — {_security_rule_paraphrase(rule)}"
                lines.append(truncate_text(row, _WATCH_SUMMARY_LIMIT))
        if blocking_by_rule:
            lines.append("- Répartition des findings bloquants par règle :")
            for rule, count in list(blocking_by_rule.items())[:5]:
                lines.append(f"  - `{rule}` — {count}")
        # Nommées explicitement : ces 3 règles sont l'allowlist bloquante du gate.
        lines.append(
            "- Règles bloquantes exactes (allowlist du gate) : "
            + ", ".join(f"`{rule}`" for rule in sorted(_BLOCKING_SECURITY_RULES))
            + "."
        )
        lines.append("- Revue humaine requise — détails non affichés.")
    lines.append("</details>")
    return "\n".join(lines) + "\n"


def _gate_status(provenance: dict[str, dict[str, object]]) -> dict[str, object]:
    """Machine-readable artifact gate; missing optional inputs remain explicit."""
    artifacts = list(provenance.values())
    required = {
        name: artifact
        for name, artifact in provenance.items()
        if artifact.get("required", name.startswith("weekly-summary-"))
    }
    optional = {name: artifact for name, artifact in provenance.items() if name not in required}
    missing = [
        a["path"]
        for a in artifacts
        if a["status"] == "absent" and (a.get("required") or a.get("applicable", True))
    ]
    ill_readable = [
        a["path"]
        for a in artifacts
        if a["status"] == "ill_readable" and (a.get("required") or a.get("applicable", True))
    ]
    return {
        "required": required,
        "optional": optional,
        "artifacts": {
            "status": "pass"
            if all(a["status"] == "present" for a in required.values())
            else "incomplete",
            "missing": missing,
            "ill_readable": ill_readable,
            "optional_missing": [
                a["path"]
                for a in artifacts
                if a["status"] == "absent" and not a.get("required") and a.get("applicable", True)
            ],
            "optional_ill_readable": [
                a["path"]
                for a in artifacts
                if a["status"] == "ill_readable"
                and not a.get("required")
                and a.get("applicable", True)
            ],
        },
        "prose": {"status": "not_validated"},
        "html": {"status": "not_run"},
        "security": {
            "status": "pass",
            "critical_count": 0,
            "blocking_count": 0,
            "blocking_rules": sorted(_BLOCKING_SECURITY_RULES),
        },
        "blocking_rules": sorted(_BLOCKING_SECURITY_RULES),
    }


_CURATION_TAG_ACTIONS = {"archive", "merge", "pin", "reference", "delete", "recalibrate"}


def _coherence_has_curation_signal(coherence: object) -> bool:
    """Vrai si les findings de cohérence portent ≥1 action de curation.

    Accepte un dict (champ ``curation_signal`` ou ``findings[]``) ou une liste
    (findings bruts). Défensif : toute entrée illisible est ignorée.
    """
    if not coherence:
        return False
    if isinstance(coherence, dict):
        sig = coherence.get("curation_signal")
        if isinstance(sig, list) and sig:
            return True
        # R4 emits a mapping (rather than a list) for archive candidates.  Any
        # non-empty mapping is an actionable signal; do not require a specific
        # producer shape here so report gating remains forward-compatible.
        if isinstance(sig, dict) and sig:
            return True
        findings = coherence.get("findings") or []
    elif isinstance(coherence, list):
        findings = coherence
    else:
        return False
    return any(
        isinstance(f, dict) and f.get("tag_action") in _CURATION_TAG_ACTIONS for f in findings
    )


def _coherence_findings(coherence: object) -> list[dict]:
    """Return coherence findings in one deterministic shape for all reports."""
    if isinstance(coherence, list):
        return [finding for finding in coherence if isinstance(finding, dict)]
    if isinstance(coherence, dict):
        findings = coherence.get("findings")
        if isinstance(findings, list):
            return [finding for finding in findings if isinstance(finding, dict)]
    return []


def _curation_manifest_detail(manifest: object) -> dict:
    """Normalize curation v1/v2 manifests without changing their contracts."""
    if not isinstance(manifest, dict):
        return {"decisions": [], "skipped_details": [], "by_action": {}, "mode": None}
    decisions = manifest.get("decisions")
    skipped = manifest.get("skipped_details")
    summary = manifest.get("summary")
    normalized_decisions = (
        [item for item in decisions if isinstance(item, dict)]
        if isinstance(decisions, list)
        else []
    )
    decision_skips = [item for item in normalized_decisions if item.get("status") == "skipped"]
    raw_skips = (
        [item for item in skipped if isinstance(item, dict)]
        if isinstance(skipped, list)
        else decision_skips
    )
    # v2 carries skipped decisions in both arrays. Keep one rendered row.
    seen_skips: set[tuple[object, ...]] = set()
    skipped_details = []
    for item in raw_skips:
        key = tuple(
            item.get(field) for field in ("skill_id", "action", "source", "reason", "status")
        )
        if key not in seen_skips:
            seen_skips.add(key)
            skipped_details.append(item)
    by_action = summary.get("by_action") if isinstance(summary, dict) else None
    if not isinstance(by_action, dict):
        by_action = dict(
            sorted(Counter(str(item.get("action") or "") for item in normalized_decisions).items())
        )
    return {
        "decisions": normalized_decisions,
        "skipped_details": skipped_details,
        "by_action": by_action,
        "mode": manifest.get("mode"),
        "dry_run": manifest.get("dry_run"),
    }


def _parse_curation_manifest(manifest: object, date: str | None) -> tuple[list[Mapping], bool, int]:
    """Forme du manifeste : (decisions, is_dry_run, raw_rc) — ValueError(reason) sinon."""
    if not _artifact_contract_valid("skill-curate", manifest, date=date, allow_legacy_v1=True):
        raise ValueError("manifeste de curation malformé (schéma attendu absent ou invalide)")
    if not isinstance(manifest, Mapping):
        raise ValueError("manifeste de curation malformé (objet JSON attendu)")

    mode_raw = manifest.get("mode")
    mode = str(mode_raw).casefold() if mode_raw is not None else None
    if mode not in {"dry-run", "dry_run", "apply"}:
        raise ValueError(f"manifeste de curation malformé (mode={mode_raw!r})")
    dry_run = manifest.get("dry_run")
    if not isinstance(dry_run, bool):
        raise ValueError("manifeste de curation malformé (dry_run non booléen)")
    if mode in {"dry-run", "dry_run"} and dry_run is False:
        raise ValueError("manifeste de curation malformé (dry-run incohérent)")
    if mode == "apply" and dry_run is True:
        raise ValueError("manifeste de curation malformé (apply avec dry_run=true)")

    decisions = manifest.get("decisions")
    if not isinstance(decisions, list) or any(not isinstance(item, Mapping) for item in decisions):
        raise ValueError("manifeste de curation malformé (decisions invalides)")
    summary = manifest.get("summary")
    if summary is not None and not isinstance(summary, Mapping):
        raise ValueError("manifeste de curation malformé (summary invalide)")
    raw_rc = _coerce_rc(manifest.get("rc", 0), default=None)
    if raw_rc is None:
        raise ValueError("manifeste de curation malformé (rc invalide)")
    if raw_rc >= 2:
        raise ValueError("manifeste de curation fatal (rc=2)")
    return decisions, mode in {"dry-run", "dry_run"} or dry_run is True, raw_rc


def _manifest_refusal_verdict(
    decisions: list[Mapping],
    raw_rc: int,
    project_root: Path | str | None,
) -> tuple[int, str | None]:
    """Verdict refus d'application : dry-run déjà filtré en amont."""
    refusal_records: list[Mapping[str, object]] = []
    for decision in decisions or []:
        statuses = {
            str(decision.get("status") or "").casefold(),
            str(decision.get("move_status") or "").casefold(),
        }
        if statuses & _REFUSAL_STATUSES:
            refusal_records.append(decision)
    if (
        raw_rc == 1
        and refusal_records
        and all(_is_report_only_record(decision, project_root=project_root))
    ):
        return 0, None
    if raw_rc == 1:
        return 1, "manifeste de curation signale un refus d'application"
    if refusal_records:
        if all(
            _is_report_only_record(decision, project_root=project_root)
            for decision in refusal_records
        ):
            return 0, None
        return 1, "manifeste de curation contient un refus d'application"
    return 0, None


def _curation_manifest_gate(
    manifest: object,
    *,
    date: str | None = None,
    project_root: Path | str | None = None,
) -> tuple[int, str | None]:
    """Return ``(rc, reason)`` for one curation manifest.

    Dry-run is a report-only proposal phase.  Its skipped/proposed decisions and
    even a non-fatal producer ``rc=1`` must not turn an otherwise valid report
    partial.  Apply refusals remain counted, except an explicitly external,
    report-only permission refusal.  A malformed manifest is always nonzero.
    """
    try:
        decisions, is_dry_run, raw_rc = _parse_curation_manifest(manifest, date)
    except ValueError as exc:
        return 2, str(exc)

    if is_dry_run:
        return 0, None
    return _manifest_refusal_verdict(decisions, raw_rc, project_root)


def _load_report_artifacts(out, date: str) -> dict | None:
    """Charge les artefacts JSON du run actif — None si summary absente/invalide."""
    summary = _load_json(out / f"weekly-summary-{date}.json")
    if not _artifact_contract_valid("weekly-summary", summary, date=date):
        return None
    digest = _load_json(out / f"weekly-harness-digest-{date}.json")
    for digest_problem in harness_digest_problems(digest):
        print(f"report: WARNING: {digest_problem}", file=sys.stderr, flush=True)
        digest = None
    audit_candidates = _load_json(out / f"weekly-audit-candidates-{date}.json")
    return {
        "summary": summary,
        "insights": _load_json(out / f"weekly-insights-{date}.json"),
        "digest": digest,
        "ecosystem": _load_json(out / f"weekly-ecosystem-{date}.json"),
        "findings": _load_json(out / f"weekly-quality-findings-{date}.json"),
        "coherence_findings": _load_json(out / f"weekly-coherence-findings-{date}.json"),
        "skill_curate": _load_json(out / f"skill-curate-{date}.json"),
        "audit_candidates": audit_candidates,
        "watch_findings": _load_json(out / f"weekly-watch-findings-{date}.json"),
        "harness_remediation": _load_json(out / f"weekly-harness-remediation-{date}.json"),
        # C11 : l'étape 4 produit ce fichier, le §Drafting le consomme. Absent ⇒
        # `draft_candidates` vaut None et la section reste muette (pas d'hypothèse).
        "draft_candidates": _load_json(out / f"weekly-draft-candidates-{date}.json"),
    }


def build_report_context(cfg: TelemetryConfig, *, anchor: str | None = None) -> dict | None:
    """Construit le ctx Jinja du rapport (v6.1) — partagé par prep et assemble.

    Reconstruit intégralement depuis les artefacts JSON du run actif à chaque
    appel : prep et assemble tournent comme sous-commandes CLI séparées, il n'y
    a donc aucune persistance inter-process. Retourne None si la summary du run
    est absente (le rapport HTML est alors silencieusement ignoré).
    """
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)

    artifacts = _load_report_artifacts(out, date)
    if artifacts is None:
        return None
    summary = artifacts["summary"]
    insights = artifacts["insights"]
    digest = artifacts["digest"]
    ecosystem = artifacts["ecosystem"]
    findings = artifacts["findings"]
    coherence_findings = artifacts["coherence_findings"]
    skill_curate = artifacts["skill_curate"]

    # FIX 6 : les résultats ENREGISTRÉS des appels `commit-draft` sont FUSIONNÉS
    # avec le git log, pas comptés à sa place. Borné par l'ancre de DÉBUT de run,
    # `git log` ne peut structurellement pas voir les commits que ce run vient de
    # drafter (ils sont postérieurs) : le décompte valait 0 à chaque run. Le log
    # reste par ailleurs SANS borne haute (cf. `_auto_commits` / `_pending_auto_commits`).
    timings = _load_json(out / f"weekly-timings-{date}.json")
    git_commits = _auto_commits(
        cfg.project_root,
        _iso(run_time - timedelta(hours=cfg.window_hours())),
        timings,
    )
    pending = _pending_auto_commits(
        cfg.project_root,
        _iso(run_time - timedelta(weeks=cfg.review_window_weeks)),
        timings,
    )
    head_commit = _head_commit(cfg.project_root)
    dirty_files = _dirty_files(cfg.project_root)
    # v6.0.l (E11) : delta par règle vs run précédent (null en first-run).
    lint_delta = ((insights or {}).get("deltas") or {}).get("lint_violations_delta_by_rule") or {}
    # C6 : l'écosystème est projeté (descriptions/résumés déjà tronqués sur frontière
    # de mot) AVANT le contexte, pour que le markdown et le HTML tronquent identiquement.
    eco_view = _ecosystem_view(ecosystem)
    raw_sessions = summary.get("all_sessions", [])
    if not isinstance(raw_sessions, list):
        raw_sessions = []
    all_sessions = [
        {
            **s,
            "title_short": truncate_text(
                s.get("title_or_topic") or s.get("session_id"), _SESSION_TITLE_LIMIT
            ),
        }
        if isinstance(s, dict)
        else s
        for s in raw_sessions
    ]

    provenance = {
        "anchor": run_time.isoformat(),
        "artifact_inputs": _artifact_provenance(out, date),
        "run_provenance": summary.get("run_provenance"),
    }
    # C8 : inventaire réel du run dir, une fois pour les DEUX rendus. Il est
    # dérivé après `provenance` parce qu'il se réconcilie avec les artefacts
    # déclarés par le gate — pas de liste de noms en dur dans un `.j2`.
    run_artifacts = _run_artifact_inventory(out, provenance["artifact_inputs"])
    warnings_grouped = _group_warnings(summary.get("warnings", []))
    ctx = {
        "date": date,
        "engine_version": __version__,
        "period": summary.get("period", {}),
        "summary": summary,
        "insights": insights,
        "digest": digest,
        "ecosystem": eco_view,
        "findings": findings,
        "models_top": _top_models(summary),
        "top_sessions": summary.get("top_sessions_by_cost", []),
        "all_sessions": all_sessions,
        "harness_breakdown": _harness_breakdown(summary),
        "harness_ignored_rules": list(cfg.harness_ignored_rules),
        "harness_top_rules": [
            {
                "rule": rule,
                "count": count,
                "delta": lint_delta.get(rule),
            }
            for rule, count in _top_harness_rules(digest, cfg.harness_ignored_rules)
        ],
        "top_next_steps": _top_next_steps(
            digest, insights, findings, ignored_rules=cfg.harness_ignored_rules
        ),
        "cost_outliers_state": summary.get("cost_outliers_state", "computed"),
        "outliers": {o["session_id"] for o in summary.get("cost_outliers", [])},
        "audit_candidates": artifacts["audit_candidates"],
        "audit_worker_statuses": ((artifacts["audit_candidates"] or {}).get("worker_statuses", [])),
        # C11 : section Drafting — candidats + cible hors périmètre nommée (D5).
        "draft_candidates": artifacts["draft_candidates"],
        "drafting": _drafting_view(artifacts["draft_candidates"], summary, cfg.project_root),
        "drafting_project_root": str(cfg.project_root or ""),
        "watch_findings": artifacts["watch_findings"],
        # C5/C6/C7 : la condensation et la troncature sont faites ICI, une fois pour
        # les deux rendus. Les gabarits ne portent plus ni limite en dur ni
        # déduplication : ils consomment `coherence_items` / `maint_sorted` /
        # `watch_groups` déjà projetés.
        "watch_groups": _group_watch_findings(artifacts["watch_findings"]),
        "coherence_findings": coherence_findings,
        "coherence_items": _dedupe_findings(
            _project_finding(f, evidence_limit=_EVIDENCE_LIMIT)
            for f in _coherence_findings(coherence_findings)
        ),
        "skill_curate": skill_curate,
        "curation_detail": _curation_manifest_detail(skill_curate),
        "coherence_curation_signal": _coherence_has_curation_signal(coherence_findings),
        "harness_budget": (digest or {}).get("budget"),
        "harness_triggers": (digest or {}).get("triggers"),
        "harness_dependencies": (digest or {}).get("dependencies"),
        "harness_scope": (digest or {}).get("harness_scope")
        or (digest or {}).get("harness_include"),
        "harness_counts": (digest or {}).get("harness_counts"),
        "run_dir": (active_run_meta(cfg.output_dir, date) or {}).get("run_dir"),
        "provenance": provenance,
        "gate_status": _gate_status(provenance["artifact_inputs"]),
        "harness_remediation": artifacts["harness_remediation"],
        "warnings_grouped": warnings_grouped,
        # D5 : `summary.warnings` ne porte plus les occurrences mais les ENTITÉS
        # (agrégateur regroupé) — la somme des `count` est le total réel.
        "warnings_occurrences": sum(int(g["count"]) for g in warnings_grouped),
        "run_artifacts": run_artifacts,
        "run_artifacts_total": len(run_artifacts),
        "run_artifacts_shown": min(_RUN_ARTIFACTS_LIST_LIMIT, len(run_artifacts)),
        "maint_sorted": _dedupe_findings(
            _project_finding(f, evidence_limit=_WATCH_EVIDENCE_LIMIT)
            for f in _sort_maintenance_findings(insights)
        ),
        "watch_warned": any(
            w.get("source") == "github:watch-repos" for w in (ecosystem or {}).get("warnings", [])
        ),
        "watch_items": [
            i
            for i in eco_view.get("new_items", [])
            if any(
                fv == "github:watch-repos" or fv.startswith("watch:")
                for fv in (i.get("found_via") or [])
            )
        ],
        "daily_totals": _complete_daily(summary.get("period", {}), summary.get("daily_totals", [])),
        "auto_commits": git_commits,
        "pending_auto_commits": pending,
        "head_commit": head_commit,
        "dirty_files": dirty_files,
        # E4 : une seule valeur de self-cost (artefact post-assemblage s'il
        # existe, sinon mesure live étiquetée « mesuré à l'assemblage »).
        **_self_cost_context(cfg, out, date),
    }
    return ctx


def report_prep(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, dict | None]:
    """Render deterministic sections into `weekly-report-draft-<date>.md`."""
    ctx = build_report_context(cfg, anchor=anchor)
    if ctx is None:
        return None, None

    date = ctx["date"]
    out = resolve_active_run_dir(cfg.output_dir, date)
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
    return draft, ctx


def report_blocks_draft(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, list[str], int]:
    """Deterministic draft of the section-4 blocks (v5.28, P5.1) — zero LLM.

    Writes `weekly-report-blocks-auto-<date>.md` from alerts, maintenance findings,
    notable sessions and top harness rules. Explicitly flagged as an automatic
    draft requiring human review. Ce texte est une file de repli : il est injecté
    VERBATIM sous le titre `## 4.` du gabarit et n'est jamais validé — il ne doit
    donc porter aucun titre de niveau 1 ou 2 (le gabarit en fournit déjà un). Ses
    `###` de sous-rubrique sont donc tolérés ici alors que la PROSE de l'agent est
    refusée sur toute ligne de titre (C1) : les deux chemins ne se valident pas
    l'un l'autre.
    """
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)

    summary = _load_json(out / f"weekly-summary-{date}.json")
    if not _artifact_contract_valid("weekly-summary", summary, date=date):
        return None, ["summary inexistante — lancer run d'abord"], 2
    insights = _load_json(out / f"weekly-insights-{date}.json")
    digest = _load_json(out / f"weekly-harness-digest-{date}.json")
    for digest_problem in harness_digest_problems(digest):
        print(f"report: WARNING: {digest_problem}", file=sys.stderr, flush=True)
        digest = None
    quality_findings = _load_json(out / f"weekly-quality-findings-{date}.json")

    lines = [
        f"*Généré le {date} par `report-blocks-draft` (déterministe, zéro LLM) — file de "
        "repli : `report-assemble` ne valide pas ce fichier et l'injecte tel quel sous le "
        "titre de la section 4 du gabarit. Ne pas y mettre de titre Markdown, le gabarit en "
        "fournit déjà un. Éditer puis relancer `report-assemble`, ou conserver tel quel : "
        "la section 4 du rapport restera marquée comme brouillon automatique.*",
        "",
    ]
    if insights and insights.get("alerts"):
        lines += ["### Alertes", ""]
        for a in insights["alerts"]:
            lines += [
                f"- **`{a['rule']}`** ({a['severity']}) : observé {a.get('observed')} vs seuil {a.get('threshold')}"
                f"{(' ' + str(a.get('unit'))) if a.get('unit') else ''}"
                f"{(' — ' + str(a.get('note'))) if a.get('note') else ''}",
                "",
            ]
    findings = (insights or {}).get("maintenance", {}).get("findings", [])
    if findings:
        lines += ["### Constats de maintenance", ""]
        for f in findings:
            lines += [
                f"- [{f.get('severity')}] {f.get('category')} — {f.get('description')} "
                f"{f.get('recommendation')}",
                "",
            ]
    for t in summary.get("top_sessions_by_cost", [])[:2]:
        lines += [
            f"- **{t.get('title_or_topic') or t['session_id']}** — ${t.get('cost_usd', 0.0):.4f}, "
            f"{t.get('total_tokens', 0):,} tokens, cache eff. {t.get('cache_efficiency')}, "
            f"{t.get('api_call_count')} appels API",
            "",
        ]
    if quality_findings and quality_findings.get("findings"):
        lines += ["### Constats de l'audit qualitatif (Partie 3)", ""]
        for f in quality_findings["findings"]:
            lines += [
                f"- [{f.get('severity', 'low').upper()}] {f.get('category', '?')} — "
                f"{f.get('description', '')} → {f.get('recommendation', '')}"
                f"{(' *(repris de ' + str(f.get('carried_from')) + ')*') if f.get('source') == 'carried' else ''}",
                "",
            ]
    flat = flatten_harness_findings(digest)
    if flat:
        lines += ["### Règles harness les plus violées", ""]
        for rule, count in _top_harness_rules(digest, cfg.harness_ignored_rules):
            lines += [f"- `{rule}` : {count} violation(s)", ""]
    lines += ["### Recommandations", ""]
    lines += [
        "- Revoir les alertes et constats ci-dessus ; corriger les violations harness en priorité (R4).",
        "",
    ]

    # v5.29/7b-hybride : le brouillon déterministe vit dans -auto- (filet de sécurité) ;
    # le fichier weekly-report-blocks-<date>.md est réservé à la prose LLM de l'agent.
    blocks_path = out / f"weekly-report-blocks-auto-{date}.md"
    blocks_path.write_text("\n".join(lines), encoding="utf-8")
    return (
        blocks_path,
        [f"brouillon de blocs généré ({len(lines)} lignes) — à éditer si besoin"],
        applicable_summary_rc(summary, out=out, date=date, fallback_rc=0),
    )


#: Bande `[N:19 findings]` (C2) : charge utile = nombre + unité libre. Le nombre
#: doit résoudre vers un scalaire des artefacts, l'unité n'est pas validée.
_NUMBER_BAND_RE = re.compile(r"\[N:([^\[\]]*)\]")

#: Titre Markdown dans le bloc qualitatif (C1) : `# Foo` … `###### Foo`, plus le
#: `#` nu (`^#{1,6}(\s|$)`). La garde `line.startswith("#")` du validateur ratisse
#: le reste (`###Foo`, `#######Foo`), donc les deux formes sont nécessaires.
_MD_HEADING_RE = re.compile(r"^#{1,6}(\s|$)")

#: Unités décomptables du rapport — ce qui suit un nombre en français.
_COUNT_UNITS = (
    r"findings?|constats?|alertes?|sessions?|skills?|règles?|violations?|fichiers?|"
    r"catégories|occurrences?|éléments?|entrées?|lignes?|points?|appels?|tests?|"
    r"jours?|semaines?|heures?|fois|tokens?|dossiers?|commits?|cas"
)

#: Forme écrite d'un décompte (C4) — le contournement du check « chiffres », où
#: l'agent écrit « huit sessions » au lieu de « [N:8] sessions ».
#:
#: Deux tiers, parce que la forme écrite n'est pas une catégorie uniforme :
#: - « huit », « dix-neuf », « quatre-vingt-dix » n'ont AUCUN autre sens que
#:   numérique → insensibles à la casse, ancrés sur une unité comptable ;
#: - « un constat », « une alerte » sont du français normal : les petits nombres
#:   (un→sept) ne sont refusés qu'en position de décompte, derrière un cue
#:   (« sur quatre sessions », « au total trois alertes »). Sans ce cue, la règle
#:   tuerait la moitié des blocs valides.
_WRITTEN_COUNT_RE = re.compile(
    r"(?<![\w-])(?i:"
    r"soixante-dix-neuf|soixante-dix-huit|soixante-dix-sept|soixante-et-onze|soixante-dix|"
    r"quatre-vingt-onze|quatre-vingt-dix|quatre-vingts|quatre-vingt|"
    r"vingt-et-un|dix-neuf|dix-huit|dix-sept|"
    r"zéro|huit|neuf|dix|onze|douze|treize|quatorze|quinze|seize|"
    r"vingt|trente|quarante|cinquante|soixante|cent|mille"
    r")\s+(?:" + _COUNT_UNITS + r")(?![\w-])"
)
_WRITTEN_COUNT_CUE_RE = re.compile(
    r"(?<![\w-])(?:plus de|plus qu[ei']|sur|exactement|au total|total de|en tout|"
    r"au nombre de|combien de)\s+(?:un|une|deux|trois|quatre|cinq|six|sept)"
    r"\s+(?:" + _COUNT_UNITS + r")(?![\w-])"
)

#: Entrées de l'index publiées dans weekly-report-gates.json. L'index complet sert
#: à la résolution ; le gates reste lisible (borné, avec son total pour vérifier
#: que la troncature n'a rien caché).
_INDEX_PUBLISH_LIMIT = 200


def _iter_scalar_numbers(node: object, path: str = "") -> Iterable[tuple[str, float]]:
    """Scalaires numériques d'un artefact JSON, avec leur chemin d-dot.

    Les booléens sont exclus (`isinstance(True, int)` est vrai en Python) et une
    chaîne n'est retenue que si elle EST un nombre — pas un nombre contenu dans
    une phrase, sinon n'importe quel décompte cité en prose deviendrait résolvable.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _iter_scalar_numbers(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list | tuple):
        for idx, value in enumerate(node):
            yield from _iter_scalar_numbers(value, f"{path}[{idx}]")
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, int | float):
        yield path, float(node)
    elif isinstance(node, str):
        try:
            yield path, float(node.strip().replace(",", "."))
        except ValueError:
            return


def number_band_index(findings: dict | None, insights: dict | None) -> dict[str, object]:
    """Scalaires vers lesquels une bande `[N:…]` doit résoudre (C2, politique D2).

    Deux familles, toutes deux publiées dans `weekly-report-gates.json` pour que
    le refus soit auditable sans relire le validateur :
    - `scalars`  : chaque valeur numérique des deux artefacts de la section 4
      (`weekly-quality-findings`, `weekly-insights`), par chemin d-dot ;
    - `counts`   : les cardinances dérivées (nombre de findings, d'alertes, de
      constats de maintenance, par sévérité) — un décompte d'agents est un
      agrégat, pas un scalaire stocké.

    Index COMPLET : la résolution d'une bande ne doit pas dépendre du formatage
    du gates. La troncature d'affichage est faite par `published_number_bands`.
    """
    findings_list = (findings or {}).get("findings", []) if findings else []
    alerts = (insights or {}).get("alerts", []) if insights else []
    maint = (insights or {}).get("maintenance", {}).get("findings", []) if insights else []

    scalars = dict(_iter_scalar_numbers(findings or {}))
    scalars.update(_iter_scalar_numbers(insights or {}))

    counts: dict[str, int] = {
        "findings_total": len(findings_list),
        "alerts_total": len(alerts),
        "maintenance_total": len(maint),
    }
    for severity in ("high", "medium", "low"):
        counts[f"findings_{severity}"] = sum(
            1 for f in findings_list if f.get("severity") == severity
        )
    return {"scalars": scalars, "scalars_total": len(scalars), "counts": counts}


def published_number_bands(index: dict[str, object]) -> dict[str, object]:
    """Vue lisible de l'index pour le gates : `scalars` borné, total conservé.

    Un gates de plusieurs milliers d'entrées serait illisible ; `scalars_total`
    permet au lecteur de voir que la troncature existe au lieu de le deviner.
    """
    scalars = index.get("scalars", {})
    items = list(scalars.items()) if isinstance(scalars, Mapping) else []
    return {**index, "scalars": dict(items[:_INDEX_PUBLISH_LIMIT])}


def _band_resolves(raw: str, index: dict[str, object]) -> bool:
    """La charge utile `[N:…]` est-elle un nombre, et présent dans l'index ?"""
    head = raw.split()[0] if raw.split() else ""
    try:
        value = float(head.replace(",", "."))
    except ValueError:
        return False
    scalars = index.get("scalars", {})
    counts = index.get("counts", {})
    candidates = [*(scalars.values() if isinstance(scalars, Mapping) else []), *counts.values()]
    return any(abs(float(candidate) - value) < 1e-9 for candidate in candidates)


def validate_llm_blocks(text: str, findings: dict | None, insights: dict | None):
    """Garde-fous anti-hallucination du bloc 7b LLM (v5.29, hybride).

    Retourne (violations, coverage_warnings) :
    - violations → le bloc est REJETÉ, report-assemble bascule sur le brouillon
      déterministe (fallback) ;
    - coverage_warnings → le bloc est accepté mais l'annexe signale les constats
      high non cités (omission ≠ hallucination).
    Checks : zéro chiffre (spec : le bloc ne cite que catégories/sévérités) ;
    balises de source [F:ses_xxx#cat] / [M:cat] / [A:rule] résolues dans les
    entrées ; bandes [N:…] résolues vers un scalaire des artefacts ; aucune
    ligne de titre Markdown ; aucune forme écrite d'un décompte ; taille ≤ 60
    lignes ; 1re ligne non vide ≠ titre `##`/`#` (le gabarit fournit déjà le
    titre de la section 4) ; tout finding high doit être cité.

    Portée : ce validateur ne voit QUE la prose de l'agent. Le brouillon
    automatique `-auto-` (qui porte volontairement des `###`) n'est jamais
    validé — il est injecté tel quel, cf. `_assemble_quality_block`.
    """
    violations: list[str] = []
    coverage: list[str] = []

    # les balises [F:...]/[M:...]/[A:...] portent des ids/session_ids (chiffres) —
    # le check chiffres ne s'applique qu'au texte visible (hors balises). Les
    # bandes [N:…] portent un nombre par construction : elles sont neutralisées
    # ici et contrôlées plus bas (C2), sinon toute bande serait un « chiffre libre ».
    text_no_tags = re.sub(r"\[[FMA]:[^\]]+\]", "", text)
    text_no_tags = _NUMBER_BAND_RE.sub("", text_no_tags)
    # chiffres autorisés hors balise : dates ISO, pourcentages, numéros de version.
    # la spec interdit toujours les chiffres « libres » (coûts, décomptes, durées).
    _ALLOWED_NUM = re.compile(
        r"\b\d{4}-\d{2}-\d{2}\b"  # date ISO
        r"|\b\d{1,3}([.,]\d+)?%"  # pourcentage
        r"|\bv?\d+\.\d+(\.\d+)?\b"  # version sémantique
    )
    text_no_allowed = _ALLOWED_NUM.sub(" ", text_no_tags)
    illegal_digits = re.findall(r"\d+", text_no_allowed)
    if illegal_digits:
        snippet = ", ".join(illegal_digits[:5])
        first_digit = re.search(r"\d+", text_no_allowed)
        line = text_no_allowed.count("\n", 0, first_digit.start()) + 1 if first_digit else 1
        violations.append(
            "chiffres interdits dans le bloc LLM (spec : catégories/sévérités "
            f"uniquement) — ligne {line}, chiffres hors date/pourcentage/version : {snippet}"
        )

    body_lines = text.splitlines()
    n_lines = len(body_lines)
    if n_lines > 60:
        violations.append(f"bloc trop long (ligne {n_lines}, {n_lines} lignes > 60)")

    # Le gabarit fournit déjà le titre de la section 4 : un titre en tête de bloc
    # le dupliquerait dans le rapport assemblé (2 H2). On vise la 1re ligne NON
    # vide — une ligne vide en tête ne doit pas servir d'échappatoire.
    head = next((i for i, ln in enumerate(body_lines) if ln.strip()), None)
    head_reported = head is not None and re.match(r"^#{1,2}\s", body_lines[head]) is not None
    if head_reported:
        violations.append(
            f"titre Markdown interdit en tête de bloc (ligne {head + 1}) — le gabarit "
            f"fournit déjà le titre de la section 4 : {body_lines[head].strip()[:60]!r}"
        )

    # C1 : AUCUNE ligne de titre, n'importe où et n'importe quel niveau. Les
    # `###` de l'agent passaient la validation et n'existaient que dans le rendu
    # markdown — le HTML, lui, échappe le texte puis le paraphe, donc il affichait
    # littéralement « <p>###…</p> » (run 2026-10-03, 4 sous-titres). Deux rendus,
    # deux sémantiques : on ferme la divergence à la racine, à l'écriture, plutôt
    # qu'en essayant d'apprendre le markdown au renderer HTML.
    for idx, line in enumerate(body_lines):
        # La ligne de tête n'est ignorée que si le message « en tête de bloc » la
        # a déjà signalée : sinon `### Foo` en 1re ligne passerait par les deux
        # checks (le check de tête ne couvre que `#{1,2}`).
        if idx == head and head_reported:
            continue
        if _MD_HEADING_RE.match(line) or line.startswith("#"):
            violations.append(
                f"titre Markdown interdit dans le bloc (ligne {idx + 1}) — le bloc "
                f"qualitatif est de la prose, pas du markdown structuré : "
                f"{line.strip()[:60]!r}"
            )
            break  # une seule occurrence suffit : le bloc est rejeté dans tous les cas

    # C4 : la forme écrite est le contournement du check « chiffres » — « huit
    # sessions » passe là où « 8 sessions » est refusé. Ancrée sur une unité
    # comptable (+ cue pour les petits nombres) pour ne pas tuer « un constat ».
    for pattern in (_WRITTEN_COUNT_RE, _WRITTEN_COUNT_CUE_RE):
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            violations.append(
                f"nombre écrit interdit (ligne {line}) — un décompte s'écrit en chiffre "
                f"arabe dans une bande [N:…] résolue, pas en toutes lettres : "
                f"{match.group(0)[:60]!r}"
            )
            break

    findings_list = (findings or {}).get("findings", []) if findings else []
    alerts = (insights or {}).get("alerts", []) if insights else []
    maint = (insights or {}).get("maintenance", {}).get("findings", []) if insights else []

    f_refs = {f"{f.get('session_id')}#{f.get('category')}" for f in findings_list}
    m_refs = {f.get("category") for f in maint}
    a_refs = {a.get("rule") for a in alerts}
    seen_f: set[str] = set()

    # Empty source references are not merely unknown: report them as malformed so
    # the author can repair the exact traceability marker instead of guessing.
    for match in re.finditer(r"\[([FMA]):([^\]]*)\]", text):
        kind, ref = match.group(1), match.group(2)
        line = text.count("\n", 0, match.start()) + 1
        if not ref.strip():
            violations.append(f"balise de source mal formée [{kind}:] — ligne {line}")
            continue
        if kind == "F":
            if ref not in f_refs:
                violations.append(
                    f"balise inconnue [F:{ref}] — ligne {line}, aucun finding correspondant"
                )
            else:
                seen_f.add(ref)
        elif kind == "M":
            if ref not in m_refs:
                violations.append(
                    f"balise inconnue [M:{ref}] — ligne {line}, aucun constat de maintenance"
                )
        elif kind == "A" and ref not in a_refs:
            violations.append(f"balise inconnue [A:{ref}] — ligne {line}, aucune alerte")

    # C2 : une bande [N:…] est une AFFIRMATION chiffrée, donc elle se résout comme
    # une balise de source : vers un scalaire réellement présent dans les artefacts
    # de la section 4. Pas d'assouplissement « petits entiers autorisés » (D2) — un
    # décompte juste par hasard reste un décompte inventé.
    band_index = number_band_index(findings, insights)
    for match in _NUMBER_BAND_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        raw = match.group(1).strip()
        if not raw:
            violations.append(f"bande de chiffre mal formée [N:] — ligne {line}")
            continue
        head_token = raw.split()[0]
        if not re.fullmatch(r"\d+(?:[.,]\d+)?", head_token):
            violations.append(
                f"bande de chiffre non numérique [N:{raw[:40]}] — ligne {line}, "
                "un décompte s'écrit en chiffre arabe (la forme écrite est refusée)"
            )
        elif not _band_resolves(raw, band_index):
            violations.append(
                f"bande de chiffre non résolue [N:{raw[:40]}] — ligne {line}, aucun "
                "scalaire correspondant dans weekly-quality-findings / weekly-insights"
            )

    for f in findings_list:
        if f.get("severity") == "high":
            ref = f"{f.get('session_id')}#{f.get('category')}"
            if ref not in seen_f:
                coverage.append(f"constat high non couvert par le bloc : [F:{ref}]")

    return violations, coverage


def report_assemble(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, list[str], int]:
    """Inject the LLM blocks file into the draft → final report.

    Bascule tardive : si aucun rapport n'est écrit (gate bloquante, rc>=2),
    l'alias ``runs/current`` est restauré sur le run précédent — il désigne
    toujours le dernier run avec un livrable, jamais un run vide.
    """
    path, warnings, rc = _report_assemble_inner(cfg, anchor=anchor)
    if path is None and rc >= 2 and rollback_current_link(cfg.output_dir):
        warnings = [
            *warnings,
            "alias runs/current restauré sur le run précédent (aucun rapport écrit)",
        ]
    return path, warnings, rc


def _record_audit_envelope_rejects(cfg: TelemetryConfig, artifact_gate: dict) -> None:
    """Comptabilise les rejets d'enveloppe audit hors contrat (extrait de _report_assemble_inner, CCN-3)."""
    for key, entry in artifact_gate["required"].items():
        if (
            key.startswith("audit-findings-")
            and isinstance(entry, Mapping)
            and entry.get("reason") in _AUDIT_ENVELOPE_REASONS
        ):
            #: Rejet hors contrat (Task 1) — compté une fois par assemble,
            #: chemins partiel comme bloquant (observabilité Task 6).
            record_resilience_event(cfg.output_dir, "audit_envelope_reject")


def _split_missing_artifacts(missing: list[tuple[str, dict]]) -> tuple[list[str], list[str]]:
    """Sépare les artefacts manquants : autres chemins vs audits dynamiques (extrait de _report_assemble_inner, CCN-2)."""
    missing_other = [
        entry["path"] for key, entry in missing if not key.startswith("audit-findings-")
    ]
    missing_audit = [
        f"{key} ({entry.get('reason', 'absent')})"
        for key, entry in missing
        if key.startswith("audit-findings-")
    ]
    return missing_other, missing_audit


def _assemble_artifact_gate(
    out: Path, date: str, timings: object, cfg: TelemetryConfig, warnings: list[str]
) -> tuple[int, tuple[Path | None, list[str], int] | None]:
    """Gate artefacts requis + JOIN partiel → (rc, fatal). fatal = return immédiat."""
    artifact_gate = validate_required_artifacts(
        out,
        date,
        applicability=_branch_applicability(timings, date, out=out),
        dynamic_audit_artifacts=_audit_artifact_declarations(timings),
    )
    if artifact_gate["status"] == "pass":
        return 0, None
    _record_audit_envelope_rejects(cfg, artifact_gate)
    missing = [
        (key, entry)
        for key, entry in artifact_gate["required"].items()
        if entry["status"] != "present"
    ]
    missing_other, missing_audit = _split_missing_artifacts(missing)
    if missing_other:
        return (
            0,
            (
                None,
                [
                    "artefact requis manquant ou illisible — "
                    + ", ".join(missing_other + missing_audit)
                    + " (relancer weekly_run)"
                ],
                2,
            ),
        )
    # JOIN partiel : seuls des audits dynamiques manquent — rapport écrit
    # avec mention explicite (rc>=1), jamais de STOP sans rapport.
    record_resilience_event(cfg.output_dir, "audit_partial_fallback")
    warnings.append(
        "⚠ JOIN partiel : audit(s) manquant(s) — "
        + ", ".join(missing_audit)
        + " — rapport généré sans ces sessions (relancer le worker ciblé)"
    )
    return 1, None


def _assemble_summary_rc(
    out: Path, date: str, timings: object, timings_state: str, cfg: TelemetryConfig
) -> int:
    """rc du résumé (+ pin JOIN-partiel géré par l'appelant)."""
    summary_for_rc = _load_json(out / f"weekly-summary-{date}.json")
    return applicable_summary_rc(
        summary_for_rc,
        out=out,
        date=date,
        additional_records=_join_status_records(timings) if timings_state == "present" else (),
        project_root=cfg.project_root,
        fallback_rc=0,
    )


def _assemble_curation_gate(
    out: Path, date: str, cfg: TelemetryConfig, warnings: list[str]
) -> tuple[int, tuple[Path | None, list[str], int] | None]:
    """Gate curation WAVE 2.5 + signal REQUIRED → (rc, fatal)."""
    coherence = _load_json(out / f"weekly-coherence-findings-{date}.json")
    curation_path = out / f"skill-curate-{date}.json"
    curation_manifest, curation_state = _json_file_state(curation_path)
    if curation_state == "ill_readable":
        warning = f"manifeste de curation malformé ou illisible : {curation_path.name}"
        warnings.append(warning)
        return 0, (None, warnings, 2)
    rc = 0
    if curation_state == "present":
        curation_rc, curation_warning = _curation_manifest_gate(
            curation_manifest,
            date=date,
            project_root=cfg.project_root,
        )
        if curation_warning:
            warnings.append(curation_warning)
        if curation_rc >= 2:
            return 0, (None, warnings, 2)
        rc = max(rc, curation_rc)
    if _coherence_has_curation_signal(coherence) and curation_state == "absent":
        warnings.append(
            f"⚠ WAVE 2.5 (curation) REQUIRED : findings de cohérence porte(nt) des "
            f"actions de curation mais skill-curate-{date}.json est absent — "
            f"exécuter `weekly_skill_curate --apply` puis regénérer le rapport (P0)."
        )
        rc = max(rc, 1)
    return rc, None


def _security_counts_by_rule(findings: Iterable[Mapping]) -> dict[str, int]:
    """Rule → count, tri déterministe (count décroissant puis nom croissant).

    `rule` est normalisé comme `_is_blocking_security_rule` (minuscules, sans
    préfixe `security/`) afin que les deux vues — répartition générale et
    répartition bloquante — soient directement comparables.
    """
    counter: Counter[str] = Counter()
    for finding in findings:
        rule = str(finding.get("rule") or finding.get("id") or "").strip().lower()
        rule = rule.removeprefix("security/")
        if rule:
            counter[rule] += 1
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def _digest_findings_counters(harness_digest: object) -> tuple[int | None, int | None]:
    """(findings_raw, findings_unique) dédup par harness-eval, ou (None, None).

    Le digest publie ces deux compteurs dans `harness_counts`. Ils sont la seule
    référence pour expliquer l'écart avec `critical_count` : ce dernier compte un
    parcours RÉCURSIF de tout le digest (`inspection.*.findings`), tandis que
    `findings_unique` est le même population après déduplication harness-eval.
    """
    if not isinstance(harness_digest, Mapping):
        return None, None
    counts = harness_digest.get("harness_counts")
    if not isinstance(counts, Mapping):
        return None, None
    out: list[int | None] = []
    for key in ("findings_raw", "findings_unique"):
        try:
            out.append(max(0, int(counts.get(key) or 0)))
        except (TypeError, ValueError):
            out.append(None)
    return out[0], out[1]


def _assemble_security_gate(
    harness_digest: object, cfg: TelemetryConfig | None = None
) -> tuple[dict[str, object], str | None]:
    """Gate sécurité warn-only → (security_gate, warning).

    FIX 8 : `critical_count` est un comptage RECURSIF de tout le digest (les
    findings rattachés aux composants `inspection.*.findings` sont visités même
    quand le tableau `findings` de premier niveau est vide), tandis que
    `blocking_count` applique l'allowlist des 3 règles bloquantes. Les deux sont
    donc volontairement NON comparables au `harness_counts.findings_raw` /
    `findings_unique` du digest (32/29 sur le run réel) : `by_rule` et
    `blocking_by_rule` sont publiés pour que l'écart soit vérifiable par le
    lecteur au lieu d'être seulement Constat.

    FIX 8b : les lignes `result` (matrice d'évaluation, une par composant
    scanné) ne sont plus des findings — le compteur suit les occurrences réelles
    (44 sur le digest 2026-10-04 : 28/8/8, pas 1319 : 453/433/433).
    `cfg.harness_ignored_rules` est appliqué ici comme il l'est déjà en
    insights.py et en §5 : une règle explicitement ignorée ne peut plus
    déclencher le gate.
    """
    ignored = list(cfg.harness_ignored_rules) if cfg is not None else None
    critical_security = _critical_security_findings(harness_digest, ignored_rules=ignored)
    blocking = _blocking_security_findings(harness_digest, ignored_rules=ignored)
    findings_raw, findings_unique = _digest_findings_counters(harness_digest)
    security_gate: dict[str, object] = {
        "status": "pass",
        "critical_count": len(critical_security),
        "blocking_count": len(blocking),
        "blocking_rules": sorted(_BLOCKING_SECURITY_RULES),
        "by_rule": _security_counts_by_rule(critical_security),
        "blocking_by_rule": _security_counts_by_rule(blocking),
        "digest_findings_raw": findings_raw,
        "digest_findings_unique": findings_unique,
        "ignored_rules": sorted(ignored or []),
    }
    if not critical_security:
        return security_gate, None
    # Warn-only sécu : les règles blocking restent visibles (rc=1) mais
    # n'empêchent plus l'écriture du rapport. Seuls les cas non-sécu
    # (draft manquant, artefact requis, curation malformée) restent exit 2.
    security_gate["status"] = "warn"
    return (
        security_gate,
        "⚠ findings security/critical présents — rapport marqué en échec déterministe"
        + (" (blocking security rule)" if blocking else "")
        + (" — warn-only, rapport écrit" if blocking else ""),
    )


def _assemble_quality_block(
    out: Path,
    date: str,
    cfg: TelemetryConfig,
    llm_text: str | None,
    auto_text: str | None,
    warnings: list[str],
) -> tuple[str, str]:
    """Résolution prose LLM vs brouillon auto → (replacement, status)."""
    replacement: str | None = None
    status = "non disponible (placeholder)"
    if llm_text is not None:
        word_count = len(llm_text.split())
        if word_count < cfg.blocks_min_words:
            violation = (
                f"bloc LLM trop court ({word_count} mots < {cfg.blocks_min_words}) — "
                f"revoir weekly-report-blocks-{date}.md"
            )
            status = f"brouillon automatique (bloc LLM rejeté : {violation}) — auto_draft_fallback; never validated"
            warnings.append(f"bloc LLM rejeté — fallback brouillon automatique : {violation}")
            replacement = (
                auto_text
                if auto_text is not None
                else (
                    "*Section 4 non disponible (bloc LLM rejeté et brouillon automatique absent — "
                    "lancer report-blocks-draft).*\n"
                )
            )
        else:
            findings = _load_json(out / f"weekly-quality-findings-{date}.json")
            insights = _load_json(out / f"weekly-insights-{date}.json")
            violations, coverage = validate_llm_blocks(llm_text, findings, insights)
            warnings.extend(f"bloc LLM : {v}" for v in coverage)
            if violations:
                status = f"brouillon automatique (bloc LLM rejeté : {' ; '.join(violations)}) — auto_draft_fallback; never validated"
                warnings.append(
                    f"bloc LLM rejeté — fallback brouillon automatique ({len(violations)} violation(s))"
                )
                replacement = (
                    auto_text
                    if auto_text is not None
                    else (
                        "*Section 4 non disponible (bloc LLM rejeté et brouillon automatique absent — "
                        "lancer report-blocks-draft).*\n"
                    )
                )
            else:
                status = "prose agent (7b LLM)"
                replacement = llm_text
    elif auto_text is not None:
        status = "brouillon automatique (report-blocks-draft) — auto_draft_fallback; prose absente; never validated"
        replacement = auto_text
    else:
        replacement = (
            "*Section 4 non disponible (bloc de constats absent — ni l'agent (7b) ni "
            "report-blocks-draft n'ont produit de bloc).*\n\n"
            "Lancer report-blocks-draft, ou coller les constats dans "
            "`weekly-report-blocks-<date>.md` puis relancer report-assemble."
        )
        warnings.append("bloc de constats absent — section 4 remplacée par un placeholder")
    return replacement, status


def _assemble_html_gate(
    cfg: TelemetryConfig,
    anchor: str | None,
    out: Path,
    date: str,
    replacement: str,
    warnings: list[str],
) -> tuple[dict | None, int]:
    """Rendu HTML best-effort + fusion gate → (ctx, rc)."""
    ctx = build_report_context(cfg, anchor=anchor)
    rc = 0
    if ctx is None:
        return None, 0
    html_enabled = bool(cfg.html_report_dir)
    render_error: Exception | None = None
    try:
        html_path = render_html_report(cfg, anchor=anchor, ctx=ctx, quality_block=replacement)
    except Exception as exc:  # renderer is best-effort; gate remains deterministic
        html_path = None
        render_error = exc
    artifact_gate = validate_required_artifacts(
        out, date, html_enabled=html_enabled, html_path=html_path
    )
    ctx["gate_status"]["required"] = artifact_gate["required"]
    ctx["gate_status"]["optional"] = artifact_gate["optional"]
    ctx["gate_status"]["artifacts"] = {
        "status": artifact_gate["status"],
        "missing": [
            artifact["path"]
            for artifact in artifact_gate["artifacts"].values()
            if artifact["status"] == "absent"
            and (artifact.get("required") or artifact.get("applicable", True))
        ],
        "ill_readable": [
            artifact["path"]
            for artifact in artifact_gate["artifacts"].values()
            if artifact["status"] == "ill_readable"
            and (artifact.get("required") or artifact.get("applicable", True))
        ],
        "optional_missing": artifact_gate["optional_missing"],
        "optional_ill_readable": artifact_gate["optional_ill_readable"],
    }
    ctx["gate_status"]["html"] = artifact_gate["html"]
    if render_error is not None:
        report_only_permission = _is_external_permission_failure(cfg, render_error)
        ctx["gate_status"]["html"] = {
            "status": "report-only" if report_only_permission else "failure",
            "path": None,
            "error": type(render_error).__name__,
        }
        if report_only_permission:
            ctx["gate_status"]["html"].update(
                {
                    "report_only": True,
                    "category": "external-permission-refusal",
                    "target": str(cfg.html_report_dir),
                    "project_root": str(cfg.project_root),
                }
            )
        warnings.append(
            "HTML renderer permission refused outside worktree; report-only"
            if report_only_permission
            else "HTML renderer failed; report artifact unavailable"
        )
        if not report_only_permission:
            rc = max(rc, 1)
    elif html_enabled and artifact_gate["html"]["status"] != "present":
        warnings.append(f"HTML enabled but report artifact {artifact_gate['html']['status']}")
        rc = max(rc, 1)
    if html_path:
        try:
            open_html_report(cfg, html_path)
        except Exception as exc:  # best effort; external permission is report-only
            if _is_external_permission_failure(cfg, exc):
                warnings.append("HTML auto-open permission refused outside worktree; report-only")
            else:
                warnings.append(f"HTML auto-open failed: {type(exc).__name__}")
                rc = max(rc, 1)
    return ctx, rc


def _persist_gate_state(
    out: Path, date: str, ctx: dict, status: str, rc: int, security_gate: dict[str, object]
) -> None:
    """État machine-readable des gates à côté du rapport final."""
    ctx["gate_status"]["prose"] = {
        "status": "validated" if status == "prose agent (7b LLM)" else "auto_draft_fallback",
        "validated": status == "prose agent (7b LLM)",
    }
    ctx["gate_status"]["summary_rc"] = rc
    ctx["gate_status"]["security"] = security_gate
    ctx["gate_status"]["blocking_rules"] = sorted(_BLOCKING_SECURITY_RULES)
    # Index des scalaires résolvables par une bande [N:…] (C2). Relu depuis les
    # MÊMES artefacts que le validateur, donc il décrit exactement ce contre quoi
    # la prose a été jugée — un refus de bande s'audite sans rouvrir le validateur.
    ctx["gate_status"]["number_bands"] = published_number_bands(
        number_band_index(
            _load_json(out / f"weekly-quality-findings-{date}.json"),
            _load_json(out / f"weekly-insights-{date}.json"),
        )
    )
    (out / f"weekly-report-gates-{date}.json").write_text(
        json.dumps(ctx["gate_status"], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _report_assemble_inner(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, list[str], int]:
    """Inject the LLM blocks file into the draft → final report."""
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)
    warnings: list[str] = []
    rc = 0

    draft = out / f"weekly-report-draft-{date}.md"
    text = _load_text(draft)
    if text is None:
        return (
            None,
            [
                f"draft inexistant {draft} — un assemble précédent l'a consommé/supprimé : "
                "relancer report-prep d'abord"
            ],
            2,
        )

    # Required-input validation happens after the draft existence check so the
    # historical "assemble consumed the draft" diagnostic remains actionable.
    timings, timings_state = _json_file_state(out / f"weekly-timings-{date}.json")
    gate_rc, gate_fatal = _assemble_artifact_gate(out, date, timings, cfg, warnings)
    if gate_fatal is not None:
        return gate_fatal
    rc = max(rc, gate_rc)

    rc = _assemble_summary_rc(out, date, timings, timings_state, cfg)
    # JOIN partiel (voir gate ci-dessus) : le rapport reste marqué partiel
    # même si les warnings restants sont non-bloquants (rc remonterait à 0).
    if any("JOIN partiel" in w for w in warnings):
        rc = max(rc, 1)

    curation_rc, curation_fatal = _assemble_curation_gate(out, date, cfg, warnings)
    if curation_fatal is not None:
        return curation_fatal
    rc = max(rc, curation_rc)

    harness_digest = _load_json(out / f"weekly-harness-digest-{date}.json")
    security_gate, security_warning = _assemble_security_gate(harness_digest, cfg)
    if security_warning is not None:
        warnings.append(security_warning)
        rc = max(rc, 1)

    marker = "<!-- QUALITY_BLOCK -->"
    if marker not in text:
        return None, ["marqueur QUALITY_BLOCK absent du draft — gabarit incohérent"], 2

    # v5.29 hybride : brouillon déterministe (-auto-) toujours disponible ;
    # le fichier weekly-report-blocks-<date>.md est la prose LLM (7b), validée.
    auto_path = out / f"weekly-report-blocks-auto-{date}.md"
    llm_path = out / f"weekly-report-blocks-{date}.md"
    auto_text = _load_text(auto_path)
    llm_text = _load_text(llm_path)

    replacement, status = _assemble_quality_block(out, date, cfg, llm_text, auto_text, warnings)

    # C3 : le markdown résout aussi les bandes [N:…] que le HTML résout — sans ça
    # les deux rendus divergeraient sur la seule ligne qui porte un chiffre.
    # La substitution est idempotente et n'introduit que des chiffres.
    final_text = (
        text.replace(marker, resolve_number_bands(replacement))
        + f"\n---\n*Statut section 4 : {status}*\n"
    )
    # Task2 : section Sécurité repliée (counts-only Task1, jamais de findings bruts).
    final_text += "\n\n" + _render_security_section(security_gate)
    final_path = out / f"weekly-report-{date}.md"
    final_path.write_text(final_text, encoding="utf-8")
    if replacement is not None:
        final_path.with_name(f"weekly-report-draft-{date}.md").unlink(missing_ok=True)

    # v6.1 : rapport HTML autonome, best-effort (échec → warning + None, jamais
    # fatal). Le ctx est reconstruit depuis les artefacts — prep et assemble
    # tournent comme sous-commandes CLI séparées — et le bloc qualité injecté
    # ci-dessus (prose LLM validée ou fallback auto) alimente la section 4.
    ctx, html_rc = _assemble_html_gate(cfg, anchor, out, date, replacement, warnings)
    rc = max(rc, html_rc)

    # Persist machine-readable gate state alongside final report metadata.
    if ctx is not None:
        _persist_gate_state(out, date, ctx, status, rc, security_gate)

    return final_path, warnings, rc
