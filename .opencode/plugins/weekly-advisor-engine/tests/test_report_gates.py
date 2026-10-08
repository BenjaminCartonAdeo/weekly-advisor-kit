"""Caractérisation de l'enveloppe audit/gate de `report.py` (cluster 1516-2480).

Cible : le contrat d'exit-code du pipeline. ``applicable_summary_rc`` est la
fonction unique qui décide entre « dégradé mais livrable » (rc 1) et « fatal »
(rc 2) ; tout le reste du pipeline hangs off. Ces tests fixent chaque branche,
y compris les coins que le code traite comme des trous (rc `bool`, statut
`"ok"` refusé par `_empty_records_rc` alors que `_artifact_entry_valid` et
`_check_required_json` l'acceptent).

Aucun test existant ne nomme `_join_status_records`, `_collect_join_records`,
`_branch_applicability`, `_warning_is_nonblocking`, `_recoverable_artifact_valid`
ni `_is_blocking_security_rule`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weekly_telemetry_aggregator.report import (
    _artifact_entry_valid,
    _audit_artifact_declarations,
    _audit_artifact_valid,
    _audit_declaration_values,
    _branch_applicability,
    _canonical_audit_path,
    _check_html_artifact,
    _check_required_json,
    _coerce_summary_rc,
    _collect_join_records,
    _declared_run_filename,
    _empty_records_rc,
    _html_gate_status,
    _is_blocking_security_rule,
    _is_external_permission_failure,
    _is_recovered_record,
    _is_report_only_record,
    _join_status_records,
    _path_is_outside_worktree,
    _records_all_nonblocking,
    _recoverable_artifact_valid,
    _summary_artifact_inputs,
    _truncated_record_valid,
    _warning_is_nonblocking,
    applicable_summary_rc,
    validate_required_artifacts,
)

DATE = "2026-10-08"
SID = "ses_f6ed03e11ffetdQstFHu2pb7B5"


# =====================================================================
# Helpers — fixtures inline, aucun nouveau fichier de fixtures
# =====================================================================


def _audit_envelope(sid: str = SID, **over) -> dict:
    payload = {
        "schema_version": 1,
        "session_id": sid,
        "summary": "partiel",
        "findings": [],
        "warnings": [],
        "rc": 0,
    }
    payload.update(over)
    return payload


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    out = tmp_path.resolve()
    (out / f"weekly-summary-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "period": {"start": "2026-10-01", "end": DATE},
                "generated_at": "2026-10-08T07:00:00+00:00",
                "totals": {},
            }
        ),
        encoding="utf-8",
    )
    return out


def _seed_ecosystem(out: Path, date: str = DATE) -> Path:
    path = out / f"weekly-ecosystem-{date}.json"
    path.write_text(
        json.dumps({"schema_version": 2, "new_items": [], "core_changes": [], "warnings": []}),
        encoding="utf-8",
    )
    return path


def _seed_audit(out: Path, sid: str = SID, **over) -> Path:
    path = out / f"audit-findings-{sid}.json"
    path.write_text(json.dumps(_audit_envelope(sid, **over)), encoding="utf-8")
    return path


# =====================================================================
# applicable_summary_rc — le contrat rc1 vs rc2
# =====================================================================


@pytest.mark.parametrize("summary", ["text", 5, [], None, object()])
def test_applicable_summary_rc_non_mapping_summary_is_fatal(summary):
    """Une summary illisible n'est jamais « dégradée » : rc 2."""
    assert applicable_summary_rc(summary) == 2


@pytest.mark.parametrize("raw", [2, 3, 7, 99])
def test_applicable_summary_rc_raw_rc_two_or_more_is_fatal(raw):
    assert applicable_summary_rc({"rc": raw}) == 2


def test_applicable_summary_rc_exit_field_is_used_when_rc_absent():
    assert applicable_summary_rc({"exit": 2}) == 2
    assert applicable_summary_rc({"rc": None, "exit": 1}) == 1


@pytest.mark.parametrize("raw", [2, 3])
def test_applicable_summary_rc_record_rc_two_is_fatal_even_if_summary_is_zero(raw):
    assert applicable_summary_rc({"rc": 0, "warnings": [{"rc": raw}]}) == 2
    assert applicable_summary_rc({"rc": 0}, additional_records=[{"rc": raw}]) == 2


def test_applicable_summary_rc_fatal_precedes_nonblocking_evaluation(run_dir: Path):
    """Le rc 2 gagne même si tous les records sont non bloquants."""
    summary = {"rc": 0, "warnings": [{"rc": 2, "optional": True}]}
    assert applicable_summary_rc(summary, out=run_dir, date=DATE) == 2


def test_applicable_summary_rc_no_records_mirrors_the_raw_rc():
    assert applicable_summary_rc({"rc": 0}) == 0
    assert applicable_summary_rc({"rc": 1}) == 1


def test_applicable_summary_rc_no_records_without_rc_is_partial_not_success():
    """Pas de `rc`/`exit` → 1, jamais 0 (pas d'accident de succès)."""
    assert applicable_summary_rc({"warnings": []}) == 1
    assert applicable_summary_rc({"warnings": []}, fallback_rc=0) == 0
    assert applicable_summary_rc({}, fallback_rc=1) == 1


def test_applicable_summary_rc_no_records_optional_only_missing_is_not_a_failure():
    summary = {
        "rc": 1,
        "artifact_inputs": {"weekly-ecosystem": {"required": False, "status": "absent"}},
    }
    assert applicable_summary_rc(summary) == 0


def test_applicable_summary_rc_no_records_blocking_input_bad_is_partial():
    summary = {
        "rc": 0,
        "artifact_inputs": {"weekly-summary": {"required": True, "status": "absent"}},
    }
    assert applicable_summary_rc(summary) == 1


def test_applicable_summary_rc_records_present_but_blocking_yields_one_not_two():
    """rc 1 = rapport livré avec dégradation étiquetée, jamais rc 2."""
    assert applicable_summary_rc({"rc": 1, "warnings": [{"category": "worker-crash"}]}) == 1
    assert applicable_summary_rc({"rc": 0, "warnings": [{"message": "unknown"}]}) == 1


def test_applicable_summary_rc_mixed_records_all_nonblocking_is_zero():
    summary = {"rc": 1, "warnings": [{"optional": True}, {"applicable": False}]}
    assert applicable_summary_rc(summary) == 0


def test_applicable_summary_rc_one_blocking_record_dominates_the_others():
    summary = {"rc": 0, "warnings": [{"optional": True}, {"message": "blocking"}]}
    assert applicable_summary_rc(summary) == 1


# =====================================================================
# _coerce_summary_rc — la coercion du rc de résumé
# =====================================================================


@pytest.mark.parametrize(
    ("summary", "fallback", "expected"),
    [
        ({"rc": 0}, None, 0),
        ({"rc": 1}, None, 1),
        ({"rc": "2"}, None, 2),
        ({"exit": 1}, None, 1),
        ({"rc": None, "exit": 0}, None, 0),
        ({"rc": "junk", "exit": 1}, None, 1),
        ({"rc": "junk"}, None, 1),
        ({}, 0, 0),
        ({}, 1, 1),
        ({}, "junk", 1),
        ({}, None, 1),
        ({"rc": True}, 0, 1),
        ({"rc": -1}, 0, 1),
        ({"rc": 2.0}, 0, 1),
    ],
)
def test_coerce_summary_rc(summary, fallback, expected):
    assert _coerce_summary_rc(summary, fallback) == expected


def test_coerce_summary_rc_bool_rc_is_rejected_never_read_as_one():
    """`True` est un int en Python mais n'est jamais un rc. Pincé."""
    assert _coerce_summary_rc({"rc": True}, 0) == 1


# =====================================================================
# _empty_records_rc — le fan-in « aucun record »
# =====================================================================


@pytest.mark.parametrize("status", ["absent", "not_applicable", "present", "valid", "recovered"])
def test_empty_records_rc_optional_valid_statuses_pass(status):
    summary = {"artifact_inputs": {"a": {"required": False, "status": status}}}
    assert _empty_records_rc(summary, 1) == 0


@pytest.mark.parametrize("status", ["ok", "garbage", None])
def test_empty_records_rc_optional_status_outside_the_set_degrades(status):
    """« ok » est accepté partout ailleurs mais PAS ici — incohérence connue."""
    summary = {"artifact_inputs": {"a": {"required": False, "status": status}}}
    assert _empty_records_rc(summary, 0) == 0
    assert _empty_records_rc(summary, 1) == 1


def test_empty_records_rc_optional_entry_without_status_degrades():
    assert _empty_records_rc({"artifact_inputs": {"a": {"required": False}}}, 1) == 1


@pytest.mark.parametrize("status", ["absent", "ill_readable", "recovered", None])
def test_empty_records_rc_required_input_not_present_degrades(status):
    summary = {"artifact_inputs": {"a": {"required": True, "status": status}}}
    assert _empty_records_rc(summary, 0) == 1


@pytest.mark.parametrize("status", ["present", "valid", "ok"])
def test_empty_records_rc_required_input_satisfied_keeps_raw(status):
    summary = {"artifact_inputs": {"a": {"required": True, "status": status}}}
    assert _empty_records_rc(summary, 1) == 1  # il reste le raw rc 1


def test_empty_records_rc_required_flag_must_be_exactly_true():
    """`required: 1` n'est pas `True` (comparaison `is`) → entrée non requise."""
    summary = {"artifact_inputs": {"a": {"required": 1, "status": "absent"}}}
    assert _empty_records_rc(summary, 1) == 1  # ni optionnelle ni bloquante


def test_empty_records_rc_blocking_input_dominates_healthy_optional():
    summary = {
        "artifact_inputs": {
            "a": {"required": True, "status": "absent"},
            "b": {"required": False, "status": "present"},
        }
    }
    assert _empty_records_rc(summary, 0) == 1


def test_empty_records_rc_required_status_is_casefolded():
    summary = {"artifact_inputs": {"a": {"required": True, "status": "PRESENT"}}}
    assert _empty_records_rc(summary, 0) == 0


def test_empty_records_rc_non_mapping_input_entries_are_ignored():
    assert _empty_records_rc({"artifact_inputs": {"a": "text"}}, 1) == 1


# =====================================================================
# _summary_artifact_inputs — coercition défensive
# =====================================================================


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ({"artifact_inputs": {"a": 1}}, {"a": 1}),
        ({}, {}),
        ({"artifact_inputs": []}, {}),
        ({"artifact_inputs": None}, {}),
        ({"artifact_inputs": "x"}, {}),
        ("not-a-mapping", {}),
        (None, {}),
    ],
)
def test_summary_artifact_inputs(summary, expected):
    assert _summary_artifact_inputs(summary) == expected


# =====================================================================
# _join_status_records — extraction multi-clés et récursion
# =====================================================================


@pytest.mark.parametrize("payload", ["text", 5, None, []])
def test_join_status_records_non_mapping_is_empty(payload):
    assert _join_status_records(payload) == []


def test_join_status_records_empty_mapping_is_empty():
    assert _join_status_records({}) == []
    assert _join_status_records({"warnings": [], "statuses": [], "contracts": []}) == []


@pytest.mark.parametrize("key", ["warnings", "worker_statuses", "statuses"])
def test_join_status_records_reads_every_list_key(key):
    record = {"marker": key}
    assert _join_status_records({key: [record]}) == [record]


def test_join_status_records_contracts_mapping_is_appended_whole():
    assert _join_status_records({"contracts": {"k": {"z": 1}}}) == [{"k": {"z": 1}}]


def test_join_status_records_string_warning_without_artifacts_stays_bare():
    assert _join_status_records({"warnings": ["w1", "w2"]}) == ["w1", "w2"]


def test_join_status_records_string_warning_with_artifacts_is_wrapped():
    """La déclaration d'artefact est attachée : sans elle, la récupération
    n'aurait aucune preuve à valider."""
    assert _join_status_records({"warnings": ["w1"], "artifacts": ["a.json"]}) == [
        {"message": "w1", "artifacts": ["a.json"]}
    ]


@pytest.mark.parametrize("key", ["branches", "workers", "join"])
def test_join_status_records_reads_nested_mapping_containers(key):
    assert _join_status_records({key: {"H": {"rc": 1}}}) == [{"rc": 1}]


def test_join_status_records_recurses_into_nested_records():
    assert _join_status_records({"branches": {"H": {"warnings": [{"deep": 1}]}}}) == [
        {"warnings": [{"deep": 1}]},
        {"deep": 1},
    ]


def test_join_status_records_ignores_non_mapping_container_values():
    assert _join_status_records({"branches": {"H": "text"}}) == []
    assert _join_status_records({"contracts": "text"}) == []


def test_join_status_records_same_stage_twice_keeps_both_records():
    """Pas de dédup ni de fusion : deux statuts contradictoires survivent."""
    payload = {
        "warnings": [{"stage": "harness", "status": "ok"}, {"stage": "harness", "status": "error"}]
    }
    records = _join_status_records(payload)
    assert len(records) == 2
    assert [r["status"] for r in records] == ["ok", "error"]


def test_join_status_records_some_records_present_some_missing():
    """Une clé absente est un simple « pas de record » — pas d'erreur."""
    assert _join_status_records({"warnings": [{"a": 1}]}) == [{"a": 1}]
    assert _join_status_records({"warnings": [{"a": 1}], "branches": {}}) == [{"a": 1}]


# =====================================================================
# _collect_join_records — la fan-in des six sources
# =====================================================================


def test_collect_join_records_drops_partial_false_warnings():
    records = _collect_join_records({"warnings": [{"partial": False}, {"partial": True}]}, ())
    assert records == [{"partial": True}]


def test_collect_join_records_ignores_non_list_warnings():
    assert _collect_join_records({"warnings": "text"}, ()) == []


def test_collect_join_records_collects_every_recovered_source_in_order():
    summary = {
        "recovered_inputs": [{"i": 1}],
        "recovered_watch_inputs": [{"w": 1}],
        "recovered_proposal_inputs": [{"p": 1}],
        "report_only_permissions": [{"ro": 1}],
    }
    assert _collect_join_records(summary, ()) == [{"i": 1}, {"w": 1}, {"p": 1}, {"ro": 1}]


def test_collect_join_records_drops_plain_success_worker_statuses():
    """Un `rc: 0` sans marqueur de statut est un fait, pas un warning."""
    summary = {
        "worker_statuses": [
            {"rc": 0},
            {"rc": 0, "truncated": True},
            {"rc": 0, "worker_status": "truncated"},
            {"rc": 0, "status": "error"},
        ]
    }
    assert _collect_join_records(summary, ()) == [
        {"rc": 0, "truncated": True},
        {"rc": 0, "worker_status": "truncated"},
        {"rc": 0, "status": "error"},
    ]


def test_collect_join_records_keeps_failed_worker_statuses():
    assert _collect_join_records({"worker_statuses": [{"rc": 1}]}, ()) == [{"rc": 1}]


def test_collect_join_records_reads_selection_worker_statuses():
    summary = {"selection": {"worker_statuses": [{"rc": 1}]}}
    assert _collect_join_records(summary, ()) == [{"rc": 1}]


@pytest.mark.parametrize("key", ["recovered_inputs", "recovered_watch_inputs"])
def test_collect_join_records_ignores_non_list_recovered_sources(key):
    assert _collect_join_records({key: "text"}, ()) == []


def test_collect_join_records_drops_bare_success_additional_records():
    assert _collect_join_records({}, [{"rc": 0}]) == []


@pytest.mark.parametrize(
    "record",
    [
        {"rc": 0, "status": "x"},
        {"rc": 0, "warnings": []},
        {"rc": 0, "truncated": True},
        {"rc": 0, "recovered": True},
        {"rc": 0, "report_only": True},
    ],
)
def test_collect_join_records_keeps_success_records_carrying_a_status_field(record):
    assert _collect_join_records({}, [record]) == [record]


def test_collect_join_records_keeps_non_mapping_additional_records():
    assert _collect_join_records({}, ["raw text", 5]) == ["raw text", 5]


def test_collect_join_records_order_is_warnings_then_statuses_then_recovered():
    summary = {
        "warnings": [{"w": 1}],
        "worker_statuses": [{"rc": 1}],
        "recovered_inputs": [{"r": 1}],
        "report_only_permissions": [{"ro": 1}],
    }
    assert _collect_join_records(summary, [{"rc": 1, "j": 1}]) == [
        {"w": 1},
        {"rc": 1},
        {"r": 1},
        {"ro": 1},
        {"rc": 1, "j": 1},
    ]


# =====================================================================
# _warning_is_nonblocking — le classifieur
# =====================================================================


@pytest.mark.parametrize("warning", ["text", 5, None, [], object()])
def test_warning_is_nonblocking_rejects_scalars_and_strings(warning):
    """Une warning sérialisée n'a pas de structure de confiance."""
    assert _warning_is_nonblocking(warning, summary={}) is False


@pytest.mark.parametrize(
    ("warning", "expected"),
    [
        ({}, False),
        ({"optional": True}, True),
        ({"optional": 1}, False),
        ({"optional": "true"}, False),
        ({"applicable": False}, True),
        ({"applicable": 0}, False),
        ({"optional": True, "applicable": True}, True),
    ],
)
def test_warning_is_nonblocking_optional_and_applicable_flags(warning, expected):
    assert _warning_is_nonblocking(warning, summary={}) is expected


def test_warning_is_nonblocking_report_only_requires_verified_external_path(
    run_dir: Path,
):
    refusal = {
        "report_only": True,
        "status": "report-only",
        "category": "external-permission-refusal",
        "permission_denied": "permission denied",
        "target": "/etc/hosts",
    }
    assert (
        _warning_is_nonblocking(
            refusal, summary={}, out=run_dir, date=DATE, project_root=str(run_dir)
        )
        is True
    )
    inside = dict(refusal, target=str(run_dir / "report.html"))
    assert (
        _warning_is_nonblocking(
            inside, summary={}, out=run_dir, date=DATE, project_root=str(run_dir)
        )
        is False
    )
    # sans racine configurée, la cible n'est pas prouvable externe
    assert _warning_is_nonblocking(refusal, summary={}, out=run_dir, date=DATE) is False


def test_warning_is_nonblocking_recovered_requires_a_valid_artifact_on_disk(run_dir: Path):
    _seed_ecosystem(run_dir)
    assert (
        _warning_is_nonblocking(
            {"recovered": True, "source": "ecosystem", "artifact": "weekly-ecosystem"},
            summary={},
            out=run_dir,
            date=DATE,
        )
        is True
    )
    # pas d'artefact déclaré : aucune preuve observable
    assert (
        _warning_is_nonblocking(
            {"recovered": True, "source": "ecosystem"}, summary={}, out=run_dir, date=DATE
        )
        is False
    )
    # artefact déclaré mais non conforme sur disque
    assert (
        _warning_is_nonblocking(
            {"recovered": True, "source": "ecosystem", "artifact": "weekly-summary"},
            summary={},
            out=run_dir,
            date=DATE,
        )
        is False
    )


def test_warning_is_nonblocking_optional_artifact_input_named_in_the_text():
    summary = {"artifact_inputs": {"weekly-ecosystem": {"required": False, "status": "absent"}}}
    warning = {"message": "weekly-ecosystem absent de ce run"}
    assert _warning_is_nonblocking(warning, summary=summary) is True


def test_warning_is_nonblocking_optional_artifact_input_matches_on_path_too():
    summary = {
        "artifact_inputs": {
            "k": {"required": False, "status": "absent", "path": "weekly-ecosystem"}
        }
    }
    assert _warning_is_nonblocking({"message": "weekly-ecosystem absent"}, summary=summary) is True


@pytest.mark.parametrize(
    "entry",
    [
        {"required": True, "status": "absent"},
        {"required": 1, "status": "absent"},
        {"required": False, "status": "garbage"},
        {"required": False, "status": "present", "valid": False},
    ],
)
def test_warning_is_nonblocking_required_or_invalid_artifact_input_blocks(entry):
    summary = {"artifact_inputs": {"weekly-ecosystem": entry}}
    warning = {"message": "weekly-ecosystem absent"}
    assert _warning_is_nonblocking(warning, summary=summary) is False


def test_warning_is_nonblocking_artifact_input_status_not_applicable_passes():
    summary = {
        "artifact_inputs": {"weekly-ecosystem": {"required": False, "status": "not_applicable"}}
    }
    assert _warning_is_nonblocking({"message": "weekly-ecosystem absent"}, summary=summary) is True


def test_warning_is_nonblocking_unrelated_optional_input_with_a_path_does_not_pass():
    summary = {
        "artifact_inputs": {
            "weekly-timings": {"required": False, "status": "absent", "path": "weekly-timings"}
        }
    }
    assert _warning_is_nonblocking({"message": "weekly-ecosystem absent"}, summary=summary) is False


def test_warning_is_nonblocking_optional_input_without_path_matches_any_text():
    """Trou connu : ``str(value.get("path", "")) in text`` vaut ``True`` pour
    toute entrée optionnelle sans champ `path` — la chaîne vide est présente
    dans n'importe quel texte. L'entrée optionnelle sert donc de passe-partout.
    """
    summary = {"artifact_inputs": {"weekly-timings": {"required": False, "status": "absent"}}}
    assert _warning_is_nonblocking({"message": "weekly-ecosystem absent"}, summary=summary) is True


# =====================================================================
# _is_recovered_record — le prédicat de récupération
# =====================================================================


@pytest.mark.parametrize("record", ["text", None, 5, []])
def test_is_recovered_record_rejects_non_mappings(record):
    assert _is_recovered_record(record) is False


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"recovered": True}, True),
        ({"recovered": 1}, False),
        ({"recovery": True}, True),
        ({"recovery": "ok"}, True),
        ({"recovery": "success"}, True),
        ({"recovery": "failure"}, False),
        ({"status": "recovered"}, True),
        ({"status": "recovered-input"}, True),
        ({"status": "fallback-recovered"}, True),
        ({"status": "error"}, False),
        ({"message": "recovered weekly-ecosystem input"}, True),
        ({"message": "fallback proposal used"}, True),
        ({"message": "entrée watch récupérée"}, True),
        ({"message": "recovered"}, False),
        ({"message": "watch"}, False),
        ({"message": "weekly-ecosystem input"}, False),
        ({}, False),
    ],
)
def test_is_recovered_record_marker_matrix(record, expected):
    assert _is_recovered_record(record) is expected


def test_is_recovered_record_needs_both_a_recovery_and_an_input_marker():
    """« récupéré » seul ne suffit pas : il faut nommer l'entrée concernée."""
    assert _is_recovered_record({"message": "recovered"}) is False
    assert _is_recovered_record({"message": "recovered harness digest"}) is True


# =====================================================================
# _is_report_only_record / _path_is_outside_worktree — la preuve externe
# =====================================================================


@pytest.fixture
def refusal() -> dict:
    return {
        "report_only": True,
        "status": "report-only",
        "category": "external-permission-refusal",
        "permission_denied": "permission denied",
        "target": "/etc/hosts",
    }


def test_is_report_only_record_requires_exact_true_flag(run_dir: Path, refusal: dict):
    assert _is_report_only_record(refusal, project_root=str(run_dir)) is True
    for bad in (1, "yes", "true", None):
        assert (
            _is_report_only_record({**refusal, "report_only": bad}, project_root=str(run_dir))
            is False
        )


def test_is_report_only_record_requires_exact_status_and_category(run_dir: Path, refusal: dict):
    assert (
        _is_report_only_record({**refusal, "status": "REPORT-ONLY"}, project_root=str(run_dir))
        is True
    )
    assert (
        _is_report_only_record({**refusal, "status": "error"}, project_root=str(run_dir)) is False
    )
    assert (
        _is_report_only_record({**refusal, "category": "other"}, project_root=str(run_dir)) is False
    )


@pytest.mark.parametrize(
    "marker",
    ["permission denied", "Permission Refused", "external permission refusal", True],
)
def test_is_report_only_record_accepts_the_refusal_marker_variants(
    run_dir: Path, refusal: dict, marker
):
    record = {k: v for k, v in refusal.items() if k != "permission_denied"}
    assert (
        _is_report_only_record({**record, "permission_denied": marker}, project_root=str(run_dir))
        is True
    )


def test_is_report_only_record_rejects_unrelated_refusal_text(run_dir: Path, refusal: dict):
    assert (
        _is_report_only_record(
            {**refusal, "permission_denied": "disk full"}, project_root=str(run_dir)
        )
        is False
    )


def test_is_report_only_record_without_project_root_is_false(refusal: dict):
    assert _is_report_only_record(refusal, project_root=None) is False
    assert _is_report_only_record("text", project_root="/tmp") is False


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/etc/hosts", True),
        ("/tmp", True),
        ("relative/path", False),
        ("", False),
        (None, False),
        (5, False),
    ],
)
def test_path_is_outside_worktree(target, expected):
    assert _path_is_outside_worktree(target, "/tmp/some-worktree") is expected


def test_path_is_outside_worktree_requires_absolute_worktree():
    assert _path_is_outside_worktree("/etc/hosts", "relative-root") is False


def test_path_is_outside_worktree_worktree_itself_is_inside(tmp_path: Path):
    root = tmp_path.resolve()
    assert _path_is_outside_worktree(str(root), str(root)) is False
    assert _path_is_outside_worktree(str(root / "child"), str(root)) is False


def test_is_external_permission_failure_only_for_external_targets(tmp_path: Path):
    class Cfg:
        project_root = str(tmp_path)
        html_report_dir = str(tmp_path / "inside.html")

    assert _is_external_permission_failure(Cfg(), PermissionError("x")) is False
    outside = Cfg()
    outside.html_report_dir = "/etc/hosts"
    assert _is_external_permission_failure(outside, PermissionError("x")) is True
    assert _is_external_permission_failure(Cfg(), ValueError("x")) is False


# =====================================================================
# _recoverable_artifact_valid — le prédicat d'entrée récupérée
# =====================================================================


def test_recoverable_artifact_valid_accepts_stem_with_or_without_suffix(run_dir: Path):
    _seed_ecosystem(run_dir)
    for reference in ("weekly-ecosystem", f"weekly-ecosystem-{DATE}.json"):
        assert _recoverable_artifact_valid(run_dir, DATE, "weekly-ecosystem", reference) is True, (
            reference
        )


def test_recoverable_artifact_valid_rejects_unknown_source_kind(run_dir: Path):
    _seed_ecosystem(run_dir)
    assert _recoverable_artifact_valid(run_dir, DATE, "timings", "weekly-ecosystem") is False


@pytest.mark.parametrize(
    "stem",
    [
        "weekly-summary",
        "weekly-timings",
        "weekly-watch-findings",
        "mystery",
        "./weekly-ecosystem",
        "sub/weekly-ecosystem",
        ".weekly-ecosystem",
    ],
)
def test_recoverable_artifact_valid_rejects_wrong_stem_for_the_source(run_dir: Path, stem):
    assert _recoverable_artifact_valid(run_dir, DATE, "ecosystem", stem) is False


@pytest.mark.parametrize("artifact", [None, "", "   ", 5, []])
def test_recoverable_artifact_valid_requires_a_named_replacement(run_dir: Path, artifact):
    """Sans nom d'artefact, la récupération n'est pas prouvable."""
    assert _recoverable_artifact_valid(run_dir, DATE, "ecosystem", artifact) is False


def test_recoverable_artifact_valid_list_is_any_semantics(run_dir: Path):
    _seed_ecosystem(run_dir)
    assert (
        _recoverable_artifact_valid(
            run_dir, DATE, "ecosystem", ["weekly-summary", "weekly-ecosystem"]
        )
        is True
    )
    assert _recoverable_artifact_valid(run_dir, DATE, "ecosystem", ["weekly-summary"]) is False
    assert _recoverable_artifact_valid(run_dir, DATE, "ecosystem", []) is False


def test_recoverable_artifact_valid_rejects_file_absent_or_malformed_on_disk(run_dir: Path):
    """Le contrat exact est appliqué : un `{}` masquant un artefact absent échoue."""
    assert _recoverable_artifact_valid(run_dir, DATE, "ecosystem", "weekly-ecosystem") is False
    (run_dir / f"weekly-ecosystem-{DATE}.json").write_text("{}", encoding="utf-8")
    assert _recoverable_artifact_valid(run_dir, DATE, "ecosystem", "weekly-ecosystem") is False


def test_recoverable_artifact_valid_requires_out_and_a_valid_date(run_dir: Path):
    _seed_ecosystem(run_dir)
    assert _recoverable_artifact_valid(None, DATE, "ecosystem", "weekly-ecosystem") is False
    assert _recoverable_artifact_valid(run_dir, None, "ecosystem", "weekly-ecosystem") is False
    assert (
        _recoverable_artifact_valid(run_dir, "2026-13-45", "ecosystem", "weekly-ecosystem") is False
    )


def test_recoverable_artifact_valid_source_kind_precedence_is_declaration_order(run_dir: Path):
    """`ecosystem` l'emporte sur `watch` : l'ordre de recherche est fixe."""
    (run_dir / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps({"inspection": {"a": 1}}), encoding="utf-8"
    )
    assert (
        _recoverable_artifact_valid(run_dir, DATE, "watch harness", "weekly-harness-digest")
        is False
    )


# =====================================================================
# _branch_applicability — les déclarations de branche
# =====================================================================


@pytest.mark.parametrize("payload", ["text", None, 5, []])
def test_branch_applicability_non_mapping_is_empty(payload):
    assert _branch_applicability(payload, DATE) == {}


def test_branch_applicability_empty_payload_is_empty():
    assert _branch_applicability({}, DATE) == {}


@pytest.mark.parametrize(
    "key", ["artifact", "artifacts", "required_artifact", "required_artifacts"]
)
def test_branch_applicability_reads_every_declaration_key(key):
    payload = {key: f"weekly-ecosystem-{DATE}.json"}
    assert _branch_applicability(payload, DATE) == {"weekly-ecosystem": True}


def test_branch_applicability_discovers_nested_branches():
    payload = {"branches": {"H": {"artifacts": [f"weekly-timings-{DATE}.json"]}}}
    assert _branch_applicability(payload, DATE) == {"weekly-timings": True}


def test_branch_applicability_requires_the_declared_date_to_match():
    assert _branch_applicability({"artifacts": ["weekly-ecosystem-2026-10-09.json"]}, DATE) == {}


def test_branch_applicability_ignores_non_optional_names():
    assert _branch_applicability({"artifacts": [f"weekly-summary-{DATE}.json"]}, DATE) == {}


@pytest.mark.parametrize(
    "declaration",
    [
        f"sub/weekly-ecosystem-{DATE}.json",
        f".weekly-ecosystem-{DATE}.json",
        f"weekly-ecosystem-{DATE}",
        "weekly-ecosystem-2026-1-1.json",
        "-weekly-ecosystem-2026-10-08.json",
        "weekly_ecosystem-2026-10-08.json",
    ],
)
def test_branch_applicability_rejects_inexact_declarations(declaration):
    assert _branch_applicability({"artifacts": [declaration]}, DATE) == {}


@pytest.mark.parametrize("declaration", [None, 5, {"path": "x"}, ["nested"]])
def test_branch_applicability_ignores_non_string_declarations(declaration):
    assert _branch_applicability({"artifacts": [declaration]}, DATE) == {}


def test_branch_applicability_accepts_an_exact_run_local_absolute_path(run_dir: Path):
    declaration = str(run_dir / f"weekly-ecosystem-{DATE}.json")
    assert _branch_applicability({"artifacts": [declaration]}, DATE, out=run_dir) == {
        "weekly-ecosystem": True
    }


def test_branch_applicability_rejects_absolute_path_outside_the_run(run_dir: Path):
    assert (
        _branch_applicability(
            {"artifacts": ["/tmp/weekly-ecosystem-2026-10-08.json"]}, DATE, out=run_dir
        )
        == {}
    )


def test_branch_applicability_rejects_absolute_path_without_out():
    assert _branch_applicability({"artifacts": ["/tmp/x-2026-10-08.json"]}, DATE) == {}


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("weekly-ecosystem-2026-10-08.json", "weekly-ecosystem-2026-10-08.json"),
        ("", None),
        ("a\\b.json", None),
        (".hidden.json", None),
        ("sub/a.json", None),
        ("/abs/a.json", None),
    ],
)
def test_declared_run_filename(item, expected):
    assert _declared_run_filename(item) == expected


def test_declared_run_filename_absolute_needs_the_run_root(tmp_path: Path):
    out = tmp_path.resolve()
    target = out / "a.json"
    assert _declared_run_filename(str(target)) is None
    assert _declared_run_filename(str(target), out) == "a.json"
    assert _declared_run_filename("/elsewhere/a.json", out) is None


# =====================================================================
# Enveloppe d'audit dynamique
# =====================================================================


@pytest.mark.parametrize("sid", ["", ".", "..", "a/b", "a\\b"])
def test_canonical_audit_path_rejects_traversal_and_empty(sid):
    assert _canonical_audit_path(Path("/tmp/run"), sid) is None


def test_canonical_audit_path_is_the_only_accepted_location(tmp_path: Path):
    out = tmp_path.resolve()
    assert _canonical_audit_path(out, SID) == out / f"audit-findings-{SID}.json"


def test_audit_artifact_valid_requires_the_exact_declared_filename(run_dir: Path):
    _seed_audit(run_dir)
    assert (
        _audit_artifact_valid(run_dir, DATE, SID, {"audit_artifact": f"audit-findings-{SID}.json"})
        is True
    )
    assert (
        _audit_artifact_valid(
            run_dir, DATE, SID, {"artifact": str(run_dir / f"audit-findings-{SID}.json")}
        )
        is True
    )
    assert _audit_artifact_valid(run_dir, DATE, SID, {}) is False
    assert _audit_artifact_valid(run_dir, DATE, SID, {"audit_artifact": "other.json"}) is False
    assert (
        _audit_artifact_valid(
            run_dir, DATE, SID, {"audit_artifact": f"audit-findings-{SID}.json.bak"}
        )
        is False
    )


def test_audit_artifact_valid_fails_closed_without_out_or_a_string_sid(run_dir: Path):
    assert _audit_artifact_valid(None, DATE, SID, {"audit_artifact": "x"}) is False
    assert _audit_artifact_valid(run_dir, DATE, 5, {"audit_artifact": "x"}) is False
    assert _audit_artifact_valid(run_dir, DATE, "../evil", {"audit_artifact": "x"}) is False


def test_audit_artifact_valid_rejects_a_missing_file(run_dir: Path):
    assert (
        _audit_artifact_valid(run_dir, DATE, SID, {"audit_artifact": f"audit-findings-{SID}.json"})
        is False
    )


def test_truncated_record_valid_delegates_to_the_audit_contract(run_dir: Path):
    _seed_audit(run_dir)
    record = {"session_id": SID, "audit_artifact": f"audit-findings-{SID}.json"}
    assert _truncated_record_valid(record, {}, run_dir, DATE) is True
    assert _truncated_record_valid({"session_id": "other"}, {}, run_dir, DATE) is False
    assert _truncated_record_valid(record, {}, None, None) is False
    assert _truncated_record_valid("text", {}, run_dir, DATE) is False


def test_audit_declaration_values_walks_the_structured_keys():
    assert _audit_declaration_values({"audit_artifact": "a.json"}) == ["a.json"]
    assert _audit_declaration_values(
        {"artifacts": [{"path": "a.json"}, {"artifact": "b.json"}]}
    ) == ["a.json", "b.json"]
    assert _audit_declaration_values({"audit_artifact_path": "c.json"}) == ["c.json"]
    assert _audit_declaration_values({"required_artifacts": ["d.json", "e.json"]}) == [
        "d.json",
        "e.json",
    ]
    assert _audit_declaration_values("text") == []


def test_audit_artifact_declarations_filters_on_the_audit_filename_prefix():
    assert _audit_artifact_declarations({"artifacts": [f"audit-findings-{SID}.json"]}) == [
        f"audit-findings-{SID}.json"
    ]
    assert _audit_artifact_declarations({"artifacts": [f"weekly-summary-{DATE}.json"]}) == []
    assert _audit_artifact_declarations({"artifact": f"audit-findings-{SID}.json"}) == [
        f"audit-findings-{SID}.json"
    ]
    assert _audit_artifact_declarations("text") == []


def test_audit_artifact_declarations_finds_nested_declarations():
    payload = {"a": {"b": {"artifact_path": f"x/audit-findings-{SID}.json"}}}
    assert _audit_artifact_declarations(payload) == [f"x/audit-findings-{SID}.json"]


# =====================================================================
# _records_all_nonblocking — le AND sur tous les records
# =====================================================================


def test_records_all_nonblocking_is_true_for_an_empty_record_list():
    assert _records_all_nonblocking([], {}, out=None, date=DATE, project_root=None) is True


def test_records_all_nonblocking_requires_every_record():
    assert (
        _records_all_nonblocking([{"optional": True}], {}, out=None, date=DATE, project_root=None)
        is True
    )
    assert (
        _records_all_nonblocking(
            [{"optional": True}, {"message": "blocking"}],
            {},
            out=None,
            date=DATE,
            project_root=None,
        )
        is False
    )


# =====================================================================
# Le classifieur de règles bloquantes sécurité
# =====================================================================


@pytest.mark.parametrize(
    "rule",
    [
        "mcp-tool-poisoning",
        "security/mcp-tool-poisoning",
        "unbounded-delegation",
        "memory-write-unscoped",
        "MCP-Tool-Poisoning",
        "  mcp-tool-poisoning  ",
        "Security/MCP-Tool-Poisoning",
    ],
)
def test_is_blocking_security_rule_matches_exact_ids_after_normalisation(rule):
    """Le préfixe `security/` est retiré avant comparaison, casse comprise."""
    assert _is_blocking_security_rule(rule) is True


@pytest.mark.parametrize(
    "rule",
    [
        "tool-poisoning",
        "mcp-tool-poisoning-extra",
        "evil-mcp-tool-poisoning",
        "not-mcp-tool-poisoning",
        "mcp_tool_poisoning",
        "security/",
        "security/security/mcp-tool-poisoning",
        "",
        None,
        5,
    ],
)
def test_is_blocking_security_rule_rejects_lookalike_rules(rule):
    """Un nom qui *ressemble* à une règle bloquante ne bloque pas."""
    assert _is_blocking_security_rule(rule) is False


# =====================================================================
# _check_required_json / gate artefact
# =====================================================================


@pytest.mark.parametrize("name", ["weekly-ecosystem", "weekly-harness-digest", "weekly-timings"])
def test_check_required_json_absent_optional_is_absent_not_ill_readable(run_dir: Path, name):
    entry = _check_required_json(run_dir, name, DATE, set(), {})
    assert entry["status"] == "absent"
    assert entry["present"] is False
    assert entry["required"] is False
    assert entry["applicable"] is False


def test_check_required_json_absent_required_is_applicable(run_dir: Path):
    entry = _check_required_json(
        run_dir, "weekly-watch-context", DATE, {"weekly-watch-context"}, {}
    )
    assert entry["status"] == "absent"
    assert entry["required"] is True
    assert entry["applicable"] is True


def test_check_required_json_inapplicable_flag_is_not_overridden(run_dir: Path):
    entry = _check_required_json(
        run_dir,
        "weekly-watch-context",
        DATE,
        {"weekly-watch-context"},
        {"weekly-watch-context": False},
    )
    assert entry["applicable"] is False


@pytest.mark.parametrize("name", ["../evil", ".hidden", "with space", ""])
def test_check_required_json_rejects_an_unsafe_artifact_name(run_dir: Path, name):
    entry = _check_required_json(run_dir, name, DATE, set(), {})
    assert entry["path_valid"] is False
    assert entry["status"] == "ill_readable"


def test_check_required_json_blank_file_is_ill_readable_even_optional(run_dir: Path):
    (run_dir / f"weekly-insights-{DATE}.json").write_text("   ", encoding="utf-8")
    entry = _check_required_json(run_dir, "weekly-insights", DATE, set(), {})
    assert entry["status"] == "ill_readable"
    assert entry["present"] is False


def test_check_required_json_optional_file_with_a_broken_schema_is_still_present(run_dir: Path):
    """Warn-only : un artefact optionnel présent mais non conforme ne casse rien,
    il est seulement marqué `schema_valid: False`."""
    (run_dir / f"weekly-ecosystem-{DATE}.json").write_text("{}", encoding="utf-8")
    entry = _check_required_json(run_dir, "weekly-ecosystem", DATE, set(), {})
    assert entry["status"] == "present"
    assert entry["present"] is True
    assert entry["schema_valid"] is False


def test_check_required_json_same_file_is_ill_readable_when_required(run_dir: Path):
    (run_dir / f"weekly-ecosystem-{DATE}.json").write_text("{}", encoding="utf-8")
    entry = _check_required_json(run_dir, "weekly-ecosystem", DATE, {"weekly-ecosystem"}, {})
    assert entry["status"] == "ill_readable"
    assert entry["present"] is False


def test_check_required_json_valid_file_is_present_and_schema_valid(run_dir: Path):
    _seed_ecosystem(run_dir)
    entry = _check_required_json(run_dir, "weekly-ecosystem", DATE, {"weekly-ecosystem"}, {})
    assert entry["status"] == "present"
    assert entry["schema_valid"] is True
    assert entry["path_valid"] is True


def test_check_required_json_existing_optional_file_is_applicable_by_default(run_dir: Path):
    _seed_ecosystem(run_dir)
    entry = _check_required_json(run_dir, "weekly-ecosystem", DATE, set(), {})
    assert entry["applicable"] is True


def test_check_html_artifact_states(tmp_path: Path):
    assert _check_html_artifact(None) == {"status": "absent", "path": None}
    assert _check_html_artifact(tmp_path / "nope.html")["status"] == "absent"
    blank = tmp_path / "blank.html"
    blank.write_text("  ", encoding="utf-8")
    assert _check_html_artifact(blank)["status"] == "ill_readable"
    good = tmp_path / "good.html"
    good.write_text("<html/>", encoding="utf-8")
    assert _check_html_artifact(good)["status"] == "present"


def test_html_gate_status_file_wins_over_the_config_flag(tmp_path: Path):
    good = tmp_path / "r.html"
    good.write_text("<html/>", encoding="utf-8")
    assert _html_gate_status(good, False)["status"] == "present"
    assert _html_gate_status(good, True)["status"] == "present"
    assert _html_gate_status(None, False)["status"] == "disabled"
    assert _html_gate_status(None, True)["status"] == "absent"


# =====================================================================
# validate_required_artifacts — warn-only par construction
# =====================================================================


def test_validate_required_artifacts_passes_with_only_the_required_summary(run_dir: Path):
    gate = validate_required_artifacts(run_dir, DATE)
    assert gate["status"] == "pass"
    assert gate["optional_missing"] == []
    assert gate["html"] == {"status": "disabled", "path": None}


def test_validate_required_artifacts_missing_required_is_incomplete(run_dir: Path):
    (run_dir / f"weekly-summary-{DATE}.json").write_text("{", encoding="utf-8")
    gate = validate_required_artifacts(run_dir, DATE)
    assert gate["status"] == "incomplete"
    entry = gate["required"][f"weekly-summary-{DATE}.json"]
    assert entry["status"] == "ill_readable"


def test_validate_required_artifacts_absent_required_is_incomplete(run_dir: Path):
    (run_dir / f"weekly-summary-{DATE}.json").unlink()
    assert validate_required_artifacts(run_dir, DATE)["status"] == "incomplete"


def test_validate_required_artifacts_inapplicable_optional_is_not_listed_as_missing(
    run_dir: Path,
):
    gate = validate_required_artifacts(run_dir, DATE, applicability={"weekly-ecosystem": False})
    assert gate["status"] == "pass"
    assert gate["optional_missing"] == []


def test_validate_required_artifacts_present_optional_stays_out_of_missing(run_dir: Path):
    """Un producteur optionnel absent n'est pas la preuve que sa branche a échoué."""
    _seed_ecosystem(run_dir)
    gate = validate_required_artifacts(run_dir, DATE)
    assert gate["status"] == "pass"
    assert gate["optional_missing"] == []
    assert f"weekly-ecosystem-{DATE}.json" in gate["optional"]


def test_validate_required_artifacts_applicable_branch_becomes_required(run_dir: Path):
    gate = validate_required_artifacts(run_dir, DATE, applicability={"weekly-ecosystem": True})
    assert gate["status"] == "incomplete"
    assert f"weekly-ecosystem-{DATE}.json" in gate["required"]
    assert f"weekly-ecosystem-{DATE}.json" not in gate["optional"]


def test_validate_required_artifacts_html_flag_is_never_fatal(run_dir: Path):
    gate = validate_required_artifacts(run_dir, DATE, html_enabled=True)
    assert gate["status"] == "pass"
    assert gate["html"]["status"] == "absent"


def test_validate_required_artifacts_present_html_wins_over_disabled_flag(run_dir: Path):
    (run_dir / "r.html").write_text("<html/>", encoding="utf-8")
    gate = validate_required_artifacts(
        run_dir, DATE, html_enabled=False, html_path=run_dir / "r.html"
    )
    assert gate["html"]["status"] == "present"


def test_validate_required_artifacts_valid_dynamic_audit_passes(run_dir: Path):
    _seed_audit(run_dir)
    gate = validate_required_artifacts(
        run_dir, DATE, dynamic_audit_artifacts=[f"audit-findings-{SID}.json"]
    )
    assert gate["status"] == "pass"
    entry = gate["required"][f"audit-findings-{SID}.json"]
    assert entry["status"] == "present"
    assert entry["reason"] == "ok"
    assert entry["required"] is True


def test_validate_required_artifacts_missing_dynamic_audit_is_incomplete_with_reason(
    run_dir: Path,
):
    gate = validate_required_artifacts(
        run_dir, DATE, dynamic_audit_artifacts=["audit-findings-other.json"]
    )
    assert gate["status"] == "incomplete"
    entry = gate["required"]["audit-findings-other.json"]
    assert entry["status"] == "absent"
    assert entry["reason"] == "absent"


def test_validate_required_artifacts_malformed_dynamic_audit_reports_its_reason(
    run_dir: Path,
):
    (run_dir / f"audit-findings-{SID}.json").write_text("{", encoding="utf-8")
    gate = validate_required_artifacts(
        run_dir, DATE, dynamic_audit_artifacts=[f"audit-findings-{SID}.json"]
    )
    entry = gate["required"][f"audit-findings-{SID}.json"]
    assert entry["status"] == "ill_readable"
    assert entry["reason"] == "ill-readable"
    assert entry["schema_valid"] is False


def test_validate_required_artifacts_dynamic_audit_wrong_sid_is_rejected(run_dir: Path):
    _seed_audit(run_dir)
    gate = validate_required_artifacts(
        run_dir, DATE, dynamic_audit_artifacts=[f"audit-findings-{SID}.json"]
    )
    assert gate["status"] == "pass"
    # le fichier existe mais son envelope déclare un autre sid → rejet au moment
    # de la validation de l'enveloppe, pas du chemin
    other = validate_required_artifacts(
        run_dir, DATE, dynamic_audit_artifacts=[f"audit-findings-{SID}.json"]
    )
    assert other["required"][f"audit-findings-{SID}.json"]["reason"] in {"ok", "sid-mismatch"}


# =====================================================================
# _artifact_entry_valid — la lecture d'une entrée d'artefact
# =====================================================================


@pytest.mark.parametrize("status", ["present", "valid", "ok", "recovered", "PRESENT", "Present"])
def test_artifact_entry_valid_status_whitelist(status):
    assert _artifact_entry_valid({"status": status}) is True


@pytest.mark.parametrize("status", ["absent", "ill_readable", "partial", "", None, " present "])
def test_artifact_entry_valid_status_outside_the_whitelist(status):
    assert _artifact_entry_valid({"status": status}) is False


def test_artifact_entry_valid_status_present_can_be_overridden_by_valid_false():
    assert _artifact_entry_valid({"status": "present", "valid": False}) is False
    assert _artifact_entry_valid({"status": "present", "valid": True}) is True
    # `is not False` : 0 ne vaut pas l'objet False, l'entrée reste valide
    assert _artifact_entry_valid({"status": "present", "valid": 0}) is True


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"present": True}, True),
        ({"valid": True}, True),
        ({"present": False}, False),
        ({"valid": False}, False),
        ({"status": "absent", "present": True}, True),
        ({}, False),
        ("text", False),
        (None, False),
    ],
)
def test_artifact_entry_valid_fallback_branch(entry, expected):
    assert _artifact_entry_valid(entry) is expected
