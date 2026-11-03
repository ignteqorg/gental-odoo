"""Tool provider registry: later plans append their provider factories with ``register_provider``."""
from typing import Callable, List

from odoo.modules.module import get_manifest

from ..core.protocol import McpServer
from . import curated, generic_tools

PROVIDERS: List[Callable] = []


def register_provider(factory: Callable) -> Callable:
    """Add ``factory(env) -> ToolProvider`` to the registry (idempotent); usable as a decorator."""
    if factory not in PROVIDERS:
        PROVIDERS.append(factory)
    return factory


def unregister_provider(factory: Callable) -> None:
    if factory in PROVIDERS:
        PROVIDERS.remove(factory)


def server_version() -> str:
    return get_manifest("gental_mcp").get("version", "")


def build_server(env) -> McpServer:
    """An ``McpServer`` over the providers built for ``env`` (the acting user's environment)."""
    return McpServer([factory(env) for factory in PROVIDERS], server_version())


register_provider(generic_tools.GenericToolProvider)
register_provider(curated.CuratedToolProvider)
