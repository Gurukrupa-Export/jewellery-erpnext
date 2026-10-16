# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Finish Operation for Finding Manufacturing - Without SO work orders.

Transfers whatever is still left on the work order's current Manufacturing Operation to the
department's Raw Material warehouse, then marks that operation Finished.

The remaining quantity and the transfer are the Manufacturing Operation's own "Make Receive
Entry" machinery, used unchanged and for every row at once:

* ``get_make_receive_entry_rows`` -- per (item, batch), what is still reserved for the work order
  (reserved - delivered), capped by the operation's MOP Log balance. That is "the quantity
  available against the work order after what was already consumed / transferred".
* ``create_mr_wo_stock_entry`` -- one ``Material Receive (WORK ORDER)`` Stock Entry (purpose
  Material Transfer) from the department Manufacturing warehouse to the department Raw Material
  warehouse, releasing or re-creating each Stock Reservation Entry and bridging MOP Log rows.

Reusing it is what keeps the reservations and the operation's weights consistent; a hand-built
transfer would leave the moved stock still reserved for the work order.
"""

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	create_mr_wo_stock_entry,
	get_make_receive_entry_rows,
)


@frappe.whitelist()
def finish_without_so_operation(mwo):
	doc = frappe.get_doc("Manufacturing Work Order", mwo)
	doc.check_permission("write")

	if not doc.is_without_so:
		frappe.throw(
			_(
				"Finish Operation applies only to Finding Manufacturing - Without SO work orders."
			)
		)
	if doc.docstatus != 1:
		frappe.throw(
			_("Submit the Manufacturing Work Order before finishing its operation.")
		)
	if not doc.manufacturing_operation:
		frappe.throw(
			_(
				"Manufacturing Work Order {0} has no current Manufacturing Operation."
			).format(mwo)
		)

	mop = frappe.db.get_value(
		"Manufacturing Operation",
		doc.manufacturing_operation,
		["name", "status", "department", "employee"],
		as_dict=True,
	)
	if mop.status == "WIP":
		# Issued to an employee: the material is theirs until the Employee IR Receive brings it
		# back, so finishing here would move stock out from under an open issue.
		frappe.throw(
			_(
				"Operation {0} is issued to {1}. Receive it through Employee IR before finishing."
			).format(mop.name, mop.employee or _("an employee"))
		)
	if mop.status == "Finished":
		frappe.throw(_("Operation {0} is already Finished.").format(mop.name))

	result = get_make_receive_entry_rows(mop.name)
	rows = [
		row
		for row in (result or {}).get("rows") or []
		if flt(row.get("available_to_receive_qty")) > 0
	]
	receive_items = [
		{
			"idx": idx,
			"stock_reservation_entry": row.get("stock_reservation_entry"),
			"stock_reservation_entry_detail": row.get("stock_reservation_entry_detail"),
			"item_code": row.get("item_code"),
			"s_warehouse": row.get("s_warehouse"),
			"qty": flt(row.get("available_to_receive_qty")),
			"pcs": flt(row.get("available_to_receive_pcs"))
			if row.get("is_pcs_item")
			else 0,
			"batch_no": row.get("batch_no"),
		}
		for idx, row in enumerate(rows, start=1)
	]

	stock_entry = None
	if receive_items:
		created = create_mr_wo_stock_entry(
			{
				"manufacturing_work_order": doc.name,
				"manufacturing_operation": mop.name,
				"manufacturing_order": doc.manufacturing_order,
				"department": mop.department,
				"receive_items": receive_items,
			},
			request_id=f"finish-operation-{mop.name}",
		)
		stock_entry = (created or {}).get("docname")
		if stock_entry:
			doc.db_set("final_transfer_entry", stock_entry)

	frappe.db.set_value("Manufacturing Operation", mop.name, "status", "Finished")

	if stock_entry:
		frappe.msgprint(
			_(
				"Remaining material transferred to the department Raw Material warehouse by {0}."
			).format(frappe.utils.get_link_to_form("Stock Entry", stock_entry))
		)
	else:
		frappe.msgprint(
			_("No remaining material on {0}; the operation is marked Finished.").format(
				mop.name
			)
		)

	return {"stock_entry": stock_entry, "manufacturing_operation": mop.name}
