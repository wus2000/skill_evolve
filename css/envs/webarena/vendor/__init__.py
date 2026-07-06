# Vendored WebArena browser_env core: text-only accessibility-tree observation +
# id-based (sync) action path. See VENDOR_NOTES.md for provenance, the exact trimmed
# public API, the action-string grammar, and an end-to-end usage snippet.
#
# Source: https://github.com/web-arena-x/webarena (browser_env/), main @ 2026-07-06.
# License: Apache-2.0 (see ./LICENSE). Trimmed by removal-only surgery.
#
# NOTE: importing this package pulls in `playwright.sync_api` (a hard dependency of
# the surviving code); import it only where playwright is installed.
from .actions import (
    Action,
    ActionParsingError,
    ActionTypes,
    action2str,
    create_id_based_action,
    execute_action,
)
from .processors import (
    ObservationMetadata,
    ObservationProcessor,
    TextObervationProcessor,
    create_empty_metadata,
)

__all__ = [
    "Action",
    "ActionParsingError",
    "ActionTypes",
    "action2str",
    "create_id_based_action",
    "execute_action",
    "ObservationMetadata",
    "ObservationProcessor",
    "TextObervationProcessor",
    "create_empty_metadata",
]
