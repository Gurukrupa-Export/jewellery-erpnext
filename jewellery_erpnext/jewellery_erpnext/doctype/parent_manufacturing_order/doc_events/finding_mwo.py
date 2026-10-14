import json

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc


def create_finding_mwo(self, finding_data=None):
	if not finding_data:
		return

	for row in finding_data:
		if frappe.db.get_value(
			"Item", row.item_variant, "custom_is_manufacturing_item"
		):
			mwo_doc = get_mapped_doc(
				"Parent Manufacturing Order",
				self.name,
				{
					"Parent Manufacturing Order": {
						"doctype": "Manufacturing Work Order",
						"field_map": {"name": "manufacturing_order"},
					}
				},
			)
			mwo_doc.type = "Finding Manufacturing"
			mwo_doc.item_code = row.item_variant
			mwo_doc.master_bom = frappe.db.get_value(
				"Item", row.item_variant, "master_bom"
			)
			mwo_doc.metal_touch = row.metal_touch
			mwo_doc.metal_type = row.metal_type
			mwo_doc.metal_purity = row.metal_purity
			mwo_doc.metal_colour = row.metal_colour
			mwo_doc.seq = int(self.name.split("-")[-1])
			mwo_doc.is_finding_mwo = True
			mwo_doc.auto_created = 1
			mwo_doc.department = frappe.db.get_value(
				"Manufacturing Setting",
				{"manufacturer": mwo_doc.manufacturer},
				"default_department",
			)
			mwo_doc.save()


def create_finding_without_so_mwo(self):
	"""Create the Manufacturing Work Order for a submitted Without-SO finding PMO.

	Built the way create_finding_mwo builds a finding MWO, but from the PMO alone: there is
	no Tracking BOM row to read the item and metal off, so they come from the PMO, which the
	Manufacturing Plan stamped from its Finding Details row. One PMO is one finding piece, so
	it gets exactly one MWO. Left in Draft like every other auto-created MWO; submitting it is
	what raises the Manufacturing Operation."""
	if frappe.db.exists(
		"Manufacturing Work Order",
		{"manufacturing_order": self.name, "docstatus": ["<", 2]},
	):
		return

	mwo_doc = get_mapped_doc(
		"Parent Manufacturing Order",
		self.name,
		{
			"Parent Manufacturing Order": {
				"doctype": "Manufacturing Work Order",
				"field_map": {"name": "manufacturing_order"},
			}
		},
	)
	mwo_doc.type = "Finding Manufacturing"
	mwo_doc.item_code = self.item_code
	mwo_doc.master_bom = frappe.db.get_value("Item", self.item_code, "master_bom")
	mwo_doc.metal_type = self.metal_type
	mwo_doc.metal_touch = self.metal_touch
	mwo_doc.metal_purity = self.metal_purity
	mwo_doc.metal_colour = self.metal_colour
	mwo_doc.seq = int(self.name.split("-")[-1])
	mwo_doc.is_finding_mwo = 1
	mwo_doc.is_without_so = 1
	mwo_doc.auto_created = 1
	mwo_doc.department = frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": self.manufacturer},
		"default_department",
	)
	mwo_doc.save()
	return mwo_doc


def delete_without_so_drafts(doctype, filters):
	"""Delete the Draft documents a cancelled Without-SO source raised.

	Frappe's cancel link check only stops on submitted links, so without this the drafts would
	survive the cancel and could still be submitted into live manufacturing."""
	for name in frappe.get_all(
		doctype, filters={**filters, "docstatus": 0}, pluck="name"
	):
		frappe.delete_doc(doctype, name, ignore_permissions=True)


def validate_without_so_source_submitted(doctype, name, label):
	"""Refuse to submit a Without-SO document whose source is no longer submitted."""
	if not name or frappe.db.get_value(doctype, name, "docstatus") != 1:
		frappe.throw(
			_("{0} {1} is not submitted; this document cannot be submitted.").format(
				label, frappe.bold(name or "-")
			)
		)


def create_stock_entry(self, department_data):
	for key, values in department_data.items():
		warehouse = frappe.db.get_value(
			"Warehouse",
			{"department": key, "warehouse_type": "Manufacturing", "disabled": 0},
		)
		stock_data = frappe.db.get_all(
			"Stock Entry Detail",
			{"manufacturing_operation": ["in", values], "t_warehouse": warehouse},
			[
				"item_code",
				"qty",
				"pcs",
				"inventory_type",
				"customer",
				"batch_no",
				"serial_no",
				"basic_rate",
				"manufacturing_operation",
			],
		)

		central_department = frappe.db.get_value(
			"Manufacturer", self.manufacturer, "custom_central_department"
		)

		create_se_entry(self, stock_data, central_department, warehouse)

		self.db_set("sent_for_customer_approval", 1)


def create_se_entry(self, stock_data, central_department, warehouse):
	transit_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"department": central_department,
			"warehouse_type": "Manufacturing",
			"disabled": 0,
		},
		["default_in_transit_warehouse", "name"],
		as_dict=1,
	)

	set_doc = frappe.new_doc("Stock Entry")
	set_doc.stock_entry_type = "Material Transfer to Department"
	set_doc.add_to_transit = 1

	for row in stock_data:
		set_doc.append(
			"items",
			{
				"s_warehouse": warehouse,
				"t_warehouse": transit_warehouse.default_in_transit_warehouse,
				"item_code": row.item_code,
				"pcs": row.pcs,
				"qty": row.qty,
				"basic_rate": row.basic_rate,
				"batch_no": row.get("batch_no"),
				"serial_no": row.get("serial_no"),
				"use_serial_batch_fields": 1,
				"inventory_type": row.inventory_type,
				"customer": row.get("customer"),
				"custom_parent_manufacturing_order": self.name,
				"manufacturing_operation": row.manufacturing_operation,
			},
		)

	set_doc.flags.ignore_permissions = True
	set_doc.save()
	set_doc.submit()

	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = "Material Transfer to Department"
	se_doc.add_to_transit = 0

	for row in stock_data:
		se_doc.append(
			"items",
			{
				"s_warehouse": transit_warehouse.default_in_transit_warehouse,
				"t_warehouse": transit_warehouse.name,
				"item_code": row.item_code,
				"pcs": row.pcs,
				"qty": row.qty,
				"basic_rate": row.basic_rate,
				"batch_no": row.get("batch_no"),
				"serial_no": row.get("serial_no"),
				"use_serial_batch_fields": 1,
				"inventory_type": row.inventory_type,
				"customer": row.get("customer"),
				"custom_parent_manufacturing_order": self.name,
				"custom_is_for_customer_approval": 1,
			},
		)

	se_doc.save()
	se_doc.submit()

	frappe.msgprint(_("{0} Stock Entry has been created").format(se_doc.name))


@frappe.whitelist()
def get_items_for_pmo(source_name, target_doc=None, ignore_permissions=False):
	if isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))

	manufacturer, customer = frappe.db.get_value(
		"Parent Manufacturing Order", source_name, ["manufacturer", "customer"]
	)

	central_department = frappe.db.get_value(
		"Manufacturer", manufacturer, "custom_central_department"
	)

	central_mfg_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"department": central_department,
			"warehouse_type": "Manufacturing",
			"disabled": 0,
		},
	)
	filters = {
		"custom_parent_manufacturing_order": source_name,
		"t_warehouse": central_mfg_warehouse,
	}
	if target_doc.custom_item_type:
		variant_of_dict = {"Gemstone": "G", "Diamond": "D"}
		if variant_of_dict.get(target_doc.custom_item_type):
			filters.update(
				{"custom_variant_of": variant_of_dict.get(target_doc.custom_item_type)}
			)

	stock_entry_details = frappe.db.get_all(
		"Stock Entry Detail",
		filters,
		[
			"item_code",
			"qty",
			"pcs",
			"inventory_type",
			"customer",
			"batch_no",
			"serial_no",
			"basic_rate",
			"manufacturing_operation",
		],
	)

	warehouse = frappe.db.get_value("Customer", customer, "custom_approval_warehouse")

	target_doc.items = []

	for row in stock_entry_details:
		target_doc.append(
			"items",
			{
				"s_warehouse": warehouse
				if target_doc.stock_entry_type
				== "Work Order for Customer Approval Receive"
				else None,
				"t_warehouse": warehouse
				if target_doc.stock_entry_type
				== "Work Order for Customer Approval Issue"
				else None,
				"item_code": row.item_code,
				"pcs": row.pcs,
				"qty": row.qty,
				"basic_rate": row.basic_rate,
				"batch_no": row.get("batch_no"),
				"serial_no": row.get("serial_no"),
				"use_serial_batch_fields": 1,
				"inventory_type": row.inventory_type,
				"customer": row.get("customer"),
				"custom_parent_manufacturing_order": source_name,
			},
		)

	return target_doc
