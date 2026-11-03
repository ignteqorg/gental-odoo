"""Contacts curated tools (area ``contacts``, always available): find a contact, create or complete one."""
import unicodedata

from odoo.tools.translate import _lt

from .. import compat
from ..core.tools import ToolError
from ..services import curated, identity, result_meta
from ..services.action_keys import once_per_key
from .common import like_escape, odoo_errors, result, text

MAX_LIMIT = 20
MIN_PHONE_DIGITS = 6
# Filled on a matched contact only when empty (company_name: gone in 20.0, offered where it exists). Email
# (where invoices go) and phone need a verified customer.
FILLABLE = ("company_name", "street", "city")

FOUND = _lt("Contacts found: %s")
NONE_FOUND = _lt("No contact matches this search.")
QUERY_TOO_SHORT = _lt("Type at least 2 characters to search.")
NAME_REQUIRED = _lt("The contact needs a name.")
BAD_PHONE = _lt("This phone number isn't valid. Send it with the country code, for example +966 5X XXX XXXX.")
BAD_EMAIL = _lt("This email address isn't valid.")
CREATED = _lt("New contact created: %s")
MATCHED = _lt("This customer is already saved, so no new contact was created and nothing was overwritten.")
NEEDS_REVIEW = _lt("Staff need to check this phone or email, so nothing was saved. Carry on with the customer; "
                   "staff will follow up.")
REASON_SEVERAL = _lt("Several contacts share this phone or email: %s")
REASON_NAME = _lt("A contact matched by phone or email, but the name differs: “%(sent)s” vs “%(saved)s”.")


def _digits(value: str) -> str:
    return "".join(str(unicodedata.decimal(ch)) for ch in value if ch.isdecimal())


def _first_phone(partner):
    return next((partner[f] for f in compat.partner_phone_fields(partner.env) if partner[f]), "") or ""


def _contact(partner) -> dict:
    return {
        "id": partner.id,
        "name": partner.name or "",
        "phone": _first_phone(partner),
        "email": partner.email or "",
        "ref": partner.ref or "",
        "company": _company_of(partner),
    }


def _company_of(partner) -> str:
    """The company ``partner`` belongs to; empty for a company (Odoo's ``is_company``, computed from the VAT in
    20.0) and for a contact on its own, which keeps only 17-19's typed ``company_name``. 20.0 relates
    ``commercial_company_name`` to the commercial partner's name, which is the contact's own name there."""
    if partner.is_company:
        return ""
    if partner.commercial_partner_id == partner:
        return (partner["company_name"] or "") if "company_name" in partner._fields else ""
    return partner.commercial_company_name or ""


def _summary_line(contact: dict) -> str:
    return " · ".join(v for v in (f"{contact['name']} (#{contact['id']})", contact["phone"], contact["email"]) if v)


def _phone_matches(env, query):
    """Partners whose number is exactly the phone in ``query`` (normalised), from capped candidates."""
    if len(_digits(query)) < MIN_PHONE_DIGITS or not compat.partner_phone_fields(env):
        return env["res.partner"]
    phone = identity.normalise_phone(query, env.company.country_id.phone_code)
    candidates = identity.phone_candidates(env, phone) if phone else None
    return identity.same_phone(candidates, phone) if candidates else env["res.partner"]


@curated.register
class FindContact(curated.CuratedTool):
    name = "find_contact"
    area = "contacts"
    kind = "read"
    title_en = "Find a contact"
    title_ar = "البحث عن جهة اتصال"
    description = (
        "Finds customers and other contacts in Odoo by name, phone number (any format), email or internal "
        "reference. Returns id, name, phone, email, reference and company."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2, "maxLength": 200,
                          "description": "Name, phone, email or reference."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"res.partner": ["name", "phone", curated.AppField("mobile", dropped_in=19), "email", "ref",
                                "commercial_company_name", "is_company", "commercial_partner_id",
                                curated.AppField("company_name", dropped_in=20), "country_id"],
                "res.country": ["phone_code"]}

    def run(self, args, ctx):
        query = args["query"].strip()
        if len(query) < 2:
            raise ToolError(text(ctx, QUERY_TOO_SHORT))
        limit = args.get("limit") or 10
        domain = ["|", "|", ("name", "ilike", query), ("email", "ilike", query), ("ref", "ilike", query)]
        with odoo_errors(ctx):
            partners = ctx.env["res.partner"].search(domain, limit=limit)
            partners = (partners | _phone_matches(ctx.env, query))[:limit]
            contacts = [_contact(p) for p in partners]
        if not contacts:
            return result(text(ctx, NONE_FOUND), {"contacts": []})
        summary = text(ctx, FOUND, "\n" + "\n".join(map(_summary_line, contacts)))
        return result(summary, {"contacts": contacts})


@curated.register
class UpsertContact(curated.CuratedTool):
    name = "upsert_contact"
    area = "contacts"
    kind = "write"
    idempotent = True
    title_en = "Create or update a contact"
    title_ar = "إضافة جهة اتصال أو تحديثها"
    description = (
        "Saves a customer without creating duplicates: matches an existing contact by phone number first, "
        "then by email. One match with the same name: status matched; empty company and address fields are "
        "filled, nothing is overwritten, and the email is added only for the verified customer. Another name "
        "or several matches: nothing is saved and staff are flagged (status needs_review). No match: creates "
        "the contact (with only a name, it always creates one). Returns the status (created, matched or "
        "needs_review) and, for created or matched, the contact id."
    )

    def input_schema(self, ctx):
        text_field = {"type": "string", "maxLength": 200}
        properties = {
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "phone": {"type": "string", "maxLength": 40, "description": "Phone number, ideally with country code."},
            "email": {"type": "string", "maxLength": 200},
            "company_name": dict(text_field, description="The customer's company, if any."),
            "street": text_field,
            "city": text_field,
        }
        if "company_name" not in ctx.env["res.partner"]._fields:
            del properties["company_name"]
        return {"type": "object", "properties": properties, "required": ["name"], "additionalProperties": False}

    def required_fields(self):
        return {"res.partner": ["name", "phone", curated.AppField("mobile", dropped_in=19), "email",
                                curated.AppField("company_name", dropped_in=20), "street", "city", "country_id"],
                "res.country": ["phone_code"]}

    def run(self, args, ctx):
        env = ctx.env
        name = args["name"].strip()
        if not name:
            raise ToolError(text(ctx, NAME_REQUIRED))
        fields = env["res.partner"]._fields
        values = {f: (args.get(f) or "").strip() for f in ("email",) + FILLABLE if f in fields}
        values = {f: v for f, v in values.items() if v}
        phone = None
        if (args.get("phone") or "").strip():
            phone = identity.normalise_phone(args["phone"], env.company.country_id.phone_code)
            if not phone:
                raise ToolError(text(ctx, BAD_PHONE))
        if "email" in values and "@" not in values["email"]:
            raise ToolError(text(ctx, BAD_EMAIL))

        with odoo_errors(ctx):
            matches = self._matches(env, phone, values.get("email"))
            if matches is None or len(matches) > 1:
                listed = ", ".join(f"{p.name or ''} (#{p.id})" for p in (matches or env["res.partner"])) or "-"
                return self._needs_review(ctx, text(ctx, REASON_SEVERAL, listed))
            if matches:
                return self._complete(ctx, matches, name, phone, values)
            return self._create(ctx, name, phone, values)

    @staticmethod
    def _needs_review(ctx, reason):
        """Nothing saved: no other contact's name or id goes to the model, only the staff reason (``_meta``)."""
        res = result(text(ctx, NEEDS_REVIEW), {"status": "needs_review"})
        return result_meta.staff_review(res, reason)

    def _matches(self, env, phone, email):
        """Contacts with this phone (normalised), else with this email (case-insensitive); ``None`` when too
        many contacts share the number to tell."""
        Partner = env["res.partner"]
        if phone and compat.partner_phone_fields(env):
            candidates = identity.phone_candidates(env, phone)
            if candidates is None:
                return None
            found = identity.same_phone(candidates, phone)
            if found:
                return found
        if email:
            found = Partner.search([("email", "=ilike", like_escape(email))], limit=identity.MAX_CANDIDATES + 1)
            return None if len(found) > identity.MAX_CANDIDATES else found
        return Partner

    def _complete(self, ctx, partner, name, phone, values):
        record = ("res.partner", partner.id)
        if " ".join(name.split()).casefold() != " ".join((partner.name or "").split()).casefold():
            # Unverified chat data never lands on someone else's contact, and their saved name isn't revealed.
            return self._needs_review(ctx, text(ctx, REASON_NAME, sent=name, saved=partner.name or ""))
        updates = {f: v for f, v in values.items() if f in FILLABLE and not partner[f]}
        # An email decides where this customer's invoices go: only the verified customer's own number, saved on
        # this contact, may add one. A phone is never added to an existing contact.
        if values.get("email") and not partner.email and _verified_owner(ctx, partner):
            updates["email"] = values["email"]
        if updates:
            partner.write(updates)
        return result(text(ctx, MATCHED), {"id": partner.id, "status": "matched"}, record=record)

    def _create(self, ctx, name, phone, values):
        create_values = dict(values, name=name)
        phone_fields = compat.partner_phone_fields(ctx.env)
        if phone and phone_fields:
            create_values[phone_fields[0]] = phone
        # A retried action (same Gental action key) gets the contact its first attempt created.
        partner, _created = once_per_key(ctx, "res.partner", lambda: ctx.env["res.partner"].create(create_values))
        return result(text(ctx, CREATED, partner.name), {"id": partner.id, "status": "created"},
                      record=("res.partner", partner.id))


def _verified_owner(ctx, partner) -> bool:
    """Whether this call carries a verified customer identity whose phone is one of ``partner``'s numbers."""
    who = identity.current(ctx)
    phone = identity.normalise_phone(who.phone, None) if who else None
    return bool(phone and identity.same_phone(partner, phone))
