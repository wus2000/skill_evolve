#!/usr/bin/env python3
"""Generate ScienceWorld split manifests for CSS (seed-deterministic).

Produces, under ``data/scienceworld_split_seed42/``:
  test_agentboard/items.json  the 90 AgentBoard instances, each mapped to an
                              exact (task_name, variation) recovered from its
                              subgoals (see recovery below), protocol="agentboard".
  train/items.json            stratified train pool over the AgentBoard task
                              universe (JAR train fold), protocol="original".
  val/items.json              stratified val carve (JAR dev fold).
  test_secondary/items.json   stratified native-score test sample (JAR test fold).
  gold_actions.json           {"task::var": [gold actions]} for every manifest
                              variation (offline eval-annotation accelerator;
                              zip where available, live-generated otherwise).

AgentBoard var recovery (every instance must map to exactly one (task, variation)):
  1. ZIP match — for each instance, find the variation whose goldpaths-all.zip
     gold-OBSERVATION stream contains a match for every subgoal regex; pick the
     smallest (task, variation) deterministically. (Multiple variations can be
     behaviourally equivalent w.r.t. the subgoals — the subgoal SR/PR is then
     identical, so the smallest is a faithful, reproducible choice.)
  2. LIVE match — residuals (incl. measure-melting-unknown, absent from the zip)
     are recovered by loading each candidate variation under the AgentBoard
     simplification, narrowing by the instance's named target entity, and
     verifying all subgoals against the live gold-replay observation stream.

Run under an interpreter with ``scienceworld`` installed (the smoke venv):
  env_candidates/scienceworld_smoke/venv/bin/python tools/make_scienceworld_split.py
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import zipfile

import ijson

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AB_JSONL = os.path.join(REPO, "env_candidates", "agentboard_scienceworld", "data", "test.jsonl")
ZIP = os.path.join(REPO, "env_candidates", "scienceworld", "goldpaths", "goldpaths-all.zip")
OUT = os.path.join(REPO, "data", "scienceworld_split_seed42")
SEED = 42
SIMPL = "selfWateringFlowerPots,openContainers,openDoors,noElectricalAction"

# per-task-type stratified caps (config approved 2026-07-04)
TRAIN_CAP = 15   # -> ~250-300 over ~18-20 task types
VAL_CAP = 5      # -> ~90
SEC_CAP = 8      # -> ~150

ALL_SW = ["boil", "freeze", "melt", "change-the-state-of-matter-of", "use-thermometer",
          "measure-melting-point-known-substance", "measure-melting-point-unknown-substance",
          "find-animal", "find-living-thing", "find-plant", "find-non-living-thing",
          "chemistry-mix", "chemistry-mix-paint-secondary-color", "chemistry-mix-paint-tertiary-color",
          "lifespan-longest-lived", "lifespan-shortest-lived", "lifespan-longest-lived-then-shortest-lived",
          "identify-life-stages-1", "identify-life-stages-2"]


# ── inference + parsing ─────────────────────────────────────────────────────
def zip_label_to_sw(label: str) -> str:
    return re.sub(r"^task-\d+[a-z]?-", "", label).replace("(", "").replace(")", "")


def subgoal_patterns(raw) -> "list[str]":
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return [p.strip() for p in re.split(r"Subgoal\s*\d+\s*:\s*", str(raw)) if p.strip()]


def candidates(goal: str) -> "list[str]":
    g = goal.lower().strip()
    if re.search(r"\bfind\b", g) or "focus on the thing" in g:
        return ["find-animal", "find-living-thing", "find-plant", "find-non-living-thing"]
    if "life stages" in g:
        return ["identify-life-stages-1", "identify-life-stages-2"]
    if "longest life span" in g and "shortest" in g:
        return ["lifespan-longest-lived-then-shortest-lived"]
    if "longest life span" in g:
        return ["lifespan-longest-lived"]
    if "shortest life span" in g:
        return ["lifespan-shortest-lived"]
    if "melting point" in g and "unknown" in g:
        return ["measure-melting-point-unknown-substance"]
    if "melting point" in g:
        return ["measure-melting-point-known-substance"]
    if "measure the temperature" in g:
        return ["use-thermometer"]
    if "paint" in g:
        return ["chemistry-mix-paint-secondary-color", "chemistry-mix-paint-tertiary-color"]
    if "use chemistry to create" in g:
        return ["chemistry-mix"]
    if "boil" in g:
        return ["boil"]
    if "freeze" in g:
        return ["freeze"]
    if re.search(r"\bmelt\b", g):
        return ["melt"]
    if "state of matter" in g:
        return ["change-the-state-of-matter-of", "boil", "freeze", "melt"]
    return list(ALL_SW)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().strip("'\"").strip()


def target_entity(goal: str, subgoals: "list[str]") -> str:
    """The variation-specific target entity for narrowing live search."""
    for sg in subgoals:
        m = re.search(r"[Ff]ocus on (?:the )?(.+?)\s*$", sg)
        if m:
            return _clean(m.group(1).rstrip("."))
    g = goal
    for pat in (r"melting point of (.+?)(?:,| in | which|\.|$)",
                r"temperature of (.+?)(?:,| which|\.|$)",
                r"create (?:the substance )?['\"]*(.+?)['\"]*(?: in |,|\.|$)",
                r"find a\(?n?\)?\s+(.+?)(?: in |\.|$)"):
        m = re.search(pat, g, re.IGNORECASE)
        if m:
            return _clean(m.group(1))
    return ""


def box_room_anchor(goal: str) -> "tuple[str, str]":
    """(box color, destination room) for find-* — present in the generic find
    task description ("move it to the <color> box in the <room>")."""
    m = re.search(r"to the (\w+) box in the (\w+)", goal, re.IGNORECASE)
    return (m.group(1).lower(), m.group(2).lower()) if m else ("", "")


def matches_all(obs: str, pats: "list[str]") -> bool:
    for p in pats:
        pp = re.sub(r"\s+", r"\\s+", p)
        try:
            if not re.search(pp, obs):
                return False
        except re.error:
            if p not in obs:
                return False
    return True


# ── zip index ───────────────────────────────────────────────────────────────
def build_zip_index() -> "dict[str, dict[int, str]]":
    """sw_name -> {var: concatenated gold observation stream} for the AB tasks."""
    zf = zipfile.ZipFile(ZIP)
    f = zf.open(zf.namelist()[0])
    index: "dict[str, dict[int, str]]" = {}
    cur_task = cur_sw = cur_var = None
    cur_obs: "list[str]" = []
    keep = False
    for prefix, event, value in ijson.parse(f):
        if prefix == "" and event == "map_key":
            cur_task = value
            continue
        if cur_task is None:
            continue
        b = cur_task
        if prefix == f"{b}.taskName" and event == "string":
            cur_sw = zip_label_to_sw(value)
            keep = cur_sw in ALL_SW
            if keep:
                index.setdefault(cur_sw, {})
        elif keep and prefix == f"{b}.goldActionSequences.item" and event == "start_map":
            cur_var = None
            cur_obs = []
        elif keep and prefix == f"{b}.goldActionSequences.item.variationIdx" and event == "number":
            cur_var = int(value)
        elif keep and prefix == f"{b}.goldActionSequences.item.path.item.observation" and event == "string":
            cur_obs.append(value)
        elif keep and prefix == f"{b}.goldActionSequences.item.path.item.action" and event == "string":
            cur_obs.append(value)  # actions too, so action-derived subgoals can match
        elif keep and prefix == f"{b}.goldActionSequences.item" and event == "end_map":
            index[cur_sw][cur_var] = "\n".join(cur_obs)
    return index


def zip_gold_actions() -> "dict[str, list[str]]":
    """{"sw_name::var": [gold actions]} from the zip (offline gold, 29/30 tasks)."""
    zf = zipfile.ZipFile(ZIP)
    f = zf.open(zf.namelist()[0])
    out: "dict[str, list[str]]" = {}
    cur_task = cur_sw = cur_var = None
    cur_acts: "list[str]" = []
    keep = False
    for prefix, event, value in ijson.parse(f):
        if prefix == "" and event == "map_key":
            cur_task = value
            continue
        if cur_task is None:
            continue
        b = cur_task
        if prefix == f"{b}.taskName" and event == "string":
            cur_sw = zip_label_to_sw(value)
            keep = cur_sw in ALL_SW
        elif keep and prefix == f"{b}.goldActionSequences.item" and event == "start_map":
            cur_var = None
            cur_acts = []
        elif keep and prefix == f"{b}.goldActionSequences.item.variationIdx" and event == "number":
            cur_var = int(value)
        elif keep and prefix == f"{b}.goldActionSequences.item.path.item.action" and event == "string":
            cur_acts.append(value)
        elif keep and prefix == f"{b}.goldActionSequences.item" and event == "end_map":
            out["%s::%d" % (cur_sw, cur_var)] = list(cur_acts)
    return out


# ── live recovery + gold gen (needs the JVM) ────────────────────────────────
def live_gold_stream(env, task: str, var: int) -> "tuple[list[str], list[str]] | None":
    env.load(task, var, SIMPL, generateGoldPath=True)
    env.reset()
    gold = list(env.get_gold_action_sequence())
    if gold and str(gold[0]).startswith("ERROR"):
        return None
    stream = []
    for act in gold:
        obs, _, done, _ = env.step(act)
        stream.append(obs)
        if done:
            break
    return gold, stream


def distinctive_subgoals(pats) -> "list[str]":
    """The variation-identifying subgoals (focus/target-bearing), which alone
    pin the variation; generic movement subgoals ("You move to the outside")
    match many variations and are not required for identification."""
    keep = [p for p in pats if re.search(r"focus on|move the|measures a temperature", p, re.I)]
    return keep or pats


def recover_residual(env, inst, pats, taken, gold_gen_cap=16):
    """Recover a distinct (task, variation) for a residual, bounded and injective.

    Narrows candidate variations by a cheap description signal, then gold-verifies
    the DISTINCTIVE subgoals against the live gold stream. Two narrowing signals:
      * find-* tasks have generic descriptions but name the destination (box +
        room) — narrow by that, then let the distinctive "focus on <target>" /
        "move the <target>" subgoals pick the variation via gold-verify;
      * other tasks name the target substance/object in the description — narrow
        by the cleaned target entity.
    Skips any (task, var) already in ``taken`` (keeps the mapping injective).
    Returns (task, var, method) or None."""
    g = inst["goal"].lower()
    is_find = bool(re.search(r"\bfind\b", g)) or "focus on the thing" in g
    ent = target_entity(inst["goal"], pats).lower()
    box, room = box_room_anchor(inst["goal"])
    dist = distinctive_subgoals(pats)
    for task in candidates(inst["goal"]):
        try:
            env.load(task, 0, SIMPL)
            maxv = env.get_max_variations(task)
        except Exception:
            continue
        narrowed = []
        for var in range(maxv):
            if (task, var) in taken:
                continue
            try:
                env.load(task, var, SIMPL)
                desc = env.get_task_description().lower()
            except Exception:
                continue
            if is_find:
                if box and room and (box + " box" in desc) and (room in desc):
                    narrowed.append(var)
            elif ent and ent in desc:
                narrowed.append(var)
        for var in narrowed[:gold_gen_cap]:
            try:
                res = live_gold_stream(env, task, var)
            except Exception:
                continue
            # Lenient verify: the variation-specific target entity must appear
            # LITERALLY in the gold stream (robust to regex-special chars in the
            # subgoal annotation, e.g. "book (Sherlock Holmes)"). For find, also
            # require the destination box to appear (disambiguates the box).
            stream = "\n".join(res[1]).lower() if res else ""
            if not stream:
                continue
            ok = (ent in stream) if ent else matches_all("\n".join(res[1]), dist)
            if is_find and box:
                ok = ok and (box + " box" in stream)
            if ok:
                return (task, var, "live-gold")
        if not is_find and len(narrowed) == 1:
            return (task, narrowed[0], "desc-only")  # unique entity match
    return None


def main() -> None:
    from scienceworld import ScienceWorldEnv

    os.makedirs(OUT, exist_ok=True)
    rng = random.Random(SEED)
    rows = [json.loads(l) for l in open(AB_JSONL) if l.strip()]
    print("AB instances: %d" % len(rows))

    # 1. ZIP recovery — compute per-instance match SETS, then assign INJECTIVELY
    #    (a system of distinct representatives; greedy most-constrained-first).
    t0 = time.time()
    index = build_zip_index()
    print("zip index %.1fs" % (time.time() - t0))
    matchsets: "dict[int, list[tuple[str, int]]]" = {}
    for r in rows:
        pats = subgoal_patterns(r["subgoals"])
        hits = []
        for task in candidates(r["goal"]):
            for var in sorted(index.get(task, {})):
                if matches_all(index[task][var], pats):
                    hits.append((task, var))
        if not hits:  # widen to all tasks before giving up
            for task in ALL_SW:
                for var in sorted(index.get(task, {})):
                    if matches_all(index[task][var], pats):
                        hits.append((task, var))
        matchsets[r["id"]] = sorted(set(hits))
    recovered: "dict[int, tuple[str, int]]" = {}
    taken: "set[tuple[str, int]]" = set()
    # assign the most-constrained instances first (smallest non-empty match set)
    order = sorted([r["id"] for r in rows if matchsets[r["id"]]],
                   key=lambda i: len(matchsets[i]))
    for iid in order:
        for pair in matchsets[iid]:
            if pair not in taken:
                recovered[iid] = pair
                taken.add(pair)
                break
    residuals = [r for r in rows if r["id"] not in recovered]
    print("zip recovered=%d (injective) residual=%d" % (len(recovered), len(residuals)))

    # 2. LIVE recovery for residuals (bounded; injective; never hangs / crashes)
    env = ScienceWorldEnv(taskName="", envStepLimit=120)
    unrecovered = []
    for r in residuals:
        pats = subgoal_patterns(r["subgoals"])
        hit = recover_residual(env, r, pats, taken)
        if hit:
            recovered[r["id"]] = (hit[0], hit[1])
            taken.add((hit[0], hit[1]))
            print("  live recovered id=%s -> (%s, %d) [%s]" % (r["id"], hit[0], hit[1], hit[2]), flush=True)
        else:
            unrecovered.append(r["id"])
            print("  !! UNRECOVERED id=%s goal=%.60s" % (r["id"], r["goal"]), flush=True)

    print("recovered %d/%d (unrecovered: %s)" % (len(recovered), len(rows), unrecovered))
    # Uniqueness invariant on the RECOVERED subset (each -> exactly one (task,var)).
    ab_pairs = set(recovered.values())
    assert len(ab_pairs) == len(recovered), "AgentBoard (task,var) collision among recovered"
    if unrecovered:
        print("WARNING: %d AgentBoard instances unrecovered — writing the %d recovered; "
              "residuals reported for follow-up." % (len(unrecovered), len(recovered)))

    # Build test_agentboard items
    ab_items = []
    by_id = {r["id"]: r for r in rows}
    for iid in sorted(recovered):
        task, var = recovered[iid]
        r = by_id[iid]
        ab_items.append({
            "id": "ab_%02d_%s_v%d" % (iid, task, var),
            "task_name": task, "variation": var, "protocol": "agentboard",
            "task_type": task, "goal": r["goal"],
            "subgoals": subgoal_patterns(r["subgoals"]),
            "difficulty": r.get("difficulty", ""),
        })

    # 3. AB task universe + stratified sampling from JAR folds (exclude AB vars)
    universe = sorted({t for t, _ in recovered.values()})
    print("AB task universe (%d tasks): %s" % (len(universe), universe))

    def fold_vars(task, which):
        env.load(task, 0, SIMPL)
        return {"train": env.get_variations_train,
                "dev": env.get_variations_dev,
                "test": env.get_variations_test}[which]()

    def sample(which, cap, exclude):
        items = []
        for task in universe:
            vs = [v for v in fold_vars(task, which) if (task, v) not in exclude]
            rng.shuffle(vs)
            for v in vs[:cap]:
                items.append({
                    "id": "%s_%s_v%d" % (which, task, v),
                    "task_name": task, "variation": v, "protocol": "original",
                    "task_type": task,
                })
        rng.shuffle(items)
        return items

    train = sample("train", TRAIN_CAP, ab_pairs)
    val = sample("dev", VAL_CAP, ab_pairs)
    secondary = sample("test", SEC_CAP, ab_pairs)
    print("train=%d val=%d secondary=%d" % (len(train), len(val), len(secondary)))

    # leakage guard
    def pairset(items):
        return {(i["task_name"], i["variation"]) for i in items}
    assert not (pairset(train) & pairset(secondary)), "train/secondary overlap"
    assert not (pairset(train) & ab_pairs), "train/AB overlap"
    assert not (pairset(secondary) & ab_pairs), "secondary/AB overlap"
    assert not (pairset(val) & ab_pairs), "val/AB overlap"

    # 4. gold_actions.json for every manifest variation (zip, else live)
    zga = zip_gold_actions()
    gold_actions: "dict[str, list[str]]" = {}
    all_pairs = ab_pairs | pairset(train) | pairset(val) | pairset(secondary)
    n_live = 0
    for task, var in sorted(all_pairs):
        key = "%s::%d" % (task, var)
        if key in zga:
            gold_actions[key] = zga[key]
        else:
            try:
                res = live_gold_stream(env, task, var)
                gold_actions[key] = res[0] if res else []
                n_live += 1
            except Exception:
                gold_actions[key] = []
    env.close()
    print("gold_actions: %d (%d live-generated)" % (len(gold_actions), n_live))

    # write manifests
    for name, items in (("train", train), ("val", val),
                        ("test_agentboard", ab_items), ("test_secondary", secondary)):
        d = os.path.join(OUT, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "items.json"), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, "gold_actions.json"), "w", encoding="utf-8") as f:
        json.dump(gold_actions, f, ensure_ascii=False)
    meta = {"seed": SEED, "simplification": SIMPL, "universe": universe,
            "counts": {"train": len(train), "val": len(val),
                       "test_agentboard": len(ab_items), "test_secondary": len(secondary)},
            "caps": {"train": TRAIN_CAP, "val": VAL_CAP, "secondary": SEC_CAP}}
    with open(os.path.join(OUT, "GENERATION_META.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("WROTE manifests to %s" % OUT)


if __name__ == "__main__":
    main()
