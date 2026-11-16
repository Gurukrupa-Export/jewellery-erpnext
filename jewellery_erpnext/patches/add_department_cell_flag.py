"""Add Department.custom_is_cell_department (used by the WO transfer process check).

Idempotent. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_department_cell_flag.execute
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Department": [
				{
					"fieldname": "custom_is_cell_department",
					"fieldtype": "Check",
					"label": "Is Cell Department",
					"insert_after": "custom_is_chain_subcontracting",
					"description": "Transfers by users of this department are checked against the Manufacturing Operation's process.",
				}
			]
		}
	)
