# Vendored from WebArena - https://github.com/web-arena-x/webarena (browser_env/utils.py)
# Upstream ref: main @ 2026-07-06 | License: Apache-2.0 (see ./LICENSE, ./VENDOR_NOTES.md)
# Trimmed for skills_evolve: only the typedefs used by the text-obs path; DetachedPage,
# the png-to-array helper, StateInfo, and the array/image imports were removed
# (details in VENDOR_NOTES.md). Removal-only surgery; surviving types byte-verbatim
# EXCEPT the Observation alias reduced to `str`.
from typing import Any, TypedDict


class AccessibilityTreeNode(TypedDict):
    nodeId: str
    ignored: bool
    role: dict[str, Any]
    chromeRole: dict[str, Any]
    name: dict[str, Any]
    properties: list[dict[str, Any]]
    childIds: list[str]
    parentId: str
    backendDOMNodeId: str
    frameId: str
    bound: list[float] | None
    union_bound: list[float] | None
    offsetrect_bound: list[float] | None


class DOMNode(TypedDict):
    nodeId: str
    nodeType: str
    nodeName: str
    nodeValue: str
    attributes: str
    backendNodeId: str
    parentId: str
    childIds: list[str]
    cursor: int
    union_bound: list[float] | None


class BrowserConfig(TypedDict):
    win_top_bound: float
    win_left_bound: float
    win_width: float
    win_height: float
    win_right_bound: float
    win_lower_bound: float
    device_pixel_ratio: float


class BrowserInfo(TypedDict):
    DOMTree: dict[str, Any]
    config: BrowserConfig


AccessibilityTree = list[AccessibilityTreeNode]
DOMTree = list[DOMNode]


Observation = str
