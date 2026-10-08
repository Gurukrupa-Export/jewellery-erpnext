"""Provision the design-tolerance override fields on ``Serial Number Creator`` and its role (F6).

At SNC submit, a finished piece outside its PMO's product tolerance is refused unless a
Product Tolerance Approver records a reason (``product_tolerance.validate_snc_design_tolerance``).
These fields hold that reason and the approver. The check is armed per department by
``Department.custom_apply_product_tolerance``; with it off nothing reads these fields.

Wired in two idempotent places per the app convention (``after_migrate`` is disabled): this
``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run ad hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_snc_tolerance_override_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

APPROVER_ROLE = "Product Tolerance Approver"


def execute():
	if not frappe.db.exists("Role", APPROVER_ROLE):
		frappe.get_doc(
			{"doctype": "Role", "role_name": APPROVER_ROLE, "desk_access": 1}
		).insert(ignore_permissions=True)

	create_custom_fields(
		{
			"Serial Number Creator": [
				{
					"fieldname": "custom_tolerance_override_reason",
					"fieldtype": "Small Text",
					"label": "Tolerance Override Reason",
					"insert_after": "total_weight",
					"module": "Jewellery Erpnext",
					"no_copy": 1,
					"description": (
						"Required to submit a piece that differs from its design beyond the product "
						"tolerance. Only a Product Tolerance Approver can submit it."
					),
				},
				{
					"fieldname": "custom_tolerance_override_by",
					"fieldtype": "Link",
					"options": "User",
					"label": "Tolerance Override Approved By",
					"insert_after": "custom_tolerance_override_reason",
					"module": "Jewellery Erpnext",
					"read_only": 1,
					"no_copy": 1,
				},
			]
		},
		ignore_validate=True,
	)
