"""Ensure ``Stock Entry Detail.custom_conversion_lane`` exists.

A Metal Conversion now draws FIFO across every ownership in the source warehouse
and splits the result into per-``(inventory_type, customer)`` lanes, emitting one
Stock Entry that carries all of them. Two pieces of downstream machinery need to
know which lane a row belongs to:

* ``customer_subcontracting/batch_rename.py::create_child_batches`` -- so the
  Regular lane's target row is left for the Serial-and-Batch path instead of
  being minted as the customer's, and so each customer lane inherits the parent
  batch of its OWN lane rather than the voucher's first source row.
* ``customization/serial_and_batch_bundle/doc_events/utils.py::update_parent_batch_id``
  -- so each target batch's ``custom_origin_entries`` (and therefore the
  qty-weighted Batch Rate blended by ``customization/batch/batch.py::on_update``)
  only sees its own lane's sources.

The lane cannot be *inferred* for every row: alloy consume rows are booked
"Regular Stock" yet legitimately fund a customer lane, and no per-lane alloy
proportion exists anywhere else. So the builder stamps the lane explicitly and
these consumers key off the tag.

Why a patch and not ``custom_fields/stock_entry_detail.json``: this app's
``after_migrate`` hook is disabled (hooks.py), so its ``custom_fields/*.json`` are
never applied by ``bench migrate`` -- the recurring patch-only custom-field gap
documented in ``fetch_from_guard``. Per the app convention this is wired in two
idempotent places: this ``post_model_sync`` patch and ``create_test_data.setup_data``.
Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_conversion_lane_tag_field.execute

Idempotent: guarded on ``frappe.db.has_column``.
"""

import frappe

FIELD = {
	"fieldname": "custom_conversion_lane",
	"label": "Conversion Lane",
	"fieldtype": "Data",
	"insert_after": "inventory_type",
	"read_only": 1,
	"hidden": 1,
	"no_copy": 1,
	"print_hide": 1,
	"translatable": 0,
	"description": (
		"Ownership lane this row belongs to, as "
		"'<inventory type>|<customer>'. Stamped by Metal Conversions so child-batch "
		"minting and Batch Rate origin entries stay scoped to one lane."
	),
}


def execute():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	if frappe.db.has_column("Stock Entry Detail", FIELD["fieldname"]):
		return

	create_custom_fields({"Stock Entry Detail": [FIELD]}, ignore_validate=True)
	frappe.db.commit()
	frappe.logger().info(
		"add_conversion_lane_tag_field: created Stock Entry Detail.custom_conversion_lane"
	)
