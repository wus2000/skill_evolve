"""ALFWorld environment worker — a standalone one-episode subprocess CLI.

WHY A SUBPROCESS CLI (not threads, not multiprocessing.Queue):
  * The TextWorld/ALFWorld engine is NOT thread-safe (the tatsu grammar parser
    keeps process-global state; concurrent create OR step crashes — measured
    1/16 survival). Process isolation is the only safe concurrency model.
  * ``multiprocessing`` spawn would re-import the parent's interpreter — but
    the harness may run on a Python without textworld installed (the server's
    css process is system 3.8; alfworld needs >=3.9). A plain ``subprocess``
    with a configurable interpreter (``cfg.extra["alfworld_python"]``) fully
    decouples the two environments: css itself NEVER imports textworld.
  * ``fork`` from the harness's multithreaded rollout process is unsafe anyway.

PROTOCOL (JSON lines):
  parent -> child stdin : one environment command per line;
                          the literal ``__CLOSE__`` terminates the episode.
  child  -> parent fd   : one JSON object per line on the child's ORIGINAL
                          stdout (duplicated before fd redirection):
      {"event": "ready", "obs": str, "admissible": [str, ...]}
      {"event": "step", "obs": str, "done": bool, "won": bool,
       "admissible": [str, ...]}
      {"event": "fatal", "error": str}

GOLD-REPLAY MODE (``--gold`` as the third argv): no stdin interaction — the
worker itself follows the built-in handcoded expert (AlfredExpert) to the end
and emits a single event, then exits:
      {"event": "gold", "won": bool,
       "steps": [{"action": str, "obs": str}, ...]}
This produces the ground-truth EPISODE trajectory (executable commands + the
observations they yield) used by the env's optimizer-only eval annotation in
``alfworld_gt_mode="episode"`` (vs the high-level plan string in mode "plan").

  All other child output is silenced at the FD level: the engine (and the
  Fast Downward grounder it shells out to) prints noise to fd1/fd2, which
  would corrupt a line protocol. We dup the real stdout for the protocol,
  then point fd1 at fd2 so every stray write (Python or C or subprocess)
  lands on stderr, which the parent discards.

This module imports ONLY the standard library at module level; textworld /
alfworld load lazily inside main() in the worker interpreter.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

CLOSE_SENTINEL = "__CLOSE__"


def _protocol_channel():
    """Duplicate the real stdout for protocol lines, then fd1 -> fd2."""
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return proto


def _emit(proto, payload: dict) -> None:
    proto.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proto.flush()


def _gold_replay(proto, gamefile: str, max_steps: int) -> int:
    """Follow the handcoded expert to the end; emit one ``gold`` event."""
    import textworld
    import textworld.gym
    from alfworld.agents.environment.alfred_tw_env import (
        AlfredDemangler, AlfredExpert, AlfredExpertType)

    request = textworld.EnvInfos(
        won=True, admissible_commands=True, extras=["expert_plan"])
    env_id = textworld.gym.register_game(
        gamefile, request, max_episode_steps=max_steps,
        wrappers=[AlfredDemangler(),
                  AlfredExpert(expert_type=AlfredExpertType.HANDCODED)])
    env = textworld.gym.make(env_id)
    _obs, infos = env.reset()
    steps = []
    won = False
    for _ in range(max_steps):
        plan = infos.get("extra.expert_plan") or []
        if not plan:
            break
        action = str(plan[0])
        obs, _score, done, infos = env.step(action)
        steps.append({"action": action, "obs": obs})
        if done:
            won = bool(infos.get("won", False))
            break
    _emit(proto, {"event": "gold", "won": won, "steps": steps})
    return 0


def main() -> int:
    proto = _protocol_channel()
    try:
        gamefile = sys.argv[1]
        max_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        if len(sys.argv) > 3 and sys.argv[3] == "--gold":
            return _gold_replay(proto, gamefile, max_steps)

        import textworld
        import textworld.gym
        from alfworld.agents.environment.alfred_tw_env import AlfredDemangler

        request = textworld.EnvInfos(won=True, admissible_commands=True)
        env_id = textworld.gym.register_game(
            gamefile,
            request,
            max_episode_steps=max_steps,
            wrappers=[AlfredDemangler()],
        )
        env = textworld.gym.make(env_id)
        obs, infos = env.reset()
        _emit(proto, {
            "event": "ready",
            "obs": obs,
            "admissible": list(infos.get("admissible_commands", [])),
        })
    except Exception:  # noqa: BLE001 - report through the protocol, then die
        _emit(proto, {"event": "fatal", "error": traceback.format_exc()})
        return 1

    for line in sys.stdin:
        command = line.rstrip("\n")
        if command == CLOSE_SENTINEL:
            break
        try:
            obs, _score, done, infos = env.step(command)
            _emit(proto, {
                "event": "step",
                "obs": obs,
                "done": bool(done),
                "won": bool(infos.get("won", False)),
                "admissible": list(infos.get("admissible_commands", [])),
            })
        except Exception:  # noqa: BLE001
            _emit(proto, {"event": "fatal", "error": traceback.format_exc()})
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
