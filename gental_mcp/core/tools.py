"""Tool contracts: specs, results, errors and the provider protocol."""
from dataclasses import dataclass, field
from typing import Optional, Protocol, Tuple

from .context import CallContext


@dataclass
class ToolAnnotations:
    read_only: bool
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False

    def to_mcp(self) -> dict:
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }


@dataclass
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict
    annotations: ToolAnnotations
    output_schema: Optional[dict] = None
    # Tool ``_meta`` (e.g. Gental's ``ai.gental/audience``); omitted when empty.
    meta: Optional[dict] = None

    def to_mcp(self) -> dict:
        out = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations.to_mcp(),
        }
        if self.output_schema is not None:
            out["outputSchema"] = self.output_schema
        if self.meta:
            out["_meta"] = self.meta
        return out


@dataclass
class ToolResult:
    text: str
    structured: Optional[dict] = None
    is_error: bool = False
    meta: dict = field(default_factory=dict)
    resource_links: list = field(default_factory=list)
    # (model, id) for logging only; never serialized.
    record: Optional[Tuple[str, int]] = None

    def to_mcp(self) -> dict:
        out = {
            "content": [{"type": "text", "text": self.text}, *self.resource_links],
            "isError": self.is_error,
        }
        if self.structured is not None:
            out["structuredContent"] = self.structured
        if self.meta:
            out["_meta"] = self.meta
        return out


class ToolError(Exception):
    """Message is shown to the model as an `isError` result."""


class ToolDenied(ToolError):
    """A ToolError that is logged as denied."""


class ToolOutcomeUnknown(Exception):
    """The write may have happened but can't be confirmed: reported as a non-error result whose
    ``_meta`` carries ``ai.gental/outcome = "unknown"`` (spec §7.4)."""


class ToolProvider(Protocol):
    def list_tools(self, ctx: CallContext) -> list: ...

    def call(self, name: str, args: dict, ctx: CallContext) -> ToolResult: ...
