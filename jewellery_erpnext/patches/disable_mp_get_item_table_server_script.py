"""Disable the "GK Manufacturing Plan (Get Item Table)" Server Script.

Its "Select atleast one Item" check now lives in ``ManufacturingPlan.before_insert``, which
exempts Finding Manufacturing - Without SO plans. The script is a database row and runs after the
controller, so while it stays enabled it still refuses every Without-SO plan on sites that carry
its old condition. ``create_test_data`` calls this for the CI ``test_site``, which loads the script
from the git_action_v16 fixtures.

Idempotent, and only disables: the record is kept so a site can inspect or re-enable it.
"""

import frappe

SCRIPT_NAME = "GK Manufacturing Plan (Get Item Table)"


def execute():
	if not frappe.db.exists("Server Script", SCRIPT_NAME):
		return
	doc = frappe.get_doc("Server Script", SCRIPT_NAME)
	if doc.disabled:
		return
	# A save, not db.set_value, so the server-script map cache is cleared with it.
	doc.disabled = 1
	doc.flags.ignore_permissions = True
	doc.save()
