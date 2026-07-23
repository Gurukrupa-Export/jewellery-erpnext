"""Provision ``Serial No.custom_ownership_tag`` — the Outright/Outwork/Hybrid marker.

A finished piece carries no indication of *whose material it was made from*. The
information exists one hop away: the Manufacture Stock Entry auto-created at Serial
Number Creator submit stamps ``inventory_type`` ("Regular Stock" / "Customer Goods")
on every consumed row. This field lifts that onto the Serial No itself so the piece
can be identified and filtered by ownership:

- ``Outright`` — every consumed row is Regular Stock (our own material)
- ``Outwork``  — every consumed row is Customer Goods (customer-supplied material)
- ``Hybrid``   — both appear in the same job

The value is derived and written by ``_derive_ownership_tag`` /
``create_manufacturing_entry`` (manufacturing_operation.py) right after the Manufacture
SE is submitted, alongside the sibling ``custom_product_type`` / ``custom_gross_wt`` /
``custom_repair_type`` stamps. The field is therefore ``read_only`` — it must never
drift from the ledger it is derived from. It is forward-only: serials minted before
this field existed stay blank, and blank is a legitimate state (hence the empty first
Select option).

``in_standard_filter`` / ``in_list_view`` are the whole point of the feature — no app
overrides the Serial No list view, so a Custom Field flag is the only lever that puts
a filter on that list.

``insert_after`` deliberately anchors on the STANDARD ``customer`` field rather than one
of the twenty gke_customization Serial No fields, so the layout does not depend on
another app's fixtures being installed.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete
WITHOUT running them on fresh / CI sites, a fixture-only column would never reach
the DB and ``frappe.db.set_value("Serial No", ..., "custom_ownership_tag", ...)`` would
raise ``1054 Unknown column``. Per the app convention this is wired in two idempotent
places: this ``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be
run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_serial_no_ownership_tag_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Serial No": [
			{
				"fieldname": "custom_ownership_tag",
				"fieldtype": "Select",
				"label": "Ownership Tag",
				"options": "\nOutright\nOutwork\nHybrid",
				"insert_after": "customer",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"in_list_view": 1,
				"in_standard_filter": 1,
				"description": (
					"Outright = own material, Outwork = customer material, Hybrid = both. "
					"Derived from the material consumed by the Manufacture Stock Entry "
					"created at Serial Number Creator submit."
				),
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_serial_no_ownership_tag_field: ensured Serial No.custom_ownership_tag"
	)
