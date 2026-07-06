# WebArena `browser_env` core — vendored (trimmed)

## Provenance
- **Source:** https://github.com/web-arena-x/webarena — `browser_env/`
- **Upstream ref:** `main @ 2026-07-06`
- **License:** Apache-2.0 — full text in [`./LICENSE`](./LICENSE). Each vendored file
  carries a provenance header pointing back to its upstream path.
- **Method:** removal-only surgery. Surviving code is byte-verbatim from upstream
  except the small, explicitly-listed deviations below. No surviving logic was
  rewritten or reformatted.

## Why vendored / what this is
We need only the **text (accessibility-tree) observation** and the **synchronous,
id-based action** path of WebArena's `browser_env`. Everything else (screenshot /
Set-of-Marks image observations, the async Playwright path, `gymnasium` action/observation
spaces, `beartype` runtime checks, and the coordinate/low-level action creators) was
dropped so the only third-party dependency is **Playwright** (plus the Python stdlib).

## Files
| File | Kept | Dropped |
|---|---|---|
| `processors.py` | `ObservationProcessor`, `ObservationMetadata`, `create_empty_metadata`, **`TextObervationProcessor`** (all methods incl. `fetch_browser_info`, `fetch_page_accessibility_tree`, `parse_accessibility_tree`, `clean_accesibility_tree`, `parse_html`, `get_element_center`) | `ImageObservationProcessor`, `ObservationHandler`, `get_observation_space`, `numpy`/`PIL`/`gymnasium` imports |
| `actions.py` | `Action`, `ActionTypes`, `ActionParsingError`, `ParsedPlaywrightCode`, **`create_id_based_action`** + the create-helpers it calls, `create_none_action`, **`execute_action`** (sync) + its sync execution helpers (`execute_scroll/key_press/mouse_click/mouse_hover/type/focus/click_current`, `locate`, `execute_playwright_*`, `parse_playwright_code`), `is_in_viewport`, **`action2str`**, id/role lookup tables | every `async def a*` variant, `aexecute_action`, `is_equivalent`, `action2create_function`, `get_action_space`, `create_random_action`, `create_mouse_*`, `create_keyboard_type_action`, `create_check_action`, `create_select_option_action`, `create_focus*`, `create_playwright_action`; `numpy`/`beartype`/`gymnasium`/`playwright.async_api` imports |
| `utils.py` | `AccessibilityTreeNode`, `DOMNode`, `BrowserConfig`, `BrowserInfo`, `AccessibilityTree`, `DOMTree`, `Observation` | `DetachedPage`, `png_bytes_to_numpy`, `StateInfo`, `numpy`/`PIL` imports |
| `constants.py` | `ROLES`, `SPECIAL_LOCATORS`, `ASCII_CHARSET`, `FREQ_UNICODE_CHARSET`, `SPECIAL_KEYS`, `SPECIAL_KEY_MAPPINGS`, `RolesType`, `PLAYWRIGHT_LOCATORS`, `PLAYWRIGHT_ACTIONS`, `IGNORED_ACTREE_PROPERTIES` | viewport / coordinate / answer-size numerics (`WINDOW_*`, `TASK_*`, `FLIGHT_*`, `*_MAX_LENGTH`, `MAX_ELEMENT_*`, `MAX_PAGE_NUMBER`, `MIN_REF`/`MAX_REF`, `MAX_VANILLA_STR_LENGTH`) — none imported by the survivors |
| `env_config.py` | **NOT vendored** (hardcodes upstream site URLs/accounts) — see "Config the caller must supply" |

Note the upstream **spelling quirks** you must use verbatim: class `TextObervation­Processor`
(no "s"), methods `parse_accessibility_tree` / `clean_accesibility_tree`, and the
`execute_action` parameter name `obseration_processor`.

## Deviations from byte-verbatim (all forced by removing `numpy`)
1. `actions.py` `Action.coords`: annotation `npt.NDArray[np.float32]` → `list[float]`.
2. `actions.py` `create_none_action`: `"coords": np.zeros(2, dtype=np.float32)` → `"coords": [0.0, 0.0]`.
3. `utils.py` `Observation = str | npt.NDArray[np.uint8]` → `Observation = str`.

These are runtime-equivalent for the surviving code (coords is only ever read as
`coords[0]`/`coords[1]`; the numpy branch of `Observation` was the image path we dropped).

**Pre-existing upstream lint, preserved verbatim (NOT introduced here):** `processors.py`
has unused `json` and `typing.Union` imports and three `except Exception as e:` with unused
`e`; `actions.py` keeps upstream's redundant double-import of `ObservationProcessor` (and an
unused `TextObervationProcessor` import). Left as-is under removal-only discipline.

---

## PUBLIC API (what the adapter should call)

Import from the package (`from css.envs.webarena.vendor import ...`). Import pulls in
`playwright.sync_api`, so import only where Playwright is installed.

### 1. Text (accessibility-tree) observation
```python
TextObervationProcessor(
    observation_type: str,        # "accessibility_tree" (numbered AXTree) | "html" (numbered DOM)
    current_viewport_only: bool,  # True = keep only in-viewport nodes (need scroll to reach others)
    viewport_size: ViewportSize,  # a dict {"width": int, "height": int}
)
```
- `proc.process(page: Page, client: CDPSession) -> str`
  Returns a tab-title header line + a blank line + the AXTree text, e.g.:
  ```
  Tab 0 (current): One Stop Market
  <blank line>
  [1] RootWebArea 'One Stop Market' ...
  	[12] link 'Sign In' ...
  	[27] textbox 'Search' required: False ...
  ```
  The bracketed integers (`[12]`, `[27]`, …) **are the element_ids** — feed them back
  verbatim into the action strings below.
  **Side effects:** populates `proc.obs_nodes_info` and `proc.meta_data["obs_nodes_info"]`,
  a `dict[str_id -> {"backend_id", "union_bound": [x, y, w, h] | None, "text"}]`, which
  `execute_action` reads for id-based clicks. Call `process()` to refresh it **before**
  each id-based action.
- `proc.get_element_center(element_id: str) -> tuple[float, float]`
  Viewport-normalized (0..1) center of the element, from the last `process()`'s
  `obs_nodes_info`. Raises `KeyError` if the id isn't in the current observation and
  `TypeError` if its `union_bound` is `None` (off-screen element).

**CDP plumbing (required):** `client` must be a Chromium CDP session bound to the page:
`client = page.context.new_cdp_session(page)`. `process()` issues CDP calls
(`DOMSnapshot.captureSnapshot`, `Accessibility.getFullAXTree`, `DOM.resolveNode`,
`Runtime.callFunctionOn`) and **asserts `window.devicePixelRatio == 1.0`** — launch the
context with `device_scale_factor=1` (no HiDPI) and a viewport equal to the one you pass
to the processor.

### 2. Parse a model action string → `Action`
```python
create_id_based_action(action_str: str) -> Action     # raises ActionParsingError on bad input
```
The parser takes a **bare** action string (no markdown fences / prose — upstream stripped
those in a separate agent step that we did NOT vendor; the adapter must extract the bare
string first). The action keyword is the text before the first `[` (or the first
whitespace token if there's no `[`). Accepted grammar (exact — regex in parens):

| Action string | Notes |
|---|---|
| `click [id]` | `id` = digits (`click ?\[(\d+)\]`) |
| `hover [id]` | digits |
| `type [id] [text] [flag]` | `flag` ∈ `0\|1`; **optional** — if the string doesn't end with `[0]`/`[1]`, ` [1]` is appended; `flag==1` appends `"\n"` (submit). Regex `type ?\[(\d+)\] ?\[(.+)\] ?\[(\d+)\]` (greedy on `text`) |
| `press [key_comb]` | e.g. `press [Enter]`, `press [Meta+a]`; combos split on `+` and mapped via `SPECIAL_KEY_MAPPINGS` |
| `scroll [up]` / `scroll [down]` | brackets optional: `scroll up` also parses (`scroll ?\[?(up\|down)\]?`) |
| `goto [url]` | `goto ?\[(.+)\]` |
| `new_tab` | bare |
| `go_back` | bare |
| `go_forward` | bare |
| `tab_focus [index]` | digits — switches active tab |
| `close_tab` | bare |
| `stop [answer]` | `answer` **optional** → `""` if absent (`stop ?\[(.+)\]`) |

`ActionTypes` (IntEnum) values referenced by the id-based path: `CLICK, TYPE, HOVER,
KEY_PRESS, SCROLL, GOTO_URL, NEW_TAB, GO_BACK, GO_FORWARD, PAGE_FOCUS, PAGE_CLOSE, STOP,
NONE`. Detect episode end with `action["action_type"] == ActionTypes.STOP` (answer in
`action["answer"]`).

### 3. Render an `Action` back to a string (for logging/trajectories)
```python
action2str(action: Action, action_set_tag: str, semantic_element: str = "") -> str
```
`action_set_tag` **must** be `"id_accessibility_tree"` (the only surviving branch; anything
else raises `NotImplementedError`). E.g. a click renders as
`"click [12] where [12] is <semantic_element>"`.

### 4. Execute an `Action` against the live page
```python
execute_action(
    action: Action,
    page: Page,                              # current active sync Page
    browser_ctx: BrowserContext,             # == page.context
    obseration_processor: ObservationProcessor,  # pass the SAME TextObervationProcessor
) -> Page                                    # returns the (possibly NEW) active page — adopt it!
```
- For `CLICK`/`HOVER`/`TYPE` with an `element_id`, it calls
  `obseration_processor.get_element_center(id)` and issues a **mouse click/hover at that
  viewport coordinate**. So (a) pass the processor whose `process()` produced the current
  observation, and (b) `page.viewport_size` must equal `proc.viewport_size`.
- `NEW_TAB` opens a page and sets `page.client = ctx.new_cdp_session(page)`;
  `PAGE_FOCUS`/`PAGE_CLOSE`/`NEW_TAB` change which page is active — **the returned `page`
  is authoritative**; use it (and a fresh `ctx.new_cdp_session(page)`) for the next step.
- May raise `ValueError` / `NotImplementedError` / `KeyError` / `TypeError` on
  unlocatable elements; the adapter should catch and treat as a failed step.

## Config the caller must supply (env_config.py was NOT vendored)
The vendored survivors reference **no** `os.environ` variables and **no** site URLs.
Upstream's `env_config.py` (dropped, because it hardcodes their infra) defined:
- Site base-URL env vars: `REDDIT`, `SHOPPING`, `SHOPPING_ADMIN`, `GITLAB`, `WIKIPEDIA`,
  `MAP`, `HOMEPAGE`.
- `ACCOUNTS` (per-site login creds) and `URL_MAPPINGS` (real→canonical URL rewrites).

Our config layer must supply the site base URL(s) to the **task/agent layer** (which emits
`goto [url]`) itself — the vendored code neither reads env vars nor needs these constants.

## End-to-end usage (sync Playwright)
```python
from playwright.sync_api import sync_playwright
from css.envs.webarena.vendor import (
    TextObervationProcessor, create_id_based_action, execute_action, ActionTypes,
)

VIEWPORT = {"width": 1280, "height": 720}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
    page = ctx.new_page()
    page.goto("http://<your-site>/")                 # base URL supplied by your config layer

    proc = TextObervationProcessor("accessibility_tree", current_viewport_only=True,
                                   viewport_size=VIEWPORT)

    for _ in range(max_steps):
        client = ctx.new_cdp_session(page)           # fresh CDP session for the active page
        obs = proc.process(page, client)             # str; fills proc.obs_nodes_info
        model_str = agent(obs)                        # -> e.g. "click [12]" (bare string)
        action = create_id_based_action(model_str)
        if action["action_type"] == ActionTypes.STOP:
            answer = action["answer"]; break
        page = execute_action(action, page, ctx, proc)   # adopt the returned page
    browser.close()
```

## Validation performed
- `python3 -m py_compile` on all 5 vendored `.py` files → **OK**.
- `grep -E "beartype|gymnasium|PIL|numpy|async_api"` over the vendored `*.py` → **nothing**
  (these words appear only in this notes file, describing what was removed).
- Import surface grep → only `playwright.*` + stdlib (`ast, json, re, enum, itertools,
  typing, collections`) + relative `.` imports; no `browser_env.*` absolute imports remain.
- `pyflakes` → **no undefined names** (only the pre-existing upstream unused-import/`e`
  lint noted above).
- Functional smoke test (stubbed Playwright): 20/20 checks — full `create_id_based_action`
  grammar, `action2str`, numpy-free `create_none_action` coords, and `TextObervationProcessor`
  construction all pass.
