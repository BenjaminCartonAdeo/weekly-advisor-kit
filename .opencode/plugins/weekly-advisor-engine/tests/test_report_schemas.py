"""Caractérisation des validateurs de schéma de `report.py` (cluster 1357-1540).

Ces fonctions ne sont atteignables aujourd'hui que transitivement, via
``validate_required_artifacts`` : aucun test existant ne les nomme. On les
pilote directement pour figer le contrat *actuel* — y compris les trous
silencieux (`bool` vs `int`, dates hors calendrier, `limit` négatif), qui sont
signalés dans le rapport du run et non corrigés ici.

Chaque assertion porte sur la valeur de retour exacte (`is True` / `is False`),
jamais sur la simple véracité.
"""

from __future__ import annotations

import pytest

from weekly_telemetry_aggregator.report import (
    _ARTIFACT_CONTRACTS,
    _artifact_contract_valid,
    _coerce_rc,
    _has_findings_list,
    _json_file_state,
    _nonempty_text,
    _valid_audit_candidates,
    _valid_coherence,
    _valid_date,
    _valid_ecosystem,
    _valid_harness_digest,
    _valid_remediation,
    _valid_remediation_proposals,
    _valid_schema_version,
    _valid_skill_curate,
    _valid_timings,
    _valid_watch_candidates,
    _valid_watch_context,
    _valid_watch_findings,
    _valid_weekly_insights,
    _valid_weekly_summary,
)

DATE = "2026-10-08"


# =====================================================================
# _valid_schema_version — le trou `bool`/`int` est ici, pas ailleurs
# =====================================================================


@pytest.mark.parametrize(
    ("value", "expected", "label"),
    [
        (1, True, "int attendu"),
        (2, False, "autre version"),
        (0, False, "zéro"),
        (-1, False, "négatif"),
        (True, False, "bool True vaut 1 mais est rejeté"),
        (False, False, "bool False vaut 0 mais est rejeté"),
        (1.0, False, "float 1.0 n'est pas un int"),
        ("1", False, "chaîne numérique"),
        (None, False, "None"),
    ],
)
def test_valid_schema_version_pins_bool_and_type(value, expected, label):
    assert _valid_schema_version(value, 1) is expected, label


def test_valid_schema_version_expects_may_be_any_int():
    assert _valid_schema_version(42, 42) is True


# =====================================================================
# _nonempty_text
# =====================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("x", True),
        (" x ", True),
        ("", False),
        (" ", False),
        ("\t\n", False),
        (5, False),
        (0, False),
        (True, False),
        (None, False),
        (["a"], False),
        ({"a": 1}, False),
        (b"a", False),
    ],
)
def test_nonempty_text(value, expected):
    assert _nonempty_text(value) is expected


# =====================================================================
# _valid_date — borne de forme seulement (pas de calendrier)
# =====================================================================


@pytest.mark.parametrize(
    ("value", "expected", "label"),
    [
        ("2026-10-08", True, "forme exacte"),
        ("0000-00-00", True, "zéros acceptés (aucune validation calendrier)"),
        ("2026-13-45", True, "mois/jour hors calendrier acceptés — trou connu"),
        ("2026-10-08T00:00", False, "suffixe datetime"),
        ("2026-10-08 ", False, "espace final"),
        (" 2026-10-08", False, "espace initial"),
        ("2026-1-8", False, "champs non paddés"),
        ("26-10-08", False, "année sur 2 chiffres"),
        ("2026/10/08", False, "séparateurs"),
        ("20261008", False, "aucun séparateur"),
        ("2026-10-0", False, "jour sur 1 chiffre"),
        (20261008, False, "int"),
        (True, False, "bool"),
        (None, False, "None"),
    ],
)
def test_valid_date(value, expected, label):
    assert _valid_date(value) is expected, label


# =====================================================================
# _coerce_rc — bornes, bool vs int, coercition de chaîne
# =====================================================================


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (0, None, 0),
        (2, None, 2),
        (-1, 7, 7),
        (-2, 7, 7),
        ("2", None, 2),
        (" 2 ", None, 2),
        ("0", None, 0),
        ("-1", 5, 5),
        ("2.0", 5, 5),
        ("crash", 5, 5),
        ("", 5, 5),
        (True, 0, 0),
        (False, 0, 0),
        (None, 1, 1),
        (1.0, 3, 3),
        ([], 3, 3),
        ({"rc": 1}, 3, 3),
    ],
)
def test_coerce_rc(value, default, expected):
    assert _coerce_rc(value, default=default) == expected


def test_coerce_rc_without_default_rejects_bool_none_and_negative():
    """`default=None` rend le statut « inconnu », jamais un 0 accidentel."""
    assert _coerce_rc(True, default=None) is None
    assert _coerce_rc(False, default=None) is None
    assert _coerce_rc(None, default=None) is None
    assert _coerce_rc(-1, default=None) is None
    assert _coerce_rc("crash", default=None) is None
    # une vraie valeur reste intacte, y compris « 0 »
    assert _coerce_rc(0, default=None) == 0
    assert _coerce_rc("0", default=None) == 0


def test_coerce_rc_boundary_zero_is_valid_negative_is_not():
    assert _coerce_rc(0, default=-1) == 0  # borne exacte acceptée
    assert _coerce_rc(-1, default=-1) == -1  # une unité past → default


# =====================================================================
# _json_file_state — absent / illisible / présent
# =====================================================================


def test_json_file_state_distinguishes_absent_blank_and_non_dict(tmp_path):
    absent = tmp_path / "absent.json"
    assert _json_file_state(absent) == (None, "absent")

    blank = tmp_path / "blank.json"
    blank.write_text("   \n", encoding="utf-8")
    assert _json_file_state(blank) == (None, "ill_readable")

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert _json_file_state(broken) == (None, "ill_readable")

    listed = tmp_path / "listed.json"
    listed.write_text("[1, 2]", encoding="utf-8")
    assert _json_file_state(listed) == (None, "ill_readable")

    good = tmp_path / "good.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    assert _json_file_state(good) == ({"a": 1}, "present")

    empty_dict = tmp_path / "empty_dict.json"
    empty_dict.write_text("{}", encoding="utf-8")
    assert _json_file_state(empty_dict) == ({}, "present")


# =====================================================================
# _valid_weekly_summary — schema_version 2
# =====================================================================


def _summary() -> dict:
    return {
        "schema_version": 2,
        "period": {"start": "2026-10-01", "end": "2026-10-08"},
        "generated_at": "2026-10-08T07:00:00+00:00",
        "totals": {},
    }


def test_valid_weekly_summary_accepts_the_contract():
    assert _valid_weekly_summary(_summary()) is True


@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        (lambda v: v.update(schema_version=1), "version 1 refusée"),
        (lambda v: v.update(schema_version=3), "version 3 refusée"),
        (lambda v: v.update(schema_version=True), "bool True refusé"),
        (lambda v: v.pop("schema_version"), "clé manquante"),
        (lambda v: v.update(period=[]), "period liste"),
        (lambda v: v.update(period=None), "period None"),
        (lambda v: v["period"].pop("start"), "start manquant"),
        (lambda v: v["period"].update(start="   "), "start blanc"),
        (lambda v: v["period"].update(start=None), "start None"),
        (lambda v: v["period"].pop("end"), "end manquant"),
        (lambda v: v.pop("generated_at"), "generated_at manquant"),
        (lambda v: v.update(generated_at=""), "generated_at vide"),
        (lambda v: v.pop("totals"), "totals manquant"),
        (lambda v: v.update(totals=None), "totals None"),
        (lambda v: v.update(totals=[]), "totals liste"),
    ],
)
def test_valid_weekly_summary_rejections(mutate, label):
    payload = _summary()
    mutate(payload)
    assert _valid_weekly_summary(payload) is False, label


def test_valid_weekly_summary_empty_payload_is_invalid():
    assert _valid_weekly_summary({}) is False


# =====================================================================
# _valid_weekly_insights — schema_version 1, six champs requis
# =====================================================================


def _insights() -> dict:
    return {
        "schema_version": 1,
        "period": {},
        "generated_at": "2026-10-08T07:00:00+00:00",
        "deltas": {},
        "alerts": [],
        "maintenance": {},
    }


def test_valid_weekly_insights_accepts_the_contract():
    assert _valid_weekly_insights(_insights()) is True


@pytest.mark.parametrize(
    ("key", "bad", "label"),
    [
        ("schema_version", 2, "version attendue 1"),
        ("schema_version", True, "bool refusé"),
        ("schema_version", None, "None refusé"),
        ("period", None, "period non-mapping"),
        ("period", [], "period liste"),
        ("generated_at", None, "generated_at None"),
        ("generated_at", "  ", "generated_at blanc"),
        ("deltas", None, "deltas None"),
        ("deltas", [], "deltas liste"),
        ("alerts", None, "alerts None"),
        ("alerts", {}, "alerts mapping"),
        ("maintenance", None, "maintenance None"),
        ("maintenance", [], "maintenance liste"),
    ],
)
def test_valid_weekly_insights_rejections(key, bad, label):
    payload = _insights()
    payload[key] = bad
    assert _valid_weekly_insights(payload) is False, label


@pytest.mark.parametrize("key", ["period", "generated_at", "deltas", "alerts", "maintenance"])
def test_valid_weekly_insights_missing_key(key):
    payload = _insights()
    payload.pop(key)
    assert _valid_weekly_insights(payload) is False


# =====================================================================
# _valid_harness_digest — disjonction « inspection | rules | findings »
# =====================================================================


def test_valid_harness_digest_accepts_any_single_nonempty_source():
    assert _valid_harness_digest({"inspection": {"a": 1}}) is True
    assert _valid_harness_digest({"rules": [{"id": "r"}]}) is True
    assert _valid_harness_digest({"findings": [{"rule": "r"}]}) is True
    # les trois sources ensemble restent valides
    assert _valid_harness_digest({"inspection": {}, "rules": [1], "findings": [2]}) is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"inspection": {}},
        {"inspection": {}, "rules": []},
        {"inspection": {}, "findings": []},
        {"inspection": [], "rules": {}, "findings": None},
        {"inspection": "x"},
        {"rules": None, "findings": {}},
    ],
)
def test_valid_harness_digest_rejects_empty_or_wrong_typed_sources(payload):
    assert _valid_harness_digest(payload) is False


def test_valid_harness_digest_ignores_schema_version_entirely():
    """Aucun contrôle de version : un digest sans `schema_version` passe."""
    assert _valid_harness_digest({"inspection": {"a": 1}}) is True
    assert _valid_harness_digest({"schema_version": 99, "findings": [1]}) is True


# =====================================================================
# _valid_ecosystem — schema_version 2, trois listes
# =====================================================================


def test_valid_ecosystem_accepts_the_contract():
    assert (
        _valid_ecosystem({"schema_version": 2, "new_items": [], "core_changes": [], "warnings": []})
        is True
    )


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("schema_version", 1),
        ("schema_version", 3),
        ("schema_version", True),
        ("new_items", None),
        ("new_items", {}),
        ("core_changes", None),
        ("warnings", None),
        ("warnings", "warn"),
    ],
)
def test_valid_ecosystem_rejections(key, bad):
    payload = {"schema_version": 2, "new_items": [], "core_changes": [], "warnings": []}
    payload[key] = bad
    assert _valid_ecosystem(payload) is False


# =====================================================================
# _has_findings_list — simple présence de liste
# =====================================================================


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"findings": []}, True),
        ({"findings": [{}]}, True),
        ({}, False),
        ({"findings": None}, False),
        ({"findings": {}}, False),
        ({"findings": "x"}, False),
        ({"findings": ("a",)}, False),
    ],
)
def test_has_findings_list(payload, expected):
    assert _has_findings_list(payload) is expected


def test_has_findings_list_is_the_contract_for_two_artifact_names():
    """Les deux artefacts « findings » partagent ce validateur minimal."""
    assert _ARTIFACT_CONTRACTS["weekly-quality-findings"] is _has_findings_list
    assert _ARTIFACT_CONTRACTS["weekly-watch-findings-raw"] is _has_findings_list


# =====================================================================
# _valid_coherence — findings[] OU curation_signal (list|Mapping)
# =====================================================================


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"findings": []}, True),
        ({"findings": [1]}, True),
        ({"curation_signal": []}, True),
        ({"curation_signal": {}}, True),
        ({"findings": [], "curation_signal": {}}, True),
        ({}, False),
        ({"findings": None}, False),
        ({"curation_signal": None}, False),
        ({"curation_signal": "archive"}, False),
        ({"curation_signal": ("archive",)}, False),
        ({"findings": {}, "curation_signal": 1}, False),
    ],
)
def test_valid_coherence(payload, expected):
    assert _valid_coherence(payload) is expected


# =====================================================================
# _valid_audit_candidates — schema_version 1 + `limit` int non-bool
# =====================================================================


def _candidates() -> dict:
    return {"schema_version": 1, "audited": [], "unaudited": [], "limit": 0}


def test_valid_audit_candidates_accepts_the_contract():
    assert _valid_audit_candidates(_candidates()) is True


@pytest.mark.parametrize(
    ("key", "bad", "label"),
    [
        ("schema_version", 2, "version attendue 1"),
        ("schema_version", True, "bool refusé"),
        ("schema_version", None, "None refusé"),
        ("audited", None, "audited None"),
        ("audited", {}, "audited mapping"),
        ("unaudited", None, "unaudited None"),
        ("limit", True, "limit True (bool) refusé"),
        ("limit", False, "limit False (bool) refusé"),
        ("limit", 1.0, "limit float"),
        ("limit", "3", "limit chaîne"),
        ("limit", None, "limit None"),
    ],
)
def test_valid_audit_candidates_rejections(key, bad, label):
    payload = _candidates()
    payload[key] = bad
    assert _valid_audit_candidates(payload) is False, label


def test_valid_audit_candidates_accepts_negative_limit():
    """Trou connu : `limit` négatif accepté (aucun contrôle de plage)."""
    assert _valid_audit_candidates({**_candidates(), "limit": -5}) is True
    assert _valid_audit_candidates({**_candidates(), "limit": 10**9}) is True


@pytest.mark.parametrize("key", ["schema_version", "audited", "unaudited", "limit"])
def test_valid_audit_candidates_missing_key(key):
    payload = _candidates()
    payload.pop(key)
    assert _valid_audit_candidates(payload) is False


# =====================================================================
# _valid_watch_context / _valid_watch_findings / _valid_watch_candidates
# =====================================================================


def test_valid_watch_context_accepts_the_contract():
    assert _valid_watch_context({"schema_version": 1, "market_matches": []}) is True


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("market_matches", None),
        ("market_matches", {}),
    ],
)
def test_valid_watch_context_rejections(key, bad):
    payload = {"schema_version": 1, "market_matches": []}
    payload[key] = bad
    assert _valid_watch_context(payload) is False


def test_valid_watch_context_requires_the_market_matches_key():
    assert _valid_watch_context({"schema_version": 1}) is False


def test_valid_watch_findings_accepts_the_contract():
    assert _valid_watch_findings({"schema_version": 2, "findings": [], "validation": {}}) is True


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("schema_version", 1),
        ("schema_version", 3),
        ("schema_version", True),
        ("findings", None),
        ("findings", {}),
        ("validation", None),
        ("validation", []),
    ],
)
def test_valid_watch_findings_rejections(key, bad):
    payload = {"schema_version": 2, "findings": [], "validation": {}}
    payload[key] = bad
    assert _valid_watch_findings(payload) is False


def test_valid_watch_candidates_accepts_the_contract():
    assert (
        _valid_watch_candidates({"schema_version": 1, "candidates": [], "security_annex": []})
        is True
    )


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("candidates", None),
        ("candidates", {}),
        ("security_annex", None),
        ("security_annex", "none"),
    ],
)
def test_valid_watch_candidates_rejections(key, bad):
    payload = {"schema_version": 1, "candidates": [], "security_annex": []}
    payload[key] = bad
    assert _valid_watch_candidates(payload) is False


def test_valid_watch_candidates_requires_both_keys():
    assert _valid_watch_candidates({"schema_version": 1, "candidates": []}) is False
    assert _valid_watch_candidates({"schema_version": 1, "security_annex": []}) is False


# =====================================================================
# _valid_remediation / _valid_timings — deuxshapes « au moins un champ »
# =====================================================================


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"summary": {}, "postcheck": {}}, True),
        ({"summary": {"a": 1}, "postcheck": {}}, True),
        ({"summary": {}}, False),
        ({"postcheck": {}}, False),
        ({}, False),
        ({"summary": None, "postcheck": None}, False),
        ({"summary": [], "postcheck": {}}, False),
    ],
)
def test_valid_remediation(payload, expected):
    assert _valid_remediation(payload) is expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"branches": {}}, True),
        ({"steps": []}, True),
        ({"steps": {}}, True),
        ({"branches": {}, "steps": []}, True),
        ({}, False),
        ({"branches": None, "steps": None}, False),
        ({"branches": [], "steps": "x"}, False),
        ({"branches": 1}, False),
        ({"steps": 1}, False),
    ],
)
def test_valid_timings(payload, expected):
    assert _valid_timings(payload) is expected


# =====================================================================
# _valid_remediation_proposals — v1 ET v2 acceptés, date comparée
# =====================================================================


@pytest.mark.parametrize("version", [1, 2])
def test_valid_remediation_proposals_accepts_v1_and_v2(version):
    assert (
        _valid_remediation_proposals(
            {"schema_version": version, "date": DATE, "proposals": []}, DATE
        )
        is True
    )


@pytest.mark.parametrize("version", [0, 3, True, "1", None])
def test_valid_remediation_proposals_rejects_other_versions(version):
    payload = {"schema_version": version, "date": DATE, "proposals": []}
    assert _valid_remediation_proposals(payload, DATE) is False


@pytest.mark.parametrize(
    ("mutate", "date", "label"),
    [
        (lambda v: v.update(date=None), DATE, "date None"),
        (lambda v: v.update(date="08-10-2026"), DATE, "date hors format"),
        (lambda v: v.update(date="2026-10-09"), DATE, "date ≠ anchor"),
        (lambda v: v.update(proposals=None), DATE, "proposals None"),
        (lambda v: v.update(proposals={}), DATE, "proposals mapping"),
        (lambda v: v.pop("date"), DATE, "date manquant"),
        (lambda v: v.pop("proposals"), DATE, "proposals manquant"),
        (lambda v: v.clear(), DATE, "payload vide"),
    ],
)
def test_valid_remediation_proposals_rejections(mutate, date, label):
    payload = {"schema_version": 2, "date": DATE, "proposals": []}
    mutate(payload)
    assert _valid_remediation_proposals(payload, date) is False, label


def test_valid_remediation_proposals_anchor_none_skips_the_comparison():
    payload = {"schema_version": 2, "date": "1999-01-01", "proposals": []}
    assert _valid_remediation_proposals(payload, None) is True
    assert _valid_remediation_proposals(payload, "2026-10-08") is False


# =====================================================================
# _valid_skill_curate — mode enum, rc borné, v2 strict, v1 behind a flag
# =====================================================================


def _curate() -> dict:
    return {
        "schema_version": 2,
        "mode": "dry-run",
        "dry_run": True,
        "decisions": [],
        "rc": 0,
        "date": DATE,
        "generated_at": "2026-10-08T07:00:00+00:00",
        "anchor": "2026-10-01",
        "summary": {},
        "skipped_details": [],
    }


def test_valid_skill_curate_accepts_the_v2_contract():
    assert _valid_skill_curate(_curate(), DATE, False) is True
    # anchor=None : la comparaison de date est sautée, le reste est inchangé
    assert _valid_skill_curate(_curate(), None, False) is True


@pytest.mark.parametrize("mode", ["dry-run", "dry_run", "apply"])
def test_valid_skill_curate_accepts_the_three_modes(mode):
    assert _valid_skill_curate({**_curate(), "mode": mode}, DATE, False) is True


@pytest.mark.parametrize("mode", ["APPLY", "force", "", None, 1, True])
def test_valid_skill_curate_rejects_unknown_modes(mode):
    assert _valid_skill_curate({**_curate(), "mode": mode}, DATE, False) is False


@pytest.mark.parametrize("mode", [["apply"], {"mode": "apply"}])
def test_valid_skill_curate_unhashable_mode_raises_typeerror(mode):
    """Trou connu : `mode in {...}` lève `TypeError` sur une valeur non hashable.

    Le validateur est censé retourner `False` pour toute entrée hors contrat ;
    un `mode` liste issu d'un JSON malformé remonte une exception jusqu'à
    l'appelant (`_check_required_json` → `validate_required_artifacts`).
    """
    with pytest.raises(TypeError):
        _valid_skill_curate({**_curate(), "mode": mode}, DATE, False)


@pytest.mark.parametrize("bad", [1, 0, "1", None])
def test_valid_skill_curate_rejects_non_bool_dry_run(bad):
    assert _valid_skill_curate({**_curate(), "dry_run": bad}, DATE, False) is False


def test_valid_skill_curate_accepts_both_bool_dry_run_values():
    assert _valid_skill_curate({**_curate(), "dry_run": True}, DATE, False) is True
    assert _valid_skill_curate({**_curate(), "dry_run": False}, DATE, False) is True


def test_valid_skill_curate_rc_uses_coerce_rc_semantics():
    assert _valid_skill_curate({**_curate(), "rc": 1}, DATE, False) is True
    assert _valid_skill_curate({**_curate(), "rc": "2"}, DATE, False) is True
    # borne basse : -1 est hors plage → refus
    assert _valid_skill_curate({**_curate(), "rc": -1}, DATE, False) is False
    # bool True est un int en Python mais est traité comme absent
    assert _valid_skill_curate({**_curate(), "rc": True}, DATE, False) is False


@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        (lambda v: v.pop("decisions"), "decisions manquant"),
        (lambda v: v.update(decisions=None), "decisions None"),
        (lambda v: v.update(decisions={}), "decisions mapping"),
        (lambda v: v.pop("date"), "date manquant"),
        (lambda v: v.update(date="2026-10-09"), "date ≠ anchor"),
        (lambda v: v.update(date="bad"), "date hors format"),
        (lambda v: v.pop("generated_at"), "generated_at manquant"),
        (lambda v: v.update(generated_at="  "), "generated_at blanc"),
        (lambda v: v.pop("anchor"), "anchor manquant"),
        (lambda v: v.update(anchor=""), "anchor vide"),
        (lambda v: v.update(summary=None), "summary None"),
        (lambda v: v.update(summary=[]), "summary liste"),
        (lambda v: v.update(skipped_details=None), "skipped_details None"),
        (lambda v: v.update(skipped_details={}), "skipped_details mapping"),
    ],
)
def test_valid_skill_curate_v2_rejections(mutate, label):
    payload = _curate()
    mutate(payload)
    assert _valid_skill_curate(payload, DATE, False) is False, label


def test_valid_skill_curate_v1_is_only_accepted_behind_the_legacy_flag():
    legacy = {
        "schema_version": 1,
        "mode": "apply",
        "dry_run": False,
        "decisions": [],
        "rc": 1,
        "date": DATE,
    }
    assert _valid_skill_curate(legacy, DATE, allow_legacy_v1=False) is False
    assert _valid_skill_curate(legacy, DATE, allow_legacy_v1=True) is True
    # le drapeau n'ouvre pas la version 3
    assert _valid_skill_curate({**legacy, "schema_version": 3}, DATE, allow_legacy_v1=True) is False
    # ni les autres fautes du socle commun
    assert _valid_skill_curate({**legacy, "rc": -1}, DATE, allow_legacy_v1=True) is False


# =====================================================================
# _artifact_contract_valid — dispatch par nom + fallbacks
# =====================================================================


def test_artifact_contract_dispatches_by_name_and_date():
    assert _artifact_contract_valid("weekly-summary", _summary(), date=DATE) is True
    assert (
        _artifact_contract_valid(
            "weekly-harness-remediation-proposals",
            {"schema_version": 2, "date": DATE, "proposals": []},
            date=DATE,
        )
        is True
    )
    assert _artifact_contract_valid("skill-curate", _curate(), date=DATE) is True


def test_artifact_contract_rejects_empty_non_mapping_and_wrong_shape():
    assert _artifact_contract_valid("weekly-summary", {}) is False
    assert _artifact_contract_valid("weekly-summary", []) is False
    assert _artifact_contract_valid("weekly-summary", "text") is False
    assert _artifact_contract_valid("weekly-summary", None) is False
    assert _artifact_contract_valid("weekly-summary", {"schema_version": 1}) is False


def test_artifact_contract_ignores_the_date_for_non_dated_artifacts():
    """`date` ne sert qu'aux deux artefacts datés ; sinon il est ignoré."""
    assert _artifact_contract_valid("weekly-summary", _summary(), date="not-a-date") is True
    assert _artifact_contract_valid("weekly-insights", _insights(), date="2026-99-99") is True


def test_artifact_contract_gates_legacy_curate_on_the_flag():
    legacy = {
        "schema_version": 1,
        "mode": "apply",
        "dry_run": False,
        "decisions": [],
        "rc": 0,
        "date": DATE,
    }
    assert _artifact_contract_valid("skill-curate", legacy, date=DATE) is False
    assert _artifact_contract_valid("skill-curate", legacy, date=DATE, allow_legacy_v1=True) is True


def test_artifact_contract_unknown_name_only_needs_a_non_empty_mapping():
    assert _artifact_contract_valid("brand-new-artefact", {"a": 1}) is True
    assert _artifact_contract_valid("brand-new-artefact", {}) is False
    assert _artifact_contract_valid("brand-new-artefact", ["a"]) is False


def test_artifact_contract_table_covers_every_named_contract():
    expected = {
        "weekly-summary",
        "weekly-insights",
        "weekly-harness-digest",
        "weekly-ecosystem",
        "weekly-quality-findings",
        "weekly-watch-findings-raw",
        "weekly-coherence-findings",
        "weekly-audit-candidates",
        "weekly-watch-context",
        "weekly-watch-findings",
        "watch-candidates",
        "weekly-harness-remediation",
        "weekly-timings",
    }
    assert set(_ARTIFACT_CONTRACTS) == expected
    # les deux noms « datés » sont hors table : traités par cas particulier
    assert "weekly-harness-remediation-proposals" not in _ARTIFACT_CONTRACTS
    assert "skill-curate" not in _ARTIFACT_CONTRACTS
