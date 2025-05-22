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
from frappe.utils import (
	cint,
	comma_or,
	cstr,
	flt,
	format_time,
	formatdate,
	getdate,
	month_diff,
	nowdate,
)


class SwapMetal(Document):
	def validate(self):
		self.set_source_and_remain_table()
		self.set_sum()
		# self.target_qty_calculation()
		# self.validate_target_table()
		self.validate_target_source_table()

	def validate_target_source_table(self):
		if self.manufacturing_order:
			custommer = frappe.db.get_value('Parent Manufacturing Order', self.manufacturing_order, 'customer')

		if self.source_table:
			existing_rows = {row.item_code: row for row in self.target_table}
			appended = False

		
		m_items = {}
		for r in self.source_table:
			if r.item_code.startswith('M'):
				if r.item_code in m_items:
					m_items[r.item_code]['qty'] += r.qty
					m_items[r.item_code]['transfer_qty'] += r.transfer_qty
				else:
					m_items[r.item_code] = {
						"qty": r.qty,
						"uom": r.uom,
						"transfer_qty": r.transfer_qty,
						"s_warehouse": r.s_warehouse
					}

	
		for item_code, data in m_items.items():
			department = frappe.db.get_value('Warehouse', data['s_warehouse'], 'department')
			department_value = frappe.get_all(
				'Warehouse',
				filters={"department": department, "warehouse_type": "Raw Material"},
				fields=["name", "warehouse_type", "department", "parent_warehouse"],
				limit=1
			)
			parent_whouse = department_value[0].get("name") if department_value else None

			if item_code in existing_rows:
				row = existing_rows[item_code]
				row.qty = data['qty']
				row.uom = data['uom']
				row.transfer_qty = data['transfer_qty']
				row.inventory_type = "Customer Goods"
				row.s_warehouse = parent_whouse
				row.customer = custommer
				row.flags.modified = True
			else:
				self.append("target_table", {
					"item_code": item_code,
					"qty": data['qty'],
					"uom": data['uom'],
					"transfer_qty": data['transfer_qty'],
					"inventory_type": "Customer Goods",
					"s_warehouse": parent_whouse,
					"customer": custommer
				})
				appended = True

		
		for row in self.target_table :
			batch = frappe.get_list("Batch", 
					filters={
						"item": row.item_code,
						"custom_customer": row.customer,
						"custom_inventory_type": row.inventory_type
					},
					fields=["name"],
					limit=1
				)
			if batch:
				row.batch_no = batch[0].name
				# frappe.throw(f"{row.customer}")
			else:
				frappe.msgprint(f"No matching Batch found for Item: {row.item_code}, Customer: {row.customer}, Inventory Type: {row.inventory_type}")	
	



	def on_submit(self):
		self.make_stock_entry()

	def validate_target_table(self):
		if not self.target_table:
			self.purity_wise_allowed_qty = 0

		if self.target_table:
			error = []
			for row in self.target_table:
				if row.qty <= 0:
					error.append("Qty Missing")
			if self.purity_wise_allowed_qty < self.sum_target_table:
				error.append("Qty sum not allowed grater then purity wise allowed qty")
			if error:
				frappe.throw("<br>".join(error))

	@frappe.whitelist()
	def get_warehouse(self):
		emp_wh = frappe.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": self.company,
				"employee": self.employee,
				"warehouse_type": "Manufacturing",
			},
			"name",
		)
		dep_wh = frappe.get_value("Warehouse", {"department": self.department}, "name")
		return [emp_wh, dep_wh]

	def set_source_and_remain_table(self):
		get_balance_table = frappe.get_all("MOP Balance Table", {"parent": self.operation}, ["*"])
		idx_source_table = 1
		idx_remain_balance = 1
		self.source_table = []
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
				self.append("source_table", row)
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

	def set_sum(self):
		if not self.target_table:
			self.sum_source_table = 0
			self.sum_target_table = 0
		else:
			self.sum_source_table = 0
			self.sum_target_table = 0

		for row in self.source_table:
			self.sum_source_table += row.qty
		for row in self.target_table:
			self.sum_target_table += row.qty

	@frappe.whitelist()
	def target_qty_calculation(self):
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
					self.purity_wise_allowed_qty = qty
					# Add item code to set
					unique_item_codes.add(row.item_code)
					return qty
			# Check if more than one unique item code exists
			if len(unique_item_codes) > 1:
				frappe.throw(_("All rows must have the same item code"))
		else:
			self.purity_wise_allowed_qty = 0

	def make_stock_entry(self):
		source_item = []
		target_item = []
		# swap_inventroy_type = frappe.get_value(
		# 	"Manufacturing Setting", {"company": self.company}, "inventory_type"
		# )
		swap_inventroy_type = frappe.get_value(
			"Manufacturing Setting", {"manufacturer": self.manufacturer}, "inventory_type"
		)
		# frappe.throw(f"{swap_inventroy_type}")
		if not swap_inventroy_type:
			frappe.throw(_("Inventory type missing please check Manufacturing setting"))
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Repack-Swap Metal",
				"purpose": "Repack",
				"company": self.company,
				"custom_metal_conversions": self.name,
				"inventory_type": "Regular Stock",
				# "_customer": self.customer,
				"auto_created": 1,
			}
		)
		customer = frappe.db.get_value(
			"Parent Manufacturing Order", self.manufacturing_order, "customer"
		)
		for row_st in self.source_table:
			copy_row = row_st.__dict__.copy()
			copy_row["name"] = None
			copy_row["idx"] = None
			copy_row["parentfield"] = None
			copy_row["serial_and_batch_bundle"] = None
			copy_row["use_serial_batch_fields"] = 1

			row_data = row_st.__dict__.copy()
			row_data["serial_and_batch_bundle"] = None
			row_data["use_serial_batch_fields"] = 1
			row_data["name"] = None
			row_data["idx"] = None
			row_data["parentfield"] = None
			row_data["t_warehouse"] = row_data["s_warehouse"]
			row_data["s_warehouse"] = None
			row_data["batch_no"] = None
			row_data["inventory_type"] = swap_inventroy_type  # "Customer Stock"
			if swap_inventroy_type in ["Customer Stock", "Customer Goods"]:
				row_data["customer"] = customer

			source_item.append(copy_row)
			source_item.append(row_data)

		for row_st in self.target_table:
			copy_row = row_st.__dict__.copy()
			copy_row["name"] = None
			copy_row["idx"] = None
			copy_row["parentfield"] = None

			row_data = row_st.__dict__.copy()
			row_data["name"] = None
			row_data["idx"] = None
			row_data["parentfield"] = None
			row_data["t_warehouse"] = row_data["s_warehouse"]
			row_data["s_warehouse"] = None
			row_data["batch_no"] = None
			row_data["inventory_type"] = "Regular Stock"

			target_item.append(copy_row)
			target_item.append(row_data)
		# frappe.throw(f"{target_item}")
		for row in source_item:
			se.append("items", row)
		for row in target_item:
			se.append("items", row)
		se.custom_swap_metal = self.name
		se.custom_metal_conversions = None
		se.manufacturing_order = self.manufacturing_order
		se.manufacturing_work_order = self.work_order
		se.manufacturing_operation = self.operation

		se.save()
		se.submit()

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
					Item.end_of_life.isnull() | (Item.end_of_life < "1900-01-01") | (Item.end_of_life > nowdate())
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


from erpnext.controllers.queries import get_batch_no


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def from_standard_get_batch_no(doctype, txt, searchfield, start, page_len, filters):
	data = get_batch_no(doctype, txt, searchfield, start, page_len, filters)
	return data


@frappe.whitelist()
def get_selected_batch_qty(batch):
	batch_qty = frappe.get_value("Batch", batch, "batch_qty")
	return batch_qty
