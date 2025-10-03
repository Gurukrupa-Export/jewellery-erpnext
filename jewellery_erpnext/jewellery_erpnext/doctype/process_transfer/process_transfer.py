# Copyright (c) 2025, Nirali and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document


class ProcessTransfer(Document):
	def before_save(self):
		if not self.department:
			frappe.throw("Please select current department first")


	def on_submit(self):
		for row in self.process_transfer_operation:
				frappe.db.set_value(
							"Manufacturing Work Order",
							{
								"name": row.manufacturing_work_order,
							},
							"department_process",
							self.next_process
				)
				frappe.db.set_value(
							"Manufacturing Operation",
							{
								"name": row.manufacturing_operation,
							},
							"department_process",
							self.next_process
				)
	
	# @frappe.whitelist()
	# def get_manufacturing_operations_from_department_ir(self, docname):
	# 	self.department_ir_operation = []
	# 	for row in frappe.get_all(
	# 		"Manufacturing Operation",
	# 		{"department_issue_id": docname, "department_ir_status": "In-Transit"},
	# 		[
	# 			"name as manufacturing_operation",
	# 			"manufacturing_work_order",
	# 			"prev_gross_wt as gross_wt",
	# 			"previous_mop",
	# 			"department",
	# 		],
	# 	):
	# 		self.current_department = row.department
	# 		mop_details = frappe.db.get_value(
	# 			"Manufacturing Operation",
	# 			row.previous_mop,
	# 			[
	# 				"diamond_wt",
	# 				"net_wt",
	# 				"finding_wt",
	# 				"diamond_pcs",
	# 				"gemstone_pcs",
	# 				"gemstone_wt",
	# 				"other_wt",
	# 				"department",
	# 			],
	# 			as_dict=1,
	# 		)
	# 		self.previous_department = mop_details.get("department")
	# 		self.append(
	# 			"department_ir_operation",
	# 			{
	# 				"manufacturing_operation": row.manufacturing_operation,
	# 				"manufacturing_work_order": row.manufacturing_work_order,
	# 				"gross_wt": row.gross_wt,
	# 				"net_wt": mop_details.get("net_wt"),
	# 				"diamond_wt": mop_details.get("diamond_wt"),
	# 				"finding_wt": mop_details.get("finding_wt"),
	# 				"diamond_pcs": mop_details.get("diamond_pcs"),
	# 				"gemstone_pcs": mop_details.get("gemstone_pcs"),
	# 				"gemstone_wt": mop_details.get("gemstone_wt"),
	# 				"other_wt": mop_details.get("other_wt"),
	# 			},
	# 		)

@frappe.whitelist()
def get_manufacturing_operations(source_name, target_doc=None):
	if not target_doc:
		target_doc = frappe.new_doc("Process Transfer")
	elif isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))

	operation = frappe.db.get_value(
		"Manufacturing Operation",
		source_name,
		["gross_wt", "manufacturing_work_order", "diamond_wt"],
		as_dict=1,
	)
	if not target_doc.get(
		"process_transfer_operation", {"manufacturing_work_order": operation["manufacturing_work_order"]}
	):
		target_doc.append(
			"process_transfer_operation",
			{
				"manufacturing_operation": source_name,
				"manufacturing_work_order": operation["manufacturing_work_order"],
				"gross_wt": operation["gross_wt"],
				"diamond_wt": operation["diamond_wt"],
			},
		)
	return target_doc

# @frappe.whitelist()
# def check_existing_department_ir(scan_mwo, current_process):
# 	existing = frappe.get_all(
#         "Process Transfer",
#         filters={
#             "manufacturing_work_order": scan_mwo,
#             "current_process": current_process,
#             "docstatus": 0  # or 1 if you want to include submitted records
#         },
#         fields=["name"],
#         limit_page_length=1
#     )
# 	if existing:
# 		return existing[0].name
# 	return None