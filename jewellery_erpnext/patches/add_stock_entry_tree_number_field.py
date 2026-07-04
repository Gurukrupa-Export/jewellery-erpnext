"""Provision the ``Stock Entry.custom_tree_number`` back-link custom field.

The Tree Number "Issue Material" / "Receive Material" buttons create plain
``Material Transfer`` Stock Entries and stamp ``custom_tree_number`` on each so the
movement is traceable from the tree and can be reversed
(``tree_stock_entry.cancel_tree_stock_entries`` filters on it).

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete WITHOUT
running them on fresh / CI sites, a fixture-only column would never reach the DB and
``se.custom_tree_number = ...`` would raise ``1054 Unknown column``. Per the app convention
(schema fixes go through a guard called from both a patch AND setup_data, not a fixture),
this is wired in two idempotent places: this ``post_model_sync`` patch (existing-site
migrate) and ``create_test_data.setup_data`` (fresh / CI sites). Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_stock_entry_tree_number_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``, so it is a no-op once
the column exists.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Stock Entry": [
			{
				"fieldname": "custom_tree_number",
				"fieldtype": "Link",
				"options": "Tree Number",
				"label": "Tree Number",
				"insert_after": "auto_created",
				"read_only": 1,
				"no_copy": 1,
				"print_hide": 1,
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_stock_entry_tree_number_field: ensured Stock Entry.custom_tree_number"
	)
