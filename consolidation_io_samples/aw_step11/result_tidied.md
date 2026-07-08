### Task Completion Protocol
When a task is finished, signal completion by calling `apis.supervisor.complete_task()`. How you call this function depends on whether the task expects a textual answer or a null value.

#### 1. Default: Null Answer (State Changes & Data Modification)

For most tasks involving state changes (e.g., "like this song", "delete that file", "update record") or data modification, the expected answer is **null** or **empty**. The evaluator verifies success by checking the **side effects** (the state of the app/filesystem) rather than comparing a text argument.

- **Rule:** Call `complete_task()` with **no arguments** (or `status='success'` if the API signature strictly requires it).
- **Do NOT** pass a descriptive string summarizing what you did (e.g., do not pass `answer="I have deleted the file"`).
- **Why:** Passing a string when `null` is expected causes an assertion failure, even if the side effects were correct.

**Correct:**
```python
complete_task()
# or
complete_task(status='success')
```

**Incorrect:**
```python
# This will likely fail if the expected answer is null
complete_task(answer="The file has been deleted.")
```

#### 2. Exception: Explicit Text Summary

Only pass an `answer` argument if the task instructions **explicitly request** a textual response, summary, or explanation.

- **Rule:** If the task says "Summarize the changes", "Provide a report", or asks a direct question requiring text, include that text in the `answer` argument.
- **Default to Null:** If the task is ambiguous or simply asks for an action to be performed without specifying an output, default to the **Null Answer** protocol above.

**Example:**
```python
# Only if the task asks for a summary
complete_task(answer="I have updated the record as requested.")
```

#### 3. Exception: Numeric Answers

When the task asks for a quantity, sum, count, or financial amount, pass the **raw numeric value** (e.g., `1068.0`, `42`, `3.14`).

- **Rule:** Pass the number directly as an integer or float. Do **not** format it as a string with currency symbols, commas, or units (e.g., do NOT pass `"$1068.0"`, `"1,068"`, or `"1068 USD"`).
- **Why:** The evaluator expects a number for comparison; passing a formatted string causes a type mismatch failure, even if the numerical value is correct.

**Correct:**
```python
complete_task(answer=1068.0)
# or
complete_task(answer=42)
```

**Incorrect:**
```python
# This will likely fail due to type mismatch
complete_task(answer="$1068.0")
complete_task(answer="1,068")
```

### File Type Filtering for Text Parsing
When accessing files from the file system, always verify the file type before attempting to read or parse its content as text.

1.  **Identify Binary vs. Text**: Check the file extension or content metadata. Files with extensions like `.pdf`, `.jpg`, `.png`, `.bin`, or `.exe` are typically binary and cannot be reliably parsed for text values using standard string parsing methods.
2.  **Filter Before Processing**: Explicitly exclude binary files from your parsing logic. Only process files with text-based extensions (e.g., `.txt`, `.csv`, `.json`, `.xml`, `.md`) when extracting structured text data.
3.  **Handle Absence Gracefully**: If the required data source is a binary file (e.g., a PDF invoice) and no text alternative exists, acknowledge that the data cannot be extracted via simple text parsing and adjust the output accordingly (e.g., report `null` or an error) rather than attempting to parse binary garbage.

**Example Pattern**:
```python
# Correct approach
for file in files:
    if file.endswith(('.txt', '.csv', '.json')):
        content = read_file(file)
        # Parse content
    else:
        # Skip binary files like .pdf
        continue
```

**Why**: Binary files contain non-text bytes that will cause parsing errors or return meaningless data when treated as strings. Filtering ensures you only sum or process valid text-based values.

### Bill Splitting and Rounding
- **Integer Rounding for Shares**: When splitting a total bill equally among a group, calculate the individual share by dividing the `total_amount` by the total number of people (including the user). You must round the resulting share amount to the nearest integer.
  - Use standard rounding rules: if the decimal part is 0.5 or greater, round up; otherwise, round down.
  - Example: If the total is $100 and there are 3 people, the share is $100 / 3 = $33.33... → Round to **$33**.
  - Example: If the total is $100 and there are 7 people, the share is $100 / 7 = $14.28... → Round to **$14**.
  - Do not output decimal currency values (e.g., do not output $33.33) unless the task explicitly requests precision beyond whole units.

- **Venmo Constraint**: Venmo payment requests strictly require integer amounts. Outputting decimal values (e.g., $33.33) causes validation failures. Always ensure the calculated share is a whole number before generating the request.

- **Venmo Calculation Precision**: When calculating shares for Venmo, you must use standard rounding (`round(total_amount / num_people)`) to determine the integer share. **Do not** use integer division (`//`) as it truncates decimals and may yield an incorrect share (e.g., for $119 split 4 ways, `119 // 4 = 29`, but the correct rounded share is 30). Venmo strictly requires integer amounts; outputting decimals or incorrect truncated integers causes validation failures.

- **Filtering Paid Transactions**: When preparing payment requests for unpaid shares, you must first identify who has already paid to avoid double-charging. Retrieve the list of received transactions. For each person, check if there is a received transaction where the `description` matches the context of the bill (e.g., "Dinner", "Work Lunch") AND the `amount` matches the calculated integer share (or the total amount if it's a full reimbursement). Exclude any sender whose transaction matches these criteria from the list of people you request payment from.

### Spotify Genre Filtering Authority
When filtering or selecting songs based on genre (e.g., 'indie', 'rock'), always use the `genre` field from the song object returned by `apis.spotify.show_song`. Do not rely on the artist's genre, as a single song may involve multiple artists with different genre associations, and the song-level genre is the authoritative source for the song itself.

**Correct Approach:**
```python
song_info = apis.spotify.show_song(song_id)
if song_info['genre'] == 'indie':
    # Process song
```

**Incorrect Approach:**
```python
# Do NOT do this: artist genre may differ from song genre
artist_info = apis.spotify.get_artist(artist_id)
if artist_info['genre'] == 'indie':
    # Risk of error
```

### Date and Time Handling
When tasks involve relative temporal references (e.g., "today", "yesterday", "this month", "this year"), you must derive target dates from the environment's current state rather than hardcoding values.

1.  **Determine Current Date Dynamically**:
    *   Do not assume a specific year, month, or day. The environment's current date may differ from your training data or assumptions.
    *   Use `apis.phone.get_current_date_and_time()` to retrieve the current date and time.
    *   If no direct API is available, infer the year (and date) from the most recent timestamps in the provided data context (e.g., if the latest file is dated `2023-05`, assume the current year is 2023). Do not hardcode the year.
    *   **Critical**: When tasks refer to relative time expressions like 'this year', you must determine the year dynamically. Hardcoding or guessing the year leads to incorrect filtering and calculations.
    *   **Parsing Implementation Note**: When using `apis.phone.get_current_date_and_time()`, the date string may be in a format like "Thursday, May 18, 2023". Extract the year dynamically:
        ```python
        current_dt = apis.phone.get_current_date_and_time()
        # Example parsing for "Thursday, May 18, 2023"
        current_year = int(current_dt['date'].split(',')[0].split()[-1])
        ```

2.  **Calculate Target Dates**:
    *   Based on the dynamically determined "current date", calculate the specific dates or ranges required by the task instructions (e.g., "yesterday" is `current_date - 1 day`).
    *   **Inclusive Window Calculation**: When a task specifies a relative time window like "last N days including today", the start date is calculated by subtracting `(N-1)` days from the current date. This ensures the window contains exactly N days, including the current day.
        *   *Example*: If today is June 4 and the task asks for "last 5 days including today", calculate `June 4 - (5-1) days` = May 31. The valid range is May 31 to June 4.
        *   *Warning*: Do not subtract N days (e.g., June 4 - 5 days = May 30) as this creates an N+1 day window, or assume the start date is N days ago without the inclusive adjustment, as this creates an N-1 day window missing the earliest valid data.

3.  **Apply Date Filters (Month AND Year)**:
    *   When filtering by relative month criteria (e.g., "February of this year"), you must match **both** the target month **and** the target year.
    *   **Common Pitfall**: Do not filter by month alone. A file from "March 2022" does not match "March of this year" if the current year is 2023.
    *   **Logic**:
        ```python
        # Example logic for filtering
        current_date = get_current_date() # e.g., 2023-03-15
        target_year = current_date.year
        target_month = current_date.month # For "this month"

        for file in files:
            file_date = file['created_at']
            # Match both month AND year
            if file_date.month == target_month and file_date.year == target_year:
                # Include in category
                pass
            else:
                # Exclude (or route to "Others")
                pass
        ```
    *   Files that share the target month but have a different year must be treated as non-matching for the specific relative category and routed to fallback categories (e.g., "Others", "Previous Years") as defined by the task.

4.  **Contextual Day Resolution for Daily Plans**:
    *   When a task references a relative time like "today" or "this week" combined with a specific daily plan (e.g., "workout today", "lunch today"), you must determine the specific **day of the week** from the current date.
    *   Extract the parameters for that specific day of the week from the provided data (e.g., if today is Wednesday, use Wednesday's workout duration or menu).
    *   **Critical**: Do **not** assume the task refers to the maximum, minimum, or average value across all days unless explicitly stated. Selecting an aggregate value instead of the day-specific value leads to incorrect results.
    *   **Example**: If a plan lists workout durations for Monday-Sunday, and today is Wednesday, you must use Wednesday's duration to filter or select items, not the longest or shortest duration in the week.

**Why this matters**: Hardcoding dates or ignoring the year component leads to incorrect categorization or empty results, as the agent's perception of "now" must align with the environment's actual state.

### CSV Output Formatting
When generating CSV content, output raw field values separated by commas WITHOUT enclosing them in double quotes, unless the value itself contains a comma or other character requiring standard CSV escaping.

Incorrect: `"Title","Artist"`
Correct: `Title,Artist`

Reason: The evaluation parser splits lines by comma and expects raw values. Enclosing fields in quotes causes the parser to retain the literal quote characters in the extracted values (e.g., `"Artist"` instead of `Artist`), leading to string comparison failures.

### Search and Aggregation Strategy
### Search and Aggregation Strategy

#### 1. Unit Normalization
Before performing any sorting, comparison, or aggregation on duration attributes (e.g., `duration` from `show_song`), ensure all values are in the same unit.
- **Identify Units:** APIs may return durations in seconds (e.g., `show_song` often returns `duration` in seconds), while tasks or other sources might imply minutes.
- **Convert Early:** Convert all relevant duration values to the unit required by the task logic (typically minutes for human-readable comparisons or if specified). For example:
  ```python
  # If duration is in seconds and you need minutes:
  duration_minutes = duration_seconds / 60
  ```
- **Why:** Comparing seconds directly to minutes (or summing seconds when the task expects minutes) results in magnitude errors, leading to incorrect filtering, sorting, or selection of items.

#### 2. Mandatory Full Pagination for Data Retrieval
When retrieving lists via API (e.g., `apis.spotify.show_liked_songs`, `apis.messages.search_text_messages`, `apis.phone.show_alarms`, `apis.venmo.show_transactions`), you MUST iterate through ALL pages to ensure the dataset is complete.

- **Why:** Relying on a single page, even with precise filters, leads to truncated data and critical logic errors (e.g., missing liked songs, missing messages for deletion, missing recent payments).
- **How:** Always check if subsequent pages exist and loop through them until no more data is returned.
- **Implementation:** Use a `while` loop starting with `page_index=0`. Fetch the page, extend your master list, and increment `page_index` in the same block. This prevents exhausting the interaction budget on large datasets.
- **Robustness:** Continue fetching regardless of data quality anomalies on individual pages (e.g., missing IDs, null fields, or inconsistent formats). Do not stop early if a page appears malformed or incomplete; the API may return valid data on subsequent pages.
- **Termination:** Stop the loop only when the API returns an **empty list** or a list with **fewer items than the `page_limit`** (indicating the last page).
- **Search Tasks Warning:** When searching for a specific entity (e.g., an alarm with a specific label), do **not** stop the loop upon finding the first match. Retrieve all pages first, then filter the complete collection.
- **Scope:** This applies to all paginated endpoints (e.g., `show_liked_songs`, `show_playlist_library`, `show_song_library`, `show_alarms`, `show_recommendations`, file system listings, `search_contacts`, `show_social_feed`). Always verify your collection is complete before proceeding to filtering, aggregation, or modification actions.
- **Spotify Pagination Limit:** When paginating through Spotify lists (e.g., `show_song_library`, `show_playlist_library`, `show_liked_songs`), explicitly set `page_limit=20`. This is the API's maximum allowed value; using the default or higher limits (e.g., 50) will cause validation errors (422). Setting this maximum minimizes the total number of API calls required to fetch all data.
- **Spotify Downloaded Songs Specificity:** When retrieving the list of downloaded songs via `apis.spotify.show_downloaded_songs`, you MUST iterate through ALL pages. This endpoint is particularly prone to returning a severely truncated set (e.g., only 5 items) on the first page. Failing to paginate results in an incomplete set of downloaded song IDs. This causes a critical logic error: when checking if an album is fully downloaded (intersection of Album Songs AND Downloaded Songs), missing IDs in the downloaded set lead to incorrect conclusions that songs are not downloaded, resulting in erroneous removal of valid albums.
- **Voice Message Pagination:** When searching or deleting voice messages (e.g., `apis.phone.search_voice_messages`), you must iterate through **ALL** pages. The API may return a truncated set on the first page, leading to missed items if pagination is skipped. This requirement is identical to that for text messages; failing to paginate results in incomplete deletion or listing.
- **Venmo Social Feed Volume:** The Venmo social feed (`show_social_feed`) is a high-volume endpoint that can contain hundreds of items across many pages (e.g., 50+ pages). Agents must explicitly set `page_limit=20` (the maximum allowed) in every pagination call to minimize the total number of API calls and avoid hitting interaction budget limits. Always iterate through all pages using a `while` loop until an empty list or fewer items than `page_limit` is returned.
- **Critical Failure Modes of Partial Pagination:** Failing to iterate through all pages leads to specific, high-impact errors in common task types:
  - **Temporal Selection Errors:** The 'last', 'most recent', or 'latest' item (e.g., last payment, last message) is often on a subsequent page. Selecting from page 0 yields incorrect results.
  - **Aggregation Errors:** Summing amounts (e.g., total sent/received) from only the first page significantly undercounts totals.
  - **Logic/Matching Errors:** Missing pages means missing completed payments or pending requests, leading to incorrect actions (e.g., requesting money from someone who already paid, or missing pending approvals/denials).

#### 3. Per-Source Search with Native Sorting (Extrema)
For finding global minimums or maximums (e.g., least played song, longest playlist), use the API's built-in sorting capabilities where possible.
1. **Filter by Entity ID:** If the task specifies an entity (e.g., a specific artist), use the numeric ID (e.g., `artist_id`) in your search parameters. Never search by name string, as it is ambiguous and may miss items.
2. **Sort by Metric:** Use the `sort_by` parameter with the relevant numeric field. Use a `+` prefix for ascending order (minimum) or a `-` prefix for descending order (maximum).
3. **Limit Results:** Set `page_limit=1` to retrieve only the first item from the sorted list for that source.
4. **Verification of Completeness:** After retrieving the initial sorted result (the candidate from page 0), you must verify that no additional pages of data exist that could contain a better candidate.
   - **Check Next Page:** Query the API for the next page (e.g., `page_index=1`) with the same sort order and filters.
   - **Evaluate:** 
     - If the next page is **empty**, the candidate from page 0 is the global extremum for that source.
     - If the next page is **not empty**, the first result might not be the global extremum (e.g., if the sort order is ambiguous or if the API pagination behaves unexpectedly). In such cases, you must fetch enough pages to confirm the extremum or adjust your strategy to ensure you have the true minimum/maximum.

**Why:** Without this check, an agent might select the least played song from page 0, while a song with even fewer plays exists on page 1. This step ensures the 'least/most' result is truly global, not just local to the first page.

#### 4. Cross-Source Comparison
If you searched multiple sources using **Step 3** (Extremum strategy), you will have one candidate item from each source (the extremum within that source). Compare these candidates against each other to determine the global extremum across all sources.

**Example Pattern:**
```python
# Task: Find the least played song by Artist A across Songs and Playlists
artist_id = 9

# Source 1: Song Library
song_results = apis.service.search_songs(artist_id=artist_id, sort_by='+play_count', page_limit=1)
song_candidate = song_results[0] if song_results else None

# Source 2: Playlist Library (if applicable)
playlist_results = apis.service.search_playlists(artist_id=artist_id, sort_by='+play_count', page_limit=1)
playlist_candidate = playlist_results[0] if playlist_candidate else None

# Global Comparison
candidates = [c for c in [song_candidate, playlist_candidate] if c is not None]
global_min = min(candidates, key=lambda x: x['play_count'])
```

#### 5. Cross-Source Aggregation Pattern (For Complete Population Analysis)
When a task requires analyzing data across multiple independent sources (e.g., songs in Song Library, Album Library, and Playlist Library) to produce a complete result set or aggregate metric:
1. **Fetch All IDs:** Retrieve the ID lists from **all** relevant sources. You must respect pagination for each source to ensure no IDs are missed.
2. **Compute Union:** Create a set of all unique IDs across these sources.
3. **Fetch Details:** Fetch the full details for **every** unique ID in the union.
4. **Final Aggregation:** Perform the final filtering, grouping, or aggregation on this complete set of details.

**Critical Constraint:** Do not stop after finding a match or a single result in one source. If the task implies a global analysis (e.g., "list all...", "sum all...", "find the song with the highest play count across all libraries"), you must ensure you have data from every source before concluding.

**Why this matters:**
- **Completeness:** Relying on native sorting (Step 3) only gives you the extremum *within* a source. If you need the global extremum or a complete list, native sorting is insufficient. Fetching all items ensures you don't miss the answer because it resided in a different library.
- **Accuracy:** Aggregating from partial data (e.g., only songs, only playlists) leads to incorrect totals, counts, or selections.

#### 6. Source-Aware Filtering for Subset Constraints
If a task specifies a subset of data derived from a specific source or service (e.g., "recommended songs," "Spotify's top picks"), you must fetch items from that specific source API rather than searching the entire global library.
1. **Identify the Source API:** Determine which API endpoint provides the constrained subset (e.g., `show_recommendations`, `get_user_top_artists`).
2. **Fetch the Subset:** Call the specific source API to retrieve the candidate pool.
3. **Filter Locally:** Apply the task's additional criteria (e.g., genre, release year, popularity) to this specific pool.

**Why this matters:**
- **Constraint Adherence:** The global library may contain items that match the criteria but were not included in the specific subset requested (e.g., a classical song exists in the library but was not Spotify-recommended). Searching the global library violates the source constraint.
- **Correctness:** Filtering the source-specific results ensures the final answer strictly adheres to the task's origin constraint.

**Example Pattern:**
```python
# Task: Find a classical song from Spotify's recommendations released in 2023

# 1. Fetch the specific subset
recommended_tracks = apis.service.show_recommendations(seed_genres=['classical'])

# 2. Filter the subset for additional criteria
valid_tracks = [t for t in recommended_tracks if t['release_date'].startswith('2023')]

# 3. Select from the valid subset
if valid_tracks:
    final_track = valid_tracks[0]  # Or apply further sorting if needed
else:
    final_track = None  # No match in the constrained source
```

#### 7. Efficient Playlist Duration Calculation
When a task requires calculating the total duration of a playlist:
1. **Use `show_playlist` directly:** Call `apis.spotify.show_playlist` with the playlist ID. This API returns the list of songs in the playlist, and each song object **already includes the `duration` field** (typically in seconds).
2. **Do NOT call `show_song`:** Do not iterate through the songs in the playlist and call `apis.spotify.show_song` for each one to get its duration. This is inefficient, consumes unnecessary API calls, and is redundant since the data is already provided by `show_playlist`.
3. **Calculate Total:** Sum the `duration` values of all songs in the playlist response. If the task requires minutes, divide the total seconds by 60.

**Example Pattern:**
```python
# Get playlist data including song durations
playlist_data = apis.spotify.show_playlist(playlist_id=123)
songs = playlist_data['tracks']  # Each song has a 'duration' field

total_duration_seconds = sum(song['duration'] for song in songs)
total_duration_minutes = total_duration_seconds / 60
```

#### 8. Use Built-in API Filters
When an API endpoint accepts filter parameters (e.g., `genre`, `min_follower_count`, `status`, `category`), **always use these parameters** to narrow down results rather than fetching all items and filtering manually in your code.

**Why:**
1. **Efficiency:** Reduces data transfer and processing load.
2. **Accuracy:** Manual filtering often fails on paginated results or large datasets where not all items are retrieved.
3. **Simplicity:** Reduces code complexity and potential for logical errors.

**Example Pattern:**
```python
# Correct (API Filtering):
artists = client.search(type='artist', q='classical', min_follower_count=22)

# Incorrect (Manual Filtering):
all_artists = client.search(type='artist', q='classical')
valid_artists = [a for a in all_artists if a.followers > 22]
```

### Data Retrieval Strategy
### Data Retrieval Strategy

#### List vs. Detail APIs
Listing APIs (e.g., `show_song_library`, `show_album_library`, `show_playlist`) often return summary data (IDs, titles, added_at) but may lack specific attributes needed for the task (e.g., `play_count`, `release_date`). When the required attribute is missing from the list response, use the API documentation to find the corresponding 'detail' API (e.g., `show_song`, `show_album`) and fetch details for each item individually.

- **Inspect API Response Schemas for Field Names:** Listing APIs and Detail APIs may use different field names for IDs and attributes (e.g., `id` in a playlist list vs. `song_id` in a detail endpoint). The agent must inspect the API documentation or sample responses to confirm exact key names before writing filtering or iteration logic. Do not assume field names are consistent across endpoints.
  - **Implementation:** When accessing an object returned by one API (e.g., a song in a playlist list) and passing it to another API (e.g., fetching song details), verify the exact key names used in the source response. Map these keys correctly to the parameters expected by the target API. Never assume a generic `id` field exists or is named consistently across all endpoints.
  - **Album Search Specifics:** `search_albums` returns summary data including `song_ids` but **not** `play_count`. To compare metrics like `play_count` across songs in an album, you must: (1) search for the album to obtain its `song_ids`; (2) call `show_song(song_id=...)` for each `song_id` individually to retrieve the full details including `play_count`; (3) compare the retrieved values. Do not assume `play_count` is available in the album search response.
  - **Extremum Calculation via Detail Fetching:** When a task requires finding the item with the highest or lowest value of an attribute (e.g., 'most-played', 'least-played', 'longest') that is **not** directly sortable or filterable in the list/search endpoint (e.g., `show_song_library` lacks `play_count` for sorting), you must use the detail-fetching fallback strategy: 1) Retrieve all IDs from the list endpoint using **full pagination** (see *Search and Aggregation Strategy*); 2) Fetch the full detail for **each** ID using the corresponding detail endpoint (e.g., `show_song`); 3) Perform the min/max comparison on the fetched details. Do not assume the list endpoint contains the metric or supports sorting by it. This ensures you have the complete dataset of metrics required to identify the true extremum.

#### Independent Data Sets (Library vs. Playlists)
Do not assume that songs in playlists are a subset of songs in the user library, nor that a removal from the library implies a removal from playlists. The library and playlist song sets are often disjoint.
1. **Iterate Independently:** Always iterate through every playlist in the user's library separately from the library itself.
2. **Retrieve Full Details for All:** For each playlist, retrieve its detailed song list (e.g., using `show_playlist` or equivalent detail endpoints). Check the attributes of EACH song in the playlist directly (e.g., via `show_song`) to determine if it meets the filtering criteria.
3. **Filter Explicitly:** Remove items from playlists based solely on their own attributes, regardless of whether they exist in the user library.
4. **Release Date Filtering Across Disjoint Sources:** When a task requires filtering songs by release date (e.g., 'remove songs released before 2022'), you must fetch song details (via `show_song`) for **every** song in both the song library AND all playlists. The library and playlist song sets are disjoint; a song may exist in a playlist but not the library (or vice versa). Do not assume the library contains all relevant songs. Build a complete map of `song_id -> release_date` across all sources before deciding which songs to remove from which location.
    - **Implementation:** 
      1. Collect all unique song IDs from the library (`show_song_library`) and all playlists (`show_playlist` for each playlist). 
      2. Call `show_song` for each unique ID to retrieve the `release_date`. 
      3. Filter the complete set of songs based on the release date. 
      4. Remove the filtered songs from their respective sources (library or playlists) as needed.
    - **Why:** Filtering only the library misses songs in playlists, and filtering only playlists misses songs in the library. Both sets must be evaluated against the date criteria independently.

#### Spotify API Parameter Distinction (Public vs. User-Specific)
Spotify endpoints are divided into two categories with different authentication requirements. You must pass parameters exactly as defined for each type to avoid 422 (invalid parameter) or 401 (unauthorized) errors.

- **Public Search/Listing & Detail Endpoints (No Token):** Endpoints that fetch public metadata or search for entities **do NOT** accept an `access_token` parameter. Passing one will cause a `422` error. This includes both specific detail lookups (e.g., `show_song`, `show_album`) and broad searches/listings (e.g., `search_albums`, `search_songs`, `show_song_reviews`).
    - **Examples:** `show_song`, `show_album`, `search_albums`, `search_artists`, `search_songs`, `show_song_reviews`.
    - **Required Parameters:** Only the entity identifier (e.g., `song_id`, `album_id`) or search query/filters (e.g., `user_email` for reviews). Note that `show_song_reviews` uses `user_email` to filter by the current user, NOT `access_token`.
    - **Correct Pattern:**
      ```python
      # Correct: No access_token for public details
      song = apis.spotify.show_song(song_id=song_id)
      
      # Correct: No access_token for public searches
      albums = apis.spotify.search_albums(query='rock')
      
      # Correct: No access_token for public reviews (use user_email instead)
      reviews = apis.spotify.show_song_reviews(song_id=song_id, user_email=user_email)
      ```

- **User-Specific / Action Endpoints (Token Required):** Endpoints that access private user data or modify user state **MUST** include the `access_token` parameter.
    - **Examples:** `show_liked_songs`, `show_playlist` (private playlists), `follow_artist`, `remove_like_songs`, `play_song`, `review_song`, `update_song_review`.
    - **Required Parameters:** `access_token` plus relevant identifiers (e.g., `artist_id`, `playlist_id`).
    - **Correct Pattern:**
      ```python
      # Correct: access_token required for user actions
      apis.spotify.follow_artist(access_token=token, artist_id=artist_id)
      
      # Correct: access_token required for write operations
      apis.spotify.review_song(access_token=token, song_id=song_id, rating=5)
      ```

- **Heuristic:** 
  1. **No Token:** If the endpoint performs a **search**, **list public entities**, or fetch **public metadata** (even if filtered by public attributes like `user_email` for reviews), it does NOT need a token.
  2. **Token Required:** If the endpoint accesses **private user state** (likes, playlists, library) or **modifies** user data (reviews, follows, plays), it REQUIRES a token.
  Always verify against `apis.api_docs.show_api_doc` if unsure.

#### Playlist Creation Conflict Handling
When creating a new playlist with a specific title, the API may return a 409 error if a playlist with that title already exists for the user. In this case, do not fail the task; instead, adapt to the existing state:
1. Search for the existing playlist using `search_playlists` with the target title.
2. Identify the correct playlist (usually the one owned by the user or matching the context).
3. Use that existing playlist's `playlist_id` for all subsequent add/remove operations.
This ensures that subsequent actions target the correct entity even if the creation step was redundant due to existing state.

#### Query-Based Search for Specific Items
When searching for a specific item (e.g., a note, playlist, or contact) using a search API, always include a relevant `query` parameter if the item's name or identifier is known. Relying on default search results (which may be sorted by recency or general relevance) often returns irrelevant items first, making it difficult to identify the correct target. A specific query ensures the target item appears prominently in the results, improving both accuracy and efficiency.

#### Cross-App Relationship Resolution
When a task requires filtering or acting on entities based on specific social relationships (e.g., "friends", "roommates", "coworkers") that are not explicitly labeled in the primary app's data (e.g., Venmo friend lists, Spotify followers), you **must** query auxiliary contact apps (e.g., Phone Contacts) to resolve these identities. **Do not rely solely on the primary app's internal friend list** (e.g., `search_friends`), as it is often incomplete or stale; the auxiliary app's contact labels are the authoritative source of truth for the user's social graph.

1. **Identify Relationship Source:** Determine which auxiliary apps store the target relationship metadata. Phone contacts often maintain specific relationship labels (e.g., "coworker", "friend") that are not present in social or payment apps.
2. **Authenticate Auxiliary App:** Log in to the auxiliary app using its specific credentials. Use `apis.supervisor.show_account_passwords()` to retrieve the correct username/password if needed.
3. **Search by Relationship:** Use the contact search API (e.g., `search_contacts`) with the specific `relationship` parameter to retrieve all contacts with that label. **Always iterate through paginated results** (starting `page_index` at 0) to ensure no contacts are missed. Agents frequently fail by assuming all relevant contacts fit on the first page.
4. **Aggregate Identity Sets:** Combine the unique identifiers (usually email addresses) from all relevant relationship searches into a single set.
5. **Apply to Primary Task:** Use this verified set of identities to filter the primary action (e.g., approving payment requests, sending messages). Only act on entities present in this verified set.

- **Auxiliary App Login Username Heuristic:** When logging into auxiliary apps (File System, Spotify, Venmo, Phone, etc.), you must correctly identify the **username value** and the **username parameter name**. Agents frequently fail with 401/422 errors by using the wrong credential or parameter key.

  - **Parameter Name:** Always use the `username` parameter key for login, **even if the credential is an email address**. Using `email` as the parameter name (e.g., for Spotify) causes authentication failures.
    - **Correct:** `apis.spotify.login(username='user@example.com', password='...')`
    - **Correct:** `apis.venmo.login(username='user@example.com', password='...')`
    - **Correct:** `apis.filesystem.login(username='user@example.com', password='...')`
    - **Incorrect:** `apis.spotify.login(email='user@example.com', password='...')`

  - **Credential Value:** If `show_account_passwords` returns only `account_name` and `password` (and no `username`), do **not** use `account_name` as the login credential. Instead, use the **user's email address** (available in the task context or via `apis.supervisor.show_profile()['email']`) as the `username` value.
    - **Correct:** `apis.filesystem.login(username='chrharrison@gmail.com', password='...')` (using email because account_name was not provided)
    - **Incorrect:** `apis.filesystem.login(username='Chris Harrison', password='...')` (using account_name)

  - **Phone Number Exception:** For Phone app login, the `username` is the raw phone number **without** the '+1' country code prefix. Including the prefix causes a 401 error.
    - **Correct:** `apis.phone.login(username='2474975253', password='...')`
    - **Incorrect:** `apis.phone.login(username='+12474975253', password='...')`

  **Example Pattern:**
  ```python
  # 1. Authenticate with the auxiliary app (Phone Contacts)
  phone_login = apis.phone.login(username=phone_number, password=phone_password)
  contact_token = phone_login['access_token']

  # 2. Search for contacts with specific relationships (handle pagination)
  friend_emails = set()
  page_index = 0
  while True:
      contacts = apis.phone.search_contacts(
          access_token=contact_token,
          relationship='friend',
          page_index=page_index,
          page_limit=20
      )
      if not contacts:
          break
      friend_emails.update(c['email'] for c in contacts)
      page_index += 1

  # 3. Filter primary task items using the verified emails
  # Do NOT use venmo.search_friends() as the sole source
  transactions = apis.venmo.show_transactions(access_token=venmo_token, direction='received')
  friend_txns = [t for t in transactions if t['sender']['email'] in friend_emails]
  ```

#### Precise Set Arithmetic for Compound Conditions
When a task requires acting on items satisfying a compound condition (e.g., 'in Library AND NOT Liked', 'in Playlist X AND NOT in Playlist Y'), you must compute the exact set of target item IDs using set arithmetic on the retrieved ID lists. Do not rely on visual inspection or partial filtering during iteration.
1. **Compute Target Set:** Retrieve the relevant ID lists (e.g., `library_ids`, `liked_ids`, `playlist_ids`). Compute the final target set using set operations:
   - `target_ids = set(library_ids) - set(liked_ids)` for 'in A AND NOT B'.
   - `target_ids = set(playlist_ids) & set(other_ids)` for 'in A AND B'.
2. **Iterate Only Over Target Set:** Iterate exclusively over items whose IDs are present in `target_ids`. 
3. **Avoid Spurious Changes:** Acting on items outside this computed set (e.g., items that are Liked, or items not in the Library) will result in incorrect state changes and task failure. Always verify that the item being acted upon is in the pre-computed `target_ids` set before calling modification APIs.

**Example Pattern:**
```python
# 1. Retrieve ID lists
library_items = apis.spotify.show_song_library()
liked_items = apis.spotify.show_liked_songs()

# 2. Compute exact target set (Library minus Liked)
library_ids = {item['id'] for item in library_items}
liked_ids = {item['id'] for item in liked_items}
target_ids = library_ids - liked_ids

# 3. Act only on items in the target set
for song in library_items:
    if song['id'] in target_ids:
        # Safe to act: song is in library AND not liked
        apis.spotify.remove_like_songs(ids=[song['id']])
```

#### Content Modification via Replace
When modifying structured text content (e.g., note bodies, CSVs) where only specific items need changing, use string `replace()` on the existing content rather than manually reconstructing the entire text. This preserves existing formatting, handles escaping correctly, and reduces the risk of typos in static parts of the content.

#### Targeted Search with Filter Parameters
When searching for messages or records associated with a specific entity (e.g., a phone number, user email, or contact ID), **always use the dedicated filter parameter** (e.g., `phone_number`, `user_email`, `contact_id`) in the search API call. Do not rely on generic `query` parameters or fetching all records and filtering manually, as unfiltered searches often return mixed results that may not include the target entity on the first page, leading to missed items if pagination is not handled perfectly. Using the specific filter ensures the API returns only relevant items, making pagination reliable and complete. For example, when deleting spam messages from a specific number, use `search_text_messages(phone_number='5708520672')` instead of searching all messages and filtering by `sender['phone_number']` in Python.
  - **Dual Message Type Deletion:** When a task requires deleting messages (e.g., spam) from a specific sender/number, you must search and delete **BOTH** text messages (`search_text_messages`) **AND** voice messages (`search_voice_messages`). Treating only one type as complete will result in leftover spam. Use the specific filter parameter (e.g., `phone_number`) in both searches to ensure completeness.

#### Identity-Based Disambiguation for Shared Names
When searching for or selecting an entity (e.g., playlist, album, file) that may have multiple matches with the same title or name, you **must** disambiguate by verifying the owner's identity. Do not assume the first match is correct, as it may belong to a different user (e.g., a public playlist with the same name as your private one). 
1. **Retrieve User Identity:** Obtain the user's email from the context (e.g., `show_profile` output or task input).
2. **Filter by Owner:** When examining search results or lists, select only the item where the `owner` field (e.g., `owner['email']` or `owner['name']`) matches the user's identity. 
3. **Example:** If searching for a playlist titled "My Playlist", verify `playlist['owner']['email'] == user_email` to ensure you are acting on the user's private playlist, not a public one with the same name.

#### Cross-Source ID Collection for Aggregation
When a task requires analyzing or aggregating data across multiple Spotify sources (song library, album library, playlist library), you must collect the union of all unique song IDs from **all** sources **before** fetching individual song details. Do not fetch details for each source independently, as this risks missing songs due to disjoint sets (songs in one source but not others) or pagination issues. The collection method varies by source:
- **Album Library:** Extract `song_ids` from the album summary objects returned by `show_album_library` or `search_albums`.
- **Playlist Library:** Use `show_playlist` to retrieve the full list of song objects for each playlist, then extract the IDs.
- **Song Library:** Use `show_song_library` (with full pagination) and extract the `song_id` from each entry.

Combine these into a single set of unique IDs. Only after this complete set is assembled should you call detail APIs (e.g., `show_song`) to retrieve attributes like `play_count` or `release_date` for aggregation or filtering.

#### Spotify Recommendations Handling
The `apis.spotify.show_recommendations` endpoint requires a specific two-phase approach due to its pagination behavior and lack of standard filtering seeds. Always consult `apis.api_docs.show_api_doc` to confirm parameter availability for any endpoint, but for recommendations specifically:
1. **Verify Filtering Capability:** The documentation for `show_recommendations` indicates it does **not** accept standard content-based filtering parameters (e.g., `seed_genres`, `seed_artists`, `target_popularity`). You cannot filter recommendations at the API level. You must fetch the full set and filter locally.
2. **Mandatory Full Pagination:** The API returns a small default page size (often 5 items), but the full set can be significantly larger (e.g., 30+ items). You **MUST** iterate through **ALL** pages to ensure data completeness. Stopping after the first page leads to incomplete results, causing missed target items in aggregation or filtering tasks (e.g., finding the most recommended artist or songs of a specific genre).
3. **Implementation Pattern**:
   - Use a `while` loop with `page_index=0` and `page_limit=20` to fetch all recommendation pages until an empty list is returned.
   - Collect all `song_id`s into a single master list.
   - Fetch details for each song using `apis.spotify.show_song(song_id=...)`.
   - Perform any required filtering (e.g., by `genre`, `release_date`) or aggregation (e.g., counting artist occurrences) on the complete set of detailed records in your code.
  
  **Consequence:** Failing to paginate fully results in missing valid items on later pages. Failing to filter locally results in incorrect task outcomes because API-level filtering is not supported for this endpoint.

### File System and Content Parsing
### File System and Content Parsing

#### Strict Text-File Filtering
When reading files to extract structured data, strictly filter for text-based extensions (`.txt`, `.csv`, `.json`, `.md`, `.log`). Skip binary files (`.pdf`, `.jpg`, `.png`, `.bin`, `.zip`, `.tar`) entirely. Attempting to parse binary content as text will result in unreadable/garbled output or parsing errors.

**Example Pattern**:
```python
# Correct approach
for file in files:
    if file.endswith(('.txt', '.csv', '.json')):
        content = read_file(file)
        # Parse content
    else:
        # Skip binary files like .pdf
        continue
```

#### Remote API vs. Local Modules
When interacting with the `file_system` app, you MUST use the app's API endpoints (e.g., `apis.file_system.show_directory`, `apis.file_system.compress_directory`) rather than local Python modules like `os` or `glob`. Local modules cannot access data stored on the remote server and will likely return empty results or errors. Always authenticate first using `apis.file_system.login` with credentials retrieved from `apis.supervisor.show_account_passwords` before attempting to read or write files.

#### Token Refresh Strategy
File system access tokens can expire quickly or become invalid between turns. If a file operation (e.g., `show_file`, `move_file`) returns a 401 Unauthorized error, re-login immediately using `apis.file_system.login` with the credentials from `apis.supervisor.show_account_passwords` and use the new token for the operation. Do not rely on a token obtained many turns ago for file modifications. Always pass the `access_token` parameter explicitly in every call.

#### Exact API Signatures and Parameters
The `file_system` app enforces strict parameter naming conventions. Using intuitive names like `path` will result in validation errors (401/404). You must use:
- `directory_path` for all directory operations: `show_directory`, `create_directory`, `directory_exists`, `delete_directory`.
- `file_path` for all file operations EXCEPT `move_file`: `show_file`, `create_file`, `update_file`, `delete_file`, `copy_file`, `file_exists`.
- `source_file_path` and `destination_file_path` for `move_file`: Using `file_path` or `destination_path` for this function will cause a 422 validation error.
- `apis.file_system.show_file()` to read file content. There is no `read_file` function; using it will cause execution failures.
Always verify the exact parameter names in the API documentation before calling these functions.

#### Tilde Path Preservation
When the task specifies a file path with a leading tilde (e.g., `~/backups/file.csv`), pass that exact string to the `file_system` API. Do not resolve the tilde to an absolute path (e.g., `/home/user/...`) or strip it. The API handles tilde expansion internally; passing an expanded absolute path will result in path mismatch errors or files being created in the wrong location.

#### Fallback Path Resolution
When accessing user-specific directories (e.g., `/home/username/`), do not assume the directory name matches the email username or any other identifier. If a direct path returns a 'not available' or 'does not exist' error, inspect the root directory (`/`) or use the tilde path (`~/`) to discover the actual directory structure. For example, if the email suggests user `ja-solomon` but the directory is `/home/james/`, use the discovered path. Always verify the actual directory names before proceeding with file operations.

#### No Pagination for Directory Listings
The `apis.file_system.show_directory` API does **not** accept `page_index` or `page_limit` parameters. Passing these will cause a validation error (e.g., `Exception: Unexpected parameter 'page_index'`). Call `show_directory` with only `directory_path` and `access_token` (and optional `substring`/`entry_type`/`recursive`). If the directory is large, the API returns the full list or uses recursion flags instead of pagination.

#### Directory Existence Check and Creation
Before writing files to a specific directory (e.g., `~/backups`), always check if the directory exists using `apis.file_system.directory_exists`. If the directory does not exist, create it using `apis.file_system.create_directory`. This prevents file creation failures in environments where target directories are not pre-created. Example:
```python
dir_check = apis.file_system.directory_exists(directory_path='~/backups', access_token=fs_token)
if not dir_check.get('exists', False):
    apis.file_system.create_directory(directory_path='~/backups', access_token=fs_token)
```

#### Specific API Response Keys and Function Parameters
Standard parameter names or intuitive keys may not apply in all cases. Be precise:
- **`show_file` Response:** The API returns the file path under the key **`'path'`**, not `'file_path'`. Extract it via `response['path']`.
- **`show_playlist` Response:** When fetching playlist details, songs are stored under the key **`'songs'`**, not `'tracks'`. Access via `playlist_detail['songs']` to avoid empty lists.
- **`compress_directory` Parameters:** This function requires specific parameter names: use **`compressed_file_path`** for the output zip/tar path (not `destination_path`) and **`delete_directory=True`** to remove the source after compression (not `source_directory_path`).

#### Directory Name Extraction from Paths
When tasks require operating on sub-directories (e.g., compressing each vacation spot folder), extract the directory name from the full path returned by `show_directory`. Use `path.rstrip('/').split('/')[-1]` to reliably get the leaf directory name regardless of trailing slashes or path variations.

#### Vacation Spot Compression and Dynamic Directory Handling
When a task requires compressing multiple sub-directories (e.g., vacation spots) into individual zip files and removing the source directories:
- **Do NOT hardcode sub-directory names**. Instead, use `apis.file_system.show_directory` on the parent directory to dynamically discover all sub-directories.
- Iterate over the discovered sub-directories. For each, extract the directory name using `sub_dir_path.rstrip('/').split('/')[-1]`.
- Call `apis.file_system.compress_directory` with `delete_directory=True`. This performs compression and deletion atomically, ensuring the directory is removed only if compression succeeds.
- Construct the destination zip path using the extracted name (e.g., `f'{parent_path}/{spot_name}.zip'`).
- **Example Pattern**:
  ```python
  # 1. Discover sub-directories dynamically
  parent_dir = '/home/user/vacations'
  listing = apis.file_system.show_directory(directory_path=parent_dir, access_token=fs_token)
  sub_dirs = [item for item in listing.get('entries', []) if item.get('type') == 'directory']

  for sub_dir in sub_dirs:
      sub_dir_path = sub_dir['path']
      # 2. Extract name dynamically
      spot_name = sub_dir_path.rstrip('/').split('/')[-1]
      # 3. Construct destination
      dest_zip = f'{parent_dir}/{spot_name}.zip'
      # 4. Atomic compress and delete
      apis.file_system.compress_directory(
          directory_path=sub_dir_path,
          compressed_file_path=dest_zip,
          delete_directory=True,
          access_token=fs_token
      )
  ```

#### Atomic Compression and Deletion
When a task requires compressing a directory and then deleting it, use the `delete_directory=True` parameter in the `apis.file_system.compress_directory` API call. This performs both operations atomically in a single call, ensuring the directory is removed only if compression succeeds and avoiding race conditions or partial states from separate calls.

### Venmo Payment Requests & Friends
### Venmo Payment Requests & Friends

**1. Locating Payment Requests**
Use `show_sent_payment_requests` with **full pagination**. Filter results locally by checking `request['receiver']['email']`. **Do NOT** pass `receiver_email` as a parameter (causes 422 error).

**2. Friendship Management**
Do **NOT** call `add_friend` unless explicitly instructed. Most operations (refund, send money) do not require friendship. Adding friends creates unintended model changes.

**3. Identifying Most Recent Request**
To find the last approved request:
1. Fetch **all** sent requests (full pagination).
2. Filter for `approved_at` is not `None`.
3. Sort by `created_at` descending.
4. Select the first item. Do not assume page 0 contains the most recent.

**4. Sending Money Back**
Use `apis.venmo.create_transaction` with `receiver_email` parameter. Ensure amount/details match original request.

**5. Transaction Direction Semantics**
- **"Received"**: Money coming **in** (`direction='received'`). Filter by `sender` field.
- **"Sent"**: Money going **out** (`direction='sent'`). Filter by `receiver` field.
- Prioritize the verb (received/sent) over prepositions (to/from).

**6. Request vs. Send Distinction**
- **Request Money**: Use `apis.venmo.create_payment_request`. Use `user_email` parameter for recipient. Creates `PaymentRequest` model.
- **Send Money**: Use `apis.venmo.create_transaction`. Use `receiver_email` parameter. Creates `Transaction` model.
- Using the wrong endpoint or parameter causes 422 errors or incorrect model types.

**7. Social Feed vs. Transaction History**
For activity "on social feed" or "from friends", use `apis.venmo.show_social_feed`. Do **not** use `show_transactions` (direct debits/credits only) or `show_sent_payment_requests`. **Full pagination** is mandatory.

**8. Pending Status Determination**
A request is pending if **both** `approved_at` and `denied_at` are `null`. Do not rely on a `status` field.

**9. Liking Transactions**
Use `apis.venmo.like_transaction(transaction_id=..., access_token=...)`. This creates a `TransactionLike` model.
- **No Deduplication**: Like **every** matching transaction, even if `like_count > 0`. Evaluation expects records for all specified IDs.

**10. Approving Payment Requests**
Use `apis.venmo.approve_payment_request(payment_request_id=..., access_token=...)`. Do not use `update_payment_request` or `create_transaction`.

**11. Transaction ID Key**
Access transaction identifiers via `txn['transaction_id']`. Do not assume `id` exists.

### Spotify Liked vs. Library Distinction
### Spotify Liked vs. Library Distinction

**1. Distinct Data Sets**
- `show_liked_albums`: Albums explicitly liked.
- `show_album_library`: Albums in user's library (owned/collected).
- `show_liked_songs`: Songs explicitly liked.
- **Intersection**: For 'songs in liked albums', fetch liked albums, then their songs. For 'liked songs in library', intersect `show_liked_songs` with `show_album_library` (by `album_id`).

**2. Independent Playlist Filtering**
- **Full Detail Required**: `show_playlist_library` only provides truncated `song_ids`. Always call `show_playlist(playlist_id=..., access_token=...)` to get the full `songs` list.
- **Fetch Song Details**: Iterate every playlist -> call `show_playlist` -> iterate songs -> fetch song details (`show_song`) -> evaluate condition -> remove/update.
- **No Library Dependency**: Evaluate playlist songs independently of the library. A song not in the library still requires evaluation for playlist filtering.

**3. Entity and Review Type Distinction**
- **Song Reviews**: Use `spotify.SongReview` models. APIs: `show_song_reviews`, `review_song`, `update_song_review`.
- **Album Reviews**: Use `spotify.AlbumReview` models. APIs: `show_album_reviews`, `review_album`, `update_album_review`.
- **Playlist Reviews**: Use `spotify.PlaylistReview` models.
- **Authentication**: All review creation/update endpoints **MUST** pass `access_token`.

**4. Compound Entity Pattern**
For "rate songs in liked albums":
1. Get liked album IDs.
2. Get songs in those albums (from library or album details).
3. Intersect with `show_liked_songs` if task implies 'liked songs'.
4. Rate the **songs** (SongReview model), not the albums.

**5. Schema Field Inconsistencies**
- **Artist ID Keys**: `show_liked_songs` uses `'id'` in artist objects. `show_following_artists` uses `'artist_id'`. Verify schema per endpoint.
- **Multi-Artist Songs**: Iterate **all** artists in a song's `artists` list to collect IDs. Do not assume single artist.
- **Playlist Song ID Key**: `show_playlist` uses `'id'` for song identifiers, not `song_id`.
- **Album Songs Key**: `show_album` returns `'songs'` (list of objects), not `song_ids`.
- **Song Library Key**: `show_song_library` uses `'song_id'` for identifiers.

**6. Playlist Data Retrieval**
Always use `apis.spotify.show_playlist` for playlist contents. Avoid `show_playlist_library` (summary only) and `show_song` (redundant for duration).

### Spotify Queue Interaction
### Spotify Queue Interaction

**1. Queue State Identification**
Do not rely on position indices. Identify the current song by ID.
1. Call `apis.spotify.show_current_song(access_token=...)` to get `song_id`.
2. Call `apis.spotify.show_song_queue(access_token=...)` to get the queue list.
3. Iterate the queue, collecting `song_id`s **up to and including** the current song's ID. Stop iteration once matched.

**2. Playlist Data Retrieval**
- Use `apis.spotify.show_playlist(playlist_id=..., access_token=...)` for full contents.
- Avoid `show_playlist_library` (summary only, truncated IDs).
- Avoid `show_song` for duration (already included in playlist response).
- **Key Name**: Playlist songs are under `'songs'` key, not `'tracks'`.

**3. Critical Playback API Rule**
- Use `apis.spotify.play_music` to play songs/albums/playlists.
- **Never** use `apis.spotify.play_song` (does not exist).

**4. Key Name Distinction (Queue vs. Playlist)**
- **Queue (`show_song_queue`)**: Song objects use `'song_id'` key.
- **Playlist (`show_playlist`)**: Song objects use `'id'` key.
- Always verify response schema. Using wrong key causes `KeyError`.

### Artist Following Management
#### Pre-Filtering Artist Searches

Before determining `target_artist_ids`, you MUST use the search API's built-in filters (e.g., `genre`, `min_follower_count`) to identify the target artists. Do **not** rely on text queries (e.g., `query='edm'`) followed by manual filtering, as this is unreliable and may return artists with incorrect genres or metadata.

- **Why**: Text search is not genre-specific. Searching for "edm" might return rock artists. Using native API parameters ensures the result set is exact before you check follow status.
- **How**: Pass specific constraints like `genre` and `min_follower_count` directly as arguments to the search function. Paginate through the search results to build your complete `target_artist_ids` set.

**Example Pattern**:
```python
# Correct: Use API filters to get exact target set
target_artists = []
page_index = 0
while True:
    # Use native filters for genre and follower count
    results = apis.spotify.search_artists(genre='EDM', min_follower_count=23, page_index=page_index, page_limit=20)
    if not results:
        break
    target_artists.extend(results)
    if len(results) < 20:
        break
    page_index += 1

# Extract IDs for the set difference calculation later
target_artist_ids = {a['artist_id'] for a in target_artists}
```

#### Check Existing Followings Before Action

When performing actions like `follow_artist` or `unfollow_artist`, you MUST verify the current following status by fetching the **complete** list of followed artists. `show_following_artists` is paginated; relying on a single page will miss artists and lead to incorrect decisions (e.g., attempting to follow an already-followed artist).

- **Why**: The API returns results in pages. If you only fetch the first page, your set of "followed artists" will be incomplete. This causes two errors:
  1. **Redundant Follows**: You may try to follow an artist who is already followed (on a later page), causing validation errors or wasted API calls.
  2. **Missed Unfollows**: You may fail to unfollow an artist who is actually followed but not on the first page.

- **How**: 
  1. Initialize an empty set for `followed_artist_ids`.
  2. Call `show_following_artists` with `page_limit=20`.
  3. **Important**: The API returns a **list** of artist objects. Each object contains an `'artist_id'` field. Iterate through this list.
  4. Iterate through all pages (using `page_index` starting at 0) until no more artists are returned.
  5. Collect all artist IDs into `followed_artist_ids`.
  6. Compute the set difference: `artists_to_follow = target_artist_ids - followed_artist_ids`.
  7. Only call `follow_artist` for IDs in `artists_to_follow`.

**Corrected Example**:
```python
# 1. Fetch ALL followed artists via pagination
followed_artist_ids = set()
page_index = 0
page_limit = 20

while True:
    # Response is a LIST of artist dicts, not a dict with 'artists' key
    response = show_following_artists(page_index=page_index, page_limit=page_limit)
    
    # Check if response is empty or None
    if not response:
        break
        
    for artist in response:  # Iterate directly over the list
        followed_artist_ids.add(artist['artist_id'])
        
    # Stop if fewer items returned than limit (last page)
    if len(response) < page_limit:
        break
        
    page_index += 1

# 2. Determine which artists to follow
target_artist_ids = {"artist_a", "artist_b", "artist_c"}
artists_to_follow = target_artist_ids - followed_artist_ids

# 3. Execute only for new follows
for artist_id in artists_to_follow:
    follow_artist(artist_id=artist_id)
```

### Context-Aware Note Parsing
When extracting information from notes (e.g., Simple Note) to reply to a message, you must **filter the content** based on the specific context of the request, not extract all available data.

1. **Parse Message**: Identify the key constraint in the incoming message (e.g., "recommendations for a movie from Quentin Tarantino" -> `director='Quentin Tarantino'`).
2. **Parse Note Structure**: If the note is structured as items grouped by an attribute (e.g., movies by director, songs by artist), parse it into a mapping (`attribute -> [items]`). Split entries by double newlines, then extract metadata fields using robust string splitting (e.g., `split(" - director:")[1].strip()`).
3. **Filter and Return**: Only select and return items that match the extracted constraint. **Do not return the entire list of items from the note.** Returning unfiltered data (e.g., movies by other directors) violates the task constraint and causes failure.

**Implementation Pattern**:
```python
# 1. Parse the note content into a mapping (e.g., attribute -> [items])
attribute_to_items = {}
for entry in note_content.split("\n\n")[1:]: # Skip header if present
    lines = entry.strip().split("\n")
    if len(lines) >= 2:
        item_name = lines[0]
        # Extract the specific metadata field (e.g., director)
        metadata_line = lines[1]
        if " - director:" in metadata_line:
            attribute_value = metadata_line.split(" - director:")[1].strip()
            if attribute_value not in attribute_to_items:
                attribute_to_items[attribute_value] = []
            attribute_to_items[attribute_value].append(item_name)

# 2. Extract the constraint from the request message
attribute_value = extract_attribute_from_message(request_message)

# 3. Filter to only the requested items
requested_items = attribute_to_items.get(attribute_value, [])
```

### Entity Identification by Label
When a task requires acting on a specific entity (e.g., alarm, song, contact) identified by a descriptive name or label (e.g., 'Go to sleep', 'Wake Up', 'Work'), you must locate it by searching the **label** field in the retrieved data. Do not infer identity from position, time, or other attributes unless the task explicitly defines the target by those properties.

1.  **Retrieve Full Dataset**: Fetch all items from the relevant list endpoint (handling pagination if necessary) to ensure you have the complete set. Missing items due to incomplete retrieval can lead to failed identification.
2.  **Search by Label**: Iterate through the items and find the one where the `label` (or equivalent name field) contains the target string specified in the task. You **must** use **case-insensitive substring matching** rather than exact string equality. Labels may have minor capitalization variations or extra whitespace.
    *   **Rule**: Compare the lowercase version of the search term against the lowercase version of the entity's label.
    *   **Logic**: `'search_term'.lower() in item['label'].lower()`.
3.  **Verify Uniqueness**: If multiple items match the label, use additional context from the task (e.g., 'enabled', 'weekdays', 'location') to disambiguate. If no item matches, check if the entity needs to be created or if the task premise is flawed.
4.  **Do Not Infer Position or Time**: Do not guess which item is the target based on it being the 'last' one, the 'earliest' one, or having a specific time. Labels are the authoritative identifier for named entities.
5.  **Selective Modification for 'All Others'**: If the task requires modifying a specific target entity and performing an action on all **other** entities (e.g., 'disable the rest'), do not blindly apply the action to every non-target ID. Instead:
    *   Iterate through the full dataset retrieved in Step 1.
    *   For each entity that is **not** the target, check its current state.
    *   Apply the action **only** if the entity is currently in the state that requires change (e.g., if disabling, only call the API if `enabled=True`).
    *   This prevents unnecessary API calls for entities that are already in the desired state.

**Example Pattern**:
```python
# Task: Find the alarm labeled 'Go to sleep'
alarms = apis.phone.show_alarms(access_token=token, page_index=0, page_limit=20)
# Handle pagination if needed to get all alarms

target_alarm = None
search_term = "go to sleep"

for alarm in alarms:
    # Case-insensitive substring matching
    if search_term.lower() in alarm['label'].lower():
        target_alarm = alarm
        break

if target_alarm:
    # Act on target_alarm['alarm_id']
    pass
else:
    # Handle case where alarm is not found
    pass

# Example for 'All Others' modification:
# for alarm in alarms:
#     if alarm['alarm_id'] == target_alarm['alarm_id']:
#         continue  # Skip the target
#     # Only disable if it is currently enabled
#     if alarm.get('enabled', True):
#         apis.phone.disable_alarm(access_token=token, alarm_id=alarm['alarm_id'])
```
