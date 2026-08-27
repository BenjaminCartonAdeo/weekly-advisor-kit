"""Contrat de données du fan-out S2 (WAVE 1.5, audit parallèle).

Le fan-out lui-même (spawn de K workers via `task`) est déclaratif (markdown
`weekly-advisor.md` / `weekly-advisor-worker.md`) — non exécutable en Python pur.
Ce fichier teste la partie *testable* du contrat :

1. Schéma minimal de l'artifact par worker A :
   `audit-findings-<id>.json` = {session_id, summary, findings: [...], rc}
   avec l'invariant anti-empty-dispatch (S3) : `summary` non-vide OBLIGATOIRE,
   sauf fail-soft (`summary: null` autorisé UNIQUEMENT si `rc == 1`).

2. Invariant de consolidation déterministe (WAVE 1.5 JOIN) : merge des K
   `audit-findings-*.json` → `weekly-quality-findings-<date>.json` en conservant
   les K sessions (ids distincts) et un ordre déterministe (tri par session_id),
   sans re-LLM.

Les validateurs ci-dessous sont des DOUBLES DE TEST (pas de code de production) :
ils cristallisent le contrat documenté pour empêcher une régression de format qui
casserait la consolidation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REQUIRED_KEYS = ("session_id", "summary", "findings", "rc")


def validate_audit_findings(data: object) -> None:
    """Contrat minimal d'un artifact `audit-findings-<id>.json` (branch A).

    Lève AssertionError si le contrat n'est pas respecté (comportement attendu
    d'un validateur de contrat : fail loud sur format invalide).
    """
    assert isinstance(data, dict), "audit-findings doit être un objet JSON"
    for key in REQUIRED_KEYS:
        assert key in data, f"champ requis manquant: {key}"
    assert isinstance(data["session_id"], str) and data["session_id"], (
        "session_id doit être une chaîne non-vide"
    )
    assert isinstance(data["findings"], list), "findings doit être une liste"
    assert isinstance(data["rc"], int) and data["rc"] in (0, 1, 2), "rc ∈ {0,1,2}"
    # Anti-empty-dispatch (S3) : summary null toléré UNIQUEMENT en fail-soft (rc=1).
    if data["summary"] is None:
        assert data["rc"] == 1, "summary null exige rc=1 (fail-soft uniquement)"
    else:
        assert isinstance(data["summary"], str) and data["summary"].strip(), (
            "summary non-vide OBLIGATOIRE (sauf fail-soft rc=1)"
        )


def consolidate(findings: list[dict]) -> dict:
    """Consolidation déterministe WAVE 1.5 JOIN (mirror du contrat déclaratif).

    Tri par session_id → ordre reproductible quel que soit l'ordre de fin des
    workers ; aucun re-LLM (on merge les artifacts tels quels).
    """
    ordered = sorted(findings, key=lambda f: f["session_id"])
    return {
        "sessions_audited": len(ordered),
        "findings": ordered,
    }


# ============================================================ schéma minimal


def _valid_finding(session_id: str, *, rc: int = 0, summary: str | None = "ok") -> dict:
    return {
        "session_id": session_id,
        "summary": summary,
        "findings": [{"category": "perf", "text": "x"}],
        "rc": rc,
    }


def test_valid_audit_findings_passes_contract():
    validate_audit_findings(_valid_finding("ses_a"))


@pytest.mark.parametrize("missing", list(REQUIRED_KEYS))
def test_audit_findings_rejects_missing_required_key(missing: str):
    data = _valid_finding("ses_a")
    del data[missing]
    with pytest.raises(AssertionError):
        validate_audit_findings(data)


def test_audit_findings_rejects_invalid_rc():
    with pytest.raises(AssertionError):
        validate_audit_findings(_valid_finding("ses_a", rc=9))


def test_audit_findings_rejects_empty_summary_when_rc0():
    """Anti-empty-dispatch (S3) : summary vide interdit hors fail-soft."""
    with pytest.raises(AssertionError):
        validate_audit_findings(_valid_finding("ses_a", rc=0, summary="   "))


def test_audit_findings_allows_null_summary_only_with_rc1():
    """Fail-soft (worker A) : extrait illisible → rc=1 + summary null, fichier écrit."""
    validate_audit_findings(_valid_finding("ses_a", rc=1, summary=None))


def test_audit_findings_rejects_null_summary_with_rc0():
    """Null summary sans rc=1 est une violation de contrat."""
    with pytest.raises(AssertionError):
        validate_audit_findings(_valid_finding("ses_a", rc=0, summary=None))


# ============================================================ consolidation JOIN


def test_consolidation_preserves_all_sessions_and_is_deterministic():
    """WAVE 1.5 JOIN : K artifacts → K sessions distinctes, ordre déterministe.

    L'ordre de fin des workers ne doit pas influencer le résultat (tri par id).
    """
    a = _valid_finding("ses_c")
    b = _valid_finding("ses_a")
    c = _valid_finding("ses_b")

    merged_forward = consolidate([a, b, c])
    merged_reversed = consolidate([c, b, a])  # workers finissent dans un autre ordre

    assert merged_forward["sessions_audited"] == 3
    assert merged_reversed["sessions_audited"] == 3
    # Même contenu, ordre reproductible.
    assert merged_forward == merged_reversed
    ids = [f["session_id"] for f in merged_forward["findings"]]
    assert ids == ["ses_a", "ses_b", "ses_c"]  # tri déterministe
    assert len(set(ids)) == 3  # sessions distinctes préservées


def test_consolidation_is_json_serializable(tmp_path: Path):
    """L'artifact consolidé doit être du JSON valide (contrat de format strict)."""
    merged = consolidate([_valid_finding("ses_a"), _valid_finding("ses_b")])
    out = tmp_path / "weekly-quality-findings-2026-08-25.json"
    out.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded["sessions_audited"] == 2
