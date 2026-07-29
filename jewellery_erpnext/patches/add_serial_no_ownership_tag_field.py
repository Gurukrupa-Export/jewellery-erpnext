"""Provision ``Serial No.custom_ownership_tag`` — the ownership/source marker.

A finished piece carries no indication of *whose material it was made from*. The
information exists one hop away: the Manufacture Stock Entry auto-created at Serial
Number Creator submit stamps ``inventory_type`` ("Regular Stock" / "Customer Goods")
on every consumed row. This field lifts that onto the Serial No itself so the piece
can be identified and filtered by ownership:

- ``Outright`` — every consumed row is Regular Stock (our own material)
- ``Outwork``  — every consumed row is Customer Goods (customer-supplied material)
- ``Hybrid``   — both appear in the same job

It can also carry an early default copied straight from the source Sales Order's
``sales_type`` (see ``create_manufacturing_entry`` in manufacturing_operation.py),
overwritten by the ledger-derived value above when one is derivable. ``Sales Type``
is a free-form master (Customers can carry several via Sales Type Multiselect, e.g.
"Finished Goods" for ready-made-piece buyers) — not limited to Outright/Outwork/
Hybrid — so this field is ``Data``, not ``Select``: a fixed option list would reject
any Sales Type value outside that trio (as happened with "Finished Goods").

The field is ``read_only`` — it must never be hand-edited; it only ever gets written
by ``create_manufacturing_entry``. It is forward-only: serials minted before this
field existed stay blank, and blank is a legitimate state.

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
run ad-hoc (REQUIRED to pick up the Select -> Data fieldtype change on a site where
this patch already ran, since patches.txt entries only run once per site otherwise)::

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
				"fieldtype": "Data",
				"label": "Ownership Tag",
				"insert_after": "customer",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"in_list_view": 1,
				"in_standard_filter": 1,
				"description": (
					"Outright = own material, Outwork = customer material, Hybrid = both "
					"(derived from the material consumed by the Manufacture Stock Entry "
					"created at Serial Number Creator submit); may also briefly hold the "
					"source Sales Order's Sales Type as an early default, e.g. "
					"'Finished Goods'."
				),
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_serial_no_ownership_tag_field: ensured Serial No.custom_ownership_tag (Data)"
	)
