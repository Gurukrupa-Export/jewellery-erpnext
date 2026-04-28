# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir import (
	DepartmentIR,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	ManufacturingOperation,
	get_material_wt,
	get_stock_entries_against_mfg_operation,
)


class TestManufacturingOperation(FrappeTestCase):
	def setUp(self):
		return super().setUp()

	def test_manufacturing_operations(self):
		mwo_list = mo_creation()
		serial_no_mwo = None
		mr_list = frappe.get_all(
			"Material Request",
			filters={
				"manufacturing_order": mwo_list[0].manufacturing_order,
				"docstatus": 0,
			},
			pluck="name",
		)
		for row in mwo_list:
			if row.department == "Manufacturing Plan & Management - GEPL":
				mwo = frappe.get_doc("Manufacturing Work Order", row.name)
				mwo.submit()
				mo_man = frappe.get_last_doc(
					"Manufacturing Operation",
					filters={"manufacturing_work_order": mwo.name},
				)

				if mr_list:
					mop_log_se = mop_log_creation(mr_list[0], mo_man)
					sed = frappe.get_doc("Stock Entry Detail", mop_log_se.row_name)
					self.assertEqual(mop_log_se.voucher_no, sed.parent)
					self.assertEqual(mop_log_se.row_name, sed.name)
					self.assertEqual(mop_log_se.item_code, sed.item_code)
					self.assertEqual(mop_log_se.from_warehouse, sed.s_warehouse)
					self.assertEqual(mop_log_se.to_warehouse, sed.t_warehouse)
					self.assertEqual(mop_log_se.qty_change, sed.qty)
					self.assertEqual(
						mop_log_se.serial_and_batch_bundle, sed.serial_and_batch_bundle
					)
					self.assertEqual(mop_log_se.batch_no, sed.batch_no)
					self.assertEqual(
						mop_log_se.manufacturing_operation, sed.manufacturing_operation
					)
					print(mop_log_se.name)

				dir_issue = dir_for_issue(
					"Manufacturing Plan & Management - GEPL", "Waxing - GEPL", mo_man
				)
				mo_man.reload()
				self.assertEqual("Finished", mo_man.status)
				print(frappe.get_last_doc("MOP Log").name)

				mop_log = frappe.get_doc(
					"MOP Log",
					frappe.get_value("MOP Log", filters={"voucher_no": dir_issue.name}),
				)
				from_warehouse = frappe.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_issue.current_department,
						"warehouse_type": "Manufacturing",
					},
				)
				to_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_issue.next_department,
						"warehouse_type": "Manufacturing",
					},
					"default_in_transit_warehouse",
				)
				self.assertEqual(mop_log.voucher_no, dir_issue.name)
				self.assertEqual(mop_log.from_warehouse, from_warehouse)
				self.assertEqual(mop_log.to_warehouse, to_warehouse)
				self.assertEqual(
					mop_log.row_name, dir_issue.department_ir_operation[0].name
				)

				mo_wax = frappe.get_last_doc("Manufacturing Operation")
				self.assertIsNotNone(mo_wax.department_issue_id)
				self.assertEqual(mo_wax.department_issue_id, dir_issue.name)

				dir_receive = dir_for_receive(dir_issue)
				mo_wax.reload()
				self.assertIsNotNone(mo_wax.department_receive_id)
				self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

				mop_log = frappe.get_doc(
					"MOP Log",
					frappe.get_value(
						"MOP Log", filters={"voucher_no": dir_receive.name}
					),
				)
				to_warehouse = frappe.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_receive.current_department,
						"warehouse_type": "Manufacturing",
					},
				)
				from_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": dir_receive.current_department,
						"warehouse_type": "Manufacturing",
					},
					"default_in_transit_warehouse",
				)

				self.assertEqual(mop_log.voucher_no, dir_receive.name)
				self.assertEqual(mop_log.from_warehouse, from_warehouse)
				self.assertEqual(mop_log.to_warehouse, to_warehouse)
				self.assertEqual(
					mop_log.row_name, dir_receive.department_ir_operation[0].name
				)

				eir_issue = frappe.new_doc("Employee IR")
				eir_issue.department = "Waxing - GEPL"
				eir_issue.operation = "Wax Pull Out"
				eir_issue.employee = "GEPL - 00157"
				eir_issue.scan_mwo = mo_wax.manufacturing_work_order
				scan_mwo_eir(eir_issue)
				eir_issue.save()
				eir_issue.submit()

				mo_wax.reload()
				self.assertEqual(mo_wax.status, "WIP")
				self.assertEqual(eir_issue.operation, mo_wax.operation)

				mop_log = frappe.get_doc(
					"MOP Log",
					frappe.get_value("MOP Log", filters={"voucher_no": eir_issue.name}),
				)
				from_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": eir_issue.department,
						"warehouse_type": "Manufacturing",
					},
				)
				to_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"warehouse_type": "Manufacturing",
						"disabled": 0,
						"employee": eir_issue.employee,
					},
				)
				self.assertEqual(mop_log.voucher_no, eir_issue.name)
				self.assertEqual(mop_log.from_warehouse, from_warehouse)
				self.assertEqual(mop_log.to_warehouse, to_warehouse)
				self.assertEqual(
					mop_log.row_name, eir_issue.employee_ir_operations[0].name
				)

				eir_receive = frappe.new_doc("Employee IR")
				eir_receive.department = "Waxing - GEPL"
				eir_receive.type = "Receive"
				eir_receive.operation = "Wax Pull out"
				eir_receive.employee = "GEPL - 00157"
				eir_receive.scan_mwo = mo_wax.manufacturing_work_order
				scan_mwo_eir(eir_receive)
				eir_receive.save()
				eir_receive.employee_ir_operations[
					0
				].received_gross_wt = frappe.get_value(
					"BOM", mwo.master_bom, "metal_weight"
				)
				eir_receive.submit()

				mo_wax.reload()
				self.assertEqual(mo_wax.status, "Finished")

				mo_wax1 = frappe.get_last_doc("Manufacturing Operation")
				self.assertEqual(mo_wax.operation, mo_wax1.previous_operation)
				dir_issue = dir_for_issue("Waxing - GEPL", "Tagging - GEPL", mo_wax1)

				mop_log = frappe.get_doc(
					"MOP Log",
					frappe.get_value(
						"MOP Log", filters={"voucher_no": eir_receive.name}
					),
				)
				to_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"disabled": 0,
						"department": eir_receive.department,
						"warehouse_type": "Manufacturing",
					},
				)
				from_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"warehouse_type": "Manufacturing",
						"disabled": 0,
						"employee": eir_receive.employee,
					},
				)
				self.assertEqual(mop_log.voucher_no, eir_receive.name)
				self.assertEqual(mop_log.from_warehouse, from_warehouse)
				self.assertEqual(mop_log.to_warehouse, to_warehouse)
				self.assertEqual(
					mop_log.row_name, eir_receive.employee_ir_operations[0].name
				)

				mo_tag = frappe.get_last_doc("Manufacturing Operation")
				self.assertEqual(mo_wax1.operation, mo_tag.previous_operation)
				self.assertEqual(mo_tag.previous_mop, mo_wax1.name)

				dir_receive = dir_for_receive(dir_issue)
				mo_tag.reload()

				self.assertEqual(mo_tag.department_issue_id, dir_issue.name)
				self.assertEqual(mo_tag.department_receive_id, dir_receive.name)

				mwo.reload()
				self.assertEqual(mwo.department, mo_tag.department)
			elif row.department == "Serial Number - GEPL":
				serial_no_mwo = row

		mwo_serial_no = frappe.get_doc("Manufacturing Work Order", serial_no_mwo.name)
		mwo_serial_no.submit()
		mo_tag.reload()
		self.assertEqual(mo_tag.status, "Finished")

		mo_serial = frappe.get_last_doc("Manufacturing Operation")
		self.assertEqual(mwo_serial_no.name, mo_serial.manufacturing_work_order)
		self.assertEqual(mo_serial.department, "Tagging - GEPL")

		serial_no_creater = frappe.get_last_doc("Serial Number Creator")
		self.assertEqual(serial_no_creater.manufacturing_work_order, mwo_serial_no.name)

	def test_get_material_wt_mixed_variants(self):
		mop = frappe.new_doc("Manufacturing Operation")
		mop.company = "_Test Indian Registered Company"
		mop.mop_balance_table = []

		mop.append("mop_balance_table", {"item_code": "M-001", "qty": 2, "pcs": 0})
		mop.append("mop_balance_table", {"item_code": "D-001", "qty": 1.5, "pcs": "2"})
		mop.append("mop_balance_table", {"item_code": "G-001", "qty": 0.5, "pcs": "1"})
		mop.append("mop_balance_table", {"item_code": "F-001", "qty": 0.3, "pcs": 0})
		mop.append("mop_balance_table", {"item_code": "O-001", "qty": 0.2, "pcs": 0})

		res = get_material_wt(mop)

		self.assertAlmostEqual(res.get("diamond_wt_in_gram"), 0.3)

		expected_gross = 2 + 0.3 + 0.3 + 0.1 + 0.2
		self.assertAlmostEqual(res.get("gross_wt"), expected_gross)

	def test_get_material_wt_empty(self):
		mop = frappe.new_doc("Manufacturing Operation")
		mop.company = "_Test Indian Registered Company"
		mop.mop_balance_table = []

		res = get_material_wt(mop)
		self.assertIsInstance(res, dict)
		self.assertEqual(res.get("gross_wt"), 0)
		self.assertEqual(res.get("net_wt"), 0)

	def test_get_stock_entries_against_mfg_operation_aggregation(self):
		dept = frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": "Test Manufacturing Department",
				"company": "_Test Indian Registered Company",
			}
		)
		dept.insert()

		# Create a warehouse linked to the department
		wh = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": "Test Mfg Warehouse",
				"company": "_Test Indian Registered Company",
				"warehouse_type": "Manufacturing",
				"department": dept.name,
			}
		)
		wh.insert()

		# Create a MOP with the department set
		mop = frappe.get_doc(
			{
				"doctype": "Manufacturing Operation",
				"department": dept.name,
				"company": "_Test Indian Registered Company",
			}
		)
		mop.insert()

		# Create a Stock Entry
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Transfer",
				"company": "_Test Indian Registered Company",
				"docstatus": 1,
			}
		)
		se.insert()

		# Create Stock Entry Detail rows with t_warehouse pointing to the same warehouse
		sed1 = frappe.get_doc(
			{
				"doctype": "Stock Entry Detail",
				"parent": se.name,
				"parenttype": se.doctype,
				"parentfield": "items",
				"item_code": "_Test Nil Rated Item",
				"qty": 2,
				"uom": "Nos",
				"t_warehouse": wh.name,
				"manufacturing_operation": mop.name,
				"conversion_factor": 1,
				"transfer_qty": 2,
				"docstatus": 1,
			}
		)
		sed1.insert()

		sed2 = frappe.get_doc(
			{
				"doctype": "Stock Entry Detail",
				"parent": se.name,
				"parenttype": se.doctype,
				"parentfield": "items",
				"item_code": "_Test Nil Rated Item",
				"qty": 3,
				"uom": "Nos",
				"t_warehouse": wh.name,
				"manufacturing_operation": mop.name,
				"conversion_factor": 1,
				"transfer_qty": 3,
				"docstatus": 1,
			}
		)
		sed2.insert()

		res = get_stock_entries_against_mfg_operation(mop)
		self.assertIn("_Test Nil Rated Item", res)
		self.assertEqual(res["_Test Nil Rated Item"]["qty"], 5)
		self.assertEqual(res["_Test Nil Rated Item"]["uom"], "Nos")

	def test_validate_loss_raises_on_invalid_item(self):
		mop = frappe.new_doc("Manufacturing Operation")
		mop.insert()

		mop.append(
			"loss_details",
			{"item_code": "NON-EXIST", "stock_uom": "Nos", "stock_qty": 1, "idx": 1},
		)

		with self.assertRaises(frappe.ValidationError):
			mop.validate_loss()

	def test_has_overlap_detects_overlap(self):
		mop = frappe.new_doc("Manufacturing Operation")

		time_logs = [
			{"from_time": "2023-01-01 10:00:00", "to_time": "2023-01-01 12:00:00"},
			{"from_time": "2023-01-01 11:00:00", "to_time": "2023-01-01 13:00:00"},
		]

		res = ManufacturingOperation.has_overlap(mop, 2, time_logs)
		self.assertTrue(res)

		mop = frappe.new_doc("Manufacturing Operation")

		time_logs = [
			{"from_time": "2023-01-01 10:00:00", "to_time": "2023-01-01 12:00:00"},
			{"from_time": "2023-01-01 12:00:00", "to_time": "2023-01-01 13:00:00"},
		]

		res = ManufacturingOperation.has_overlap(mop, 2, time_logs)
		self.assertFalse(res)

	def test_validate_loss_wrong_uom(self):
		mop = frappe.get_doc(
			{
				"doctype": "Manufacturing Operation",
				"company": "_Test Indian Registered Company",
			}
		)
		mop.insert()

		dept = frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": "Test Dept Loss",
				"company": "_Test Indian Registered Company",
			}
		)
		dept.insert()

		wh = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": "Test Wh Loss",
				"company": "_Test Indian Registered Company",
				"warehouse_type": "Manufacturing",
				"department": dept.name,
			}
		)
		wh.insert()

		mop.department = dept.name
		mop.save()

		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Transfer",
				"company": "_Test Indian Registered Company",
				"docstatus": 1,
			}
		)
		se.insert()

		frappe.get_doc(
			{
				"doctype": "Stock Entry Detail",
				"parent": se.name,
				"parenttype": "Stock Entry",
				"parentfield": "items",
				"item_code": "_Test Nil Rated Item",
				"qty": 5,
				"uom": "Nos",
				"t_warehouse": wh.name,
				"manufacturing_operation": mop.name,
				"docstatus": 1,
				"transfer_qty": 1,
				"conversion_factor": 1,
			}
		).insert()

		mop.append(
			"loss_details",
			{"item_code": "_Test Nil Rated Item", "stock_uom": "Kg", "stock_qty": 1},
		)

		with self.assertRaises(frappe.ValidationError):
			mop.validate_loss()

	def test_validate_loss_qty_greater_than_available(self):
		mop = frappe.get_doc(
			{
				"doctype": "Manufacturing Operation",
				"company": "_Test Indian Registered Company",
			}
		)
		mop.insert()

		dept = frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": "Test Dept Loss Qty",
				"company": "_Test Indian Registered Company",
			}
		)
		dept.insert()

		wh = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": "Test Wh Loss Qty",
				"company": "_Test Indian Registered Company",
				"warehouse_type": "Manufacturing",
				"department": dept.name,
			}
		)
		wh.insert()

		mop.department = dept.name
		mop.save()

		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Transfer",
				"company": "_Test Indian Registered Company",
				"docstatus": 1,
			}
		)
		se.insert()

		frappe.get_doc(
			{
				"doctype": "Stock Entry Detail",
				"parent": se.name,
				"parenttype": "Stock Entry",
				"parentfield": "items",
				"item_code": "_Test Nil Rated Item",
				"qty": 5,
				"uom": "Nos",
				"t_warehouse": wh.name,
				"manufacturing_operation": mop.name,
				"docstatus": 1,
				"transfer_qty": 1,
				"conversion_factor": 1,
			}
		).insert()

		mop.append(
			"loss_details",
			{"item_code": "_Test Nil Rated Item", "stock_uom": "Nos", "stock_qty": 10},
		)

		with self.assertRaises(frappe.ValidationError):
			mop.validate_loss()

	def test_set_start_finish_time_on_wip_status(self):
		mop = frappe.get_doc(
			{
				"doctype": "Manufacturing Operation",
				"company": "_Test Indian Registered Company",
				"status": "Not Started",
			}
		)
		mop.insert()

		mop.append(
			"time_logs",
			{
				"from_time": "2024-01-01 10:00:00",
				"to_time": "2024-01-01 12:00:00",
			},
		)
		mop.save()

		mop.status = "WIP"
		mop.save()

		self.assertEqual(
			mop.start_time,
			str(
				frappe.get_doc("Manufacturing Operation", mop.name)
				.time_logs[0]
				.from_time
			),
			"start_time should be set from first time log when status changes to WIP",
		)

	def test_set_start_finish_time_on_finished_status(self):
		mop = frappe.get_doc(
			{
				"doctype": "Manufacturing Operation",
				"company": "_Test Indian Registered Company",
				"status": "WIP",
			}
		)
		mop.insert()

		mop.append(
			"time_logs",
			{
				"from_time": "2024-01-01 10:00:00",
				"to_time": "2024-01-01 11:00:00",
			},
		)
		mop.append(
			"time_logs",
			{
				"from_time": "2024-01-01 11:00:00",
				"to_time": "2024-01-01 13:00:00",
			},
		)
		mop.save()

		mop.status = "Finished"
		mop.save()

		reloaded = frappe.get_doc("Manufacturing Operation", mop.name)
		self.assertEqual(
			reloaded.start_time,
			reloaded.time_logs[0].from_time,
			"start_time should be set from first time log when status changes to Finished",
		)
		self.assertEqual(
			reloaded.finish_time,
			reloaded.time_logs[-1].to_time,
			"finish_time should be set from last time log when status changes to Finished",
		)

	def tearDown(self):
		return super().tearDown()


def mo_creation():
	pmo = frappe.get_last_doc("Parent Manufacturing Order", filters={"docstatus": 0})
	pmo.manufacturer = "Shubh"
	pmo.save()
	pmo.submit()
	print(pmo.name)
	return frappe.get_all(
		"Manufacturing Work Order",
		filters={"manufacturing_order": pmo.name},
		fields=["name", "department", "manufacturing_order"],
	)


def scan_mwo_dir(doc):
	for item in doc.department_ir_operation:
		if item.manufacturing_work_order == doc.scan_mwo:
			frappe.throw(
				"{} Manufacturing Work Order already exists".format(doc.scan_mwo)
			)

	if not doc.current_department:
		frappe.throw("Please select current department first")

	values = frappe.get_last_doc(
		"Manufacturing Operation", filters={"manufacturing_work_order": doc.scan_mwo}
	)

	prev = frappe.get_value(
		"Manufacturing Operation",
		values.previous_mop,
		[
			"gross_wt",
			"diamond_wt",
			"net_wt",
			"finding_wt",
			"diamond_pcs",
			"gemstone_pcs",
			"gemstone_wt",
			"other_wt",
			"received_gross_wt",
		],
		as_dict=True,
	)

	gr_wt = 0
	if values.gross_wt and values.gross_wt > 0:
		gr_wt = values.gross_wt
	elif prev:
		if prev.received_gross_wt and prev.received_gross_wt > 0:
			gr_wt = prev.received_gross_wt
		elif prev.gross_wt and prev.gross_wt > 0:
			gr_wt = prev.gross_wt

	doc.append(
		"department_ir_operation",
		{
			"manufacturing_work_order": values.manufacturing_work_order,
			"manufacturing_operation": values.name,
			"status": values.status,
			"gross_wt": gr_wt,
			"diamond_wt": values.diamond_wt
			if values.diamond_wt > 0
			else (prev.diamond_wt if prev else 0),
			"net_wt": values.net_wt
			if values.net_wt > 0
			else (prev.net_wt if prev else 0),
			"finding_wt": values.finding_wt
			if values.finding_wt > 0
			else (prev.finding_wt if prev else 0),
			"gemstone_wt": values.gemstone_wt
			if values.gemstone_wt > 0
			else (prev.gemstone_wt if prev else 0),
			"other_wt": values.other_wt
			if values.other_wt > 0
			else (prev.other_wt if prev else 0),
			"diamond_pcs": values.diamond_pcs
			if values.diamond_pcs > 0
			else (prev.diamond_pcs if prev else 0),
			"gemstone_pcs": values.gemstone_pcs
			if values.gemstone_pcs > 0
			else (prev.gemstone_pcs if prev else 0),
		},
	)

	doc.scan_mwo = ""


def scan_mwo_eir(doc):
	for item in doc.employee_ir_operations:
		if item.manufacturing_work_order == doc.scan_mwo:
			frappe.throw(
				"{} Manufacturing Work Order already exists".format(doc.scan_mwo)
			)

	values = frappe.get_last_doc(
		"Manufacturing Operation", filters={"manufacturing_work_order": doc.scan_mwo}
	)

	if not values:
		frappe.throw("No Manufacturing Operation Found")

	qc = frappe.get_value(
		"QC",
		{
			"manufacturing_work_order": values.manufacturing_work_order,
			"manufacturing_operation": values.name,
			"status": ["!=", "Rejected"],
			"docstatus": 1,
		},
		["name", "received_gross_wt"],
		as_dict=True,
	)

	doc.append(
		"employee_ir_operations",
		{
			"manufacturing_work_order": values.manufacturing_work_order,
			"manufacturing_operation": values.name,
			"qc": qc.name if qc else None,
			"received_gross_wt": qc.received_gross_wt if qc else 0,
			"rpt_wt_issue": 0,
		},
	)

	doc.scan_mwo = ""


def dir_for_issue(cur_dep, nxt_dep, mo):
	dir_issue = frappe.new_doc("Department IR")
	dir_issue.manufacturer = "Shubh"
	dir_issue.current_department = cur_dep
	dir_issue.next_department = nxt_dep
	dir_issue.scan_mwo = mo.manufacturing_work_order
	scan_mwo_dir(dir_issue)
	dir_issue.save()
	dir_issue.submit()

	return dir_issue


def dir_for_receive(dir_issue):
	dir_receive = frappe.new_doc("Department IR")
	dir_receive.manufacturer = "Shubh"
	dir_receive.type = "Receive"
	dir_receive.receive_against = dir_issue.name
	dir_receive.current_department = dir_issue.next_department
	dir_receive.previous_department = dir_issue.current_department
	DepartmentIR.get_manufacturing_operations_from_department_ir(
		dir_receive, dir_issue.name
	)
	dir_receive.save()
	dir_receive.submit()

	return dir_receive


def mop_log_creation(mr_name, mo):
	mr = frappe.get_doc("Material Request", mr_name)
	apply_workflow(mr, "Send for Reservation")
	apply_workflow(mr, "Reserve Material")
	apply_workflow(mr, "Transfer Material")
	mr.reload()
	mr.custom_manufacturing_operation = mo.name
	mr.save()
	apply_workflow(mr, "Transfer to MOP")
	return frappe.get_last_doc("MOP Log")
