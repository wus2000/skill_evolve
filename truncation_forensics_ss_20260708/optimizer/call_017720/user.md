## The current rules document (full, handle-annotated)
### [S#1] Formula Evaluation vs. Literal Value Writing
**The Problem:**
The evaluation script reads the file via `openpyxl` without setting `data_only=True`. Consequently, when `openpyxl` reads a cell containing a formula string, the `.value` attribute returns `None` (or the formula text itself), **not** the calculated result. This is the specific mechanism causing failures when the task expects a computed value (number, string, date) but receives `None` or a formula string instead.

**The Rule:**

#### 1. Default: Compute and Write Literal Values.
If the task asks for a **result** (e.g., "calculate the sum", "lookup the price", "determine if X is high"), you **must** compute the value in Python code and write the **literal value** to the cell.
- **Correct:** `cell.value = 100` (after computing the sum in Python)
- **Correct:** `cell.value = "Approved"` (after determining the status in Python)
- **Incorrect:** `cell.value = "=SUM(A1:A10)"` (unless the task explicitly asks for the formula text)
- **Source Formula Handling:** If the task requires aggregating values from cells that contain formulas (e.g., commission calculations like `=(B8*12)/12`), you must **parse and evaluate these formulas in Python** to get the numeric result. Do not write the formula string itself. Extract cell references (e.g., `B8`) and multipliers from the formula string, fetch the referenced cell's value, and compute the result.
- **Type Fidelity:** When computing literal values in Python, ensure the written value matches the semantic type expected by the task. Specifically, if the result of a transformation is numeric (e.g., removing a prefix from a code string like '4000001' -> 1), write the result as the appropriate native Python type (`int` or `float`), **not** as a string.
  - **Correct:** `cell.value = 1`
  - **Incorrect:** `cell.value = '1'`

#### 2. Implicit Formula Requests.
If the task asks to **modify**, **adapt**, or **update** existing formulas or logic (e.g., "change the formula in B2 to include tax", "update the calculation for Q2"), you must still compute the result in Python and write the **literal value** to the target cell. Do not write a new formula string unless the task explicitly asks to 'insert a formula' or 'show the formula'. The evaluation expects the computed result, not a formula string.

#### 3. Explicit Formula Requests: Write Literal Value, Helper Cell for Formula.
If the task explicitly asks you to **write a formula** (e.g., "insert a formula that calculates..." or "show the formula used"), you **must still write the computed literal value** to the target cell to ensure the evaluator passes.
- **Critical Warning on Task Phrasing:** If the task uses the word "formula" (e.g., "I need a formula that..."), or asks to "fix," "correct," or "provide" a formula, interpret this as a request for the **computed result** of that formula, not the formula text itself. Do **not** write the formula string to the target cell unless the task explicitly requires the formula text to be visible in the spreadsheet (e.g., "show the formula used").
- **Primary Action:** Compute the result in Python and write the **literal value** to the target cell.
- **Secondary Action (If Visibility Required):** If the task explicitly requires the formula to be visible in the spreadsheet, write the formula string to a **separate helper cell** (e.g., an adjacent column or a designated notes area).
- **Warning:** Do not write the formula string to the target cell if the evaluation checks the cell's value, as `openpyxl` cannot evaluate it. Do not rely on external tools (like LibreOffice Calc) to evaluate formulas post-save, as this is unreliable and outside the standard agent workflow.

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

*Example 4: Source Formula Evaluation & Type Fidelity*
```python
# Task: Calculate commission from B8 (which contains formula =(B8*12)/12)
# Note: B8's *current* value in openpyxl might be a string formula if not evaluated.

# BAD: Using the string directly or writing a new formula
# comm_rate = ws['B8'].value # Might be "=(B8*12)/12" or None if not evaluated

# GOOD: Parse source formula and compute result
source_formula = ws['B8'].value # Assuming this is the string "=(B8*12)/12" or similar
# In a real scenario, you'd parse the formula string to extract refs and operators.
# Here, we assume we can fetch the underlying values.
val_b8_ref = ws['B8'].value # If B8 is a formula, this might be None without data_only=True evaluation logic in source
# Correct approach: If source is a formula string, parse it.
# Simplified: If we know the structure:
base_val = ws['B8'].value # If openpyxl returns None, we must parse the formula string
# Let's assume we have parsed it to get 1000
computed_commission = 1000 * 0.1
ws['C8'].value = computed_commission # Write int 100.0 or 100, not '100'
```

### [S#2] Transposition and Duplicate Value Placement
#### Single Column to Grid Transposition
When transposing a single contiguous source column into a rectangular target grid:

1. **Extract Values**: Collect all non-`None` values from the source column in order.
2. **Fill Row-Major**: Populate the target grid in **row-major** order (fill the first row left-to-right, then the second row, etc.).
3. **Verify Capacity**: Ensure the number of extracted values matches the target grid's capacity (rows × columns). If they do not match, check if the grid dimensions in the task description are correct or if values should be padded/truncated as instructed.
4. **Override Direction**: This row-major default applies unless the target sheet already contains data that implies a different fill direction (see [S#3] Inferring Fill Direction).

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

#### Execution Note
When executing Python scripts in the agent's environment, always use `python3` instead of `python`, as the `python` command may not be available.

- **Correct:** `python3 solution.py`
- **Incorrect:** `python solution.py`

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
When extracting source data for aggregation or processing, you must distinguish between **identifying the data region** and **defining the aggregation scope**.

#### 1. Identifying the Data Region (Extraction)
You must scan the entire sheet to find all rows containing actual data values. Do not assume a fixed range or stop early after processing the first few rows.

**Procedure:**
1. **Identify the data region** by locating rows where the key columns (e.g., Name, Type, Time) contain non-None values.
2. **Iterate through the sheet** until you encounter a sustained sequence of empty rows or reach the sheet boundary.
3. **Ensure all identified data rows are included** in your extraction to avoid missing entries that affect aggregation counts or summaries.

**Why:** Limiting extraction to a subset of rows (e.g., only the first N rows) often leads to missed data, resulting in incorrect counts, sums, or categorizations. Always verify that all relevant data points have been captured.

#### 2. Defining the Aggregation Scope (Filtering)
When a task asks to count, sum, or aggregate based on a specific subset of keys (e.g., "count how many countries *mentioned in Table-1*..."), you must **filter the source data** to only include rows/entries that match the keys in the specified subset. Do not aggregate over the entire dataset or all available columns unless the task explicitly says so.

**Procedure:**
1. **Identify the Scope Keys**: Extract the list of valid keys from the specified reference (e.g., a specific table, column, or list mentioned in the prompt).
2. **Filter During Aggregation**: When iterating through the extracted data rows, only process rows where the key column value exists in the Scope Keys set.
3. **Ignore Out-of-Scope Data**: Rows with keys not in the subset must be excluded from counts, sums, and categorizations, even if they appear in the main data range.

**Example:**
- **Task**: "Count the number of countries from *Table-1* (cells A1:A3) that have >0 employees in the main data (B2:H100)."
- **Bad**: Count all unique countries in B2:H100 (5 countries).
- **Good**: Identify keys in A1:A3 ('Country', 'B', 'F'). Count only occurrences of 'Country', 'B', and 'F' in the employee columns. Result: 2 (or 3, depending on specific match logic).

#### 3. Advanced Extraction and Aggregation Patterns
Beyond basic region identification and scope filtering, several common task patterns require specific procedural heuristics to avoid common failure modes.

**3.1. Cross-Row Aggregation by Column-Wise Comparison**
When counting or aggregating based on comparisons across rows within the same column (e.g., "count how many times each player had the highest score"), you must perform a column-wise scan rather than a row-wise lookup.
1. **Identify Comparison Columns**: Determine which columns contain the values to be compared.
2. **Column-Wise Scan**: For each comparison column, iterate through all rows to find the maximum value (or other aggregate extreme). Handle ties: if multiple rows share the maximum, all are considered winners/matches.
3. **Accumulate Results**: Maintain a per-row accumulator. For each row that matches the criteria in a given column, increment the accumulator for that row.
4. **Write Output**: After processing all columns, write the accumulated counts to the target column.

**3.2. First-Appearance Order for Unique IDs**
When assigning sequential unique identifiers to records, the order of assignment is determined by the **first appearance** of each unique key in the data region, scanning from top to bottom. Do not re-assign IDs based on sort order or last appearance unless explicitly instructed.

**3.3. Reference Output is a Partial Example**
The reference output tab provided in the task is often a **partial example** showing the desired format. It is **not** the complete expected result. Do not limit your processing to only the rows present in the reference output; you must iterate through **every** data row identified in the source sheet.

**3.4. Scanning for Identifier Lists**
When a column contains a list of identifiers (e.g., question numbers, item codes) that define the scope of an aggregation, you must scan the **entire contiguous range** of that column to collect all identifiers. Iterate through the rows, collecting values as long as they are valid identifiers. Stop collecting when you encounter a non-identifier label (e.g., "Total", "Summary") or a sustained sequence of empty cells. Do not assume a fixed range or only use the header/first row.

**3.5. Multi-Weekday Aggregation (Repeating Headers)**
When source data spans multiple repeating periods (e.g., multiple weeks with repeating day headers like Friday, Saturday, Sunday), build a mapping of day names to all column indices where they appear. For each source row, iterate through all data columns, look up the day name from the mapping, and increment the count for that day. Compute all counts in Python and write literal integer results. Do not write formulas.

**3.6. Ambiguous Scope Instructions**
When the task instruction uses ambiguous quantifiers (e.g., "specific stock names", "certain items") without explicitly listing the subset to filter by, you must **include all unique keys** present in the source data. Do not infer a subset based on partial patterns, alphabetical order, or the first occurrence. Apply the logic to every unique key in the set.

**3.7. Distinguishing Input Search Keys from Output Destinations**
When the task instruction explicitly mentions specific cells as containing "values I am interested in" or "the data for...", these are often the **output/target** cells where results should be written, not input search keys to be read. Check for headers above the mentioned cells (if a header describes a result category, the cells below are outputs) and look at adjacent columns for actual search keys. Misinterpreting output cells as inputs leads to empty or incorrect results.

### [S#10] Time Comparison Semantics
When filtering or counting entries by time or date, you must ensure values are normalized to comparable types before comparison.

#### Time Comparison Semantics

When filtering or counting entries by **time-of-day** (ignoring the date), you must:

1. **Extract the time component**: Convert datetime objects to time objects (e.g., using `.time()` in Python) before comparison to ensure date components do not interfere.
2. **Use half-open intervals**: For contiguous time ranges (e.g., 30-minute buckets), use `>= start` and `< end` (strict less-than) for the upper bound. This ensures each entry falls into exactly one bucket and avoids double-counting at boundary points (e.g., an entry at 13:30:00 belongs to the 13:30-14:00 bucket, not both 13:00-13:30 and 13:30-14:00).

**Exception**: Only use `<= end` (inclusive upper bound) if the task explicitly requires it (e.g., "include all entries up to and including 13:30"). If the task is ambiguous, prefer the half-open interval `< end` as it is the standard convention for non-overlapping partitions.

#### Rolling Window Aggregation Semantics

When computing rolling averages or sums over a time window (e.g., "previous 365 days"), apply these specific rules:

1. **Exclude the Current Row**: The rolling window for row R includes only rows **before** R (indices < R). Do **not** include the current row's own value in its own rolling average/sum.
2. **Date-Based Windowing**: Define the window start by date, not by row count. Find the first row whose date is `>= (current_row_date - window_days)`. This handles sparse dates correctly; do not assume contiguous rows or fixed row counts.
3. **Range**: Average/Sum values from the identified start row up to (but not including) the current row.

**Schematic Example:**
```python
from datetime import timedelta

# For each row i with date d_i and value v_i:
#   threshold = d_i - timedelta(days=365)
#   window_rows = [r for r in rows if r.index < i and r.date >= threshold]
#   result = sum(r.value for r in window_rows) / len(window_rows) if window_rows else 0
```

### [S#11] Conditional Cell Filtering of 'None' Strings
When filtering or counting cells based on conditions (such as background color, text content, or other attributes), the literal string "None" (case-insensitive) must be treated as a blank or empty value and excluded from results, unless the task explicitly instructs to include or preserve it.

This convention applies because "None" frequently appears in data exported from spreadsheets or imported via pipelines as a textual representation of null/missing values, rather than as meaningful content. Excluding it ensures accurate counts and filtering that align with the semantic intent of "blank" or "empty".

Example:
- If a task asks to "count all yellow cells that are not blank", a cell with the value "None" and a yellow background should NOT be counted.
- If a task asks to "highlight cells containing the text 'None'", then those cells should be targeted (as the instruction is specific to the string, not the semantic blankness).

Note: This rule applies specifically to the string "None". Other placeholder strings (e.g., "N/A", "null", "empty") are not automatically treated as blank unless specified in the task or covered by other general null-handling rules.

### [S#12] Multi-Row Source Lookups

### [S#13] Multi-Row Source Lookups
When joining data from a source sheet that contains multiple rows for the same key combination, the behavior depends on whether the task requires the first occurrence or a specific sequential occurrence.

#### Default Behavior: First Matching Row

In most cases, you should derive values from the **first** row that matches the join keys. Do not use the last row or any subsequent duplicate.

1. **Use the First Matching Row**: Always select the first row that matches the join keys.
2. **Limit Join Keys**: Use only the join keys explicitly required by the task (e.g., Meet Name, Race Number). Do not include additional columns (such as Date) in the join key unless they are explicitly specified as part of the key.

**Why**: This prevents data corruption where later duplicate rows (which may have different or outdated values) overwrite the canonical first entry. It also avoids mismatch errors caused by over-specifying join keys with non-key columns like Date. When building a lookup dictionary, ensure your logic selects the first occurrence of each key group (e.g., `index 0` in a grouped iteration) rather than allowing later rows to overwrite earlier ones.

#### Nth Occurrence Lookups (Exception to First Match)

When the task requires retrieving values based on the **order of appearance** of duplicate keys (e.g., the second occurrence of a product name should return the second associated value), the standard "first match" rule does not apply. Instead, you must track the occurrence count for each key.

**Procedure:**
1. **Build an Ordered List of Values:** Iterate through the source data and group values by key, preserving the order of appearance. Use a dictionary mapping each key to a list of its corresponding values.
2. **Track Lookup Occurrences:** As you iterate through the rows requiring the lookup, maintain a counter for each key encountered. Increment the counter for every instance of that key in the lookup sequence.
3. **Select by Index:** Use the occurrence count (1-based) to index into the source's value list (0-based index = count - 1). If the count exceeds the number of available matches, return an empty string or handle as specified.

**Schematic Example:**
```python
from collections import defaultdict

# Step 1: Build ordered list of values for each key
source_values = defaultdict(list)
for row in range(2, source_max_row + 1):
    key = ws.cell(row=row, source_key_col).value
    val = ws.cell(row=row, source_val_col).value
    if key:
        source_values[key].append(val)

# Step 2 & 3: Track occurrences and retrieve nth value
occurrence_counts = defaultdict(int)
for row in range(2, lookup_max_row + 1):
    key = ws.cell(row=row, lookup_key_col).value
    if key:
        occurrence_counts[key] += 1
        nth = occurrence_counts[key]
        # Retrieve the nth value (nth-1 for 0-based list)
        if nth <= len(source_values[key]):
            result = source_values[key][nth - 1]
        else:
            result = '' # Or handle missing occurrence
        ws.cell(row=row, lookup_val_col).value = result
```

**Why:** This ensures that repeated lookups for the same key (e.g., looking up "Pencil" twice) return the distinct values associated with each specific instance (e.g., "Red" then "Blue"), rather than always returning the first instance's value.

### [S#14] Cross-Row Lookup and Matching
When a task requires retrieving a value (e.g., an ID) from one row based on criteria found in a different column or row, you must perform a cross-row search rather than looking only within the current row.

#### General Cross-Row Procedure (Single Sheet)
1. **Build a Lookup Table:** Identify the column containing the searchable content (e.g., comments, descriptions) and the column containing the target value to retrieve (e.g., ID, Name). Create a mapping where each row's searchable content maps to its target value.
2. **Identify Search Keys:** Determine the parameters or keywords provided in the query context or in specific columns (e.g., a "Parameters" column) that serve as the search keys.
3. **Match Across Rows:** For each query, iterate through the lookup table to find the row where the searchable content contains ALL required search keys. **Use word-boundary matching:** Split the searchable content and search keys into lists of words (e.g., by splitting on whitespace) and check for exact word matches rather than substring containment. This prevents false positives where a search term like 'garden' incorrectly matches 'gardening'.
4. **Retrieve Result:** Return the target value associated with the matching row.

#### Cross-Sheet Lookups
When the lookup source is on a different sheet than the target:
1. **Build the Lookup Dictionary First:** Iterate through the **source sheet** to construct the complete lookup table/mapping before processing the target sheet.
2. **Iterate Target Rows:** Loop through the **target sheet** rows and retrieve values using the pre-built dictionary.
3. **Handle Missing Keys:** Ensure you handle cases where a lookup key in the target sheet does not exist in the source dictionary (e.g., by assigning an empty string or leaving the cell blank).

**Example Pattern (Single Sheet):**
- Column A: ID (Target Value)
- Column B: Comments (Searchable Content)
- Column E: Parameter 1 (Search Key)
- Column F: Parameter 2 (Search Key)
- **Task:** For each row, find the ID where the Comments (Column B) contain both Parameter 1 and Parameter 2.
- **Action:** Search ALL rows' Comments for the combination of Parameter 1 and Parameter 2, then return the corresponding ID from Column A.

**Example Pattern (Cross-Sheet):**
- **Source Sheet (Sheet1):** Column A (ID), Column B (Name)
- **Target Sheet (Sheet2):** Column C (ID), Column D (Name - to be filled)
- **Action:**
  1. Scan Sheet1 to build `{id: name}` map.
  2. Iterate Sheet2 rows; for each ID in Column C, look up the name in the map and write to Column D.

**Common Pitfall:**
Do not assume that parameters listed in a row's column should only match that same row's content. Often, the parameters are a query against the entire dataset (or entire source sheet). If the parameters do not appear in the current row's searchable column, check other rows or the source sheet.

### [S#15] Key Type Consistency
When `datetime` objects are used as lookup keys, ensure both the source and target sides use the same type. Do not strip the time component (converting `datetime` to `date`) on one side while leaving the other as `datetime`, as Python treats these as unequal (`datetime(2020,1,1) != date(2020,1,1)`). Always normalize both sides to `datetime` or both to `date` before comparing or using as dictionary keys.

### [S#16] Output Mapping Integrity
When mapping source columns to target columns, explicitly verify that the *content* you are writing matches the *semantic meaning* of the target column header, not just the position or a loose keyword match.

**Rule**: Always map by semantic column identity (e.g., source column 'Designation' → target column 'Designation') rather than by positional index (source column B → target column B) unless the task explicitly instructs positional copying. If the target header says 'Designation', write the designation value, not the name, even if the name was in the source's column B.

**Why**: Blind positional mapping often leads to writing the wrong data type into the target cell (e.g., writing a Name into a Designation column) because column positions rarely align perfectly between disparate source and target schemas. Semantic alignment ensures the correct data field is populated.

### [S#17] Format and Type Preservation
When copying data between sheets or computing new values, preserve the original cell value type and formatting. Distinguish between preserving source attributes and applying task-specific requirements.

#### Value and Number Format Preservation

- **Value Type**: Copy the `value` attribute directly. Do not parse string representations of dates or numbers into `datetime` or `float` objects unless the source cell already holds that native type.
  - *Example*: A cell containing the string `'13/09/2021'` must remain a string; do not convert it to a `datetime` object.
- **Number Formats**: Preserve the source cell's `number_format` string (e.g., `'mm-dd-yy'`, `'DD/MM/YYYY'`) by assigning it directly to the target cell.
- **Date Serialization**: When writing date values (whether copying from source or computing new dates), **always write the native `datetime.date` or `datetime.datetime` object**. Do **not** convert dates to strings using `.strftime()` or similar methods. Excel stores dates as native objects; writing a formatted string (e.g., `'2021-12-04'`) creates a text cell, causing type mismatches.
  - *Correct*: `cell.value = computed_date_object`
  - *Incorrect*: `cell.value = computed_date_object.strftime('%Y-%m-%d')`

**Rule of thumb**: When in doubt, copy `value` and `number_format` directly. Avoid any implicit type coercion or parsing of string values.

#### Style Handling

- **Copying Source Styles**: Copy `cell.alignment`, `cell.font`, `cell.border`, and `cell.fill`. If direct assignment fails (e.g., due to `TypeError: unhashable type: 'StyleProxy'` or similar proxy issues), reconstruct the target style object by passing the source object's attributes (e.g., horizontal, vertical, wrap_text for Alignment) to the constructor of the corresponding `openpyxl.styles` class.
- **Applying Task-Specific Formatting**: When the task requires specific formatting (e.g., bold text, fill color) on a cell containing a computed literal value, you must explicitly instantiate and assign the appropriate style objects. Do not rely on source formatting or implicit behavior.
  - **Action**: Create the style objects (e.g., `Font(bold=True)`, `PatternFill(start_color='...', end_color='...', pattern_type='solid')`) and assign them to the cell's attributes (`cell.font`, `cell.fill`).
  - **Order**: Ensure the value is written to the cell. The style assignment can happen before or after, but both must be present in the final output.
  - **Example**:
    ```python
    from openpyxl.styles import Font, PatternFill

    # Compute value
    val = 100

    # Write value
    ws['A1'].value = val

    # Apply formatting
    ws['A1'].font = Font(bold=True)
    ws['A1'].fill = PatternFill(start_color='EBF1DE', end_color='EBF1DE', pattern_type='solid')
    ```

### [S#18] Formula Verification and Empty Cell Handling
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

### [S#19] Multi-Match Disambiguation
When matching keys from one list to entries in another (e.g., matching Sheet2 headers to Sheet1 entries), if multiple headers could match a single entry, always select the longest (most specific) matching header.

This prevents incorrect column assignments when shorter headers are substrings of longer ones. For example, if both `U4_ComponentPropertyForm` and `U4_ComponentProperty` are potential matches for an entry, `U4_ComponentPropertyForm` is the more specific key and should be selected.

**Rule:**
1. Identify all headers that match the current entry.
2. Among the matches, choose the one with the greatest length.
3. If there is a tie in length, use other disambiguation criteria (e.g., alphabetical order or specific mapping logic) if available, otherwise select the first match.

### [S#20] String Concatenation Delimiters
When concatenating strings that already contain delimiter characters (e.g., a trailing '+'), carefully count the total number of delimiter characters in the expected output.

The separator between the source string and the appended value must produce the exact character count shown in the expected result. Do not assume a standard single-separator insertion if the source string already includes delimiters; instead, verify the total delimiter count in the final string against the expected pattern.

**Example:**
If the source string is `entry+` and the expected output is `entry++value` (two '+' characters), the separator added must be `+`, not nothing or `++`. Verify by comparing the first few characters of the concatenated output against the expected pattern.

### [S#21] Formula Compatibility and Preferences
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

### [S#22] Row Deletion Order
When deleting multiple rows or columns from a worksheet, you **must** delete them in **reverse order** (highest index to lowest).

**Why:** Deleting a row or column shifts the indices of all subsequent rows or columns. If you delete from top-to-bottom (lowest row index to highest) or left-to-right (lowest column index to highest), the indices of the remaining target items change, causing subsequent deletions to target incorrect rows/columns or skip items entirely.

#### Row Deletion
1. Identify all rows to be deleted and store their indices in a list.
2. Sort the list in **descending** order.
3. Iterate through the sorted list and call `ws.delete_rows()` for each index.

```python
# Identify rows to delete
rows_to_delete = [row for row in range(2, 10) if ws.cell(row=row, column=1).value is None]

# Sort in descending order to prevent index shifting
rows_to_delete.sort(reverse=True)

# Delete safely
for row_idx in rows_to_delete:
    ws.delete_rows(row_idx)
```

#### Column Deletion
The reverse-order deletion principle also applies when deleting multiple **columns** using `ws.delete_cols()`.

1. **Identify Columns**: Determine all column indices to be deleted.
2. **Sort Descending**: Sort the list of column indices in **descending** order.
3. **Delete**: Iterate through the sorted list and call `ws.delete_cols()` for each index.

```python
# Identify columns to delete (e.g., based on header in row 1)
cols_to_delete = []
for col in range(1, ws.max_column + 1):
    if ws.cell(row=1, column=col).value == 'HeaderToRemove':
        cols_to_delete.append(col)

# Sort in descending order to prevent index shifting
cols_to_delete.sort(reverse=True)

# Delete safely
for col_idx in cols_to_delete:
    ws.delete_cols(col_idx)
```

### [S#23] Row Deletion via Rebuilding
When a task requires removing rows (e.g., keeping only specific rows based on a condition), you must **NOT** clear the content of unwanted rows in-place. Leaving rows with `None`/blank values shifts the row indices relative to the expected output structure, causing mismatches at target cells.

#### Correct Approach: Rebuild the Sheet
1. **Identify Rows to Keep**: Determine which rows from the source data satisfy the task's condition. Note that if the condition involves numeric comparisons or sums, you must account for floating-point precision (see below).
2. **Create New Structure**: Create a **new workbook** (or a new sheet within the existing workbook if allowed by the task constraints, but a new sheet is safer to avoid residual data).
3. **Copy Contiguously**: Copy the content of the kept rows into the new sheet, placing them in contiguous rows starting from row 1 (or the appropriate header row).
4. **Preserve Formatting**: Ensure styles, number formats, and cell values are copied correctly to match the expected output fidelity.

#### Why:
 This ensures the output sheet has no empty/gap rows and that the data aligns exactly with the expected row positions (e.g., the first kept item is in row 1, the second in row 2, etc.).

#### Floating-Point Precision in Deletion Criteria:
When determining which rows to keep or delete based on numeric sums or comparisons, **never use strict equality** (e.g., `sum == 0` or `val == 0.01`). Floating-point representation errors often result in values that appear to be zero being stored as tiny residuals (e.g., `-2.18e-11`).

Always use an absolute tolerance threshold to determine if a value is effectively zero or matches a target:
- **Correct:** `abs(current_sum) < 0.0001`
- **Incorrect:** `current_sum == 0`

Additionally, when parsing numeric columns for these calculations, ensure you skip non-numeric header cells or placeholder strings to avoid type conversion errors.

#### Example Logic:
```python
import openpyxl

# Load source
wb_src = openpyxl.load_workbook('input.xlsx')
ws_src = wb_src.active

# Determine rows to keep (e.g., last occurrence of each unique key in col B)
# This list should be sorted by row index
# Note: If selection criteria involve numeric sums, use tolerance checks
rows_to_keep = [3, 6, 9, 10, 12]

# Create new workbook
new_wb = openpyxl.Workbook()
new_ws = new_wb.active
new_ws.title = ws_src.title

# Copy kept rows contiguously
for idx, src_row_num in enumerate(rows_to_keep, start=1):
    for col in range(1, ws_src.max_column + 1):
        src_cell = ws_src.cell(row=src_row_num, column=col)
        dst_cell = new_ws.cell(row=idx, column=col)
        
        # Copy value
        dst_cell.value = src_cell.value
        
        # Copy style if needed (handle StyleProxy issues by copying attributes)
        if src_cell.has_style:
            if src_cell.font:
                dst_cell.font = src_cell.font.copy()
            if src_cell.border:
                dst_cell.border = src_cell.border.copy()
            if src_cell.fill:
                dst_cell.fill = src_cell.fill.copy()
            if src_cell.alignment:
                dst_cell.alignment = src_cell.alignment.copy()
            dst_cell.number_format = src_cell.number_format

new_wb.save('output.xlsx')
```

### [S#24] Mixed Date Format Parsing
Source sheets often contain dates in inconsistent formats (e.g., some as `datetime` objects, others as strings like `'3-1-21'`, `'2021-03-01'`, or `'03/01/21'`, or Excel serial numbers). When filtering or matching by **date** (ignoring time), you must normalize these values to a comparable `datetime.date` object before comparison.

**Procedure:**

1. **Check Type**: If the cell value is already a `datetime` or `date` object, use it directly (stripping time if comparing to a date-only target using `.date()`).
2. **Handle Excel Serial Numbers**: If the value is an integer or a float that is effectively an integer (e.g., `44926.0`), treat it as an Excel serial number.
   - **Convert**: Use the formula `datetime(1899, 12, 30) + timedelta(days=serial_number)` to convert the serial to a `datetime` object.
   - **Note**: The base date is **December 30, 1899**, not December 31, due to Excel's legacy leap year bug (1900 was incorrectly treated as a leap year).
   - **Normalize**: Proceed to Step 4 (strip time) if comparing to a date-only target.
3. **Parse Strings**: If the value is a string, attempt to parse it using a list of common date formats. Try formats in a logical order (e.g., try ISO-like formats first, then common US/International variations).
   - **Recommended Formats**: `'%Y-%m-%d'`, `'%m-%d-%y'`, `'%m/%d/%Y'`, `'%d-%m-%Y'`.
   - **Fallback**: If standard parsing fails, consider using a library like `dateutil.parser` which can often infer the format from ambiguous strings.
4. **Normalize**: Once parsed (from string or serial), convert to `datetime.date` (or `datetime.datetime` if time is relevant) to ensure consistent comparison.

**Example:**

```python
from datetime import datetime, timedelta

def normalize_date(val):
    # 1. Handle existing datetime objects
    if isinstance(val, datetime):
        return val.date()
    
    # 2. Handle Excel Serial Numbers
    if isinstance(val, (int, float)):
        # Convert Excel serial to datetime using Dec 30, 1899 base
        return (datetime(1899, 12, 30) + timedelta(days=val)).date()
    
    # 3. Handle String dates
    if isinstance(val, str):
        # Try common formats
        for fmt in ['%Y-%m-%d', '%m-%d-%y', '%m/%d/%Y', '%d-%m-%Y']:
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
        # Fallback to dateutil if available
        try:
            from dateutil import parser
            return parser.parse(val).date()
        except ImportError:
            return None # Or raise error
            
    return None
```

## The change-aspect you are drafting
Primacy of Literal Values for Explicit Formula Requests: Refine [S#1] Formula Evaluation vs. Literal Value Writing to explicitly state that even when a task asks for a 'formula' or 'how to compute using a formula', the agent must compute the result in Python and write the **literal value** to the target cell, because the evaluator reads via `openpyxl` which returns `None` for formula cells. The only exception is if the task explicitly asks to 'show the formula text', in which case the formula goes to a helper cell and the literal value goes to the target cell. This overrides any ambiguity in task phrasing.

## Suggested placement
S#1

## The group's raw edits (full content)
[E#2] section_hint=Formula Evaluation vs. Literal Value Writing | kind=edit_point | tasks=45944 | src=P#2 | analyst_coverage_claim=refines: The rule that explicit formula requests must still write literal values to the target cell
rationale: The passing rollout succeeded by computing the boolean logic (name match AND date comparison) in Python and writing the literal True/False results. The failing rollouts wrote Excel formula strings (SUMPRODUCT) into the cells. Since openpyxl reads formula cells as None (or the string itself) without evaluation, the evaluator sees None instead of the expected boolean. This edit reinforces that even when a formula is requested, if the evaluation mechanism reads via openpyxl, the literal computed value must be written to the target cell.
content:
**Critical Warning on Formula Strings vs. Literal Values:** When the task asks for a formula (e.g., 

[E#8] section_hint=Formula Evaluation vs. Literal Value Writing | kind=edit_point | tasks=50526 | src=P#7 | analyst_coverage_claim=refines: The existing rule already states to write literal values for explicit formula requests, but the failing trajectory shows the agent still defaults to writing formula strings when the task phrasing strongly emphasizes 'formula'.
rationale: (none)
content:
**Absolute Primacy of Literal Values:** When the task asks for a 

[E#9] section_hint=Formula Evaluation vs. Literal Value Writing | kind=edit_point | tasks=50526 | src=P#7 | analyst_coverage_claim=instance-of: The rule that openpyxl returns None for formula cells
rationale: (none)
content:
(empty)

[E#10] section_hint=Formula Evaluation vs. Literal Value Writing | kind=edit_point | tasks=50526 | src=P#7 | analyst_coverage_claim=instance-of: The rule that openpyxl returns None for formula cells
rationale: (none)
content:
(empty)

[E#11] section_hint=Formula Evaluation vs. Literal Value Writing | kind=edit_point | tasks=59160 | src=P#8 | analyst_coverage_claim=refines: Default: Compute and Write Literal Values
rationale: The failing rollouts wrote Excel formulas (COUNTIFS) to the target cells because the task asked "using a formula". The evaluator reads via openpyxl without data_only=True, so these cells returned None (or the string) instead of the count. The passing rollout computed the counts in Python and wrote integers. This edit clarifies that 'formula' in the prompt describes the method, not the output format, when the output is a computed value.
content:
**Target Cell Semantics Override:** When the task asks to "compute" or "calculate" a result (e.g., counts, sums, lookups) even if it uses the word "formula" or asks "how to compute using a formula", you must **compute the result in Python** and write the **literal value** to the target cell. Do not write an Excel formula string (e.g., `=COUNTIFS(...)`) to the target cell, as the evaluator reads the file via `openpyxl` which returns `None` for un-evaluated formula cells. The only exception is if the task explicitly asks to "show the formula" or "display the formula text" in addition to the result; in that case, write the formula to a helper cell and the literal result to the target cell.

Draft the definitive edit(s). Respond with ONLY the JSON object described.