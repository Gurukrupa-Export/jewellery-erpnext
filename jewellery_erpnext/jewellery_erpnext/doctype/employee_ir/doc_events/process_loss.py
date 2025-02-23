import frappe
from frappe import _


def process_loss_entry(
	doc, row, manual_loss_items, proprtionate_loss_items, mfg_warehouse,employee_wh, department_wh, mop_data=None
):
	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = "Process Loss"
	se_doc.purpose = "Repack"
	se_doc.manufacturing_order = frappe.db.get_value(
		"Manufacturing Work Order", row.get("manufacturing_work_order"), "manufacturing_order"
	)
	se_doc.manufacturing_work_order = row.get("manufacturing_work_order")
	se_doc.manufacturing_operation = row.manufacturing_operation
	se_doc.department = doc.department
	se_doc.to_department = doc.department
	se_doc.employee = doc.employee
	se_doc.subcontractor = doc.subcontractor
	se_doc.auto_created = 1
	se_doc.employee_ir = doc.name
	se_doc.main_slip = doc.main_slip

	repack_items = manual_loss_items + proprtionate_loss_items

	for loss_item in repack_items:
		loss_item = frappe._dict(loss_item)
		if mop_data:
			for item in mop_data:
				if loss_item.manufacturing_operation == item.parent and loss_item.item_code == item.item_code:
					item.qty -= loss_item.get("loss_qty")
		if abs(loss_item.get("loss_qty")):
			if doc.main_slip and loss_item.get("variant_of") in ["M", "F"]:
				continue
			else:
				process_loss_item(doc, row, se_doc, loss_item, mfg_warehouse,employee_wh, department_wh)

	if se_doc.items:
		se_doc.flags.ignore_permissions = True
		se_doc.save()
		se_doc.submit()


def process_loss_item(doc, row, se_doc, loss_item, mfg_warehouse,employee_wh, department_wh):
	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import get_item_loss_item

	loss_warehouse = department_wh
	dust_item = get_item_loss_item(
		doc.company,
		loss_item.get("item_code"),
		loss_item.get("item_code")[0],
		loss_item.get("loss_type"),
	)
	variant_of = loss_item.get("item_code")[0]

	variant_loss_details = frappe.db.get_value(
		"Variant Loss Warehouse",
		{"parent": doc.manufacturer, "variant": variant_of},
		["loss_warehouse", "consider_department_warehouse", "warehouse_type"],
		as_dict=1,
	)

	if variant_loss_details and variant_loss_details.get("loss_warehouse"):
		loss_warehouse = variant_loss_details.get("loss_warehouse")

	elif variant_loss_details.get("consider_department_warehouse") and variant_loss_details.get(
		"warehouse_type"
	):
		loss_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": doc.department,
				"warehouse_type": variant_loss_details.get("warehouse_type"),
			},
		)
	if not loss_warehouse:
		frappe.throw(_("Default loss warehouse is not set in Manufacturer loss table"))

	se_doc.append(
		"items",
		{
			"item_code": loss_item.get("item_code"),
			"s_warehouse": mfg_warehouse,
			"t_warehouse": None,
			"to_employee": None,
			"employee": doc.employee,
			"to_subcontractor": None,
			"use_serial_batch_fields": True,
			"serial_and_batch_bundle": None,
			"subcontractor": doc.subcontractor,
			"to_main_slip": None,
			"main_slip": doc.main_slip
			if frappe.db.get_value(
				"Manufacturing Work Order", row.manufacturing_work_order, "is_finding_mwo"
			)
			else None,
			"qty": abs(loss_item.get("loss_qty")),
			"manufacturing_operation": loss_item.get("manufacturing_operation"),
			"department": doc.department,
			"to_department": doc.department,
			"manufacturer": doc.manufacturer,
			"material_request": None,
			"material_request_item": None,
			"inventory_type": loss_item.get("inventory_type"),
			"customer": loss_item.get("customer"),
			"batch_no": loss_item.get("batch_no"),
			"custom_sub_setting_type": loss_item.get("sub_setting_type"),
			"pcs": loss_item.get("pcs") or 0,
		},
	)
	if frappe.db.get_value("Item", dust_item, "valuation_rate") == 0:
		frappe.db.set_value("Item", dust_item, "valuation_rate", se_doc.items[0].get("basic_rate") or 1)
	se_doc.append(
		"items",
		{
			"item_code": dust_item,
			"s_warehouse": None,
			"t_warehouse": loss_warehouse,
			"to_employee": None,
			"employee": doc.employee,
			"to_subcontractor": None,
			"use_serial_batch_fields": True,
			"serial_and_batch_bundle": None,
			"subcontractor": doc.subcontractor,
			"to_main_slip": None,
			"main_slip": doc.main_slip,
			"qty": abs(loss_item.get("loss_qty")),
			# "manufacturing_operation": loss_item.get("manufacturing_operation"),
			"department": doc.department,
			"to_department": doc.department,
			"manufacturer": doc.manufacturer,
			"material_request": None,
			"material_request_item": None,
			"inventory_type": loss_item.get("inventory_type"),
			"customer": loss_item.get("customer"),
			"custom_sub_setting_type": loss_item.get("sub_setting_type"),
			"pcs": loss_item.get("pcs") or 0,
		},
	)
