"""Contract tests for the WebArena env adapter (no playwright, no sites).

Mirrors the house contract suite (test_env_template.py): the six-method
surface, id/caching invariants, GT firewall, and the env-specific pieces —
mutation-aware leasing and the structured stop contract. Episodes and scoring
are injected so the suite runs anywhere.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest

from css.config import CSSConfig
from css.envs.common import skill_hash
from css.envs.registry import build_env
from css.envs.webarena.prompts import parse_stop_payload
from css.envs.webarena.agent import extract_action_str, resolve_start_url
from css.envs.webarena.scheduler import Lease, SiteLeaseManager
from css.envs.webarena.scoring import write_agent_response


def _mini_records():
    def rec(tid, tpl, sites, ttype):
        return {"id": f"wa_{tid:04d}", "task_id": tid, "intent_template_id": tpl,
                "sites": sites, "start_urls": [f"__{sites[0].upper()}__"],
                "intent": f"task {tid}", "intent_template": "t",
                "instantiation_dict": {},
                "eval": [{"evaluator": "AgentResponseEvaluator",
                          "expected": {"task_type": ttype, "status": "SUCCESS",
                                       "retrieved_data": None}}],
                "revision": 2}
    train = [rec(1, 10, ["shopping"], "retrieve"),
             rec(2, 11, ["reddit"], "mutate"),
             rec(3, 12, ["gitlab", "reddit"], "mutate")]
    return {"train": train, "val": [rec(4, 13, ["shopping"], "navigate")],
            "test": [rec(5, 14, ["reddit"], "retrieve")]}


def _cfg(**extra):
    base = {"webarena_stacks": {"s1": {"shopping": "http://h:7770",
                                        "reddit": "http://h:9999",
                                        "gitlab": "http://h:8023"}}}
    base.update(extra)
    return CSSConfig(env_name="webarena", n_train=0, n_val=0, n_test=0,
                     extra=base)


def _episode(record_holder=None):
    def run(item, skill_text, client, cfg, lease, workdir):
        assert "eval" not in item, "GT firewall breach"
        if record_holder is not None:
            record_holder.append((item["task_id"], lease.sites))
        write_agent_response(workdir, {"task_type": "retrieve",
                                       "status": "SUCCESS",
                                       "retrieved_data": ["x"],
                                       "error_details": None})
        return {"messages": [{"role": "user", "content": "obs"},
                              {"role": "assistant", "content": "stop"}],
                "n_turns": 2, "agent_response": {"status": "SUCCESS"}}
    return run


class PassScorer:
    def score(self, task_id, workdir):
        assert os.path.exists(os.path.join(workdir, "agent_response.json"))
        return {"hard": 1, "detail": {"score": 1.0, "status": "success"}}


class FailScorer:
    def score(self, task_id, workdir):
        return {"hard": 0, "detail": {"status": "failure", "score": 0.0}}


class TestWebArenaEnv(unittest.TestCase):
    def test_registry_and_splits(self):
        env = build_env(_cfg(), items=_mini_records(),
                        episode_fn=_episode(), scorer=PassScorer())
        self.assertEqual([r["id"] for r in env.train_items()],
                         ["wa_0001", "wa_0002", "wa_0003"])
        self.assertEqual(len(env.val_items()), 1)
        self.assertEqual(len(env.test_items()), 1)
        self.assertTrue(env.action_space_description().strip())

    def test_run_one_pass_and_annotation(self):
        env = build_env(_cfg(), items=_mini_records(),
                        episode_fn=_episode(), scorer=PassScorer())
        with tempfile.TemporaryDirectory() as td:
            res = env.run_one(env.train_items()[0], "S", object(), td,
                              rollout_index=0, epoch=1, node_id="n")
            self.assertEqual(res.hard, 1)
            last = res.messages[-1]
            self.assertEqual(last["role"], "evaluation")
            self.assertIn("PASS", str(last))

    def test_run_one_fail_reason(self):
        env = build_env(_cfg(), items=_mini_records(),
                        episode_fn=_episode(), scorer=FailScorer())
        with tempfile.TemporaryDirectory() as td:
            res = env.run_one(env.train_items()[0], "S", object(), td,
                              rollout_index=0, epoch=0, node_id="n")
            self.assertEqual(res.hard, 0)
            self.assertTrue(res.fail_reason)

    def test_cache_roundtrip_and_stale_miss(self):
        env = build_env(_cfg(), items=_mini_records(),
                        episode_fn=_episode(), scorer=PassScorer())
        item = env.train_items()[0]
        with tempfile.TemporaryDirectory() as td:
            env.run_one(item, "SKILL", object(), td,
                        rollout_index=0, epoch=0, node_id="n")
            hit = env.load_cached_result(item, td, rollout_index=0,
                                         skill_hash=skill_hash("SKILL"))
            self.assertIsNotNone(hit)
            self.assertEqual(hit.hard, 1)
            miss = env.load_cached_result(item, td, rollout_index=0,
                                          skill_hash=skill_hash("OTHER"))
            self.assertIsNone(miss)

    def test_multisite_mutate_pins_all_sites(self):
        seen = []
        env = build_env(_cfg(), items=_mini_records(),
                        episode_fn=_episode(seen), scorer=PassScorer())
        multisite = env.train_items()[2]
        with tempfile.TemporaryDirectory() as td:
            env.run_one(multisite, "S", object(), td,
                        rollout_index=0, epoch=0, node_id="n")
        self.assertEqual(seen[-1], (3, ("gitlab", "reddit")))

    def test_lease_exclusive_and_refresh_before_next_mutate(self):
        refreshed = []
        mgr = SiteLeaseManager({"s1": {"reddit": "http://h:9999"}},
                               refresh_fn=lambda st, si: refreshed.append((st, si)))
        l1 = mgr.acquire(["reddit"], "mutate")
        self.assertEqual(refreshed, [])          # clean lane: no refresh
        blocked = {}

        def contender():
            blocked["l"] = mgr.acquire(["reddit"], "mutate", timeout_s=10)
        t = threading.Thread(target=contender)
        t.start(); t.join(0.3)
        self.assertTrue(t.is_alive())            # exclusive while held
        mgr.release(l1)
        t.join(10)
        self.assertFalse(t.is_alive())
        # exactly-once refresh between the two leases (eager thread and the
        # contender's sync fallback race for the lock; loser must no-op)
        self.assertEqual(refreshed, [("s1", "reddit")])
        mgr.release(blocked["l"])

    def test_release_triggers_eager_refresh(self):
        refreshed = []
        mgr = SiteLeaseManager({"s1": {"reddit": "http://h:9999"}},
                               refresh_fn=lambda st, si: refreshed.append((st, si)))
        mgr.release(mgr.acquire(["reddit"], "mutate"))
        deadline = time.monotonic() + 5
        while not refreshed and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(refreshed, [("s1", "reddit")])  # no acquire needed

    def test_readonly_needs_no_lock(self):
        mgr = SiteLeaseManager({"s1": {"reddit": "http://h:9999"}})
        l1 = mgr.acquire(["reddit"], "mutate")
        ro = mgr.acquire(["reddit"], "retrieve", timeout_s=1)
        self.assertFalse(ro.exclusive)           # read-only never blocks
        mgr.release(l1); mgr.release(ro)

    def test_readonly_avoids_refreshing_stack(self):
        gate = threading.Event()

        def slow_refresh(st, si):
            gate.wait(10)
        mgr = SiteLeaseManager({"s1": {"reddit": "http://h:9999"},
                                "s2": {"reddit": "http://h:19999"}},
                               refresh_fn=slow_refresh)
        mgr.release(mgr.acquire(["reddit"], "mutate"))   # dirty one stack
        lane_keys = [k for k, ln in mgr._lanes.items()]
        deadline = time.monotonic() + 5
        refreshing = None
        while refreshing is None and time.monotonic() < deadline:
            hot = [k for k in lane_keys if mgr._lanes[k].refreshing]
            refreshing = hot[0] if hot else None
            time.sleep(0.02)
        self.assertIsNotNone(refreshing)         # eager refresh in flight
        for _ in range(6):                       # RR must never land on it
            ro = mgr.acquire(["reddit"], "retrieve", timeout_s=2)
            self.assertNotEqual(ro.stack, refreshing[0])
        # unrelated sites are unaffected by the busy lane
        gate.set()

    def test_readonly_single_stack_waits_out_refresh(self):
        gate = threading.Event()

        def slow_refresh(st, si):
            gate.wait(10)
        mgr = SiteLeaseManager({"s1": {"reddit": "http://h:9999"}},
                               refresh_fn=slow_refresh)
        mgr.release(mgr.acquire(["reddit"], "mutate"))
        deadline = time.monotonic() + 5
        while not mgr._lanes[("s1", "reddit")].refreshing \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(mgr._lanes[("s1", "reddit")].refreshing)
        with self.assertRaises(TimeoutError):    # no healthy stack available
            mgr.acquire(["reddit"], "retrieve", timeout_s=1)
        gate.set()
        ro = mgr.acquire(["reddit"], "retrieve", timeout_s=10)
        self.assertFalse(ro.exclusive)

    def test_refresh_storm_capped(self):
        active = {"n": 0, "peak": 0}
        mu = threading.Lock()

        def slow_refresh(st, si):
            with mu:
                active["n"] += 1
                active["peak"] = max(active["peak"], active["n"])
            time.sleep(0.15)
            with mu:
                active["n"] -= 1
        stacks = {f"s{i}": {"reddit": f"http://h:{i}9999"} for i in range(6)}
        mgr = SiteLeaseManager(stacks, refresh_fn=slow_refresh,
                               refresh_concurrency=2)
        leases = [mgr.acquire(["reddit"], "mutate", timeout_s=5)
                  for _ in range(6)]
        for l in leases:          # release all at once -> eager refresh storm
            mgr.release(l)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with mgr._mu:
                if not any(ln.dirty or ln.refreshing
                           for ln in mgr._lanes.values()):
                    break
            time.sleep(0.02)
        self.assertLessEqual(active["peak"], 2)   # cap honoured
        self.assertEqual(active["n"], 0)          # all refreshes drained

    def test_failed_refresh_leaves_lane_dirty_then_retries(self):
        calls = []

        def flaky(st, si):
            calls.append((st, si))
            if len(calls) == 1:
                raise RuntimeError("boom")
        mgr = SiteLeaseManager({"s1": {"reddit": "http://h:9999"}},
                               refresh_fn=flaky)
        mgr.release(mgr.acquire(["reddit"], "mutate"))
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(calls), 1)          # eager attempt failed
        l2 = mgr.acquire(["reddit"], "mutate", timeout_s=10)
        self.assertEqual(len(calls), 2)          # sync fallback re-refreshed
        mgr.release(l2)

    def test_stop_contract_and_action_extraction(self):
        s = parse_stop_payload('{"task_type": "mutate", "status": "SUCCESS", '
                               '"retrieved_data": null}')
        self.assertEqual(s["status"], "SUCCESS")
        loose = parse_stop_payload("the answer is 42")
        self.assertEqual(loose["retrieved_data"], ["the answer is 42"])
        act = extract_action_str("thinking...\n```click [12]```")
        self.assertEqual(act, "click [12]")
        lease = Lease(stack="s1", urls={"shopping": "http://h:7770/"},
                      sites=(), exclusive=False)
        self.assertEqual(resolve_start_url({"start_urls": ["__SHOPPING__"]},
                                           lease), "http://h:7770")


if __name__ == "__main__":
    unittest.main()
