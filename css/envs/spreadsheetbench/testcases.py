"""Test-case discovery + output auto-verification for SpreadsheetBench.

Self-contained CSS copy of the two pure helpers the rollout needs:
``_find_test_cases`` (locate (input, answer) xlsx pairs on disk) and
``_auto_verify_output`` (human-readable cell-by-cell verification report
appended to the trajectory so the analyst can see what went wrong).
"""
from __future__ import annotations

import glob as _glob
import os

import openpyxl

from css.envs.spreadsheetbench.evaluator import _generate_cell_names


def _find_test_cases(task_dir: str) -> list[tuple[str, str, str]]:
    """Return [(case_no, input_path, answer_path), ...] sorted by case_no.

    Supports naming conventions used by SpreadsheetBench releases:
      * ``{no}_{id}_input.xlsx``  + ``{no}_{id}_answer.xlsx``  (original)
      * ``{no}_{id}_init.xlsx``   + ``{no}_{id}_golden.xlsx``  (verified_400)
      * ``initial.xlsx``          + ``golden.xlsx``             (verified_400, no prefix)
    """
    cases: list[tuple[str, str, str]] = []
    inputs = sorted(_glob.glob(os.path.join(task_dir, "*_input.xlsx")))
    for ip in inputs:
        no = os.path.basename(ip).split("_", 1)[0]
        ap = ip.replace("_input.xlsx", "_answer.xlsx")
        if os.path.exists(ap):
            cases.append((no, ip, ap))
    inits = sorted(_glob.glob(os.path.join(task_dir, "*_init.xlsx")))
    for ip in inits:
        no = os.path.basename(ip).split("_", 1)[0]
        ap = ip.replace("_init.xlsx", "_golden.xlsx")
        if os.path.exists(ap):
            cases.append((no, ip, ap))

    # Fallback: bare initial.xlsx + golden.xlsx (no numbered prefix)
    if not cases:
        bare_init = os.path.join(task_dir, "initial.xlsx")
        bare_gold = os.path.join(task_dir, "golden.xlsx")
        if os.path.exists(bare_init) and os.path.exists(bare_gold):
            cases.append(("1", bare_init, bare_gold))

    return cases


def _auto_verify_output(
    pred_path: str,
    gold_path: str,
    answer_position: str,
) -> str:
    """Reopen the predicted xlsx and compare cells at answer_position with gold.

    Returns a human-readable verification report that can be appended to the
    trajectory so the error analyst can see exactly what went wrong (e.g.
    ``cell A1: got=None, expected=420``).
    """
    if not os.path.exists(pred_path):
        return "Verification: output file does not exist."
    try:
        wb_pred = openpyxl.load_workbook(pred_path, data_only=True)
        wb_gold = openpyxl.load_workbook(gold_path, data_only=True)
    except Exception as e:
        return f"Verification: could not open workbooks: {e}"

    lines = ["## Output Verification"]
    try:
        for scr in (answer_position or "").split(","):
            scr = scr.strip()
            if not scr:
                continue
            if "!" in scr:
                sheet_name, cell_range = scr.split("!", 1)
                sheet_name = sheet_name.strip().strip("'\"")
            else:
                sheet_name = wb_gold.sheetnames[0]
                cell_range = scr
            cell_range = cell_range.strip().strip("'\"")

            cell_names = _generate_cell_names(cell_range)
            ws_pred = wb_pred[sheet_name] if sheet_name in wb_pred.sheetnames else None
            ws_gold = wb_gold[sheet_name] if sheet_name in wb_gold.sheetnames else None

            if ws_pred is None:
                lines.append(f"  Sheet '{sheet_name}' NOT FOUND in output.")
                continue

            for cn in cell_names:
                gv = ws_gold[cn].value if ws_gold else "N/A"
                pv = ws_pred[cn].value
                match = "✓" if repr(gv) == repr(pv) else "✗"
                lines.append(f"  {sheet_name}!{cn}: got={pv!r}, expected={gv!r} {match}")

        # Also check if any cells in the output contain formula strings
        formula_cells = []
        for sn in wb_pred.sheetnames:
            ws = wb_pred[sn]
            for row in ws.iter_rows(max_row=min(ws.max_row, 200), values_only=False):
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        formula_cells.append(f"{sn}!{cell.coordinate}={cell.value}")
                        if len(formula_cells) >= 10:
                            break
                if len(formula_cells) >= 10:
                    break
            if len(formula_cells) >= 10:
                break
        if formula_cells:
            lines.append(f"\n  WARNING: {len(formula_cells)} cells contain Excel formulas (openpyxl cannot evaluate them):")
            for fc in formula_cells[:5]:
                lines.append(f"    {fc}")
            if len(formula_cells) > 5:
                lines.append(f"    ... and {len(formula_cells) - 5} more")
    finally:
        wb_pred.close()
        wb_gold.close()

    return "\n".join(lines)
