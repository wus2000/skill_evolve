"""Layer 3 living documents: no-silent-loss audit catches a dropped claim and
forces a single integration re-run; first-burst and resume behavior."""
from __future__ import annotations

from css.config import CSSConfig
from css.data.tree import TreeNode
from css.materials import common, dossier
from css.tests import materials_helpers as H
from css.tree_search import BurstResult


def _cfg() -> CSSConfig:
    return CSSConfig(max_api_workers=2)


def _burst(i=0) -> BurstResult:
    return BurstResult(node_id="n0000", burst_index=i, decision_index=i)


def _mining():
    return {
        "burst_summary_md": "## Burst\nExplore-then-commit dominated.",
        "adherence": {"reading": "Exploration honored."},
        "group_analyses": [{"group_key": "g",
                            "distilled_claims": [{"claim": "commit late", "evidence": "t1"}]}],
    }


def _new_section(user: str) -> str:
    idx = user.find("## NEW document")
    return user[idx:] if idx >= 0 else ""


def test_no_silent_loss_forces_reintegration(tmp_path, monkeypatch):
    out = str(tmp_path)
    prof_path = common.dossier_path(out, "n0000", "behavior_profile.md")
    common.write_text_atomic(prof_path,
                             "## Actual behavior patterns\nCLAIM_X: exploration helps.\n"
                             "\n## Evolution log\n- burst -1")

    def integrate(user: str) -> dict:
        if "MANDATORY corrections" in user:
            # the re-run restores the dropped claim with a stated disposition
            return {"document_md": "## Actual behavior patterns\nCLAIM_X: exploration "
                    "helps (kept, still supported).\nCommit late.\n\n## Evolution log\n- burst 0"}
        # first integration silently drops CLAIM_X
        return {"document_md": "## Actual behavior patterns\nCommit late.\n"
                "\n## Evolution log\n- burst 0"}

    def audit(user: str) -> dict:
        new = _new_section(user)
        if "CLAIM_X" in new:
            return {"ledger": [{"old_claim": "CLAIM_X: exploration helps.",
                                "disposition": "kept", "reason": "", "new_claim": ""}],
                    "unaccounted": []}
        return {"ledger": [{"old_claim": "CLAIM_X: exploration helps.",
                            "disposition": "", "reason": "dropped"}],
                "unaccounted": ["CLAIM_X: exploration helps."]}

    fake = H.FakeStage({"profile_integrate": integrate, "profile_audit": audit})
    monkeypatch.setattr(common, "run_json_stage", fake)

    result = dossier.update_behavior_profile(
        TreeNode(node_id="n0000"), _burst(0), _mining(), object(), _cfg(), out)

    # integration ran twice (drop -> audit flags -> re-run restores)
    assert fake.count("profile_integrate") == 2
    assert result["reran"] is True
    assert result["ledger"]["unaccounted"] == []
    # final document keeps the prior claim
    assert "CLAIM_X" in common.read_text(prof_path)
    # diff ledger persisted for this burst
    diff = common.read_json(common.analysis_burst_dir(out, "n0000", 0) + "/profile_update_diff.json")
    assert diff and diff["reran"] is True


def test_first_burst_no_audit_no_rerun(tmp_path, monkeypatch):
    out = str(tmp_path)
    fake = H.FakeStage({
        "profile_integrate": lambda u: {"document_md": "## Actual behavior patterns\nfresh."},
        "profile_audit": lambda u: {"ledger": [], "unaccounted": []},
    })
    monkeypatch.setattr(common, "run_json_stage", fake)
    result = dossier.update_behavior_profile(
        TreeNode(node_id="n0000"), _burst(0), _mining(), object(), _cfg(), out)
    assert result["reran"] is False
    assert fake.count("profile_integrate") == 1
    assert fake.count("profile_audit") == 0, "empty prior document -> no audit"


def test_resume_skips_integrated_burst(tmp_path, monkeypatch):
    out = str(tmp_path)
    fake = H.FakeStage({
        "profile_integrate": lambda u: {"document_md": "## X\nbody\n## Evolution log\n- b"},
        "profile_audit": lambda u: {"ledger": [], "unaccounted": []},
    })
    monkeypatch.setattr(common, "run_json_stage", fake)
    dossier.update_behavior_profile(TreeNode(node_id="n0000"), _burst(0), _mining(),
                                    object(), _cfg(), out)
    n1 = fake.count("profile_integrate")
    # second call with the same burst index: the diff file is the resume marker
    r2 = dossier.update_behavior_profile(TreeNode(node_id="n0000"), _burst(0), _mining(),
                                         object(), _cfg(), out)
    assert r2.get("skipped") is True
    assert fake.count("profile_integrate") == n1, "resume must not re-integrate the burst"
