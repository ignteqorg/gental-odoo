# Refuse to load before anything else on an Odoo build without api.private (compat.UNSUPPORTED_BUILD).
from . import compat  # noqa: F401  (import order matters)
from . import controllers
from . import models
from . import services
from . import tools
from . import wizards
