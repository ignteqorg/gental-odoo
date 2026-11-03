"""Gental ``_meta`` markers on tool results (spec §7.4)."""
from ..core import meta
from ..core.tools import ToolResult

OUTCOME_UNKNOWN = "unknown"
STAFF_REVIEW = "staff_review"


def outcome_unknown(result: ToolResult) -> ToolResult:
    """Mark that a write may have happened but the module can't prove it."""
    result.meta[meta.OUTCOME] = OUTCOME_UNKNOWN
    return result


def staff_review(result: ToolResult, reason: str) -> ToolResult:
    """Flag the result for staff attention with a short ``reason``."""
    result.meta[meta.FLAG] = STAFF_REVIEW
    result.meta[meta.FLAG_REASON] = reason
    return result
