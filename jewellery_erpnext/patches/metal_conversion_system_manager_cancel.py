"""Let System Manager cancel -- and amend -- a Metal Conversion, on every site.

A conversion now saves its Stock Entry link at submit, so Frappe refuses to cancel that entry on
its own while the conversion is submitted: the conversion's own cancel cancels the entry
(``metal_conversions.cancel_conversion_stock_entries``). On kg-gk and gk no role could cancel a
Metal Conversion -- only Administrator -- so reversing one needs a role that can, and System
Manager is it (decided 29 Sep 2026).

The standard permission (``metal_conversions.json``) gives System Manager read, write, create,
submit, cancel and amend. A site that keeps Custom DocPerms for the doctype ignores the standard
rows, so the same rights are added there -- to System Manager's own rule at level 0, never to an
"only if creator" rule. Additive and idempotent: it never removes or narrows a right, and does
nothing on a site without Custom DocPerms for Metal Conversions, where the JSON applies.
"""

import frappe

DOCTYPE = "Metal Conversions"
ROLE = "System Manager"
RIGHTS = ("read", "write", "create", "submit", "cancel", "amend")


def execute():
	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype
	from frappe.permissions import add_permission

	if not frappe.db.exists("Custom DocPerm", {"parent": DOCTYPE}):
		return

	# frappe.permissions.update_permission_property finds its rule without if_owner, so it
	# could widen an "only if creator" rule instead; the rule is named here.
	rule = {"parent": DOCTYPE, "role": ROLE, "permlevel": 0, "if_owner": 0}
	if not frappe.db.exists("Custom DocPerm", rule):
		add_permission(DOCTYPE, ROLE, 0)
	perm = frappe.get_doc("Custom DocPerm", frappe.db.get_value("Custom DocPerm", rule))
	missing = {right: 1 for right in RIGHTS if not perm.get(right)}
	if missing:
		perm.update(missing)
		perm.save(ignore_permissions=True)
	validate_permissions_for_doctype(DOCTYPE)
	frappe.clear_cache(doctype=DOCTYPE)
