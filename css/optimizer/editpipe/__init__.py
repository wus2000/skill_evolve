"""editpipe — v2 edit pipeline: merger -> syntax gate -> semantic adjudication
-> deterministic apply, with a full per-edit audit trail.

Design axioms (each module enforces one or more):

1. SINGLE SOURCE OF IDENTITY. An edit's identity is its ``subject`` field and
   nothing else. Bodies never embed headings; headings are rendered from
   subjects at document-assembly time (``render.py``). The legacy failure mode
   "section_target field disagrees with the heading inside content" is not
   representable.

2. MECHANICAL LAYER = SYNTAX GATE ONLY. Deterministic code checks syntax,
   executability and literal duplication — and *detects* semantic conflicts —
   but never adjudicates them. The only mechanical drop is a byte-identical
   duplicate (``schema.py``).

3. SEMANTIC RULINGS BELONG TO THE LLM. Identity collisions, purity issues and
   overlaps are violations fed to the adjudication loop (``adjudicate.py``),
   which repairs with per-edit ID-diff operations and records a rationale for
   every kill. Nothing is silently discarded.

4. CONSTRAINTS ARE ENFORCED BY PURPOSE, NOT FORM. The ablation requirement is
   that edits act on pairwise-disjoint document regions; the disjointness
   table in ``schema.py`` encodes exactly that, so two *different* new
   sections never conflict merely because a field collided.

5. LLMs NEVER DO MECHANICAL TRANSCRIPTION. Section-level apply is
   deterministic (``apply.py``); an LLM is consulted only for semantic anchor
   resolution inside a single section, and its blast radius is that section.
   Post-apply structure assertions guarantee the document invariants.

6. FAILURE DEGRADES POSITION, NEVER CONTENT. Wrong placement falls back to
   document end; unresolvable anchors degrade to an append within the target
   section; adjudication non-convergence falls back to keep-max-support +
   demote-rest. Every degradation is audited.
"""
from css.optimizer.editpipe.schema import (
    EDIT_KINDS,
    POINT_KINDS,
    SECTION_KINDS,
    EditAudit,
    SectionEdit,
    detect_conflicts,
    syntax_gate,
)
from css.optimizer.editpipe.render import (
    DocSection,
    RulesDoc,
    assert_structure,
    normalize_subject,
    subject_key,
)

__all__ = [
    "EDIT_KINDS",
    "POINT_KINDS",
    "SECTION_KINDS",
    "EditAudit",
    "SectionEdit",
    "detect_conflicts",
    "syntax_gate",
    "DocSection",
    "RulesDoc",
    "assert_structure",
    "normalize_subject",
    "subject_key",
]
