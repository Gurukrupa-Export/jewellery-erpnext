import json

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc
from frappe.utils import flt, nowdate

from jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request import (
	make_department_mop_stock_entry,
	make_mop_stock_entry,
)
from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils.before_validate import (
	update_pure_qty,
	validate_warehouse,
)


def before_validate(self, method):
	if self.set_warehouse and self.set_from_warehouse:
		source_branch = frappe.db.get_value(
			"Warehouse", self.set_from_warehouse, "custom_branch"
		)
		target_branch = frappe.db.get_value(
			"Warehouse", self.set_warehouse, "custom_branch"
		)

		if source_branch == target_branch:
			self.custom_transfer_type = "Transfer To Department"
		else:
			self.custom_transfer_type = "Transfer To Branch"

	elif self.material_request_type == "Manufacture":
		self.custom_transfer_type = "Transfer to Reserve"

	update_pure_qty(self)
	validate_target_item(self)
	validate_warehouse(self)

	if self.custom_manufacturing_operation:
		linked_mo = frappe.db.get_value(
			"Manufacturing Operation",
			self.custom_manufacturing_operation,
			"manufacturing_order",
		)
		if self.manufacturing_order != linked_mo:
			frappe.throw(
				_("Manufacturing Order and Manufacturing Operation are not linked.")
			)


def before_update_after_submit(self, method):
	if self.workflow_state != "Material Transferred to MOP":
		return

	if not self.custom_manufacturing_operation:
		frappe.throw(_("Please select a Manufacturing Operation."))

	mop_fields = frappe.db.get_value(
		"Manufacturing Operation",
		self.custom_manufacturing_operation,
		["status", "department"],
		as_dict=True,
	)

	if not mop_fields:
		frappe.throw(
			_("Manufacturing Operation {0} not found.").format(
				self.custom_manufacturing_operation
			)
		)

	if mop_fields.status == "Finished":
		frappe.throw(_("Cannot select an operation that is already Finished."))

	if self.custom_department:
		make_department_mop_stock_entry(self, mop=self.custom_manufacturing_operation)
	else:
		if not self.items or not self.items[0].warehouse:
			frappe.throw(_("Warehouse is missing from Request Items."))

		table_warehouse_department = frappe.db.get_value(
			"Warehouse", self.items[0].warehouse, "department"
		)

		if mop_fields.department != table_warehouse_department:
			frappe.throw(
				_(
					"Manufacturing Operation's Department and selected Warehouse Department do not match."
				)
			)

		make_mop_stock_entry(self, mop=self.custom_manufacturing_operation)


def validate_target_item(self):
	for row in self.items:
		if not getattr(row, "custom_alternative_item", None):
			continue

		attr_value = frappe.db.get_value(
			"Item Variant Attribute",
			{"attribute": "Diamond Sieve Size", "parent": row.item_code},
			"attribute_value",
		)
		if not attr_value:
			continue

		alternative_item_attr_value = frappe.db.get_value(
			"Item Variant Attribute",
			{"attribute": "Diamond Sieve Size", "parent": row.custom_alternative_item},
			"attribute_value",
		)

		if not alternative_item_attr_value:
			continue

		height_weight = frappe.db.get_value(
			"Attribute Value", attr_value, ["height", "weight"], as_dict=True
		)
		alt_height_weight = frappe.db.get_value(
			"Attribute Value",
			alternative_item_attr_value,
			["height", "weight"],
			as_dict=True,
		)

		if not height_weight or not alt_height_weight:
			continue

		height, weight = height_weight.height, height_weight.weight
		alt_height, alt_weight = alt_height_weight.height, alt_height_weight.weight

		if height is None or weight is None or alt_height is None or alt_weight is None:
			continue

		if abs(alt_height - height) > 0.5 or abs(weight - alt_weight) > 0.5:
			frappe.throw(
				_(
					"The Diamond Sieve Size in <b>{0}</b> is not within the size range of <b>{1}</b>."
				).format(row.item_code, row.custom_alternative_item)
			)


def on_submit(self, method=None):
	if not self.custom_reserve_se:
		return

	se_doc = frappe.get_doc("Stock Entry", self.custom_reserve_se)
	new_se_doc = frappe.copy_doc(se_doc)

	new_se_doc.stock_entry_type = "Material Transfer From Reserve"

	for row in new_se_doc.items:
		original_t_warehouse = frappe.db.get_value(
			"Material Request Item", row.material_request_item, "warehouse"
		)
		row.s_warehouse = row.t_warehouse
		row.t_warehouse = original_t_warehouse
		row.serial_and_batch_bundle = None

	new_se_doc.auto_created = 1
	new_se_doc.save()
	new_se_doc.submit()


@frappe.whitelist()
def make_stock_in_entry(source_name, target_doc=None):
	def set_missing_values(source, target):
		target.material_request_type = "Material Transfer"
		target.customer = source._customer
		target.set_missing_values()
		target.custom_reserve_se = None

	def update_item(source_doc, target_doc, source_parent):
		target_doc.material_request = source_doc.parent
		target_doc.material_request_item = source_doc.name
		target_doc.warehouse = ""
		target_doc.from_warehouse = source_doc.t_warehouse
		target_doc.qty = source_doc.qty

	return get_mapped_doc(
		"Stock Entry",
		source_name,
		{
			"Stock Entry": {
				"doctype": "Material Request",
				"validation": {"docstatus": ["=", 1]},
			},
			"Stock Entry Detail": {
				"doctype": "Material Request Item",
				"field_map": {
					"name": "ste_detail",
					"parent": "against_stock_entry",
					"serial_no": "serial_no",
					"batch_no": "batch_no",
				},
				"postprocess": update_item,
			},
		},
		target_doc,
		set_missing_values,
	)


@frappe.whitelist()
def make_stock_entry(source_name, target_doc=None):
	def update_item(obj, target, source_parent):
		qty = (
			flt(flt(obj.stock_qty) - flt(obj.ordered_qty)) / target.conversion_factor
			if flt(obj.stock_qty) > flt(obj.ordered_qty)
			else 0
		)
		target.qty = qty
		target.transfer_qty = qty * obj.conversion_factor
		target.conversion_factor = obj.conversion_factor

		if source_parent.material_request_type in [
			"Material Transfer",
			"Customer Provided",
		]:
			target.t_warehouse = obj.warehouse
		else:
			target.s_warehouse = obj.warehouse

		if source_parent.material_request_type == "Customer Provided":
			target.allow_zero_valuation_rate = 1

		if source_parent.material_request_type == "Material Transfer":
			target.s_warehouse = obj.from_warehouse

	def set_missing_values(source, target):
		target.purpose = source.material_request_type
		target.custom_material_request_reference = source.name

		if source.job_card:
			target.purpose = "Material Transfer for Manufacture"
		elif source.material_request_type == "Customer Provided":
			target.purpose = "Material Receipt"

		target.set_transfer_qty()
		target.set_actual_qty()
		target.calculate_rate_and_amount(raise_error_if_no_rate=False)

		if (
			source.material_request_type == "Material Transfer"
			and source.inventory_type == "Customer Goods"
		):
			target.stock_entry_type = "Customer Goods Transfer"
		else:
			target.stock_entry_type = target.purpose

		target.set_job_card_data()

		# Map item batches using O(N) lookup instead of O(N^2)
		batch_map = {
			(i.item_code, i.idx): {"batch": i.batch_no, "serial": i.serial_no}
			for i in source.items
		}
		for itm in target.items:
			mapped_data = batch_map.get((itm.item_code, itm.idx))
			if mapped_data:
				itm.batch_no = mapped_data["batch"]
				itm.serial_no = mapped_data["serial"]

		if source.job_card:
			job_card_details = frappe.get_value(
				"Job Card", source.job_card, ["bom_no", "for_quantity"], as_dict=True
			)
			if job_card_details:
				target.bom_no = job_card_details.bom_no
				target.fg_completed_qty = job_card_details.for_quantity
				target.from_bom = 1

	return get_mapped_doc(
		"Material Request",
		source_name,
		{
			"Material Request": {
				"doctype": "Stock Entry",
				"field_no_map": ["manufacturing_order"],
				"validation": {
					"docstatus": ["=", 1],
					"material_request_type": [
						"in",
						["Material Transfer", "Material Issue", "Customer Provided"],
					],
				},
			},
			"Material Request Item": {
				"doctype": "Stock Entry Detail",
				"field_map": {
					"name": "material_request_item",
					"parent": "material_request",
					"uom": "stock_uom",
					"job_card_item": "job_card_item",
				},
				"postprocess": update_item,
				"condition": lambda doc: flt(
					doc.ordered_qty, doc.precision("ordered_qty")
				)
				< flt(doc.stock_qty, doc.precision("ordered_qty")),
			},
		},
		target_doc,
		set_missing_values,
	)


@frappe.whitelist()
def make_in_transit_stock_entry(
	source_name, to_warehouse, transfer_type, pmo=None, mnfr=None
):
	to_department, warehouse_type = frappe.db.get_value(
		"Warehouse", to_warehouse, ["department", "warehouse_type"]
	)
	from_department, set_warehouse = frappe.db.get_value(
		"Material Request", source_name, ["set_from_warehouse", "set_warehouse"]
	)
	in_transit_warehouse = frappe.db.get_value(
		"Warehouse", to_warehouse, "default_in_transit_warehouse"
	)

	check_frm_warehus_type = None
	if from_department:
		check_frm_warehus_type = frappe.db.get_value(
			"Warehouse", from_department, "warehouse_type"
		)

	if not in_transit_warehouse:
		frappe.throw(_("Transit warehouse is not mentioned in Target Warehouse"))

	ste_doc = make_stock_entry(source_name)
	if not getattr(ste_doc, "employee", None):
		ste_doc.add_to_transit = 1

	stock_entry_type = frappe.db.get_value(
		"Transfer Type", transfer_type, "stock_entry_type"
	)
	if not stock_entry_type:
		frappe.throw(
			_("Please specify a Stock Entry Type for the selected Transfer Type.")
		)

	if ste_doc.items and ste_doc.items[0].customer:
		ste_doc.stock_entry_type = "Customer Goods Transfer"
	else:
		if (
			check_frm_warehus_type
			and to_department
			and check_frm_warehus_type == "Consumables"
			and warehouse_type == "Consumables"
		):
			ste_doc.stock_entry_type = "Consumables Issue to  Department"
			ste_doc.to_warehouse = set_warehouse
		else:
			ste_doc.stock_entry_type = stock_entry_type
			ste_doc.to_warehouse = in_transit_warehouse
			ste_doc.to_department = to_department

	if mnfr and pmo:
		pmo_doc = frappe.get_value(
			"Parent Manufacturing Order",
			pmo,
			[
				"customer_sample",
				"customer_voucher_no",
				"customer_gold",
				"customer_diamond",
				"customer_stone",
				"customer_good",
				"customer",
			],
			as_dict=True,
		)

		if pmo_doc and all(
			[
				pmo_doc.customer_sample,
				pmo_doc.customer_voucher_no,
				pmo_doc.customer_gold,
				pmo_doc.customer_diamond,
				pmo_doc.customer_stone,
				pmo_doc.customer_good,
			]
		):
			ste_doc.inventory_type = "Customer Goods"
			ste_doc.customer = pmo_doc.customer
			for row in ste_doc.items:
				row.inventory_type = "Customer Goods"
				row.customer = pmo_doc.customer

	for row in ste_doc.items:
		if ste_doc.stock_entry_type == "Consumables Issue to  Department":
			row.t_warehouse = set_warehouse
		else:
			row.t_warehouse = in_transit_warehouse

	return ste_doc


@frappe.whitelist()
def create_stock_entry(self, method):
	if (
		self.workflow_state != "Material Reserved"
		or self.custom_reserve_se
		or not self.manufacturing_order
	):
		return

	se_doc = frappe.new_doc("Stock Entry")
	se_doc.company = self.company

	stock_entry_type = frappe.db.get_value(
		"Transfer Type", self.custom_transfer_type, "stock_entry_type"
	)
	if not stock_entry_type:
		frappe.throw(
			_("Please specify a Stock Entry type for the selected Transfer type.")
		)

	se_doc.stock_entry_type = stock_entry_type
	se_doc.purpose = "Material Transfer"
	se_doc.add_to_transit = True

	for row in self.items:
		department = frappe.db.get_value("Warehouse", row.from_warehouse, "department")
		t_warehouse = frappe.db.get_value(
			"Warehouse",
			{"disabled": 0, "department": department, "warehouse_type": "Reserve"},
			"name",
		)

		if not t_warehouse:
			frappe.throw(_("Transit warehouse not found for {0}").format(department))

		se_doc.append(
			"items",
			{
				"material_request": self.name,
				"material_request_item": row.name,
				"s_warehouse": row.from_warehouse,
				"t_warehouse": t_warehouse,
				"item_code": row.custom_alternative_item or row.item_code,
				"qty": row.qty,
				"inventory_type": row.inventory_type,
				"customer": row.customer,
				"batch_no": row.batch_no,
				"pcs": row.pcs,
				"cost_center": row.cost_center,
				"sub_setting_type": row.custom_sub_setting_type,
				"use_serial_batch_fields": True,
				"custom_parent_manufacturing_order": self.manufacturing_order,
			},
		)

	se_doc.flags.throw_batch_error = True
	se_doc.save()
	self.custom_reserve_se = se_doc.name
	se_doc.submit()

	frappe.msgprint(_("Reserved Stock Entry {0} has been created").format(se_doc.name))


@frappe.whitelist()
def get_item_details(args, for_update=False):
	if isinstance(args, str):
		args = json.loads(args)

	Item = frappe.qb.DocType("Item")
	ItemDefault = frappe.qb.DocType("Item Default")

	item_data = (
		frappe.qb.from_(Item)
		.left_join(ItemDefault)
		.on(
			(Item.name == ItemDefault.parent)
			& (ItemDefault.company == args.get("company"))
		)
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
	).run(as_dict=True)

	if not item_data:
		frappe.throw(
			_("Item {0} is inactive or its end-of-life has been reached.").format(
				args.get("item_code")
			)
		)

	item = item_data[0]

	return frappe._dict(
		{
			"uom": item.stock_uom,
			"stock_uom": item.stock_uom,
			"description": item.description,
			"image": item.image,
			"item_name": item.item_name,
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
