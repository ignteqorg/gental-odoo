"""Curated business tools, one module per area; each tool class is added with ``@curated.register``."""
from . import contacts
from . import sales
from . import invoicing
from . import inventory
from . import pos

from . import crm  # noqa: E402,F401
