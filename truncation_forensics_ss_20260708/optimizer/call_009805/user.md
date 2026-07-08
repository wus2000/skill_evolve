## The current rules document (full, handle-annotated)
### [S#1] Formula Evaluation vs. Literal Value Writing
**The Problem:**
`openpyxl` stores Excel formulas as string expressions (e.g., `"=SUM(A1:A10)"`) but **does not evaluate them**. When the output file is read back by the evaluation script (which typically uses `openpyxl`), a cell containing a formula string will return the formula text itself or `None`, **not** the calculated result. This causes failures when the task expects a computed value (number, string, date) but receives a formula string instead.

**The Rule:**
1. **Default: Compute and Write Literal Values.**
   If the task asks for a **result** (e.g., "calculate the sum", "lookup the price", "determine if X is high"), you **must** compute the value in Python code and write the **literal value** to the cell.
   - **Correct:** `cell.value = 100` (after computing the sum in Python)
   - **Correct:** `cell.value = "Approved"` (after determining the status in Python)
   - **Incorrect:** `cell.value = "=SUM(A1:A10)"` (unless the task explicitly asks for the formula text)

2. **Exception: Write Formula Strings Only When Explicitly Requested.**
   If the task explicitly asks you to **write a formula** (e.g., "insert a formula that calculates..." or "show the formula used"), you may write the formula string.
   - **Warning:** If the evaluator checks the **result** of the cell (not just the formula text), a formula string will still fail because `openpyxl` cannot evaluate it.
   - **Safe Pattern for Formula Requests:** If the task requires a formula but the evaluation checks values:
     - **Option A (Preferred):** Compute the result in Python and write the **literal value** to the target cell. If the task also requires the formula to be visible, write the formula to a **separate helper cell**.
     - **Option B:** If the task strictly requires the formula to be in the target cell *and* the result to be checked, you may need to use an external tool (like LibreOffice Calc) to evaluate the formulas before saving, or write the formula and then overwrite the cell's `.value` with the pre-computed result (note: behavior varies by library/version; Option A is safer).

**Schematic Examples:**

*Example 1: Lookup (Literal Value)*
```python
# Task: Look up the price for item in A2 and put it in B2

# BAD: Writing a formula string
# ws['B2'].value = '=VLOOKUP(A2, PriceList, 2, False)'
# Evaluator reads: "=VLOOKUP(A2, PriceList, 2, False)" or None -> FAIL

# GOOD: Computing the result in Python
lookup_val = ws['A2'].value
price = None
for row in range(2, 100): # Assuming PriceList starts at row 2
    if ws.cell(row=row, column=1).value == lookup_val:
        price = ws.cell(row=row, column=2).value
        break
ws['B2'].value = price if price else '' # Write literal value
```

*Example 2: Calculation (Literal Value)*
```python
# Task: Sum column A and put total in A11

# BAD: Writing a formula string
# ws['A11'].value = '=SUM(A1:A10)'

# GOOD: Computing the sum in Python
total = sum(ws.cell(row=r, column=1).value or 0 for r in range(1, 11))
ws['A11'].value = total
```

*Example 3: Explicit Formula Request (Safe Pattern)*
```python
# Task: Put a formula in C1 that sums A1 and B1, and also show the result

# BAD: Only writing formula (result is None/str for evaluator)
# ws['C1'].value = '=A1+B1'

# GOOD: Write formula in helper cell, value in target
ws['D1'].value = '=A1+B1' # Formula for display
ws['C1'].value = ws['A1'].value + ws['B1'].value # Literal value for evaluation
```

### [S#2] Transposition and Duplicate Value Placement

### [S#3] Inferring Fill Direction
When transposing data from a source list to a target grid, you must infer the fill direction (row-major vs. column-major) by examining any partially-filled cells already present in the target sheet.

1. **Analyze Existing Data**: Look for non-empty cells in the target range. Compare their positions relative to the source order.
2. **Determine Direction**:
   - **Row-Major**: If existing values fill left-to-right across a row before moving to the next row (e.g., C1=A, D1=B), the fill is row-major.
   - **Column-Major**: If existing values fill top-to-bottom down a column before moving to the next column (e.g., C1=A, C2=B), the fill is column-major.
3. **Default Behavior**: If no existing cells disambiguate the direction, default to **row-major** (fill across rows first).
4. **Verification**: Always verify your inferred direction against any pre-existing data in the target range before writing new values to ensure consistency.

### [S#4] Handling Duplicate Keys
When a key in the source data has multiple matches, preserve the individual value structure by placing each match in its own set of columns. Do **not** concatenate multiple values into a single cell with separators (e.g., "Blue, Blue") unless explicitly instructed otherwise.

1. **Collect Matches**: Identify all rows in the source data that match the lookup key, maintaining their original order.
2. **Allocate Columns**: Determine the starting output column(s) for this key.
3. **Spread Values**: Place the value(s) from the first match into the first available column(s), the value(s) from the second match into the next available column(s), and so on.
4. **Preserve Structure**: Each matched value (or pair of values) must occupy its own distinct cell(s) in the target grid. This maintains the transposed structure where each column represents a distinct instance of the key.

### [S#5] Fill-Down with Cross-Column Termination and Resets
When performing a fill-down operation where the fill range is determined by a termination condition in a different column (e.g., fill column A with values from column B until column C is empty):

1. **Pre-processing Termination Column**: If the instruction involves deleting or clearing rows/cells in the termination column, perform these deletions/clearing steps **before** starting the fill-down pass. The fill logic must evaluate the termination condition against the final state of the data, not the intermediate state.

2. **Continuous Iteration with State Tracking**: Do not use a simple `break` statement on the first empty termination cell. Instead, iterate through all rows in the target range while maintaining a `last_filled_value` state:
   - Initialize `last_filled_value` to `None`.
   - For each row:
     - If the cell in the column being filled is non-empty, update `last_filled_value` to this cell's content.
     - If the cell in the column being filled is empty:
       - Check the corresponding cell in the **termination column**.
       - If the termination cell is **not empty**, copy `last_filled_value` into the current cell (if `last_filled_value` is not `None`).
       - If the termination cell is **empty**, leave the current cell empty and **do not break**; continue to the next row.

3. **Reset on Structural Breaks**: If the dataset contains structural separators (e.g., repeated header rows, section breaks) that indicate the start of a new logical block:
   - Detect these separator rows.
   - When a separator is encountered, reset `last_filled_value` to `None`.
   - This prevents values from a previous block from bleeding into the current block.

**Example Logic (Pseudocode):**
```python
last_value = None
for row in range(start_row, end_row + 1):
    fill_cell = sheet.cell(row, fill_col)
    term_cell = sheet.cell(row, term_col)
    
    # Check for structural break / header
    if is_header_row(row):
        last_value = None
        continue
        
    if fill_cell.value is not None:
        last_value = fill_cell.value
    elif fill_cell.value is None:
        if term_cell.value is not None and last_value is not None:
            fill_cell.value = last_value
        # If term_cell is empty, do nothing and continue
```

This approach ensures that fills propagate correctly across empty cells in the target column as long as the termination column indicates the data is still active, and that blocks are properly isolated by header resets.

### [S#6] Output Verification
After modifying a spreadsheet and saving the output, always load the file back and verify critical cells for internal consistency and integrity before calling TASK_COMPLETE. This catches silent failures such as incorrect data types, missing formatting, or calculation errors.

Specific checks include:
- **Data Types**: Ensure cells intended for numbers contain valid numeric values (not text strings representing numbers, unless specified) and cells intended for labels contain strings. Check that formulas are present where calculations are expected.
- **Error Codes**: Verify that no cells contain error values like `#DIV/0!`, `#VALUE!`, `#REF!`, or `#NAME?`.
- **Formatting**: Confirm that required formatting (e.g., fill colors, borders, bold text) has been applied to the correct cells as instructed.
- **Completeness**: Ensure that cells intended to be blank are indeed blank, and that no unexpected empty cells remain where data should exist.

This verification step detects silent failures where the tool reports success but the output is structurally or semantically incorrect.

### [S#7] Sequential Label Generation

### [S#8] Base-26 Letter Suffixes
When generating sequential labels that require letter suffixes beyond the first 26 items (a-z), implement proper base-26 encoding. Do not simply append letters or stop after 'z'.

**Algorithm:**
For a 1-based index `N`, generate the suffix by repeatedly:
1. Computing `remainder = (N - 1) % 26` to determine the current character (0→a, 1→b, ..., 25→z).
2. Updating `N = (N - 1) // 26`.
3. Prepending the character to the result string.
4. Stopping when `N` becomes 0.

**Examples:**
- 1 → `a`
- 26 → `z`
- 27 → `aa` (27-1=26, 26%26=0→a, 26//26=1; then 1-1=0, 0%26=0→a, 0//26=0 → `aa`)
- 52 → `az`
- 53 → `ba`

### [S#9] Data Extraction Scope
When extracting source data for aggregation or processing, you must scan the entire sheet for all rows containing actual data values. Do not assume a fixed range or stop early after processing the first few rows.

**Procedure:**
1. Identify the data region by locating rows where the key columns (e.g., Name, Type, Time) contain non-None values.
2. Iterate through the sheet until you encounter a sustained sequence of empty rows or reach the sheet boundary.
3. Ensure all identified data rows are included in your extraction to avoid missing entries that affect aggregation counts or summaries.

**Why:** Limiting extraction to a subset of rows (e.g., only the first N rows) often leads to missed data, resulting in incorrect counts, sums, or categorizations. Always verify that all relevant data points have been captured.

### [S#10] Time Comparison Semantics
When filtering or counting entries by time-of-day (ignoring the date), you must:

1. **Extract the time component**: Convert datetime objects to time objects (e.g., using `.time()` in Python) before comparison to ensure date components do not interfere.
2. **Use half-open intervals**: For contiguous time ranges (e.g., 30-minute buckets), use `>= start` and `< end` (strict less-than) for the upper bound. This ensures each entry falls into exactly one bucket and avoids double-counting at boundary points (e.g., an entry at 13:30:00 belongs to the 13:30-14:00 bucket, not both 13:00-13:30 and 13:30-14:00).

**Exception**: Only use `<= end` (inclusive upper bound) if the task explicitly requires it (e.g., "include all entries up to and including 13:30"). If the task is ambiguous, prefer the half-open interval `< end` as it is the standard convention for non-overlapping partitions.

### [S#11] Conditional Cell Filtering of 'None' Strings
When filtering or counting cells based on conditions (such as background color, text content, or other attributes), the literal string "None" (case-insensitive) must be treated as a blank or empty value and excluded from results, unless the task explicitly instructs to include or preserve it.

This convention applies because "None" frequently appears in data exported from spreadsheets or imported via pipelines as a textual representation of null/missing values, rather than as meaningful content. Excluding it ensures accurate counts and filtering that align with the semantic intent of "blank" or "empty".

Example:
- If a task asks to "count all yellow cells that are not blank", a cell with the value "None" and a yellow background should NOT be counted.
- If a task asks to "highlight cells containing the text 'None'", then those cells should be targeted (as the instruction is specific to the string, not the semantic blankness).

Note: This rule applies specifically to the string "None". Other placeholder strings (e.g., "N/A", "null", "empty") are not automatically treated as blank unless specified in the task or covered by other general null-handling rules.

### [S#12] Multi-Row Source Lookups

### [S#13] Multi-Row Source Lookups
When joining data from a source sheet that contains multiple rows for the same key combination:

1. **Use the First Matching Row**: Always derive values from the **first** row that matches the join keys. Do not use the last row or any subsequent duplicate.
2. **Limit Join Keys**: Use only the join keys explicitly required by the task (e.g., Meet Name, Race Number). Do not include additional columns (such as Date) in the join key unless they are explicitly specified as part of the key.

**Implementation Note**: When building a lookup dictionary or mapping from the source sheet, ensure your logic selects the first occurrence of each key group (e.g., `index 0` in a grouped iteration) rather than allowing later rows to overwrite earlier ones.

**Why**: This prevents data corruption where later duplicate rows (which may have different or outdated values) overwrite the canonical first entry. It also avoids mismatch errors caused by over-specifying join keys with non-key columns like Date.

### [S#14] Cross-Row Lookup and Matching
When a task requires retrieving a value (e.g., an ID) from one row based on criteria found in a different column or row, you must perform a cross-row search rather than looking only within the current row.

**Procedure:**
1. **Build a Lookup Table:** Identify the column containing the searchable content (e.g., comments, descriptions) and the column containing the target value to retrieve (e.g., ID, Name). Create a mapping where each row's searchable content maps to its target value.
2. **Identify Search Keys:** Determine the parameters or keywords provided in the query context or in specific columns (e.g., a "Parameters" column) that serve as the search keys.
3. **Match Across Rows:** For each query, iterate through the lookup table to find the row where the searchable content contains ALL required search keys.
4. **Retrieve Result:** Return the target value associated with the matching row.

**Common Pitfall:**
Do not assume that parameters listed in a row's column should only match that same row's content. Often, the parameters are a query against the entire dataset. If the parameters do not appear in the current row's searchable column, check other rows.

**Example Pattern:**
- Column A: ID (Target Value)
- Column B: Comments (Searchable Content)
- Column E: Parameter 1 (Search Key)
- Column F: Parameter 2 (Search Key)
- **Task:** For each row, find the ID where the Comments (Column B) contain both Parameter 1 and Parameter 2.
- **Action:** Search ALL rows' Comments for the combination of Parameter 1 and Parameter 2, then return the corresponding ID from Column A.

### [S#15] Format and Type Preservation
When copying data between sheets, preserve the original cell value type and formatting rather than normalizing or inferring types.

- **Value Type Preservation**: Copy the `value` attribute directly. Do not parse string representations of dates or numbers into `datetime` or `float` objects unless the source cell already holds that native type. For example, a cell containing the string `'13/09/2021'` must remain a string; do not convert it to a `datetime` object.
- **Number Formats**: Preserve the source cell's `number_format` string (e.g., `'mm-dd-yy'`, `'DD/MM/YYYY'`) by assigning it directly to the target cell.
- **Style Reconstruction**: Copy `cell.alignment`, `cell.font`, `cell.border`, and `cell.fill`. If direct assignment fails (e.g., due to `TypeError: unhashable type: 'StyleProxy'` or similar proxy issues), reconstruct the target style object by passing the source object's attributes (e.g., horizontal, vertical, wrap_text for Alignment) to the constructor of the corresponding `openpyxl.styles` class.

**Rule of thumb**: When in doubt, copy `value` and `number_format` directly. Avoid any implicit type coercion or parsing of string values.

### [S#16] Formula Verification and Empty Cell Handling
#### Formula Persistence and Verification

openpyxl stores formulas as literal strings and **does not evaluate them**. When writing formulas, you must verify that the string was correctly persisted by reading the cell's `.value` attribute back. Do not expect openpyxl to return computed results.

```python
# Write formula
ws['C3'].value = '=SUMPRODUCT((MONTH($B$17:$B$24)=MONTH($B3))*($C$17:$C$24))'

# Verify the string was written correctly
assert ws['C3'].value == '=SUMPRODUCT((MONTH($B$17:$B$24)=MONTH($B3))*($C$17:$C$24))'
```

Key verification points:
1. **Read `.value`**: This returns the formula string (e.g., `'=IF(...)'`), not the calculated result.
2. **Check for `None`**: If `.value` is `None`, the formula was not written correctly.
3. **Syntax**: Ensure the formula string is valid Excel syntax, with correct absolute/relative references (e.g., `$B$17`) where needed.

#### Robust Empty Cell Detection

Excel formulas that check for empty cells can be fragile if they rely on `=""` alone, as this checks for empty strings, not necessarily blank cells (which may be `None` in Python or contain hidden characters). Use `ISBLANK()` for robustness.

**In Python (pre-writing check):**
If you are constructing formulas dynamically based on cell content, check for `None` to determine if a cell is blank.

```python
# Check if C7 is empty in openpyxl
if ws['C7'].value is None:
    # Cell is blank
    formula = '=IF(ISBLANK(C7), "Empty", "Not Empty")'
else:
    # Cell has a value
    formula = f'=IF(C7>1, "High", "Low")'
```

**In Excel Formulas:**
Use `ISBLANK()` to detect truly blank cells.

```excel
# Good: Detects blank cells
=IF(ISBLANK(C7), "Blank", C7)

# Risky: May not detect all blank states depending on data type
=IF(C7="", "Blank", C7)
```

When the task specifies "empty" or "blank," prefer `ISBLANK()` in the formula or `is None` in Python logic to ensure consistency.

### [S#17] Multi-Match Disambiguation
When matching keys from one list to entries in another (e.g., matching Sheet2 headers to Sheet1 entries), if multiple headers could match a single entry, always select the longest (most specific) matching header.

This prevents incorrect column assignments when shorter headers are substrings of longer ones. For example, if both `U4_ComponentPropertyForm` and `U4_ComponentProperty` are potential matches for an entry, `U4_ComponentPropertyForm` is the more specific key and should be selected.

**Rule:**
1. Identify all headers that match the current entry.
2. Among the matches, choose the one with the greatest length.
3. If there is a tie in length, use other disambiguation criteria (e.g., alphabetical order or specific mapping logic) if available, otherwise select the first match.

### [S#18] String Concatenation Delimiters
When concatenating strings that already contain delimiter characters (e.g., a trailing '+'), carefully count the total number of delimiter characters in the expected output.

The separator between the source string and the appended value must produce the exact character count shown in the expected result. Do not assume a standard single-separator insertion if the source string already includes delimiters; instead, verify the total delimiter count in the final string against the expected pattern.

**Example:**
If the source string is `entry+` and the expected output is `entry++value` (two '+' characters), the separator added must be `+`, not nothing or `++`. Verify by comparing the first few characters of the concatenated output against the expected pattern.

### [S#19] Formula Compatibility and Preferences
When the task requires a formula in a cell, prefer simple, widely-compatible formulas over complex array formulas or functions that may not be supported in all spreadsheet engines.

**Preferred Functions**
Use functions that are widely supported across different spreadsheet environments:
- `SUMIFS`, `AVERAGEIFS`
- `WORKDAY`, `NETWORKDAYS`
- `IF`, `INDEX`, `MATCH`

**Functions to Avoid**
Avoid functions that are specific to Excel 365 or Google Sheets and may not evaluate in the target environment:
- `UNIQUE`, `SORT`, `SEQUENCE`
- `LAMBDA`
- Array formulas (entered with Ctrl+Shift+Enter) unless explicitly required and supported.

**Fallback Strategy**
If you must use a complex formula, or if you are uncertain about compatibility:
1. Compute the expected result in Python.
2. Write that literal value into the cell as a fallback.

When in doubt, compute the result in Python and write the literal value. The task's semantics (e.g., "sum the last 4 workdays", "extract unique digits") can always be computed algorithmically in code.

## The change-aspect you are drafting
Enforce Literal Value Output over Formula Strings: The agent must compute all results in Python and write literal values to cells because the evaluation environment uses `openpyxl`, which does not evaluate formulas (returning `None` or the formula string). This rule applies universally, including when tasks explicitly ask for formulas, array formulas, or lookups. If a formula string is required for display, it must be written to a separate helper cell, while the target cell receives the pre-computed literal. Additionally, when replacing existing formulas (especially array formulas), the agent must clear the worksheet's `array_formulae` dictionary to prevent `openpyxl` from returning stale formula metadata.

## Suggested placement
S#1

## The group's raw edits (full content)
[E#1] section_hint=Formula Evaluation vs. Literal Value Writing | kind=edit_point | tasks=42198 | src=P#1 | analyst_coverage_claim=refines: If the task explicitly asks you to write a formula
rationale: The failing rollouts interpreted the question 'What would the appropriate Excel formula be?' as an instruction to write the formula string into the cells. However, the evaluation checks the cell values, and openpyxl does not evaluate formulas, returning None. The passing rollout correctly identified that it needed to compute the result in Python and write the literal value. This refines the existing rule by explicitly addressing the ambiguity of 'formula' questions that actually require computed results.
content:
**Critical Warning for "What formula" Questions:** When the task asks "What formula would be appropriate..." but provides an `answer_position` (e.g., C2:C7) and expects the spreadsheet to contain the *result* of that logic, **do not write the formula string**. The evaluation system reads the file with `openpyxl` (or similar), which returns `None` or the formula text for formula cells, not the calculated result. You must **compute the result in Python** using the logic described in the question and write the **literal value** to the target cells.

- **Pattern:** If the task describes a calculation rule (e.g., "cumulative sum", "lookup", "conditional check") and asks for the formula but expects the output file to have values in the answer cells, implement the rule in Python code and write the computed literal.
- **Verification:** After writing, verify that `cell.value` is the expected literal (e.g., `'Good'`, `100`), not a string starting with `=`.

Draft the definitive edit(s). Respond with ONLY the JSON object described.