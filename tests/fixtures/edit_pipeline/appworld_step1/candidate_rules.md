### Pagination Discipline
- **Mandatory Loop**: When fetching list data, you MUST implement a loop that increments `page_index` starting from 0 until the API returns fewer results than `page_limit` or an empty list.
- **Accumulate Results**: Store results from all pages in a single list/set before processing. Do not process results page-by-page if the task requires acting on the complete set (e.g., 'all artists', 'disable the rest').
- **Stop Condition**: The loop ends when `len(results) < page_limit` or `results` is empty. Never assume a single page contains all data.
- **Verification**: If the task requires 'all' items, verify completeness by ensuring you have fetched every page. Missing a page leads to incomplete action sets.
- **Explicit Page Limit**: Always set `page_limit` to the maximum allowed value (e.g., 20) in your initial request to minimize API calls and avoid truncation from small defaults. Do not rely on the API's default page limit.

### Authentication and Action Discipline
### Authentication and Action Discipline
- **Explicit Authentication Parameters**: Many APIs (e.g., Spotify, Simple Note) require `access_token` to be passed as an explicit parameter in *every* API call, even after successful login. Do not assume the token is stored in session context. If a call returns 401, verify you are passing `access_token=...` correctly.
- **Login Parameter Discipline**: Before calling any `login` API, inspect its documentation to determine the exact parameter name for the identifier (e.g., `username` vs `email`) and the expected format. Use the identity information from the task prompt or `show_profile`. Never brute-force usernames. For the `phone` app, `username` is the phone number; for others (Spotify, Venmo, Simple Note), it is the email.
- **Idempotent Action Execution**: For actions like 'like', 'follow', or 'add' that are idempotent, execute the action for every entity matching the criteria, regardless of current state (e.g., already liked). Do not skip based on status indicators unless explicitly told to only act on unliked items. The task likely implies ensuring the entire set is acted upon.
- **Entity Label Matching**: When a task refers to a specific entity by name or label (e.g., 'go-to-sleep alarm'), search for that exact string or a clear substring within the entity's label/title field. Do NOT guess based on position, enabled status, or other attributes. Perform case-insensitive matching.

### Temporal Context Resolution
- **Never Hardcode Year/Date**: Tasks often say 'this year', 'last month', or 'recently', but the simulated environment may have a different system date. Never assume the current year matches the calendar year or the prompt generation time.
- **Infer System Date**: Determine the current year dynamically:
  1. Check `apis.supervisor.show_profile()` or `apis.supervisor.show_account_passwords()` for timestamps.
  2. Inspect recent data (e.g., latest transactions, messages, songs) to find the most recent `created_at` or `release_date`. The current date is shortly after this timestamp.
  3. Check API response metadata (e.g., token expiry times) for clues.
- **Dynamic Filtering**: Use the dynamically obtained year/date to filter data (e.g., `release_date.startswith(str(current_year))`). Calculate relative thresholds (e.g., 'last N days') based on the inferred current date.
- **Year and Month**: When filtering by 'this year' and a month, always check BOTH the month AND the year. Files from previous years with the same month belong to 'others' or a different category.

### Playback API Resolution
### Playback API Resolution
- **Direct API Usage**: `show_api_descriptions` output is often truncated. Do NOT rely on scanning it to find playback APIs. If you need to play music, try `apis.spotify.play_music(access_token=..., playlist_id=...)` directly.
- **Fallback Logic**: If `play_music` fails with 422, check for `play_playlist` or `play_song`. If all fail, check `add_to_queue` followed by verifying if playback starts automatically.
- **Avoid Infinite Loops**: Do not spend excessive turns iterating through pages of playlists or searching for the API name. Once a suitable playlist is identified, attempt to play it immediately.