"""Task environments adapted into the CSS TaskEnv protocol.

``TaskEnv`` (the mechanism<->environment contract) lives in :mod:`css.envs.base`
and is re-exported here. Concrete environments (SpreadsheetBench, Bird, ...) are
NOT imported at package level — import them from their own subpackages so this
package stays cheap and free of heavy/optional dependencies.
"""
from css.envs.base import TaskEnv

__all__ = ["TaskEnv"]
