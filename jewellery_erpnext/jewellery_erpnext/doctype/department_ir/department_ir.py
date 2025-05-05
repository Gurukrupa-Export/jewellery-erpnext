# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _, scrub
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.query_builder import CustomFunction
from frappe.query_builder.functions import IfNull, Sum
from frappe.utils import flt, get_datetime
from jewellery_erpnext.utils import group_aggregate_with_concat

from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	update_manufacturing_operation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.department_ir_utils import (
	get_summary_data,
	validate_and_update_gross_wt_from_mop,
	valid_reparing_or_next_operation,
	validate_mwo,
	validate_tolerance,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	get_previous_operation,
)
from jewellery_erpnext.utils import set_values_in_bulk


class DepartmentIR(Document):
	def before_validate(self):
		if self.docstatus != 1:

			if self.company != frappe.db.get_value("Department", self.current_department, "company"):
				frappe.throw(_("{0} does not belongs to {1}").format(self.current_department, self.company))

			other_department = self.previous_department or self.next_department
			if self.company != frappe.db.get_value("Department", other_department, "company"):
				frappe.throw(_("{0} does not belongs to {1}").format(other_department, self.company))

			warehouse = frappe.db.get_value(
				"Warehouse",
				{"disabled": 0, "department": self.current_department, "warehouse_type": "Manufacturing"},
			)
			if not warehouse:
				frappe.throw(_("MFG Warehouse not available for department"))
			if frappe.db.get_value(
				"Stock Reconciliation",
				{"set_warehouse": warehouse, "workflow_state": ["in", ["In Progress", "Send for Approval"]]},
			):
				frappe.throw(_("Stock Reconciliation is under process"))
			mwo_list = validate_and_update_gross_wt_from_mop(self)
			valid_reparing_or_next_operation(self, mwo_list)

		validate_mwo(self)

	@frappe.whitelist()
	def get_operations(self):
		dir_status = "In-Transit" if self.type == "Receive" else ["not in", ["In-Transit", "Received"]]
		filters = {"department_ir_status": dir_status}
		if self.type == "Issue":
			filters["status"] = ["in", ["Finished", "Revert"]]
			filters["department"] = self.current_department
		records = frappe.get_list("Manufacturing Operation", filters, ["name", "gross_wt"])
		self.department_ir_operation = []
		if records:
			for row in records:
				self.append("department_ir_operation", {"manufacturing_operation": row.name})

	def before_submit(self):
		if not self.department_ir_operation:
			frappe.throw("Add row in <b>Department IR Operations Table</b>")

		if self.type == 'Receive' and not self.receive_against:
			frappe.throw("<b>Receive Against</b> is not set for this Receive entry")

	def on_submit(self):
		if self.type == "Issue":
			self.on_submit_issue_new()
		else:
			self.on_submit_receive()

	def on_cancel(self):
		if self.type == "Issue":
			self.on_submit_issue_new(cancel=True)
		else:
			self.on_submit_receive(cancel=True)

	# for Receive
	def on_submit_receive(self, cancel=False):
		import copy

		values = {}
		values["department_receive_id"] = self.name
		values["department_ir_status"] = "Received"

		se_item_list = []
		dt_string = get_datetime()

		in_transit_wh = frappe.db.get_value(
			"Warehouse",
			{"disabled": 0, "department": self.current_department, "warehouse_type": "Manufacturing"},
			"default_in_transit_warehouse",
		)

		department_wh = frappe.get_value(
			"Warehouse",
			{"disabled": 0, "department": self.current_department, "warehouse_type": "Manufacturing"},
		)
		for row in self.department_ir_operation:

			for se_item in frappe.db.get_all(
				"Stock Entry Detail",
				{
					"manufacturing_operation": ["like", f"%{row.manufacturing_operation}%"],
					"t_warehouse": in_transit_wh,
					"department": self.previous_department,
					"to_department": self.current_department,
					"docstatus": 1,
				},
				["*"],
			):
				temp_row = copy.deepcopy(se_item)
				temp_row["name"] = None
				temp_row["idx"] = None
				temp_row["s_warehouse"] = in_transit_wh
				temp_row["t_warehouse"] = department_wh
				temp_row["serial_and_batch_bundle"] = None
				temp_row["main_slip"] = None
				temp_row["employee"] = None
				temp_row["to_main_slip"] = None
				temp_row["to_employee"] = None
				se_item_list += [temp_row]

			if cancel:
				values.update({"department_receive_id": None, "department_ir_status": "In-Transit"})
			frappe.db.set_value("Manufacturing Operation", row.manufacturing_operation, values)
			frappe.db.set_value(
				"Manufacturing Work Order", row.manufacturing_work_order, "department", self.current_department
			)

			doc = frappe.get_doc("Manufacturing Operation", row.manufacturing_operation)
			doc.set("department_time_logs", [])
			doc.save()
			time_values = copy.deepcopy(values)
			time_values["department_start_time"] = dt_string
			add_time_log(doc, time_values)
		if not se_item_list:
			frappe.msgprint(_("No Stock Entries were generated during this Department IR"))
			return
		if not cancel:
			stock_doc = frappe.new_doc("Stock Entry")
			stock_doc.update(
				{
					"stock_entry_type": "Material Transfer to Department",
					"company": self.company,
					"department_ir": self.name,
					"auto_created": True,
					"add_to_transit": 0,
					"inventory_type": None,
				}
			)

			for row in se_item_list:
				stock_doc.append("items", row)
			stock_doc.flags.ignore_permissions = True
			stock_doc.save()
			stock_doc.submit()

		if cancel:
			se_list = frappe.db.get_list("Stock Entry", {"department_ir": self.name})
			for row in se_list:
				se_doc = frappe.get_doc("Stock Entry", row.name)
				se_doc.cancel()

			for row in self.department_ir_operation:
				frappe.db.set_value(
					"Manufacturing Operation", row.manufacturing_operation, "status", "Not Started"
				)

	# for Issue
	def on_submit_issue(self, cancel=False):
		dt_string = get_datetime()
		status = "Not Started" if cancel else "Finished"
		values = {"status": status}

		mop_data = frappe._dict({})
		for row in self.department_ir_operation:
			if cancel:
				new_operation = frappe.db.get_value(
					"Manufacturing Operation",
					{"department_issue_id": self.name, "manufacturing_work_order": row.manufacturing_work_order},
				)
				se_list = frappe.db.get_list("Stock Entry", {"department_ir": self.name})
				for se in se_list:
					se_doc = frappe.get_doc("Stock Entry", se.name)
					if se_doc.docstatus == 1:
						se_doc.cancel()

					frappe.db.set_value(
						"Stock Entry Detail", {"parent": se.name}, "manufacturing_operation", None
					)

				frappe.db.set_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					"manufacturing_operation",
					row.manufacturing_operation,
				)
				if new_operation:
					frappe.db.set_value(
						"Department IR Operation",
						{"docstatus": 2, "manufacturing_operation": new_operation},
						"manufacturing_operation",
						None,
					)
					frappe.db.set_value(
						"Stock Entry Detail",
						{"docstatus": 2, "manufacturing_operation": new_operation},
						"manufacturing_operation",
						None,
					)
					frappe.delete_doc("Manufacturing Operation", new_operation, ignore_permissions=1)
				frappe.db.set_value(
					"Manufacturing Operation", row.manufacturing_operation, "status", "In Transit"
				)

			else:
				values["complete_time"] = dt_string
				new_operation = create_operation_for_next_dept(
					self.name, row.manufacturing_work_order, row.manufacturing_operation, self.next_department
				)
				update_stock_entry_dimensions(self, row, new_operation)
				# create_stock_entry_for_issue(self, row, new_operation)
				frappe.db.set_value(
					"Manufacturing Operation", row.manufacturing_operation, "status", "Finished"
				)
				doc = frappe.get_doc("Manufacturing Operation", row.manufacturing_operation)
				# new_operation_name = f"{doc.name}-{i}"
				mop_data.update(
					{
						row.manufacturing_work_order: {
							"cur_mop": row.manufacturing_operation,
							"new_mop": new_operation,
						}
					}
				)
				# i += 1
				# new_operation_data.append(
				# 	(new_operation_name, "Not Started", doc.company, doc.department, doc.manufacturer, doc.manufacturing_work_order, doc.manufacturing_order, doc.name, doc.qty, doc.item_code, doc.design_id_bom, doc.metal_type, doc.metal_colour, doc.metal_touch, doc.metal_purity, frappe.utils.now(), frappe.utils.now())
				# )
				add_time_log(doc, values)

		# fields = ["name", "status", "company", "department", "manufacturer", "manufacturing_work_order", "manufacturing_order", "previous_mop", "qty", "item_code", "design_id_bom", "metal_type", "metal_colour", "metal_touch", "metal_purity", "creation", "modified"]
		# frappe.db.bulk_insert("Manufacturing Operation", fields=fields, values=set(new_operation_data))
		add_to_transit = []
		strat_transit = []
		if mop_data and not cancel:
			in_transit_wh = frappe.get_value(
				"Warehouse",
				{"department": self.next_department, "warehouse_type": "Manufacturing"},
				"default_in_transit_warehouse",
			)

			department_wh, send_in_transit_wh = frappe.get_value(
				"Warehouse",
				{"disabled": 0, "department": self.current_department, "warehouse_type": "Manufacturing"},
				["name", "default_in_transit_warehouse"],
			)
			if not department_wh:
				frappe.throw(_("Please set warhouse for department {0}").format(self.current_department))

			for row in mop_data:
				lst1, lst2 = get_se_items(
					self, row, mop_data[row], in_transit_wh, send_in_transit_wh, department_wh
				)
				add_to_transit += lst1
				strat_transit += lst2

			if add_to_transit:
				stock_doc = frappe.new_doc("Stock Entry")
				stock_doc.stock_entry_type = "Material Transfer to Department"
				stock_doc.company = self.company
				stock_doc.department_ir = self.name
				stock_doc.auto_created = True
				stock_doc.add_to_transit = 1
				stock_doc.inventory_type = None

				for row in add_to_transit:
					stock_doc.append("items", row)

				stock_doc.flags.ignore_permissions = True
				# stock_doc.save()
				stock_doc.submit()

				stock_doc = frappe.new_doc("Stock Entry")
				stock_doc.stock_entry_type = "Material Transfer to Department"
				stock_doc.company = self.company
				stock_doc.department_ir = self.name
				stock_doc.auto_created = True
				stock_doc.inventory_type = None

				for row in add_to_transit:
					if row["qty"] > 0:
						row["t_warehouse"] = department_wh
						row["s_warehouse"] = in_transit_wh
						stock_doc.append("items", row)

				stock_doc.flags.ignore_permissions = True
				# stock_doc.save()
				stock_doc.submit()

			if strat_transit:
				stock_doc = frappe.new_doc("Stock Entry")
				stock_doc.stock_entry_type = "Material Transfer to Department"
				stock_doc.company = self.company
				stock_doc.department_ir = self.name
				stock_doc.auto_created = True
				for row in strat_transit:
					if row["qty"] > 0:
						stock_doc.append("items", row)
				stock_doc.flags.ignore_permissions = True
				# stock_doc.save()
				stock_doc.submit()

	def on_submit_issue_new(self, cancel=False):
		dt_string = get_datetime()
		status = "Not Started" if cancel else "Finished"
		values = {"status": status}

		mop_data = frappe._dict({})
		stock_entry_data = []  # Accumulate data for batch update

		for row in self.department_ir_operation:
			if cancel:
				new_operation = frappe.db.get_value(
					"Manufacturing Operation",
					{"department_issue_id": self.name, "manufacturing_work_order": row.manufacturing_work_order},
				)
				se_list = frappe.db.get_list("Stock Entry", {"department_ir": self.name})
				for se in se_list:
					se_doc = frappe.get_doc("Stock Entry", se.name)
					if se_doc.docstatus == 1:
						se_doc.cancel()

					frappe.db.set_value(
						"Stock Entry Detail", {"parent": se.name}, "manufacturing_operation", None
					)

				frappe.db.set_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					"manufacturing_operation",
					row.manufacturing_operation,
				)
				if new_operation:
					frappe.db.set_value(
						"Department IR Operation",
						{"docstatus": 2, "manufacturing_operation": new_operation},
						"manufacturing_operation",
						None,
					)
					frappe.db.set_value(
						"Stock Entry Detail",
						{"docstatus": 2, "manufacturing_operation": new_operation},
						"manufacturing_operation",
						None,
					)
					frappe.delete_doc("Manufacturing Operation", new_operation, ignore_permissions=1)
				frappe.db.set_value(
					"Manufacturing Operation", row.manufacturing_operation, "status", "In Transit"
				)

			else:
				values["complete_time"] = dt_string
				new_operation = create_operation_for_next_dept(
					self.name, row.manufacturing_work_order, row.manufacturing_operation, self.next_department
				)
				# Accumulate data for batch update instead of calling the function here
				stock_entry_data.append((row.manufacturing_work_order, new_operation))

				frappe.db.set_value(
					"Manufacturing Operation", row.manufacturing_operation, "status", "Finished"
				)
				doc = frappe.get_cached_doc("Manufacturing Operation", row.manufacturing_operation)
				mop_data.update(
					{
						row.manufacturing_work_order: {
							"cur_mop": row.manufacturing_operation,
							"new_mop": new_operation,
						}
					}
				)
				add_time_log(doc, values)

		# Batch update the stock entry dimensions
		if stock_entry_data and not cancel:
			batch_update_stock_entry_dimensions(self, stock_entry_data, employee=None, for_employee=False)

		add_to_transit = []
		strat_transit = []
		if mop_data and not cancel:
			in_transit_wh = frappe.get_value(
				"Warehouse",
				{"department": self.next_department, "warehouse_type": "Manufacturing"},
				"default_in_transit_warehouse",
			)

			department_wh, send_in_transit_wh = frappe.get_value(
				"Warehouse",
				{"disabled": 0, "department": self.current_department, "warehouse_type": "Manufacturing"},
				["name", "default_in_transit_warehouse"],
			)
			if not department_wh:
				frappe.throw(_("Please set warehouse for department {0}").format(self.current_department))

			for row in mop_data:
				lst1, lst2 = get_se_items(
					self, row, mop_data[row], in_transit_wh, send_in_transit_wh, department_wh
				)
				add_to_transit += lst1
				strat_transit += lst2

			if add_to_transit:
				stock_doc = frappe.new_doc("Stock Entry")
				stock_doc.stock_entry_type = "Material Transfer to Department"
				stock_doc.company = self.company
				stock_doc.department_ir = self.name
				stock_doc.auto_created = True
				stock_doc.add_to_transit = 1
				stock_doc.inventory_type = None

				for row in add_to_transit:
					stock_doc.append("items", row)

				stock_doc.flags.ignore_permissions = True
				stock_doc.save()
				stock_doc.submit()

				stock_doc = frappe.new_doc("Stock Entry")
				stock_doc.stock_entry_type = "Material Transfer to Department"
				stock_doc.company = self.company
				stock_doc.department_ir = self.name
				stock_doc.auto_created = True
				stock_doc.inventory_type = None

				for row in add_to_transit:
					if row["qty"] > 0:
						row["t_warehouse"] = department_wh
						row["s_warehouse"] = in_transit_wh
						stock_doc.append("items", row)

				stock_doc.flags.ignore_permissions = True
				stock_doc.save()
				stock_doc.submit()

			if strat_transit:
				group_keys = ["item_code", "batch_no"]
				sum_keys = ["qty", "transfer_qty", "pcs"]
				concat_keys = ["custom_parent_manufacturing_order", "custom_manufacturing_work_order", "manufacturing_operation"]
				grouped_items = group_aggregate_with_concat(strat_transit, group_keys, sum_keys, concat_keys)

				stock_doc = frappe.new_doc("Stock Entry")
				stock_doc.stock_entry_type = "Material Transfer to Department"
				stock_doc.company = self.company
				stock_doc.department_ir = self.name
				stock_doc.auto_created = True
				for row in grouped_items:
					if row["qty"] > 0:
						stock_doc.append("items", row)
				stock_doc.flags.ignore_permissions = True
				stock_doc.save()
				stock_doc.submit()

	def group_se_items(self, se_items:list):
		pass

	@frappe.whitelist()
	def get_summary_data(self):
		return get_summary_data(self)

	@frappe.whitelist()
	def get_manufacturing_operations_from_department_ir(self, docname):
		self.department_ir_operation = []
		for row in frappe.get_all(
			"Manufacturing Operation",
			{"department_issue_id": docname, "department_ir_status": "In-Transit"},
			[
				"name as manufacturing_operation",
				"manufacturing_work_order",
				"prev_gross_wt as gross_wt",
				"previous_mop",
				"department",
			],
		):
			self.current_department = row.department
			mop_details = frappe.db.get_value(
				"Manufacturing Operation",
				row.previous_mop,
				[
					"diamond_wt",
					"net_wt",
					"finding_wt",
					"diamond_pcs",
					"gemstone_pcs",
					"gemstone_wt",
					"other_wt",
					"department",
				],
				as_dict=1,
			)
			self.previous_department = mop_details.get("department")
			self.append(
				"department_ir_operation",
				{
					"manufacturing_operation": row.manufacturing_operation,
					"manufacturing_work_order": row.manufacturing_work_order,
					"gross_wt": row.gross_wt,
					"net_wt": mop_details.get("net_wt"),
					"diamond_wt": mop_details.get("diamond_wt"),
					"finding_wt": mop_details.get("finding_wt"),
					"diamond_pcs": mop_details.get("diamond_pcs"),
					"gemstone_pcs": mop_details.get("gemstone_pcs"),
					"gemstone_wt": mop_details.get("gemstone_wt"),
					"other_wt": mop_details.get("other_wt"),
				},
			)


def get_se_items(doc, mwo, mop_data, in_transit_wh, send_in_transit_wh, department_wh):
	lst1 = []
	lst2 = []
	import copy

	balance_data = frappe._dict()
	department = doc.next_department or doc.current_department
	apply_tolerance = frappe.db.get_value("Department", department, "custom_apply_product_tolerance")

	for row in frappe.db.get_all("MOP Balance Table", {"parent": mop_data["cur_mop"]}, ["*"]):
		temp_row = copy.deepcopy(row)
		if apply_tolerance:
			variant_of = frappe.db.get_value("Item", temp_row.item_code, "variant_of")

			extra_attribute1 = None
			extra_attribute2 = None

			if variant_of in ["M", "F"]:
				variant_of = "MF"
				attribute = "Metal Type"
			elif variant_of == "D":
				attribute = "Diamond Type"
				extra_attribute1 = "Diamond Sieve Size"
				extra_attribute2 = "Diamond Sieve Size Range"
			elif variant_of == "G":
				attribute = "Gemstone Type"
				extra_attribute1 = "Stone Shape"
			if attribute:
				extra_type = None
				extra_type1 = None
				item_type = frappe.db.get_value(
					"Item Variant Attribute",
					{"parent": temp_row.item_code, "attribute": attribute},
					"attribute_value",
				)
				if extra_attribute1:
					extra_type = frappe.db.get_value(
						"Item Variant Attribute",
						{"parent": temp_row.item_code, "attribute": extra_attribute1},
						"attribute_value",
					)
					if extra_type:
						balance_data.setdefault((variant_of, extra_type, item_type), 0)
						balance_data[(variant_of, extra_type, item_type)] += temp_row.qty
				if extra_attribute2:
					if extra_attribute2 == "Diamond Sieve Size Range" and extra_type:
						extra_type1 = frappe.db.get_value("Attribute Value", extra_type, "sieve_size_range")
					else:
						extra_type1 = frappe.db.get_value(
							"Item Variant Attribute",
							{"parent": temp_row.item_code, "attribute": extra_attribute2},
							"attribute_value",
						)
					if extra_type1:
						balance_data.setdefault((variant_of, extra_type1, item_type), 0)
						balance_data[(variant_of, extra_type1, item_type)] += temp_row.qty
				if not extra_type and not extra_type1:
					balance_data.setdefault((variant_of, item_type), 0)
					balance_data[(variant_of, item_type)] += temp_row.qty

		temp_row["name"] = None
		temp_row["idx"] = None
		s_warehouse = row.s_warehouse
		temp_row["t_warehouse"] = in_transit_wh
		temp_row["s_warehouse"] = department_wh
		temp_row["manufacturing_operation"] = mop_data["new_mop"]
		temp_row["department"] = doc.current_department
		temp_row["to_department"] = doc.next_department
		temp_row["use_serial_batch_fields"] = True
		temp_row["serial_and_batch_bundle"] = None
		temp_row["main_slip"] = None
		temp_row["to_main_slip"] = None
		temp_row["employee"] = None
		temp_row["to_employee"] = None
		temp_row["custom_manufacturing_work_order"] = mwo

		if s_warehouse == send_in_transit_wh:
			lst1.append(temp_row)
		elif s_warehouse == department_wh:
			lst2.append(temp_row)

	if apply_tolerance:
		doc.flags.metal_inculded = False
		doc.flags.diamond_inculded = False
		doc.flags.gemstone_inculded = False
		tolerance_data = validate_tolerance(doc, mop_data)
		for row in balance_data:
			data = []
			if tolerance_data.get(row[1]):
				if row[0] == "MF":
					doc.flags.metal_inculded = True
					range_variables = ["from_weight", "to_weight"]
				if row[0] in ["D", "G"]:
					if row[0] == "D":
						doc.flags.diamond_inculded = True
					if row[0] == "G":
						doc.flags.gemstone_inculded = True
					range_variables = ["from_diamond", "to_diamond"]
				data = tolerance_data[row[1]]

			elif len(row) > 2 and tolerance_data.get(row[2]):
				if row[0] in ["D", "G"]:
					if row[0] == "D":
						doc.flags.diamond_inculded = True
					if row[0] == "G":
						doc.flags.gemstone_inculded = True
					range_variables = ["from_diamond", "to_diamond"]
					data = tolerance_data[row[2]]
			else:
				if row[0] == "MF" and tolerance_data.get("Gold"):
					doc.flags.metal_inculded = True
					range_variables = ["from_weight", "to_weight"]
					data = tolerance_data["Gold"]
				elif row[0] == "D" and tolerance_data.get("Diamond"):
					doc.flags.diamond_inculded = True
					range_variables = ["from_diamond", "to_diamond"]
					data = tolerance_data["Diamond"]
				elif row[0] == "G" and tolerance_data.get("Gemstone"):
					doc.flags.gemstone_inculded = True
					range_variables = ["from_diamond", "to_diamond"]
					data = tolerance_data["Gemstone"]

			mwo_qty = frappe.db.get_value("Manufacturing Work Order", mwo, "qty")

			for t_data in data:
				if t_data.get(range_variables[0]) != t_data.get(range_variables[1]):
					if t_data.get(range_variables[0]) <= t_data.bom_qty <= t_data.get(range_variables[1]):
						if t_data.range_type == "Percentage" or range_variables[0] == "from_diamond":
							upper_limit = flt((t_data.plus_percent * t_data.bom_qty) / 100, 3)
							lower_limit = flt((t_data.minus_percent * t_data.bom_qty) / 100, 3)
						else:
							upper_limit = flt(t_data.plus_percent + t_data.bom_qty, 3)
							lower_limit = flt(t_data.minus_percent - t_data.bom_qty, 3)

						plus_tolerance = flt(t_data.bom_qty + upper_limit, 3) * mwo_qty
						minus_tolerance = flt(t_data.bom_qty - lower_limit, 3) * mwo_qty

						if not minus_tolerance <= balance_data[row] <= plus_tolerance:
							frappe.throw(
								_("Quantity is {0} but it should be between {1} to {2} for {3} in {4}").format(
									flt(balance_data[row], 3), minus_tolerance, plus_tolerance, row[0], mwo
								)
							)

				else:
					if t_data.range_type == "Percentage" or range_variables[0] == "from_diamond":
						upper_limit = flt((t_data.plus_percent * t_data.bom_qty) / 100, 3)
						lower_limit = flt((t_data.minus_percent * t_data.bom_qty) / 100, 3)
					else:
						upper_limit = flt(t_data.plus_percent + t_data.bom_qty, 3)
						lower_limit = flt(t_data.minus_percent - t_data.bom_qty, 3)

					plus_tolerance = flt(t_data.bom_qty + upper_limit, 3) * mwo_qty
					minus_tolerance = flt(t_data.bom_qty - lower_limit, 3) * mwo_qty

					if not minus_tolerance <= balance_data[row] <= plus_tolerance:
						frappe.throw(
							_("Quantity is {0} but it should be between {1} to {2} for {3} in {4}").format(
								flt(balance_data[row], 3), minus_tolerance, plus_tolerance, row[1], mwo
							)
						)
		if not doc.flags.metal_inculded and tolerance_data.get("metal_included"):
			frappe.throw(_("Metal not available in the entry in {0}").format(mwo))

		if not doc.flags.diamond_inculded and tolerance_data.get("diamond_included"):
			frappe.throw(_("Diamond not available in the entry in {0}").format(mwo))

		if not doc.flags.gemstone_inculded and tolerance_data.get("gemstone_included"):
			frappe.throw(_("Gemstone not available in the entry in {0}").format(mwo))

	return lst1, lst2


def update_stock_entry_dimensions(doc, row, manufacturing_operation, for_employee=False):
	filters = {}
	if for_employee:
		filters["employee" if doc.type == "Receive" else "to_employee"] = doc.employee
		current_dep = doc.department
		next_dep = doc.department
	else:
		current_dep = doc.current_department
		next_dep = doc.next_department
	filters.update(
		{
			"manufacturing_work_order": row.manufacturing_work_order,
			"docstatus": 1,
			"manufacturing_operation": ["is", "not set"],
			"department": current_dep,
			"to_department": next_dep,
		}
	)
	stock_entries = frappe.db.get_all("Stock Entry", filters=filters, pluck="name")
	values = {"manufacturing_operation": manufacturing_operation}
	for stock_entry in stock_entries:
		rows = frappe.db.get_all("Stock Entry Detail", {"parent": stock_entry}, pluck="name")
		set_values_in_bulk("Stock Entry Detail", rows, values)
		values[scrub(doc.doctype)] = doc.name
		frappe.db.set_value("Stock Entry", stock_entry, values)
		update_manufacturing_operation(stock_entry)
		del values[scrub(doc.doctype)]


def batch_update_stock_entry_dimensions(doc, stock_entry_data, employee, for_employee=False):
	"""
	Batch update Stock Entry and Stock Entry Detail with manufacturing_operation using ORM.
	stock_entry_data: List of (manufacturing_work_order, manufacturing_operation) tuples.
	"""
	# Prepare filters
	if for_employee:
		emp_field = "employee" if doc.type == "Receive" else "to_employee"
		filters = {emp_field: employee}
		current_dep = doc.department
		next_dep = doc.department
	else:
		filters = {}
		current_dep = doc.current_department
		next_dep = doc.next_department

	# Batch fetch all matching Stock Entries
	mwo_list = [d[0] for d in stock_entry_data]
	filters.update({
		"manufacturing_work_order": ["in", mwo_list],
		"docstatus": 1,
		"manufacturing_operation": ["is", "not set"],
		"department": current_dep,
		"to_department": next_dep
	})
	stock_entries = frappe.db.get_all("Stock Entry", filters=filters, pluck="name")

	if not stock_entries:
		return

	# Map manufacturing_operation to Stock Entry names
	mwo_to_mop = dict(stock_entry_data)
	se_updates = {}
	sed_updates = {}

	# Fetch all Stock Entry Detail rows in one query
	sed_rows = frappe.db.get_all(
		"Stock Entry Detail",
		filters={"parent": ["in", stock_entries]},
		fields=["name", "parent", "manufacturing_operation"]
	)

	# Prepare batch updates
	for se in stock_entries:
		mop = mwo_to_mop.get(frappe.db.get_value("Stock Entry", se, "manufacturing_work_order"))
		if mop:
			se_updates[se] = {
				"manufacturing_operation": mop,
				scrub(doc.doctype): doc.name
			}

	for sed in sed_rows:
		mop = se_updates.get(sed.parent, {}).get("manufacturing_operation")
		if mop:
			mop_list = sed.manufacturing_operation.split(",") if sed.manufacturing_operation else mop + ","
			sed_updates[sed.name] = {"manufacturing_operation": mop_list}

	# Batch update Stock Entry
	if se_updates:
		frappe.db.bulk_update("Stock Entry", se_updates, chunk_size=150, update_modified=True)
		for se_name in se_updates:
			update_manufacturing_operation(se_name)

	# Batch update Stock Entry Detail
	if sed_updates:
		frappe.db.bulk_update("Stock Entry Detail", sed_updates, chunk_size=150, update_modified=True)


# def create_stock_entry_for_issue(doc, row, manufacturing_operation):

# 	in_transit_wh = frappe.get_value(
# 		"Warehouse",
# 		{"disabled": 0, "department": doc.next_department, "warehouse_type": "Manufacturing"},
# 		"default_in_transit_warehouse",
# 	)

# 	department_wh = frappe.get_value(
# 		"Warehouse",
# 		{"disabled": 0, "department": doc.current_department, "warehouse_type": "Manufacturing"},
# 	)
# 	if not department_wh:
# 		# frappe.throw(_(f"Please set warhouse for department {doc.current_department}"))
# 		frappe.throw(_("Please set warehouse for department {0}").format(doc.current_department))

# 	send_in_transit_wh = frappe.get_value(
# 		"Warehouse",
# 		{"disabled": 0, "department": doc.current_department, "warehouse_type": "Manufacturing"},
# 		"default_in_transit_warehouse",
# 	)

# 	## make filter to fetch the stock entry created against warehouse and operations
# 	SE = frappe.qb.DocType("Stock Entry")
# 	SED = frappe.qb.DocType("Stock Entry Detail")

# 	fetch_manual_stock_entries = (
# 		frappe.qb.from_(SE)
# 		.left_join(SED)
# 		.on(SE.name == SED.parent)
# 		.select(SE.name)
# 		.where(
# 			(SED.t_warehouse == send_in_transit_wh)
# 			& (SED.manufacturing_operation == row.manufacturing_operation)
# 			& (SED.to_department == doc.current_department)
# 			& (SED.docstatus == 1)
# 			& (SE.auto_created == 0)
# 		)
# 		.groupby(SE.name)
# 	).run(pluck="name")

# 	stock_entries = (
# 		frappe.qb.from_(SED)
# 		.left_join(SE)
# 		.on(SED.parent == SE.name)
# 		.select(SE.name)
# 		.where(
# 			(SE.auto_created == 1)
# 			& (SE.docstatus == 1)
# 			& (SED.manufacturing_operation == row.manufacturing_operation)
# 			& (SED.t_warehouse == department_wh)
# 			& (SED.to_department == doc.current_department)
# 		)
# 		.groupby(SE.name)
# 		.orderby(SE.posting_date)
# 	).run(as_dict=1, pluck=1)

# 	non_automated_entries = []
# 	if not stock_entries:
# 		non_automated_entries = (
# 			frappe.qb.from_(SED)
# 			.left_join(SE)
# 			.on(SED.parent == SE.name)
# 			.select(SE.name)
# 			.where(
# 				(SE.auto_created == 0)
# 				& (SE.docstatus == 1)
# 				& (SED.manufacturing_operation == row.manufacturing_operation)
# 				& (SED.t_warehouse == department_wh)
# 				& (SED.to_department == doc.current_department)
# 			)
# 			.groupby(SE.name)
# 			.orderby(SE.posting_date)
# 		).run(as_dict=1, pluck=1)

# 		prev_mfg_operation = get_previous_operation(row.manufacturing_operation)
# 		in_transit_wh = frappe.get_value(
# 			"Warehouse",
# 			{"disabled": 0, "department": doc.next_department, "warehouse_type": "Manufacturing"},
# 			"default_in_transit_warehouse",
# 		)
# 		stock_entries = frappe.get_all(
# 			"Stock Entry Detail",
# 			filters={
# 				"manufacturing_operation": prev_mfg_operation,
# 				"t_warehouse": department_wh,
# 				"to_department": doc.current_department,
# 				"docstatus": 1,
# 			},
# 			or_filters={"employee": ["is", "set"], "subcontractor": ["is", "set"]},
# 			pluck="parent",
# 			group_by="parent",
# 		)

# 	for stock_entry in fetch_manual_stock_entries:
# 		end_transit(doc, send_in_transit_wh, department_wh, manufacturing_operation, stock_entry)
# 		start_transit(doc, in_transit_wh, department_wh, manufacturing_operation, stock_entry)

# 	for stock_entry in stock_entries + non_automated_entries:
# 		start_transit(doc, in_transit_wh, department_wh, manufacturing_operation, stock_entry)


# def start_transit(doc, in_transit_wh, department_wh, manufacturing_operation, stock_entry):
# 	existing_doc = frappe.get_doc("Stock Entry", stock_entry)
# 	se_doc = frappe.copy_doc(existing_doc)
# 	se_doc.stock_entry_type = "Material Transfer to Department"
# 	se_doc.from_warehouse = None
# 	se_doc.to_warehouse = None
# 	for child in se_doc.items:
# 		child.t_warehouse = in_transit_wh
# 		child.s_warehouse = department_wh
# 		child.material_request = None
# 		child.material_request_item = None
# 		child.manufacturing_operation = manufacturing_operation
# 		child.department = doc.current_department
# 		child.to_department = doc.next_department
# 		child.to_main_slip = None
# 		child.main_slip = None
# 		child.employee = None
# 		child.to_employee = None
# 		child.subcontractor = None
# 		child.to_subcontractor = None
# 		child.use_serial_batch_fields = True
# 		child.serial_and_batch_bundle = None

# 	se_doc.to_main_slip = None
# 	se_doc.main_slip = None
# 	se_doc.employee = None
# 	se_doc.to_employee = None
# 	se_doc.subcontractor = None
# 	se_doc.to_subcontractor = None
# 	se_doc.department = doc.current_department
# 	se_doc.to_department = doc.next_department
# 	se_doc.department_ir = doc.name
# 	se_doc.manufacturing_operation = manufacturing_operation
# 	se_doc.auto_created = True
# 	se_doc.add_to_transit = 1
# 	se_doc.flags.ignore_permissions = True
# 	se_doc.save()
# 	se_doc.submit()


# def end_transit(doc, in_transit_wh, department_wh, manufacturing_operation, stock_entry):

# 	existing_doc = frappe.get_doc("Stock Entry", stock_entry)
# 	se_doc = frappe.copy_doc(existing_doc)
# 	se_doc.stock_entry_type = "Material Transfer to Department"
# 	se_doc.from_warehouse = None
# 	se_doc.to_warehouse = None

# 	# for child in se_doc.items:
# 	for i, child in enumerate(se_doc.items):
# 		child.t_warehouse = department_wh
# 		child.s_warehouse = in_transit_wh
# 		child.material_request = None
# 		child.material_request_item = None
# 		child.manufacturing_operation = manufacturing_operation
# 		child.department = doc.current_department
# 		child.to_department = doc.next_department
# 		child.to_main_slip = None
# 		child.main_slip = None
# 		child.employee = None
# 		child.to_employee = None
# 		child.subcontractor = None
# 		child.to_subcontractor = None
# 		child.against_stock_entry = stock_entry
# 		child.stock_entry = stock_entry
# 		child.ste_detail = existing_doc.items[i].name
# 		child.use_serial_batch_fields = True
# 		child.serial_and_batch_bundle = None

# 	se_doc.to_main_slip = None
# 	se_doc.outgoing_stock_entry = stock_entry
# 	se_doc.main_slip = None
# 	se_doc.employee = None
# 	se_doc.to_employee = None
# 	se_doc.subcontractor = None
# 	se_doc.to_subcontractor = None
# 	se_doc.department = existing_doc.department
# 	se_doc.to_department = doc.current_department
# 	se_doc.department_ir = doc.name
# 	se_doc.manufacturing_operation = manufacturing_operation
# 	se_doc.auto_created = True
# 	se_doc.add_to_transit = 0
# 	se_doc.flags.ignore_permissions = True
# 	se_doc.save()
# 	se_doc.submit()
# 	return se_doc


def fetch_and_update(doc, row, manufacturing_operation):
	filters = {}
	current_dep = doc.current_department
	filters.update(
		{
			"manufacturing_work_order": row.manufacturing_work_order,
			"docstatus": 1,
			# "manufacturing_operation": ["is", "not set"],
			"to_department": current_dep,
			#  "t_warehouse": department_wh
		}
	)
	stock_entries = frappe.get_all("Stock Entry", filters=filters, pluck="name")

	if not stock_entries:
		# update_manufacturing_operation(stock_entry)
		# frappe.msgprint(f"No entries received against MWO : {row.manufacturing_work_order} and Department{doc.current_department}")
		return False
	else:

		values = {"manufacturing_operation": manufacturing_operation}
		for stock_entry in stock_entries:
			rows = frappe.get_all("Stock Entry Detail", {"parent": stock_entry}, pluck="name")
			set_values_in_bulk("Stock Entry Detail", rows, values)
			values[scrub(doc.doctype)] = doc.name
			frappe.db.set_value("Stock Entry", stock_entry, values)
			del values[scrub(doc.doctype)]
			update_manufacturing_operation(stock_entry)


# def create_stock_entry(doc, row):

# 	in_transit_wh = frappe.db.get_value(
# 		"Warehouse",
# 		{"disabled": 0, "department": doc.current_department, "warehouse_type": "Manufacturing"},
# 		"default_in_transit_warehouse",
# 	)
# 	if not in_transit_wh:
# 		# frappe.throw(_(f"Please set transit warhouse for Current Department {doc.current_department}"))
# 		frappe.throw(
# 			_("Please set transit warhouse for Current Department {0}").format(doc.current_department)
# 		)

# 	department_wh = frappe.get_value(
# 		"Warehouse",
# 		{"disabled": 0, "department": doc.current_department, "warehouse_type": "Manufacturing"},
# 	)
# 	if not department_wh:
# 		# frappe.throw(_(f"Please set warhouse for department {doc.current_department}"))
# 		frappe.throw(_("Please set warhouse for department {0}").format(doc.current_department))

# 	stock_entries = frappe.get_all(
# 		"Stock Entry Detail",
# 		{
# 			"manufacturing_operation": row.manufacturing_operation,
# 			"t_warehouse": in_transit_wh,
# 			"department": doc.previous_department,
# 			"to_department": doc.current_department,
# 			"docstatus": 1,
# 		},
# 		pluck="parent",
# 		group_by="parent",
# 	)

# 	for stock_entry in stock_entries:
# 		existing_doc = frappe.get_doc("Stock Entry", stock_entry)
# 		se_doc = frappe.copy_doc(existing_doc)
# 		se_doc.stock_entry_type = "Material Transfer to Department"
# 		se_doc.branch = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "branch")
# 		# for child in se_doc.items:
# 		for i, child in enumerate(se_doc.items):
# 			child.s_warehouse = in_transit_wh
# 			child.t_warehouse = department_wh
# 			child.material_request = None
# 			child.material_request_item = None
# 			child.department = doc.previous_department
# 			child.to_department = doc.current_department
# 			child.against_stock_entry = stock_entry
# 			child.stock_entry = stock_entry
# 			child.ste_detail = existing_doc.items[i].name
# 			child.use_serial_batch_fields = True
# 			child.serial_and_batch_bundle = None
# 		se_doc.department = doc.previous_department
# 		se_doc.to_department = doc.current_department
# 		se_doc.auto_created = True
# 		se_doc.add_to_transit = 0
# 		se_doc.department_ir = doc.name
# 		se_doc.flags.ignore_permissions = True
# 		se_doc.save()
# 		se_doc.submit()


def create_operation_for_next_dept(ir_name, mwo, mop, next_department):
	new_mop_doc = frappe.copy_doc(frappe.get_cached_doc("Manufacturing Operation", mop))
	new_mop_doc.name = None
	new_mop_doc.department_issue_id = ir_name
	new_mop_doc.department_ir_status = "In-Transit"
	new_mop_doc.department_receive_id = None
	new_mop_doc.previous_operation = new_mop_doc.operation
	new_mop_doc.department = next_department
	new_mop_doc.previous_mop = mop
	new_mop_doc.operation = None
	new_mop_doc.department_source_table = []
	new_mop_doc.department_target_table = []
	new_mop_doc.employee_source_table = []
	new_mop_doc.employee_target_table = []
	new_mop_doc.previous_se_data_updated = 0
	new_mop_doc.insert()
	# target.prev_gross_wt = source.received_gross_wt or source.gross_wt or source.prev_gross_wt
	# target.previous_mop = source.name

	# # def set_missing_value(source, target):

	# target_doc = get_mapped_doc(
	# 	"Manufacturing Operation",
	# 	docname,
	# 	{
	# 		"Manufacturing Operation": {
	# 			"doctype": "Manufacturing Operation",
	# 			"field_no_map": [
	# 				"status",
	# 				"employee",
	# 				"department",
	# 				"start_time",
	# 				"for_subcontracting",
	# 				"subcontractor",
	# 				"finish_time",
	# 				"department_issue_id",
	# 				"department_receive_id",
	# 				"department_ir_status",
	# 				"operation",
	# 				"previous_operation",
	# 				"start_time",
	# 				"finish_time",
	# 				"time_taken",
	# 				"started_time",
	# 				"current_time",
	# 				"on_hold",
	# 				"total_minutes",
	# 				"time_logs",
	# 			],
	# 		}
	# 	},
	# 	target_doc,
	# 	# set_missing_value,
	# )
	# target_doc.department_source_table = []
	# target_doc.department_target_table = []
	# target_doc.employee_source_table = []
	# target_doc.employee_target_table = []
	# # target_doc.time_logs =[]
	# target_doc.department_issue_id = ir_name
	# target_doc.department_ir_status = "In-Transit"
	# target_doc.department = next_department
	# target_doc.time_taken = None
	# target_doc.save()
	# target_doc.db_set("employee", None)
	frappe.db.set_value("Manufacturing Work Order", mwo, "manufacturing_operation", new_mop_doc.name)
	return new_mop_doc.name

def create_operation_for_next_dept_new(ir_name, mwo, mop, next_department):
	operation = frappe.db.get_value("Manufacturing Operation", mop, "operation")
	new_mop_doc = frappe.new_doc("Manufacturing Operation")
	new_mop_doc.department_issue_id = ir_name
	new_mop_doc.department_ir_status = "In-Transit"
	new_mop_doc.department_receive_id = None
	new_mop_doc.previous_operation = operation
	new_mop_doc.department = next_department
	new_mop_doc.previous_mop = mop
	new_mop_doc.operation = None
	new_mop_doc.department_source_table = []
	new_mop_doc.department_target_table = []
	new_mop_doc.employee_source_table = []
	new_mop_doc.employee_target_table = []
	new_mop_doc.previous_se_data_updated = 0
	new_mop_doc.insert()
	frappe.db.set_value("Manufacturing Work Order", mwo, "manufacturing_operation", new_mop_doc.name)
	return new_mop_doc.name


@frappe.whitelist()
def get_manufacturing_operations(source_name, target_doc=None):
	if not target_doc:
		target_doc = frappe.new_doc("Department IR")
	elif isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))

	operation = frappe.db.get_value(
		"Manufacturing Operation",
		source_name,
		["gross_wt", "manufacturing_work_order", "diamond_wt"],
		as_dict=1,
	)
	if not target_doc.get(
		"department_ir_operation", {"manufacturing_work_order": operation["manufacturing_work_order"]}
	):
		target_doc.append(
			"department_ir_operation",
			{
				"manufacturing_operation": source_name,
				"manufacturing_work_order": operation["manufacturing_work_order"],
				"gross_wt": operation["gross_wt"],
				"diamond_wt": operation["diamond_wt"],
			},
		)
	return target_doc


@frappe.whitelist()
def department_receive_query(doctype, txt, searchfield, start, page_len, filters):

	DIR = frappe.qb.DocType("Department IR")
	DP = frappe.qb.DocType("Department IR")
	query = (
		frappe.qb.from_(DIR)
		.select(DIR.name)
		.where(
			(DIR.type == "Issue")
			& (DIR.docstatus == 1)
			& (DIR.name.like("%{0}%".format(txt)))
			& (
				DIR.name.notin(
					frappe.qb.from_(DP)
					.select(DP.receive_against)
					.where((DP.docstatus == 1) & (DP.type == "Receive") & (DP.receive_against.isnotnull()))
				)
			)
		)
	)
	if filters.get("current_department") and filters.get("current_department") != "":
		query = query.where(DIR.current_department == filters.get("current_department"))

	if filters.get("next_department") and filters.get("next_department") != "":
		query = query.where(DIR.next_department == filters.get("next_department"))
	data = query.run()

	return data if data else []


def get_material_wt(doc, manufacturing_operation):
	SED = frappe.qb.DocType("Stock Entry Detail")
	SE = frappe.qb.DocType("Stock Entry")
	Item = frappe.qb.DocType("Item")

	IF = CustomFunction("IF", ["condition", "true_expr", "false_expr"])
	query = (
		frappe.qb.from_(SED)
		.left_join(SE)
		.on(SED.parent == SE.name)
		.left_join(Item)
		.on(Item.name == SED.item_code)
		.select(
			IfNull(Sum(IF(SED.uom == "Carat", SED.qty * 0.2, SED.qty)), 0).as_("gross_wt"),
			IfNull(Sum(IF(Item.variant_of == "M", SED.qty, 0)), 0).as_("net_wt"),
			IfNull(Sum(IF(Item.variant_of == "D", SED.qty, 0)), 0).as_("diamond_wt"),
			IfNull(
				Sum(IF(Item.variant_of == "D", IF(SED.uom == "Carat", SED.qty * 0.2, SED.qty), 0)), 0
			).as_("diamond_wt_in_gram"),
			IfNull(Sum(IF(Item.variant_of == "G", SED.qty, 0)), 0).as_("gemstone_wt"),
			IfNull(
				Sum(IF(Item.variant_of == "G", IF(SED.uom == "Carat", SED.qty * 0.2, SED.qty), 0)), 0
			).as_("gemstone_wt_in_gram"),
			IfNull(Sum(IF(Item.variant_of == "O", SED.qty, 0)), 0).as_("other_wt"),
		)
		.where(
			(SE[scrub(doc.doctype)] == doc.name)
			& (SED.manufacturing_operation == manufacturing_operation)
			& (SE.docstatus == 1)
		)
	)
	res = query.run(as_dict=True)

	if res:
		return res[0]
	return {}


# timer code
def add_time_log(doc, args):
	last_row = []

	if doc.department_time_logs and len(doc.department_time_logs) > 0:
		last_row = doc.department_time_logs[-1]

	doc.reset_timer_value(args)

	# issue - complete_time
	if last_row and args.get("complete_time"):
		for row in doc.department_time_logs:
			if not row.department_to_time:
				row.update(
					{
						"department_to_time": get_datetime(args.get("complete_time")),
					}
				)

	# receive - department_start_time
	elif args.get("department_start_time"):

		new_args = frappe._dict(
			{
				"department_from_time": get_datetime(args.get("department_start_time")),
			}
		)
		doc.add_start_time_log(new_args)

	doc.update_children()
	doc.db_update_all()

def add_time_log_optimize(mop_name, args):
	status = args.get("status")

	# Normalize status
	if status == "Resume Job":
		status = "WIP"

	# Reset timer values (status, current_time, started_time)
	update_fields = {}
	if status:
		update_fields["status"] = status
	if status in ["WIP", "Finished"]:
		update_fields["current_time"] = 0.0
	if status == "WIP" and args.get("start_time"):
		update_fields["started_time"] = get_datetime(args["start_time"])

	if update_fields:
		frappe.db.set_value("Manufacturing Operation", mop_name, update_fields)

	# 1. If complete_time exists → update all open department_time_logs
	if args.get("complete_time"):
		complete_time = get_datetime(args["complete_time"])
		frappe.db.sql(
			"""
			UPDATE `tabManufacturing Operation Department Time Log`
			SET department_to_time = %s
			WHERE parent = %s
				AND parenttype = 'Manufacturing Operation'
				AND department_to_time IS NULL
			""",
			(complete_time, mop_name)
		)

	# 2. Else if department_start_time exists → insert a department_time_log row
	elif args.get("department_start_time"):
		dept_from_time = get_datetime(args["department_start_time"])
		frappe.db.sql(
			"""
			INSERT INTO `tabManufacturing Operation Department Time Log`
			(name, parent, parenttype, parentfield, creation, modified,
			department_from_time)
			VALUES (%s, %s, 'Manufacturing Operation', 'department_time_logs', NOW(), NOW(), %s)
			""",
			(frappe.generate_hash(), mop_name, dept_from_time)
		)

	# 3. Else if start_time exists → insert into time_logs with optional employee
	elif args.get("start_time"):
		from_time = get_datetime(args["start_time"])
		employee = args.get("employee")
		frappe.db.sql(
			"""
			INSERT INTO `tabManufacturing Operation Time Log`
			(name, parent, parenttype, parentfield, creation, modified,
			from_time, employee)
			VALUES (%s, %s, 'Manufacturing Operation', 'time_logs', NOW(), NOW(), %s, %s)
			""",
			(frappe.generate_hash(), mop_name, from_time, employee)
		)
