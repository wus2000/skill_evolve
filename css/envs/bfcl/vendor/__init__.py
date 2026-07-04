"""Vendored BFCL multi-turn backend classes (Apache-2.0).

Copied verbatim from ShishirPatil/gorilla @ commit
6ea57973c7a6097fd7c5915698c54c17c5b1b6c8 (git describe v1.3-48-g6ea5797),
path berkeley-function-call-leaderboard/bfcl_eval/eval_checker/multi_turn_eval/
func_source_code/. See LICENSE (Apache-2.0) in this directory and
docs/env_prep/bfcl_PREP.md for provenance.

Two minimal, behaviour-preserving changes were made to the upstream files:
  1. the four backends that pull the distractor-padding constants rewrote
     ``from bfcl_eval.eval_checker.multi_turn_eval.func_source_code.long_context
     import`` to the relative ``from .long_context import`` (self-contained, no
     dependency on an installed ``bfcl_eval``);
  2. one annotation in ``vehicle_control.py`` (``lockDoors(..., door: list[str])``)
     was written in the file's own typing style (``List[str]``) so the package
     imports in-process under a Python 3.8 harness (upstream targets >=3.10).
Everything else is byte-identical.

The 8 stateful multi-turn backends and their func-call schemas live here; the
per-turn state/response checker that drives them lives in
``css/envs/bfcl/checker.py`` (adapted to run on fresh per-rollout instances
instead of the upstream module-``globals()`` registry). Dependency: ``mpmath``
(math_api only) + stdlib.
"""
