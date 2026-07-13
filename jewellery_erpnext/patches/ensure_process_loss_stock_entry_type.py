"""Ensure the "Process Loss" Stock Entry Type master exists.

Employee IR's loss engine (``employee_ir/doc_events/loss_stock_entry.py``), Main
Slip's ``create_process_loss``, and the Metal Conversions melting-loss flow
(``metal_conversions/doc_events/melting_loss.py``) all build Stock Entries of type
"Process Loss" (purpose Repack). This master exists on production only because it
was created manually -- it is absent from ``fixtures/stock_entry_type.json`` (which
spells a different string, "Metal Conversion Repack"), from every other patch, and
from ``create_test_data``.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete
WITHOUT running them on fresh / CI sites, ``se.insert()`` would raise a
LinkValidationError on the missing Stock Entry Type. Per the app convention this is
wired in two idempotent places: this ``post_model_sync`` patch and
``create_test_data.setup_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.ensure_process_loss_stock_entry_type.execute

Idempotent: guarded on ``frappe.db.exists``.
"""

import frappe


def execute():
	if not frappe.db.exists("Stock Entry Type", "Process Loss"):
		frappe.get_doc(
			{
				"doctype": "Stock Entry Type",
				"name": "Process Loss",
				"purpose": "Repack",
			}
		).insert(ignore_permissions=True)
		frappe.logger().info(
			"ensure_process_loss_stock_entry_type: created Stock Entry Type 'Process Loss'"
		)
