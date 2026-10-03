"""`report-blocks-check` (7b.1) — validation NON destructive du fichier de prose.

Verrou du contrat qui a coûté le run 2026-10-01 : `report-assemble` SUPPRIME le
draft à chaque réussite, donc corriger un bloc rejeté imposait un `report-prep`
complet (2 assemble ratés + 1 prep supplémentaire sur ce run). Le check valide le
même fichier avec le MÊME validateur que l'assemble et rend les violations
numérotées — sans rien consommer.

Ces tests vérifient donc deux choses à chaque cas :
- le verdict et le numéro de ligne ;
- le fichier de prose ET le draft sont intacts après l'appel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import tzutc

from weekly_telemetry_aggregator.cli import _cmd_report_blocks_check
from weekly_telemetry_aggregator.config import TelemetryConfig

RUN = tzutc(2026, 8, 12)
DATE = "2026-08-12"
SESSION = "ses_01J7XQ4FULLID0000000000"


def _cfg(tmp_path: Path) -> TelemetryConfig:
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    cfg.opencode_db_path = "/nonexistent/opencode.db"
    cfg.project_root = tmp_path
    cfg.open_browser = False
    return cfg


def _write_blocks(tmp_path: Path, body: str) -> Path:
    path = tmp_path / f"weekly-report-blocks-{DATE}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _write_findings(tmp_path: Path) -> None:
    """Un finding `high` dont la balise doit être `[F:<SESSION>#qualite]` — ID complet."""
    payload = {
        "schema_version": 1,
        "session_id": SESSION,
        "summary": "audit qualitatif",
        "findings": [
            {
                "session_id": SESSION,
                "category": "qualite",
                "severity": "high",
                "description": "réécriture non vérifiée",
                "recommendation": "relire avant commit",
            }
        ],
        "rc": 0,
        "warnings": [],
    }
    (tmp_path / f"weekly-quality-findings-{DATE}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _prose(*extra: str) -> str:
    """Bloc de prose conforme : 43 lignes, ≥ 40 mots, balises résolues, sans chiffre."""
    lines = [
        f"- Constat qualité relevé sur la session de revue [F:{SESSION}#qualite]",
        "  Le contrôle de sortie a été contourné plusieurs fois de suite, ce qui",
        "  a laissé des fichiers modifiés sans relecture : la trace de validation",
        "  est absente du journal et l'écart n'a été rattrapé qu'après coup.",
        "- Le même motif est revenu sur la préparation de revue, où l'étape de",
        "  contrôle a été déclarée terminée sans exécution réelle du script.",
    ]
    lines.extend(extra)
    while len(lines) < 43:
        lines.append("  Le constat se répète sur la même séquence, sans changement d'effet.")
    return "\n".join(lines[:43])


def _run(tmp_path: Path, capsys) -> tuple[int, str, str]:
    rc = _cmd_report_blocks_check(type("Args", (), {"anchor": RUN.isoformat()})(), _cfg(tmp_path))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_blocks_check_rejects_71_lines_and_reports_the_count(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    _write_findings(tmp_path)
    path = _write_blocks(tmp_path, _prose() + "\n" + "\n".join(["  ligne de remplissage."] * 28))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 71
    before = path.read_bytes()

    rc, out, err = _run(tmp_path, capsys)

    assert rc == 1, err
    assert "NON CONFORME" in out
    # La violation de taille est listée ET numérotée, avec le nombre de lignes.
    numbered = [line for line in out.splitlines() if line.startswith("  1.")]
    assert numbered, out
    assert "71 lignes > 60" in numbered[0], out
    # Le draft n'est pas consommé, pas plus le fichier de prose.
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("weekly-report-draft-*.md"))


def test_blocks_check_rejects_a_truncated_session_id_with_its_line_number(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    _write_findings(tmp_path)
    # Même 43 lignes que le cas conforme, seule la balise est tronquée.
    path = _write_blocks(
        tmp_path,
        _prose().replace(f"[F:{SESSION}#qualite]", "[F:ses_01J7…#qualite]", 1),
    )
    before = path.read_bytes()

    rc, out, err = _run(tmp_path, capsys)

    assert rc == 1, err
    assert "NON CONFORME" in out
    truncated = [line for line in out.splitlines() if "balise inconnue" in line]
    assert truncated, out
    assert "ligne 1" in truncated[0], out
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("weekly-report-draft-*.md"))


def test_blocks_check_accepts_a_conforming_block_and_consumes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    _write_findings(tmp_path)
    path = _write_blocks(tmp_path, _prose())
    draft = tmp_path / f"weekly-report-draft-{DATE}.md"
    draft.write_text("<!-- QUALITY_BLOCK -->\n", encoding="utf-8")
    blocks_before = path.read_bytes()
    draft_before = draft.read_bytes()

    rc, out, err = _run(tmp_path, capsys)

    assert rc == 0, err
    assert "CONFORME" in out
    assert "43 lignes" in out
    assert "NON consommé" in out
    assert "NON CONFORME" not in out
    # Le point du check : ni le bloc ni le draft ne sont touchés.
    assert path.read_bytes() == blocks_before
    assert draft.read_bytes() == draft_before


def test_blocks_check_is_stable_across_two_calls(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Deux checks consécutifs rendent le même verdict : le check n'a pas d'effet."""
    _write_findings(tmp_path)
    path = _write_blocks(tmp_path, _prose() + "\n" + "\n".join(["  ligne de remplissage."] * 28))
    before = path.read_bytes()

    first_rc, first_out, _ = _run(tmp_path, capsys)
    second_rc, second_out, _ = _run(tmp_path, capsys)

    assert (first_rc, first_out) == (second_rc, second_out)
    assert first_rc == 1
    assert path.read_bytes() == before


def test_blocks_check_rc2_when_the_prose_file_is_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    rc, out, err = _run(tmp_path, capsys)

    assert rc == 2
    assert "prose absente ou illisible" in err
    assert out == ""


def test_blocks_check_flags_the_min_word_floor_like_the_assembler(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """Le plancher de mots est un gate de l'assemble : le check doit le voir aussi.

    Sans ce check, un bloc de 15 mots passerait le check vert puis serait rejeté
    par `_assemble_quality_block` — exactement le faux vert à supprimer.
    """
    cfg = TelemetryConfig()
    assert cfg.blocks_min_words == 40
    path = tmp_path / f"weekly-report-blocks-{DATE}.md"
    short = "- Un constat bref [F:ses_01J7XQ4FULLID0000000000#qualite] sans développement."
    path.write_text(short, encoding="utf-8")
    (tmp_path / f"weekly-quality-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": SESSION,
                "summary": "audit qualitatif",
                "findings": [
                    {
                        "session_id": SESSION,
                        "category": "qualite",
                        "severity": "high",
                        "description": "d",
                        "recommendation": "r",
                    }
                ],
                "rc": 0,
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()

    rc, out, err = _run(tmp_path, capsys)

    assert rc == 1, err
    assert "mots < 40" in out, out
    assert path.read_bytes() == before
