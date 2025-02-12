import copy

import frappe
from frappe.model.naming import make_autoname
from frappe.utils import flt


def create_repack_entry(self):
	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = "Manufacture"
	# se_doc.set_posting_time = 1
	# se_doc.posting_date = self.date
	# se_doc.posting_time = frappe.utils.nowtime()
	se_doc.inventory_type = "Regular Stock"
	se_doc.to_main_slip = self.main_slip
	se_doc.to_subcontractor = self.supplier
	se_doc.auto_created = 1
	se_doc.manufacturing_order = self.manufacturing_order
	se_doc.manufacturing_work_order = self.work_order
	se_doc.manufacturing_operation = self.operation

	total_qty = 0
	rows_to_append = []
	purity_percentage = self.purity_percentage if self.purity_percentage > 0 else 100
	inventory_type = None
	for row in self.source_table:
		temp_row = copy.deepcopy(row.__dict__)
		temp_row["name"] = None
		temp_row["idx"] = None
		temp_row["use_serial_batch_fields"] = True
		temp_row["serial_and_batch_bundle"] = None
		temp_row["inventory_type"] = "Regular Stock"
		attribute_value = frappe.db.get_value(
			"Item Variant Attribute",
			{"attribute": "Metal Purity", "parent": temp_row["item_code"]},
			"attribute_value",
		)
		if attribute_value:
			purity_percentage = frappe.db.get_value("Attribute Value", attribute_value, "purity_percentage")

		temp_row["pure_qty"] = flt((purity_percentage * temp_row["qty"]) / 100, 3)

		total_qty += temp_row["pure_qty"]
		rows_to_append.append(temp_row)
		inventory_type = row.inventory_type
	warehouse = self.source_table[0].s_warehouse or self.source_table[0].t_warehouse
	uom = rows_to_append[0]["uom"]

	item_dict = {
		"s_warehouse": warehouse if self.transaction_type == "Issue" else None,
		"t_warehouse": warehouse if self.transaction_type == "Receive" else None,
		"item_code": self.finish_item,
		"qty": total_qty,
		"use_serial_batch_fields": 1,
		"uom": uom,
		"is_finished_item": 1 if self.transaction_type == "Receive" else 0,
		"inventory_type": inventory_type,
		"to_main_slip": self.main_slip,
	}

	finish_raw = []

	if self.transaction_type == "Receive":
		batch_number_series = frappe.db.get_value("Item", self.finish_item, "batch_number_series")

		batch_doc = frappe.new_doc("Batch")
		batch_doc.item = self.finish_item

		if batch_number_series:
			batch_doc.batch_id = make_autoname(batch_number_series, doc=batch_doc)

		batch_doc.flags.ignore_permissions = True
		batch_doc.save()
		item_dict["batch_no"] = batch_doc.name
		finish_raw.append(item_dict)
	else:
		msl_batch_data = frappe.db.get_all(
			"Main Slip SE Details",
			{
				"parentfield": "batch_details",
				"parent": self.main_slip,
				"item_code": item_dict["item_code"],
				"qty": ["!=", "consume_qty"],
			},
			["batch_no", "qty", "(consume_qty + employee_qty) as consume_qty", "inventory_type"],
		)
		total_qty = item_dict["qty"]
		for row in msl_batch_data:
			if total_qty > 0:
				temp_dict = item_dict.copy()
				temp_dict["batch_no"] = row.batch_no
				if temp_dict["qty"] >= row.qty:
					total_qty -= row.qty
				else:
					temp_dict["qty"] = total_qty
					total_qty = 0
				temp_dict["inventory_type"] = row.inventory_type
				finish_raw.append(temp_dict)

	if self.transaction_type == "Receive":
		for row in rows_to_append:
			se_doc.append("items", row)
		for row in finish_raw:
			se_doc.append("items", row)
	else:
		for row in finish_raw:
			se_doc.append("items", row)
		for row in rows_to_append:
			row["is_finished_item"] = 1
			se_doc.append("items", row)

	se_doc.save()
	se_doc.submit()

	frappe.db.set_value("Stock Entry", self.stock_entry, "repack_entry", se_doc.name)
	self.db_set("stock_entry", se_doc.name)
