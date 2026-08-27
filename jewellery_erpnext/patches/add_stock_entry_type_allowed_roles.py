"""Provision ``Stock Entry Type.custom_allowed_roles`` -- the per-type Role whitelist.

The field is a Table MultiSelect over frappe core's ``Has Role`` child table, reused
deliberately so this feature mints no DocType of its own. It is safe to reuse:
``frappe.get_roles()`` filters on ``parenttype = "User"``, so rows parented to a Stock
Entry Type can never leak into anybody's actual role list.

The rule is a **strict whitelist**: a type is visible and usable only to the roles listed
on it, and a type with no rows is visible to nobody but Administrator / System Manager.
Grant frappe's built-in ``All`` role to keep a type open to everyone. Enforcement lives in
``jewellery_erpnext/doc_events/stock_entry_type.py``: a ``permission_query_conditions``
hook that filters the dropdown, and a Stock Entry ``validate`` handler that blocks the
API bypass.

Because the rule is strict, the grants must live in ``fixtures/stock_entry_type.json`` --
the fixture import deletes and re-inserts every record it names, so a grant made only in
the desk is wiped by the next migrate, and a wiped grant locks the type for everyone.

Because ``after_migrate`` is disabled (``hooks.py:12``) and ``install-app`` marks patches
complete WITHOUT running them on fresh / CI sites, a ``custom_fields/`` declaration alone
would never reach the DB. Per the app convention this is wired in two idempotent places:
this ``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run
ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_stock_entry_type_allowed_roles.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Stock Entry Type": [
			{
				"fieldname": "custom_allowed_roles",
				"fieldtype": "Table MultiSelect",
				"label": "Allowed Roles",
				"options": "Has Role",
				"insert_after": "add_to_transit",
				"module": "Jewellery Erpnext",
				"description": (
					"Only these roles can see and use this Stock Entry Type. Leave empty "
					"and nobody can - grant the All role to keep it open to everyone. "
					"Administrator and System Manager always bypass."
				),
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_stock_entry_type_allowed_roles: ensured Stock Entry Type.custom_allowed_roles"
	)
