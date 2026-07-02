# Improvement Candidates (recorded, not yet implemented)

Candidate mechanisms agreed to be worth trying, deferred until the current
upgrade wave (point-edit merger + independence validation + verification
signal floor + paired gate) has produced ablation data.

---

## 1. Amortized gate — accumulate-then-test

**Problem addressed.** Even with the paired sign-test gate, a single L0
step's true effect (typically ±0.3–1pp) can sit below any affordable gate's
resolution. Per-step gating then rejects real-but-small improvements
("cannot certify what cannot be measured"), and compute is spent on gate
evaluations that are structurally incapable of firing. This is the
measurement-timescale mismatch behind the earlier diagnosis "15K盲刀只换不攒"
— per-step effects are individually sub-measurable but collectively real.

**Mechanism sketch.**

```
gate_every_n_steps = N (e.g. 4):
  within a window: the (signal-floored) per-edit ablation verification is the
    ONLY step-level filter — surviving edits apply directly, no per-step gate
  every N steps: full paired gate compares "rules after N steps of accepted
    edits" vs the checkpoint taken N steps ago
    pass -> advance the checkpoint
    fail -> roll back node.rules to the checkpoint (the window's edits are
            discarded wholesale)
```

**Why it works.** Effects accumulate linearly over the window while gate
noise stays constant, so the cumulative delta crosses the sign test's
detection threshold; gate cost is divided by N. Risk: a harmful edit rides
along for up to N steps — mitigated by the verification layer's padded
control tasks (regression guard) and bounded by the rollback.

**Config surface.** `gate_every_n_steps: int = 1` (1 = current per-step
behavior; ablate with 4). Composes with `gate_mode="paired"`.

**Prerequisite before trying.** Verification-layer false-pass rate must be
low (B1 floor + B2 net-flips criterion deployed), otherwise the window
accumulates noise edits and the periodic gate rejects every window.

---

## 2. Group-test verification — bundle-then-split attribution

**Problem addressed.** The granularity dilemma: verifying every point edit
alone starves each verification of signal; verifying section-level bundles
loses attribution (a failing bundle discards its good members — the original
motivation for point edits).

**Mechanism sketch.** Verify at BUNDLE level first (e.g. all surviving
proposals for one section, on the union of their target tasks). If the
bundle passes, apply it whole — attribution inside a passing bundle is
optional. If the bundle fails WITH MIXED SIGNAL (some GAINED, some LOST),
binary-split it and re-verify the halves on their sub-unions (at most one
split level, budget-bounded); uniform-null bundles are dropped without
splitting.

**Why deferred.** Adds a second verification round-trip and scheduling
complexity; only worth it if BOTH granularity modes (point / section) prove
unsatisfactory in the ablation runs.
