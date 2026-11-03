"""Validation of domains sent by assistants (spec §6.2): allowed fields and operators only, no paths."""
from odoo import _

from ..core.tools import ToolError

OPERATORS = frozenset({
    "=", "!=", ">", ">=", "<", "<=", "in", "not in", "like", "not like", "ilike", "not ilike",
    "=like", "=ilike", "child_of", "parent_of",
})
LOGICAL = frozenset({"&", "|", "!"})
# Operators that resolve string values by name on the target records: integers only.
HIERARCHY_OPERATORS = frozenset({"child_of", "parent_of"})
# On relational fields any other operator (even with a number) becomes a name search on the target.
ID_OPERATORS = frozenset({"=", "!=", "in", "not in"}) | HIERARCHY_OPERATORS
SCALARS = (str, int, float, bool, type(None))


def validate(domain, allowed, env=None, relational=frozenset()) -> None:
    """Raise ``ToolError`` unless ``domain`` is a well-formed domain over ``allowed`` field names.

    Leaves are 3-element lists ``[field, operator, value]``; ``field`` is a plain name from ``allowed``
    (no dotted paths: a path could reach fields hidden on related models); ``operator`` comes from
    ``OPERATORS``; ``value`` is a JSON scalar or a list of scalars. Fields in ``relational`` and the
    ``child_of`` / ``parent_of`` operators take record ids only (an int, a list of ints, or false) and
    relational fields only ``ID_OPERATORS``: anything else is a name search on the target model, which
    can match fields hidden from assistants.
    ``env`` only picks the message language.

    Callers pass only stored fields (and ``id``) as ``allowed``: searching a computed field such as
    ``display_name`` runs its search method, which may filter on hidden fields.
    """
    context = {"lang": (env.lang if env is not None else None) or "en_US"}  # noqa: F841 - read by _()
    if not isinstance(domain, list):
        raise ToolError(_("The domain must be a list."))
    operands = 0  # Polish notation, checked from the end
    for item in reversed(domain):
        if isinstance(item, str):
            if item not in LOGICAL:
                raise ToolError(_("Unknown domain operator: %s", item))
            arity = 1 if item == "!" else 2
            if operands < arity:
                raise ToolError(_("The domain is malformed: “%s” is missing operands.", item))
            operands -= arity - 1
            continue
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise ToolError(_("Each domain condition must be [field, operator, value]: %s", item))
        field, operator, value = item
        if not isinstance(field, str) or not field:
            raise ToolError(_("A domain field must be a field name: %s", field))
        if "." in field:
            raise ToolError(_("Field paths are not allowed in domains: %s", field))
        if field not in allowed:
            raise ToolError(_("Unknown or hidden field in the domain: %s", field))
        if not isinstance(operator, str) or operator not in OPERATORS:
            raise ToolError(_("Operator not allowed in domains: %s", operator))
        if not _is_value(value):
            raise ToolError(_("A domain value must be a scalar or a list of scalars (field %s).", field))
        if field in relational and operator not in ID_OPERATORS:
            raise ToolError(_("The field %(field)s only takes the operators %(ops)s.",
                              field=field, ops=", ".join(sorted(ID_OPERATORS))))
        if (field in relational or operator in HIERARCHY_OPERATORS) and not _is_ids(value):
            raise ToolError(_("The field %s takes record ids here (a number, a list of numbers or false).", field))
        operands += 1


def _is_value(value):
    if isinstance(value, SCALARS):
        return True
    return isinstance(value, (list, tuple)) and all(isinstance(v, SCALARS) for v in value)


def _is_id(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_ids(value):
    if value is False or value is None or _is_id(value):
        return True
    return isinstance(value, (list, tuple)) and all(_is_id(v) for v in value)
