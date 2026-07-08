"""Document metabolism: differential drafting (U) + burst-end consolidation (D).

Stub-driven (no network). Load-bearing invariants:
  * draft contract — absorbed_as_covered accounting closes the member
    partition; an all-absorbed empty edit list is LEGAL (the differential
    drafter's success state) and audited, never silently dropped;
  * NEW titles never carry leaked protocol handles;
  * the applier's lossless backstop — a fusion losing a backtick identifier
    without declaring it repairs once, then degrades to conservative append
    (content is never silently lost);
  * over-budget sections get the curation trigger note (signal, not a cap);
  * consolidation plan validation (every handle exactly once), deterministic
    execution (plan order), the identifier abandon path, the non-inferiority
    decision (LOST<=GAINED and mean >= inc-margin), and idempotent replay.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

from css.model.client import StubLLMClient
import css.optimizer.editpipe3.consolidate as cons_mod
from css.optimizer.editpipe3.docmodel import RulesDocV3
from css.optimizer.editpipe3.pipeline import (
    AspectGroup,
    DraftEdit,
    _check_draft,
    _strip_handle_title,
    apply_groups,
    consolidate,
)

BASE = ("### Data Retrieval\n- Always page through `page_index` until an "
        "empty list.\n- Set `page_limit=20` on Spotify lists.\n\n"
        "### Error Handling\nRetry once on transient failures.\n\n"
        "### Output Format\nWrite raw numbers, never formatted strings.\n")


def _draft_ok(obj):
    return isinstance(obj, dict) and "edits" in obj


# ── U: differential draft contract ───────────────────────────────────────────
def test_check_draft_absorbed_accounting_and_empty_edits():
    doc = RulesDocV3.parse(BASE)
    catalog = doc.handle_map()
    obj = {"edits": [],
           "absorbed_as_covered": [
               {"ids": ["E#1", "E#2"],
                "covered_by": "S#1: Always page through page_index"}],
           "dropped_ids": []}
    assert _check_draft(obj, ["E#1", "E#2"], catalog) == []
    # Missing covered_by -> violation; unaccounted member -> violation.
    obj2 = {"edits": [], "absorbed_as_covered": [{"ids": ["E#1"]}],
            "dropped_ids": []}
    v = _check_draft(obj2, ["E#1", "E#2"], catalog)
    assert any("covered_by" in x for x in v)
    assert any("E#2" in x for x in v)


def test_check_draft_new_title_rejects_handle_text():
    doc = RulesDocV3.parse(BASE)
    obj = {"edits": [{"op": "add_section", "section": "NEW: [S#6] Queue Ops",
                      "content": "- x", "source_ids": ["E#1"]}],
           "dropped_ids": []}
    v = _check_draft(obj, ["E#1"], doc.handle_map())
    assert any("handle text" in x for x in v)


def test_strip_handle_title():
    assert _strip_handle_title("[S#6] Sheet Structure") == "Sheet Structure"
    assert _strip_handle_title("S#15 - Queue Interaction") == "Queue Interaction"
    assert _strip_handle_title("Plain Title") == "Plain Title"
    assert _strip_handle_title("[S#3]") == "[S#3]"  # nothing left -> keep


def test_consolidate_fully_absorbed_group_is_audited():
    def optimizer_fn(system, user):
        if system.startswith("You organize a batch of RAW EDITS"):
            return json.dumps({"groups": [
                {"ids": ["E#1"], "aspect": "pagination instance",
                 "placement": "S#1"}]})
        if "DIFFERENTIAL" in system:
            return json.dumps({
                "analysis": "already covered",
                "edits": [],
                "absorbed_as_covered": [
                    {"ids": ["E#1"],
                     "covered_by": "S#1: page through page_index"}],
                "dropped_ids": []})
        return json.dumps({"pass": True, "issues": []})

    client = StubLLMClient(optimizer_fn=optimizer_fn)
    res = consolidate(client, BASE, [
        {"section_target": "Data Retrieval", "kind": "add_point",
         "body": "- paginate voice messages too", "rationale": "seen",
         "target_tasks": ["t1"], "src": "P#1",
         "vs_doc": "instance-of: page through all pages"}])
    assert res.groups == []
    absorbed = [a for a in res.audit
                if a.get("action") == "group_fully_absorbed"]
    assert len(absorbed) == 1 and absorbed[0]["member_ids"] == ["E#1"]


def test_apply_strips_leaked_handle_from_new_title():
    def optimizer_fn(system, user):
        return json.dumps({"application_notes": "", "unapplied": [],
                           "absorbed": [], "dropped": [],
                           "new_section_text": "merged"})
    client = StubLLMClient(optimizer_fn=optimizer_fn)
    g = AspectGroup(gid="G#1", member_ids=["E#1"], edits=[DraftEdit(
        op="add_section", section="NEW: [S#9] Fresh Topic",
        content="- brand new", source_ids=["E#1"])])
    out, deferred = apply_groups(client, BASE, [g])
    assert deferred == []
    titles = [s.title for s in RulesDocV3.parse(out).sections]
    assert "Fresh Topic" in titles
    assert not any("S#9" in t for t in titles)


def test_applier_lossless_repair_then_degrade():
    calls = {"n": 0}

    def optimizer_fn(system, user):
        calls["n"] += 1
        # Both the fusion and the repair keep losing `page_limit=20`.
        return json.dumps({"application_notes": "", "unapplied": [],
                           "absorbed": [], "dropped": [],
                           "new_section_text": "- paginate everything"})

    client = StubLLMClient(optimizer_fn=optimizer_fn)
    g = AspectGroup(gid="G#1", member_ids=["E#1"], edits=[DraftEdit(
        op="append_to_section", section="S#1",
        content="- also paginate voice messages", source_ids=["E#1"])])
    audit: list = []
    out, deferred = apply_groups(client, BASE, [g], audit)
    assert calls["n"] == 2, "one fusion + one lossless repair attempt"
    body = RulesDocV3.parse(out).sections[0].body
    assert "page_limit=20" in body, "degrade keeps the old body"
    assert "also paginate voice messages" in body, "and appends the edit"
    assert any(a.get("apply") == "lossless_degraded_to_append"
               for a in audit)


def test_applier_lossless_repair_success_adopted():
    calls = {"n": 0}

    def optimizer_fn(system, user):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"application_notes": "", "unapplied": [],
                               "absorbed": [], "dropped": [],
                               "new_section_text": "- paginate everything"})
        return json.dumps({"application_notes": "", "unapplied": [],
                           "absorbed": [], "dropped": [],
                           "new_section_text":
                               "- paginate everything via `page_index`; "
                               "Spotify needs `page_limit=20`"})

    client = StubLLMClient(optimizer_fn=optimizer_fn)
    g = AspectGroup(gid="G#1", member_ids=["E#1"], edits=[DraftEdit(
        op="append_to_section", section="S#1", content="- x",
        source_ids=["E#1"])])
    audit: list = []
    out, _ = apply_groups(client, BASE, [g], audit)
    assert any(a.get("apply") == "lossless_repaired" for a in audit)
    assert "page_limit=20" in RulesDocV3.parse(out).sections[0].body


def test_curation_note_on_over_budget_section():
    seen = {}

    def optimizer_fn(system, user):
        seen["user"] = user
        return json.dumps({"application_notes": "", "unapplied": [],
                           "absorbed": [], "dropped": [],
                           "new_section_text": "fused `page_index` and "
                                               "`page_limit=20` kept"})
    fat = "### Fat\n" + "\n".join("- `page_index` rule %d" % i
                                  for i in range(20)) + "\n"
    client = StubLLMClient(optimizer_fn=optimizer_fn)
    g = AspectGroup(gid="G#1", member_ids=["E#1"], edits=[DraftEdit(
        op="append_to_section", section="S#1", content="- one more",
        source_ids=["E#1"])])
    apply_groups(client, fat, [g], bullet_budget=15)
    assert "Curation signal" in seen["user"]
    apply_groups(client, fat, [g], bullet_budget=0)  # 0 disables the signal
    assert "Curation signal" not in seen["user"]


# ── D: consolidation plan / execution / gate ─────────────────────────────────
def _node(rules=BASE):
    return SimpleNamespace(rules=rules, best_rules=rules, strategy="",
                           best_score=0.5, best_step=2, val_score=0.5,
                           val_ledger={})


def _cfg(**kw):
    base = dict(gate_screen_k=1, gate_escalation_k=3,
                consolidation_margin=0.015, l0_section_bullet_budget=15,
                max_api_workers=2, task_timeout_s=10)
    base.update(kw)
    return SimpleNamespace(**base)


def test_check_plan_completeness():
    doc = RulesDocV3.parse(BASE)
    scope = ["S#1", "S#2", "S#3"]
    good = {"sections": [
        {"op": "keep", "handles": ["S#1"]},
        {"op": "merge", "handles": ["S#2", "S#3"], "title": "Ops",
         "body": "merged body"}]}
    assert cons_mod._check_plan(good, doc, scope) == []
    v = cons_mod._check_plan({"sections": [
        {"op": "keep", "handles": ["S#1"]},
        {"op": "rewrite", "handles": ["S#1"], "title": "X", "body": "y"},
        {"op": "delete", "handles": ["S#2"]}]}, doc, scope)
    assert any("MORE than one" in x for x in v)      # S#1 twice
    assert any("S#3" in x for x in v)                # S#3 missing
    assert any("reason" in x for x in v)             # delete without reason


def test_build_output_order_and_audit():
    doc = RulesDocV3.parse(BASE)
    audit: list = []
    out = cons_mod._build_output(doc, [
        {"op": "merge", "handles": ["S#1", "S#3"], "title": "Retrieval",
         "body": "- page via `page_index`, `page_limit=20`; raw numbers"},
        {"op": "delete", "handles": ["S#2"], "reason": "duplicate"},
    ], audit)
    parsed = RulesDocV3.parse(out)
    assert [s.title for s in parsed.sections] == ["Retrieval"]
    deleted = [a for a in audit if a["action"] == "delete"]
    assert deleted and deleted[0]["old"][0]["title"] == "Error Handling"


def test_consolidation_abandons_on_lost_identifier(tmp_path, monkeypatch):
    def optimizer_fn(system, user):
        # Plan deletes S#1 whose identifiers exist nowhere else and are not
        # declared in dropped_facts -> the tidy-up must abandon.
        return json.dumps({"plan": "aggressive", "sections": [
            {"op": "delete", "handles": ["S#1"], "reason": "noise"},
            {"op": "keep", "handles": ["S#2"]},
            {"op": "keep", "handles": ["S#3"]}], "dropped_facts": []})
    client = StubLLMClient(optimizer_fn=optimizer_fn)
    node = _node()
    out = cons_mod.run_burst_consolidation(
        node, env=None, val_items=[], target_client=None,
        optimizer_client=client, cfg=_cfg(), out_dir=str(tmp_path / "c0"))
    assert out.ran and not out.accepted
    assert "identifier" in out.reason
    assert node.rules == BASE and node.best_rules == BASE
    gd = json.load(open(tmp_path / "c0" / "gate.json"))
    assert gd["applied"] is False


def test_consolidation_accept_updates_node_and_replays(tmp_path, monkeypatch):
    tidy_body = ("### Retrieval\n- page via `page_index` until empty; "
                 "`page_limit=20` on Spotify.\n\n### Error Handling\nRetry "
                 "once on transient failures.\n\n### Output Format\nWrite "
                 "raw numbers, never formatted strings.\n")
    llm_calls = {"n": 0}

    def optimizer_fn(system, user):
        llm_calls["n"] += 1
        return json.dumps({"plan": "merge S#1", "sections": [
            {"op": "rewrite", "handles": ["S#1"], "title": "Retrieval",
             "body": "- page via `page_index` until empty; `page_limit=20` "
                     "on Spotify."},
            {"op": "keep", "handles": ["S#2"]},
            {"op": "keep", "handles": ["S#3"]}], "dropped_facts": []})

    from css.evaluation import paired_gate as pg

    def fake_gate(env, node, incumbent_rules, candidate_rules, val_items,
                  target_client, cfg, out_dir):
        node.val_ledger = {"t1": {"passes": 1, "trials": 1}}
        return pg.PairedGateResult(
            accept=True, p_value=1.0, n_pos=0, n_neg=0,
            n_screen_discordant=0, cand_mean=0.495,
            ledger_bootstrapped=0, inc_mean=0.5)

    monkeypatch.setattr(pg, "run_noninferiority_gate", fake_gate)
    client = StubLLMClient(optimizer_fn=optimizer_fn)
    node = _node()
    out_dir = str(tmp_path / "c1")
    out = cons_mod.run_burst_consolidation(
        node, env=None, val_items=[], target_client=None,
        optimizer_client=client, cfg=_cfg(), out_dir=out_dir)
    assert out.accepted and node.best_score == 0.495
    assert "Retrieval" in node.rules and node.rules == node.best_rules
    assert node.val_ledger == {"t1": {"passes": 1, "trials": 1}}
    calls_after_first = llm_calls["n"]

    # Idempotent replay: no new LLM call, node fields re-applied.
    node2 = _node()
    out2 = cons_mod.run_burst_consolidation(
        node2, env=None, val_items=[], target_client=None,
        optimizer_client=client, cfg=_cfg(), out_dir=out_dir)
    assert out2.accepted and llm_calls["n"] == calls_after_first
    assert node2.rules == node.rules and node2.best_score == 0.495


def test_consolidation_skips_tiny_document():
    out = cons_mod.run_burst_consolidation(
        _node("### Only\n- one\n"), env=None, val_items=[],
        target_client=None, optimizer_client=None, cfg=_cfg(),
        out_dir="/nonexistent-must-not-be-created")
    assert not out.ran and "too small" in out.reason
    assert not os.path.exists("/nonexistent-must-not-be-created")


def test_noninferiority_gate_decision(monkeypatch):
    from css.evaluation import paired_gate as pg

    items = [{"id": "t%d" % i} for i in range(3)]

    def make_roll(inc_map, cand_map):
        def _fake_roll(env, skill_text, roll_items, client, k, out_dir, cfg):
            table = cand_map if "CAND" in skill_text else inc_map
            return {str(it["id"]): table[str(it["id"])] for it in roll_items}
        return _fake_roll

    node = SimpleNamespace(strategy="", val_score=0.5, val_ledger={})

    # A: identical results -> no discordant -> non-inferior -> ACCEPT.
    monkeypatch.setattr(pg, "_roll_items", make_roll(
        {"t0": (1, 1), "t1": (1, 1), "t2": (0, 1)},
        {"t0": (1, 1), "t1": (1, 1), "t2": (0, 1)}))
    r = pg.run_noninferiority_gate(
        None, node, "OLD", "CAND", items, None, _cfg(), "/tmp/unused")
    assert r.accept and r.n_screen_discordant == 0

    # B: candidate loses one item outright (screen + escalation) -> reject.
    monkeypatch.setattr(pg, "_roll_items", make_roll(
        {"t0": (1, 1), "t1": (1, 1), "t2": (1, 1)},
        {"t0": (1, 1), "t1": (1, 1), "t2": (0, 1)}))
    r = pg.run_noninferiority_gate(
        None, node, "OLD", "CAND", items, None, _cfg(), "/tmp/unused")
    assert not r.accept and r.n_neg == 1 and r.n_pos == 0

    # C: no per-item verdicts, small mean dip inside the margin -> ACCEPT
    # (the plain "not lower than before" rule would have killed it).
    monkeypatch.setattr(pg, "_roll_items", make_roll(
        {"t0": (3, 3), "t1": (3, 3), "t2": (3, 3)},
        {"t0": (3, 3), "t1": (3, 3), "t2": (3, 3)}))
    r = pg.run_noninferiority_gate(
        None, node, "OLD", "CAND", items, None,
        _cfg(gate_screen_k=3), "/tmp/unused")
    assert r.accept


def test_v3_step_metrics_shape():
    from css.optimizer.exploitation import _v3_step_metrics

    class _C:
        def count_tokens(self, text):
            return len(text) // 4

    g = AspectGroup(gid="G#1", member_ids=["E#1", "E#2"],
                    edits=[DraftEdit(op="amend_section", section="S#1",
                                     content="- inc", source_ids=["E#1"])],
                    absorbed=[{"ids": ["E#2"], "covered_by": "S#1"}])
    audit = [{"stage": "B", "gid": "G#2", "action": "group_fully_absorbed",
              "member_ids": ["E#3"],
              "absorbed": [{"ids": ["E#3"], "covered_by": "S#2"}],
              "dropped": []},
             {"stage": "C", "action": "revised",
              "issues": [{"type": "redundant_with_doc"}]}]
    m = _v3_step_metrics(BASE, BASE + "\n- inc", [g], audit, _C())
    assert m["op_counts"] == {"amend_section": 1}
    assert m["n_sources_absorbed_as_covered"] == 2
    assert m["n_groups_fully_absorbed"] == 1
    assert m["n_redundant_with_doc_issues"] == 1
    assert m["net_delta_chars"] == len("\n- inc")
    assert m["candidate_tokens"] == (len(BASE + "\n- inc")) // 4
