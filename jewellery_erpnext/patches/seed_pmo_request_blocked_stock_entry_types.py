"""Fill MOP Settings > Stock Entry Types Blocked During a Request on first install.

While a Parent Manufacturing Order has a Close or Hold request, no raw material may be
transferred to its work orders (doc_events/close_open.py refuse_stock_entry_while_blocked).
These are the types that issue raw material to a work order or its department. Only types
that exist on the site are added, and a table someone already filled is left alone.
"""

import frappe

SETTINGS = "MOP Settings"
FIELD = "pmo_request_blocked_stock_entry_types"
DEFAULT_TYPES = (
	"Material Transfer (WORK ORDER)",
	"Material Transfer From Reserve",
	"Material Transfer (DEPARTMENT)",
)


def execute():
	if not frappe.get_meta(SETTINGS).has_field(FIELD):
		return
	settings = frappe.get_single(SETTINGS)
	if settings.get(FIELD):
		return
	types = [t for t in DEFAULT_TYPES if frappe.db.exists("Stock Entry Type", t)]
	if not types:
		return
	for stock_entry_type in types:
		settings.append(FIELD, {"stock_entry_type": stock_entry_type})
	settings.flags.ignore_permissions = True
	settings.flags.ignore_mandatory = True
	settings.save()
