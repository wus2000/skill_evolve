## Section: Multi-Row Source Lookups
When joining data from a source sheet that contains multiple rows for the same key combination, the behavior depends on whether the task requires the first occurrence, a specific sequential occurrence, or a combination of multiple columns. Before performing any lookup, ensure keys are correctly constructed and normalized.

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

#### Default Behavior: First Matching Row

In most cases, you should derive values from the **first** row that matches the join keys. Do not use the last row or any subsequent duplicate.

1. **Use the First Matching Row**: Always select the first row that matches the join keys.
2. **Limit Join Keys**: Use only the join keys explicitly required by the task (e.g., Meet Name, Race Number). Do not include additional columns (such as Date) in the join key unless they are explicitly specified as part of the key.

**Why**: This prevents data corruption where later duplicate rows (which may have different or outdated values) overwrite the canonical first entry. It also avoids mismatch errors caused by over-specifying join keys with non-key columns like Date. When building a lookup dictionary, ensure your logic selects the first occurrence of each key group (e.g., `index 0` in a grouped iteration) rather than allowing later rows to overwrite earlier ones.

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

#### Nth Occurrence Lookups (Exception to First Match)

When the task requires retrieving values based on the **order of appearance** of duplicate keys (e.g., the second occurrence of a product name should return the second associated value), the standard "first match" rule does not apply. Instead, you must track the occurrence count for each key.

**Procedure**:
1. **Build an Ordered List of Values:** Iterate through the source data and group values by key, preserving the order of appearance. Use a dictionary mapping each key to a list of its corresponding values.
2. **Track Lookup Occurrences:** As you iterate through the rows requiring the lookup, maintain a counter for each key encountered. Increment the counter for every instance of that key in the lookup sequence.
3. **Select by Index:** Use the occurrence count (1-based) to index into the source's value list (0-based index = count - 1). If the count exceeds the number of available matches, return an empty string or handle as specified.

**Schematic Example**:
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

## Edits to apply to THIS section
[A#1] op=amend_section
intended content:
#### Nth Occurrence Lookups (Exception to First Match)

**Critical Distinction: Independent vs. Shared Occurrence Counters**

The behavior for duplicate keys depends on whether the occurrences are **independent per target row** or **shared across the entire lookup sequence**. You must determine which pattern the task requires.

1. **Independent Counter (Default for Row-by-Row Lookup):**
   When the task requires looking up a value for *each* target row independently, every row with a given key should receive the **first** matching value from the source, regardless of how many times that key appeared in previous target rows.
   - **Mechanism:** Do NOT increment a shared occurrence counter across rows. For each target row, always look up the **1st** match in the source list for that key.
   - **When to use:** This is the standard behavior for tasks like "lookup the color for each product in Column A" where Column A has duplicate products.
   - **Example:** If Table 1 has 'AAA' in row 7 and row 10, and Table 2 has 'AAA' at positions 1 and 2:
     - Row 7 gets the 1st AAA match (position 1).
     - Row 10 also gets the 1st AAA match (position 1).

2. **Shared Counter (Sequential Lookup):**
   When the task requires retrieving values sequentially for duplicate keys in the target (e.g., filling multiple columns for the same row, or explicitly asking for the 1st, 2nd, 3rd occurrences), you must track a shared occurrence count.
   - **Mechanism:** Track how many times each key has been encountered in the *target* data so far. The nth occurrence of a key in the target gets the nth value from the source's list of matches.
   - **When to use:** This is required when the task implies a sequence or when transposing multiple matches into multiple columns for a *single* target row.
   - **Example:** If Table 1 has 'AAA' in row 7, and Table 2 has 'AAA' at positions 1 and 2:
     - The first column for row 7 gets the 1st AAA match.
     - The second column for row 7 gets the 2nd AAA match.
   - **Schematic for Shared Counter:**
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

#### Two-Tier Fallback Aggregation

When a task requires a lookup that first tries an **exact match** on multiple criteria, and then falls back to a **broader aggregation** (e.g., sum all values for a category) if the exact match is missing, implement this as a two-step check.

**Procedure**:
1. **Exact Match Check**: First, sum/aggregate only rows that match **all** specified lookup keys (e.g., Dept AND RU).
2. **Fallback Check**: If the exact match sum is zero (or no rows matched), aggregate over the **subset** of rows matching only the primary key (e.g., Dept only), ignoring the secondary key.
3. **Write Result**: Write the result of whichever step found data.

**Why**: This pattern handles hierarchical or conditional aggregation where specific combinations may not exist, requiring a generalization to the parent category. It prevents `None` or zero results when specific sub-categories are missing.

**Schematic Example**:
```python
# Source: (dept, ru, value)
# Target: (target_dept, target_ru) -> sum

exact_sum = sum(v for d, r, v in source if d == target_dept and r == target_ru)
if exact_sum > 0:
    result = exact_sum
else:
    # Fallback: sum all for dept regardless of ru
    result = sum(v for d, r, v in source if d == target_dept)
```

#### Semantic Condition Interpretation (Loss/Profit)
When a task condition involves "sold at a loss" or "loss," and the spreadsheet contains an explicit **Profit** (or Gain/Loss) column, define "loss" as **Profit < 0** (i.e., the explicit profit value is negative). Do NOT derive the loss condition by comparing Proceeds to Cost or Book Value, even if those columns are available. The explicit Profit column is the canonical source for determining gain/loss status.

**Procedure:**
1. **Check the Profit Column:** For each row, read the value in the Profit column (e.g., Column F).
2. **Determine Loss:** If the Profit value is negative (`< 0`), the asset was sold at a loss. If it is zero or positive, it was not sold at a loss.
3. **Apply Logic:** Use this determination to select the appropriate recoupment/calculation branch.

**Example:**
- Row with Profit = -200 → Sold at a loss → Use loss-specific formula (e.g., ITV + Profit).
- Row with Profit = 800 → Not a loss → Check next condition (e.g., Profit < Cost).

#### Row-by-Row Stateful Aggregation (Difference/Change Tracking)
When a task requires computing a value based on the **difference** between the current row and the **most recent previous row** for the same group key (e.g., "amount used since last usage", "change in weight"), you must track state across the iteration.

**Procedure**:
1. **Initialize State**: Create a dictionary (e.g., `last_value = {}`) to store the value from the most recent row for each group key.
2. **Iterate Rows**: Loop through the data rows in order.
3. **Compute Difference**: For each row, look up the group key in the state dictionary.
   - If the key exists, compute `difference = last_value[key] - current_value` (or `current - last`, depending on task semantics).
   - If the key does not exist (first occurrence), set the difference to a default (usually `0`).
4. **Update State**: After computing, update `last_value[key] = current_value` so the next row sees the correct previous value.
5. **Write Literal**: Write the computed difference as a literal integer/float.

**Why**: This pattern captures temporal or sequential dependencies within groups that cannot be solved by simple static lookups. The state must be updated *after* the current row's computation to avoid using the current row's value as its own previous value.

**Schematic Example**:
```python
last_weight = {}
for row in range(start, end + 1):
    key = ws.cell(row=row, col_key).value
    val = ws.cell(row=row, col_val).value
    if key in last_weight:
        diff = last_weight[key] - val
    else:
        diff = 0
    ws.cell(row=row, col_out).value = diff
    last_weight[key] = val
```

#### Conditional Key Selection (Year-Based Overrides)
When a lookup key is not static but depends on row-specific attributes (e.g., using a default month column for most years, but an override month for a specific year), you must determine the correct key/column dynamically for each row before performing the lookup.

**Procedure:**
1. **Map Headers to Indices:** Build a dictionary mapping column headers (normalized) to their column indices for the source data range.
2. **Per-Row Key Determination:** Iterate through the target rows. For each row, evaluate the conditional logic (e.g., check the year column) to decide which key or column index to use.
3. **Lookup and Write:** Use the determined key/index to fetch the value from the source and write the literal value to the target cell.

**Schematic Example (Month Lookup with Year Override):**
```python
# Map month headers to column indices
month_to_col = {}
for col in range(2, 14):  # Assuming B1:M1 are JAN-DEC
    header = ws.cell(row=1, column=col).value
    if header:
        month_to_col[str(header).upper()] = col

# Per-row lookup
for row in range(2, target_max_row + 1):
    year = ws.cell(row=row, year_col).value
    default_month = ws.cell(row=row, month_col).value
    
    # Determine lookup month
    if year == 2022:  # Override year
        lookup_month = 'JAN'
    else:
        lookup_month = str(default_month).upper() if default_month else None
    
    # Fetch value
    if lookup_month and lookup_month in month_to_col:
        target_cell = ws.cell(row=row, target_col)
        source_cell = ws.cell(row=row, column=month_to_col[lookup_month])
        target_cell.value = source_cell.value
    else:
        ws.cell(row=row, target_col).value = None
```

#### Grid Key Identification
When performing a grid lookup (filling a rectangular target grid based on source data), you must correctly identify which column in the source data provides the **row keys** (values corresponding to output rows) and which column provides the **column keys** (values corresponding to output columns).

- **Do not assume** that the first available column with unique values is the row key.
- **Verify against the output grid:** The values in the output grid's row labels (often in the first column of the grid area or adjacent to it) must match the values you use as the first element of your lookup tuple.
- **Common Pitfall:** In tasks with multiple data regions, the row keys might be in a column that is not the first column of the source data. Always align your lookup keys with the explicit row headers in the target structure.

#### Range-Based Multi-Criteria Lookup
When the lookup requires matching a categorical key (e.g., Package Type) AND a numeric value falling within a range (e.g., Weight Between From and To), a simple tuple-key dictionary is insufficient. Instead, build a list of lookup entries and iterate through them to find the matching range.

**Procedure**:
1. **Build Lookup List**: Iterate the source sheet and collect entries as dictionaries or tuples containing the category key, range bounds (min/max), and the target value.
2. **Iterate and Match**: For each target row, iterate through the lookup list. Find the entry where the category key matches AND the target numeric value falls within the range bounds (`min_val <= target <= max_val`).
3. **Return First Match**: Return the target value from the first matching entry.

**Schematic Example**:
```python
# Build list of range entries
lookup_list = []
for row in range(2, source_max_row + 1):
    category = ws.cell(row=row, cat_col).value
    min_val = ws.cell(row=row, min_col).value
    max_val = ws.cell(row=row, max_col).value
    price = ws.cell(row=row, val_col).value
    if category is not None:
        lookup_list.append({'cat': category, 'min': min_val, 'max': max_val, 'val': price})

# Match for each target row
for row in range(2, target_max_row + 1):
    target_cat = ws.cell(row=row, target_cat_col).value
    target_num = ws.cell(row=row, target_num_col).value
    
    result = None
    for entry in lookup_list:
        if entry['cat'] == target_cat and entry['min'] <= target_num <= entry['max']:
            result = entry['val']
            break
    
    ws.cell(row=row, target_val_col).value = result
```

**Why:** This pattern handles lookups where one criterion is exact match and another is a range containment, which is common in pricing, tax, and shipping tables. A simple dictionary lookup cannot handle range conditions.

**Handle Missing Keys:** When performing a lookup, if the source key from the target row is not found in the source lookup dictionary, write `None` to the target cell. This results in a blank cell in the spreadsheet. Do not attempt to infer a value, use a default fallback, or leave the cell with an error-like state unless explicitly instructed otherwise.

**Value Preservation and Distinction:**
When building lookup dictionaries or writing results to target cells, adhere to the following:

1. **Distinguish Empty String from None/Zero in Source Data:**
   Strictly distinguish between:
   - `None`: Structurally empty cell (missing data).
   - `''`: Empty string (often from exported data or explicit clearing; valid content).
   - `0` or `0.0`: Numeric zero (valid content).

   Do not treat `''` as equivalent to `None` unless the task explicitly defines empty strings as missing data. When checking for existence or validity, use explicit checks:
   - Use `value is not None` to check for presence (includes `0` and `''`).
   - Use `value != ''` to check for non-empty string content.
   - Avoid `if value:` checks for lookup validity, as they incorrectly filter out `0` and `''`.

   **Schematic:**
   ```python
   # Correct: Treat 0 as a valid value, '' as empty, None as missing
   if source_val is not None:  # Includes 0, 'text', 1.5
       if source_val != '':    # Excludes empty string
           # Process valid value
           pass
   ```

2. **Preserve Pre-Existing Target Values:**
   Before writing to a target cell, check if it already contains a non-None value. If the task instructs to "fill," "add," or "configure" cells, do not blindly overwrite all cells in the answer range.
   - **Procedure:**
     1. Read the current value of the target cell.
     2. If the current value is `None` or `''` (empty), write the computed result.
     3. If the current value is a non-empty, non-None value, **preserve it** unless the task explicitly demands full replacement or overwriting.
   
   **Schematic:**
   ```python
   target_cell = ws.cell(row=row, col=target)
   existing = target_cell.value
   if existing is None or existing == '':
       target_cell.value = computed_value
   # Else: preserve existing value unless task explicitly says to overwrite
   ```
(rationale: This single amend consolidates all remaining S#13-relevant edits from the previous groups. It refines 'Nth Occurrence' (E#1, E#19, E#20), adds 'Two-Tier Fallback' (E#29), 'Semantic Condition Interpretation' (E#16), 'Stateful Aggregation' (E#28), 'Conditional Key Selection' (E#5), 'Grid Key Identification' (E#13), 'Range-Based Lookup' (E#43), and refines 'Missing Keys/Value Preservation' (E#8, E#31, E#32). E#33 is excluded as it belongs in S#14.)
supersedes (retire these statements): Nth Occurrence Lookups (Exception to First Match) ... When the task requires retrieving values based on the order of appearance of duplicate keys ... the standard 'first match' rule does not apply. | If a normalized key is not found in the dictionary, ensure you handle the missing value appropriately (e.g., empty string or default value) rather than crashing or leaving the cell blank unexpectedly.

=== LOSSLESS REPAIR REQUIRED ===
Your previous output (below) LOST the following identifiers from the old section: they appear neither in new_section_text nor in any absorbed/dropped entry. Re-produce the COMPLETE output object, keeping your fusion but restoring each lost identifier — either weave its fact back into the text or declare it under absorbed/dropped with its destination/reason.

## Lost identifiers
- `(row_key1, row_key2)`

## Your previous output
{
 "application_notes": "The edit [A#1] is an 'amend_section' operation that effectively replaces the content of this section with a new, comprehensive set of rules. The rationale indicates it consolidates multiple previous edits. The 'supersedes' clause explicitly retires the old 'Nth Occurrence Lookups' header and the old 'Handle Missing Keys' advice. \n\nI have merged the incoming content into a single, logically structured section. \n1. **Key Matching & Normalization**: Combined the existing 'Case-Insensitive Key Matching' with the new 'Value Preservation and Distinction' and 'Handle Missing Keys' concepts. The new edit's detailed distinction between None, '', and 0 is stronger and supersedes the old vague 'handle missing value appropriately' advice. \n2. **Lookup Strategies**: The edit introduces several distinct lookup patterns. I have organized them by complexity/specificity:\n   - **Simple/First Match**: Retained the existing 'Default Behavior: First Matching Row' and 'Multi-Key Tuple Lookup' as they are standard and not contradicted.\n   - **Nth Occurrence**: Replaced the old 'Nth Occurrence Lookups' section with the new 'Critical Distinction: Independent vs. Shared Occurrence Counters'. This is a direct merge/replace as instructed by the 'supersedes' clause and the new content's detail.\n   - **Advanced/Stateful/Conditional**: Added the new subsections: 'Two-Tier Fallback Aggregation', 'Semantic Condition Interpretation', 'Row-by-Row Stateful Aggregation', 'Conditional Key Selection', 'Grid Key Identification', and 'Range-Based Multi-Criteria Lookup'.\n3. **Merges**: \n   - The old 'Handle Missing Keys' bullet was absorbed into the new 'Value Preservation and Distinction' section, which provides the rigorous logic for `None` vs `''` vs `0` and how to handle missing lookups (write `None`/blank).\n   - The old 'Nth Occurrence' code example was absorbed into the new 'Shared Counter' schematic.\n   - The new 'Grid Key Identification' and 'Range-Based' examples were integrated as distinct subsections.\n4. **Cleanup**: Removed redundant 'Why' explanations where the procedure was self-evident or covered by the general 'First Match' or 'Tuple' rules, but kept them where the logic is non-obvious (e.g., Stateful Aggregation, Range-Based). Ensured all code snippets are preserved as they contain specific implementation details (e.g., `defaultdict`, specific column logic).",
 "unapplied": [],
 "absorbed": [
  {
   "old": "Nth Occurrence Lookups (Exception to First Match) ... When the task requires retrieving values based on the order of appearance of duplicate keys ... the standard 'first match' rule does not apply.",
   "into": "Merged into 'Critical Distinction: Independent vs. Shared Occurrence Counters' which refines this behavior into two distinct mechanisms (Independent vs Shared)."
  },
  {
   "old": "If a normalized key is not found in the dictionary, ensure you handle the missing value appropriately (e.g., empty string or default value) rather than crashing or leaving the cell blank unexpectedly.",
   "into": "Merged into 'Value Preservation and Distinction' -> 'Handle Missing Keys' subsection, which now explicitly states to write `None` (resulting in blank) and distinguishes it from other empty states."
  },
  {
   "old": "Case-Insensitive Key Matching ... Procedure: 1. Normalize Lookup Keys... 2. Normalize Target Keys...",
   "into": "Retained as 'Case-Insensitive Key Matching' subsection, but updated with the new 'Value Preservation' logic for robustness."
  }
 ],
 "dropped": [],
 "new_section_text": "### Multi-Row Source Lookups\n\nWhen joining data from a source sheet that contains multiple rows for the same key combination, the behavior depends on whether the task requires the first occurrence, a specific sequential occurrence, or a combination of multiple columns. Before performing any lookup, ensure keys are correctly constructed, normalized, and that value states (None vs empty string) are handled precisely.\n\n#### Case-Insensitive Key Matching\n\nWhen matching string keys (e.g., product codes, category names) between source data and lookup targets, normalize both sides to the same case (typically uppercase or lowercase) before comparison. Spreadsheet headers often differ in case from data rows (e.g., 'JAN' vs 'Jan'), and case-sensitive matching causes missed matches.\n\n**Procedure**:\n1. **Normalize Lookup Keys**: When building the lookup dictionary, convert all keys to a consistent case (e.g., `.upper()`).\n2. **Normalize Target Keys**: When iterating through target rows, convert the lookup key value to the same case before querying.\n3. **Handle Missing Keys & Value Distinction**: See \"Value Preservation and Distinction\" below for rigorous handling of `None`, `''`, and `0`.\n\n**Example**:\n```python\n# Build lookup with normalized keys\nsource_lookup = {}\nfor row in range(2, source_max_row + 1):\n    raw_key = ws.cell(row=row, source_key_col).value\n    if raw_key: # Check is not None\n        norm_key = str(raw_key).upper()\n        source_lookup[norm_key] = ws.cell(row=row, source_val_col).value\n\n# Perform lookup with normalized target key\nfor row in range(2, target_max_row + 1):\n    raw_lookup_key = ws.cell(row=row, lookup_key_col).value\n    if raw_lookup_key:\n        norm_lookup_key = str(raw_lookup_key).upper()\n        result = source_lookup.get(norm_lookup_key, None) # Write None for missing\n        ws.cell(row=row, target_val_col).value = result\n```\n\n#### Default Behavior: First Matching Row\n\nIn most cases, derive values from the **first** row that matches the join keys. Do not use the last row or subsequent duplicates.\n\n1. **Use the First Matching Row**: Always select the first row that matches the join keys. When building a lookup dictionary, ensure your logic selects the first occurrence (e.g., `index 0` in grouped iteration) rather than allowing later rows to overwrite earlier ones.\n2. **Limit Join Keys**: Use only the join keys explicitly required by the task. Do not include additional columns (such as Date) in the join key unless explicitly specified. This prevents data corruption from outdated duplicate rows and mismatch errors from over-specifying keys.\n\n#### Multi-Key Tuple Lookup (Cross-Column Join)\n\nWhen the task requires looking up a value based on **two or more columns** simultaneously (e.g., match col A to key1 AND col B to key2, then return col C), do not perform sequential single-column lookups.\n\n1. **Build a Tuple-Key Dictionary**: Create a dictionary where the key is a **tuple** of the lookup column values and the value is the target column.\n   ```python\n   lookup = {}\n   for row in range(start_row, end_row + 1):\n       key1 = ws.cell(row=row, col1).value\n       key2 = ws.cell(row=row, col2).value\n       val = ws.cell(row=row, target_col).value\n       if key1 is not None and key2 is not None:\n           lookup[(key1, key2)] = val\n   ```\n2. **Retrieve by Tuple**: Construct the same tuple from the current row's identifiers and retrieve the value.\n   ```python\n   for row_idx in target_rows:\n       row_key = ws.cell(row=row_idx, row_key_col).value\n       for col_idx in target_cols:\n           col_key = ws.cell(row=header_row, column=col_idx).value\n           target_cell = ws.cell(row=row_idx, column=col_idx)\n           target_cell.value = lookup.get((row_key, col_key), None)\n   ```\n\n#### Critical Distinction: Independent vs. Shared Occurrence Counters\n\nWhen duplicate keys exist in the source, the behavior depends on whether occurrences are **independent per target row** or **shared across the entire lookup sequence**.\n\n1. **Independent Counter (Default for Row-by-Row Lookup):**\n   - **Mechanism**: Do NOT increment a shared counter across rows. For each target row, always look up the **1st** match in the source list for that key.\n   - **When to use**: Standard tasks like \"lookup the color for each product in Column A\" where Column A has duplicates. Every instance of 'AAA' in the target gets the 1st 'AAA' from the source.\n\n2. **Shared Counter (Sequential Lookup):**\n   - **Mechanism**: Track how many times each key has been encountered in the *target* data so far. The nth occurrence of a key in the target gets the nth value from the source's list of matches.\n   - **When to use**: Required when transposing multiple matches into multiple columns for a *single* target row, or when explicitly asking for 1st, 2nd, 3rd occurrences.\n   - **Schematic**:\n     ```python\n     from collections import defaultdict\n\n     # Step 1: Build ordered list of values for each key\n     source_values = defaultdict(list)\n     for row in range(2, source_max_row + 1):\n         key = ws.cell(row=row, source_key_col).value\n         val = ws.cell(row=row, source_val_col).value\n         if key:\n             source_values[key].append(val)\n\n     # Step 2 & 3: Track occurrences and retrieve nth value\n     occurrence_counts = defaultdict(int)\n     for row in range(2, lookup_max_row + 1):\n         key = ws.cell(row=row, lookup_key_col).value\n         if key:\n             occurrence_counts[key] += 1\n             nth = occurrence_counts[key]\n             # Retrieve the nth value (nth-1 for 0-based list)\n             if nth <= len(source_values[key]):\n                 result = source_values[key][nth - 1]\n             else:\n                 result = None # Handle missing occurrence\n             ws.cell(row=row, lookup_val_col).value = result\n     ```\n\n#### Two-Tier Fallback Aggregation\n\nWhen a task requires a lookup that first tries an **exact match** on multiple criteria, and then falls back to a **broader aggregation** (e.g., sum all values for a category) if the exact match is missing:\n\n1. **Exact Match Check**: Sum/aggregate only rows that match **all** specified lookup keys (e.g., Dept AND RU).\n2. **Fallback Check**: If the exact match sum is zero (or no rows matched), aggregate over the **subset** of rows matching only the primary key (e.g., Dept only).\n3. **Write Result**: Write the result of whichever step found data.\n\n**Schematic**:\n```python\n# Source: (dept, ru, value)\nexact_sum = sum(v for d, r, v in source if d == target_dept and r == target_ru)\nif exact_sum > 0:\n    result = exact_sum\nelse:\n    # Fallback: sum all for dept regardless of ru\n    result = sum(v for d, r, v in source if d == target_dept)\n```\n\n#### Semantic Condition Interpretation (Loss/Profit)\n\nWhen a task condition involves \"sold at a loss\" or \"loss,\" and the spreadsheet contains an explicit **Profit** (or Gain/Loss) column, define \"loss\" as **Profit < 0**. Do NOT derive the loss condition by comparing Proceeds to Cost, even if those columns are available. The explicit Profit column is the canonical source.\n\n**Procedure**:\n1. **Check the Profit Column**: Read the value in the Profit column.\n2. **Determine Loss**: If Profit < 0, the asset was sold at a loss. If >= 0, it was not.\n3. **Apply Logic**: Use this determination to select the appropriate calculation branch.\n\n#### Row-by-Row Stateful Aggregation (Difference/Change Tracking)\n\nWhen a task requires computing a value based on the **difference** between the current row and the **most recent previous row** for the same group key (e.g., \"amount used since last usage\"):\n\n1. **Initialize State**: Create a dictionary (e.g., `last_value = {}`) to store the value from the most recent row for each group key.\n2. **Iterate Rows**: Loop through the data rows in order.\n3. **Compute Difference**: Look up the group key in the state dictionary.\n   - If the key exists, compute `difference = last_value[key] - current_value` (or `current - last`, depending on task semantics).\n   - If the key does not exist (first occurrence), set the difference to a default (usually `0`).\n4. **Update State**: After computing, update `last_value[key] = current_value` so the next row sees the correct previous value.\n\n**Schematic**:\n```python\nlast_weight = {}\nfor row in range(start, end + 1):\n    key = ws.cell(row=row, col_key).value\n    val = ws.cell(row=row, col_val).value\n    if key in last_weight:\n        diff = last_weight[key] - val\n    else:\n        diff = 0\n    ws.cell(row=row, col_out).value = diff\n    last_weight[key] = val\n```\n\n#### Conditional Key Selection (Year-Based Overrides)\n\nWhen a lookup key is not static but depends on row-specific attributes (e.g., using a default month column for most years, but an override month for a specific year):\n\n1. **Map Headers to Indices**: Build a dictionary mapping column headers (normalized) to their column indices for the source data range.\n2. **Per-Row Key Determination**: Iterate through target rows. For each row, evaluate the conditional logic (e.g., check the year column) to decide which key or column index to use.\n3. **Lookup and Write**: Use the determined key/index to fetch the value from the source.\n\n**Schematic (Month Lookup with Year Override)**:\n```python\n# Map month headers to column indices\nmonth_to_col = {}\nfor col in range(2, 14):  # Assuming B1:M1 are JAN-DEC\n    header = ws.cell(row=1, column=col).value\n    if header:\n        month_to_col[str(header).upper()] = col\n\n# Per-row lookup\nfor row in range(2, target_max_row + 1):\n    year = ws.cell(row=row, year_col).value\n    default_month = ws.cell(row=row, month_col).value\n    \n    # Determine lookup month\n    if year == 2022:  # Override year\n        lookup_month = 'JAN'\n    else:\n        lookup_month = str(default_month).upper() if default_month else None\n    \n    # Fetch value\n    if lookup_month and lookup_month in month_to_col:\n        target_cell = ws.cell(row=row, target_col)\n        source_cell = ws.cell(row=row, column=month_to_col[lookup_month])\n        target_cell.value = source_cell.value\n    else:\n        ws.cell(row=row, target_col).value = None\n```\n\n#### Grid Key Identification\n\nWhen performing a grid lookup (filling a rectangular target grid based on source data), correctly identify which column in the source data provides the **row keys** and which provides the **column keys**.\n\n- **Do not assume** that the first available column with unique values is the row key.\n- **Verify against the output grid**: The values in the output grid's row labels must match the values you use as the first element of your lookup tuple.\n- **Common Pitfall**: In tasks with multiple data regions, the row keys might be in a column that is not the first column of the source data. Always align your lookup keys with the explicit row headers in the target structure.\n\n#### Range-Based Multi-Criteria Lookup\n\nWhen the lookup requires matching a categorical key AND a numeric value falling within a range (e.g., Weight Between From and To), a simple tuple-key dictionary is insufficient.\n\n1. **Build Lookup List**: Iterate the source sheet and collect entries as dictionaries/tuples containing the category key, range bounds (min/max), and the target value.\n2. **Iterate and Match**: For each target row, iterate through the lookup list. Find the entry where the category key matches AND the target numeric value falls within the range bounds (`min_val <= target <= max_val`).\n3. **Return First Match**: Return the target value from the first matching entry.\n\n**Schematic**:\n```python\n# Build list of range entries\nlookup_list = []\nfor row in range(2, source_max_row + 1):\n    category = ws.cell(row=row, cat_col).value\n    min_val = ws.cell(row=row, min_col).value\n    max_val = ws.cell(row=row, max_col).value\n    price = ws.cell(row=row, val_col).value\n    if category is not None:\n        lookup_list.append({'cat': category, 'min': min_val, 'max': max_val, 'val': price})\n\n# Match for each target row\nfor row in range(2, target_max_row + 1):\n    target_cat = ws.cell(row=row, target_cat_col).value\n    target_num = ws.cell(row=row, target_num_col).value\n    \n    result = None\n    for entry in lookup_list:\n        if entry['cat'] == target_cat and entry['min'] <= target_num <= entry['max']:\n            result = entry['val']\n            break\n    \n    ws.cell(row=row, target_val_col).value = result\n```\n\n#### Value Preservation and Distinction\n\nWhen building lookup dictionaries or writing results to target cells, adhere to the following strict distinctions:\n\n1. **Distinguish Empty String from None/Zero in Source Data:**\n   - `None`: Structurally empty cell (missing data).\n   - `''`: Empty string (often from exported data or explicit clearing; valid content).\n   - `0` or `0.0`: Numeric zero (valid content).\n\n   Do not treat `''` as equivalent to `None` unless the task explicitly defines empty strings as missing data. When checking for existence or validity, use explicit checks:\n   - Use `value is not None` to check for presence (includes `0` and `''`).\n   - Use `value != ''` to check for non-empty string content.\n   - Avoid `if value:` checks for lookup validity, as they incorrectly filter out `0` and `''`.\n\n   **Schematic**:\n   ```python\n   # Correct: Treat 0 as a valid value, '' as empty, None as missing\n   if source_val is not None:  # Includes 0, 'text', 1.5\n       if source_val != '':    # Excludes empty string\n           # Process valid value\n           pass\n   ```\n\n2. **Preserve Pre-Existing Target Values:**\n   Before writing to a target cell, check if it already contains a non-None value. If the task instructs to \"fill,\" \"add,\" or \"configure\" cells, do not blindly overwrite all cells in the answer range.\n   - **Procedure**:\n     1. Read the current value of the target cell.\n     2. If the current value is `None` or `''` (empty), write the computed result.\n     3. If the current value is a non-empty, non-None value, **preserve it** unless the task explicitly demands full replacement or overwriting.\n   \n   **Schematic**:\n   ```python\n   target_cell = ws.cell(row=row, col=target)\n   existing = target_cell.value\n   if existing is None or existing == '':\n       target_cell.value = computed_value\n   # Else: preserve existing value unless task explicitly says to overwrite\n   ```\n\n3. **Handle Missing Keys:**\n   When performing a lookup, if the source key from the target row is not found in the source lookup dictionary, write `None` to the target cell. This results in a blank cell in the spreadsheet. Do not attempt to infer a value, use a default fallback, or leave the cell with an error-like state unless explicitly instructed otherwise."
}

Respond with ONLY the corrected JSON object.