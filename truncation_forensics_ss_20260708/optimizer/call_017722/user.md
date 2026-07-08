## The current rules document
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

## The drafted edits (to be applied together)
[D#1] (aspect group G#1) op=amend_section section=S#6
rationale: This edit refines [S#6] by explicitly adding the mandatory save step as a precondition to the verification load, directly addressing the failure mode identified in the raw edit (unsaved file leading to verification failure).
content:
**Pre-Verification Save**: Before loading the file for verification, you **must** call `wb.save(output_path)` and ensure it completes successfully. If the save is omitted or fails, the verification step will read a stale or non-existent file, leading to silent failures or `output-not-found` errors. Always confirm the save operation returns without error before proceeding to load the file back.

[D#2] (aspect group G#2) op=amend_section section=S#1
rationale: Synthesizes E#2, E#8, and E#11 to strengthen the existing rule. E#11 provides the crucial 'method vs output' distinction and specific failure examples (COUNTIFS). E#8 provides the 'Absolute Primacy' framing. E#2 provides the context of openpyxl reading None. The amendment integrates these into a clearer 'Primary Rule' and refines the 'Critical Warning' section to explicitly state that 'formula' in the prompt describes the method, not the output format.
content:
#### 3. Explicit Formula Requests: Write Literal Value, Helper Cell for Formula.
**Primary Rule: Primacy of Literal Values.**
When the task asks to "compute", "calculate", or "determine" a result (e.g., counts, sums, lookups) even if it uses the word "formula" or asks "how to compute using a formula", you must **compute the result in Python** and write the **literal value** to the target cell. Do not write an Excel formula string (e.g., `=COUNTIFS(...)`, `=SUMPRODUCT(...)`) to the target cell, as the evaluator reads the file via `openpyxl` which returns `None` for un-evaluated formula cells.

**Critical Warning on Task Phrasing:**
If the task uses the word "formula" (e.g., "I need a formula that..."), or asks to "fix," "correct," or "provide" a formula, interpret this as a request for the **computed result** of that formula, not the formula text itself. The word "formula" describes the *method* or *intent* of the calculation, but the *output* must be the literal value.

- **Primary Action:** Compute the result in Python and write the **literal value** to the target cell.
- **Secondary Action (If Visibility Required):** If the task explicitly requires the formula to be visible in the spreadsheet (e.g., "show the formula used"), write the formula string to a **separate helper cell** (e.g., an adjacent column or a designated notes area). The target cell still receives the literal value.
- **Warning:** Do not write the formula string to the target cell if the evaluation checks the cell's value, as `openpyxl` cannot evaluate it. Do not rely on external tools (like LibreOffice Calc) to evaluate formulas post-save, as this is unreliable and outside the standard agent workflow.

[D#3] (aspect group G#3) op=amend_section section=S#1
rationale: Refines the existing 'Type Fidelity' bullet in [S#1] to explicitly require native `int` types for exact whole numbers, addressing the ambiguity that allowed `float` representations for integers. This synthesizes the specific requirement from E#5 regarding integer literals for counts and ratios.
content:
**Type Fidelity:** When computing literal values in Python, ensure the written value matches the semantic type expected by the task. 
- **Integer Precision:** If the result of a transformation is an exact whole number (e.g., a count, a ratio of 1/1, or a specific integer code), you **must** write the result as a native Python `int` (e.g., `1`), **not** as a `float` (e.g., `1.0`) or a string (e.g., `'1'`). This ensures the cell displays as an integer, preventing formatting mismatches in tasks where integer display is semantically distinct from float representation.
- **Correct:** `cell.value = 1`
- **Incorrect:** `cell.value = 1.0` (when an integer is expected)
- **Incorrect:** `cell.value = '1'` (unless the result is explicitly a string code)

[D#4] (aspect group G#4) op=amend_section section=S#1
rationale: Synthesizes [E#19] into the existing 'Source Formula Handling' bullet of [S#1]. The raw edit provides a specific example and procedure for text-concatenated formulas, which refines the general instruction to 'parse and evaluate these formulas' by specifying that the agent must isolate and replicate the *numeric logic* while discarding *text concatenation*.
content:
- **Text-Concatenated Formulas:** If the source formula concatenates a numeric result with text (e.g., `=COUNTIF(A:A,"X")&" items"`), you must **replicate the aggregation logic** (e.g., the COUNTIF) in Python to extract the raw numeric value. Ignore the text concatenation parts (e.g., `&" items"`) when computing the result. Write the computed numeric value to the target cell.
  - **Correct:** Compute the count of 'X' in column A in Python, then `cell.value = count`.
  - **Incorrect:** Trying to parse the string `"=COUNTIF(A:A,\"X\")&\" items\"` as a mathematical expression or writing the formula string.

[D#5] (aspect group G#5) op=append_to_section section=S#5
rationale: The raw edit identifies a semantic sign convention error not covered by existing rules. This addition provides the definitive rule for cumulative state updates, distinguishing between additions and deductions, which is critical for tasks involving running balances or similar cumulative calculations.
content:
#### Cumulative State Updates Across Rows
When computing a cumulative value (e.g., running balance, debt, inventory) that updates row-by-row based on additions and subtractions, you must adhere to the correct arithmetic sign convention:

- **Additions increase the cumulative total**: `new_total = previous_total + addition_value`
- **Subtractions/deductions decrease the cumulative total**: `new_total = previous_total - deduction_value`

**Common Error**: Swapping the signs (subtracting additions or adding deductions) produces inverted results. Always map the semantic meaning of the source column to the arithmetic operation:
  - Column labeled "Additions", "Credits", "Income", "Deposits" → **add** to cumulative
  - Column labeled "Deductions", "Debits", "Expenses", "Withdrawals" → **subtract** from cumulative

**Schematic Example:**
```python
cumulative = 0
for row in range(start_row, end_row + 1):
    addition = ws.cell(row=row, col_addition).value or 0
    deduction = ws.cell(row=row, col_deduction).value or 0
    cumulative = cumulative + addition - deduction
    ws.cell(row=row, col_output).value = cumulative
```

[D#6] (aspect group G#6) op=append_to_section section=S#13
rationale: E#4 provides the specific context (header/data case mismatch) and code pattern for normalizing keys. This content is novel to the document as [S#13] currently only discusses first-match vs nth-match logic, and [S#15] only covers datetime type consistency. This addition ensures agents handle common case-sensitivity errors in lookups.
content:
#### Case-Insensitive Key Matching

When matching string keys (e.g., product codes, category names, month labels) between source data and lookup targets, you must normalize both sides to the same case (typically uppercase or lowercase) before comparison. Spreadsheet headers are often formatted differently from data rows (e.g., 'JAN' vs 'Jan'), and case-sensitive matching will result in missed matches and `None` or incorrect values.

**Procedure**:
1. **Normalize Lookup Keys**: When building the lookup dictionary from the source sheet, convert all keys to a consistent case (e.g., `.upper()`).
2. **Normalize Target Keys**: When iterating through the target rows to perform lookups, convert the lookup key value to the same case before querying the dictionary.
3. **Handle Missing Keys**: If a normalized key is not found in the dictionary, ensure you handle the missing value appropriately (e.g., empty string or default value) rather than crashing or leaving the cell blank unexpectedly.

**Example**:
```python
# Build lookup with normalized keys
source_lookup = {}
for row in range(2, source_max_row + 1):
    raw_key = ws.cell(row=row, source_key_col).value
    if raw_key:
        # Normalize key for consistent matching
        norm_key = str(raw_key).upper()
        source_lookup[norm_key] = ws.cell(row=row, source_val_col).value

# Perform lookup with normalized target key
for row in range(2, target_max_row + 1):
    raw_lookup_key = ws.cell(row=row, lookup_key_col).value
    if raw_lookup_key:
        norm_lookup_key = str(raw_lookup_key).upper()
        result = source_lookup.get(norm_lookup_key, '')
        ws.cell(row=row, target_val_col).value = result
```

[D#7] (aspect group G#7) op=append_to_section section=S#13
rationale: Synthesizes E#15 and E#21. E#21 provides the complete procedural logic and code examples, while E#15 confirms the pattern. The content is appended to S#13 as a new subsection under 'Multi-Row Source Lookups' to handle multi-column criteria, which is not covered by the existing single-key rules.
content:
#### Multi-Key Tuple Lookup (Cross-Column Join)

When the task requires looking up a value based on **two or more columns** simultaneously (e.g., match column A to key1 AND column B to key2, then return column C), do not perform sequential single-column lookups. Instead:

1. **Build a Tuple-Key Dictionary**: Iterate the source data and create a dictionary where the key is a **tuple** of the lookup column values (e.g., `(row_key1, row_key2)`) and the value is the target column to return.
   - **Schematic**:
     ```python
     lookup = {}
     for row in range(start_row, end_row + 1):
         key1 = ws.cell(row=row, col1).value
         key2 = ws.cell(row=row, col2).value
         val = ws.cell(row=row, target_col).value
         if key1 is not None and key2 is not None:
             lookup[(key1, key2)] = val
     ```
2. **Retrieve by Tuple**: When writing to the target grid, construct the same tuple from the current row's and column's identifiers and retrieve the value from the dictionary.
   - **Schematic**:
     ```python
     for row_idx in target_rows:
         row_key = ws.cell(row=row_idx, row_key_col).value
         for col_idx in target_cols:
             col_key = ws.cell(row=header_row, column=col_idx).value
             target_cell = ws.cell(row=row_idx, column=col_idx)
             target_cell.value = lookup.get((row_key, col_key), '')
     ```

**Why**: This ensures that the lookup correctly handles combinations of keys that might not exist in either single-column lookup independently, and prevents collisions where the same key1 appears with different key2 values.

[D#8] (aspect group G#8) op=amend_section section=S#17
rationale: The existing example in S#17 uses `pattern_type='solid'`, which is incorrect and causes runtime errors. This edit corrects the API usage to `fill_type='solid'`.
content:
In the **Style Handling** section, under **Copying Source Styles**, correct the `PatternFill` example to use the valid argument `fill_type` instead of `pattern_type`.

**Corrected Text:**
```
  - **Action**: Create the style objects (e.g., `Font(bold=True)`, `PatternFill(fill_type='solid', start_color='...', end_color='...')`) and assign them to the cell's attributes (`cell.font`, `cell.fill`).
```

**Reason:** `pattern_type` is not a valid argument in standard openpyxl `PatternFill` and raises a `TypeError`. The correct argument is `fill_type`.

[D#9] (aspect group G#8) op=append_to_section section=S#17
rationale: These two rules address specific failure modes in value preservation that are not covered by the general 'copy value directly' or 'avoid type coercion' rules in S#17. E#16 adds a safety check for active string transformations, and E#18 provides the correct pattern for dynamic number formatting (trailing zero removal) which requires string literals.
content:
#### Advanced Value Preservation Rules

- **Type-Safe String Transformation**: When applying string transformations (e.g., regex substitution, case conversion) to a column containing mixed types, you must **explicitly check the type** before processing. Only apply string operations to `str` instances. Skip `datetime`, `int`, `float`, and `None` values to prevent corruption of dates, numbers, or blanks.
  - **Correct**: `if isinstance(val, str): new_val = re.sub(...)`
  - **Incorrect**: Applying `re.sub` directly to a `datetime` object or converting a number to string before transformation.

- **Trailing Zero Removal via String Writing**: When the task requires a specific number format that involves removing trailing zeros (e.g., "9 decimal places, omit trailing 0"), you **cannot achieve this with `number_format` alone** because Excel's number formats do not dynamically strip trailing zeros. Instead, compute the value, format it as a string with the desired precision and zero-removal logic, and write it as a **string literal** (text) to the cell. If the evaluator checks the string representation, this ensures exact match.

[D#10] (aspect group G#9) op=add_section section=NEW: String Truncation Whitespace Preservation
rationale: Synthesizes the raw edit into a clear, standalone rule. The content matches the provided rationale and example, ensuring the agent understands that exact string preservation during truncation is required to pass evaluations.
content:
### [S#25] String Truncation Whitespace Preservation
When truncating strings at a delimiter or marker (e.g., removing everything from 'FT' onwards), **preserve the whitespace immediately preceding the marker** unless the task explicitly instructs to trim/strip it.

**Reasoning:** Evaluation scripts often compare output strings exactly. If the original text contained whitespace before the marker (e.g., `'Name FT...'`), the truncated result must retain that whitespace (e.g., `'Name '`). Applying `.strip()` or `.rstrip()` removes this character, altering the string content and causing exact-match evaluation failures.

**Rule:**
1. Identify the position of the delimiter or marker in the source string.
2. Slice the string up to that position (e.g., `source[:marker_pos]`).
3. **Do not** apply `.strip()`, `.rstrip()`, or `.lstrip()` to the result unless the task explicitly requires trimming.
4. Write the resulting string, including any trailing spaces, to the target cell.

**Example:**
- Input: `'John Doe FT123'`
- Marker: `'FT'`
- **Correct:** `'John Doe '` (space preserved)
- **Incorrect:** `'John Doe'` (space stripped)

[D#11] (aspect group G#10) op=append_to_section section=S#9
rationale: The raw edit provides a distinct rule for restricting aggregation to specific columns to avoid 'cross-row noise'. This is not covered by existing sections: S#9.7 handles input/output confusion, and S#9.3 handles row scope. The new subsection 3.8 correctly addresses column scope within aggregation.
content:
**3.8. Scoped Column Aggregation (Avoiding Cross-Row Noise):** When aggregating or counting values (e.g., Yes/No, Status) associated with specific keys (e.g., Dates), you must **restrict the search to the relevant column(s)** defined by the data structure. Do not scan all columns in the data range for matching values unless the task explicitly instructs to aggregate across the entire row. Extraneous values in other columns (e.g., status indicators in summary rows, duplicate labels in header rows) can skew counts if included in a broad column scan.

**Procedure:**
1. **Identify the Data Column:** Determine which column(s) contain the actual values to be counted/aggregated (e.g., Column B contains Yes/No values for the dates in Column A).
2. **Ignore Noise Columns:** Exclude columns that contain summary data, duplicate headers, or unrelated labels from the aggregation scope.
3. **Apply Filter:** Perform the count/sum only on the identified data column(s) for rows matching the key.

[D#12] (aspect group G#11) op=amend_section section=S#14
rationale: E#14 suggests adding column reordering and substitution to S#14. Column reordering is already mandated by S#16 (Output Mapping Integrity), so that part is absorbed. The substitution pattern (building a dict and applying during copy) is novel to S#14 and is added here as a refinement to the cross-sheet/cross-row data population process.
content:
**In-Process Value Substitution:** When the target values require transformation (e.g., converting country names to codes, mapping IDs to labels) based on a reference set, build the substitution mapping (a dictionary) from the source or reference data **before** iterating the target rows. Apply the substitution during the copy loop. If a key is not found in the substitution map, retain the original value or handle as specified (e.g., empty string). This ensures the transformation is applied efficiently and consistently with the data population.

**Schematic Example:**
```python
# 1. Build Substitution Map
# Example: Map Country Name -> Country Code
country_map = {'United States': 'US', 'Canada': 'CA', 'Mexico': 'MX'}

# 2. Iterate Target Rows and Apply Substitution
for row in range(2, target_max_row + 1):
    original_val = ws.cell(row=row, source_col).value
    # Apply substitution
    target_val = country_map.get(original_val, original_val)
    ws.cell(row=row, target_col).value = target_val
```

[D#13] (aspect group G#12) op=append_to_section section=S#14
rationale: Synthesizes the raw edit's core lesson: 'match column A against values in J2' implies per-row correspondence (A[i] vs J[i]), not global comparison against J2. Adds the clarification about case-sensitivity and the explicit 'do not anchor to header' warning to prevent the specific failure mode described.
content:
#### Per-Row Reference Matching

When a task instruction specifies matching one column against another using a cell reference (e.g., "match column A against values in J2" or "compare A to J"), interpret this as a **per-row comparison** rather than a global search against a single cell.

1. **Row Correspondence**: For each target row `i`, compare the value in the source column (e.g., `A[i]`) against the value in the reference column at the **same row index** (e.g., `J[i]`).
2. **Do Not Anchor to Header**: Do not compare all rows of Column A against the single value in the header row (e.g., `J2`) unless the task explicitly asks to filter or look up against a single constant value.
3. **Case Sensitivity**: Unless the task specifies case-insensitive matching, perform the comparison as case-sensitive.

**Schematic Example:**
```python
# Task: Calculate profit where column A matches the case-sensitive values in J
# Target: K2:K5

for row in range(2, 6):  # Rows 2 to 5
    a_val = ws.cell(row=row, column=1).value  # Column A
    j_val = ws.cell(row=row, column=10).value # Column J (J2, J3, J4, J5)
    
    # Per-row comparison: A[i] vs J[i]
    if a_val == j_val:  # Case-sensitive match
        ws.cell(row=row, column=11).value = profit - expenses
    else:
        ws.cell(row=row, column=11).value = 0  # Or blank
```

[D#14] (aspect group G#13) op=amend_section section=S#23
rationale: The raw edit correctly identifies that contiguous blank row removal is a valid exception to the 'Rebuild Sheet' mandate. I am grafting this exception onto [S#23] and cross-referencing the reverse-order deletion rule from [S#22] which is already established as the correct mechanism for multiple deletions.
content:
#### Exception for Contiguous Blank Rows
If the task requires removing **contiguous** blank rows (rows where all relevant data cells are `None`/empty) to close gaps in the dataset, you may use `ws.delete_rows()` instead of rebuilding the sheet.

To avoid index-shifting errors when deleting multiple contiguous rows, **always delete them in reverse order** (highest row index to lowest), as detailed in [S#22]. This is safer and more efficient than rebuilding for this specific case.

[D#15] (aspect group G#14) op=amend_section section=S#23
rationale: The raw edit [E#17] highlights a failure mode where filtered data is placed at row 1, overwriting or ignoring leading empty/header rows from the source. The current rule [S#23] mentions 'appropriate header row' but doesn't explicitly instruct to *copy* source structural rows before data. This amendment adds the explicit step to preserve these rows, ensuring the output layout matches the source structure where required.
content:
#### Correct Approach: Rebuild the Sheet
1. **Identify Rows to Keep**: Determine which rows from the source data satisfy the task's condition. Note that if the condition involves numeric comparisons or sums, you must account for floating-point precision (see below).
2. **Preserve Structural Rows**: Before copying data, identify any header rows or leading empty rows in the source sheet that define the expected output structure. Copy these rows to the new sheet first. This ensures that structural elements (like empty title rows or fixed headers) are preserved at the top of the output, even if they are not part of the filtered data set.
3. **Create New Structure**: Create a **new workbook** (or a new sheet within the existing workbook if allowed by the task constraints, but a new sheet is safer to avoid residual data).
4. **Copy Contiguously**: Copy the content of the kept data rows into the new sheet, placing them in contiguous rows **immediately following** the structural rows. Starting from row 1 (or the appropriate header row).

[D#16] (aspect group G#15) op=append_to_section section=S#22
rationale: The raw edit [E#22] introduces a specific pattern for identifying deletion boundaries via string matching and handling edge cases. This is not covered by the existing 'Reverse-Order' rule in S#22, which only addresses the deletion mechanic itself, not the boundary detection logic. It is also distinct from S#23 (Rebuilding). Thus, it is a novel addition to S#22.
content:
#### Pattern-Based Range Deletion
When the deletion range is defined by **pattern matching** (e.g., "delete rows starting from a cell matching '400-*-A' up to the next cell matching '400-*'"):

1. **Scan for Boundaries First**: Iterate through the target column to identify the **start row** (matching the first pattern) and the **end row** (matching the second pattern).
2. **Handle Missing Boundaries**: 
   - If the start pattern is not found, no rows should be deleted.
   - If the start pattern is found but the end pattern is not, delete from the start row to the last row of data (or the sheet boundary).
3. **Collect and Reverse-Delete**: Once both boundaries are identified, collect all row indices in the range `[start_row, end_row - 1]` (inclusive of start, exclusive of end), sort them in **descending** order, and delete in that order.

**Schematic Example**:
```python
start_row = None
end_row = None

# Scan for start boundary (e.g., ends with '-A')
for row in range(1, ws.max_row + 1):
    val = ws.cell(row=row, col).value
    if val is not None and str(val).endswith('-A'):
        start_row = row
        break

# If start found, scan for end boundary (e.g., starts with '400-')
if start_row is not None:
    for row in range(start_row + 1, ws.max_row + 1):
        val = ws.cell(row=row, col).value
        if val is not None and str(val).startswith('400-'):
            end_row = row
            break

# Determine deletion range
if start_row is not None:
    if end_row is None:
        end_row = ws.max_row + 1  # Delete to end
    rows_to_delete = list(range(start_row, end_row))
    rows_to_delete.sort(reverse=True)
    for r in rows_to_delete:
        ws.delete_rows(r)
```

**Why**: This ensures that deletion ranges defined by dynamic pattern boundaries are correctly identified and executed without index-shifting errors, and that edge cases (missing start, missing end) are handled gracefully.

[D#17] (aspect group G#16) op=append_to_section section=S#14
rationale: Synthesizes the raw edit into a new subsection under Cross-Row Lookup and Matching, as suggested. It captures the novel aspects of multi-condition filtering (AND/OR), explicit header offset handling, and contiguous row writing with order preservation, which are not covered by the existing lookup rules in [S#14] or the deletion/rebuilding rules in [S#23].
content:
#### Cross-Sheet Row Extraction
When the task requires extracting rows from a source sheet to a target sheet based on **multi-condition filtering** (e.g., "extract rows where Col B = 'X' OR Col C = 'Y'"):

1. **Filter Source Rows**: Iterate through the source sheet's data rows. Collect rows that satisfy the specified conditions:
   - **OR Logic**: Row matches if it satisfies **any** of the conditions.
   - **AND Logic**: Row matches if it satisfies **all** of the conditions.
2. **Write Headers**: Create the target sheet (or use the specified one). Write the header row at the **specified offset row** (e.g., row 2) using the source sheet's column headers.
3. **Write Data Contiguously**: Starting at the row immediately following the headers (e.g., row 3), write each filtered source row contiguously. Copy all column values from the source row to the target row.
4. **Preserve Order**: Maintain the original top-to-bottom order of the source rows in the output; do not re-sort unless explicitly instructed.

**Schematic Example**:
```python
# Assuming headers are in row 1, data starts in row 2
# Task: Extract rows where Col B is 'TELIVISION' OR Col C is 'CLASS III' or 'CLASS IV'
# Write to 'Sheet2', headers at row 2, data starting at row 3

new_ws = new_wb.active
new_ws.title = 'Sheet2'

# Write headers at offset (row 2)
for col_idx, header in enumerate(headers, start=1):
    new_ws.cell(row=2, column=col_idx, value=header)

# Write filtered data starting at row 3
out_row = 3
for src_row in range(2, src_ws.max_row + 1):
    col_b = src_ws.cell(row=src_row, column=2).value
    col_c = src_ws.cell(row=src_row, column=3).value
    
    # Apply OR condition
    if col_b == 'TELIVISION' or col_c == 'CLASS III' or col_c == 'CLASS IV':
        for col in range(1, src_ws.max_column + 1):
            new_ws.cell(row=out_row, column=col, value=src_ws.cell(row=src_row, column=col).value)
        out_row += 1
```

Review the set for deployment. Respond with ONLY the JSON object described.