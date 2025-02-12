# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

import copy

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils import (
	get_fifo_batches,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.utils import (
	update_batch_details,
)


class DiamondConversion(Document):
	def before_validate(self):
		update_batch_details(self)

	def on_submit(self):
		make_diamond_stock_entry(self)

	def validate(self):
		to_check_valid_qty_in_table(self)
		validate_target_item(self)

	@frappe.whitelist()
	def get_detail_tab_value(self):
		errors = []
		dpt, branch = frappe.get_value("Employee", self.employee, ["department", "branch"])
		if not dpt:
			errors.append(f"Department Messing against <b>{self.employee} Employee Master</b>")
		if not branch:
			errors.append(f"Branch Messing against <b>{self.employee} Employee Master</b>")
		mnf = frappe.get_value("Department", dpt, "manufacturer")
		if not mnf:
			errors.append("Manufacturer Messing against <b>Department Master</b>")
		s_wh = frappe.get_value("Warehouse", {"disabled": 0, "department": dpt}, "name")
		if not mnf:
			errors.append("Warehouse Missing Warehouse Master Department Not Set")
		if errors:
			frappe.throw("<br>".join(errors))
		if dpt and mnf and s_wh:
			self.department = dpt
			self.branch = branch
			self.manufacturer = mnf
			self.source_warehouse = s_wh
			self.target_warehouse = s_wh

	@frappe.whitelist()
	def get_batch_detail(self):
		bal_qty = ""
		supplier = ""
		customer = ""
		inventory_type = ""

		error = []
		for row in self.sc_source_table:
			bal_qty = get_batch_qty(batch_no=row.batch, warehouse=self.source_warehouse)
			reference_doctype = None
			if row.batch:
				reference_doctype, reference_name = frappe.get_value(
					"Batch", row.batch, ["reference_doctype", "reference_name"]
				)
			if not bal_qty:
				error.append("Batch Qty zero")
			if reference_doctype:
				if reference_doctype == "Purchase Receipt":
					supplier = frappe.get_value(reference_doctype, reference_name, "supplier")
					inventory_type = "Regular Stock"
				if reference_doctype == "Stock Entry":
					inventory_type = frappe.get_value(reference_doctype, reference_name, "inventory_type")
					if inventory_type == "Customer Goods":
						customer = frappe.get_value(reference_doctype, reference_name, "_customer")
			if error:
				frappe.throw(", ".join(error))
		return bal_qty or None, supplier or None, customer or None, inventory_type or None


def to_check_valid_qty_in_table(self):
	for row in self.sc_source_table:
		if row.qty <= 0:
			frappe.throw(_("Source Table Qty not allowed Nigative or Zero Value"))
	for row in self.sc_target_table:
		if row.qty <= 0:
			frappe.throw(_("Target Table Qty not allowed Nigative or Zero Value"))
	if not self.sc_source_table:
		frappe.throw(_("Source table is empty. Please add rows."))
	if not self.sc_target_table:
		frappe.throw(_("Target table is empty. Please add rows."))


def validate_target_item(self):
	attribute_data = frappe._dict()
	sieve_size_range_value = []
	for row in self.sc_source_table:
		attr_value = frappe.db.get_value(
			"Item Variant Attribute",
			{"attribute": "Diamond Sieve Size Range", "parent": row.item_code},
			"attribute_value",
		)
		if attr_value:
			if not attribute_data.get(attr_value):
				attribute_data[attr_value] = frappe.db.get_all(
					"Attribute Value", {"sieve_size_range": attr_value}, pluck="name"
				)
			sieve_size_range_value += attribute_data.get(attr_value)
	# sieve_size_range_value = []
	# for row in attr_value_list:

	for row in self.sc_target_table:
		attr_value = frappe.db.get_value(
			"Item Variant Attribute",
			{"attribute": "Diamond Sieve Size", "parent": row.item_code},
			"attribute_value",
		)
		if attr_value not in sieve_size_range_value:
			frappe.throw(
				_("{0} attribute value not available in the sieve size range value").format(row.item_code)
			)


def make_diamond_stock_entry(self):
	target_wh = self.target_warehouse
	source_wh = self.source_warehouse

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"company": self.company,
			"stock_entry_type": "Repack-Diamond Conversion",
			"purpose": "Repack",
			"custom_diamond_conversion": self.name,
			"auto_created": 1,
			"branch": self.branch,
		}
	)
	inventory_wise_data = {}
	for row in self.sc_source_table:
		if inventory_wise_data.get(row.inventory_type):
			inventory_wise_data[row.inventory_type]["qty"] += row.qty
		else:
			inventory_wise_data[row.inventory_type] = {"customer": row.get("customer"), "qty": row.qty}
		se.append(
			"items",
			{
				"item_code": row.item_code,
				"qty": row.qty,
				"inventory_type": row.inventory_type,
				"batch_no": row.batch,
				"department": self.department,
				"employee": self.employee,
				"manufacturer": self.manufacturer,
				"s_warehouse": source_wh,
				"use_serial_batch_fields": True,
				"customer": row.get("customer"),
			},
		)
	for row in self.sc_target_table:
		for inventory in inventory_wise_data:
			se.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": ((row.qty * inventory_wise_data[inventory]["qty"]) / self.sum_source_table),
					# "inventory_type": "Regular Stock",  # row.inventory_type,
					# "batch_no":row.batch,
					"department": self.department,
					"employee": self.employee,
					"manufacturer": self.manufacturer,
					"t_warehouse": target_wh,
					"inventory_type": inventory,
					"set_basic_rate_manually": 1,
					"customer": inventory_wise_data[inventory].get("customer"),
				},
			)
	se.save()
	amount = 0
	for row in se.items:
		if row.s_warehouse:
			amount += row.amount

	avg_amount = amount / self.sum_source_table
	for row in se.items:
		if row.t_warehouse:
			row.basic_rate = flt(avg_amount, 3)
			row.amount = row.qty * avg_amount
			row.basic_amount = row.qty * avg_amount

	se.save()
	se.submit()
	self.stock_entry = se.name
