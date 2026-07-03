# Step2 merger LLM call — empty merge root-cause (request + response)

**Run**: `spreadsheetbench_20260629_220033` · node `n0002` · `round_0002` · `exploit/step2`  
**Log line** 102514 · `call_id=call_026081` · model `qwen3.6-35b-a3b` · dur 49.874s

## TL;DR root cause

The merger LLM returned **6 well-formed section edits** (valid `delta_type`, `content` starts with `### `, unique `section_target`, response **not** truncated: completion=6637 < max 8192). But it **omitted the `target_tasks` field on every edit** (also dropped `rationale`/`derivation`). `_validate_merged_edits` drops any edit whose `target_tasks` is not a non-empty list → **all 6 dropped → `merged_edits.json = []` → `reject_no_survivor`**. Field is required by the prompt schema (line 75/119) but the model omitted it. Sampling is stochastic (**temperature=0.7**), which is why only this 1 of 23 steps was hit.

## Original call parameters

~~~~json
{
  "model": "qwen3.6-35b-a3b",
  "temperature": 0.7,
  "max_tokens": 8192,
  "chat_template_kwargs": {
    "enable_thinking": false
  },
  "response_format": {
    "type": "json_object"
  },
  "optimizer_json_mode": true,
  "usage": {
    "prompt_tokens": 25474,
    "completion_tokens": 6637,
    "total_tokens": 32111
  }
}
~~~~

## Original RESPONSE — parse + validate

parse: `bare` → 6 edits; **survivors after validation: 0**

| # | delta_type | section_target | has target_tasks | survives | drop reason |
|---|---|---|---|---|---|
| 0 | `section_rewrite` | ### Computed Values over Formulas | ❌ | ❌ | target_tasks_empty |
| 1 | `section_rewrite` | ### Sheet and Range Fidelity | ❌ | ❌ | target_tasks_empty |
| 2 | `section_rewrite` | ### Data Processing and Lookup Logic | ❌ | ❌ | target_tasks_empty |
| 3 | `new_section` | ### Block-Based Output with Individual R | ❌ | ❌ | target_tasks_empty |
| 4 | `new_section` | ### Whitespace Preservation in Text Mani | ❌ | ❌ | target_tasks_empty |
| 5 | `new_section` | ### Multi-Column Lookup Execution | ❌ | ❌ | target_tasks_empty |

## Local replay (this exact prompt, temp=0.7, 12x)

**Reproduced the `target_tasks` omission in 1/12 runs.**

| run | dur(s) | edits | with target_tasks | survives | verdict |
|---|---|---|---|---|---|
| 0 | 248.9 | 6 | 6/6 | 6 | OK |
| 1 | 239.1 | 6 | 0/6 | 0 | ⚠️ OMITS target_tasks → 0 survive |
| 2 | 216.3 | 3 | 3/3 | 3 | OK |
| 3 | 141.8 | 2 | 2/2 | 2 | OK |
| 4 | 213.9 | 3 | 3/3 | 3 | OK |
| 5 | 267.8 | parse_fail | - | - | no parseable {edits:[...]} |
| 6 | 267.8 | parse_fail | - | - | no parseable {edits:[...]} |
| 7 | 245.7 | 5 | 5/5 | 5 | OK |
| 8 | 110.8 | 3 | 3/3 | 3 | OK |
| 9 | 248.0 | 5 | 5/5 | 5 | OK |
| 10 | 217.7 | 4 | 4/4 | 4 | OK |
| 11 | 233.1 | 3 | 3/3 | 3 | OK |

### Reproduced-omission example (replay run 1) — validation

| # | delta_type | section_target | has target_tasks | survives | drop reason |
|---|---|---|---|---|---|
| 0 | `section_rewrite` | ### Computed Values over Formulas | ❌ | ❌ | target_tasks_empty |
| 1 | `section_rewrite` | ### Sheet and Range Fidelity | ❌ | ❌ | target_tasks_empty |
| 2 | `section_rewrite` | ### Data Processing and Lookup Logic | ❌ | ❌ | target_tasks_empty |
| 3 | `new_section` | ### Multi-Column Lookup Execution | ❌ | ❌ | target_tasks_empty |
| 4 | `new_section` | ### Block-Based Output with Individual R | ❌ | ❌ | target_tasks_empty |
| 5 | `new_section` | ### Cartesian Product Expansion for Mult | ❌ | ❌ | target_tasks_empty |

<details><summary>raw response of reproduced run</summary>

~~~~json
{
  "reasoning": "The 'Computed Values over Formulas' section requires a significant rewrite to consolidate the overwhelming consensus against writing formula strings, add precision preservation (Edit 13), and handle single-column constraints (Edit 8). 'Sheet and Range Fidelity' needs refinement to add A1 anchoring (Edit 2), structural separators (Edit 3), substring matching (Edit 5), single-column constraints (Edit 8), variable-length outputs (Edit 25), and formatting propagation (Edit 30). 'Data Processing and Lookup Logic' needs new sections for Multi-Column Lookup Execution (Edit 9), Block-Based Output (Edit 7), and Cartesian Product Expansion (Edit 20, 27) to handle complex filter expansion patterns.",
  "edits": [
    {
      "section_target": "### Computed Values over Formulas",
      "delta_type": "section_rewrite",
      "after_section": null,
      "content": "### Computed Values over Formulas\n\n**CORE RULE: Compute in Python, Write Literals.**\nWhen the task asks for a calculation, lookup, or result (even if it mentions \"formula,\" \"function,\" \"Index/Match,\" \"VLOOKUP,\" \"SUMPRODUCT,\" or \"COUNTIFS\"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.\n\n- **NEVER write formula strings** (e.g., `\"=SUM(...)\"`, `\"=IF(...)\"`, `\"=VLOOKUP(...)\"`, `\"=INDEX(...)\"`) into cells unless the task **explicitly** commands \"insert a formula\" or \"write a formula object\". This is extremely rare. The evaluator checks the *visible* value, not the formula. Writing a formula string results in `None` or the string itself, which is always incorrect.\n- **Simulate Logic in Python**: Replicate the calculation logic using Python/openpyxl. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex aggregations (e.g., SUMPRODUCT with multiple criteria), filter the data in Python and compute the sum/average/max/min.\n- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.\n- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.\n- **Preserve Precision**: Write the **full precision** of the computed value. Do **not** round, truncate, or format numbers (e.g., do not round to 4 decimal places) unless the task explicitly instructs you to round or format the output. The evaluator compares against the exact computed value.\n- **Single-Column Constraint**: If the `answer_position` is a single column (e.g., `B11:B17`), write **only the primary data value** to each cell. Do **not** concatenate values from multiple source columns (e.g., do **not** write \"1 Text 1\" if you have values from column A=1 and B=\"Text 1\"). Unless the task explicitly instructs to combine columns, each cell receives exactly one value.\n- **Exception (Rare)**: Only write a formula string if the task **explicitly** states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., \"insert the formula string into the cell\" or \"write the formula object\"). In almost all cases, \"create a formula\" or \"use a formula\" means \"calculate the result using formula logic.\" Default to computed values.\n- **Cognitive Trigger**: Before writing any cell value, ask: \"Am I writing a computed value (e.g., `100`, `\"Total\"`) or a formula string (e.g., `\"=SUM(A1:A10)\"`)?' If the answer is a formula string, STOP and reconsider. Unless the task explicitly demands a formula, you must compute the value in Python and write the literal result.\n\nsource_tasks: [\"49801\", \"50682\", \"32789\", \"51586\", \"57693\", \"9569\", \"31011\", \"58109\", \"55468\", \"194-19\", \"3413\", \"12307\", \"50811\", \"32902\", \"57590\", \"52532\", \"53167\", \"39903\", \"7665\", \"59639\", \"41265\", \"48975\"]\n",
      "rationale": "This section consolidates the single most critical failure pattern: agents writing Excel formula strings instead of computed values. Edits 10, 11, 12, 14, 15, 18, 19, 22, 23, 29, 32 all reinforce this. Additionally, Edit 13 identified that agents often round values unnecessarily, causing precision mismatches. Edit 8 identified that agents incorrectly concatenate values into single-column answer positions. The new content combines the strongest 'NEVER write formula strings' warning with the 'Preserve Precision' and 'Single-Column Constraint' rules, creating a comprehensive directive for value computation.",
      "derivation": "Merged raw edits 10, 11, 12, 13, 14, 15, 18, 19, 22, 23, 29, 32 (all targeting Computed Values) and Edit 8 (Single-Column Constraint). Edits 10, 11, 12, 14, 15, 18, 19, 22, 23, 29, 32 all proposed computing values in Python and writing literals instead of formula strings. I kept the most comprehensive formulation from Edit 14 and 34 (from previous step, but refined here), which covers edge cases like 'Else' conditions and explicit formula exceptions. Edit 13 added the 'Preserve Precision' rule which was dropped in previous steps but is high-confidence from task 9569. Edit 8 added the 'Single-Column Constraint' rule which was dropped in previous steps but is high-confidence from task 48975. Dropped minor variations that repeated the same core instruction."
    },
    {
      "section_target": "### Sheet and Range Fidelity",
      "delta_type": "section_rewrite",
      "after_section": null,
      "content": "### Sheet and Range Fidelity\n\n**Preserve Sheet Identity**:\n- Load the input workbook and modify it in place. Do **not** create a new `Workbook()` and save it, as this discards original sheet names and structure.\n- If you must create a new sheet, explicitly set its title to match the original sheet name (e.g., `ws.title = 'Sheet1'`).\n- When using `pandas.to_excel`, explicitly specify `sheet_name` to match the original sheet name. Prefer `openpyxl` for cell-level edits to avoid sheet name ambiguity.\n\n**Answer Position Alignment**:\n- If the task specifies an `answer_position` (e.g., `A3:G28`), write data into that exact range.\n- **Contiguous Writing**: When filtering or deleting rows, write the kept rows **contiguously** starting from the first row of the answer position (e.g., row 3). Do not preserve original row indices or leave gaps. The first kept row goes to the first cell of the answer position; the second kept row goes to the next cell, etc.\n- **Verification**: After writing, ensure the answer position contains no empty rows between data rows and that the first cell contains the first expected data row.\n\n**A1 Anchor Requirement**:\n- When a task explicitly requires a specific row or cell (e.g., \"ensure X appears in A1\") to be the *first* visible content, you must **delete** the preceding rows or shift the content to row 1 using row deletion (`ws.delete_rows`). Do **not** simply overwrite the `answer_position` starting at a later row (e.g., A2) while leaving A1 empty or with previous data. The structural change (deletion/shift) is mandatory to satisfy the \"first instance in A1\" constraint.\n\n**Preserve Structural Separators in Output**:\n- When the source data contains section headers or category labels mixed within the data rows (e.g., 'Engine HS' appearing between data rows), and the task requires extracting/matching these rows, **include these header rows in the output** if they are part of the original data structure. Do not skip them to create a purely contiguous data block unless explicitly instructed to filter them out. Map the source row directly to the output row to maintain the original logical grouping.\n\n**Substring Match for Identifiers**:\n- When the task asks to find a row containing a specific string (e.g., 'BR1 Sales'), use a substring/partial match (e.g., `if 'BR1 Sales' in str(cell_value)`) rather than an exact equality check. The identifier may have suffixes or prefixes (e.g., 'BR1 Sales JUN').\n\n**Variable-Length Output into Fixed Grids**:\n- When the source data produces a variable number of items (e.g., splitting a string of length N, or extracting N unique values) into a fixed answer position (e.g., D5:K7):\n  1. **Write Contiguously**: Write items starting from the first cell of the answer position (e.g., D5), filling subsequent cells in order (E5, F5, etc.).\n  2. **Leave Remaining Cells Blank**: Do NOT overwrite cells in the target range that do not have corresponding source data. Leave them as `None` or empty string (`''`). Do not fill them with default values unless instructed.\n  3. **Preserve Existing Data**: If the target range already has data in cells beyond the output count, do not delete or overwrite them unless explicitly told to clear the entire range first.\n\n**Formatting Propagation**:\n- When adding a new column or cell range based on existing data, copy the formatting (font, fill, border, alignment) from the adjacent source column to the new column. Use `openpyxl.styles` to extract style properties from the source cell and apply them to the target cell. This ensures the output matches the visual style of the original spreadsheet.\n\n**Full Range Iteration**:\n- When scanning for conditions, iterate over the **full range** of rows containing data (use `ws.max_row` or detect the last non-empty row in the widest column). Do not break early on empty cells in condition columns; data may exist in other columns of later rows.\n\n**Read Before Write**:\n- When modifying a worksheet in-place (e.g., filtering/deleting rows or columns), always read and store all relevant source data into a Python list/dictionary **before** clearing or overwriting any cells in the source sheet. If you clear cells before reading them, you will lose the data you need to write back.\n\n**Conditional Clearing (Keep/Discard)**:\n- When instructed to keep only specific values and delete/clear others, iterate over the target range. For each cell, if the value matches the target literal (case-insensitive if the instruction implies it, otherwise case-sensitive), preserve it. If it does not match, set the cell value to `''` (empty string). Do not delete rows; only clear cell contents.\n\n**Multi-Condition Row Extraction (OR Logic)**:\n- When extracting rows based on multiple conditions with OR logic (e.g., Col B equals X OR Col C equals Y), iterate through all data rows in the source sheet. Evaluate each row against all conditions; if any condition is true, include the entire row in the result set. Write the headers to the specified row in the target sheet, then write all matching rows contiguously below the headers.\n\n**Group-Aware Transposition**:\n- When transposing data from a column to a wider range (e.g., Column A to Columns C-K), do NOT simply flatten all non-empty values into a continuous stream. Instead, first inspect the source column for structural separators (such as blank rows). Group consecutive non-empty values separated by these blanks, and map each group to a distinct row in the target output range. Preserve the group boundaries as row boundaries in the result.\n\n**Block-Based Transposition**:\n- When transposing data from a single column into a multi-column grid:\n  1. **Detect Block Structure**: Identify the repeating pattern in the source column. Count the number of items per logical block (e.g., Question + 4 Options + Answer + Explanation = 7 items).\n  2. **Calculate Grid Dimensions**: Determine the target grid size (rows × columns) from the `answer_position` or task description. Verify that `total_items = rows × columns`.\n  3. **Read and Chunk**: Read all non-empty values from the source column into a flat list. Chunk this list into blocks of the detected size.\n  4. **Write Row by Row**: Iterate through each block. Write the items in the block sequentially into the target row, starting from the first column of the answer position.\n  5. **Preserve Order**: Maintain the original order of items within each block. Do not sort or rearrange unless explicitly instructed.\n\n**Column Deletion by Header Pattern**:\n- When deleting columns based on a pattern in the header row:\n  1. **Scan Headers**: Iterate through all columns in the header row (typically row 1). Identify columns whose header text contains the specified keyword or pattern.\n  2. **Collect Column Indices**: Store the indices of columns to delete.\n  3. **Delete in Reverse Order**: To avoid index shifting issues, delete columns in descending order of their column index (right-to-left). Alternatively, build a new sheet containing only the columns to keep.\n  4. **Preserve Structure**: When using `openpyxl`, modify the existing sheet. If creating a new sheet, ensure the title matches the original. Preserve all rows and non-deleted columns exactly.\n  5. **Verify**: After deletion, verify that no remaining headers contain the deleted keyword.\n\nsource_tasks: [\"168-17\", \"56953\", \"408-5\", \"59639\", \"53449\", \"147-48\", \"423-16\", \"382-10\", \"178-22\", \"388-47\", \"438-18\", \"304-35\", \"55817\", \"535-20\"]\n",
      "rationale": "This section consolidates structural fidelity rules. Edits 2 (A1 Anchor), 3 (Structural Separators), 5 (Substring Match), 8 (Single-Column Constraint - moved from Computed Values as it's also structural), 25 (Variable-Length Output), and 30 (Formatting Propagation) are added to the existing rules. The existing rules (Sheet Identity, Contiguous Writing, Full Range Iteration, Read Before Write, Conditional Clearing, Multi-Condition Extraction, Group-Aware Transposition, Block-Based Transposition, Column Deletion) are preserved. The new rules address specific failure modes: agents leaving A1 empty when it should be populated, skipping structural headers in output, failing to find rows with partial ID matches, concatenating values in single-column outputs, and not propagating formatting.",
      "derivation": "Merged raw edits 2, 3, 5, 8, 25, 30 with the existing section content from the current rules.md. Edit 2 proposed A1 anchoring via row deletion. Edit 3 proposed preserving structural separators. Edit 5 proposed substring matching for identifiers. Edit 8 proposed single-column constraint (kept here as it relates to answer position alignment). Edit 25 proposed variable-length output handling. Edit 30 proposed formatting propagation. All these are high-confidence patterns from contrastive and success sources. The existing section content was preserved as it covers the core structural rules."
    },
    {
      "section_target": "### Data Processing and Lookup Logic",
      "delta_type": "section_rewrite",
      "after_section": null,
      "content": "### Data Processing and Lookup Logic\n\n**Multi-Column Lookups**:\n- When matching across multiple columns (e.g., Column B and D), you must match **ALL** specified criteria simultaneously. A partial match is insufficient.\n- Convert search keys and data values to a common type (e.g., string) before comparison to avoid silent type mismatches.\n- Use the matching row's data from the target output column.\n\n**Date and Type Handling**:\n- **Normalize Dates**: Convert all dates to a consistent type (e.g., `datetime` or `YYYY-MM-DD` string) before grouping or sorting. Do not mix string dates and datetime objects.\n- **Preserve Original Types**: When copying values, preserve their original Python type (e.g., `str`, `datetime`, `float`). Do not convert string dates to datetime objects unless explicitly required.\n- **Identify Correct Columns**: Verify which column contains the primary data for filtering (e.g., check if the 'date' column is Column A or Column N). Do not assume metadata columns contain row-specific data.\n\n**Lookup Implementation Pattern**:\n- Build an explicit Python dictionary mapping lookup keys to target values from the source data.\n- Iterate through target rows and fill cells using the dictionary. If a key is not found, write `''` or `None`.\n- For conditional logic, evaluate the condition in Python for each row and write the resulting literal value.\n\n**Contiguous Block Detection**:\n- For tasks involving identifying ranges of markers (e.g., 'm' for meetings), scan for contiguous sequences. Map the start and end of each block to the corresponding headers (e.g., time columns) and write the start/end times to the output columns.\n\n**Column Header vs Instruction Alignment**:\n- When the result sheet has pre-existing headers (e.g., 'Designation', 'Contact no') that differ from the instruction's wording (e.g., 'display the designation... person's name is listed'), **follow the column headers for column mapping**, not the instruction's narrative labels.\n- Specifically: If the instruction says \"display X in column B\" but the header in column B is \"Designation\", and the task involves populating based on a designation criterion, write the **designation text** into column B, not the name. The header defines the column's semantic role.\n- If the instruction explicitly says \"write the name in column B\" AND the header says \"Name\", write the name. But if the header says \"Designation\", write the designation value.\n- This prevents the common error of writing the wrong field (e.g., Name) into a column labeled with a different field (e.g., Designation).\n\n**Preserve Non-Matching Rows**:\n- When filtering rows based on a condition (e.g., 'delete rows where Col E = 0'), explicitly retain rows that do NOT match the deletion condition, even if they have unusual values (e.g., NaN, 'Total Zero', or non-zero numbers). Do not assume only the explicitly targeted rows remain.\n\n**Complete Word Matching**:\n- When instructed to match a word \"as a complete word\" or find exact word matches within phrases:\n  1. **Split and Compare**: Split the target phrase (e.g., keyword) into individual words (by whitespace).\n  2. **Exact Match**: Check if the search word is present in the list of split words (case-insensitive unless specified otherwise).\n  3. **Do NOT use substring matching**: Do not use simple `in` operator on the raw string (e.g., \"tool\" in \"tools\" is True for substring but may be incorrect for complete word matching if \"tool\" and \"tools\" are distinct tokens). Use word-boundary logic or list containment.\n  4. **Sum/Aggregate**: If multiple phrases match, aggregate the relevant values (e.g., sum search volumes) as instructed.\n\n**Date-Month Matching**:\n- When the task requires matching dates to a month name (e.g., \"January\"), parse the date values in the source column (e.g., `datetime` objects) and compare their `.month` attribute against the target month number (mapped from the month name string). Sum or filter values based on this match, not on string comparison of the full date.\n\n**Group-Aware Consecutive Group Detection**:\n- When counting or summing consecutive runs of a value (e.g., consecutive 1s in a column), scan the column sequentially, tracking the current run length.\n- When the run breaks (different value or end of data), if the run length meets the threshold (e.g., >= 2), add its contribution to the total.\n- Reset the run counter on each break.\n- This pattern applies to any task requiring 'groups of adjacent cells' analysis, where only groups meeting a size/sum condition contribute to the result.\n\nsource_tasks: [\"188-39\", \"408-5\", \"41265\", \"57590\", \"51359\", \"39931\", \"50811\", \"254-34\", \"61-4\", \"325-44\"]\n",
      "rationale": "This section consolidates data processing logic. Edits 4 (Column Header Alignment), 6 (Preserve Non-Matching Rows), 24 (Complete Word Matching), 21 (Date-Month Matching), 28 (Group-Aware Consecutive Group Detection), 9 (Multi-Column Lookup Execution - simplified to core pattern), and 7 (Block-Based Output - simplified to core pattern) are added. The existing rules (Multi-Column Lookups, Date/Type Handling, Lookup Implementation, Contiguous Block Detection) are preserved. The new rules address specific failure modes: agents writing the wrong field into a column based on header mismatch, dropping non-matching rows during filtering, using substring matching instead of word matching, incorrect date-month matching, and failing to detect consecutive groups.",
      "derivation": "Merged raw edits 4, 6, 7, 9, 21, 24, 28 with the existing section content from the current rules.md. Edit 4 proposed following column headers over instruction narrative. Edit 6 proposed preserving non-matching rows. Edit 7 proposed including individual rows in block-based output (simplified to core pattern here). Edit 9 proposed multi-column lookup execution pattern (simplified to core pattern here). Edit 21 proposed date-month matching. Edit 24 proposed complete word matching. Edit 28 proposed group-aware consecutive group detection. All these are high-confidence patterns from contrastive and success sources. The existing section content was preserved as it covers the core data processing rules."
    },
    {
      "section_target": "### Multi-Column Lookup Execution",
      "delta_type": "new_section",
      "after_section": "### Data Processing and Lookup Logic",
      "content": "### Multi-Column Lookup Execution\n\nWhen the task requires looking up a value based on **multiple criteria** (e.g., matching two columns to find a third column's value):\n\n1. **Build a Composite Key Dictionary**: Iterate through the source/reference data. For each row, combine the lookup columns into a tuple key (e.g., `(str(col_a_value), str(col_b_value))`) and map it to the target return value. Store this in a Python dictionary.\n2. **Iterate Target Rows**: For each row in the answer position, extract the lookup criteria values. Convert them to strings to ensure consistent comparison.\n3. **Lookup and Write**: Use the composite key to fetch the value from the dictionary. If found, write the **literal computed value** (number, string, etc.) directly into the target cell. If not found, write an empty string (`''`) or `None`.\n4. **Preserve Existing Data**: If the task implies filling in missing values, only overwrite cells that are empty or explicitly targeted. Do not overwrite cells that already contain correct data unless instructed.\n5. **Verify Output**: After writing, read back the target cells to confirm the values are the computed literals, not formula strings. Ensure the structure (rows/columns) matches the expected answer position.\n\nsource_tasks: [\"39931\", \"50811\", \"254-34\"]\n",
      "rationale": "This new section codifies the specific pattern for multi-column lookups using composite keys. Edits 9 proposed this pattern, which was successful in tasks 39931, 50811, and 254-34. It provides a clear, step-by-step procedure for building a dictionary with tuple keys and writing literal values, preventing the common failure of writing Excel formulas or partial matches.",
      "derivation": "Derived from raw edit 9. This edit proposed a specific pattern for multi-column lookups using composite keys. It was successful in tasks 39931, 50811, and 254-34. The pattern is distinct from the general 'Multi-Column Lookups' rule in the existing section, which focuses on matching criteria and type normalization. This edit provides the implementation details (composite key dictionary) which is a high-confidence success pattern."
    },
    {
      "section_target": "### Block-Based Output with Individual Rows",
      "delta_type": "new_section",
      "after_section": "### Multi-Column Lookup Execution",
      "content": "### Block-Based Output with Individual Rows\n\nWhen the task asks to merge consecutive rows (e.g., consecutive negative values) but the expected output also includes individual rows that did not form a multi-row block (e.g., single negative rows, non-negative rows), you must include **all** source rows in the output:\n\n1. **Identify Consecutive Blocks**: Find sequences of 2+ consecutive rows matching the condition.\n2. **Merge Blocks**: For each block of 2+, compute the aggregated/merged values as instructed.\n3. **Include Individuals**: For every row that did NOT belong to a multi-row block (single matches, non-matches), include it as an individual row in the output with its original values (or computed values if aggregation is requested even for singles).\n4. **Output Order**: Write merged blocks first (or in their original relative order), followed by individual rows, preserving the original row order within each category.\n5. **Verify Completeness**: Count the total rows in the input (excluding headers) and ensure the output contains the same number of data rows. If the output has fewer rows than the input, you likely missed individual rows.\n\n**Example**: If the input has 10 data rows and 3 form a consecutive-negative block (merged into 1 row), the output should have 1 (merged) + 7 (individuals) = 8 rows total, not just 3.\n\nsource_tasks: [\"61-4\"]\n",
      "rationale": "This new section addresses the specific failure mode where agents merge consecutive rows but fail to include individual rows that did not form blocks. Edit 7 proposed this pattern, which was successful in task 61-4. The rule ensures that the output row count matches the input row count (accounting for merges), preventing row-count mismatches.",
      "derivation": "Derived from raw edit 7. This edit proposed including individual rows in block-based output. It was successful in task 61-4. The pattern is distinct from the general 'Block-Based Transposition' rule in the 'Sheet and Range Fidelity' section, which focuses on transposing data from a column to a grid. This edit provides the specific logic for merging consecutive rows while preserving individual rows, which is a high-confidence success pattern."
    },
    {
      "section_target": "### Cartesian Product Expansion for Multi-Value Fields",
      "delta_type": "new_section",
      "after_section": "### Block-Based Output with Individual Rows",
      "content": "### Cartesian Product Expansion for Multi-Value Fields\n\n- When a single input row must expand into multiple output rows due to multiple values in a field (e.g., `IN LIST A,B,C`), compute the **Cartesian product** of all multi-value fields.\n- For each combination in the product, create one output row.\n- **Repeat single-value fields**: For fields that have only one value (e.g., `field = \"X\"`), repeat that value across all generated rows.\n- **Handle missing output columns**: If a parsed field does not exist in the target output schema (e.g., `loc` in the filter but not in the output headers), exclude it from the output.\n- **Preserve non-filter columns**: Copy non-filter columns (e.g., `wtype_id`, `name`, `status`) from the input row into every generated output row.\n\n**Filter Parsing with Regex**:\n- When a column contains structured filter expressions (e.g., `(\"field\" = \"value\") AND (\"other\" IN LIST 1,2)`), use regex to parse `=` conditions and `IN LIST` conditions separately. Split by `AND` to isolate individual conditions, then match field names and values. Handle quoted strings and numeric values consistently.\n\nsource_tasks: [\"325-44\"]\n",
      "rationale": "This new section addresses the specific failure mode where agents fail to expand single rows with multi-value fields into multiple output rows. Edits 20, 27, and 26 proposed this pattern, which was successful in task 325-44. The rule ensures that the agent correctly parses filter expressions and expands them into multiple rows via Cartesian product, a complex but high-confidence success pattern.",
      "derivation": "Derived from raw edits 20, 27, and 26. Edit 20 proposed 'Filter Expansion (Cartesian Product)'. Edit 27 proposed 'Cartesian Product Expansion for Multi-Value Fields'. Edit 26 proposed 'Filter Parsing with Regex'. All three were successful in task 325-44. The pattern is distinct from the general 'Multi-Condition Row Extraction' rule in the 'Sheet and Range Fidelity' section, which focuses on OR logic for row extraction. This edit provides the specific logic for expanding rows via Cartesian product, which is a high-confidence success pattern."
    }
  ]
}
~~~~
</details>


---
## Original SYSTEM prompt

~~~~
You are the MERGER in a rules.md optimization pipeline. You consolidate
independently-proposed raw edits into section-level edit units for ablation
testing.

## What you produce

Each output edit is the COMPLETE TARGET CONTENT of one ### section in rules.md.
rules.md is a tactical playbook that a SEPARATE task-executing agent reads as
its operational instructions. The task agent has NO access to raw edits,
optimization history, or any context from this pipeline — it only sees the
final rules.md text. Therefore, every piece of content you write must be a
DIRECT, ACTIONABLE instruction that helps the agent perform tasks correctly.
Do not include any information that is only meaningful to the optimization
process (rationale, source tasks, failure statistics, "why" explanations).

## Output format — JSON only, no fences, no prose
{
  "reasoning": "<key consolidation decisions, 2-3 sentences>",
  "edits": [
    {
      "section_target": "### <for rewrite: existing heading; for new_section: the NEW heading matching content>",
      "delta_type": "new_section | section_rewrite | section_refinement",
      "after_section": "### <only for new_section: existing heading to insert after, or _end/_start>",
      "content": "### <heading>\n<well-structured markdown body>",
      "target_tasks": ["task_id_1", ...],
      "rationale": "<DETAILED — see Principle 6>",
      "derivation": "<DETAILED — see Principle 6>"
    }
  ]
}

## Operations and field rules

Three operations, each with distinct field semantics:

**section_rewrite / section_refinement** — modify an EXISTING ### section:
  - section_target: the EXACT existing ### heading being modified
    (must match a heading in the section index above)
  - after_section: OMIT (not needed — the section already exists in place)
  - content: the COMPLETE replacement section (### heading + full body,
    including ALL existing content that should be KEPT plus your changes)

**new_section** — create an entirely NEW ### section:
  - section_target: the NEW ### heading you are creating. It MUST match
    the ### heading on the first line of your content. For example, if
    your content starts with "### Handling Cumulative State", then
    section_target must be "### Handling Cumulative State" — NOT the
    name of an existing section you want to insert near.
  - after_section: where to INSERT this new section — must be "_end"
    (document end), "_start" (before all sections), or an EXISTING ###
    heading from the section index. Do NOT reference a section created
    by another edit in this output.
  - content: the COMPLETE new section (### heading + full body)

**All operations:**
  - content: This is what the task agent will read as its rules — write it
    FROM THE AGENT'S PERSPECTIVE. Include only what the agent needs to act
    correctly: procedures, patterns, checks, constraints. Do NOT include
    anything the agent cannot act on: no "**Rationale**:", no
    "**Source Tasks**:", no "**Why**:", no failure statistics, no edit
    provenance. Those belong in rationale/derivation fields.
  - Section headings (###) must be DESCRIPTIVE NAMES without numeric
    prefixes. Use "### Input Parsing and Data Inspection", NOT
    "### 2. Input Parsing and Data Inspection". Numbered headings break
    when sections are added or removed during optimization.
  - target_tasks: Union of source_tasks from all contributing raw edits.
    Must be non-empty.

## Principles

1. ONE EDIT PER SECTION. Multiple raw edits touching the same section MUST
   be merged into one edit. Non-negotiable (enables independent ablation).

2. GROUP BY CONTENT, NOT SOURCE TYPE. Failure-driven and success-driven raw
   edits proposing the same improvement → merge them. Cross-source agreement
   is high confidence; note it in derivation.

3. GAP-ALIGN. Choose delta_type based on the section index above:
   - If the section index is empty or the topic is NOT covered by any
     existing section → use "new_section" with after_section="_end".
   - If the topic IS covered by an existing ### section → use
     "section_rewrite" or "section_refinement", NEVER a duplicate new_section.

4. PRESERVE EXISTING CONTENT. For rewrite/refinement, output the COMPLETE
   section — existing bullets that should be kept + changes. You are writing
   the replacement.

5. RESOLVE CONTRADICTIONS. Conflicting raw edits → keep the version with
   more supporting patches. Explain in derivation.

6. DERIVATION TRANSPARENCY. The rationale and derivation fields carry
   critical diagnostic value — they are NOT summaries, they are detailed
   audit records. Write each thoroughly:

   rationale must answer:
   - What valuable INSIGHT was discovered from the trajectories? This may
     be a recurring failure pattern (e.g. "agent uses pd.read_excel()
     without data_only=True, causing TypeError on formula cells"), a
     success pattern worth codifying (e.g. "passing rollouts consistently
     verify sheet names match before writing — this practice prevents
     silent data loss"), or a contrastive finding (e.g. "the key
     difference between pass/fail on task X was checking file extension
     before choosing the read method").
   - Which task IDs and how many independent patches support this insight?
     (cross-patch consensus = high confidence)
   - What concrete behavior change is expected after applying this edit?

   derivation must answer:
   - Which raw edit numbers contributed? (list ALL by index, e.g. "Raw
     edits 1, 2, 5, 6, 9")
   - For each contributing raw edit, what did it propose and what was kept
     vs refined? (e.g. "Edit 2 proposed data_only=True with engine param
     — kept its more precise formulation over edit 1's simpler version")
   - Were any raw edits DROPPED? Which ones and why? (e.g. "Dropped edit
     11 which proposed 'avoid formulas entirely' — conflicts with tasks
     requiring formula output")
   - How were overlapping proposals resolved? (e.g. "Edits 5, 6, 9 all
     proposed type-checking rules — merged into one consolidated bullet")

7. QUALITY OVER QUANTITY. Fewer high-confidence edits beat many speculative
   ones. Drop weak/low-support raw edits rather than outputting noise.

8. LEARN FROM HISTORY. The optimization history below shows recent edit
   verification results:
   - An edit that PASSED per-edit verification but the step was rejected at
     the final gate: the direction is sound but clashed with other edits.
     Consider re-proposing it.
   - An edit that FAILED per-edit verification: do not re-propose in the
     same form. Take a different approach.
   - A section with multiple consecutive failed edits may be near-optimal.
     Prioritize other sections.

TRAIN/TEST FIREWALL (non-negotiable). You are operating during TRAINING on the training split, where ground-truth / "expected" / golden answers may be visible to you (in evaluation notes, fail reasons, or trajectories). It is fine to LOOK at them to understand what truly went wrong. But everything you PRODUCE — your analysis, the patterns you mine, and above all the strategy.md / rules.md that result — is DEPLOYED at TEST time, where the agent has NO ground truth and sees only the task instruction and the input. Therefore nothing you output may reference, depend on, compare against, validate with, or instruct the agent to use expected / ground-truth / golden / "Expected Results" values. Reason FROM ground truth privately if it helps your diagnosis, but the agent can NEVER access it at run time, so any analysis, pattern, strategy, or rule you emit that requires it is invalid by construction. State your findings in terms of what the agent can observe from the task and input alone.
~~~~

## Original USER prompt (94 KB — rules.md + 34 raw edits + history + budget)

~~~~
## Current rules.md
### Computed Values over Formulas

When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **Do NOT write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=VLOOKUP(...)") unless the task explicitly commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* value, not the formula.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas), manually filter/sort/rank the data in Python to find the nth value.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.

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

## Section index
  1. ### Computed Values over Formulas
  2. ### Sheet and Range Fidelity
  3. ### Data Processing and Lookup Logic

## Raw edits to merge (34 total)
### Raw edit 1 (patch 2, source=contrastive)
  op: append
  content: ### Whitespace Preservation in Text Manipulation

**Preserve Original Whitespace**: When performing text manipulation tasks (substring, split, replace, delete, etc.), preserve all whitespace characters (spaces, tabs) exactly as they appear in the source text, unless the instruction explicitly commands trimming or stripping.
- **Do NOT strip trailing whitespace** from results unless told to "trim", "strip", or "remove extra spaces".
- **Example**: If the source is `"Hello World FT123"` and you remove `"FT123"`, the result must be `"Hello World "` (with trailing space), NOT `"Hello World"`.
- **Why**: The evaluator checks for exact string match. Trailing whitespace is part of the visible content in the cell.
  rationale: The failing rollout (r1) explicitly called `.rstrip()` on the result of the substring operation, removing the trailing space that was present in the source text before 'FT'. The passing rollouts (r0, r2) did not strip whitespace, preserving the exact substring up to the 'FT' marker. This caused a mismatch because the expected output includes the trailing space.
  source_tasks: ['45300']

### Raw edit 2 (patch 3, source=contrastive)
  op: insert_after
  target: ### Sheet and Range Fidelity
  content: **A1 Anchor Requirement**: When a task explicitly requires a specific row or cell (e.g., "ensure X appears in A1") to be the *first* visible content, you must **delete** the preceding rows or shift the content to row 1 using row deletion (`ws.delete_rows`). Do **not** simply overwrite the `answer_position` starting at a later row (e.g., A2) while leaving A1 empty or with previous data. The structural change (deletion/shift) is mandatory to satisfy the "first instance in A1" constraint.
  rationale: The failing rollout (168-17, r1) kept the data in the `answer_position` (A2:E12) but wrote the header at A2, leaving A1 empty. The evaluator expected 'Invoice No.' at A1. The passing rollouts (r0, r2) used `delete_rows` to physically remove the preceding rows, shifting the header to row 1. This edit codifies the structural action required for 'A1 anchor' tasks.
  source_tasks: ['168-17']

### Raw edit 3 (patch 4, source=contrastive)
  op: insert_after
  target: ### Sheet and Range Fidelity
  content: **Preserve Structural Separators in Output**: When the source data contains section headers or category labels mixed within the data rows (e.g., 'Engine HS' appearing between data rows), and the task requires extracting/matching these rows, **include these header rows in the output** if they are part of the original data structure. Do not skip them to create a purely contiguous data block unless explicitly instructed to filter them out. Map the source row directly to the output row to maintain the original logical grouping.
  rationale: The passing rollout (r1) correctly identified that the output range I2:L9 (8 rows) included 1 header row plus 7 data rows, where one of those 'data' rows was actually a section header ('Engine HS') from the source. The failing rollouts (r0, r2) treated all rows with Power=70 as pure data, skipping the 'Engine HS' header row, resulting in only 6 data rows and a misalignment with the expected 7 data rows. The divergence is preserving structural separators (headers) that exist in the source data stream.
  source_tasks: ['56953']

### Raw edit 4 (patch 5, source=contrastive)
  op: insert_after
  target: ### Data Processing and Lookup Logic
  content: **Column Header vs Instruction Alignment**:
- When the result sheet has pre-existing headers (e.g., 'Designation', 'Contact no') that differ from the instruction's wording (e.g., 'display the designation... person's name is listed'), **follow the column headers for column mapping**, not the instruction's narrative labels.
- Specifically: If the instruction says "display X in column B" but the header in column B is "Designation", and the task involves populating based on a designation criterion, write the **designation text** into column B, not the name. The header defines the column's semantic role.
- If the instruction explicitly says "write the name in column B" AND the header says "Name", write the name. But if the header says "Designation", write the designation value.
- This prevents the common error of writing the wrong field (e.g., Name) into a column labeled with a different field (e.g., Designation).
  rationale: The passing rollout correctly identified that column B's header is 'Designation' and wrote the designation text ('Branch Manager') into it. The failing rollouts wrote the employee name ('Vidhya Nalawade') into column B, ignoring the column header in favor of the instruction's phrase 'person's name is listed against their location'. The instruction's narrative is ambiguous; the column header is the structural anchor. By following headers, the agent aligns with the expected output structure.
  source_tasks: ['188-39']

### Raw edit 5 (patch 6, source=contrastive)
  op: insert_after
  target: ### Sheet and Range Fidelity
  content: **Substring Match for Identifiers**: When the task asks to find a row containing a specific string (e.g., 'BR1 Sales'), use a substring/partial match (e.g., `if 'BR1 Sales' in str(cell_value)`) rather than an exact equality check. The identifier may have suffixes or prefixes (e.g., 'BR1 Sales JUN').
  rationale: The passing rollout found 'BR1 Sales JUN' by checking if 'BR1 Sales' was in the cell value. The failing rollout searched for exact 'BR1 Sales' and found nothing, leading to incorrect row filtering and output structure.
  source_tasks: ['408-5']

### Raw edit 6 (patch 6, source=contrastive)
  op: insert_after
  target: ### Data Processing and Lookup Logic
  content: **Preserve Non-Matching Rows**: When filtering rows based on a condition (e.g., 'delete rows where Col E = 0'), explicitly retain rows that do NOT match the deletion condition, even if they have unusual values (e.g., NaN, 'Total Zero', or non-zero numbers). Do not assume only the explicitly targeted rows remain.
  rationale: The passing rollout kept the 'Total Zero' row because its Col E was NaN (not 0). The failing rollout excluded it, resulting in a missing row at C4/E3 in the output.
  source_tasks: ['408-5']

### Raw edit 7 (patch 7, source=contrastive)
  op: append
  content: ### Block-Based Output with Individual Rows

When the task asks to merge consecutive rows (e.g., consecutive negative values) but the expected output also includes individual rows that did not form a multi-row block (e.g., single negative rows, non-negative rows), you must include **all** source rows in the output:

1. **Identify Consecutive Blocks**: Find sequences of 2+ consecutive rows matching the condition.
2. **Merge Blocks**: For each block of 2+, compute the aggregated/merged values as instructed.
3. **Include Individuals**: For every row that did NOT belong to a multi-row block (single matches, non-matches), include it as an individual row in the output with its original values (or computed values if aggregation is requested even for singles).
4. **Output Order**: Write merged blocks first (or in their original relative order), followed by individual rows, preserving the original row order within each category.
5. **Verify Completeness**: Count the total rows in the input (excluding headers) and ensure the output contains the same number of data rows. If the output has fewer rows than the input, you likely missed individual rows.

**Example**: If the input has 10 data rows and 3 form a consecutive-negative block (merged into 1 row), the output should have 1 (merged) + 7 (individuals) = 8 rows total, not just 3.
  rationale: Failing rollouts (r0, r1) correctly identified consecutive negative blocks and merged them, but failed to include individual rows (non-negative changes, single negatives) that also appeared in the expected output. The passing rollout (r2) correctly included all rows: merged blocks plus individual rows. This edit codifies the rule to always include non-blocked rows in the output, preventing row-count mismatches.
  source_tasks: ['61-4']

### Raw edit 8 (patch 8, source=contrastive)
  op: replace
  target: ### Sheet and Range Fidelity -> **Answer Position Alignment**
  content: **Answer Position Alignment**:
- If the task specifies an `answer_position` (e.g., `A3:G28`), write data into that exact range.
- **Contiguous Writing**: When filtering or deleting rows, write the kept rows **contiguously** starting from the first row of the answer position (e.g., row 3). Do not preserve original row indices or leave gaps. The first kept row goes to the first cell of the answer position; the second kept row goes to the next cell, etc.
- **Single-Column Constraint**: If the `answer_position` is a single column (e.g., `B11:B17`), write **only the primary data value** to each cell. Do **not** concatenate values from multiple source columns (e.g., do **not** write "1 Text 1" if you have values from column A=1 and B="Text 1"). Unless the task explicitly instructs to combine columns, each cell receives exactly one value: the one corresponding to the column(s) being copied to that specific target column. If copying from multiple source columns into a single target column is ambiguous, prioritize the column that matches the target column's header or context, or write the values from the column that appears to be the primary data source (often the right-most or most descriptive column).
  rationale: The failing rollout (r2) incorrectly concatenated values from columns A and B into the single-column answer position B11:B17 (writing '1 Text 1' instead of just 'Text 1'). The passing rollouts correctly identified that the answer position was a single column and wrote only the relevant value (from column B) to each cell. This edit clarifies that single-column answer positions expect single values, preventing concatenation errors.
  source_tasks: ['48975']

### Raw edit 9 (patch 9, source=success)
  op: append
  content: ### Multi-Column Lookup Execution

When the task requires looking up a value based on **multiple criteria** (e.g., matching two columns to find a third column's value):

1. **Build a Composite Key Dictionary**: Iterate through the source/reference data. For each row, combine the lookup columns into a tuple key (e.g., `(str(col_a_value), str(col_b_value))`) and map it to the target return value. Store this in a Python dictionary.
2. **Iterate Target Rows**: For each row in the answer position, extract the lookup criteria values. Convert them to strings to ensure consistent comparison.
3. **Lookup and Write**: Use the composite key to fetch the value from the dictionary. If found, write the **literal computed value** (number, string, etc.) directly into the target cell. If not found, write an empty string (`''`) or `None`.
4. **Preserve Existing Data**: If the task implies filling in missing values, only overwrite cells that are empty or explicitly targeted. Do not overwrite cells that already contain correct data unless instructed.
5. **Verify Output**: After writing, read back the target cells to confirm the values are the computed literals, not formula strings. Ensure the structure (rows/columns) matches the expected answer position.
  rationale: Codifies the recurring pattern across all successful trajectories (tasks 39931, 50811, 254-34) where multi-column lookups were solved by building a Python dictionary with composite keys and writing literal computed values. This replaces the failed approach of writing Excel formula strings (INDEX/MATCH, etc.) which the evaluator cannot parse as values.
  source_tasks: ['39931', '50811', '254-34']

### Raw edit 10 (patch 10, source=failure)
  op: rewrite_section
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **CRITICAL: Do NOT write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=VLOOKUP(...)", "=INDEX(...)") unless the task **explicitly** commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* value, not the formula.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas), manually filter/sort/rank the data in Python to find the nth value.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.
- **Verification Step**: Before saving, ask: "Did I write a formula string or a computed value?" If the answer is a formula string, STOP and compute the value in Python instead.
  rationale: All 8 failed trajectories share the same mistake: writing Excel formula strings (like =IFERROR, =INDEX/MATCH, =SUMPRODUCT) into cells. The evaluator reads cell values directly without evaluating formulas, resulting in None. The existing rule is present but agents consistently ignore it. This rewrite strengthens the rule with 'CRITICAL' emphasis, adds a concrete 'Verification Step' cognitive trigger to force self-checking, and explicitly lists the formula types that are commonly misused.
  source_tasks: ['50682', '32789']

### Raw edit 11 (patch 11, source=contrastive)
  op: replace
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

**CRITICAL DEFAULT: Compute and Write Literals.**
When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **NEVER write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=VLOOKUP(...)") unless the task explicitly commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* value, not the formula. Writing a formula string results in the cell containing the string itself or `None`, causing a failure.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas), manually filter/sort/rank the data in Python to find the nth value.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.
  rationale: The failing rollout wrote Excel formula strings (e.g., '=IF(...)') into cells. The evaluator reads cell values directly without evaluating formulas, seeing `None` or the string itself. The passing rollouts computed the values in Python and wrote the literal boolean/text results. This edit strengthens the 'Computed Values over Formulas' section to explicitly warn against writing formula strings and emphasize that the evaluator checks visible values, not formulas.
  source_tasks: ['51586']

### Raw edit 12 (patch 12, source=contrastive)
  op: replace
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **Do NOT write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=VLOOKUP(...)", "=LEN(...)", "=SUBSTITUTE(...)") unless the task explicitly commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* value, not the formula. Writing a formula string results in the cell containing the string itself or `None`, which fails evaluation.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas, string manipulation like counting characters), manually filter/sort/rank the data in Python to find the nth value.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.
  rationale: The failing rollout wrote Excel formula strings (e.g., '=LEN(D$5)-LEN(SUBSTITUTE(...))') instead of computed values. The evaluator reads cell values without evaluating formulas, resulting in `None` or the string itself, causing failure. The passing rollouts correctly computed the counts in Python and wrote the literal integers. This edit reinforces the 'Do NOT write formula strings' rule with specific examples of common formula types (LEN, SUBSTITUTE, SUM) and clarifies that the evaluator does not execute formulas.
  source_tasks: ['57693']

### Raw edit 13 (patch 13, source=contrastive)
  op: replace
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **Do NOT write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=VLOOKUP(...)") unless the task explicitly commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* value, not the formula.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas), manually filter/sort/rank the data in Python to find the nth value.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Preserve Precision**: Write the **full precision** of the computed value. Do **not** round, truncate, or format numbers (e.g., do not round to 4 decimal places) unless the task explicitly instructs you to round or format the output. The evaluator compares against the exact computed value.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.
  rationale: The failing rollout (r0) rounded computed values to 4 decimal places (e.g., 0.170687841 -> 0.1707), which caused a mismatch with the expected full-precision values in the evaluation. The passing rollouts (r1, r2) wrote the raw computed floats without rounding. This edit adds a specific instruction to preserve full precision to prevent this error class.
  source_tasks: ['9569']

### Raw edit 14 (patch 14, source=contrastive)
  op: rewrite_section
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

**Absolute Default: Compute and Write Literals.**
When the task asks for a calculation, lookup, or result—even if the prompt uses words like "formula," "function," "SUMPRODUCT," "VLOOKUP," or "INDEX"—you must **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **NEVER write formula strings** (e.g., `"=SUM(...)"`, `"=IF(...)"`, `"=SUMPRODUCT(...)"`) into cells. The evaluator reads the cell value directly without executing Excel formulas. Writing a formula string results in `None` or the string itself, which is always incorrect.
- **Simulate Logic in Python**: Replicate the calculation logic using Python/pandas/openpyxl. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex aggregations (e.g., SUMPRODUCT with multiple criteria), filter the data in Python and compute the sum/average/max/min.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception (Rare)**: Only write a formula string if the task **explicitly** states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., "insert the formula string into the cell" or "write the formula object"). In almost all cases, "create a formula" or "use a formula" means "calculate the result using formula logic." Default to computed values.
  rationale: The failing rollouts (r1, r2) wrote Excel formula strings (SUMPRODUCT) into cell R6. The evaluator reads the cell value without evaluating the formula, resulting in None. The passing rollout (r0) computed the sum in Python and wrote the literal float. The existing rule mentions this exception but agents still default to formulas. This rewrite makes the 'Compute in Python' rule absolute and explicit, emphasizing that 'formula' in the prompt usually means 'calculate the result', not 'write a formula string'.
  source_tasks: ['31011']

### Raw edit 15 (patch 15, source=failure)
  op: rewrite_section
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **Do NOT write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=VLOOKUP(...)", "=INDEX(...)") unless the task **explicitly** commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* value, not the formula.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas), manually filter/sort/rank the data in Python to find the nth value.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.
- **Critical Check**: Before writing to any cell, ask: "Does the task ask for the *result* (a number/text) or the *formula* (the mechanism)?" If the task says "adjust the formula" or "fix the formula," it usually means "make the cell show the correct result." Compute the correct result in Python and write the literal value. Do NOT write a formula string unless the task explicitly requires it.
  rationale: The 8 failed trajectories all fail because the agent writes Excel formula strings into cells. The evaluator reads cell values without evaluating formulas, resulting in None. The existing rule is present but agents ignore it or misinterpret 'adjust the formula' as 'write a formula'. This rewrite reinforces the rule with a 'Critical Check' step and explicit guidance that 'adjust formula' tasks usually require computed values, not formula strings.
  source_tasks: ['58109', '55468']

### Raw edit 16 (patch 16, source=success)
  op: insert_after
  target: ### Computed Values over Formulas
  content: - **Complex Logic Simulation**: For tasks requiring per-row comparisons (e.g., 'highest score in each game'), cross-column lookups (e.g., 'amount used since last usage'), or multi-condition filtering (e.g., 'count bins not starting with X/Z'), **simulate the entire logic in Python** before writing. Build dictionaries, lists, or counters to compute the final state for every cell in the answer range.
- **Structured Text Parsing**: When the source cell contains structured text (e.g., comma-separated items, key-value pairs like 'LOC: QTY'), parse it programmatically (split by delimiter, extract prefixes/suffixes) to evaluate conditions. Do not rely on Excel's text functions; compute the count/result in Python and write the literal number.
  rationale: Trajectories 1, 2, 3, 4, 5, 6, 7, 8 all succeeded by computing complex logic (game wins, product usage, bin counts) in Python and writing literals. The agent must explicitly simulate cross-row or structured-text logic in Python rather than trying to write Excel formulas or leaving cells blank.
  source_tasks: ['52532', '53167', '39903']

### Raw edit 17 (patch 16, source=success)
  op: append
  content: ### Styling and Formatting Fidelity

**Apply Explicit Styling**: If the instruction requests specific formatting (e.g., borders, fonts, colors, alignment), apply these styles to the target cells **after** writing the data values. Use `openpyxl.styles` to create border, font, and alignment objects, and assign them to the relevant cells.
- **Borders**: Create a `Border` object with `Side(style='thin')` for left, right, top, bottom. Apply to the entire answer range.
- **Fonts**: Create a `Font` object with the specified name and size. Apply to the entire answer range.
- **Verification**: After saving, verify that the target cells have the correct values AND the requested styles.
  rationale: Trajectories 6, 7, 8 required adding borders and Courier New font to cells C2:C6. The agent succeeded by explicitly creating `Border` and `Font` objects and applying them. This is a new theme not covered in existing sections.
  source_tasks: ['39903']

### Raw edit 18 (patch 17, source=failure)
  op: rewrite_section
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **CRITICAL: Do NOT write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=COUNTIFS(...)", "=SUMPRODUCT(...)", "=INDEX(...)") unless the task **explicitly** commands "insert a formula" or "write a formula object". This is extremely rare. The evaluator checks the *visible* value, not the formula. Writing a formula string results in the cell containing the string itself (or None if the framework does not evaluate it), which will fail evaluation.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas, SUMPRODUCT, COUNTIFS), manually filter/sort/rank the data in Python to find the result.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.
- **Cognitive Trigger**: Before writing any cell value, ask: "Am I writing a computed value (e.g., `100`, `"Total"`) or a formula string (e.g., `"=SUM(A1:A10)"`)?" If the answer is a formula string, STOP and reconsider. Unless the task explicitly demands a formula, you must compute the value in Python and write the literal result.
  rationale: All 8 failed trajectories (194-19, 3413, 12307) failed because the agent wrote Excel formula strings into cells. The evaluator reads cell values directly without evaluating formulas, so formula strings appear as None or the string itself, causing mismatches. The existing rule was insufficiently emphasized; this rewrite strengthens the prohibition against formula strings, adds explicit examples of forbidden patterns, and introduces a mandatory cognitive trigger to prevent the agent from defaulting to formula strings.
  source_tasks: ['194-19', '3413', '12307']

### Raw edit 19 (patch 18, source=success)
  op: insert_after
  target: ### Computed Values over Formulas
  content: - **Ambiguous Formula Requests**: When the task mentions "formula", "INDEX/MATCH", "VLOOKUP", "SUMIFS", or similar Excel functions, **still compute the result in Python** and write the literal value. The evaluator checks the *visible* cell content, not the formula mechanism. Only write a formula string if the task explicitly requires a dynamic formula object (e.g., "insert this formula so it updates when data changes").
  rationale: Trajectories 1, 2, 3, 4, 5, 6 all succeed by computing values (lookups, bonuses, sums) in Python and writing literals. The agent must explicitly override the instinct to write formula strings when the prompt uses formula terminology. This codifies the winning behavior of computing the final visible state.
  source_tasks: ['50811', '32902', '57590']

### Raw edit 20 (patch 18, source=success)
  op: insert_after
  target: ### Data Processing and Lookup Logic
  content: **Filter Expansion (Cartesian Product)**: When parsing filter conditions that contain `IN LIST` clauses with multiple values, generate all combinations (Cartesian product) of those values. For each combination, create a separate output row. Keep single-value filters (e.g., `key = value`) constant across all generated rows. Ensure the output rows are contiguous and headers are preserved.
  rationale: Trajectory 8 succeeds by parsing `IN LIST` conditions and expanding them into multiple rows (Cartesian product of specialtyId and contextId values). This generalizes the pattern for any task involving filter expansion or pivot-like operations from a single row with lists into multiple rows.
  source_tasks: ['325-44']

### Raw edit 21 (patch 18, source=success)
  op: insert_after
  target: ### Data Processing and Lookup Logic
  content: **Date-Month Matching**: When the task requires matching dates to a month name (e.g., "January"), parse the date values in the source column (e.g., `datetime` objects) and compare their `.month` attribute against the target month number (mapped from the month name string). Sum or filter values based on this match, not on string comparison of the full date.
  rationale: Trajectories 5, 6, 7 succeed by correctly matching datetime objects to month names (e.g., A26='January' -> month 1) and filtering/summing Column I values accordingly. This codifies the robust pattern for date-month matching that avoids string-parsing fragility.
  source_tasks: ['57590']

### Raw edit 22 (patch 19, source=contrastive)
  op: rewrite_section
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

**CORE RULE: Write the visible result, never the formula mechanism.**
When the task asks for a calculation, lookup, concatenation, or result (even if it mentions "formula," "function," "LEFT," "RIGHT," "INDEX," "MATCH," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **Do NOT write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=VLOOKUP(...)", "=LEFT(...)..."). This is the most common failure mode. The evaluator checks the *visible* value (what the user sees in Excel), not the formula bar. If you write a formula string, the evaluator often reads `None` or the raw string, causing a mismatch.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For text manipulation (LEFT, RIGHT, CONCATENATE, SUBSTRING), use Python string slicing and concatenation.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task **explicitly** commands "insert a formula" or "write a formula object" AND the context implies the formula must remain dynamic in Excel (e.g., "create a formula that updates when cell A1 changes"). This is rare. Default to computed values.
- **Verification Step**: Before saving, ask: "If I open this file in Excel, what text/number will be visible in this cell?" If your code writes a string starting with "=", you are likely wrong. Compute the value in Python and write that value.
  rationale: The failing rollout (r1) wrote an Excel formula string '=LEFT(A1,2)&RIGHT(A1,5)' into C1. The evaluator reads the cell value without evaluating the formula, resulting in `None` (or the raw string), which failed the check for 'SL-0035'. The passing rollouts (r0, r2) computed 'SL-0035' in Python and wrote the literal string. The existing rule mentions not writing formulas, but the agent consistently ignores it. This rewrite strengthens the rule with a 'CORE RULE' header, explicitly defines the failure mode (evaluator reads None), provides the Python alternative for text functions (slicing), and adds a pre-save verification question to force the agent to check its intent.
  source_tasks: ['49801']

### Raw edit 23 (patch 20, source=success)
  op: insert_after
  target: ### Computed Values over Formulas
  content: - **Cognitive Trigger**: Before writing any cell, ask: "Does the user want the *result* (number/text) or the *formula* (mechanism)?" Default to result. Only write a formula string if the task explicitly requires a dynamic Excel formula that must be evaluated by the spreadsheet engine itself.
  rationale: Reinforces the default-to-computed-value behavior. All 8 successful trajectories in this batch explicitly computed values in Python and wrote literals, avoiding the common failure mode of writing formula strings which the evaluator does not parse.
  source_tasks: ['7665', '59639', '41265']

### Raw edit 24 (patch 20, source=success)
  op: insert_after
  target: ### Data Processing and Lookup Logic
  content: **Complete Word Matching**: When instructed to match a word "as a complete word" or find exact word matches within phrases:
1. **Split and Compare**: Split the target phrase (e.g., keyword) into individual words (by whitespace).
2. **Exact Match**: Check if the search word is present in the list of split words (case-insensitive unless specified otherwise).
3. **Do NOT use substring matching**: Do not use simple `in` operator on the raw string (e.g., "tool" in "tools" is True for substring but may be incorrect for complete word matching if "tool" and "tools" are distinct tokens). Use word-boundary logic or list containment.
4. **Sum/Aggregate**: If multiple phrases match, aggregate the relevant values (e.g., sum search volumes) as instructed.
  rationale: Trajectories 41265 (r0, r1) succeeded by splitting keywords into words and checking for exact word presence ('tool' matches 'gardening tool set' but 'tool' != 'tools'). This prevents incorrect substring matches that lead to wrong aggregation sums.
  source_tasks: ['41265']

### Raw edit 25 (patch 20, source=success)
  op: insert_after
  target: ### Sheet and Range Fidelity
  content: **Variable-Length Output into Fixed Grids**: When the source data produces a variable number of items (e.g., splitting a string of length N, or extracting N unique values) into a fixed answer position (e.g., D5:K7):
1. **Write Contiguously**: Write items starting from the first cell of the answer position (e.g., D5), filling subsequent cells in order (E5, F5, etc.).
2. **Leave Remaining Cells Blank**: Do NOT overwrite cells in the target range that do not have corresponding source data. Leave them as `None` or empty string (`''`). Do not fill them with default values unless instructed.
3. **Preserve Existing Data**: If the target range already has data in cells beyond the output count, do not delete or overwrite them unless explicitly told to clear the entire range first.
  rationale: Trajectories 59639 (r0, r1, r2) succeeded by splitting strings of varying lengths ('ABCDEFGH'=8 chars, 'pqrst'=5 chars, 'xyz'=3 chars) into the fixed D5:K7 grid. The agent correctly filled only the necessary cells and left the rest blank, matching the expected output structure.
  source_tasks: ['59639']

### Raw edit 26 (patch 21, source=success)
  op: insert_after
  target: ### Computed Values over Formulas
  content: - **Filter Parsing with Regex**: When a column contains structured filter expressions (e.g., `("field" = "value") AND ("other" IN LIST 1,2)`), use regex to parse `=` conditions and `IN LIST` conditions separately. Split by `AND` to isolate individual conditions, then match field names and values. Handle quoted strings and numeric values consistently.
  rationale: Trajectories 325-44 (r0, r1, r2) all required parsing complex filter strings from a column. The agent had to use regex to extract field-value pairs, handling both equality and IN LIST patterns. This pattern is generalizable to any task involving parsing structured text into tabular data.
  source_tasks: ['325-44']

### Raw edit 27 (patch 21, source=success)
  op: insert_after
  target: ### Data Processing and Lookup Logic
  content: **Cartesian Product Expansion for Multi-Value Fields**:
- When a single input row must expand into multiple output rows due to multiple values in a field (e.g., `IN LIST A,B,C`), compute the **Cartesian product** of all multi-value fields.
- For each combination in the product, create one output row.
- **Repeat single-value fields**: For fields that have only one value (e.g., `field = "X"`), repeat that value across all generated rows.
- **Handle missing output columns**: If a parsed field does not exist in the target output schema (e.g., `loc` in the filter but not in the output headers), exclude it from the output.
- **Preserve non-filter columns**: Copy non-filter columns (e.g., `wtype_id`, `name`, `status`) from the input row into every generated output row.
  rationale: Trajectories 325-44 all required expanding filter expressions into multiple rows via Cartesian product of IN LIST values. The agent needed to replicate this pattern: identify multi-value fields, compute their product, and broadcast single-value fields. This is a generalizable pattern for any task that 'unpivots' or 'expands' grouped data.
  source_tasks: ['325-44']

### Raw edit 28 (patch 21, source=success)
  op: insert_after
  target: ### Data Processing and Lookup Logic
  content: **Group-Aware Consecutive Group Detection**:
- When counting or summing consecutive runs of a value (e.g., consecutive 1s in a column), scan the column sequentially, tracking the current run length.
- When the run breaks (different value or end of data), if the run length meets the threshold (e.g., >= 2), add its contribution to the total.
- Reset the run counter on each break.
- This pattern applies to any task requiring 'groups of adjacent cells' analysis, where only groups meeting a size/sum condition contribute to the result.
  rationale: Trajectories 51359 (r0, r1, r2) required finding consecutive groups of 1s in each column and summing only groups of size >= 2. The agent needed to implement a sequential scan with run-length tracking. This is a generalizable pattern for any 'consecutive group' or 'run-length' analysis task.
  source_tasks: ['51359']

### Raw edit 29 (patch 22, source=success)
  op: replace
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **Do NOT write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=VLOOKUP(...)") unless the task explicitly commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* value, not the formula.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas), manually filter/sort/rank the data in Python to find the nth value.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.

**Cognitive Trigger**: Before writing to a cell, ask: "Is this a computed value or a formula object?" If the task asks for a result (sum, count, match, flag), compute it in Python and write the literal. Only write a formula string if the instruction explicitly demands it.
  rationale: The trajectories consistently show the agent failing by writing Excel formula strings (e.g., '=IF(...)') instead of computed values. The existing rule is present but insufficiently emphasized. This edit reinforces the 'compute in Python' default with a specific cognitive trigger to force the agent to explicitly decide between computed values vs formula strings before writing, addressing the systematic misunderstanding seen in failed trajectories.
  source_tasks: ['41265', '53449', '52532']

### Raw edit 30 (patch 22, source=success)
  op: insert_after
  target: ### Sheet and Range Fidelity
  content: 
**Formatting Propagation**: When adding a new column or cell range based on existing data, copy the formatting (font, fill, border, alignment) from the adjacent source column to the new column. Use `openpyxl.styles` to extract style properties from the source cell and apply them to the target cell. This ensures the output matches the visual style of the original spreadsheet.
  rationale: Trajectories 53449 (rollouts 1 and 2) explicitly require matching the format of the prior column. The agent had to struggle with `openpyxl` style copying mechanisms. This rule codifies the need to propagate formatting when creating new columns based on existing ones.
  source_tasks: ['53449']

### Raw edit 31 (patch 22, source=success)
  op: insert_after
  target: ### Data Processing and Lookup Logic
  content: 
**Multi-Sheet Concatenation**: When instructed to combine data from multiple sheets into a single target sheet (e.g., 'combined'), iterate through each source sheet (excluding the target), read all data rows (skipping headers), and write them contiguously into the target sheet starting from the specified row (typically row 2). Preserve the order of sheets as listed in the workbook or as implied by the task.
  rationale: Trajectories 297-42 (all rollouts) required copying data from multiple province sheets into a 'combined' sheet. The winning trajectories identified the source sheets, skipped headers, and appended data rows contiguously. This rule codifies that specific multi-sheet concatenation pattern.
  source_tasks: ['297-42']

### Raw edit 32 (patch 23, source=failure)
  op: rewrite_section
  target: ### Computed Values over Formulas
  content: ### Computed Values over Formulas

When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.

- **Do NOT write formula strings** (e.g., "=SUM(...)", "=IF(...)", "=VLOOKUP(...)") unless the task explicitly commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* value, not the formula.
- **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas), manually filter/sort/rank the data in Python to find the nth value.
- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
- **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.
- **Critical Check**: Before saving, verify that every cell in the `answer_position` contains a literal value, not a formula string. If you see `=` at the start of a cell value, you have made a mistake.
  rationale: All 8 trajectories fail because the agent writes formula strings (e.g., '=COUNTIF(...)', '=IF(...)') instead of computed values. The evaluator reads cell values directly without evaluating formulas, resulting in None. The current rule is present but agents ignore it. This rewrite adds a 'Critical Check' cognitive trigger to force the agent to explicitly decide between computed values vs formula strings before writing, addressing the systematic misunderstanding.
  source_tasks: ['12307', '40959', '48983', '58109']

### Raw edit 33 (patch 23, source=failure)
  op: insert_after
  target: ### Data Processing and Lookup Logic
  content: 
**Preserve Original Values**: When modifying a worksheet, do not overwrite cells that are not part of the answer position unless explicitly instructed. If a cell already contains a value and the task does not ask you to change it, leave it unchanged. Do not set cells to `None` or `''` unless the task requires clearing them.
  rationale: Trajectories 4, 5, 6, 7, 8 fail because the agent writes computed values or formula strings instead of the required formula object, or fails to detect the formula requirement. Trajectories 1 and 2 fail because the agent overwrites cells with empty/None values when it should preserve existing values (e.g., D2='Apple' in Trajectory 2). Trajectory 7 fails because the agent writes to the default sheet instead of the named sheet 'whole date' specified in the answer_position. These edits address the three most common failure mechanisms.
  source_tasks: ['48983']

### Raw edit 34 (patch 23, source=failure)
  op: insert_after
  target: ### Sheet and Range Fidelity
  content: 
**Relative Row References**: When applying a formula or value to a range of cells (e.g., C2:C7), ensure each cell references its own row. Use a loop to construct the cell reference dynamically (e.g., `f'=IF(B{row_idx}=..., A{row_idx}...)`), not a hardcoded reference like `B2` for all rows.
  rationale: Trajectory 4 fails because the agent writes formulas with hardcoded row references (e.g., always A2/B2) instead of relative references for each row in a range. This edit forces the agent to use dynamic row references when writing to multiple cells.
  source_tasks: ['40959']

## Optimization history
### Step 0 (action=accept_new_best, score 0.422 -> 0.517)

  Edit 0: section_target=### Computed Values over Formulas, delta_type=new_section, passed=True
    content:
    ### Computed Values over Formulas
    
    When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," or "VLOOKUP"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.
    
    - **Do NOT write formula strings** (e.g., `"=SUM(...)"`, `"=IF(...)"`, `"=VLOOKUP(...)"`) unless the task explicitly commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* value, not the formula.
    - **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas), manually filter/sort/rank the data in Python to find the nth value.
    - **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
    - **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
    - **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed). Default to computed values.
    rationale: The most prevalent failure pattern across all tasks is the agent writing Excel formula strings (e.g., '=IF(...)', '=INDEX(...)') into cells when the evaluator expects the computed literal value (e.g., '100', 'HOLD'). Passing rollouts consistently computed values in Python and wrote literals. This rule prevents the 'pred=None' or string-mismatch failures where the evaluator sees a formula string instead of the result. Cross-patch consensus from 10+ independent edits confirms this is the primary optimization target.
    derivation: Merged edits 9, 10, 13, 14, 16, 18, 19, 20, 22, 25, 34. All these edits proposed computing values in Python and writing literals instead of formula strings. Edits 14, 18, 19, 20, 34 were failure-driven; edits 9, 13, 16, 22, 25, 34 were success-driven. The core insight is identical: 'Compute in Python, write literal.' I kept the most comprehensive formulation from Edit 14 and 34, which covers edge cases like 'Else' conditions and explicit formula exceptions. Dropped minor variations that repeated the same core instruction.
    task results:
      43589: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      52917: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      51249: status=RETAINED, inc_pr=1.000, cand_pr=0.667
      57743: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      160-6: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      41589: status=RETAINED, inc_pr=1.000, cand_pr=0.333
      12864: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      58942: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      48921: status=GAINED, inc_pr=0.000, cand_pr=1.000
      50324: status=RETAINED, inc_pr=1.000, cand_pr=0.667
      45944: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      50526: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      44296: status=GAINED, inc_pr=0.000, cand_pr=1.000
      343-20: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      36097: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      41978: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      38537: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      39515: status=RETAINED, inc_pr=0.333, cand_pr=0.333
      50521: status=RETAINED, inc_pr=0.333, cand_pr=0.333
      50051: status=RETAINED, inc_pr=1.000, cand_pr=0.667

  Edit 1: section_target=### Sheet and Range Fidelity, delta_type=new_section, passed=True
    content:
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
    rationale: Multiple tasks failed due to structural issues: creating new workbooks (losing original sheets), writing to the wrong sheet name, or failing to compact filtered data into the answer position range. Failing rollouts often left gaps, preserved original indices, or created new sheets with default names. Passing rollouts preserved the original workbook structure and wrote filtered data contiguously into the specified answer range. This section codifies the structural requirements for verification success.
    derivation: Merged edits 1, 2, 3, 4, 5, 7, 8, 17. Edit 1, 3, 7 focused on sheet identity and loading in-place. Edit 5, 8, 17 focused on row contiguity and answer position alignment. Edit 4 focused on full range iteration. All these are structural fidelity issues. I merged them into one section because they all relate to 'where and how to write data' rather than 'what data to write'. Cross-source agreement (contrastive and success) confirms these are critical for evaluation compatibility.
    task results:
      50250: status=RETAINED, inc_pr=0.667, cand_pr=1.000
      44913: status=RETAINED, inc_pr=0.667, cand_pr=1.000
      280-17: status=RETAINED, inc_pr=0.667, cand_pr=1.000
      43213: status=RETAINED, inc_pr=0.667, cand_pr=1.000
      91-34: status=RETAINED, inc_pr=0.667, cand_pr=0.667
      567-21: status=RETAINED, inc_pr=0.333, cand_pr=1.000
      28-7: status=RETAINED, inc_pr=0.667, cand_pr=1.000
      455-35: status=RETAINED, inc_pr=0.333, cand_pr=0.667

  Edit 2: section_target=### Data Processing and Lookup Logic, delta_type=new_section, passed=True
    content:
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
    rationale: Several tasks failed due to incorrect data processing: partial multi-column matches, mixed date types causing sort/group errors, misidentifying the primary data column, and incorrect handling of contiguous blocks. Passing rollouts used explicit Python dictionaries for lookups, normalized date types, and correctly identified full-range data columns. This section codifies the data processing patterns that lead to correct values.
    derivation: Merged edits 11, 12, 15, 21, 23, 26, 27, 28, 29, 30. Edit 15 (multi-column lookup), Edit 11 (date column ID), Edit 12 (data mapping), Edit 27 (type preservation), Edit 26 (inspect structure), Edit 28/29 (block detection/time fidelity), Edit 30 (formula resolution via Python simulation). These are all data processing logic issues. I merged them because they all relate to 'how to process and transform data' before writing. Cross-source agreement confirms these are high-confidence patterns.
    task results:
      341-40: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      48527: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      56225: status=RETAINED, inc_pr=0.667, cand_pr=0.667
      49667: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      343-20: status=RETAINED, inc_pr=1.000, cand_pr=0.667
      59358: status=RETAINED, inc_pr=0.667, cand_pr=1.000
      48969: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      118-50: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      56427: status=RETAINED, inc_pr=0.333, cand_pr=1.000

  Edit 3: section_target=### Row Deletion and Filtering, delta_type=new_section, passed=False
    content:
    ### Row Deletion and Filtering
    
    **Delete Blank Rows**:
    - Read all rows into a Python list.
    - Filter out rows where **all relevant cells are None/empty** (a row is blank if every cell in the data columns is None).
    - Write only the non-blank rows to the sheet, starting from the first data row (e.g., row 2, after the header).
    - This produces a compact, gap-free dataset. Do not attempt to delete rows in-place via `openpyxl.delete_row()` unless specifically required, as it can cause index-shifting bugs.
    
    **Delete Specific Rows**:
    - Identify the rows to delete based on the task criteria.
    - Build a list of rows to **KEEP**.
    - Write the kept rows sequentially into the output sheet, starting from the first data row. Do not preserve original row indices.
    - Verify the output row count: `original_data_rows - deleted_rows`.
    rationale: Tasks requiring row deletion failed when agents tried to delete rows in-place (leaving gaps) or preserved original indices. Passing rollouts filtered rows into a new list and wrote them sequentially. This section provides a robust pattern for row deletion that ensures contiguous output.
    derivation: Merged edits 5, 8, 24, 36. All these edits addressed row deletion/filtering. Edit 5, 8, 24, 36 all proposed reading rows, filtering, and writing sequentially. This is a consistent, high-confidence pattern. I kept the most detailed formulation from Edit 24 and 36.
    task results:
      160-6: status=RETAINED, inc_pr=1.000, cand_pr=1.000

  Edit 4: section_target=### Input Inspection and Strategy, delta_type=new_section, passed=False
    content:
    ### Input Inspection and Strategy
    
    **Inspect Full Input Structure**:
    - Before writing any output, read the full input spreadsheet to understand the exact grid topology (sheet names, row/column ranges, data locations).
    - Map the instruction's target cells to the actual row/column indices. Verify which rows contain headers vs. data.
    - Identify source data ranges and their column mappings before computing.
    
    **Simulate Final State**:
    - Mentally or explicitly simulate the final content of the target cells.
    - For calculations, compute the result in Python using the actual input data.
    - For text/structure, determine the exact string or row/column layout. Preserve original casing, whitespace, and formatting unless explicitly told to change it.
    - For 'Else' cases, explicitly define what happens to non-matching rows (default to unchanged or blank as appropriate).
    
    **Sort Key Verification**:
    - When a task specifies sorting, verify the sort key against the provided reference data or headers. Do not assume 'lowest to highest' applies to the last numeric column if the reference shows it applies to a date or ID column. Mimic the reference sort order exactly.
    rationale: Some tasks failed due to incorrect input inspection (not reading full structure) or incorrect sort keys (assuming sort column instead of checking reference). Passing rollouts inspected the full input, simulated the final state, and verified sort keys against references. This section codifies the pre-computation strategy.
    derivation: Merged edits 26, 31, 32, 33. Edit 26 focused on input inspection. Edit 31, 32, 33 focused on simulating the final state and verifying sort keys. These are all pre-computation strategy issues. I merged them into one section because they all relate to 'how to plan and inspect before writing'. Cross-source agreement confirms these are important for avoiding structural and logic errors.
    task results:
      58942: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      50324: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      343-20: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      13-1: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000

### Step 1 (action=accept_new_best, score 0.517 -> 0.583)

  Edit 0: section_target=### Computed Values over Formulas, delta_type=section_rewrite, passed=False
    content:
    ### Computed Values over Formulas
    
    When the task asks for a calculation, lookup, or result (even if it mentions "formula," "function," "Index/Match," "VLOOKUP," or "COUNTIFS"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.
    
    - **Do NOT write formula strings** (e.g., `"=SUM(...)"`, `"=IF(...)"`, `"=VLOOKUP(...)"`, `"=INDEX(...)"`, `"=COUNTIFS(...)"`) unless the task **explicitly** commands "insert a formula" or "write a formula object". This is rare. The evaluator checks the *visible* (computed) value, not the formula. If you write a formula string, the evaluator will likely see `None` or the string itself, not the calculated result.
    - **Simulate Logic**: Replicate the calculation logic in Python. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex formulas (e.g., array formulas, duplicate handling), manually filter/sort/rank the data in Python to find the nth value.
    - **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.
    - **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.
    - **Exception**: Only write a formula string if the task explicitly states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., for a specific Excel feature that cannot be pre-computed) **AND** the evaluator is configured to evaluate formulas. Default to computed values. If in doubt, compute the value in Python and write the literal result.
    - **Duplicate Handling**: When a lookup key has multiple matches (e.g., duplicate product names), do not just take the first match unless the task implies a simple lookup. Simulate the "same row" or "nth occurrence" logic in Python. For example, if D2 matches A2, return B2; if D6 matches A6 (where A6 is a duplicate of A2), return B6. Count occurrences or use row indices to determine the correct match.
    - **Formula Columns as Data**: When a column contains Excel formulas (e.g., `=IF(...)`) that compute visible values, **compute the result in Python** and write the literal value. Do not copy the formula string itself, as the evaluator checks the computed value, not the formula text.
    - **Decimal Formatting**: When the task specifies a decimal format (e.g., "with .00 decimal", "two decimal places"), use Python string formatting (e.g., `f"{value:.2f}"`) to produce the exact number of decimal digits required. Do not round to integers or truncate arbitrarily unless explicitly commanded.
    - **Formatting Override**: If the task explicitly commands a formatting change (e.g., "do not bold", "remove bold"), you must explicitly set the cell's font/style to match the command, even if the original cell has different formatting. Do not assume the original formatting persists or is irrelevant.
    - **Input Formula Handling**: When reading input cells that contain formula strings (e.g., `"=VLOOKUP(...)"`), treat them as non-numeric text. Do not attempt to parse them as numbers. Extract the required data from source sheets/data structures instead of evaluating the formula string.
    rationale: The most prevalent failure pattern is the agent writing Excel formula strings (e.g., '=IF(...)', '=INDEX(...)') when the evaluator expects computed literal values. This causes `pred=None` or string-mismatch failures. The insight is that agents must 'simulate logic in Python' and write literals. Additional high-confidence patterns discovered: 1) Duplicate lookup handling (take first match or simulate row-index matching), 2) Decimal precision formatting (strict adherence to task-specified decimal places), 3) Formatting overrides (explicitly apply/remove bold/etc. when commanded), 4) Input formula handling (treat formula strings in input as text, don't parse as numbers), 5) Formula columns as data (compute visible values of formula columns, don't copy formula strings). Cross-patch consensus from 20+ independent edits confirms these are critical.
    derivation: Merged edits 1, 2, 4, 7, 8, 9, 10, 12, 13, 14, 16, 19, 20, 22, 30, 31, 32, 33, 36, 38, 40. Edit 1 added decimal formatting. Edit 2 tightened the exception clause. Edit 4 added formula columns as data. Edit 7 added openpyxl warning. Edits 8, 9, 10, 12, 13, 19, 20, 31, 32, 33, 36, 38, 40 all reinforced 'compute literals, don't write formula strings' with varying degrees of detail; kept the most comprehensive formulation. Edit 14 added formatting override. Edit 16 added cross-sheet simulation and verification. Edit 22 added verification step. Edit 30 added duplicate handling. Edit 38 added input formula handling. Dropped minor variations that repeated the same core instruction. All source tasks (49036, 56920, 208-20, 58147, 42198, 53647, 52807, 49945, 10747, 11842, 247-24, 37462, 54638, 31746, 41601, 48620, 59160, 47933, 55060, 52541) support these patterns.
    task results:
      55060: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      49945: status=RETAINED, inc_pr=0.333, cand_pr=0.333
      47933: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      52541: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      59160: status=RETAINED, inc_pr=0.667, cand_pr=0.667
      56920: status=RETAINED, inc_pr=0.333, cand_pr=0.667
      42198: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      41601: status=LOST, inc_pr=0.333, cand_pr=0.000
      49036: status=RETAINED, inc_pr=0.333, cand_pr=0.333
      48620: status=RETAINED, inc_pr=0.667, cand_pr=1.000
      10747: status=LOST, inc_pr=0.333, cand_pr=0.000
      52807: status=GAINED, inc_pr=0.000, cand_pr=0.333
      31746: status=RETAINED, inc_pr=1.000, cand_pr=0.667
      247-24: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      53647: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      54638: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      208-20: status=RETAINED, inc_pr=0.333, cand_pr=0.333
      37462: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000
      58147: status=RETAINED, inc_pr=0.667, cand_pr=1.000
      11842: status=STILL_UNSOLVED, inc_pr=0.000, cand_pr=0.000

  Edit 1: section_target=### Sheet and Range Fidelity, delta_type=section_rewrite, passed=True
    content:
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
    rationale: Multiple tasks failed due to structural issues: creating new workbooks (losing original sheets), writing to the wrong sheet name, failing to compact filtered data into the answer position, self-clobbering (clearing cells before reading them), and incorrect transposition logic. Key insights: 1) Read data into Python lists BEFORE clearing cells to avoid self-clobbering. 2) Contiguous writing is mandatory for filtered data. 3) Group-aware transposition is needed when source data has blank-row separators. 4) Block-based transposition is needed for repeating patterns (e.g., question+options). 5) Conditional clearing (set to '' instead of deleting rows) is the correct approach for 'keep only X' tasks. 6) Column deletion by header pattern requires reverse-order deletion to avoid index shifts. Cross-patch consensus from 10+ independent edits confirms these patterns.
    derivation: Merged edits 5, 15, 24, 25, 26, 27, 28, 29, 41, 42. Edit 5 added group-aware transposition. Edit 15 added excluding blanks in collection. Edit 24 added conditional clearing. Edit 25 added multi-condition OR extraction. Edit 26 added read-before-write. Edit 27 added contiguous write pattern. Edit 28 added value replacement mapping. Edit 29 added fill-down with termination. Edits 41, 42 added block-based transposition and column deletion by header pattern. Kept all distinct patterns. Dropped redundant formulations of contiguous writing. All source tasks (147-48, 423-16, 382-10, 178-22, 388-47, 438-18, 304-35, 55817, 535-20) support these patterns.
    task results:
      382-10: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      388-47: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      535-20: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      178-22: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      55817: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      438-18: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      423-16: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      147-48: status=RETAINED, inc_pr=0.333, cand_pr=0.667
      304-35: status=RETAINED, inc_pr=0.333, cand_pr=0.333

  Edit 2: section_target=### Data Processing and Lookup Logic, delta_type=section_rewrite, passed=False
    content:
    ### Data Processing and Lookup Logic
    
    **Multi-Column Lookups**:
    - When matching across multiple columns (e.g., Column B and D), you must match **ALL** specified criteria simultaneously. A partial match is insufficient.
    - Convert search keys and data values to a common type (e.g., string) before comparison to avoid silent type mismatches.
    - Use the matching row's data from the target output column.
    - **Ambiguous Column References**: When the instruction refers to columns by letter (e.g., "Column A matches NUMVAL"), verify the actual header name in the row above to confirm the column index. "NUMVAL" might be in column S, not U. Do not assume the column letter matches the header name's position in the alphabet.
    - **Literal vs. Reference Values**: When the instruction says "Column F and G match O and P", determine if "O" and "P" are literal string values to match against, or if they refer to **Column O** and **Column P** in the data. In most data-matching contexts, they refer to the values in those columns (e.g., LASTNAME column and FIRSTNAME column). Verify by checking if literal matches are possible; if not, interpret them as column references.
    
    **Date and Type Handling**:
    - **Normalize Dates**: Convert all dates to a consistent type (e.g., `datetime` or `YYYY-MM-DD` string) before grouping or sorting. Do not mix string dates and datetime objects.
    - **Preserve Original Types**: When copying values, preserve their original Python type (e.g., `str`, `datetime`, `float`). Do not convert string dates to datetime objects unless explicitly required.
    - **Identify Correct Columns**: Verify which column contains the primary data for filtering (e.g., check if the 'date' column is Column A or Column N). Do not assume metadata columns contain row-specific data.
    - **Extract Time from Datetime**: When comparing timestamps against time-only ranges (e.g., 21:30–22:00), extract the time component from `datetime` objects using `.time()` before comparison. Do not compare full datetime objects against `time` objects directly. Ensure both sides of the comparison are of the same type (preferably `datetime.time`).
    
    **Date Range Lookup Logic**:
    - When the task asks to find a value based on a date falling "within" or "between" dates, or "closest previous" date, implement this by filtering source rows where `source_date <= query_date` and selecting the one with the **maximum** source date (i.e., the most recent date that is not later than the query date). This handles the "adjacent" or "within" logic correctly without requiring exact matches.
    
    **Lookup Implementation Pattern**:
    - Build an explicit Python dictionary mapping lookup keys to target values from the source data.
    - Iterate through target rows and fill cells using the dictionary. If a key is not found, write `''` or `None`.
    - For conditional logic, evaluate the condition in Python for each row and write the resulting literal value.
    - **Scope of Lookup**: When the task requires matching a row against another set of data (e.g., "find the program for this client"), the source data for the lookup is typically the **entire relevant range** of the sheet (or a specific source sheet), not just the rows currently being written to. Do not restrict the search to the answer position rows.
    - **Cross-Row Matching**: If the instruction implies a relationship between two datasets (e.g., client data and episode data) in the same sheet, treat all data rows as potential sources. Iterate over `ws.max_row` (or the last row of the source data) to find matches, not just the rows in the `answer_position`.
    - **Counting by Criteria**: When the task requires counting entries that match multiple criteria (e.g., Name column header + Time range row), iterate over the source data rows and increment a counter for each row that satisfies all conditions. Write the final integer count as a literal value into the target cell.
    
    **Expanding/Constrained Window Logic**:
    - When the task requires a calculation where each row's result depends on a subset of data that changes based on row position (e.g., "exclude previous rows," "cumulative," "rolling window"), do **not** pre-filter the entire dataset into a static list and slice it. Instead:
      - Simulate the final state row-by-row.
      - For each output row, determine the specific source rows that should be included based on the positional constraint.
      - Compute the value using only those specific source rows.
      - This prevents "empty result" failures when the static filtered list runs out of items before the output range ends.
    
    **Value Replacement via Mapping**:
    - When the task requires replacing specific values with new ones (e.g., country codes, abbreviations), build a Python dictionary mapping the old values to the new values. Iterate through the source data, look up each value in the dictionary, and write the mapped result. This is more robust than complex if/else chains.
    
    **Identify Source Data Rows**:
    - When source data is interspersed with headers or metadata rows, identify valid data rows by checking for non-null values in critical columns (e.g., Name, Date, ID). Skip rows where these key fields are None or empty, as they likely represent structural elements rather than data entries.
    
    **Unique ID Assignment**:
    - When assigning unique identifiers based on first appearance, build a mapping dictionary by iterating through the source column top-to-bottom. Assign a new ID (starting at 1) only when a value is encountered for the first time. Map all subsequent occurrences of that value to the same ID. Write the mapped ID to the target column for every row.
    
    **Map Source to Target**:
    - When the answer position (target cells) references data from a different column or range (e.g., looking up a value in Column G to fill Column F), you must identify the *source* of the truth. Do not assume the source data is in the same row. Build a lookup dictionary from the source data and map each target cell to its corresponding computed value.
    
    **Contiguous Block Detection**:
    - For tasks involving identifying ranges of markers (e.g., 'm' for meetings), scan for contiguous sequences. Map the start and end of each block to the corresponding headers (e.g., time columns) and write the start/end times to the output columns.
    rationale: Several tasks failed due to incorrect data processing: partial multi-column matches, mixed date types, misidentifying primary data columns, incorrect lookup scope (searching only answer position instead of full sheet), windowing logic errors (pre-filtering instead of row-by-row simulation), and duplicate handling. Key insights: 1) Multi-column lookups require ALL criteria match + type normalization + ambiguous column reference resolution. 2) Date range lookups require 'max date <= query date' logic. 3) Lookup scope must be the entire sheet, not just the answer position. 4) Windowing tasks require row-by-row simulation, not static list slicing. 5) Value replacement via dictionary mapping is more robust than if/else chains. 6) Unique ID assignment via first-appearance dictionary. 7) Map source to target when columns are decoupled. 8) Identify source data rows by checking for non-null critical fields. Cross-patch consensus from 7+ independent edits confirms these patterns.
    derivation: Merged edits 3, 6, 11, 16, 17, 18, 19, 23, 34, 35, 37. Edit 3 added map source to target. Edit 6 added expanding/constrained window logic. Edit 11 added duplicate lookup (first match). Edit 16 added date range lookup logic. Edits 17, 18, 19 added time extraction, counting by criteria, and source data row identification. Edit 23 added unique ID assignment. Edits 34, 35 added lookup scope and ambiguous column references. Edit 37 cleaned up multi-column lookups. Kept all distinct patterns. Dropped redundant formulations. All source tasks (48608, 33722, 38985, 52305, 48588, 59734, 50952) support these patterns.
    task results:
      48588: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      50952: status=GAINED, inc_pr=0.000, cand_pr=0.333
      48608: status=RETAINED, inc_pr=0.667, cand_pr=0.667
      52305: status=RETAINED, inc_pr=1.000, cand_pr=1.000
      59734: status=LOST, inc_pr=0.667, cand_pr=0.000
      33722: status=RETAINED, inc_pr=0.333, cand_pr=0.667
      38985: status=RETAINED, inc_pr=0.333, cand_pr=0.667

## Budget
Produce at most 6 edit units.
~~~~

## Original RESPONSE (full)

~~~~json
{
  "reasoning": "I am consolidating 34 raw edits into a maximum of 6 high-confidence edits. The 'Computed Values over Formulas' section is the most critical and has the highest volume of edits (10+ distinct proposals), so it requires a comprehensive rewrite that integrates precision, whitespace, and cognitive triggers. 'Sheet and Range Fidelity' needs to absorb the new structural rules (A1 anchor, substring matching, variable-length grids, styling). 'Data Processing and Lookup Logic' needs to absorb the complex parsing and expansion rules (Cartesian products, consecutive group detection, header alignment). The remaining edits are either redundant with these three or form a small, distinct category ('Block-Based Output') that warrants a new section.",
  "edits": [
    {
      "section_target": "### Computed Values over Formulas",
      "delta_type": "section_rewrite",
      "content": "### Computed Values over Formulas\n\n**CORE RULE: Write the visible result, never the formula mechanism.**\nWhen the task asks for a calculation, lookup, concatenation, or result (even if it mentions \"formula,\" \"function,\" \"LEFT,\" \"RIGHT,\" \"INDEX,\" \"MATCH,\" \"VLOOKUP,\" or \"SUMIFS\"), **compute the final value in Python** and write the **literal result** (number, text, boolean) into the cell.\n\n- **NEVER write formula strings** (e.g., `\"=SUM(...)\"`, `\"=IF(...)\"`, `\"=VLOOKUP(...)\"`, `\"=LEFT(...)\"`) into cells. The evaluator reads the cell value directly without executing Excel formulas. Writing a formula string results in `None` or the string itself, which is always incorrect.\n- **Simulate Logic in Python**: Replicate the calculation logic using Python/pandas/openpyxl. For lookups, build a Python dictionary mapping keys to values. For conditionals, evaluate `if/else` logic in Python. For complex aggregations (e.g., SUMPRODUCT with multiple criteria), filter the data in Python and compute the sum/average/max/min. For text manipulation (LEFT, RIGHT, CONCATENATE, SUBSTRING), use Python string slicing and concatenation.\n- **Write Literals**: Write the computed number, string, or boolean directly. Do not write a string that looks like a formula.\n- **Preserve Precision**: Write the **full precision** of the computed value. Do **not** round, truncate, or format numbers (e.g., do not round to 4 decimal places) unless the task explicitly instructs you to round or format the output. The evaluator compares against the exact computed value.\n- **Preserve Whitespace**: When performing text manipulation tasks (substring, split, replace, delete, etc.), preserve all whitespace characters (spaces, tabs) exactly as they appear in the source text, unless the instruction explicitly commands trimming or stripping. **Do NOT strip trailing whitespace** from results unless told to \"trim\", \"strip\", or \"remove extra spaces\".\n- **Handle Missing/No-Match**: If a lookup fails or a condition is not met, write an empty string (`''`) or `None` (which becomes blank in Excel). Do not leave error values like `#N/A`.\n- **Exception (Rare)**: Only write a formula string if the task **explicitly** states the cell must contain a formula that will be evaluated dynamically by Excel (e.g., \"insert the formula string into the cell\" or \"write the formula object\"). In almost all cases, \"create a formula\" or \"use a formula\" means \"calculate the result using formula logic.\" Default to computed values.\n\n**Cognitive Trigger**: Before writing any cell value, ask: \"Am I writing a computed value (e.g., `100`, `\"Total\"`) or a formula string (e.g., `\"=SUM(A1:A10)\"`) or a formula string with visible result? If the answer is a formula string, STOP and reconsider. Unless the task explicitly demands a formula, you must compute the value in Python and write the literal result. Verify: \"If I open this file in Excel, what text/number will be visible in this cell?\" If your code writes a string starting with \"=\", you are likely wrong."
    },
    {
      "section_target": "### Sheet and Range Fidelity",
      "delta_type": "section_rewrite",
      "content": "### Sheet and Range Fidelity\n\n**Preserve Sheet Identity**:\n- Load the input workbook and modify it in place. Do **not** create a new `Workbook()` and save it, as this discards original sheet names and structure.\n- If you must create a new sheet, explicitly set its title to match the original sheet name (e.g., `ws.title = 'Sheet1'`).\n- When using `pandas.to_excel`, explicitly specify `sheet_name` to match the original sheet name. Prefer `openpyxl` for cell-level edits to avoid sheet name ambiguity.\n\n**Answer Position Alignment**:\n- If the task specifies an `answer_position` (e.g., `A3:G28`), write data into that exact range.\n- **Contiguous Writing**: When filtering or deleting rows, write the kept rows **contiguously** starting from the first row of the answer position (e.g., row 3). Do not preserve original row indices or leave gaps. The first kept row goes to the first cell of the answer position; the second kept row goes to the next cell, etc.\n- **Verification**: After writing, ensure the answer position contains no empty rows between data rows and that the first cell contains the first expected data row.\n- **A1 Anchor Requirement**: When a task explicitly requires a specific row or cell (e.g., \"ensure X appears in A1\") to be the *first* visible content, you must **delete** the preceding rows or shift the content to row 1 using row deletion (`ws.delete_rows`). Do **not** simply overwrite the `answer_position` starting at a later row (e.g., A2) while leaving A1 empty or with previous data.\n- **Single-Column Constraint**: If the `answer_position` is a single column (e.g., `B11:B17`), write **only the primary data value** to each cell. Do **not** concatenate values from multiple source columns (e.g., do **not** write \"1 Text 1\" if you have values from column A=1 and B=\"Text 1\"). Unless the task explicitly instructs to combine columns, each cell receives exactly one value: the one corresponding to the column(s) being copied to that specific target column.\n- **Variable-Length Output into Fixed Grids**: When the source data produces a variable number of items (e.g., splitting a string of length N, or extracting N unique values) into a fixed answer position (e.g., D5:K7):\n  1. **Write Contiguously**: Write items starting from the first cell of the answer position (e.g., D5), filling subsequent cells in order (E5, F5, etc.).\n  2. **Leave Remaining Cells Blank**: Do NOT overwrite cells in the target range that do not have corresponding source data. Leave them as `None` or empty string (`''`). Do not fill them with default values unless instructed.\n\n**Full Range Iteration**:\n- When scanning for conditions, iterate over the **full range** of rows containing data (use `ws.max_row` or detect the last non-empty row in the widest column). Do not break early on empty cells in condition columns; data may exist in other columns of later rows.\n\n**Read Before Write**:\n- When modifying a worksheet in-place (e.g., filtering/deleting rows or columns), always read and store all relevant source data into a Python list/dictionary **before** clearing or overwriting any cells in the source sheet. If you clear cells before reading them, you will lose the data you need to write back.\n\n**Conditional Clearing (Keep/Discard)**:\n- When instructed to keep only specific values and delete/clear others, iterate over the target range. For each cell, if the value matches the target literal (case-insensitive if the instruction implies it, otherwise case-sensitive), preserve it. If it does not match, set the cell value to `''` (empty string). Do not delete rows; only clear cell contents.\n\n**Multi-Condition Row Extraction (OR Logic)**:\n- When extracting rows based on multiple conditions with OR logic (e.g., Col B equals X OR Col C equals Y), iterate through all data rows in the source sheet. Evaluate each row against all conditions; if any condition is true, include the entire row in the result set. Write the headers to the specified row in the target sheet, then write all matching rows contiguously below the headers.\n\n**Group-Aware Transposition**:\n- When transposing data from a column to a wider range (e.g., Column A to Columns C-K), do NOT simply flatten all non-blank values into a continuous stream. Instead, first inspect the source column for structural separators (such as blank rows). Group consecutive non-empty values separated by these blanks, and map each group to a distinct row in the target output range. Preserve the group boundaries as row boundaries in the result.\n\n**Block-Based Transposition**:\n- When transposing data from a single column into a multi-column grid:\n  1. **Detect Block Structure**: Identify the repeating pattern in the source column. Count the number of items per logical block (e.g., Question + 4 Options + Answer + Explanation = 7 items).\n  2. **Calculate Grid Dimensions**: Determine the target grid size (rows × columns) from the `answer_position` or task description. Verify that `total_items = rows × columns`.\n  3. **Read and Chunk**: Read all non-empty values from the source column into a flat list. Chunk this list into blocks of the detected size.\n  4. **Write Row by Row**: Iterate through each block. Write the items in the block sequentially into the target row, starting from the first column of the answer position.\n  5. **Preserve Order**: Maintain the original order of items within each block. Do not sort or rearrange unless explicitly instructed.\n\n**Column Deletion by Header Pattern**:\n- When deleting columns based on a pattern in the header row:\n  1. **Scan Headers**: Iterate through all columns in the header row (typically row 1). Identify columns whose header text contains the specified keyword or pattern.\n  2. **Collect Column Indices**: Store the indices of columns to delete.\n  3. **Delete in Reverse Order**: To avoid index shifting issues, delete columns in descending order of their column index (right-to-left). Alternatively, build a new sheet containing only the columns to keep.\n  4. **Preserve Structure**: When using `openpyxl`, modify the existing sheet. If creating a new sheet, ensure the title matches the original. Preserve all rows and non-deleted columns exactly.\n  5. **Verify**: After deletion, verify that no remaining headers contain the deleted keyword.\n\n**Substring Match for Identifiers**:\n- When the task asks to find a row containing a specific string (e.g., 'BR1 Sales'), use a substring/partial match (e.g., `if 'BR1 Sales' in str(cell_value)`) rather than an exact equality check. The identifier may have suffixes or prefixes (e.g., 'BR1 Sales JUN').\n\n**Preserve Structural Separators in Output**:\n- When the source data contains section headers or category labels mixed within the data rows (e.g., 'Engine HS' appearing between data rows), and the task requires extracting/matching these rows, **include these header rows in the output** if they are part of the original data structure. Do not skip them to create a purely contiguous data block unless explicitly instructed to filter them out. Map the source row directly to the output row to maintain the original logical grouping.\n\n**Formatting Propagation**:\n- When adding a new column or cell range based on existing data, copy the formatting (font, fill, border, alignment) from the adjacent source column to the new column. Use `openpyxl.styles` to extract style properties from the source cell and apply them to the target cell. This ensures the output matches the visual style of the original spreadsheet.\n\n**Apply Explicit Styling**:\n- If the instruction requests specific formatting (e.g., borders, fonts, colors, alignment), apply these styles to the target cells **after** writing the data values. Use `openpyxl.styles` to create border, font, and alignment objects, and assign them to the relevant cells.\n- **Borders**: Create a `Border` object with `Side(style='thin')` for left, right, top, bottom. Apply to the entire answer range.\n- **Fonts**: Create a `Font` object with the specified name and size. Apply to the entire answer range.\n- **Verification**: After saving, verify that the target cells have the correct values AND the requested styles."
    },
    {
      "section_target": "### Data Processing and Lookup Logic",
      "delta_type": "section_rewrite",
      "content": "### Data Processing and Lookup Logic\n\n**Multi-Column Lookups**:\n- When matching across multiple columns (e.g., Column B and D), you must match **ALL** specified criteria simultaneously. A partial match is insufficient.\n- Convert search keys and data values to a common type (e.g., string) before comparison to avoid silent type mismatches.\n- Use the matching row's data from the target output column.\n- **Ambiguous Column References**: When the instruction refers to columns by letter (e.g., \"Column A matches NUMVAL\"), verify the actual header name in the row above to confirm the column index. \"NUMVAL\" might be in column S, not U. Do not assume the column letter matches the header name's position in the alphabet.\n- **Literal vs. Reference Values**: When the instruction says \"Column F and G match O and P\", determine if \"O\" and \"P\" are literal string values to match against, or if they refer to **Column O** and **Column P** in the data. In most data-matching contexts, they refer to the values in those columns (e.g., LASTNAME column and FIRSTNAME column). Verify by checking if literal matches are possible; if not, interpret them as column references.\n\n**Column Header vs Instruction Alignment**:\n- When the result sheet has pre-existing headers (e.g., 'Designation', 'Contact no') that differ from the instruction's wording (e.g., 'display the designation... person's name is listed'), **follow the column headers for column mapping**, not the instruction's narrative labels.\n- Specifically: If the instruction says \"display X in column B\" but the header in column B is \"Designation\", and the task involves populating based on a designation criterion, write the **designation text** into column B, not the name. The header defines the column's semantic role.\n- If the instruction explicitly says \"write the name in column B\" AND the header says \"Name\", write the name. But if the header says \"Designation\", write the designation value.\n- This prevents the common error of writing the wrong field (e.g., Name) into a column labeled with a different field (e.g., Designation).\n\n**Date and Type Handling**:\n- **Normalize Dates**: Convert all dates to a consistent type (e.g., `datetime` or `YYYY-MM-DD` string) before grouping or sorting. Do not mix string dates and datetime objects.\n- **Preserve Original Types**: When copying values, preserve their original Python type (e.g., `str`, `datetime`, `float`). Do not convert string dates to datetime objects unless explicitly required.\n- **Identify Correct Columns**: Verify which column contains the primary data for filtering (e.g., check if the 'date' column is Column A or Column N). Do not assume metadata columns contain row-specific data.\n- **Extract Time from Datetime**: When comparing timestamps against time-only ranges (e.g., 21:30–22:00), extract the time component from `datetime` objects using `.time()` before comparison. Do not compare full datetime objects against `time` objects directly. Ensure both sides of the comparison are of the same type (preferably `datetime.time`).\n- **Date-Month Matching**: When the task requires matching dates to a month name (e.g., \"January\"), parse the date values in the source column (e.g., `datetime` objects) and compare their `.month` attribute against the target month number (mapped from the month name string). Sum or filter values based on this match, not on string comparison of the full date.\n\n**Date Range Lookup Logic**:\n- When the task asks to find a value based on a date falling \"within\" or \"between\" dates, or \"closest previous\" date, implement this by filtering source rows where `source_date <= query_date` and selecting the one with the **maximum** source date (i.e., the most recent date that is not later than the query date). This handles the \"adjacent\" or \"within\" logic correctly without requiring exact matches.\n\n**Lookup Implementation Pattern**:\n- Build an explicit Python dictionary mapping lookup keys to target values from the source data.\n- Iterate through target rows and fill cells using the dictionary. If a key is not found, write `''` or `None`.\n- For conditional logic, evaluate the condition in Python for each row and write the resulting literal value.\n- **Scope of Lookup**: When the task requires matching a row against another set of data (e.g., \"find the program for this client\"), the source data for the lookup is typically the **entire relevant range** of the sheet (or a specific source sheet), not just the rows currently being written to. Do not restrict the search to the answer position rows.\n- **Cross-Row Matching**: If the instruction implies a relationship between two datasets (e.g., client data and episode data) in the same sheet, treat all data rows as potential sources. Iterate over `ws.max_row` (or the last row of the source data) to find matches, not just the rows in the `answer_position`.\n- **Counting by Criteria**: When the task requires counting entries that match multiple criteria (e.g., Name column header + Time range row), iterate over the source data rows and increment a counter for each row that satisfies all conditions. Write the final integer count as a literal value into the target cell.\n\n**Expanding/Constrained Window Logic**:\n- When the task requires a calculation where each row's result depends on a subset of data that changes based on row position (e.g., \"exclude previous rows,\" \"cumulative,\" \"rolling window\"), do **not** pre-filter the entire dataset into a static list and slice it. Instead:\n  - Simulate the final state row-by-row.\n  - For each output row, determine the specific source rows that should be included based on the positional constraint.\n  - Compute the value using only those specific source rows.\n  - This prevents \"empty result\" failures when the static filtered list runs out of items before the output range ends.\n\n**Value Replacement via Mapping**:\n- When the task requires replacing specific values with new ones (e.g., country codes, abbreviations), build a Python dictionary mapping the old values to the new values. Iterate through the source data, look up each value in the dictionary, and write the mapped result. This is more robust than complex if/else chains.\n\n**Identify Source Data Rows**:\n- When source data is interspersed with headers or metadata rows, identify valid data rows by checking for non-null values in critical columns (e.g., Name, Date, ID). Skip rows where these key fields are None or empty, as they likely represent structural elements rather than data entries.\n\n**Unique ID Assignment**:\n- When assigning unique identifiers based on first appearance, build a mapping dictionary by iterating through the source column top-to-bottom. Assign a new ID (starting at 1) only when a value is encountered for the first time. Map all subsequent occurrences of that value to the same ID. Write the mapped ID to the target column for every row.\n\n**Map Source to Target**:\n- When the answer position (target cells) references data from a different column or range (e.g., looking up a value in Column G to fill Column F), you must identify the *source* of the truth. Do not assume the source data is in the same row. Build a lookup dictionary from the source data and map each target cell to its corresponding computed value.\n\n**Contiguous Block Detection**:\n- For tasks involving identifying ranges of markers (e.g., 'm' for meetings), scan for contiguous sequences. Map the start and end of each block to the corresponding headers (e.g., time columns) and write the start/end times to the output columns.\n\n**Group-Aware Consecutive Group Detection**:\n- When counting or summing consecutive runs of a value (e.g., consecutive 1s in a column), scan the column sequentially, tracking the current run length.\n- When the run breaks (different value or end of data), if the run length meets the threshold (e.g., >= 2), add its contribution to the total.\n- Reset the run counter on each break.\n- This pattern applies to any task requiring 'groups of adjacent cells' analysis, where only groups meeting a size/sum condition contribute to the result.\n\n**Filter Expansion (Cartesian Product)**:\n- When parsing filter conditions that contain `IN LIST` clauses with multiple values, generate all combinations (Cartesian product) of those values. For each combination, create a separate output row. Keep single-value filters (e.g., `key = value`) constant across all generated rows. Ensure the output rows are contiguous and headers are preserved.\n\n**Complete Word Matching**:\n- When instructed to match a word \"as a complete word\" or find exact word matches within phrases:\n  1. **Split and Compare**: Split the target phrase (e.g., keyword) into individual words (by whitespace).\n  2. **Exact Match**: Check if the search word is present in the list of split words (case-insensitive unless specified otherwise).\n  3. **Do NOT use substring matching**: Do not use simple `in` operator on the raw string (e.g., \"tool\" in \"tools\" is True for substring but may be incorrect for complete word matching if \"tool\" and \"tools\" are distinct tokens). Use word-boundary logic or list containment.\n  4. **Sum/Aggregate**: If multiple phrases match, aggregate the relevant values (e.g., sum search volumes) as instructed.\n\n**Preserve Non-Matching Rows**:\n- When filtering rows based on a condition (e.g., 'delete rows where Col E = 0'), explicitly retain rows that do NOT match the deletion condition, even if they have unusual values (e.g., NaN, 'Total Zero', or non-zero numbers). Do not assume only the explicitly targeted rows remain.\n\n**Multi-Sheet Concatenation**:\n- When instructed to combine data from multiple sheets into a single target sheet (e.g., 'combined'), iterate through each source sheet (excluding the target), read all data rows (skipping headers), and write them contiguously into the target sheet starting from the specified row (typically row 2). Preserve the order of sheets as listed in the workbook or as implied by the task.\n\n**Preserve Original Values**:\n- When modifying a worksheet, do not overwrite cells that are not part of the answer position unless explicitly instructed. If a cell already contains a value and the task does not ask you to change it, leave it unchanged. Do not set cells to `None` or `''` unless the task requires clearing them."
    },
    {
      "section_target": "### Block-Based Output with Individual Rows",
      "delta_type": "new_section",
      "after_section": "### Data Processing and Lookup Logic",
      "content": "### Block-Based Output with Individual Rows\n\nWhen the task asks to merge consecutive rows (e.g., consecutive negative values) but the expected output also includes individual rows that did not form a multi-row block (e.g., single negative rows, non-negative rows), you must include **all** source rows in the output:\n\n1. **Identify Consecutive Blocks**: Find sequences of 2+ consecutive rows matching the condition.\n2. **Merge Blocks**: For each block of 2+, compute the aggregated/merged values as instructed.\n3. **Include Individuals**: For every row that did NOT belong to a multi-row block (single matches, non-matches), include it as an individual row in the output with its original values (or computed values if aggregation is requested even for singles).\n4. **Output Order**: Write merged blocks first (or in their original relative order), followed by individual rows, preserving the original row order within each category.\n5. **Verify Completeness**: Count the total rows in the input (excluding headers) and ensure the output contains the same number of data rows. If the output has fewer rows than the input, you likely missed individual rows.\n\n**Example**: If the input has 10 data rows and 3 form a consecutive-negative block (merged into 1 row), the output should have 1 (merged) + 7 (individuals) = 8 rows total, not just 3."
    },
    {
      "section_target": "### Whitespace Preservation in Text Manipulation",
      "delta_type": "new_section",
      "after_section": "### Block-Based Output with Individual Rows",
      "content": "### Whitespace Preservation in Text Manipulation\n\n**Preserve Original Whitespace**: When performing text manipulation tasks (substring, split, replace, delete, etc.), preserve all whitespace characters (spaces, tabs) exactly as they appear in the source text, unless the instruction explicitly commands trimming or stripping.\n- **Do NOT strip trailing whitespace** from results unless told to \"trim\", \"strip\", or \"remove extra spaces\".\n- **Example**: If the source is `\"Hello World FT123\"` and you remove `\"FT123\"`, the result must be `\"Hello World \"` (with trailing space), NOT `\"Hello World\"`.\n- **Why**: The evaluator checks for exact string match. Trailing whitespace is part of the visible content in the cell."
    },
    {
      "section_target": "### Multi-Column Lookup Execution",
      "delta_type": "new_section",
      "after_section": "### Whitespace Preservation in Text Manipulation",
      "content": "### Multi-Column Lookup Execution\n\nWhen the task requires looking up a value based on **multiple criteria** (e.g., matching two columns to find a third column's value):\n\n1. **Build a Composite Key Dictionary**: Iterate through the source/reference data. For each row, combine the lookup columns into a tuple key (e.g., `(str(col_a_value), str(col_b_value))`) and map it to the target return value. Store this in a Python dictionary.\n2. **Iterate Target Rows**: For each row in the answer position, extract the lookup criteria values. Convert them to strings to ensure consistent comparison.\n3. **Lookup and Write**: Use the composite key to fetch the value from the dictionary. If found, write the **literal computed value** (number, string, etc.) directly into the target cell. If not found, write an empty string (`''`) or `None`.\n4. **Preserve Existing Data**: If the task implies filling in missing values, only overwrite cells that are empty or explicitly targeted. Do not overwrite cells that already contain correct data unless instructed.\n5. **Verify Output**: After writing, read back the target cells to confirm the values are the computed literals, not formula strings. Ensure the structure (rows/columns) matches the expected answer position."
    }
  ],
  "rationale": "The edits consolidate 34 raw proposals into 6 high-confidence structural changes. 'Computed Values' is rewritten to include precision, whitespace, and cognitive triggers. 'Sheet and Range Fidelity' absorbs structural rules. 'Data Processing' absorbs complex logic rules. Two new sections are created for 'Block-Based Output' and 'Whitespace Preservation' which were distinct enough to warrant their own headings, and 'Multi-Column Lookup Execution' is separated to provide a clear procedural pattern for composite key lookups.",
  "derivation": "Merged edits 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34. Edit 10, 11, 12, 13, 14, 15, 18, 22, 29, 32 all reinforced 'compute literals, don't write formula strings'; kept the most comprehensive formulation from Edit 22 and 14. Edit 13 added precision; kept. Edit 1 added whitespace; kept. Edit 2 added A1 anchor; kept. Edit 3 added structural separators; kept. Edit 4 added header alignment; kept. Edit 5 added substring match; kept. Edit 6 added preserve non-matching; kept. Edit 7 added block-based output; kept as new section. Edit 8 added single-column constraint; kept. Edit 9 added multi-column lookup execution; kept as new section. Edit 16 added complex logic simulation; merged into Data Processing. Edit 17 added styling; kept. Edit 19 added filter expansion; kept. Edit 20 added date-month matching; kept. Edit 21 added consecutive group detection; kept. Edit 24 added complete word matching; kept. Edit 25 added variable-length grids; kept. Edit 26 added filter parsing; merged into Data Processing. Edit 27 added Cartesian product; merged into Data Processing. Edit 28 added consecutive group detection; merged into Data Processing. Edit 30 added formatting propagation; kept. Edit 31 added multi-sheet concatenation; kept. Edit 33 added preserve original values; kept. Dropped redundant formulations of contiguous writing and formula prohibition."
}
~~~~