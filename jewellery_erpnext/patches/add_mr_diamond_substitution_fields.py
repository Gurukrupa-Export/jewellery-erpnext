"""Provision the customer-diamond substitution fields on ``Material Request`` and its role (F11).

On a customer-diamond order, a Material Request for a diamond grade other than the ordered one
is refused at submit unless a Diamond Substitution Approver records a reason
(``doc_events.material_request.validate_customer_diamond_grade``). These fields hold that reason
and the approver.

Wired in two idempotent places per the app convention (``after_migrate`` is disabled for
custom fields): this ``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be
run ad hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_mr_diamond_substitution_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

APPROVER_ROLE = "Diamond Substitution Approver"


def execute():
	if not frappe.db.exists("Role", APPROVER_ROLE):
		frappe.get_doc(
			{"doctype": "Role", "role_name": APPROVER_ROLE, "desk_access": 1}
		).insert(ignore_permissions=True)

	create_custom_fields(
		{
			"Material Request": [
				{
					"fieldname": "custom_diamond_substitution_reason",
					"fieldtype": "Small Text",
					"label": "Diamond Substitution Reason",
					"insert_after": "manufacturing_order",
					"module": "Jewellery Erpnext",
					"no_copy": 1,
					"allow_on_submit": 0,
					"description": (
						"Required to submit a customer-diamond order's request for a grade other than "
						"the one ordered. Only a Diamond Substitution Approver can submit it."
					),
				},
				{
					"fieldname": "custom_diamond_substitution_by",
					"fieldtype": "Link",
					"options": "User",
					"label": "Diamond Substitution Approved By",
					"insert_after": "custom_diamond_substitution_reason",
					"module": "Jewellery Erpnext",
					"read_only": 1,
					"no_copy": 1,
				},
			]
		},
		ignore_validate=True,
	)
