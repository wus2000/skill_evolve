"""Live replay harness: v2 editpipe vs legacy pipeline on real step
fixtures, with real LLM calls. Produces per-run metric JSON.

PURE VALIDATION TOOLING — not part of the mechanism. Used to A/B the edit
pipeline against recorded real inputs (tests/fixtures/edit_pipeline/) and
to inject the recorded real merger accident as a perturbation.

Usage (from the repo root):
  python tools/replay_editpipe.py v2 appworld_step1 [n_repeats]
  python tools/replay_editpipe.py v3 appworld_step1 [n_repeats]
  python tools/replay_editpipe.py legacy appworld_step1 [n_repeats]
  python tools/replay_editpipe.py inject appworld_step1
  python tools/replay_editpipe.py v2 all 3
"""
import json
import os
import sys
import time
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from llmfleet import FleetClient  # noqa: E402
from css.model.endpoints import resolve_base_url  # noqa: E402

FLEET = resolve_base_url(
    "http://10.77.110.162:8888/v1,http://10.77.110.162:8889/v1")
MODEL = os.environ.get("CSS_REPLAY_MODEL", "qwen3.6-35b-a3b")
KEY = os.environ.get("CSS_REPLAY_API_KEY", "token-abc123")
FIX = os.path.join(REPO, "tests", "fixtures", "edit_pipeline")
OUT_DIR = os.environ.get(
    "CSS_REPLAY_OUT",
    os.path.join(REPO, "runs", "editpipe_replays"))

STEP1_SIGNALS = {
    "spotify_multi_source": ["song_library", "album_library", "three source",
                             "album library", "song library"],
    "identity_resolution": ["roommate", "relationship", "contacts",
                            "search_contacts"],
    "sort_by_ranking": ["sort_by", "most played", "least played"],
    "authentication": ["access_token", "login"],
    "pagination": ["page_limit", "page_index", "pagination"],
    "playback": ["play_music", "playback"],
    "temporal": ["this year", "current year", "timestamp"],
}


class QwenClient:
    """Minimal LLMClient (optimizer role) over llmfleet, with call metering."""

    def __init__(self, temperature=0.7):
        self.fleet = FleetClient(FLEET, api_key=KEY)
        self.temperature = temperature
        self.n_calls = 0
        self.total_s = 0.0
        self.tokens_in = 0
        self.tokens_out = 0
        self._lock = threading.Lock()

    def complete_optimizer(self, system, user, *, max_tokens=4096):
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        t0 = time.time()
        data = self.fleet.post_chat(payload, timeout=600,
                                    session_key=system[:64])
        dur = time.time() - t0
        usage = data.get("usage", {})
        finish = data["choices"][0].get("finish_reason", "?")
        with self._lock:
            self.n_calls += 1
            self.total_s += dur
            self.tokens_in += usage.get("prompt_tokens", 0)
            self.tokens_out += usage.get("completion_tokens", 0)
            if finish == "length":
                self.truncations = getattr(self, "truncations", 0) + 1
        text = (data["choices"][0]["message"].get("content") or "")
        return text, usage

    def stats(self):
        return {"llm_calls": self.n_calls, "llm_seconds": round(self.total_s, 1),
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "truncations": getattr(self, "truncations", 0)}


class AccidentInjectClient(QwenClient):
    """Perturbation: the FIRST merger call returns the real 13-edit accident
    response (verbatim from the 2026-07-04 run); every other call is real
    qwen. Tests whether the live repair layers recover a real catastrophe."""

    def __init__(self):
        super().__init__()
        self._injected = False
        with open(os.path.join(FIX, "appworld_step1",
                               "legacy_merger_response.json")) as f:
            self._payload = f.read()

    def complete_optimizer(self, system, user, *, max_tokens=4096):
        if not self._injected and "You are the MERGER" in system:
            self._injected = True
            return self._payload, {"prompt_tokens": 0, "completion_tokens": 0}
        return super().complete_optimizer(system, user, max_tokens=max_tokens)


def run_v2_inject(case, seed_tag):
    """v2 with the real accident injected as the merger's first output."""
    from css.optimizer.editpipe.pipeline import run_pipeline
    from css.optimizer.editpipe.render import RulesDoc

    base, patches, n_raw = load_case(case)
    client = AccidentInjectClient()
    t0 = time.time()
    result = run_pipeline(client, base, patches)
    wall = time.time() - t0
    cons = result.consolidation
    ap = result.apply_result
    edits_repr = [f"[{e.kind}] {e.subject} :: {e.body}" for e in cons.edits]
    drops = [a.to_dict() for a in cons.audits if a.fate == "dropped"]
    silent = [d for d in drops if d["actor"] == "gate"
              and "byte-identical" not in d["reason"]]
    metrics = {
        "pipeline": "v2_inject_accident", "case": case, "tag": seed_tag,
        "n_raw": n_raw, "n_injected": 13, "n_final": len(cons.edits),
        "final_subjects": [e.subject for e in cons.edits],
        "final_kinds": sorted(e.kind for e in cons.edits),
        "converged": cons.converged,
        "adjudication_rounds": cons.stats.get("adjudication_rounds"),
        "coherence_rounds": cons.stats.get("merger", {}).get(
            "coherence_rounds"),
        "n_drops_audited": len(drops), "n_silent_drops": len(silent),
        "drop_reasons": [d["reason"][:120] for d in drops],
        "accepted_risks": [v.to_dict() for v in cons.accepted_risks],
        "assertion_failures": ap.assertion_failures,
        "signal_coverage": signal_coverage(edits_repr, case),
        "candidate_sections": [
            s.subject for s in RulesDoc.parse(ap.text).sections],
        "wall_s": round(wall, 1), **client.stats(),
    }
    detail = {"final_edits": [e.to_dict() for e in cons.edits],
              "audits": [a.to_dict() for a in cons.audits],
              "candidate_text": ap.text}
    return metrics, detail


def load_case(case):
    with open(os.path.join(FIX, case, "raw_patches.json")) as f:
        raw = json.load(f)
    with open(os.path.join(FIX, case, "base_rules.md")) as f:
        base = f.read()
    from css.data.edit import RawPatch
    patches = [RawPatch.from_dict(d) for d in raw]
    n_raw = sum(len(p.patch.edits) for p in patches if p and p.patch)
    return base, patches, n_raw


def signal_coverage(edits_repr, case):
    if case != "appworld_step1":
        return {}
    text = "\n".join(edits_repr).lower()
    return {name: any(k.lower() in text for k in keys)
            for name, keys in STEP1_SIGNALS.items()}


def run_v2(case, seed_tag):
    from css.optimizer.editpipe.pipeline import run_pipeline
    from css.optimizer.editpipe.render import RulesDoc

    base, patches, n_raw = load_case(case)
    client = QwenClient()
    t0 = time.time()
    result = run_pipeline(client, base, patches)
    wall = time.time() - t0

    cons = result.consolidation
    ap = result.apply_result
    edits_repr = [
        f"[{e.kind}] {e.subject} :: {e.body}" for e in cons.edits]

    # audit accounting: every dropped/merged/demoted has an actor+reason
    drops = [a.to_dict() for a in cons.audits if a.fate == "dropped"]
    silent_drops = [d for d in drops
                    if d["actor"] == "gate"
                    and "byte-identical" not in d["reason"]]

    from css.optimizer.editpipe.merger import number_raw_edits
    numbered = number_raw_edits(patches)
    used = set()
    for e in cons.edits:
        used.update(e.source_raw_edits)
    n_with_refs = sum(1 for e in cons.edits if e.source_raw_edits)
    metrics = {
        "pipeline": "v2", "case": case, "tag": seed_tag,
        "n_raw": n_raw,
        "n_parsed": cons.stats.get("merger", {}).get("parsed_edits"),
        "n_final": len(cons.edits),
        "provenance": {
            "edits_with_refs": n_with_refs,
            "raw_edits_cited": len(used),
            "raw_edits_unused": len(numbered) - len(used),
            "mean_target_tasks": round(
                sum(len(e.target_tasks) for e in cons.edits)
                / max(1, len(cons.edits)), 1),
        },
        "final_kinds": sorted(e.kind for e in cons.edits),
        "final_subjects": [e.subject for e in cons.edits],
        "converged": cons.converged,
        "adjudication_rounds": cons.stats.get("adjudication_rounds"),
        "coherence_rounds": cons.stats.get("merger", {}).get(
            "coherence_rounds"),
        "n_drops_audited": len(drops),
        "n_silent_drops": len(silent_drops),
        "accepted_risks": [v.to_dict() for v in cons.accepted_risks],
        "assertion_failures": ap.assertion_failures,
        "normalize_notes": ap.normalize_notes,
        "apply_degradations": [a.to_dict() for a in ap.audits
                               if a.fate == "degraded"],
        "signal_coverage": signal_coverage(edits_repr, case),
        "candidate_sections": [
            s.subject for s in RulesDoc.parse(ap.text).sections],
        "wall_s": round(wall, 1),
        **client.stats(),
    }
    detail = {
        "final_edits": [e.to_dict() for e in cons.edits],
        "audits": [a.to_dict() for a in cons.audits],
        "candidate_text": ap.text,
    }
    return metrics, detail


def run_v3(case, seed_tag):
    """v3 consolidation (plan/draft/review) + section applier on real raws."""
    from css.optimizer.editpipe3 import pipeline as ep3
    from css.optimizer.editpipe3.docmodel import RulesDocV3
    from css.optimizer.exploitation import _flatten_raw_edits
    from css.config import CSSConfig

    base, patches, n_raw = load_case(case)
    raw_edits = _flatten_raw_edits(patches)
    client = QwenClient()
    cfg = CSSConfig()
    t0 = time.time()
    cons = ep3.consolidate(client, base, raw_edits, cfg, max_workers=8)
    candidate, deferred = ep3.apply_groups(client, base, cons.groups)
    wall = time.time() - t0

    all_ids = {r["id"] for r in raw_edits}
    cited = set()
    for g in cons.groups:
        for e in g.edits:
            cited.update(e.source_ids)
    edits_repr = [
        f"[{e.op}] {e.section} :: {e.content}"
        for g in cons.groups for e in g.edits]
    audit_actions = {}
    for a in cons.audit:
        audit_actions[a.get("action", "?")] = (
            audit_actions.get(a.get("action", "?"), 0) + 1)
    metrics = {
        "pipeline": "v3", "case": case, "tag": seed_tag,
        "n_raw": n_raw,
        "n_raw_materials": len(raw_edits),
        "n_groups": len(cons.groups),
        "group_aspects": [g.aspect for g in cons.groups],
        "edits_per_group": [len(g.edits) for g in cons.groups],
        "ops": sorted(e.op for g in cons.groups for e in g.edits),
        "provenance": {
            "raw_ids_cited": len(cited & all_ids),
            "raw_ids_uncited": len(all_ids - cited),
            "mean_target_tasks": round(
                sum(len(g.target_tasks) for g in cons.groups)
                / max(1, len(cons.groups)), 1),
        },
        "dropped_by_review": sum(len(g.dropped) for g in cons.groups),
        "audit_actions": audit_actions,
        "n_deferred_apply": len(deferred),
        "signal_coverage": signal_coverage(edits_repr, case),
        "candidate_sections": [
            s.title for s in RulesDocV3.parse(candidate).sections],
        "wall_s": round(wall, 1),
        **client.stats(),
    }
    detail = {
        "groups": [g.to_dict() for g in cons.groups],
        "audit": cons.audit,
        "deferred": deferred,
        "candidate_text": candidate,
    }
    return metrics, detail


def run_legacy(case, seed_tag):
    from types import SimpleNamespace
    from css.optimizer.exploitation import _merger_with_validation
    from css.optimizer.section_apply import llm_apply_edits
    from css.optimizer.editpipe.render import RulesDoc, assert_structure

    base, patches, n_raw = load_case(case)
    client = QwenClient()

    cfg = SimpleNamespace(merger_granularity="point",
                          merger_inject_history=False,
                          merger_history_window=3)
    step_buffer = SimpleNamespace(entries=[], recent=lambda w: [])

    t0 = time.time()
    merged, conflict_pairs = _merger_with_validation(
        client, base, patches, step_buffer, cfg)
    # collective candidate via legacy LLM apply
    candidate = llm_apply_edits(client, base, merged) if merged else base
    wall = time.time() - t0

    edits_repr = [
        f"[{e.delta_type}] {e.section_target} :: {e.content}" for e in merged]
    metrics = {
        "pipeline": "legacy", "case": case, "tag": seed_tag,
        "n_raw": n_raw,
        "n_final": len(merged),
        "final_kinds": sorted(e.delta_type for e in merged),
        "final_subjects": [e.section_target for e in merged],
        "unresolved_conflict_pairs": conflict_pairs,
        "assertion_failures": assert_structure(candidate),
        "signal_coverage": signal_coverage(edits_repr, case),
        "candidate_sections": [
            s.subject for s in RulesDoc.parse(candidate).sections],
        "wall_s": round(wall, 1),
        **client.stats(),
    }
    detail = {
        "final_edits": [e.to_dict() for e in merged],
        "candidate_text": candidate,
    }
    return metrics, detail


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "v2"
    case = sys.argv[2] if len(sys.argv) > 2 else "appworld_step1"
    repeats = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    cases = (["appworld_step0", "appworld_step1", "spreadsheetbench_step2"]
             if case == "all" else [case])
    os.makedirs(OUT_DIR, exist_ok=True)

    runner = {"v2": run_v2, "v3": run_v3, "legacy": run_legacy,
              "inject": run_v2_inject}[which]
    for c in cases:
        for r in range(repeats):
            tag = f"{which}_{c}_r{r}_{int(time.time())}"
            print("=" * 70)
            print("RUN", tag)
            try:
                metrics, detail = runner(c, tag)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                metrics = {"pipeline": which, "case": c, "tag": tag,
                           "error": f"{type(exc).__name__}: {exc}"}
                detail = {}
            print(json.dumps(metrics, indent=1, ensure_ascii=False))
            with open(os.path.join(OUT_DIR, tag + ".json"), "w") as f:
                json.dump({"metrics": metrics, "detail": detail}, f,
                          ensure_ascii=False, indent=1)
            print("saved:", os.path.join(OUT_DIR, tag + ".json"))


if __name__ == "__main__":
    main()
