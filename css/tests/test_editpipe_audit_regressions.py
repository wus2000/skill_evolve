"""Regression tests for the adversarial-audit findings (F1-F5, F8, F11, F12).

Each test encodes the exact failing scenario from the audit so the fix
cannot silently regress.
"""
from __future__ import annotations

import json
import os

import pytest

from css.model.client import StubLLMClient
from css.optimizer.editpipe.adjudicate import adjudicate
from css.optimizer.editpipe.apply import apply_edits
from css.optimizer.editpipe.pipeline import apply_merged
from css.optimizer.editpipe.render import RulesDoc
from css.optimizer.editpipe.schema import SectionEdit, syntax_gate

BASE = ("### Foo\n- original rule one\n- original rule two\n"
        "- original rule three\n\n### Bar\n- bar rule\n")


def _edit(**kw) -> SectionEdit:
    base = dict(kind="add_section", subject="New", body="- r",
                target_tasks=["t"])
    base.update(kw)
    return SectionEdit.from_dict(base)


def _valid():
    return json.dumps({"valid": True, "violations": []})


# F1 — unresolved remove_section+point conflict must not delete content ------

def test_f1_unresolved_removal_conflict_suppresses_removal():
    """remove_section(Foo) + edit_point(Foo) never resolved by the LLM:
    the fallback suppresses the removal (audited); Foo's content survives."""

    def opt(system, user):
        if "edit validator" in system:
            return _valid()
        return json.dumps({"reasoning": "no ops", "operations": []})

    client = StubLLMClient(optimizer_fn=opt)
    edits = [
        _edit(kind="remove_section", subject="Foo", body="",
              target_tasks=["a"]),
        _edit(kind="edit_point", subject="Foo",
              body="- original rule two IMPROVED",
              anchor="- original rule two", target_tasks=["b"]),
    ]
    res = adjudicate(client, BASE, edits, max_rounds=2, hard_cap_rounds=2)
    assert not res.converged
    kinds = [e.kind for e in res.edits]
    assert "remove_section" not in kinds          # suppressed
    assert any(a.fate == "dropped" and "suppressed" in a.reason
               and a.actor == "fallback" for a in res.audits)

    applied = apply_edits(BASE, res.edits)
    assert "- original rule one" in applied.text   # section intact
    assert "- original rule two IMPROVED" in applied.text
    assert applied.assertion_failures == []


def test_f1_apply_order_removal_is_last_word():
    """Even if a removal+point pair reaches apply together, the outcome is a
    clean removal — never a deleted section resurrected as a delta shell."""
    edits = [
        _edit(kind="remove_section", subject="Foo", body="",
              target_tasks=["a"]),
        _edit(kind="add_point", subject="Foo", body="- late point",
              anchor="- original rule one", target_tasks=["b"]),
    ]
    res = apply_edits(BASE, edits)
    doc = RulesDoc.parse(res.text)
    assert doc.find("Foo") is None                # removal won cleanly
    assert "- late point" not in res.text         # no resurrected shell
    assert res.assertion_failures == []


# F2 — apply audits are persisted -------------------------------------------

def test_f2_apply_merged_persists_apply_audits(tmp_path):
    def opt(system, user):
        return json.dumps({"body": ""})  # resolver garbage -> degradation

    client = StubLLMClient(optimizer_fn=opt)
    me = _edit(kind="add_point", subject="Foo", body="- appended.",
               anchor="NO SUCH ANCHOR").to_merged()
    audit_path = str(tmp_path / "apply_audit.json")
    text = apply_merged(client, BASE, [me], audit_path=audit_path)
    assert "- appended." in text
    payload = json.load(open(audit_path))
    assert any(a["fate"] == "degraded" for a in payload["apply_audits"])
    assert "assertion_failures" in payload and "gate_audits" in payload


# F3 — resolver partial section loss is rejected ----------------------------

def test_f3_resolver_partial_loss_rejected():
    """edit_point resolver output keeping the edit body but silently
    dropping unrelated lines must fail the contract and degrade."""
    base = ("### S\n- l1\n- l2\n- l3\n- l4\n- l5\n- l6\n- l7\n- l8\n"
            "- l9\n- l10\n")

    def resolver(subject, body, edit, feedback=""):
        # keeps only half the lines + the edit body
        return "- l1\n- l2\n- l3\n- l4\n- l5\n" + edit.body

    edits = [_edit(kind="edit_point", subject="S", body="- IMPROVED",
                   anchor="NOT FOUND VERBATIM")]
    res = apply_edits(base, edits, resolver=resolver)
    # rejected -> content-preserving append fallback: all 10 lines survive
    for i in range(1, 11):
        assert f"- l{i}" in res.text
    assert "- IMPROVED" in res.text
    assert any("twice" in a.reason for a in res.audits)


# F4 — repair payloads inherit provenance -----------------------------------

def test_f4_repair_merge_inherits_target_tasks_union():
    state = {"n": 0}

    def opt(system, user):
        if "edit validator" in system:
            return _valid()
        state["n"] += 1
        return json.dumps({
            "reasoning": "merge the twins",
            "operations": [{
                "op": "merge", "ids": ["E#0", "E#1"],
                "edit": {"kind": "add_section", "subject": "Merged Topic",
                         "body": "- merged rule"},  # target_tasks OMITTED
            }],
        })

    client = StubLLMClient(optimizer_fn=opt)
    edits = [
        _edit(subject="Same Topic", body="- one", target_tasks=["a", "b"]),
        _edit(subject="Same Topic", body="- two", target_tasks=["b", "c"]),
    ]
    res = adjudicate(client, BASE, edits)
    assert res.converged
    merged = [e for e in res.edits if e.subject == "Merged Topic"]
    assert len(merged) == 1
    assert merged[0].target_tasks == ["a", "b", "c"]   # union inherited


# F5 — final-round semantic violations reach the fallback audited -----------

def test_f5_final_round_purity_is_purged_or_audited():
    state = {"validator": 0}

    def opt(system, user):
        if "edit validator" in system:
            state["validator"] += 1
            return json.dumps({"valid": False, "violations": [{
                "type": "content_purity", "edit_indices": [0],
                "detail": "body cites training task ids"}]})
        if "clean ONE rules.md edit body" in system:
            return json.dumps({"body": "- clean domain rule"})
        return json.dumps({"reasoning": "no ops", "operations": []})

    client = StubLLMClient(optimizer_fn=opt)
    edits = [_edit(subject="Topic", body="- rule (from tasks 42, 77)")]
    res = adjudicate(client, BASE, edits, max_rounds=2, hard_cap_rounds=2)
    # The focused purge resolved it before delivery:
    assert res.edits[0].body == "- clean domain rule"
    assert any("purge applied" in a.reason for a in res.audits)
    assert not [v for v in res.accepted_risks
                if v.vtype == "content_purity"]


# F8 — byte-dup unions provenance -------------------------------------------

def test_f8_byte_dup_unions_target_tasks():
    doc = RulesDoc.parse(BASE)
    e1 = _edit(subject="Topic A", body="- same", target_tasks=["t1"])
    e2 = _edit(subject="Topic A", body="- same", target_tasks=["t2", "t1"])
    kept, violations, audits = syntax_gate([e1, e2], doc)
    assert len(kept) == 1
    assert kept[0].target_tasks == ["t1", "t2"]        # union, not loss
    assert any("unioned 1 target_task" in a.reason for a in audits)


# F12 — mid-line anchor insertion does not split the line --------------------

def test_f12_midline_anchor_insertion_extends_to_line_end():
    base = "### S\n- use pd.read_csv for loading data\n- other\n"
    edits = [_edit(kind="add_point", subject="S", body="- inserted",
                   anchor="read_csv")]
    res = apply_edits(base, edits)
    assert "- use pd.read_csv for loading data\n- inserted" in res.text
    assert res.assertion_failures == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
