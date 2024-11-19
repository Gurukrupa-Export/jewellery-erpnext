import copy

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils import (
	get_fifo_batches,
)


def update_batch_details(self):
	rows_to_append = []
	self.flags.only_regular_stock_allowed = True

	if self.doctype == "Diamond Conversion":
		child_table = self.sc_source_table
	else:
		child_table = self.mc_source_table

	for row in child_table:
		warehouse = row.get("s_warehouse") or self.get("source_warehouse")
		if row.get("batch") and get_batch_qty(row.batch, warehouse) >= row.qty:
			temp_row = copy.deepcopy(row)
			temp_row.batch_no = temp_row.batch
			rows_to_append += [temp_row]
		else:
			rows_to_append += get_fifo_batches(self, row)

	if rows_to_append:
		if self.doctype == "Diamond Conversion":
			self.sc_source_table = []
		else:
			self.mc_source_table = []

	for item in rows_to_append:
		if isinstance(item, dict):
			item = frappe._dict(item)
		item.name = None
		if item.batch_no:
			item.batch = item.batch_no
		batch = item.batch_no or item.batch
		if batch:
			item.inventory_type = frappe.db.get_value("Batch", batch, "custom_inventory_type")
			item.customer = frappe.db.get_value("Batch", batch, "custom_customer")
		if self.doctype == "Diamond Conversion":
			self.append("sc_source_table", item)
		else:
			self.append("mc_source_table", item)
