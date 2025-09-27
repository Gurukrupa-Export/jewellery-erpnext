# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from copy import deepcopy
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.model.naming import make_autoname
from frappe.utils import cint, flt, get_datetime, now
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import get_linked_stock_entries_for_serial_number_creator
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import get_operation_details

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.doc_events.utils import (
	add_time_log,
	create_se_entry,
	create_stock_transfer_entry,
)
from jewellery_erpnext.utils import get_item_from_attribute, set_values_in_bulk


class ManufacturingWorkOrder(Document):
	def autoname(self):
		mfg_purity = frappe.db.get_value(
			"Metal Criteria",
			{"parent": self.manufacturer, "metal_touch": self.metal_touch, "metal_type": self.metal_type},
			"metal_purity",
		)

		if not mfg_purity:
			frappe.throw(_("Metal Purity is not mentioned into Manufacturer."))

		self.metal_purity = mfg_purity

		if self.for_fg:
			self.name = make_autoname("MWO-.abbr.-.item_code.-.seq.-.##", doc=self)
		else:
			color = self.metal_colour.split("+")
			self.color = "".join([word[0] for word in color if word])

	def on_submit(self):
		if self.for_fg:
			self.validate_other_work_orders()

			# new Code
			last_department = frappe.db.get_value("Department Operation", {"is_last_operation": 1, "manufacturer": self.manufacturer}, "department")
			mop_list = frappe.db.get_list("Manufacturing Operation",filters={"department": last_department,"manufacturing_order":self.manufacturing_order},pluck="name")
			if mop_list:
				for mop in mop_list:
					frappe.db.set_value("Manufacturing Operation",mop,"status","Finished")
					
		create_manufacturing_operation(self)
		if self.split_from:
			create_mr_for_split_work_order(self.name,self.company,self.manufacturer)
		# self.start_datetime = now()
		self.db_set("start_datetime", now())
		self.db_set("status", "Not Started")

	def validate_other_work_orders(self):
		# last_department = frappe.db.get_value(
		# 	"Department Operation", {"is_last_operation": 1, "company": self.company}, "department"
		# )
		last_department = frappe.db.get_value(
			"Department Operation", {"is_last_operation": 1, "manufacturer": self.manufacturer}, "department"
		)
		if not last_department:
			frappe.throw(_("Please set last operation first in Department Operation"))
		pending_wo = frappe.get_all(
			"Manufacturing Work Order",
			{
				"name": ["!=", self.name],
				"manufacturing_order": self.manufacturing_order,
				"docstatus": ["!=", 2],
				"department": ["!=", last_department],
				"has_split_mwo": 0,
				"is_finding_mwo": 0,
			},
			"name",
		)
		if pending_wo:
			frappe.throw(
				_("All the pending manufacturing work orders should be in {0}.").format(last_department)
			)

	def on_cancel(self):
		self.db_set("status", "Cancelled")

	@frappe.whitelist()
	def transfer_to_mwo(self):
		create_stock_transfer_entry(self)

	@frappe.whitelist()
	def create_repair_un_pack_stock_entry(self):

		bom_weight = frappe.db.get_value("BOM", self.master_bom, "gross_weight")

		pmo_weight = frappe.db.get_value(
			"Parent Manufacturing Order", self.manufacturing_order, "customer_weight"
		)

		if bom_weight != pmo_weight:
			frappe.throw(_("BOM weight does not match with customer weight"))

		wh = frappe.db.get_value("Manufacturer", self.manufacturer, "custom_repair_warehouse")
		wh_department = frappe.db.get_value("Warehouse", wh, "department")

		target_wh = frappe.get_value(
			"Warehouse",
			{"disabled": 0, "warehouse_type": "Manufacturing", "department": self.department},
			"name",
		)
		if wh_department != self.department:
			frappe.throw(_("For Unpacking allwed warehouse is {0}").format(target_wh))

		parent_entry = frappe.db.get_value("Serial No", self.serial_no, "purchase_document_no")

		raw_item_data = frappe.db.get_all(
			"Stock Entry Detail", {"parent": parent_entry}, ["basic_rate", "item_code"]
		)

		from collections import defaultdict

		row_dict = defaultdict(lambda: {"count": 0, "total_basic_rate": 0, "avg_basic_rate": 0})

		for row in raw_item_data:
			item_code = row.item_code
			row_dict[item_code]["count"] += 1
			row_dict[item_code]["total_basic_rate"] += row.basic_rate
			row_dict[item_code]["avg_basic_rate"] = (
				row_dict[item_code]["total_basic_rate"] / row_dict[item_code]["count"]
			)

		mwo_data = frappe.db.get_all(
			"Manufacturing Work Order",
			{"manufacturing_order": self.manufacturing_order},
			["name", "metal_type", "metal_type", "metal_touch", "metal_purity", "manufacturing_operation"],
		)

		mwo_map = {}

		for row in mwo_data:
			metal_item = get_item_from_attribute(
				row.metal_type, row.metal_touch, row.metal_purity, row.metal_colour
			)
			mwo_map.update({metal_item: {"mwo": row.name, "mop": row.manufacturing_operation}})

		bom_item = frappe.get_doc("BOM", self.master_bom)
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Repair Unpack",
				"purpose": "Repack",
				"company": self.company,
				"inventory_type": "Regular Stock",
				"auto_created": 1,
				"branch": self.branch,
				# "manufacturing_order": self.manufacturing_order,
				# "manufacturing_work_order": self.name,
				# "manufacturing_operation": self.manufacturing_operation,
			}
		)
		source_item = []
		target_item = []
		source_item.append(
			{
				"item_code": self.item_code,
				"qty": self.qty,
				"inventory_type": "Regular Stock",
				"serial_no": self.serial_no,
				"department": self.department,
				"manufacturer": self.manufacturer,
				"use_serial_batch_fields": 1,
				"s_warehouse": wh,
				"gross_weight": bom_item.gross_weight,
				# "custom_manufacturing_work_order": self.name,
				# "manufacturing_operation": self.manufacturing_operation
			}
		)
		for row in bom_item.items:
			row_data = row.__dict__.copy()
			row_data["name"] = None
			row_data["idx"] = None
			row_data["t_warehouse"] = target_wh
			row_data["qty"] = flt((self.qty * row.qty) / bom_item.quantity, 3)
			row_data["inventory_type"] = "Regular Stock"
			row_data["department"] = self.department
			target_item.append(row_data)
		for row in source_item:
			se.append("items", row)
		for row in target_item:
			batch_number_series = frappe.db.get_value("Item", row["item_code"], "batch_number_series")

			batch_doc = frappe.new_doc("Batch")
			batch_doc.item = row["item_code"]

			if batch_number_series:
				batch_doc.batch_id = make_autoname(batch_number_series, doc=batch_doc)

			batch_doc.flags.ignore_permissions = True
			batch_doc.save()
			rate = 0
			if row_dict.get(row["item_code"]) and row_dict[row["item_code"]].get("avg_basic_rate"):
				rate = row_dict[row["item_code"]].get("avg_basic_rate")
			mwo = self.name
			mop = self.manufacturing_operation
			if mwo_map.get(row["item_code"]):
				mwo = mwo_map[row["item_code"]]["mwo"]
				mop = mwo_map[row["item_code"]]["mop"]
			se.append(
				"items",
				{
					"item_code": row["item_code"],
					"qty": row["qty"],
					"inventory_type": row["inventory_type"],
					"t_warehouse": row["t_warehouse"],
					"use_serial_batch_fields": 1,
					"set_basic_rate_manually": 1,
					"basic_rate": rate,
					"batch_no": batch_doc.name,
					"custom_manufacturing_work_order": mwo,
					"manufacturing_operation": mop,
				},
			)

		se.save()
		se.submit()

	@frappe.whitelist()
	def create_mfg_entry(self):
		create_se_entry(self)


def create_manufacturing_operation(doc):
	# timer code
	dt_string = get_datetime()

	mop = get_mapped_doc(
		"Manufacturing Work Order",
		doc.name,
		{
			"Manufacturing Work Order": {
				"doctype": "Manufacturing Operation",
				"field_map": {"name": "manufacturing_work_order"},
			}
		},
	)

	# settings = frappe.db.get_value(
	# 	"Manufacturing Setting",
	# 	{"company": doc.company},
	# 	["default_operation", "default_department"],
	# 	as_dict=1,
	# )
	settings = frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": doc.manufacturer},
		["default_operation", "default_department"],
		as_dict=1,
	)
	department = settings.get("default_department")
	operation = settings.get("default_operation")
	status = "Not Started"

	if doc.for_fg:
		# department, operation = frappe.db.get_value(
		# 	"Department Operation", {"is_last_operation": 1, "company": doc.company}, ["department", "name"]
		# ) or ["", ""]
		department, operation = frappe.db.get_value(
			"Department Operation", {"is_last_operation": 1, "manufacturer": doc.manufacturer}, ["department", "name"]
		) or ["", ""]
		# New Status
		status = "Finished"

	if doc.split_from:
		department = doc.department
		operation = None
	mop.status = status
	mop.type = "Manufacturing Work Order"
	mop.operation = operation
	mop.department = department
	mop.save()
	mop.db_set("employee", None)
	doc.db_set("manufacturing_operation", mop.name)
	values = {"operation": operation}
	values["department_start_time"] = dt_string
	add_time_log(mop, values)

	if doc.for_fg:
		for row in mop.mop_balance_table:
			copy_row = deepcopy(row.__dict__)
			doc.append("mwo_mop_balance_table", copy_row)

		# New Code
		department, operation = frappe.db.get_value("Department Operation", {"is_last_operation": 1, "manufacturer": doc.manufacturer}, ["department", "name"]) or ["", ""]
		data = get_linked_stock_entries_for_serial_number_creator(doc.name,department,doc.item_code,doc.qty)
		get_operation_details(data,mop.name,doc.name,doc.manufacturing_order,doc.company,doc.manufacturer,department,doc.for_fg,doc.master_bom)


@frappe.whitelist()
def create_split_work_order(docname, company,manufacturer, count=1):
	# limit = cint(frappe.db.get_value("Manufacturing Setting", {"company", company}, "wo_split_limit"))
	limit = cint(frappe.db.get_value("Manufacturing Setting", {"manufacturer", manufacturer}, "wo_split_limit"))
	if cint(count) < 1 or (cint(count) > limit and limit > 0):
		frappe.throw(_("Invalid split count"))
	open_operations = frappe.get_all(
		"Manufacturing Operation",
		filters={"manufacturing_work_order": docname},
		or_filters={
			"status": ["not in", ["Finished", "Not Started", "Revert"]],
			"department_ir_status": "In-Transit",
		},
		pluck="name",
	)
	if open_operations:
		frappe.throw(
			f"Following operation should be closed before splitting work order: {', '.join(open_operations)}"
		)
	for i in range(0, cint(count)):
		mop = get_mapped_doc(
			"Manufacturing Work Order",
			docname,
			{
				"Manufacturing Work Order": {
					"doctype": "Manufacturing Work Order",
					"field_map": {"name": "split_from"},
				}
			},
		)
		mop.save()
	pending_operations = frappe.get_all(
		"Manufacturing Operation",
		{"manufacturing_work_order": docname, "status": "Not Started"},
		pluck="name",
	)
	if pending_operations:  # to prevent this workorder from showing in any IR doc
		set_values_in_bulk("Manufacturing Operation", pending_operations, {"status": "Finished"})
	frappe.db.set_value("Manufacturing Work Order", docname, {"has_split_mwo": 1, "status": "Closed"})
	# frappe.db.set_value("Manufacturing Work Order", docname, "status", "Closed")
	pmo = frappe.db.get_value("Manufacturing Work Order",docname,"manufacturing_order")
	mr_list = frappe.db.get_list("Material Request",filters={"manufacturing_order":pmo,"title": ["like", "MRD%"],"custom_manufacturing_work_order":["is","not set"]},fields=["name"])
	if mr_list:
		for mr in mr_list:
			frappe.db.set_value("Material Request",mr.name,"docstatus","2")
			frappe.db.set_value("Material Request",mr.name,"workflow_state","Cancelled")


@frappe.whitelist()
def get_linked_stock_entries(mwo_name):  # MWO Details Tab code
	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
	StockEntry = frappe.qb.DocType("Stock Entry")

	query = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(StockEntry)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(
			StockEntry.manufacturing_operation,
			StockEntry.name,
			StockEntryDetail.item_code,
			StockEntryDetail.item_name,
			StockEntryDetail.qty,
			StockEntryDetail.uom,
		)
		.where((StockEntry.docstatus == 1) &
		(StockEntry.manufacturing_work_order == mwo_name))
		.orderby(StockEntry.modified, order=frappe.qb.asc)
	)

	data = query.run(as_dict=True)

	total_qty = len([item["name"] for item in data])
	return frappe.render_template(
		"jewellery_erpnext/jewellery_erpnext/doctype/manufacturing_work_order/stock_entry_details.html",
		{"data": data, "total_qty": total_qty},
	)

def create_mr_for_split_work_order(docname, company, manufacturer):
	pmo = frappe.db.get_value("Manufacturing Work Order",docname,"manufacturing_order")
	mr_list = frappe.db.get_value("Material Request",{"manufacturing_order":pmo,"title": ["like", "MRD%"]},"name")
	total_mr_count = frappe.db.count("Material Request", filters={"manufacturing_order": pmo})
	old_mr = frappe.get_doc("Material Request",mr_list)
	new_mr = frappe.copy_doc(old_mr)
	new_mr.workflow_state = 'Draft'
	new_mr.title = new_mr.title[:-1] + str(int(total_mr_count) + 1) 
	new_mr.custom_manufacturing_work_order = docname
	new_mr_items = []
	for i in new_mr.items:
		i.qty = 0
		i.pcs = 0
		new_mr_items.append(i)
	new_mr.items = []
	new_mr.items = new_mr_items
	new_mr.flags.ignore_mandatory = True
	new_mr.flags.ignore_validate = True
	new_mr.save()
	frappe.msgprint(f"Material Request is creted !!")


