### Computed Values over Formulas

When the task asks for a calculation, lookup, count, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **Do NOT write formula strings** (e.g., `"=SUM(...)"`, `"=IF(...)"`, `"=VLOOKUP(...)"`, `"=COUNTIFS(...)"`, `"=INDEX(...)"`) unless the task **explicitly** commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* (computed) value, not the formula. If you write a formula string, the evaluator will likely see `None` or the string itself, not the calculated result.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas, multi-criteria lookups), manually filter/sort/rank the data in Python to find the nth value.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed) **AND** the evaluator is configured to evaluate formulas. Default to computed values. If in doubt, compute the value in Python and write the literal result.
- **Formula Objects**: When a cell already contains an Excel formula object (e.g., `openpyxl.worksheet.formula.ArrayFormula`), do **not** write another formula string. Instead, compute the result in Python and overwrite the cell's `value` attribute with the **literal result** (number, string, or `None`). This replaces the formula object with a static value that the evaluator can read directly.
- **Read-Then-Write Pattern**: Always read all relevant source data into Python variables/lists/dictionaries **before** writing any results back to the sheet. This prevents reading overwritten or stale data if the source and target ranges overlap or if you modify the sheet in-place.
- **Critical Check**: Before writing any cell, ask: "Does this task want the *result* (a number/text) or the *formula* (a string starting with '=')?" If ambiguous, default to the result/computed value.
- **Boolean Literals**: When a task asks to "mark as False" or "mark as True" based on a condition, write the boolean literal `True` or `False`. Do not default to empty string (`''`) or `None` for False/True results unless the instruction explicitly says "leave blank" or "mark as empty".
- **Whitespace Preservation**: When modifying text strings (e.g., truncating, replacing, concatenating), preserve original whitespace (spaces, tabs) exactly as they appear in the source, unless the task explicitly instructs to trim or strip. Do not use `.strip()`, `.rstrip()`, or `.lstrip()` on the final result unless explicitly commanded.
- **Robust Date Parsing**: When tasks involve filtering by month/year from dates, dates may arrive as `datetime` objects or strings (e.g., `'2021-01-01 00:00:00'`). Always handle both formats: check `isinstance(val, datetime)` first; if string, parse with `strptime`. Extract month number via `.month` or `strftime('%m')` for comparison.
- **Sequential/Stateful Lookups**: When a calculation depends on the previous row's value for the same key (e.g., "amount used since last usage"), maintain a state dictionary mapping the key to its last seen value. Iterate through rows in order, using the dictionary to look up the previous value, then update the dictionary with the current value.
- **Exact Word Matching**: When the task requires matching words (e.g., "root word as a complete word"), use word-boundary matching (e.g., regex `\b` or split-and-compare) rather than substring matching. For example, 'garden' should NOT match 'gardening', and 'tool' should NOT match 'tools'.
- **Verify Output**: After writing the computed value, always verify the output file content (e.g., by loading the saved workbook and reading the target cell) to ensure the literal value is present and correct. Do not assume the write succeeded without checking.
- **Evaluate Source Formulas**: If source cells contain formula strings (e.g., `"=(B8*12)/12"`, `"=SUM(...)"`), **parse and evaluate them in Python** to get the numeric value. Do not treat them as literals. Replace cell references (e.g., `B8`) with their actual values from the worksheet before evaluation. If evaluation fails, treat the value as missing or 0.
- **Zero-Sum Cell Handling**: When a computed sum or count is zero, write an empty string (`''`) or `None` (blank) to the cell, as instructed by tasks that say "if the sum is zero, leave the cell as empty." Do not write `0` unless explicitly commanded.

### Sheet and Range Fidelity

**Preserve Sheet Identity**:
- Load the input workbook and modify it in place. Do **not** create a new `Workbook()` and save it, as this discards original sheet names and structure.
- If you must create a new sheet, explicitly set its title to match the original sheet name (e.g., `ws.title = 'Sheet1'`).
- When using `pandas.to_excel`, explicitly specify `sheet_name` to match the original sheet name. Prefer `openpyxl` for cell-level edits to avoid sheet name ambiguity.

**Answer Position Alignment**:
- If the task specifies an `answer_position` (e.g., `A3:G28`), write data into that exact range.
- **Contiguous Writing**: When filtering or deleting rows, write the kept rows **contiguously** starting from the first row of the answer position (e.g., row 3). Do not preserve original row indices or leave gaps. The first kept row goes to the first cell of the answer position; the second kept row goes to the next cell, etc.
- **Verification**: After writing, ensure the answer position contains no empty rows between data rows and that the first cell contains the first expected data row.

**Full Range Iteration**:
- When scanning for conditions, iterate over the **full range** of rows containing data (use `ws.max_row` or detect the last non-empty row in the widest column). Do not break early on empty cells in condition columns; data may exist in other columns of later rows.

**Read Before Write**:
- When modifying a worksheet in-place (e.g., filtering/deleting rows or columns), always read and store all relevant source data into a Python list/dictionary **before** clearing or overwriting any cells in the source sheet. If you clear cells before reading them, you will lose the data you need to write back.

**Conditional Clearing (Keep/Discard)**:
- When instructed to keep only specific values and delete/clear others, iterate over the target range. For each cell, if the value matches the target literal (case-insensitive if the instruction implies it, otherwise case-sensitive), preserve it. If it does not match, set the cell value to `''` (empty string). Do not delete rows; only clear cell contents.

**Multi-Condition Row Extraction (OR Logic)**:
- When extracting rows based on multiple conditions with OR logic (e.g., Col B equals X OR Col C equals Y), iterate through all data rows in the source sheet. Evaluate each row against all conditions; if any condition is true, include the entire row in the result set. Write the headers to the specified row in the target sheet, then write all matching rows contiguously below the headers.

**Group-Aware Transposition**:
- When transposing data from a column to a wider range (e.g., Column A to Columns C-K), do NOT simply flatten all non-blank values into a continuous stream. Instead, first inspect the source column for structural separators (such as blank rows). Group consecutive non-empty values separated by these blanks, and map each group to a distinct row in the target output range. Preserve the group boundaries as row boundaries in the result.

**Block-Based Transposition**:
- When transposing data from a single column into a multi-column grid:
  1. **Detect Block Structure**: Identify the repeating pattern in the source column. Count the number of items per logical block (e.g., Question + 4 Options + Answer + Explanation = 7 items).
  2. **Calculate Grid Dimensions**: Determine the target grid size (rows × columns) from the `answer_position` or task description. Verify that `total_items = rows × columns`.
  3. **Read and Chunk**: Read all non-empty values from the source column into a flat list. Chunk this list into blocks of the detected size.
  4. **Write Row by Row**: Iterate through each block. Write the items in the block sequentially into the target row, starting from the first column of the answer position.
  5. **Preserve Order**: Maintain the original order of items within each block. Do not sort or rearrange unless explicitly instructed.

**Column Deletion by Header Pattern**:
- When deleting columns based on a pattern in the header row:
  1. **Scan Headers**: Iterate through all columns in the header row (typically row 1). Identify columns whose header text contains the specified keyword or pattern.
  2. **Collect Column Indices**: Store the indices of columns to delete.
  3. **Delete in Reverse Order**: To avoid index shifting issues, delete columns in descending order of their column index (right-to-left). Alternatively, build a new sheet containing only the columns to keep.
  4. **Preserve Structure**: When using `openpyxl`, modify the existing sheet. If creating a new sheet, ensure the title matches the original. Preserve all rows and non-deleted columns exactly.
  5. **Verify**: After deletion, verify that no remaining headers contain the deleted keyword.

### Data Processing and Lookup Logic

**Multi-Column Lookups**:
- When matching across multiple columns (e.g., Column B and D), you must match **ALL** specified criteria simultaneously. A partial match is insufficient.
- Convert search keys and data values to a common type (e.g., string) before comparison to avoid silent type mismatches.
- Use the matching row's data from the target output column.

**Date and Type Handling**:
- **Normalize Dates**: Convert all dates to a consistent type (e.g., `datetime` or `YYYY-MM-DD` string) before grouping or sorting. Do not mix string dates and datetime objects.
- **Preserve Original Types**: When copying values, preserve their original Python type (e.g., `str`, `datetime`, `float`). Do not convert string dates to datetime objects unless explicitly required.
- **Identify Correct Columns**: Verify which column contains the primary data for filtering (e.g., check if the 'date' column is Column A or Column N). Do not assume metadata columns contain row-specific data.

**Lookup Implementation Pattern**:
- Build an explicit Python dictionary mapping lookup keys to target values from the source data.
- Iterate through target rows and fill cells using the dictionary. If a key is not found, write `''` or `None`.
- For conditional logic, evaluate the condition in Python for each row and write the resulting literal value.

**Contiguous Block Detection**:
- For tasks involving identifying ranges of markers (e.g., 'm' for meetings), scan for contiguous sequences. Map the start and end of each block to the corresponding headers (e.g., time columns) and write the start/end times to the output columns.

### Answer Position Awareness

**Respect Answer Position Dimensions**: Before writing any data, inspect the `answer_position` (e.g., `B11:B17`) to determine the exact grid shape (rows × columns) of the output.

- **Single-Column Output**: If the answer position is a single column (e.g., `B11:B17`), write **only the relevant single value** per row into that column. Do **not** concatenate multiple source columns (e.g., A and B) into one cell unless the task explicitly instructs you to merge them. The evaluator checks specific cells; writing extra data into the same cell causes a mismatch.
- **Multi-Column Output**: If the answer position spans multiple columns (e.g., `A1:B7`), map each source column to the corresponding output column. Row 1 of data goes to `A1` and `B1`; Row 2 of data goes to `A2` and `B2`, etc.
- **Verification**: After writing, verify that the number of output rows matches the number of data rows and that the data fits within the answer position bounds without overflow or concatenation errors.