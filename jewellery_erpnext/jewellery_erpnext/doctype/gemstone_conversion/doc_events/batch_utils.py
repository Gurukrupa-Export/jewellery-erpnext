import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	get_auto_batch_nos,
)
from frappe import _


def update_fifo_batch(self):
	if (
		self.batch
		and self.g_source_qty
		and get_batch_qty(self.batch, self.source_warehouse) < self.g_source_qty
	):
		frappe.msgprint(_("Selected batch does not have sufficient qty for transaction"))
	else:
		batch_data = get_auto_batch_nos(
			frappe._dict(
				{
					"posting_date": self.date,
					"item_code": self.g_source_item,
					"warehouse": self.source_warehouse,
					"qty": self.g_source_qty,
				}
			)
		)

		if not batch_data:
			frappe.throw(_("No batch available for given warehouse"))
		batch_data = batch_data[0]

		if self.g_source_qty > batch_data.qty:
			frappe.msgprint(
				_("{0} missing for transaction in Batch {1}").format(
					(self.g_source_qty - batch_data.qty), batch_data.batch_no
				)
			)

		self.batch = batch_data.batch_no
	
	if self.batch:
		bal_qty, supplier, customer, inventory_type = self.get_batch_detail()
		self.batch_avail_qty = bal_qty
		self.supplier = supplier
		self.customer = customer
		self.inventory_type = inventory_type
