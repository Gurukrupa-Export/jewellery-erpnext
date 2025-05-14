# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

import json

import frappe
from erpnext.setup.doctype.brand.brand import get_brand_defaults
from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults
from erpnext.stock.doctype.stock_entry.stock_entry import get_uom_details, get_warehouse_details
from erpnext.stock.get_item_details import (
	get_bin_details,
	get_conversion_factor,
	get_default_cost_center,
)
from frappe import _
from frappe.model.document import Document
from frappe.utils import nowdate

from jewellery_erpnext.jewellery_erpnext.doctype.subcontracting.doc_events.sub_utils import (
	create_repack_entry,
)


class Subcontracting(Document):
	def validate(self):
		self.set_PMO()
		# self.set_source_and_remain_table()

	def on_submit(self):
		if not self.finish_item:
			# self.finish_item = frappe.db.get_value("Manufacturing Setting", self.company, "service_item")
			# finish_item_value = frappe.db.get_value("Manufacturing Setting", self.company, "service_item")
			finish_item_value = frappe.db.get_value("Manufacturing Setting", {"manufacturer":self.manufacturer}, "service_item")
			self.db_set("finish_item", finish_item_value)
		create_repack_entry(self)

	def set_PMO(self):
		if self.work_order:
			self.manufacturing_order = frappe.get_value(
				"Manufacturing Work Order", self.work_order, "manufacturing_order"
			)
			self.date = frappe.utils.today()

	def set_source_and_remain_table(self):
		get_balance_table = frappe.get_all("EIR Balance Table", {"parent": self.employee_ir}, ["*"])
		idx_source_table = 1
		idx_remain_balance = 1
		self.source_table = []
		self.target_table = []
		self.remain_balance_item = []
		for row in get_balance_table:
			item_doc = frappe.get_doc("Item", row.item_code)
			if item_doc.variant_of in ["M", "F"]:
				row["idx"] = idx_source_table
				row["name"] = None
				row["parent"] = None
				row["parentfield"] = None
				row["parenttype"] = None
				# row['parent'] =self.name
				# row['parentfield']= "source_table"
				# row['parenttype'] = self.doctype
				row["department"] = self.department
				row["manufacturer"] = self.manufacturer
				row["parent_manufacturing_order"] = self.manufacturing_order
				row["manufacturing_work_order"] = self.work_order
				row["manufacturing_operation"] = self.operation
				if self.employee_ir_type == "Issue":
					self.append("source_table", row)
				else:
					self.append("target_table", row)
				idx_source_table += 1
			else:
				row["idx"] = idx_remain_balance
				row["name"] = None
				row["parent"] = None
				row["parentfield"] = None
				row["parenttype"] = None
				row["department"] = self.department
				row["manufacturer"] = self.manufacturer
				row["parent_manufacturing_order"] = self.manufacturing_order
				row["manufacturing_work_order"] = self.work_order
				row["manufacturing_operation"] = self.operation
				self.append("remain_balance_item", row)
				idx_remain_balance += 1

	@frappe.whitelist()
	def set_purity_wise_allowed_qty(self):
		if self.source_table:
			source_purity = float(
				frappe.get_value("Manufacturing Work Order", self.work_order, "metal_purity")
			)
			# Set to store unique item codes
			unique_item_codes = set()

			if self.target_table:
				for row in self.target_table:
					item_variant_attribute_value = frappe.get_value(
						"Item Variant Attribute",
						{"parent": row.item_code, "attribute": "Metal Purity"},
						"attribute_value",
					)

					if not item_variant_attribute_value:
						frappe.throw(_("Attribute Value Missing"))

					target_purity = float(
						frappe.get_value("Attribute Value", item_variant_attribute_value, "purity_percentage")
					)

					if not target_purity:
						frappe.throw(_("Purity Percentage Missing"))

					qty = (self.sum_source_table * source_purity) / target_purity

					if qty:
						# self.purity_wise_allowed_qty = qty
						# Add item code to set
						unique_item_codes.add(row.item_code)
						return qty
				# Check if more than one unique item code exists
				if len(unique_item_codes) > 1:
					frappe.throw(_("All rows must have the same item code"))

	@frappe.whitelist()
	def custom_get_item_details(self, args=None, for_update=False):
		if isinstance(args, str):
			args = json.loads(args)

		Item = frappe.qb.DocType("Item")
		ItemDefault = frappe.qb.DocType("Item Default")
		query = (
			frappe.qb.from_(Item)
			.left_join(ItemDefault)
			.on((Item.name == ItemDefault.parent) & (ItemDefault.company == self.company))
			.select(
				Item.name,
				Item.stock_uom,
				Item.description,
				Item.image,
				Item.item_name,
				Item.item_group,
				Item.has_batch_no,
				Item.sample_quantity,
				Item.has_serial_no,
				Item.allow_alternative_item,
				ItemDefault.expense_account,
				ItemDefault.buying_cost_center,
			)
			.where(
				(Item.name == args.get("item_code"))
				& (Item.disabled == 0)
				& (
					(Item.end_of_life.isnull())
					| (Item.end_of_life < "1900-01-01")
					| (Item.end_of_life > nowdate())
				)
			)
		)
		item = query.run(as_dict=True)
		if not item:
			frappe.throw(
				("Item {0} is not active or end of life has been reached").format(args.get("item_code"))
			)
		item = item[0]
		item_group_defaults = get_item_group_defaults(item.name, self.company)
		brand_defaults = get_brand_defaults(item.name, self.company)
		ret = frappe._dict(
			{
				"uom": item.stock_uom,
				"stock_uom": item.stock_uom,
				"description": item.description,
				"image": item.image,
				"item_name": item.item_name,
				"cost_center": get_default_cost_center(
					args, item, item_group_defaults, brand_defaults, self.company
				),
				"qty": args.get("qty"),
				"transfer_qty": args.get("qty"),
				"conversion_factor": 1,
				"actual_qty": 0,
				"basic_rate": 0,
				"has_serial_no": item.has_serial_no,
				"has_batch_no": item.has_batch_no,
				"sample_quantity": item.sample_quantity,
				"expense_account": item.expense_account,
			}
		)
		if args.get("uom") and for_update:
			ret.update(get_uom_details(args.get("item_code"), args.get("uom"), args.get("qty")))
		for company_field, field in {
			"stock_adjustment_account": "expense_account",
			"cost_center": "cost_center",
		}.items():
			if not ret.get(field):
				ret[field] = frappe.get_cached_value("Company", self.company, company_field)
		stock_and_rate = get_warehouse_details(args) if args.get("warehouse") else {}
		ret.update(stock_and_rate)

		return ret
