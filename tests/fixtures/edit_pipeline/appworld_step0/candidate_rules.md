### Pagination Discipline
- **Mandatory Loop**: When fetching list data, you MUST implement a loop that increments `page_index` starting from 0 until the API returns fewer results than `page_limit` or an empty list.
- **Accumulate Results**: Store results from all pages in a single list/set before processing. Do not process results page-by-page if the task requires acting on the complete set (e.g., 'all artists', 'disable the rest').
- **Stop Condition**: The loop ends when `len(results) < page_limit` or `results` is empty. Never assume a single page contains all data.
- **Verification**: If the task requires 'all' items, verify completeness by ensuring you have fetched every page. Missing a page leads to incomplete action sets.

### Temporal Context Resolution
- **Never Hardcode Year/Date**: Tasks often say 'this year', 'last month', or 'recently', but the simulated environment may have a different system date. Never assume the current year matches the calendar year or the prompt generation time.
- **Infer System Date**: Determine the current year dynamically:
  1. Check `apis.supervisor.show_profile()` or `apis.supervisor.show_account_passwords()` for timestamps.
  2. Inspect recent data (e.g., latest transactions, messages, songs) to find the most recent `created_at` or `release_date`. The current date is shortly after this timestamp.
  3. Check API response metadata (e.g., token expiry times) for clues.
- **Dynamic Filtering**: Use the dynamically obtained year/date to filter data (e.g., `release_date.startswith(str(current_year))`). Calculate relative thresholds (e.g., 'last N days') based on the inferred current date.
- **Year and Month**: When filtering by 'this year' and a month, always check BOTH the month AND the year. Files from previous years with the same month belong to 'others' or a different category.