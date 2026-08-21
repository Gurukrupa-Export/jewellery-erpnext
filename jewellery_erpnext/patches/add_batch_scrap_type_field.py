"""Provision the ``Batch.custom_batch_type`` marker used by Unused/Loose Material Refining.

Material left unused by production is received back into the department (the "Receive
Unused/Loose Material" Manufacturing Operation action) and repacked onto a dedicated
unused/loose item on a freshly created batch tagged
``custom_batch_type = "Unused/Loose Material"``. Rows with no such target (diamonds,
gemstones, alloys) keep their own item code and rely on the tag alone, which is why the tag
— not the item code — is the marker. Unused/Loose Material Refining fetches ONLY those batches
(``RefiningEntry.get_scrap_items_balance``), so ordinary department stock sharing the
warehouse is never pulled into a refining entry.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete WITHOUT
running them on fresh / CI sites, a fixture-only column would never reach the DB and the
assignment would raise ``1054 Unknown column``. Per the app convention this is wired in two
idempotent places: this ``post_model_sync`` patch and ``create_test_data.setup_data``. Can
also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_batch_scrap_type_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)`` and updates in place, so
re-running refreshes the ``options`` string. The value was renamed from "Scrap" —
``rename_refining_scrap_terminology_metadata`` re-invokes this patch and the data swap
migrates existing rows.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.refining.constants import BATCH_TYPE_UNUSED


def execute():
	custom_fields = {
		"Batch": [
			{
				"fieldname": "custom_batch_type",
				"fieldtype": "Select",
				# Blank (ordinary stock) or the unused/loose material marker.
				"options": "\n" + BATCH_TYPE_UNUSED,
				"label": "Batch Type",
				"insert_after": "item_name",
				"read_only": 1,
				"no_copy": 1,
				"in_standard_filter": 1,
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info("add_batch_scrap_type_field: ensured Batch.custom_batch_type")
