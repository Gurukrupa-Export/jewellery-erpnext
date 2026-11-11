"""Let Process Transfer User read Manufacturing Work Order on sites with Custom DocPerm.

The DocType JSON gives Process Transfer User read on Manufacturing Work Order, but a site whose
permissions for that DocType live in Custom DocPerm (Role Permission Manager) ignores the JSON
rows: kggk-uat had 23 such rows. Without read, scanning work orders into a Process Transfer reports
every one as "not found". Fresh sites (no Custom DocPerm) need nothing.

Idempotent: adds the row only when Custom DocPerm rows exist and none is for this role.
"""

import frappe
from frappe.permissions import add_permission

DOCTYPE = "Manufacturing Work Order"
ROLE = "Process Transfer User"


def execute():
	if not frappe.db.exists("Role", ROLE):
		return
	if not frappe.db.exists("Custom DocPerm", {"parent": DOCTYPE}):
		return
	if frappe.db.exists(
		"Custom DocPerm", {"parent": DOCTYPE, "role": ROLE, "permlevel": 0}
	):
		return
	add_permission(DOCTYPE, ROLE)
	frappe.clear_cache(doctype=DOCTYPE)
