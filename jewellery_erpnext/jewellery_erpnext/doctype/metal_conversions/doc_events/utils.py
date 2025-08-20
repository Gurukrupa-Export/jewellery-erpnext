import copy

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	get_auto_batch_nos,
)
from frappe import _
from frappe.utils import flt

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
			if not item.inventory_type:
				item.inventory_type = frappe.db.get_value("Batch", batch, "custom_inventory_type") 
			item.customer = frappe.db.get_value("Batch", batch, "custom_customer")
		if self.doctype == "Diamond Conversion":
			self.append("sc_source_table", item)
		else:
			self.append("mc_source_table", item)


def update_alloy_betch(self):
	if flt(self.source_alloy_qty) <=0:
		return
	if not self.source_alloy and flt(self.source_alloy_qty)>0:
		frappe.throw(_("Please Select The Alloy"))
	if (
		self.source_alloy_batch
		and self.source_alloy_qty
		and get_batch_qty(self.source_alloy_batch, self.source_warehouse) < flt(self.source_alloy_qty, 3)
	):
		frappe.msgprint(_("Selected batch does not have sufficient qty for transaction"))
	else:
		batch_data = get_auto_batch_nos(
			frappe._dict(
				{
					"posting_date": self.date,
					"item_code": self.source_alloy,
					"warehouse": self.source_warehouse,
					"qty": self.source_alloy_qty,
				}
			)
		)

		if not batch_data:
			frappe.throw(_("No batch available for given warehouse"))
		self.alloy_batch_details = []
		# batch_data = batch_data[0] 
		if batch_data:
			remaining_qty = 0
			total_qty = 0
			for i in batch_data:
				qty = 0
				if flt(self.source_alloy_qty) > i.qty:
					qty = i.qty
					remaining_qty += i.qty
					total_qty += qty
				else:
					qty = flt(self.source_alloy_qty) - remaining_qty
					total_qty += qty
				self.append("alloy_batch_details",{
					'qty':qty,
					'batch':i.batch_no
				})
			if total_qty != flt(self.source_alloy_qty):
				frappe.throw(_("The source quantity is not available for the given warehouse. The available quantity is {}.".format(total_qty)))
		# if flt(self.source_alloy_qty) > batch_data.qty:
		# 	frappe.msgprint(
		# 		_("{0} missing for transaction in Batch {1}").format(
		# 			(self.source_alloy_qty - batch_data.qty), batch_data.batch_no
		# 		)
		# 	)

		# self.source_alloy_batch = batch_data.batch_no

def update_source_betch(self):
	batch_data = get_auto_batch_nos(
		frappe._dict(
			{
				"posting_date": self.date,
				"item_code": self.source_item,
				"warehouse": self.source_warehouse,
				# "qty": self.source_qty,
			}
		)
	)

	if not batch_data:
		frappe.throw(_("No batch available for given warehouse"))
	self.source_batch_details = []
	inventory_type = "Regular Stock"
	if self.customer and self.is_customer_metal:
		inventory_type = "Customer Goods"
	if batch_data:
		remaining_qty = 0
		total_qty = 0

		for i in batch_data:
			custom_inventory_type, custom_customer = frappe.db.get_value("Batch", i.batch_no, ["custom_inventory_type", "custom_customer"])

			# Proceed only if inventory type matches
			if custom_inventory_type != inventory_type:
				continue

			# Determine the quantity to be assigned
			qty = 0
			if total_qty != flt(self.source_qty):
				if (custom_inventory_type == "Customer Goods" and custom_customer == self.customer) or custom_inventory_type == "Regular Stock":
					# If the current batch has more quantity than needed, use the difference
					if flt(self.source_qty) > remaining_qty + i.qty:
						qty = i.qty
						remaining_qty += i.qty
					else:
						qty = flt(self.source_qty) - remaining_qty
						remaining_qty = flt(self.source_qty)  # Ensure remaining_qty equals source_qty
					total_qty += qty

					# Append details to source_batch_details
					self.append("source_batch_details", {
						'qty': qty,
						'batch': i.batch_no
					})

			if remaining_qty >= flt(self.source_qty):
				break  # Stop if we have filled the required quantity

		if total_qty != flt(self.source_qty):
			frappe.throw(_("The source quantity is not available for the given warehouse. The available quantity is {}.".format(total_qty)))
