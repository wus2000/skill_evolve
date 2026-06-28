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

_REPAIR_SYSTEM = """You are a JSON repair specialist. Another model was given a task whose REQUIRED OUTPUT was a single JSON value (object or array), and it produced a response that is malformed: a strict JSON parser rejects it, or parses it into a corrupted structure (a common cause: an unescaped double-quote inside a string value leaks, prematurely ends the string, and splits the remainder into spurious keys).

You are given TWO things:
  (1) the ORIGINAL REQUEST that model received — its instructions, the exact output format/schema it was told to produce, and the source material it analyzed; and
  (2) its MALFORMED RESPONSE.
Return a corrected, strictly-valid JSON that is FAITHFUL to what that model intended.

REPAIR — do NOT REDO. Obey all:
1. AUTHORITY OF CONTENT: the MALFORMED RESPONSE is the sole authority for content and conclusions. Preserve its analysis, wording, values and conclusions wherever legible. Never re-analyze the task, change a conclusion, add a finding, drop content, or "improve" the answer. You recover what the model already wrote.
2. ROLE OF THE REQUEST (context only): use the ORIGINAL REQUEST solely to (a) determine the EXACT intended output shape — keys, nesting, types from its format spec — so your JSON conforms to it; and (b) faithfully RESTORE pieces of the response that are corrupted/truncated: prefer reconstructing from the response itself; only when a fragment is unreadable, recover it from the source material in the request that the response was visibly reproducing (a quoted formula, citation, or value). Do NOT answer the request yourself or import content the response did not use.
3. STRUCTURAL FIXES ONLY: inside string values escape inner double-quotes (\\"), stray backslashes, literal newlines/tabs; add missing commas/colons; balance/add brackets/braces; remove trailing commas; normalize single-quoted strings/keys, Python True/False/None, comments and stray prose/code-fences; when a key is duplicated because an unescaped quote split a string, keep the FIRST complete, coherent occurrence and discard the spurious fragments.
4. SHAPE: conform to the output shape the ORIGINAL REQUEST specifies (object vs array, exact key names, full nesting).
5. TRUNCATION: if cut off mid-structure, restore the missing remainder from the response/source when unambiguous, else close open structures minimally. Never fabricate conclusions.

Output ONLY the corrected JSON value — no commentary, no markdown code fences."""


def _build_repair_user(orig_system: str, orig_user: str, malformed: str) -> str:
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
        + (malformed or "") +
        "\n\n==================== YOUR OUTPUT ====================\n"
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
) -> str | None:
    """Ask the optimizer model to repair its own malformed JSON response.

    Returns the repaired response TEXT (still to be parsed by the caller's own
    parser), or ``None`` if no repair could be obtained. Never raises.
    """
    if not (malformed or "").strip():
        return None
    user = _build_repair_user(orig_system, orig_user, malformed)
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
    max_tokens: int = 4096,
    repair_max_tokens: int = DEFAULT_REPAIR_MAX_TOKENS,
    stage: str = "",
) -> Any:
    """Call ``complete_optimizer`` for JSON, with one context-aware repair retry.

    1. Calls the optimizer, parses the response with ``parse``.
    2. If the parsed result is usable (``ok(result)`` truthy, default: ``bool``),
       returns it.
    3. Otherwise asks the model to repair its own output (passing the ORIGINAL
       request as context), re-parses, and returns the repaired result if usable.
    4. Falls back to the original (unusable) parse result if repair did not help,
       so the call site's existing downstream handling is preserved.

    ``parse`` is the call site's existing parser; ``ok`` expresses what "usable"
    means for that site (default: any truthy/non-empty value).
    """
    usable = ok if ok is not None else (lambda r: bool(r))
    text, _usage = client.complete_optimizer(system, user, max_tokens=max_tokens)
    result = parse(text)
    if usable(result):
        return result
    repaired = repair_json_via_llm(
        client, system, user, text, max_tokens=repair_max_tokens, stage=stage
    )
    if repaired is not None:
        repaired_result = parse(repaired)
        if usable(repaired_result):
            _log.info(
                "[json-repair:%s] recovered a malformed JSON response via LLM repair",
                stage or "?",
            )
            return repaired_result
    return result
