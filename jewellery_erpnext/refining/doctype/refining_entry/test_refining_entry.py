from collections import defaultdict

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	create_scrap_wo_stock_entry,
	get_make_scrap_entry_rows,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
	mop_log_creation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.test_manufacturing_work_order import (
	create_pmo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.test_serial_number_creator import (
	create_snc,
)


class TestRefiningEntry(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.flt_pre = int(frappe.get_single_value("System Settings", "float_precision"))
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		return

	def test_dust_refining_entry(self):
		loss_entry()
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Dust Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.from_date = today()
		re.to_date = today()
		re.physical_quantity = 5.27
		re.save()

		re.append(
			"material_items",
			{
				"item_code": "Cap",
				"item_group": "Tools & Accessories",
				"qty": re.physical_quantity - re.system_quantity,
				"source_type": "Dust",
			},
		)
		re.save()

		self.assertGreater(re.physical_quantity, re.expected_recovery)

		apply_workflow(re, "Send for Verification")
		apply_workflow(re, "Submit")

		re.reload()

		self.assertIsNotNone(re.material_transfer_se)
		self.assertIsNotNone(re.receiving_se)

		se1 = frappe.get_doc("Stock Entry", re.material_transfer_se)
		se2 = frappe.get_doc("Stock Entry", re.receiving_se)

		self.assertEqual(se1.stock_entry_type, "Material Transfer")
		self.assertEqual(se1.items[0].s_warehouse, re.warehouse)
		self.assertEqual(se1.items[0].t_warehouse, re.refining_warehouse)
		self.assertEqual(se1.items[0].item_code, re.material_items[0].item_code)
		self.assertEqual(se1.items[0].qty, re.material_items[0].qty)
		self.assertEqual(se1.items[1].item_code, re.material_items[1].item_code)
		self.assertEqual(se1.items[1].qty, re.material_items[1].qty)

		self.assertEqual(se2.stock_entry_type, "Material Receipt")
		self.assertEqual(se2.items[0].t_warehouse, re.refining_warehouse)
		self.assertEqual(se2.items[0].item_code, re.material_items[2].item_code)
		self.assertEqual(
			se2.items[0].qty, round(re.material_items[2].qty, self.flt_pre)
		)

		dre = frappe.get_doc("Refining Entry", re.receive_materials())
		dre.generate_recovery_table()

		purity_qty = defaultdict(int)
		for row in dre.material_items:
			if "18KT" in row.item_code.split("-"):
				purity_qty["18KT"] += row.qty
			if "22KT" in row.item_code.split("-"):
				purity_qty["22KT"] += row.qty

		for row in dre.gold_recovery_details:
			self.assertEqual(row.input_weight, purity_qty[row.karat])

		dre.start_refining()
		dre.distribute_recovered_gold(3.578)

		self.assertEqual(dre.actual_recovery, 3.578)

		dre.verify_recovery()
		dre.complete_refining()

		self.assertIsNotNone(dre.repack_se)

		se3 = frappe.get_doc("Stock Entry", dre.repack_se)
		self.assertEqual(se3.stock_entry_type, "Manufacture")
		self.assertEqual(se3.custom_refining_entry, dre.name)
		for i in range(len(dre.material_items)):
			self.assertEqual(se3.items[i].s_warehouse, "Refining RM - T")
			self.assertIsNone(se3.items[i].t_warehouse)
			self.assertEqual(se3.items[i].item_code, dre.material_items[i].item_code)
			self.assertEqual(se3.items[i].qty, dre.material_items[i].qty)

		self.assertIsNone(se3.items[-1].s_warehouse)
		self.assertEqual(se3.items[-1].t_warehouse, "Refining RM - T")
		self.assertEqual(se3.items[-1].item_code, "M-G-24KT-99.9-Y")
		self.assertEqual(se3.items[-1].qty, dre.actual_recovery)

		dre.transfer_recovered_materials()

		se4 = frappe.get_doc("Stock Entry", dre.transfer_se)
		self.assertEqual(se4.stock_entry_type, "Material Transfer")
		self.assertEqual(se4.custom_refining_entry, dre.name)
		self.assertEqual(se4.items[0].s_warehouse, "Refining RM - T")
		self.assertEqual(se4.items[0].t_warehouse, "Central RM - T")
		self.assertEqual(se4.items[0].item_code, "M-G-24KT-99.9-Y")
		self.assertEqual(se4.items[0].qty, dre.actual_recovery)

	def test_work_order_refining(self):
		mwo = mwo_semi_finished_goods(self)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Work Order Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.scan_mwo_action(mwo.name)
		re.save()

		apply_workflow(re, "Send for Verification")
		apply_workflow(re, "Submit")
		self.assertEqual(
			frappe.db.get_value(
				"Manufacturing Operation",
				re.mwo_details[0].manufacturing_operation,
				"gross_wt",
			),
			0,
		)

		self.assertIsNotNone(re.material_transfer_se)

		se1 = frappe.get_doc("Stock Entry", re.material_transfer_se)
		self.assertEqual(se1.stock_entry_type, "Material Transfer")
		self.assertEqual(se1.items[0].s_warehouse, "MPM WO - T")
		self.assertEqual(se1.items[0].t_warehouse, re.refining_warehouse)
		self.assertEqual(se1.items[0].item_code, re.material_items[0].item_code)
		self.assertEqual(se1.items[0].qty, re.material_items[0].qty)
		self.assertEqual(se1.items[1].item_code, re.material_items[1].item_code)
		self.assertEqual(se1.items[1].qty, re.material_items[1].qty)

		dre = frappe.get_doc("Refining Entry", re.receive_materials())
		dre.generate_recovery_table()

		purity_qty = defaultdict(int)
		for row in dre.material_items:
			if "18KT" in row.item_code.split("-"):
				purity_qty["18KT"] += row.qty
			if "22KT" in row.item_code.split("-"):
				purity_qty["22KT"] += row.qty

		for row in dre.gold_recovery_details:
			self.assertEqual(row.input_weight, purity_qty[row.karat])

		dre.start_refining()
		dre.distribute_recovered_gold(1.191)
		dre.recovered_diamond[0].recovered_weight = 0.925

		self.assertEqual(dre.actual_recovery, 1.191)

		dre.verify_recovery()
		dre.complete_refining()

		self.assertIsNotNone(dre.repack_se)

		se2 = frappe.get_doc("Stock Entry", dre.repack_se)
		self.assertEqual(se2.stock_entry_type, "Manufacture")
		self.assertEqual(se2.custom_refining_entry, dre.name)
		for i in range(len(dre.material_items)):
			self.assertEqual(se2.items[i].s_warehouse, "Refining RM - T")
			self.assertIsNone(se2.items[i].t_warehouse)
			self.assertEqual(se2.items[i].item_code, dre.material_items[i].item_code)
			self.assertEqual(se2.items[i].qty, dre.material_items[i].qty)

		self.assertIsNone(se2.items[-1].s_warehouse)
		self.assertEqual(se2.items[-1].t_warehouse, "Refining RM - T")
		self.assertEqual(se2.items[-1].item_code, "D-NT-RO-6B-+9-9.5")
		self.assertEqual(se2.items[-1].qty, 0.925)

		self.assertIsNone(se2.items[-2].s_warehouse)
		self.assertEqual(se2.items[-2].t_warehouse, "Refining RM - T")
		self.assertEqual(se2.items[-2].item_code, "M-G-24KT-99.9-Y")
		self.assertEqual(se2.items[-2].qty, dre.actual_recovery)

		dre.transfer_recovered_materials()

		self.assertIsNotNone(dre.transfer_se)

		se3 = frappe.get_doc("Stock Entry", dre.transfer_se)
		self.assertEqual(se3.stock_entry_type, "Material Transfer")
		self.assertEqual(se3.custom_refining_entry, dre.name)
		self.assertEqual(se3.items[0].s_warehouse, "Refining RM - T")
		self.assertEqual(se3.items[0].t_warehouse, "Central RM - T")
		self.assertEqual(se3.items[0].item_code, "M-G-24KT-99.9-Y")
		self.assertEqual(se3.items[0].qty, dre.actual_recovery)
		self.assertEqual(se3.items[1].item_code, "D-NT-RO-6B-+9-9.5")
		self.assertEqual(se3.items[1].qty, 0.925)

	def test_serial_no_refining(self):
		snc = create_snc(self)
		snc.submit()
		sn = frappe.get_doc("Serial No", snc.fg_serial_no)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Serial Number Refining"
		re.company = "Test_Company"
		re.department = "Tagging - T"
		re.warehouse = "Tagging FG - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.scan_serial_no_action(sn.name)
		re.material_items.pop()
		re.save()

		apply_workflow(re, "Send for Verification")
		apply_workflow(re, "Submit")

		self.assertIsNotNone(re.material_transfer_se)

		se1 = frappe.get_doc("Stock Entry", re.material_transfer_se)
		self.assertEqual(se1.stock_entry_type, "Material Transfer")
		self.assertEqual(se1.items[0].s_warehouse, re.warehouse)
		self.assertEqual(se1.items[0].t_warehouse, re.refining_warehouse)
		self.assertEqual(se1.items[0].item_code, re.material_items[0].item_code)
		self.assertEqual(se1.items[0].qty, re.material_items[0].qty)

		dre = frappe.get_doc("Refining Entry", re.receive_materials())
		dre.generate_recovery_table()

		purity_qty = defaultdict(int)
		for row in dre.material_items:
			if "18KT" in row.item_code.split("-"):
				purity_qty["18KT"] += row.qty
			if "22KT" in row.item_code.split("-"):
				purity_qty["22KT"] += row.qty

		for row in dre.gold_recovery_details:
			self.assertEqual(row.input_weight, purity_qty[row.karat])

		dre.start_refining()
		dre.distribute_recovered_gold(1.191)
		dre.recovered_diamond[0].recovered_weight = 0.925

		self.assertEqual(dre.actual_recovery, 1.191)

		dre.verify_recovery()
		dre.complete_refining()
		self.assertIsNotNone(dre.repack_se)

		se2 = frappe.get_doc("Stock Entry", dre.repack_se)
		self.assertEqual(se2.stock_entry_type, "Manufacture")
		self.assertEqual(se2.custom_refining_entry, dre.name)
		self.assertEqual(se2.items[0].s_warehouse, "Refining RM - T")
		self.assertEqual(se2.items[0].item_code, dre.material_items[0].item_code)
		self.assertEqual(se2.items[0].qty, dre.material_items[0].qty)

		self.assertIsNone(se2.items[-1].s_warehouse)
		self.assertEqual(se2.items[-1].t_warehouse, "Refining RM - T")
		self.assertEqual(se2.items[-1].item_code, "D-NT-RO-6B-+9-9.5")
		self.assertEqual(se2.items[-1].qty, 0.925)

		self.assertIsNone(se2.items[-2].s_warehouse)
		self.assertEqual(se2.items[-2].t_warehouse, "Refining RM - T")
		self.assertEqual(se2.items[-2].item_code, "M-G-24KT-99.9-Y")
		self.assertEqual(se2.items[-2].qty, dre.actual_recovery)

		dre.transfer_recovered_materials()

		self.assertIsNotNone(dre.transfer_se)

		se4 = frappe.get_doc("Stock Entry", dre.transfer_se)
		self.assertEqual(se4.stock_entry_type, "Material Transfer")
		self.assertEqual(se4.custom_refining_entry, dre.name)
		self.assertEqual(se4.items[0].s_warehouse, "Refining RM - T")
		self.assertEqual(se4.items[0].t_warehouse, "Central RM - T")
		self.assertEqual(se4.items[0].item_code, "M-G-24KT-99.9-Y")
		self.assertEqual(se4.items[0].qty, dre.actual_recovery)
		self.assertEqual(se4.items[1].item_code, "D-NT-RO-6B-+9-9.5")
		self.assertEqual(se4.items[1].qty, 0.925)

		self.assertEqual(
			frappe.db.get_value("Serial No", dre.material_items[0].serial_no, "status"),
			"Inactive",
		)

	def test_scrap_refining(self):
		mwo = mwo_semi_finished_goods(self)
		mwo.reload()
		doc = frappe.get_doc("Manufacturing Operation", mwo.manufacturing_operation)
		rtn = get_make_scrap_entry_rows(doc.name)
		receive_entry = []
		for i in range(len(rtn["rows"])):
			receive_entry.append(
				{
					"idx": i,
					"stock_reservation_entry": rtn["rows"][i][
						"stock_reservation_entry"
					],
					"stock_reservation_entry_detail": rtn["rows"][i][
						"stock_reservation_entry_detail"
					],
					"item_code": rtn["rows"][i]["item_code"],
					"s_warehouse": rtn["rows"][i]["s_warehouse"],
					"qty": rtn["rows"][i]["reserved_qty"],
					"pcs": 0,
					"batch_no": rtn["rows"][i]["batch_no"],
				}
			)

		se_data = {
			"manufacturing_work_order": doc.manufacturing_work_order,
			"manufacturing_operation": doc.name,
			"manufacturing_order": doc.manufacturing_order,
			"department": doc.department,
			"receive_items": receive_entry,
		}
		create_scrap_wo_stock_entry(se_data)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Scrap Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.save()

		selected = re.get_scrap_items_balance()
		for row in selected:
			re.append(
				"material_items",
				{
					"item_code": row.get("item_code"),
					"item_group": row.get("item_group"),
					"warehouse": row.get("warehouse"),
					"batch_no": row.get("batch_no"),
					"qty": row.get("qty"),
					"uom": row.get("uom"),
					"purity": row.get("purity"),
					"source_type": "Scrap",
				},
			)
		re.save()

		apply_workflow(re, "Send for Verification")
		apply_workflow(re, "Submit")

		re.reload()

		se1 = frappe.get_doc("Stock Entry", re.material_transfer_se)
		self.assertEqual(se1.stock_entry_type, "Material Transfer")
		self.assertEqual(se1.items[0].s_warehouse, re.warehouse)
		self.assertEqual(se1.items[0].t_warehouse, re.refining_warehouse)
		self.assertEqual(se1.items[0].item_code, re.material_items[0].item_code)
		self.assertEqual(se1.items[0].qty, re.material_items[0].qty)

		dre = frappe.get_doc("Refining Entry", re.receive_materials())
		dre.generate_recovery_table()

		purity_qty = defaultdict(int)
		for row in dre.material_items:
			if "18KT" in row.item_code.split("-"):
				purity_qty["18KT"] += row.qty
			if "22KT" in row.item_code.split("-"):
				purity_qty["22KT"] += row.qty

		for row in dre.gold_recovery_details:
			self.assertEqual(row.input_weight, purity_qty[row.karat])

		dre.start_refining()
		dre.distribute_recovered_gold(1.191)
		dre.recovered_diamond[0].recovered_weight = 0.925

		self.assertEqual(dre.actual_recovery, 1.191)

		dre.verify_recovery()
		dre.complete_refining()
		self.assertIsNotNone(dre.repack_se)

		se2 = frappe.get_doc("Stock Entry", dre.repack_se)
		self.assertEqual(se2.stock_entry_type, "Manufacture")
		self.assertEqual(se2.custom_refining_entry, dre.name)
		self.assertEqual(se2.items[0].s_warehouse, "Refining RM - T")
		self.assertEqual(se2.items[0].item_code, dre.material_items[0].item_code)
		self.assertEqual(se2.items[0].qty, dre.material_items[0].qty)

		self.assertIsNone(se2.items[-1].s_warehouse)
		self.assertEqual(se2.items[-1].t_warehouse, "Refining RM - T")
		self.assertEqual(se2.items[-1].item_code, "D-NT-RO-6B-+9-9.5")
		self.assertEqual(se2.items[-1].qty, 0.925)

		self.assertIsNone(se2.items[-2].s_warehouse)
		self.assertEqual(se2.items[-2].t_warehouse, "Refining RM - T")
		self.assertEqual(se2.items[-2].item_code, "M-G-24KT-99.9-Y")
		self.assertEqual(se2.items[-2].qty, dre.actual_recovery)

		dre.transfer_recovered_materials()

		self.assertIsNotNone(dre.transfer_se)

		se4 = frappe.get_doc("Stock Entry", dre.transfer_se)
		self.assertEqual(se4.stock_entry_type, "Material Transfer")
		self.assertEqual(se4.custom_refining_entry, dre.name)
		self.assertEqual(se4.items[0].s_warehouse, "Refining RM - T")
		self.assertEqual(se4.items[0].t_warehouse, "Central RM - T")
		self.assertEqual(se4.items[0].item_code, "M-G-24KT-99.9-Y")
		self.assertEqual(se4.items[0].qty, dre.actual_recovery)
		self.assertEqual(se4.items[1].item_code, "D-NT-RO-6B-+9-9.5")
		self.assertEqual(se4.items[1].qty, 0.925)

	def test_validate_configuration(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Dust Refining"
		re.multiple_department = 1
		re.multiple_operation = 1
		self.assertRaises(frappe.ValidationError, re.validate)

	def test_validate_physical_quantity_zero(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Dust Refining"
		re.physical_quantity = 0
		self.assertRaises(frappe.ValidationError, re.validate)

	def test_validate_physical_quantity_negative(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Dust Refining"
		re.physical_quantity = -5.0
		self.assertRaises(frappe.ValidationError, re.validate)

	def test_submit_fails_without_materials(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Scrap Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.insert(ignore_mandatory=True)
		self.assertRaises(frappe.ValidationError, re.before_submit)

	def test_submit_fails_missing_refining_warehouse(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Scrap Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Invalid_Dept"
		re.refining_warehouse = None
		self.assertRaises(frappe.ValidationError, re.before_submit)

	def test_source_department_mismatch_mwo(self):
		mwo = mwo_semi_finished_goods(self)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Work Order Refining"
		re.company = "Test_Company"
		re.department = "Tagging - T"
		re.refining_department = "Refinery - T"
		self.assertRaises(frappe.ValidationError, re.scan_mwo_action, mwo.name)

	def test_invalid_mwo_scan(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Work Order Refining"
		self.assertRaises(frappe.ValidationError, re.scan_mwo_action, "INVALID_MWO_123")

	def test_invalid_serial_no_scan(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Serial Number Refining"
		self.assertRaises(
			frappe.ValidationError, re.scan_serial_no_action, "INVALID_SN_123"
		)

	def test_serial_no_not_in_source_warehouse(self):
		snc = create_snc(self)
		snc.submit()
		sn = frappe.get_doc("Serial No", snc.fg_serial_no)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Serial Number Refining"
		re.company = "Test_Company"
		re.warehouse = "Wrong Warehouse - T"

		self.assertRaises(frappe.ValidationError, re.scan_serial_no_action, sn.name)

	def test_recovery_exceeds_input_validation(self):
		loss_entry()
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Dust Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.from_date = today()
		re.to_date = today()
		re.physical_quantity = 5.27
		re.save()

		re.append(
			"material_items",
			{
				"item_code": "Cap",
				"item_group": "Tools & Accessories",
				"qty": re.physical_quantity - re.system_quantity,
				"source_type": "Dust",
			},
		)
		re.save()
		apply_workflow(re, "Send for Verification")
		apply_workflow(re, "Submit")

		dre = frappe.get_doc("Refining Entry", re.receive_materials())
		dre.generate_recovery_table()
		dre.start_refining()

		dre.append(
			"refined_gold", {"refining_gold_weight": 9999.0, "pure_weight": 9999.0}
		)
		dre.status = "Recovery Entered"
		self.assertRaises(frappe.ValidationError, dre.validate)

	def tearDown(self):
		return super().tearDown()


def loss_entry():
	se = frappe.new_doc("Stock Entry")
	se.company = "Test_Company"
	se.manufacturer = "Shubh"
	se.branch = frappe.db.exists("Branch", {"branch_name": "Test Branch"})
	se.stock_entry_type = "Material Receipt"
	se.append(
		"items",
		{
			"t_warehouse": "Waxing Scrap - T",
			"item_code": "ML-G-18KT-75.4-P",
			"qty": 2.71,
		},
	)
	se.append(
		"items",
		{
			"t_warehouse": "Waxing Scrap - T",
			"item_code": "ML-G-22KT-91.9-Y",
			"qty": 1.67,
		},
	)
	se.save()
	se.submit()


def mwo_semi_finished_goods(self):
	pmo = create_pmo(self)
	mwo_list = frappe.get_all(
		"Manufacturing Work Order",
		filters={"manufacturing_order": pmo.name},
		fields=["name", "department", "manufacturing_order"],
	)

	mr_list = frappe.get_all(
		"Material Request",
		filters={
			"manufacturing_order": mwo_list[0].manufacturing_order,
			"docstatus": 0,
		},
		pluck="name",
	)
	for row in mwo_list:
		if row.department == "Manufacturing Plan & Management - T":
			mwo = frappe.get_doc("Manufacturing Work Order", row.name)
			mwo.submit()
			mo_man = frappe.get_last_doc(
				"Manufacturing Operation",
				filters={"manufacturing_work_order": mwo.name},
			)

			if mr_list:
				mop_log_creation(mr_list[0], mo_man)
				mop_log_creation(mr_list[1], mo_man)

			dir_issue = dir_for_issue(
				"Manufacturing Plan & Management - T", "Waxing - T", mo_man
			)
			mo_man.reload()

			dir_for_receive(dir_issue)

	return mwo