import frappe
from frappe.model.mapper import get_mapped_doc


def update_main_slip_se_details(doc, stock_entry_type, se_row, warehouse_data, is_cancelled=False):
	based_on = "employee"
	based_on_value = doc.employee
	if doc.subcontractor:
		based_on = "subcontractor"
		based_on_value = doc.subcontractor

	if not warehouse_data.get(based_on_value):
		warehouse_data[based_on_value] = frappe._dict({"r_warehouse": None, "m_warehouse": None})

		warehouse_data[based_on_value]["m_warehouse"] = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": doc.company,
				based_on: based_on_value,
				"warehouse_type": "Manufacturing",
			},
		)
		warehouse_data[based_on_value]["r_warehouse"] = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": doc.company,
				based_on: based_on_value,
				"warehouse_type": "Raw Material",
			},
		)

	m_warehouse = warehouse_data[based_on_value]["m_warehouse"]
	r_warehouse = warehouse_data[based_on_value]["r_warehouse"]

	qty = "qty"
	consume_qty = "consume_qty"
	if se_row.custom_employee_consumption:
		consume_qty = "employee_qty"
	if (
		not se_row.custom_employee_consumption
		and se_row.manufacturing_operation
		and stock_entry_type
		not in (
			"Material Transfer (MAIN SLIP)",
			"Manufacture",
		)
	):
		qty = "mop_qty"
		consume_qty = "mop_consume_qty"

	exsting_se_details = [row.se_item for row in doc.stock_details]
	if se_row.s_warehouse == m_warehouse:
		if se_row.name not in exsting_se_details:
			if se_row.manufacturing_operation:
				consume_qty = "mop_consume_qty"
			doc.append(
				"stock_details",
				{
					"batch_no": se_row.batch_no,
					consume_qty: se_row.qty,
					"se_item": se_row.name,
					"auto_created": se_row.get("auto_created"),
					"stock_entry": se_row.parent,
				},
			)

	if se_row.t_warehouse == r_warehouse:
		if se_row.name not in exsting_se_details:
			doc.append(
				"stock_details",
				{
					"item_code": se_row.item_code,
					"batch_no": se_row.batch_no,
					qty: se_row.qty,
					"se_item": se_row.name,
					"auto_created": se_row.get("auto_created"),
					"stock_entry": se_row.parent,
				},
			)

	if (se_row.s_warehouse == r_warehouse) and (se_row.t_warehouse == m_warehouse):
		if se_row.name not in exsting_se_details:
			doc.append(
				"stock_details",
				{
					"item_code": se_row.item_code,
					"batch_no": se_row.batch_no,
					"consume_qty": se_row.qty,
					"se_item": se_row.name,
					"auto_created": se_row.get("auto_created"),
					"stock_entry": se_row.parent,
				},
			)
			doc.append(
				"stock_details",
				{
					"item_code": se_row.item_code,
					"batch_no": se_row.batch_no,
					"mop_qty": se_row.qty,
					"se_item": se_row.name,
					"auto_created": se_row.get("auto_created"),
					"stock_entry": se_row.parent,
				},
			)

	elif se_row.s_warehouse == r_warehouse:
		if se_row.name not in exsting_se_details:
			doc.append(
				"stock_details",
				{
					"batch_no": se_row.batch_no,
					consume_qty: se_row.qty,
					"se_item": se_row.name,
					"auto_created": se_row.get("auto_created"),
					"stock_entry": se_row.parent,
				},
			)

	elif se_row.t_warehouse == m_warehouse:
		if se_row.name not in exsting_se_details:
			doc.append(
				"stock_details",
				{
					"batch_no": se_row.batch_no,
					qty: se_row.qty,
					"se_item": se_row.name,
					"auto_created": se_row.get("auto_created"),
					"stock_entry": se_row.parent,
				},
			)


@frappe.whitelist()
def make_stock_in_entry(source_name, target_doc=None):
	def set_missing_values(source, target):
		material_request = frappe.db.get_value("Material Request", {"manufacturing_order": source_name})

		SE = frappe.qb.DocType("Stock Entry")
		SEI = frappe.qb.DocType("Stock Entry Detail")

		stock_se_data = (
			frappe.qb.from_(SE)
			.join(SEI)
			.on(SE.name == SEI.parent)
			.select(
				SEI.item_code,
				SEI.qty,
				SEI.uom,
				SEI.basic_rate,
				SEI.inventory_type,
				SEI.customer,
				SEI.conversion_factor,
				SEI.t_warehouse,
			)
			.where(SE.stock_entry_type == "Material Transfer From Reserve")
			.where(SEI.material_request == material_request)
			.where(SEI.custom_parent_manufacturing_order == source_name)
		)

		if target.custom_item_type:
			variant_of_dict = {"Gemstone": "G", "Diamond": "D"}
			if variant_of_dict.get(target.custom_item_type):
				stock_se_data = stock_se_data.where(
					SEI.custom_variant_of == variant_of_dict.get(target.custom_item_type)
				)

		stock_se_data = stock_se_data.run(as_dict=True)

		for row in stock_se_data:
			target.append(
				"items",
				{
					"s_warehouse": row.t_warehouse,
					"item_code": row.item_code,
					"qty": row.qty,
					"uom": row.uom,
					"conversion_factor": row.conversion_factor,
					"basic_rate": row.basic_rate,
					"inventory_type": row.inventory_type,
					"customer": row.get("customer"),
					"custom_parent_manufacturing_order": source_name,
				},
			)
		target.stock_entry_type = "Material Transfer (WORK ORDER)"
		target.purpose = "Material Transfer"

		target.set_missing_values()

	# Need to Optimize
	# from frappe.model.document import copy_doc

	# doclist = copy_doc("Parent Manufacturing Order", source_name)
	# doclist = frappe.copy_doc(source_name)
	# if doclist.docstatus == 1:
	#
	# 	set_missing_values(doclist)
	# return doclist

	doclist = get_mapped_doc(
		"Parent Manufacturing Order",
		source_name,
		{
			"Parent Manufacturing Order": {
				"validation": {"docstatus": ["=", 1]},
			},
		},
		target_doc,
		set_missing_values,
	)

	return doclist
