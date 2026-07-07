"""RulesDocV3 — the sole structural authority over rules.md (DSP, design §3).

Structure stops at ``### section``: a section = one heading line + free-form
markdown content (paragraphs, code fences, ``####``, lists — anything). The
only reserved token is a fence-outside line starting ``### ``. Consequently
THERE IS NO INVALID DOCUMENT: any text parses into a legal section list, and
an LLM output that happens to contain a stray heading merely splits into an
extra section next round (self-healing; per user ruling, no sanitization
gates).

Rule code owns parse / handle issue / render / apply / serialize; LLMs see
the rendered text with ``[S#k]`` handles and submit plain content — layout is
applied by the serializer, and the parser thereafter only ever reads its own
serializer's output (``parse(serialize(doc)) == doc`` is a test anchor).
Handles are per-pipeline-run (reissued each step); a section's durable
identity is its title + content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from css.markdown_utils import _fenced_spans, _in_spans

# A section heading: exactly three '#' at line start followed by whitespace.
_H3_RE = re.compile(r"^###[ \t]+(.*?)[ \t]*$", re.MULTILINE)

_EMPTY_NOTICE = ("(the rules document is EMPTY — there are no sections yet; "
                 "only add_section operations are possible)")


@dataclass
class Section:
    """One ### section: title + free-form body (verbatim, no heading line)."""

    title: str
    body: str = ""


@dataclass
class RulesDocV3:
    """Parsed rules document: an ordered list of sections + a preamble.

    ``preamble`` (text before the first heading) is preserved verbatim for
    legacy documents; the serializer emits it first. New documents produced
    through the pipeline never grow one.
    """

    sections: "list[Section]" = field(default_factory=list)
    preamble: str = ""

    # ── parse / serialize (round-trip pair) ────────────────────────────────
    @classmethod
    def parse(cls, text: str) -> "RulesDocV3":
        text = text or ""
        fenced = _fenced_spans(text)
        heads = [m for m in _H3_RE.finditer(text)
                 if not _in_spans(m.start(), fenced)]
        if not heads:
            return cls(sections=[], preamble=text.strip())
        preamble = text[: heads[0].start()].strip()
        sections: "list[Section]" = []
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            body = text[m.end():end].strip("\n")
            sections.append(Section(title=m.group(1).strip(),
                                    body=body.strip()))
        return cls(sections=sections, preamble=preamble)

    def serialize(self) -> str:
        parts: "list[str]" = []
        if self.preamble.strip():
            parts.append(self.preamble.strip())
        for s in self.sections:
            block = "### %s" % s.title.strip()
            if s.body.strip():
                block += "\n" + s.body.strip()
            parts.append(block)
        return ("\n\n".join(parts) + "\n") if parts else ""

    # ── handles ────────────────────────────────────────────────────────────
    def handle(self, index: int) -> str:
        return "S#%d" % (index + 1)

    def handle_map(self) -> "dict[str, int]":
        return {self.handle(i): i for i in range(len(self.sections))}

    def resolve(self, handle: str) -> "Optional[int]":
        m = re.fullmatch(r"S#(\d+)", (handle or "").strip())
        if not m:
            return None
        idx = int(m.group(1)) - 1
        return idx if 0 <= idx < len(self.sections) else None

    def render(self) -> str:
        """Handle-annotated rendering (what every LLM sees)."""
        if not self.sections and not self.preamble.strip():
            return _EMPTY_NOTICE
        parts: "list[str]" = []
        if self.preamble.strip():
            parts.append(self.preamble.strip())
        for i, s in enumerate(self.sections):
            block = "### [%s] %s" % (self.handle(i), s.title.strip())
            if s.body.strip():
                block += "\n" + s.body.strip()
            parts.append(block)
        return "\n\n".join(parts)

    def catalog(self) -> str:
        """Handle + title lines (the placement catalog)."""
        if not self.sections:
            return _EMPTY_NOTICE
        return "\n".join("[%s] %s" % (self.handle(i), s.title.strip())
                         for i, s in enumerate(self.sections))

    # ── operations (rule-side structural apply) ────────────────────────────
    def add_section(self, title: str, content: str) -> None:
        self.sections.append(Section(title=title.strip(),
                                     body=(content or "").strip()))

    def replace_body(self, index: int, content: str) -> None:
        self.sections[index].body = (content or "").strip()

    def remove_section(self, index: int) -> None:
        del self.sections[index]
