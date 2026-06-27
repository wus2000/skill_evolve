"""Prompt construction + code extraction for SpreadsheetBench codegen rollouts.

Self-contained CSS copy of the pure (LLM-free) helpers the rollout needs:
``_build_system`` / ``_build_user`` / ``_preview_workbook`` / ``extract_code``.
The system prompt is loaded from the local ``prompts/`` directory — there is no
dependency on any external prompt framework.
"""
from __future__ import annotations

import os

import openpyxl


# ── Local prompt loading ────────────────────────────────────────────────────

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_prompt_cache: dict[str, str] = {}


def load_prompt(name: str) -> str:
    """Load a prompt ``{name}.md`` from this env's local ``prompts/`` directory."""
    if name in _prompt_cache:
        return _prompt_cache[name]
    path = os.path.join(_PROMPTS_DIR, f"{name}.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    _prompt_cache[name] = content
    return content


# ── Workbook preview ────────────────────────────────────────────────────────

def _preview_workbook(path: str, max_rows: int = 5, max_cols: int = 20) -> str:
    """Generate a text preview of the first few rows of each sheet."""
    wb = openpyxl.load_workbook(path, data_only=False)
    chunks: list[str] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        chunks.append(
            f"## Sheet: {sheet_name}  "
            f"(dim={ws.dimensions}, max_row={ws.max_row}, max_col={ws.max_column})"
        )
        for row in ws.iter_rows(
            min_row=1,
            max_row=min(ws.max_row, max_rows),
            max_col=min(ws.max_column, max_cols),
            values_only=False,
        ):
            cells = []
            for cell in row:
                v = cell.value
                if v is None:
                    cells.append(f"{cell.coordinate}=")
                else:
                    s = str(v)
                    if len(s) > 40:
                        s = s[:37] + "..."
                    cells.append(f"{cell.coordinate}={s}")
            chunks.append(" | ".join(cells))
        if ws.max_row > max_rows:
            chunks.append(f"... ({ws.max_row - max_rows} more rows)")
        chunks.append("")
    wb.close()
    return "\n".join(chunks)


# ── Code extraction (same as official prompt.py) ────────────────────────────

def extract_code(text: str) -> str:
    """Extract the first ```python``` fenced code block from LLM output."""
    if "```" not in text:
        return text.strip()
    start = text.find("```")
    nl = text.find("\n", start)
    end = text.find("```", nl + 1)
    if nl == -1 or end == -1:
        return text.strip()
    return text[nl + 1 : end].strip()


# ── Prompt construction (official SpreadsheetBench prompts) ─────────────────

def _build_system(skill_content: str) -> str:
    base = load_prompt("codegen_system")
    if skill_content.strip():
        base += f"\n\n## Skill\n{skill_content.strip()}"
    return base


def _build_user(
    instruction: str,
    input_xlsx: str,
    instruction_type: str = "",
    answer_position: str = "",
    diagnostic_mode: bool = False,
    diagnostic_instruction: str = "",
    diagnostic_trace_context: str = "",
) -> str:
    try:
        preview = _preview_workbook(input_xlsx)
    except Exception as e:  # noqa: BLE001
        preview = f"(failed to preview workbook: {e})"
    extra = ""
    if instruction_type:
        extra += f"\nInstruction type: {instruction_type}"
    if answer_position:
        extra += f"\nExpected answer position: {answer_position}"
    task_suffix = "Return only a ```python``` code block."
    diagnostic = ""
    if diagnostic_mode and diagnostic_instruction.strip():
        task_suffix = (
            "First provide a short diagnostic readout that follows the training "
            "instruction below, then return a single complete ```python``` code block."
        )
        diagnostic = f"\n\n# Training readout\n{diagnostic_instruction.strip()}"
    prefix = ""
    if diagnostic_trace_context.strip():
        prefix = (
            "# Previous Trace Snapshot\n"
            "This is a partial transcript from an earlier attempt. Use it as your current reasoning context.\n\n"
            f"{diagnostic_trace_context.strip()}\n\n"
        )
    return (
        f"{prefix}"
        f"# Instruction\n{instruction}\n{extra}\n\n"
        f"# Input spreadsheet preview\n{preview}\n\n"
        "# Task\n"
        "Write a Python script that reads the workbook from the variable `INPUT_PATH`, "
        "applies the instruction, and writes the modified workbook to `OUTPUT_PATH`. "
        "Preserve all other cells unchanged. "
        "The preview may be truncated — do not hardcode row counts or assume the data ends at the last previewed row; "
        "iterate over all actual rows in the workbook instead. "
        f"{task_suffix}"
        f"{diagnostic}"
    )
