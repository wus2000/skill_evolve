"""Context-aware LLM JSON repair (the single, unified JSON-repair mechanism).

When an *optimizer* LLM call was supposed to return JSON but the response cannot
be parsed into a usable value, we re-ask the SAME model to repair its OWN output.
Unlike a blind syntactic repair, we hand the repair model BOTH:

  (1) the ORIGINAL request (its instructions, the required output format/schema,
      and the source material it was analysing), and
  (2) the malformed response,

and frame it strictly as "REPAIR, do not REDO": the malformed response is the
authority on content/conclusions; the original request is context used only to
(a) know the exact intended output shape and (b) faithfully RESTORE fragments the
response corrupted or truncated (recovering them from the source material).

Validated experimentally (see the llm_repair_v2 test record): faithful to the
original content, recovers content that pure parsing cannot (e.g. evidence whose
quotes broke the JSON), and needs no separate schema argument because the shape
is already in the original request.

Usage — the common pattern at a call site changes from::

    text, _ = client.complete_optimizer(SYSTEM, user, max_tokens=N)
    result = _parse_fn(text)

to::

    result = complete_optimizer_json(
        client, SYSTEM, user, parse=_parse_fn, max_tokens=N, stage="derive")

``complete_optimizer_json`` calls the model, parses, and — only if the parse is
unusable — runs one context-aware repair and re-parses, all transparently.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

_log = logging.getLogger("css.json_repair")

# The repaired JSON is at least as long as the input (escaping makes it longer).
# Keep this generous so the repair is not itself truncated; the validated cases
# needed ~7.5k–8.7k completion tokens.
DEFAULT_REPAIR_MAX_TOKENS = 16384

_REPAIR_SYSTEM = """You are a JSON repair specialist. Another model was given a task whose REQUIRED OUTPUT was a single JSON value (object or array), and it produced a response that needs repair. There are two cases. (1) MALFORMED: a strict JSON parser rejects it, or parses it into a corrupted structure (a common cause: an unescaped double-quote inside a string value leaks, prematurely ends the string, and splits the remainder into spurious keys). (2) INCOMPLETE: it is valid JSON but OMITS one or more REQUIRED fields named in the schema. When a "WHAT IS WRONG" section appears below listing missing required fields, ADD exactly those fields (and only those), recovering each value faithfully from the response and the source material — never invent values — and leave every other field byte-for-byte unchanged.

You are given TWO things:
  (1) the ORIGINAL REQUEST that model received — its instructions, the exact output format/schema it was told to produce, and the source material it analyzed; and
  (2) its MALFORMED RESPONSE.
Return a corrected, strictly-valid JSON that is FAITHFUL to what that model intended.

REPAIR — do NOT REDO. Obey all:
1. AUTHORITY OF CONTENT: the MALFORMED RESPONSE is the sole authority for content and conclusions. Preserve its analysis, wording, values and conclusions wherever legible. Never re-analyze the task, change a conclusion, add a finding, drop content, or "improve" the answer. You recover what the model already wrote. (Sole exception: if a "WHAT IS WRONG" section names a REQUIRED field the response omitted, adding that one field — restored faithfully from the source material — is mandatory and does NOT count as "adding a finding".)
2. ROLE OF THE REQUEST (context only): use the ORIGINAL REQUEST solely to (a) determine the EXACT intended output shape — keys, nesting, types from its format spec — so your JSON conforms to it; and (b) faithfully RESTORE pieces of the response that are corrupted/truncated: prefer reconstructing from the response itself; only when a fragment is unreadable, recover it from the source material in the request that the response was visibly reproducing (a quoted formula, citation, or value). Do NOT answer the request yourself or import content the response did not use.
3. STRUCTURAL FIXES ONLY: inside string values escape inner double-quotes (\\"), stray backslashes, literal newlines/tabs; add missing commas/colons; balance/add brackets/braces; remove trailing commas; normalize single-quoted strings/keys, Python True/False/None, comments and stray prose/code-fences; when a key is duplicated because an unescaped quote split a string, keep the FIRST complete, coherent occurrence and discard the spurious fragments.
4. SHAPE: conform to the output shape the ORIGINAL REQUEST specifies (object vs array, exact key names, full nesting).
5. TRUNCATION: if cut off mid-structure, restore the missing remainder from the response/source when unambiguous, else close open structures minimally. Never fabricate conclusions.

Output ONLY the corrected JSON value — no commentary, no markdown code fences."""


def _build_repair_user(
    orig_system: str, orig_user: str, malformed: str, feedback: str = ""
) -> str:
    # Optional targeted feedback (e.g. the list of missing REQUIRED fields).
    # Placed right after the response so it is the most salient instruction.
    issue = ""
    if (feedback or "").strip():
        issue = (
            "\n\n==================== WHAT IS WRONG (fix exactly this) "
            "====================\n" + feedback.strip()
        )
    return (
        "==================== ORIGINAL REQUEST (context only - do NOT answer it) "
        "====================\n"
        "Use it only to learn the required output shape and the source material, "
        "for faithfully restoring corrupted parts of the response.\n\n"
        "----- original SYSTEM message -----\n" + (orig_system or "(none)") + "\n\n"
        "----- original USER message -----\n" + (orig_user or "(none)") + "\n\n"
        "==================== MALFORMED RESPONSE (repair THIS) ====================\n"
        "The model's response to the above request. It is the authority on "
        "content/conclusions; fix only its JSON form.\n\n"
        + (malformed or "")
        + issue
        + "\n\n==================== YOUR OUTPUT ====================\n"
        "Return ONLY the corrected, strictly-valid JSON value - faithful to the "
        "malformed response's content, conforming to the original request's "
        "required output shape."
    )


def repair_json_via_llm(
    client: Any,
    orig_system: str,
    orig_user: str,
    malformed: str,
    *,
    max_tokens: int = DEFAULT_REPAIR_MAX_TOKENS,
    stage: str = "",
    feedback: str = "",
) -> str | None:
    """Ask the optimizer model to repair its own malformed JSON response.

    ``feedback`` is optional targeted guidance injected into the repair prompt
    (e.g. a description of which REQUIRED fields the response omitted), used for
    schema/missing-field repair on otherwise valid JSON.

    Returns the repaired response TEXT (still to be parsed by the caller's own
    parser), or ``None`` if no repair could be obtained. Never raises.
    """
    if not (malformed or "").strip():
        return None
    user = _build_repair_user(orig_system, orig_user, malformed, feedback)
    try:
        text, _usage = client.complete_optimizer(
            _REPAIR_SYSTEM, user, max_tokens=max_tokens
        )
    except Exception as exc:  # noqa: BLE001 — repair is best-effort, never fatal
        _log.warning("[json-repair:%s] repair call failed: %s", stage, exc)
        return None
    if not (text or "").strip():
        return None
    return text


def complete_optimizer_json(
    client: Any,
    system: str,
    user: str,
    *,
    parse: Callable[[str], Any],
    ok: Callable[[Any], bool] | None = None,
    required: Callable[[Any], list] | None = None,
    max_tokens: int = 4096,
    repair_max_tokens: int = DEFAULT_REPAIR_MAX_TOKENS,
    stage: str = "",
) -> Any:
    """Call ``complete_optimizer`` for JSON, with context-aware repair retries.

    Two independent recovery triggers, each at most one extra LLM call:

    1. STRUCTURAL (syntax) — the response does not ``parse`` into a usable
       value (``ok(result)`` falsy, default: ``bool``). Ask the model to
       repair its own malformed JSON and re-parse. Pure format work — the
       repair prompt forbids new content ("REPAIR, do not REDO").
    2. CONTENT (missing required fields) — the response parses fine but
       ``required(result)`` reports missing REQUIRED fields. Missing fields
       are missing CONTENT, and content must come from the ORIGINAL call
       (decision log #14: regenerate-with-critique, never a third-party
       repair answering on the model's behalf): the original call is retried
       ONCE with the missing-field list appended as explicit feedback, and
       the retry is accepted only if it is usable AND complete.

    ``required`` is the call site's missing-required-field detector: given the
    parsed result it returns a list of human-readable descriptions of fields that
    are absent/empty but mandatory (``[]`` == complete). Optional and conditional
    fields MUST NOT be reported — only fields whose absence the call site cannot
    tolerate. When ``required`` is None (default) the schema check is skipped and
    behavior is identical to before for every existing call site.

    Falls back to the original result if a repair did not help, so each call
    site's existing downstream handling is preserved.
    """
    usable = ok if ok is not None else (lambda r: bool(r))
    text, _usage = client.complete_optimizer(system, user, max_tokens=max_tokens)
    result = parse(text)

    # Trigger 1 — STRUCTURAL: unparseable / unusable -> repair malformed JSON.
    if not usable(result):
        repaired = repair_json_via_llm(
            client, system, user, text, max_tokens=repair_max_tokens, stage=stage
        )
        if repaired is None:
            return result
        repaired_result = parse(repaired)
        if not usable(repaired_result):
            return result
        _log.info(
            "[json-repair:%s] recovered a malformed JSON response via LLM repair",
            stage or "?",
        )
        # Adopt the repaired text/result; it may still need a schema repair below.
        result, text = repaired_result, repaired

    # Trigger 2 — CONTENT: valid JSON, but missing REQUIRED field(s).
    # Retry the ORIGINAL call with the omission named (regenerate-with-
    # critique); a third-party repair must never author content on the
    # original model's behalf (decision log #14).
    if required is not None:
        missing = required(result)
        if missing:
            retry_user = (
                user
                + "\n\n=== YOUR PREVIOUS ANSWER WAS INCOMPLETE — ANSWER AGAIN "
                "IN FULL ===\n"
                "Your previous answer omitted the following REQUIRED "
                "field(s):\n- " + "\n- ".join(str(m) for m in missing)
                + "\nProduce the complete answer again, including these "
                "field(s) with substantive values grounded in the material "
                "above."
            )
            try:
                retry_text, _u = client.complete_optimizer(
                    system, retry_user, max_tokens=max_tokens)
            except Exception:  # noqa: BLE001 — retry is best-effort
                retry_text = ""
            if (retry_text or "").strip():
                retry_result = parse(retry_text)
                if usable(retry_result) and not required(retry_result):
                    _log.info(
                        "[json-repair:%s] recovered %d missing required "
                        "field(s) via ORIGINAL-call retry (content path)",
                        stage or "?", len(missing),
                    )
                    return retry_result
            _log.warning(
                "[json-repair:%s] %d required field(s) still missing after the "
                "original-call retry: %s",
                stage or "?", len(missing), "; ".join(str(m) for m in missing[:5]),
            )
    return result
