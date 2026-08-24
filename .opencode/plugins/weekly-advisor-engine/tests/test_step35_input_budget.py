"""Budget tokens étape 3.5 (Task 11) : inputs post-refonte vs baseline legacy.

Mesure les octets réellement consommés par le LLM de l'étape 3.5 sur la
fixture golden (run réel 2026-08-23, 323 items) :

- avant (v6.1) : ``weekly-ecosystem`` (TOUS les items) + ``watch-context``
  non scopé (crosswalk exhaustif) ;
- après (v7) : ``watch-candidates-enriched`` (fiches × état local + bande
  résiduelle plafonnée) + ``watch-memory-digest`` (mémoire bornée).

``weekly-quality-findings-<date>.json`` (~3-7 Ko en production) est un terme
constant lu par l'agent des deux côtés : il est exclu de la comparaison car la
refonte ne change ni sa taille ni sa présence ; le rapport de validation
documente les totaux absolus l'incluant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weekly_telemetry_aggregator import watch_distill as wd
from weekly_telemetry_aggregator.util import parse_anchor
from weekly_telemetry_aggregator.watch_context import (
    build_local_inventory,
    build_watch_context,
    enrich_candidates,
)
from weekly_telemetry_aggregator.writer import write_json_atomic

ANCHOR = "2026-08-24T12:00:00Z"
DATE = "2026-08-24"
FIXTURE = Path(__file__).parent / "fixtures" / "golden-ecosystem-2026-08-23.json"


def _cfg(tmp_path: Path):
    from weekly_telemetry_aggregator.config import TelemetryConfig

    return TelemetryConfig(output_dir=tmp_path, project_root=tmp_path)


@pytest.mark.slow
def test_step35_llm_inputs_stay_under_ten_percent_of_legacy(tmp_path: Path):
    """Chaîne déterministe complète ; assert octets 3.5 < 10 % de la baseline."""
    eco_text = FIXTURE.read_text(encoding="utf-8")
    ecosystem = json.loads(eco_text)
    assert len(ecosystem["new_items"]) == 323
    (tmp_path / f"weekly-ecosystem-{DATE}.json").write_text(eco_text, encoding="utf-8")

    result, rc = wd.run(_cfg(tmp_path), anchor=ANCHOR)
    assert rc == 0
    assert result["mode"] == "distill"

    run_time = parse_anchor(ANCHOR)
    candidates_path = tmp_path / f"watch-candidates-{DATE}.json"
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    context_new = build_watch_context(
        tmp_path,
        ecosystem,
        generated_at=run_time,
        ecosystem_path=tmp_path / f"weekly-ecosystem-{DATE}.json",
        candidates_path=candidates_path,
    )
    inventory = build_local_inventory(tmp_path)
    enriched = enrich_candidates(
        candidates, context_new, ecosystem, inventory["items"], now=run_time
    )
    assert enriched is not None
    assert enriched["mode"] == "enriched"

    # Sérialisation identique à la CLI : enriched compact (indent=None),
    # contexte legacy indenté (défaut writer).
    enriched_path = tmp_path / f"watch-candidates-enriched-{DATE}.json"
    write_json_atomic(enriched_path, enriched, indent=None)
    digest_path = tmp_path / f"watch-memory-digest-{DATE}.json"
    new_inputs = enriched_path.stat().st_size + digest_path.stat().st_size

    context_legacy = build_watch_context(tmp_path, ecosystem, generated_at=run_time)
    legacy_payload = json.dumps(context_legacy, indent=2, ensure_ascii=False) + "\n"
    old_inputs = len(eco_text.encode("utf-8")) + len(legacy_payload.encode("utf-8"))

    ratio = new_inputs / old_inputs
    assert ratio < 0.10, f"inputs 3.5 = {new_inputs} o / {old_inputs} o = {ratio:.1%}"
