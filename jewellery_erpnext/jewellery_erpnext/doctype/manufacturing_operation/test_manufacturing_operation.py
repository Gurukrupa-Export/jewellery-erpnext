# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir import (
	DepartmentIR,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	ManufacturingOperation,
	get_material_wt,
	get_stock_entries_against_mfg_operation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.test_manufacturing_work_order import (
	create_pmo,
)


class TestManufacturingOperation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

	def test_manufacturing_operations(self):
		pmo = create_pmo(self)
		mwo_list = frappe.get_all(
			"Manufacturing Work Order",
			filters={"manufacturing_order": pmo.name},
			fields=["name", "department", "manufacturing_order"],
		)
		serial_no_mwo = None
		mr_list = frappe.get_all(
			"Material Request",
			filters={
				"manufacturing_order": mwo_list[0].manufacturing_order,
				"docstatus": 0,
			},
			pluck="name",
		)
		# frappe.throw(str(mr_list))
		for row in mwo_list:
			if row.department == "Manufacturing Plan & Management - T":
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

				dir_issue = dir_for_issue(
					"Manufacturing Plan & Management - T", "Waxing - T", mo_man
				)
				mo_man.reload()
				self.assertEqual("Finished", mo_man.status)

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
				eir_issue.company = "Test_Company"
				eir_issue.department = "Waxing - T"
				eir_issue.operation = "Wax Pull Out"
				eir_issue.employee = "HR-EMP-00002"
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
				eir_receive.company = "Test_Company"
				eir_receive.department = "Waxing - T"
				eir_receive.type = "Receive"
				eir_receive.operation = "Wax Pull out"
				eir_receive.employee = "HR-EMP-00002"
				eir_receive.scan_mwo = mo_wax.manufacturing_work_order
				scan_mwo_eir(eir_receive)
				eir_receive.save()
				eir_receive.employee_ir_operations[
					0
				].received_gross_wt = eir_receive.employee_ir_operations[0].gross_wt
				eir_receive.submit()

				mo_wax.reload()
				self.assertEqual(mo_wax.status, "Finished")

				mop_log = frappe.get_doc(
					"MOP Log",
					frappe.get_value(
						"MOP Log",
						filters={
							"voucher_no": eir_receive.name,
							"row_name": eir_receive.employee_ir_operations[0].name,
						},
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

				mo_wax1 = frappe.get_last_doc("Manufacturing Operation")
				self.assertEqual(mo_wax.operation, mo_wax1.previous_operation)
				dir_issue = dir_for_issue("Waxing - T", "Tagging - T", mo_wax1)
				mo_wax1.reload()
				self.assertEqual(mo_wax1.status, "Finished")

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

				mo_tag = frappe.get_last_doc("Manufacturing Operation")
				self.assertEqual(mo_wax1.operation, mo_tag.previous_operation)
				self.assertEqual(mo_tag.previous_mop, mo_wax1.name)

				dir_receive = dir_for_receive(dir_issue)
				mo_tag.reload()

				self.assertEqual(mo_tag.department_issue_id, dir_issue.name)
				self.assertEqual(mo_tag.department_receive_id, dir_receive.name)

				mwo.reload()
				self.assertEqual(mwo.department, mo_tag.department)

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
			elif row.department == "Serial Number - T":
				serial_no_mwo = row

		mwo_serial_no = frappe.get_doc("Manufacturing Work Order", serial_no_mwo.name)
		mwo_serial_no.submit()
		mo_tag.reload()
		self.assertEqual(mo_tag.status, "Finished")

		mo_serial = frappe.get_last_doc("Manufacturing Operation")
		self.assertEqual(mwo_serial_no.name, mo_serial.manufacturing_work_order)
		self.assertEqual(mo_serial.department, "Tagging - T")

		serial_no_creater = frappe.get_last_doc("Serial Number Creator")
		self.assertEqual(serial_no_creater.manufacturing_work_order, mwo_serial_no.name)

	def test_get_material_wt_empty(self):
		mop = frappe.new_doc("Manufacturing Operation")
		mop.company = "Test_Company"
		mop.mop_balance_table = []

		res = get_material_wt(mop)
		self.assertIsInstance(res, dict)
		self.assertEqual(res.get("gross_wt"), 0)
		self.assertEqual(res.get("net_wt"), 0)

	def test_get_stock_entries_against_mfg_operation_aggregation(self):
		dept = frappe.db.exists("Department", "Test_Department - T")
		if not dept:
			dept = frappe.get_doc(
				{
					"doctype": "Department",
					"department_name": "Test_Department",
					"company": "Test_Company",
				}
			)
			dept.insert()
		else:
			dept = frappe.get_doc("Department", dept)

		# Create a warehouse linked to the department
		wh = frappe.db.exists("Warehouse", "Test Mfg Warehouse")
		if not wh:
			wh = frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": "Test Mfg Warehouse",
					"company": "Test_Company",
					"warehouse_type": "Manufacturing",
					"department": dept.name,
				}
			)
			wh.insert()
		else:
			wh = frappe.get_doc("Warehouse", wh)

		# Create a MOP with the department set
		mop = frappe.get_doc(
			{
				"doctype": "Manufacturing Operation",
				"department": dept.name,
				"company": "Test_Company",
			}
		)
		mop.insert()

		# Create a Stock Entry
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Transfer",
				"company": "Test_Company",
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
				"item_code": "ITEM-001",
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
				"item_code": "ITEM-001",
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
		self.assertIn("ITEM-001", res)
		self.assertEqual(res["ITEM-001"]["qty"], 5)
		self.assertEqual(res["ITEM-001"]["uom"], "Nos")

	def test_validate_loss_raises_on_invalid_item(self):
		mop = frappe.new_doc("Manufacturing Operation")
		mop.insert()

		mop.append(
			"loss_details",
			{"item_code": "ITEM-001", "stock_uom": "Nos", "stock_qty": 1, "idx": 1},
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
				"company": "Test_Company",
			}
		)
		mop.insert()

		dept = frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": "Test Dept Ls",
				"company": "Test_Company",
			}
		)
		dept.insert()

		wh = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": "Test Wh Loss",
				"company": "Test_Company",
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
				"company": "Test_Company",
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
				"item_code": "ITEM-001",
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
			{"item_code": "ITEM-001", "stock_uom": "Kg", "stock_qty": 1},
		)

		with self.assertRaises(frappe.ValidationError):
			mop.validate_loss()

	def test_validate_loss_qty_greater_than_available(self):
		mop = frappe.get_doc(
			{
				"doctype": "Manufacturing Operation",
				"company": "Test_Company",
			}
		)
		mop.insert()

		dept = frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": "Test Dept Loss Qty",
				"company": "Test_Company",
			}
		)
		dept.insert()

		wh = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": "Test Wh Loss Qty",
				"company": "Test_Company",
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
				"company": "Test_Company",
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
				"item_code": "ITEM-001",
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
			{"item_code": "ITEM-001", "stock_uom": "Nos", "stock_qty": 10},
		)

		with self.assertRaises(frappe.ValidationError):
			mop.validate_loss()

	def test_set_start_finish_time_on_wip_status(self):
		mop = frappe.get_doc(
			{
				"doctype": "Manufacturing Operation",
				"company": "Test_Company",
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
				"company": "Test_Company",
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


# def mo_creation():
#     pmo = frappe.get_last_doc("Parent Manufacturing Order", filters={"docstatus": 0})
#     pmo.manufacturer = "Shubh"
#     pmo.save()
#     pmo.submit()
#     return frappe.get_all(
#         "Manufacturing Work Order",
#         filters={"manufacturing_order": pmo.name},
#         fields=["name", "department", "manufacturing_order"],
#     )


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

	# Mirror of the department_ir.js scan_mwo handler: the row takes the operation's own
	# weights and nothing else. Keep this in step with that handler -- it exists so the
	# suite can exercise the scan path without a browser, and it is only useful while the
	# two agree.
	doc.append(
		"department_ir_operation",
		{
			"manufacturing_work_order": values.manufacturing_work_order,
			"manufacturing_operation": values.name,
			"status": values.status,
			"gross_wt": values.gross_wt or 0,
			"diamond_wt": values.diamond_wt or 0,
			"net_wt": values.net_wt or 0,
			"finding_wt": values.finding_wt or 0,
			"gemstone_wt": values.gemstone_wt or 0,
			"other_wt": values.other_wt or 0,
			"diamond_pcs": values.diamond_pcs or 0,
			"gemstone_pcs": values.gemstone_pcs or 0,
		},
	)

	doc.scan_mwo = ""


def scan_mwo_eir(doc):
	# Mirrors employee_ir.js `scan_mwo`. Keep the message in step with the client handler AND
	# with validate_duplication_and_gr_wt's server guard -- this double is the only thing the
	# suite exercises, so it drifts silently if production changes and this does not.
	for item in doc.employee_ir_operations:
		if item.manufacturing_work_order == doc.scan_mwo:
			frappe.throw(
				"Manufacturing Work Order {0} is already scanned on this Employee IR.".format(
					doc.scan_mwo
				)
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
	dir_issue.company = "Test_Company"
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
	dir_receive.company = "Test_Company"
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


# Reserve first -- the type the reserve step itself resolves to; Raw Material second, for a
# department that has no Reserve warehouse ("Manufacturing Plan & Management - T" is one).
# "Manufacturing" is deliberately absent: make_department_mop_stock_entry already targets
# that warehouse, so routing a request at it would hand the Work Order entry the same source
# and target on the fallback branch.
_STAGING_WAREHOUSE_TYPES = ("Reserve", "Raw Material")


def department_staging_warehouse(department, company):
	"""A concrete warehouse in ``department`` a Material Request can be routed to."""
	for warehouse_type in _STAGING_WAREHOUSE_TYPES:
		warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"is_group": 0,
				"company": company,
				"department": department,
				"warehouse_type": warehouse_type,
			},
			"name",
		)
		if warehouse:
			return warehouse

	frappe.throw(f"No Reserve or Raw Material warehouse in department {department}")


def route_material_request_to_operation_department(mr, mo):
	"""Point a still-Draft Material Request at a warehouse in ``mo``'s department.

	``doc_events.material_request.validate_mop_department`` measures a request by the
	department of its Request Items' warehouse, and the ``create_pmo`` fixture cannot satisfy
	it by choice of document:

	* every generated request is routed to the Manufacturer's reservation warehouse for the
	  row's variant -- M -> ``Waxing RSV - T``, D -> ``Diamond Setting RSV - T``,
	  F -> ``Central RSV - T``
	* every Manufacturing Operation is minted in ``Manufacturing Setting.default_department``,
	  ``Manufacturing Plan & Management - T``

	The two sets are disjoint, so neither pairing works -- not another request, and not
	another operation (the only other department an operation of this PMO carries is the FG
	one, ``Tagging - T``). The request is routed into the operation's department instead,
	while it is still Draft: ``Material Request Item.warehouse`` is not ``allow_on_submit``.

	Deliberately only the routing, and deliberately not a "Transfer to Department" workflow
	run: that route is gated on ``custom_operation_type``, a ``gke_customization`` field whose
	fixtures CI moves aside, so it does not exist on ``test_site`` at all.

	Every Stock Entry these tests assert against is unaffected. The reserve entry resolves its
	own target from the row's ``from_warehouse`` department
	(``doc_events.material_request.create_stock_entry``), and
	``make_department_mop_stock_entry`` -- the branch taken, since ``custom_department`` is
	always set on these requests -- sources the Work Order entry from the last Stock Entry
	booked against the request, reaching ``items[0].warehouse`` only on a fallback that
	cannot fire once the reserve entry exists.

	Deliberately NOT the operation's department: callers drive a Department IR *out of*
	``Manufacturing Plan & Management - T`` with this same operation immediately afterwards.
	"""
	department = mo.get("department") or frappe.db.get_value(
		"Manufacturing Operation", mo.name, "department"
	)
	if not department:
		frappe.throw(f"Manufacturing Operation {mo.name} has no department")

	# A no-op when the request is already there, so a fixture that one day grows a genuinely
	# matching request keeps its own routing.
	current = mr.items[0].warehouse if mr.items else None
	if (
		current
		and frappe.db.get_value("Warehouse", current, "department") == department
	):
		return current

	warehouse = department_staging_warehouse(department, mr.company)
	# Header and rows together: reset_default_field_value only clears set_warehouse when the
	# rows disagree with each other, so leaving it behind would strand a stale header.
	mr.set_warehouse = warehouse
	for row in mr.items:
		row.warehouse = warehouse
	mr.save()

	return warehouse


def mop_log_creation(mr_name, mo):
	mr = frappe.get_doc("Material Request", mr_name)
	route_material_request_to_operation_department(mr, mo)
	apply_workflow(mr, "Send for Reservation")
	apply_workflow(mr, "Reserve Material")
	apply_workflow(mr, "Transfer Material")
	mr.reload()
	mr.custom_manufacturing_operation = mo.name
	mr.save()
	apply_workflow(mr, "Transfer to MOP")
	return frappe.get_last_doc("MOP Log")
