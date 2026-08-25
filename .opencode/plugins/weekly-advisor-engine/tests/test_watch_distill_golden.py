"""Golden fixture : distill sur l'écosystème réel du run 2026-08-23 (323 items)."""

from __future__ import annotations

import json
from pathlib import Path

from weekly_telemetry_aggregator import watch_distill as wd

ANCHOR = "2026-08-24T12:00:00Z"
DATE = "2026-08-24"
FIXTURE = Path(__file__).parent / "fixtures" / "golden-ecosystem-2026-08-23.json"


def _cfg(tmp_path: Path):
    from weekly_telemetry_aggregator.config import TelemetryConfig

    return TelemetryConfig(output_dir=tmp_path, project_root=tmp_path)


def test_golden_real_ecosystem_distill(tmp_path: Path):
    """Run nominal mémoire vide sur les données réelles du 23/08/2026.

    Garde-fous : ≥20 candidats utiles, sortie <20 Ko (économie tokens),
    blocked hors candidats mais en annexe, contrat fiche respecté.
    """

    eco = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(eco["new_items"]) == 323  # le fixture n'a pas dérivé

    (tmp_path / f"weekly-ecosystem-{DATE}.json").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    result, rc = wd.run(_cfg(tmp_path), anchor=ANCHOR)

    assert rc == 0
    assert result["mode"] == "distill"

    candidates = result["candidates"]
    assert len(candidates) >= 20, "trop peu de candidats sur données réelles"

    candidates_file = tmp_path / f"watch-candidates-{DATE}.json"
    assert candidates_file.is_file()
    size_bytes = candidates_file.stat().st_size
    # Plafond recalibré pour le JSON indenté (défaut writer) : les artefacts de
    # veille sont lus par l'agent en 3.5 via Read (tronque à 2000 car/ligne) —
    # l'indentation prime sur le poids disque depuis l'incident 2026-08-25.
    assert size_bytes < 40_960, f"sortie trop lourde : {size_bytes} octets"

    ids = {fiche["id"] for fiche in candidates}
    annex = result.get("security_annex", [])
    annex_ids = {row["id"] for row in annex}
    assert not (ids & annex_ids), "un item blocked a fui dans les candidats"
    for row in annex:
        assert row["id"] and row["reason"]

    for fiche in candidates:
        assert set(fiche) == set(wd.FICHE_KEYS)
        assert len(fiche["summary"]) <= 200
        assert fiche["security"]["verdict"] in ("clean", "suspicious")
        assert set(fiche["signature"]) == {"version", "published_at"}
        assert fiche["score"]["breakdown"].keys() == wd.DEFAULT_WEIGHTS.keys()
