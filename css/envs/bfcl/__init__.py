"""BFCL multi-turn function-calling environment (concrete ``TaskEnv``).

Short scripted-multi-turn dialogs where a frozen model calls JSON functions
against 8 in-process simulated backends; scored deterministically by per-turn
state + response-subsequence checks (no user-simulator LLM). See
docs/env_prep/bfcl_PREP.md for the full onboarding audit.
"""
