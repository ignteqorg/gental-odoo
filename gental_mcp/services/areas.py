"""Curated tool areas and the Odoo apps they need, detected at runtime (no hard ``depends``)."""
from typing import Dict, FrozenSet, Set, Tuple

AREAS: Dict[str, Tuple[str, ...]] = {
    "contacts": ("base",),
    "sales": ("sale",),
    "invoicing": ("account",),
    "crm": ("crm",),
    "inventory": ("stock",),
    "pos": ("point_of_sale",),
}

# db name -> (registry signature, installed areas). A module install or removal resets the registry,
# which changes its signature, so the cache never outlives a change of installed apps.
_CACHE: Dict[str, Tuple[tuple, FrozenSet[str]]] = {}


def installed_areas(env) -> Set[str]:
    """Areas whose Odoo apps are all installed (or installed and pending upgrade) in ``env``'s database."""
    registry = env.registry
    signature = (id(registry), getattr(registry, "registry_sequence", None))
    cached = _CACHE.get(registry.db_name)
    if cached is not None and cached[0] == signature:
        return set(cached[1])
    modules = sorted({m for needed in AREAS.values() for m in needed})
    # Module metadata, not business data: plain SQL keeps it readable for every acting user.
    env.cr.execute(
        "SELECT name FROM ir_module_module WHERE state IN ('installed', 'to upgrade') AND name IN %s", (tuple(modules),)
    )
    installed = {row[0] for row in env.cr.fetchall()}
    found = frozenset(area for area, needed in AREAS.items() if installed.issuperset(needed))
    _CACHE[registry.db_name] = (signature, found)
    return set(found)
