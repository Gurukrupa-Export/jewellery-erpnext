# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt
import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.doc_events.batch_utils import (
	update_fifo_batch,
)
from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import get_item_loss_item


class GemstoneConversion(Document):
	def before_validate(self):
		update_fifo_batch(self)
		self.validate_gemstone_type()
		self.validate_target_item()

	def on_submit(self):
		make_gemstone_stock_entry(self)
		if self.g_source_qty > self.batch_avail_qty:
			frappe.throw(_("Source Qty greater then batch available qty"))

	def validate(self):
		loss_item = get_loss_item(self.company, self.g_source_item, self.loss_type)
		remove_list = []
		self.g_target_qty = 0
		for row in self.sc_target_table:
			if row.item_code == loss_item:
				remove_list.append(row)
				continue
			self.g_target_qty += row.qty

		for row in remove_list:
			self.remove(row)

		if loss_item and flt(self.g_source_qty - self.g_target_qty, 2) > 0:
			self.append(
				"sc_target_table", {"item_code": loss_item, "qty": (self.g_source_qty - self.g_target_qty)}
			)

		if self.g_target_qty > self.g_source_qty:
			frappe.throw(_("Target Qty does not match with Source Qty"))

		if self.g_loss_qty < 0:
			frappe.throw(_("Target Qty not allowed greater than Source Qty"))
		if self.g_target_qty > self.batch_avail_qty:
			frappe.throw(_("Target Qty not allowed greater than Batch Available Qty"))
		if self.g_source_qty > self.batch_avail_qty:
			frappe.throw(
				f"Conversion failed batch available qty not meet. </br><b>(Batch Qty = {self.batch_avail_qty})</b><br>select another batch."
			)
		if self.g_source_qty == 0 or self.g_target_qty == 0:
			frappe.throw(_("Source Qty or Target Qty not allowed Zero to post transaction"))
		if self.g_source_qty < 0:
			frappe.throw(_("Source Qty invalid"))

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
		bal_qty = None
		supplier = None
		customer = None
		inventory_type = None

		error = []
		if self.batch:
			bal_qty = get_batch_qty(batch_no=self.batch, warehouse=self.source_warehouse)
			reference_doctype, reference_name = frappe.get_value(
				"Batch", self.batch, ["reference_doctype", "reference_name"]
			)
			if not bal_qty:
				error.append("Batch Qty zero")
			if reference_doctype:
				if reference_doctype == "Purchase Receipt":
					supplier = frappe.get_value(reference_doctype, reference_name, "supplier")
					inventory_type = "Regular Stock"
				if reference_doctype == "Stock Entry":
					inventory_type = frappe.get_value(reference_doctype, reference_name, "inventory_type")
					if not inventory_type:
						inventory_type = frappe.get_value("Batch", self.batch, "custom_inventory_type")
					if inventory_type == "Customer Goods":
						customer = frappe.get_value(reference_doctype, reference_name, "_customer")
			if error:
				frappe.throw(", ".join(error))

		return bal_qty, supplier, customer, inventory_type

	def validate_gemstone_type(self):
		gemstone_type = frappe.db.get_value(
			"Item Variant Attribute",
			{"attribute": "Gemstone Type", "parent": self.g_source_item},
			"attribute_value",
		)
		variant_of = frappe.db.get_value("Item", self.g_source_item, "variant_of")

		for row in self.sc_target_table:
			t_variant_of = frappe.db.get_value("Item", row.item_code, "variant_of")

			if variant_of == t_variant_of:
				continue
			t_gemstone_type = frappe.db.get_value(
				"Item Variant Attribute",
				{"attribute": "Gemstone Type", "parent": row.item_code},
				"attribute_value",
			)

			if t_gemstone_type != gemstone_type:
				frappe.throw(
					_("The gemstone type in <b>{0}</b> is different from that in <b>{1}</b>.").format(
						row.item_code, self.g_source_item
					)
				)

	def validate_target_item(self):
		attr_value = frappe.db.get_value(
			"Item Variant Attribute",
			{"attribute": "Gemstone Size", "parent": self.g_source_item},
			"attribute_value",
		)
		variant_of = frappe.db.get_value("Item", self.g_source_item, "variant_of")
		if not attr_value:
			return
		height, weight = frappe.db.get_value("Attribute Value", attr_value, ["height", "weight"])

		for row in self.sc_target_table:

			t_variant_of, is_customer_gemstone = frappe.db.get_value(
				"Item", row.item_code, ["variant_of", "custom_inventory_type_can_be_customer_goods"]
			)

			if variant_of == t_variant_of:
				continue

			t_attr_value = frappe.db.get_value(
				"Item Variant Attribute",
				{"attribute": "Gemstone Size", "parent": row.item_code},
				"attribute_value",
			)

			t_height, t_weight = frappe.db.get_value("Attribute Value", t_attr_value, ["height", "weight"])

			if is_customer_gemstone:
				if t_height != height or weight != t_weight:
					frappe.throw(
						_("The gemstone size in <b>{0}</b> is not equal size of <b>{1}</b>.").format(
							row.item_code, self.g_source_item
						)
					)

			if t_height > height or weight > t_weight:
				frappe.throw(
					_("The gemstone size in <b>{0}</b> is not within the size range of <b>{1}</b>.").format(
						row.item_code, self.g_source_item
					)
				)


def make_gemstone_stock_entry(self):
	target_wh = self.target_warehouse
	source_wh = self.source_warehouse
	inventory_type = self.inventory_type
	batch_no = self.batch
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Repack-Gemstone Conversion",
			"purpose": "Repack",
			"company": self.company,
			"custom_gemstone_conversion": self.name,
			"inventory_type": inventory_type,
			"_customer": self.customer,
			"auto_created": 1,
			"branch": self.branch,
		}
	)
	source_item = []
	target_item = []
	source_item.append(
		{
			"item_code": self.g_source_item,
			"qty": self.g_source_qty,
			"inventory_type": inventory_type,
			"batch_no": batch_no,
			"department": self.department,
			"employee": self.employee,
			"manufacturer": self.manufacturer,
			"s_warehouse": source_wh,
		}
	)
	for row in self.sc_target_table:
		target_item.append(
			{
				"item_code": row.item_code,
				"qty": row.qty,
				"inventory_type": inventory_type,
				"department": self.department,
				"employee": self.employee,
				"manufacturer": self.manufacturer,
				"t_warehouse": target_wh,
			}
		)
	# if self.g_loss_qty > 0:
	# 	target_item.append(
	# 		{
	# 			"item_code": self.g_loss_item,
	# 			"qty": self.g_loss_qty,
	# 			"inventory_type": inventory_type,
	# 			"department": self.department,
	# 			"employee": self.employee,
	# 			"manufacturer": self.manufacturer,
	# 			"t_warehouse": target_wh,
	# 		}
	# 	)
	for row in source_item:
		se.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"inventory_type": row["inventory_type"],
				"batch_no": row["batch_no"],
				"department": row["department"],
				"employee": row["employee"],
				"manufacturer": row["manufacturer"],
				"s_warehouse": row["s_warehouse"],
				"use_serial_batch_fields": True,
				"serial_and_batch_bundle": None,
			},
		)
	for row in target_item:
		se.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"inventory_type": row["inventory_type"],
				"department": row["department"],
				"employee": row["employee"],
				"manufacturer": row["manufacturer"],
				"t_warehouse": row["t_warehouse"],
			},
		)
	se.save()
	se.submit()
	self.stock_entry = se.name


@frappe.whitelist()
def get_loss_item(company, souce_item, loss_type):
	return get_item_loss_item(company, souce_item, "G", loss_type)
