"""Seed the Product Certification partial-receipt ledger on existing documents.

``receive_status`` / ``received_weight`` / ``pending_weight`` are new derived columns.
Their DocType default ("Not Received", 0) is wrong for the ~960 submitted Issue entries
already on site: nearly all of them have been fully received, and leaving them at the
default would put "Create Receiving" back on documents that are closed and let a second
receipt book stock that already came back.

Nothing is authored here — ``update_receive_status`` recomputes each Issue from the
Receive documents actually submitted against it, which is the same code path submit and
cancel use. That makes the patch idempotent and re-runnable::

    bench --site <site> execute jewellery_erpnext.patches.backfill_product_certification_receive_status.execute

Issues with no receipts at all still land on "Not Received", so the pass is total rather
than incremental — no document is left with a NULL status.
"""

import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.receive_status import (
	update_receive_status,
)


def execute():
	if not frappe.db.has_column("Product Certification", "receive_status"):
		# DocType sync has not run yet (fresh install marks patches complete without
		# running them); nothing to backfill.
		return

	issues = frappe.get_all(
		"Product Certification",
		filters={"type": "Issue", "docstatus": ["<", 2]},
		pluck="name",
		order_by="creation asc",
	)

	for index, name in enumerate(issues, start=1):
		try:
			update_receive_status(name)
		except Exception:
			frappe.log_error(
				title=f"Receive status backfill failed for {name}",
				message=frappe.get_traceback(),
			)

		if index % 200 == 0:
			frappe.db.commit()

	frappe.db.commit()
