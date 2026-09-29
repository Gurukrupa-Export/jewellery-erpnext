"""Provision the Customer Gold rate CHECK fields on ``Stock Entry``, and the approver role (F1).

``KGJPL-SE-CGR-26-00011`` booked 10 g of customer gold at Rs.1,57,655 per gram when the company
was paying Rs.15,504.85 per gram for the same item (``PR-26-00171``). The feed quotes per 10 g on
some days and per gram on others, so no single ``gold_rate_unit`` can be right, and nothing
compared the frozen rate with anything. The receipt now checks it against an independent
reference and refuses an outlier unless a Customer Gold Rate Approver records why it is right.

These fields hold that evidence beside the existing snapshot
(``add_customer_gold_rate_snapshot_fields``): the currency and grams-per-quoted-unit the rate was
normalised with, the reference it was compared against, the ratio, and -- when an outlier was
accepted -- the reason and the approver. All but the reason are machine-owned and read-only;
all are ``no_copy`` so an amendment is checked afresh.

Wired in two idempotent places per the app convention (``after_migrate`` is disabled): this
``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run ad hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_customer_gold_rate_check_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

RATE_PRECISION = "4"
APPROVER_ROLE = "Customer Gold Rate Approver"


def execute():
	if not frappe.db.exists("Role", APPROVER_ROLE):
		frappe.get_doc(
			{"doctype": "Role", "role_name": APPROVER_ROLE, "desk_access": 1}
		).insert(ignore_permissions=True)

	custom_fields = {
		"Stock Entry": [
			{
				"fieldname": "custom_gold_rate_currency",
				"fieldtype": "Link",
				"options": "Currency",
				"label": "Gold Rate Currency",
				"insert_after": "custom_gold_rate_per_gram",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "custom_gold_rate_factor",
				"fieldtype": "Float",
				"label": "Grams per Quoted Unit",
				"insert_after": "custom_gold_rate_currency",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"description": "Raw rate / this = per-gram rate. 1 for Per Gram, 10 for Per 10 Gram.",
			},
			{
				"fieldname": "custom_gold_rate_check_section",
				"fieldtype": "Section Break",
				"label": "Customer Gold Rate Check",
				"insert_after": "custom_gold_rate_factor",
				"module": "Jewellery Erpnext",
				"collapsible": 1,
				"depends_on": "eval:doc.custom_gold_rate_per_gram",
			},
			{
				"fieldname": "custom_gold_rate_check_reference",
				"fieldtype": "Currency",
				"label": "Reference Rate Per Gram",
				"insert_after": "custom_gold_rate_check_section",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"precision": RATE_PRECISION,
				"description": "Independent rate the frozen per-gram rate was compared with.",
			},
			{
				"fieldname": "custom_gold_rate_check_source",
				"fieldtype": "Data",
				"label": "Reference Rate Source",
				"insert_after": "custom_gold_rate_check_reference",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "custom_gold_rate_check_ratio",
				"fieldtype": "Float",
				"label": "Rate / Reference",
				"insert_after": "custom_gold_rate_check_source",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"precision": RATE_PRECISION,
			},
			{
				"fieldname": "custom_gold_rate_check_column_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_gold_rate_check_ratio",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_gold_rate_override_reason",
				"fieldtype": "Small Text",
				"label": "Rate Override Reason",
				"insert_after": "custom_gold_rate_check_column_break",
				"module": "Jewellery Erpnext",
				"no_copy": 1,
				"description": (
					"Required to submit a receipt whose rate is outside the accepted range of its "
					"reference. Only a Customer Gold Rate Approver can submit it."
				),
			},
			{
				"fieldname": "custom_gold_rate_override_by",
				"fieldtype": "Link",
				"options": "User",
				"label": "Rate Override Approved By",
				"insert_after": "custom_gold_rate_override_reason",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_customer_gold_rate_check_fields: ensured Customer Gold rate check fields and role"
	)
