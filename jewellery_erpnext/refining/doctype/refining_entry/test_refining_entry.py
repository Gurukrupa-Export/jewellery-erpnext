import re
from collections import defaultdict
from unittest.mock import patch

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, today

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	_create_scrap_batch,
	_resolve_unused_loose_item,
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
from jewellery_erpnext.patches.rename_refining_scrap_terminology_data import (
	_apply_swap as apply_terminology_swap,
)
from jewellery_erpnext.patches.rename_refining_scrap_terminology_data import (
	_clear_sentinel as clear_swap_sentinel,
)
from jewellery_erpnext.patches.rename_refining_scrap_terminology_data import (
	_set_sentinel as set_swap_sentinel,
)
from jewellery_erpnext.patches.rename_refining_scrap_terminology_data import (
	execute as swap_terminology,
)
from jewellery_erpnext.patches.rename_refining_scrap_terminology_metadata import (
	RENAMED_SELECTS,
)
from jewellery_erpnext.refining.constants import (
	BATCH_TYPE_UNUSED,
	REFINING_TYPE_OPTIONS,
	REFINING_TYPE_SCRAP,
	REFINING_TYPE_SERIAL,
	REFINING_TYPE_UNUSED,
	REFINING_TYPE_WORK_ORDER,
	REFINING_TYPES,
	SOURCE_TYPE_SCRAP,
	SOURCE_TYPE_UNUSED,
)
from jewellery_erpnext.refining.doctype.refinery_price_list.refinery_price_list import (
	build_refinery_price_index,
)
from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
	PURE_LOSS_ITEM,
)

#: Scrap Refining is the operator's weigh-in type: validate_quantities requires a
#: Physical Quantity > 0. Tests that exercise something else (restrictions, the rename
#: patch) still have to supply one.
#:
#: Deliberately far larger than anything the shared test site accumulates in
#: "Waxing Scrap - T": these tests all run against ONE site and every loss_entry() call
#: receipts another 4.38 g that the next Scrap Refining entry auto-fetches, so the system
#: quantity depends on how many tests ran first — i.e. on the alphabetical method order.
#: A fixed, generously large weigh-in keeps difference_quantity positive wherever the test
#: lands. Assert on system_quantity, never on this number.
PLACEHOLDER_PHYSICAL_QTY = 500.0


class TestRefiningEntry(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.flt_pre = int(frappe.get_single_value("System Settings", "float_precision"))
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		return

	def test_scrap_refining_entry(self):
		loss_entry()
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Scrap Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.from_date = today()
		re.to_date = today()
		# Positive weigh-in so the first save clears validate_quantities; the real figure is
		# derived below, once the auto-fetch has populated System Quantity.
		re.physical_quantity = PLACEHOLDER_PHYSICAL_QTY
		re.save()

		# "Waxing Scrap - T" is shared by the whole suite and every loss_entry() receipts
		# another 4.38 g into it, so the fetched system quantity depends on how many tests
		# ran before this one — i.e. on alphabetical method order. Derive the weigh-in from
		# what was actually fetched, keeping the 0.89 g of physical-over-system excess these
		# tests exercise, instead of a fixed 5.27 total that only held while this test ran
		# first and that silently goes NEGATIVE once enough scrap has accumulated.
		re.physical_quantity = flt(re.system_quantity) + 0.89

		re.append(
			"material_items",
			{
				"item_code": "Cap",
				"item_group": "Tools & Accessories",
				"qty": re.physical_quantity - re.system_quantity,
				"source_type": "Scrap",
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
		for item in se1.items:
			self.assertEqual(item.s_warehouse, re.warehouse)
			self.assertEqual(item.t_warehouse, re.refining_warehouse)

		# The material table aggregates per ITEM, while the transfer splits per BATCH — one
		# row per loss_entry() receipt still sitting in the scrap warehouse — so the two
		# agree on totals, not row for row. Comparing se1.items[i] to material_items[i]
		# only held while this test ran first and each item had exactly one batch.
		transferred = defaultdict(float)
		for item in se1.items:
			transferred[item.item_code] += flt(item.qty)
		fetched = re.material_items[:2]
		self.assertEqual(len(transferred), len(fetched))
		for row in fetched:
			self.assertEqual(
				flt(transferred[row.item_code], self.flt_pre),
				flt(row.qty, self.flt_pre),
			)

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
		# 3.578 g was the pure content of ONE loss_entry() sweep — i.e. a 100% recovery,
		# zero loss. Once the scrap warehouse accumulates, a fixed figure silently becomes a
		# PARTIAL recovery and the repack's output rows no longer match actual_recovery.
		# Recover the whole pure input so the scenario stays "fully recovered" wherever this
		# test lands in the alphabetical run order; on a site where only one sweep exists
		# this is still exactly 3.578.
		full_recovery = flt(
			sum(flt(row.pure_gold_weight) for row in dre.gold_recovery_details), 3
		)
		dre.distribute_recovered_gold(full_recovery)

		self.assertEqual(dre.actual_recovery, full_recovery)

		dre.verify_recovery()
		dre.complete_refining()

		self.assertIsNotNone(dre.repack_se)

		se3 = frappe.get_doc("Stock Entry", dre.repack_se)
		self.assertEqual(se3.stock_entry_type, "Manufacture")
		self.assertEqual(se3.custom_refining_entry, dre.name)
		# Same per-ITEM table against per-BATCH stock entry as se1 above: the repack issues
		# one row per batch, so the consumption side agrees on totals, not row for row.
		consumed = defaultdict(float)
		for item in se3.items:
			if not item.s_warehouse:
				continue
			self.assertEqual(item.s_warehouse, "Refining RM - T")
			self.assertIsNone(item.t_warehouse)
			consumed[item.item_code] += flt(item.qty)

		self.assertEqual(
			sorted(consumed), sorted(row.item_code for row in dre.material_items)
		)
		for row in dre.material_items:
			self.assertEqual(
				flt(consumed[row.item_code], self.flt_pre),
				flt(row.qty, self.flt_pre),
			)

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

	def test_unused_loose_material_refining(self):
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
		# "Waxing RM - T" is shared by the whole suite, and get_scrap_items_balance() sweeps
		# EVERY Unused/Loose Material batch sitting in it — including the ones other tests
		# minted. Record which batches this entry creates so the refining entry below is
		# built from exactly one MWO's material, whatever else has accumulated.
		before = set(frappe.get_all("Batch", pluck="name"))
		create_scrap_wo_stock_entry(se_data)
		minted = set(frappe.get_all("Batch", pluck="name")) - before

		# The received material is booked onto the DEDICATED unused/loose item, not onto
		# the production code it was issued as: M- becomes ML- and F- becomes FL-. Rows
		# with no mapping (diamonds, gemstones) keep their own code, so assert the absence
		# of M/F rather than the presence of only ML/FL.
		minted_templates = {
			frappe.db.get_value("Item", item, "variant_of")
			for item in frappe.get_all(
				"Batch", {"name": ["in", list(minted)]}, pluck="item"
			)
		}
		self.assertIn("ML", minted_templates)
		self.assertFalse(
			minted_templates & {"M", "F"},
			f"unused/loose batches must not stay on the production item: {minted_templates}",
		)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Unused/Loose Material Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.save()

		selected = [
			row for row in re.get_scrap_items_balance() if row.get("batch_no") in minted
		]
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
					"source_type": "Unused/Loose Material",
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

	def test_unused_loose_material_employee_filter(self):
		"""Employee-wise issuance: Receive Unused/Loose Material stamps the operation's
		employee onto the minted batch (Batch.custom_employee), and Unused/Loose Material
		Refining returns only that employee's batches when an Employee is selected."""
		emp = "HR-EMP-00002"
		mwo = mwo_semi_finished_goods(self)
		mwo.reload()
		doc = frappe.get_doc("Manufacturing Operation", mwo.manufacturing_operation)
		# Attribute the operation (and thus its scrap) to a known employee so the minted
		# Scrap batch is stamped with custom_employee (see _create_scrap_batch).
		doc.db_set("employee", emp)

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
		before = set(frappe.get_all("Batch", pluck="name"))
		create_scrap_wo_stock_entry(se_data)
		fresh = set(frappe.get_all("Batch", pluck="name")) - before

		# Part B: the freshly minted Scrap batch(es) carry the operation's employee.
		# Scoped to what THIS receive minted: the warehouse and the employee are shared by
		# the whole suite, so an unscoped query also returns batches left by other tests —
		# and, on a site that is reused rather than rebuilt, by earlier runs.
		minted = fresh & set(
			frappe.get_all(
				"Batch",
				{"custom_batch_type": "Unused/Loose Material", "custom_employee": emp},
				pluck="name",
			)
		)
		self.assertTrue(
			minted, "Receive-Scrap must stamp the operation employee on the Scrap batch"
		)
		# ...and they sit on the unused/loose item, never on the production code.
		self.assertFalse(
			{
				frappe.db.get_value("Item", item, "variant_of")
				for item in frappe.get_all(
					"Batch", {"name": ["in", list(minted)]}, pluck="item"
				)
			}
			& {"M", "F"}
		)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Unused/Loose Material Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.save()

		# Part C: selecting the employee returns that employee's scrap batches...
		re.employee = emp
		owned = {r["batch_no"] for r in re.get_scrap_items_balance()}
		self.assertTrue(
			minted <= owned,
			"employee filter must return the selected employee's scrap batches",
		)

		# ...and a different employee (who owns no scrap here) does not see them.
		re.employee = "__NO_SUCH_EMPLOYEE__"
		other = {r["batch_no"] for r in re.get_scrap_items_balance()}
		self.assertFalse(
			minted & other, "a non-owning employee must not see the scrap batches"
		)

	# --- Unused/loose target item resolution (Receive Unused/Loose Material) ---
	#
	# The target is resolved from the TEMPLATE plus the source item's own Metal Purity
	# and Metal Colour, so nothing is pinned to a particular site's item master. Metal
	# and Finding only; anything else keeps its own item code.

	def test_unused_loose_resolver_maps_metal_to_ml(self):
		self.assertEqual(
			_resolve_unused_loose_item("M-G-22KT-91.9-Y"), "ML-G-22KT-91.9-Y"
		)

	def test_unused_loose_resolver_maps_finding_to_fl(self):
		self.assertEqual(
			_resolve_unused_loose_item("F-G-22KT-91.9-Y-CHA-KC-2.50 MM"),
			"FL-G-22KT-91.9-Y-CHA-KC-2.50 MM",
		)

	def test_unused_loose_resolver_ignores_unmapped_templates(self):
		"""Diamonds have no unused/loose counterpart — the row keeps its own item code."""
		diamond = frappe.db.get_value("Item", {"variant_of": "D"}, "name")
		self.assertTrue(diamond, "test data must contain at least one D variant")
		self.assertIsNone(_resolve_unused_loose_item(diamond))

	def _metal_variant(self, item_code, attributes):
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"gst_hsn_code": "010121",
				"item_group": "Metal - V",
				"stock_uom": "Gram",
				"is_stock_item": 1,
				"variant_of": "M",
				"variant_based_on": "Item Attribute",
				"attributes": [
					dict(variant_of="M", attribute=a, attribute_value=v)
					for a, v in attributes
				],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc, "Item", doc.name, force=1, ignore_permissions=True
		)
		return doc

	def test_unused_loose_resolver_ignores_alloys_with_neither_attribute(self):
		"""An alloy (M-AL, M-Genia-221) carries a Metal Type but neither purity nor colour,
		so there is nothing to match on at all. It must fall through unchanged rather than
		block a receive that works today — 17 such items are live and in active stock use."""
		alloy = self._metal_variant("M-TEST-ALLOY", [("Metal Type", "Gold")])
		self.assertIsNone(_resolve_unused_loose_item(alloy.name))

	def test_unused_loose_resolver_throws_on_a_half_configured_item(self):
		"""Purity set but no colour is not an alloy — it is a half-filled item master, and
		silently keeping the material on its own code hides that. Name what is missing."""
		half = self._metal_variant(
			"M-TEST-NO-COLOUR",
			[("Metal Type", "Gold"), ("Metal Purity", "91.9")],
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			_resolve_unused_loose_item(half.name)
		message = frappe.utils.strip_html(str(caught.exception))
		self.assertIn("M-TEST-NO-COLOUR", message)
		self.assertIn("Metal Colour", message)

	def test_unused_loose_resolver_throws_when_no_target_exists(self):
		frappe.db.set_value("Item", "ML-G-22KT-91.9-Y", "disabled", 1)
		try:
			with self.assertRaises(frappe.ValidationError) as caught:
				_resolve_unused_loose_item("M-G-22KT-91.9-Y")
			message = str(caught.exception)
			self.assertIn("91.9", message)
			self.assertIn("Yellow", message)
		finally:
			frappe.db.set_value("Item", "ML-G-22KT-91.9-Y", "disabled", 0)

	def test_unused_loose_resolver_throws_when_more_than_one_target_matches(self):
		"""Purity + colour alone can match several variants. That is a hard error naming
		the candidates — never a guess, which would book the metal at the wrong purity."""
		ambiguous = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "ML-TEST-AMBIGUOUS",
				"item_name": "ML-TEST-AMBIGUOUS",
				"gst_hsn_code": "010121",
				"item_group": "Metal - V",
				"stock_uom": "Gram",
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
				"variant_of": "ML",
				"variant_based_on": "Item Attribute",
				# Deliberately no Metal Touch: a DIFFERENT attribute set, so ERPNext does
				# not reject it as a duplicate variant, that still collides on purity+colour.
				"attributes": [
					{
						"variant_of": "ML",
						"attribute": "Metal Type",
						"attribute_value": "Gold",
					},
					{
						"variant_of": "ML",
						"attribute": "Metal Purity",
						"attribute_value": "91.9",
					},
					{
						"variant_of": "ML",
						"attribute": "Metal Colour",
						"attribute_value": "Yellow",
					},
				],
			}
		).insert(ignore_permissions=True)
		try:
			with self.assertRaises(frappe.ValidationError) as caught:
				_resolve_unused_loose_item("M-G-22KT-91.9-Y")
			message = str(caught.exception)
			self.assertIn("ML-G-22KT-91.9-Y", message)
			self.assertIn(ambiguous.name, message)
		finally:
			frappe.delete_doc("Item", ambiguous.name, force=1, ignore_permissions=True)

	def test_scrap_batch_is_not_minted_for_a_non_batch_item(self):
		"""_create_scrap_batch returns None for a non-batch item — the signal the repack
		turns into a throw when the item was a RESOLVED unused/loose target, since such
		material could never be tagged and would vanish from the refining pool."""
		self.assertIsNone(_create_scrap_batch("REF-SVC-001"))

	def test_external_refinery_entry(self):
		gold_receipt("Waxing RM - T", "ML-G-18KT-75.4-P", 5.0)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Unused/Loose Material Refining"
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		re.append(
			"material_items",
			{
				"item_code": "ML-G-18KT-75.4-P",
				"warehouse": "Waxing RM - T",
				"qty": 5.0,
				"source_type": "Unused/Loose Material",
			},
		)
		re.save()

		apply_workflow(re, "Submit")
		re.reload()

		self.assertEqual(re.qty_to_refine, 5.0)
		self.assertIsNotNone(re.supplier_warehouse)

		# A Flat Charge is a per-consignment fee, so the line keeps qty 1 / Nos and the
		# agreed amount in rate. The weight stays visible in custom_gross_wt.
		po = frappe.get_doc("Purchase Order", re.refining_entry_po)
		self.assertEqual(len(po.items), 1)
		self.assertEqual(po.items[0].qty, 1)
		self.assertEqual(po.items[0].uom, "Nos")
		self.assertEqual(po.items[0].rate, 750)
		self.assertEqual(po.items[0].amount, 750)
		self.assertEqual(po.items[0].custom_gross_wt, 5.0)
		self.assertIsNotNone(re.material_transfer_se)

		# Category pricing: a Scrap-type external entry defaults its Pricing Category
		# to Metal Refining Scrap (REF-RMS-001, seeded by seed_refining_masters /
		# seed_refinery_price_list), whose 0-50 g Gross-Weight slab is Flat 750 — one
		# PO line, billed at submit. A PO must exist for EVERY external entry.
		self.assertEqual(re.pricing_item, "REF-RMS-001")
		self.assertIsNotNone(re.refining_entry_po)
		po = frappe.get_doc("Purchase Order", re.refining_entry_po)
		self.assertEqual(len(po.items), 1)
		self.assertEqual(po.items[0].rate, 750)
		self.assertEqual(po.items[0].custom_gross_wt, 5.0)

		se1 = frappe.get_doc("Stock Entry", re.material_transfer_se)
		self.assertEqual(se1.stock_entry_type, "Material Transfer")
		self.assertEqual(se1.items[0].s_warehouse, "Waxing RM - T")
		self.assertEqual(se1.items[0].t_warehouse, re.supplier_warehouse)
		self.assertEqual(se1.items[0].item_code, "ML-G-18KT-75.4-P")
		self.assertEqual(se1.items[0].qty, 5.0)
		self.assertEqual(se1.to_subcontractor, "Test_Supplier")

		supplier_qty_after_send = frappe.db.get_value(
			"Bin",
			{"item_code": "ML-G-18KT-75.4-P", "warehouse": re.supplier_warehouse},
			"actual_qty",
		)
		self.assertEqual(flt(supplier_qty_after_send, self.flt_pre), 5.0)

		# Receive: single Manufacture SE, no second Refining Entry. Recovery weight (3.6)
		# is chosen below the item's actual pure content (5.0 * 75.4% = 3.77) so the
		# Recovery Summary reflects a realistic, non-clamped recovery percentage.
		se_name = re.receive_from_supplier(recovery_weight=3.6)
		re.reload()

		self.assertEqual(re.received_weight, 3.6)
		self.assertEqual(re.repack_se, se_name)
		self.assertEqual(re.status, "Transferred")

		# Recovery Summary and Gold Recovery Details are populated by reusing
		# generate_recovery_table() — the same proportional-by-pure-content
		# distribution the other 4 refining types use — so Gross Pure Weight is the
		# item's actual pure content (5.0g * 75.4%), not its raw gross weight.
		self.assertEqual(re.gross_pure_weight, 3.77)
		self.assertEqual(re.expected_recovery, 3.77)
		self.assertEqual(re.actual_recovery, 3.6)
		self.assertEqual(re.refined_fine_weight, 3.6)
		self.assertEqual(re.refining_loss, 0.17)
		self.assertEqual(re.recovery_percentage, 95.49)

		self.assertEqual(len(re.gold_recovery_details), 1)
		grd = re.gold_recovery_details[0]
		self.assertEqual(grd.purity_percentage, 75.4)
		self.assertEqual(grd.input_weight, 5.0)
		self.assertEqual(grd.pure_gold_weight, 3.77)
		self.assertEqual(grd.recovered_weight, 3.6)

		self.assertEqual(len(re.refined_gold), 1)
		self.assertEqual(re.refined_gold[0].item_code, re.refined_metal_item)
		self.assertEqual(re.refined_gold[0].refining_gold_weight, 3.6)

		repack = frappe.get_doc("Stock Entry", re.repack_se)
		self.assertEqual(repack.stock_entry_type, "Manufacture")
		self.assertEqual(repack.items[0].item_code, "ML-G-18KT-75.4-P")
		self.assertEqual(repack.items[0].qty, 5.0)
		self.assertEqual(repack.items[0].s_warehouse, re.supplier_warehouse)
		self.assertIsNone(repack.items[0].t_warehouse)
		self.assertEqual(repack.items[1].item_code, re.refined_metal_item)
		self.assertEqual(repack.items[1].qty, 3.6)
		self.assertIsNone(repack.items[1].s_warehouse)
		self.assertEqual(repack.items[1].t_warehouse, "Waxing RM - T")

		supplier_qty_after_receive = frappe.db.get_value(
			"Bin",
			{"item_code": "ML-G-18KT-75.4-P", "warehouse": re.supplier_warehouse},
			"actual_qty",
		)
		self.assertEqual(flt(supplier_qty_after_receive, self.flt_pre), 0.0)

		# A second receive attempt against the same entry must be rejected.
		self.assertRaises(
			frappe.ValidationError, re.receive_from_supplier, recovery_weight=1.0
		)

		# Cancelling reverses both the transfer and the repack Stock Entries.
		re.cancel()
		se1.reload()
		repack.reload()
		self.assertEqual(se1.docstatus, 2)
		self.assertEqual(repack.docstatus, 2)

	def test_external_refining_po_one_line_per_price_list(self):
		"""External scrap consignment mixing a metal-loss item (covered by no price list of
		its own) with two priced categories → ONE Purchase Order line per matched Refinery
		Price List. The ML-G row falls back to the entry's Pricing Category (the Dust Item,
		REF-MD-001); REF-UL-001 (Ultra Liquid) and REF-NB-001 (Napkin/Thread/Buff) match on
		their own category item. Weight-based slabs now carry the WEIGHT as qty with a
		per-unit rate, so amount = qty x rate; the Flat band keeps qty 1."""
		# Stage the physical material in the source warehouse so the submit-time transfer
		# to the supplier warehouse has stock to move.
		gold_receipt("Waxing RM - T", "ML-G-18KT-75.4-P", 30.0)
		gold_receipt("Waxing RM - T", "REF-UL-001", 2000.0)
		gold_receipt("Waxing RM - T", "REF-NB-001", 500.0)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Scrap Refining"
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.manufacturer = "Shubh"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		# Physical == the material total, so there is no physical-over-system excess to
		# receipt (create_dust_opening_receipt_se early-returns with no shortfall) and
		# validate_dust_opening_material passes.
		re.physical_quantity = 2530.0
		re.append(
			"material_items",
			{
				"item_code": "ML-G-18KT-75.4-P",
				"warehouse": "Waxing RM - T",
				"qty": 30.0,
				"uom": "Gram",
				"source_type": "Scrap",
			},
		)
		re.append(
			"material_items",
			{
				"item_code": "REF-UL-001",
				"warehouse": "Waxing RM - T",
				"qty": 2000.0,
				"uom": "Litre",
				"source_type": "Consumable",
				"is_consumable": 1,
			},
		)
		re.append(
			"material_items",
			{
				"item_code": "REF-NB-001",
				"warehouse": "Waxing RM - T",
				"qty": 500.0,
				"uom": "Gram",
				"source_type": "Scrap",
			},
		)
		re.save()

		apply_workflow(re, "Submit")
		re.reload()

		# Only the gold alloy is melted; UL/NB are not gold items.
		self.assertEqual(re.qty_to_refine, 30.0)
		self.assertEqual(re.pricing_item, "REF-MD-001")
		self.assertIsNotNone(re.refining_entry_po)

		po = frappe.get_doc("Purchase Order", re.refining_entry_po)
		self.assertEqual(len(po.items), 3)

		# Match lines by their price-list back-link (the parent Refinery Price List doc),
		# not by index, since dict-insertion order is an implementation detail.
		lines = {row.custom_refining_price_list: row for row in po.items}

		def price_list_of(item):
			return frappe.db.get_value("Refinery Price List", {"item": item}, "name")

		# Flat Charge → qty 1 / Nos, the agreed 800 in rate, weight in custom_gross_wt.
		md = lines[price_list_of("REF-MD-001")]
		self.assertEqual(md.custom_gross_wt, 30.0)
		self.assertEqual(md.qty, 1)
		self.assertEqual(md.uom, "Nos")
		self.assertEqual(md.rate, 800)
		self.assertEqual(md.amount, 800)

		# Per Kg 1800 → 1.8 per Gram on 500 g = 900, and the qty is the real weight.
		nb = lines[price_list_of("REF-NB-001")]
		self.assertEqual(nb.custom_gross_wt, 500.0)
		self.assertEqual(nb.qty, 500.0)
		self.assertEqual(nb.uom, "Gram")
		self.assertEqual(nb.rate, 1.8)
		self.assertEqual(nb.amount, 900)

		# Per Kg 2000 → 2.0 per Litre on 2000 L = 4000. Litres and grams never share a line.
		ul = lines[price_list_of("REF-UL-001")]
		self.assertEqual(ul.custom_gross_wt, 2000.0)
		self.assertEqual(ul.qty, 2000.0)
		self.assertEqual(ul.uom, "Litre")
		self.assertEqual(ul.rate, 2.0)
		self.assertEqual(ul.amount, 4000)

		# The literal requirement, asserted for every line on the PO.
		for row in po.items:
			self.assertAlmostEqual(row.amount, row.qty * row.rate, places=2)

	def test_external_refining_po_unpriced_category_line(self):
		"""A category whose weight falls outside every slab band gets a rate-0 line for
		manual pricing (no double counting, no dropped line). REF-UL-001's only slab is
		"After burn >1kg" (from 1000 g), so a 20 g Ultra Liquid row matches nothing."""
		gold_receipt("Waxing RM - T", "ML-G-18KT-75.4-P", 30.0)
		gold_receipt("Waxing RM - T", "REF-UL-001", 20.0)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Scrap Refining"
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.manufacturer = "Shubh"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		re.physical_quantity = 50.0
		re.append(
			"material_items",
			{
				"item_code": "ML-G-18KT-75.4-P",
				"warehouse": "Waxing RM - T",
				"qty": 30.0,
				"uom": "Gram",
				"source_type": "Scrap",
			},
		)
		re.append(
			"material_items",
			{
				"item_code": "REF-UL-001",
				"warehouse": "Waxing RM - T",
				"qty": 20.0,
				"uom": "Litre",
				"source_type": "Consumable",
				"is_consumable": 1,
			},
		)
		re.save()

		apply_workflow(re, "Submit")
		re.reload()

		po = frappe.get_doc("Purchase Order", re.refining_entry_po)
		self.assertEqual(len(po.items), 2)
		lines = {row.custom_refining_price_list: row for row in po.items}

		md = lines[
			frappe.db.get_value("Refinery Price List", {"item": "REF-MD-001"}, "name")
		]
		self.assertEqual(md.rate, 800)
		# The band-gap line now carries the RESOLVED price list, so the buyer knows which
		# list has the gap, and bills the weight at rate 0.
		ul = lines[
			frappe.db.get_value("Refinery Price List", {"item": "REF-UL-001"}, "name")
		]
		self.assertEqual(ul.custom_gross_wt, 20.0)
		self.assertEqual(ul.qty, 20.0)
		self.assertEqual(ul.uom, "Litre")
		self.assertEqual(ul.rate, 0)

	def test_external_po_consumable_gets_its_own_line(self):
		"""A consumable must NEVER be folded into the material line, even when it matches no
		price list — otherwise its weight silently bills at the material's rate. Mirrors
		production, where consumables carry is_consumable = 0 and are identified only by
		source_type."""
		gold_receipt("Waxing RM - T", "ML-G-18KT-75.4-P", 30.0)
		gold_receipt("Waxing RM - T", "Cap", 3.0)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.manufacturer = "Shubh"
		# before_submit_external defaults this from EXTERNAL_PRICING_CATEGORY, and this test
		# calls _external_po_lines() straight after save() without submitting. Set it here,
		# as test_external_po_uses_the_covered_items_mapping does: without it the MATERIAL
		# row has no price list to fall back on and lands in `unpriced` too, so the split
		# this test is about would pass for the wrong reason.
		re.pricing_item = "REF-MD-001"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		re.physical_quantity = 33.0
		re.append(
			"material_items",
			{
				"item_code": "ML-G-18KT-75.4-P",
				"warehouse": "Waxing RM - T",
				"qty": 30.0,
				"uom": "Gram",
				"source_type": SOURCE_TYPE_SCRAP,
			},
		)
		re.append(
			"material_items",
			{
				"item_code": "Cap",
				"warehouse": "Waxing RM - T",
				"qty": 3.0,
				"uom": "Gram",
				"source_type": "Consumable",
				"is_consumable": 0,
			},
		)
		re.save()

		lines, unpriced = re._external_po_lines()
		self.assertEqual(len(lines), 2)

		md_list = frappe.db.get_value(
			"Refinery Price List", {"item": "REF-MD-001"}, "name"
		)
		material = [
			line for line in lines if line["custom_refining_price_list"] == md_list
		]
		self.assertEqual(len(material), 1)
		# The consumable's 3 g must NOT be in the material weight.
		self.assertEqual(material[0]["custom_gross_wt"], 30.0)

		consumable = [
			line for line in lines if line["custom_refining_price_list"] != md_list
		]
		self.assertEqual(len(consumable), 1)
		self.assertEqual(consumable[0]["custom_gross_wt"], 3.0)
		self.assertEqual(consumable[0]["rate"], 0)
		self.assertEqual(len(unpriced), 1)

	def test_external_po_consumable_rows_merge_per_item(self):
		"""Two rows of the SAME consumable still merge, at a summed qty."""
		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.manufacturer = "Shubh"
		re.pricing_item = "REF-MD-001"
		for qty in (2.0, 3.0):
			re.append(
				"material_items",
				{
					"item_code": "Cap",
					"qty": qty,
					"uom": "Gram",
					"source_type": "Consumable",
				},
			)
		groups = re._external_po_groups(build_refinery_price_index(re.refining_type))
		self.assertEqual(len(groups), 1)
		self.assertEqual(groups[0]["qty"], 5.0)
		self.assertEqual(groups[0]["consumable"], "Cap")

	def test_external_po_never_mixes_uoms_and_excludes_returned_stones(self):
		"""Grams and carats were being summed into one weight. Two independent fixes: the
		returned-intact rows are not billed at all, and the grouping key includes the UOM so
		they could never share a line even if they were."""
		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.manufacturer = "Shubh"
		re.pricing_item = "REF-MD-001"
		re.append(
			"material_items",
			{
				"item_code": "ML-G-18KT-75.4-P",
				"qty": 30.0,
				"uom": "Gram",
				"source_type": SOURCE_TYPE_SCRAP,
			},
		)
		re.append(
			"material_items",
			{
				"item_code": "DL-NT-RO-4-+00-0",
				"qty": 2.5,
				"uom": "Carat",
				"source_type": SOURCE_TYPE_SCRAP,
			},
		)
		groups = re._external_po_groups(build_refinery_price_index(re.refining_type))
		self.assertEqual(len(groups), 1, "the returned diamond row must not be billed")
		self.assertEqual(groups[0]["qty"], 30.0)
		self.assertEqual(groups[0]["uom"], "Gram")

	def test_external_po_uses_the_covered_items_mapping(self):
		"""A covered-items TEMPLATE row beats the pricing_item fallback and collapses every
		variant of that template onto one line."""
		price_list = frappe.get_doc(
			{
				"doctype": "Refinery Price List",
				"item": "REF-CF-001",
				"refining_type": REFINING_TYPE_SCRAP,
			}
		)
		price_list.append(
			"slabs",
			{
				"from_weight": 0,
				"to_weight": 0,
				"charge_type": "Per Gram",
				"rate": 11,
				"weight_basis": "Gross Weight",
			},
		)
		price_list.append("covered_items", {"item_code": "ML"})
		price_list.insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc,
			"Refinery Price List",
			price_list.name,
			force=1,
			ignore_permissions=True,
			delete_permanently=True,
		)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.manufacturer = "Shubh"
		re.pricing_item = "REF-MD-001"
		for code, qty in (("ML-G-18KT-75.4-P", 30.0), ("ML-G-22KT-91.9-Y", 20.0)):
			re.append(
				"material_items",
				{
					"item_code": code,
					"qty": qty,
					"uom": "Gram",
					"source_type": SOURCE_TYPE_SCRAP,
				},
			)

		lines, unpriced = re._external_po_lines()
		self.assertEqual(unpriced, [])
		self.assertEqual(
			len(lines), 1, "both ML variants collapse onto the template's line"
		)
		self.assertEqual(lines[0]["custom_refining_price_list"], price_list.name)
		self.assertEqual(lines[0]["qty"], 50.0)
		self.assertEqual(lines[0]["rate"], 11)
		self.assertEqual(lines[0]["uom"], "Gram")
		# The line bills the matched price list's OWN item, not the generic REF-SVC-001
		# charge item every line used to carry.
		self.assertEqual(lines[0]["item_code"], "REF-CF-001")
		# REF-* categories are stock items, so the line needs a warehouse or the PO is
		# rejected with "Warehouse is mandatory for stock Item".
		self.assertTrue(lines[0].get("warehouse"))

	def test_preview_external_refining_po_matches_the_created_po(self):
		"""The dry-run harness must not drift from the real builder — it is what the change
		was verified with against production data."""
		gold_receipt("Waxing RM - T", "ML-G-18KT-75.4-P", 30.0)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.manufacturer = "Shubh"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		re.physical_quantity = 30.0
		re.append(
			"material_items",
			{
				"item_code": "ML-G-18KT-75.4-P",
				"warehouse": "Waxing RM - T",
				"qty": 30.0,
				"uom": "Gram",
				"source_type": SOURCE_TYPE_SCRAP,
			},
		)
		re.save()

		apply_workflow(re, "Submit")
		re.reload()

		preview = re.preview_external_refining_po()
		po = frappe.get_doc("Purchase Order", re.refining_entry_po)

		def key(row):
			return (
				row["item_code"] if isinstance(row, dict) else row.item_code,
				flt(row["qty"] if isinstance(row, dict) else row.qty, 3),
				row["uom"] if isinstance(row, dict) else row.uom,
				flt(row["rate"] if isinstance(row, dict) else row.rate, 3),
				row["custom_refining_price_list"]
				if isinstance(row, dict)
				else row.custom_refining_price_list,
				flt(
					row["custom_gross_wt"]
					if isinstance(row, dict)
					else row.custom_gross_wt,
					3,
				),
			)

		self.assertEqual(
			sorted(key(r) for r in preview["lines"]), sorted(key(r) for r in po.items)
		)

	def test_external_refinery_submit_fails_without_supplier(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Unused/Loose Material Refining"
		re.is_external = 1
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		re.append(
			"material_items",
			{
				"item_code": "ML-G-18KT-75.4-P",
				"warehouse": "Waxing RM - T",
				"qty": 1.0,
				"source_type": "Unused/Loose Material",
			},
		)
		re.insert(ignore_mandatory=True)
		self.assertRaises(frappe.ValidationError, re.before_submit)

	def test_validate_configuration(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Scrap Refining"
		re.multiple_department = 1
		re.multiple_operation = 1
		self.assertRaises(frappe.ValidationError, re.validate)

	def test_validate_physical_quantity_zero(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Scrap Refining"
		re.physical_quantity = 0
		self.assertRaises(frappe.ValidationError, re.validate)

	def test_validate_physical_quantity_negative(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Scrap Refining"
		re.physical_quantity = -5.0
		self.assertRaises(frappe.ValidationError, re.validate)

	def test_submit_fails_without_materials(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Unused/Loose Material Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.insert(ignore_mandatory=True)
		self.assertRaises(frappe.ValidationError, re.before_submit)

	def test_submit_fails_missing_refining_warehouse(self):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Unused/Loose Material Refining"
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
		re.refining_type = "Scrap Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.from_date = today()
		re.to_date = today()
		# Positive weigh-in so the first save clears validate_quantities; the real figure is
		# derived below, once the auto-fetch has populated System Quantity.
		re.physical_quantity = PLACEHOLDER_PHYSICAL_QTY
		re.save()

		# "Waxing Scrap - T" is shared by the whole suite and every loss_entry() receipts
		# another 4.38 g into it, so the fetched system quantity depends on how many tests
		# ran before this one — i.e. on alphabetical method order. Derive the weigh-in from
		# what was actually fetched, keeping the 0.89 g of physical-over-system excess these
		# tests exercise, instead of a fixed 5.27 total that only held while this test ran
		# first and that silently goes NEGATIVE once enough scrap has accumulated.
		re.physical_quantity = flt(re.system_quantity) + 0.89

		re.append(
			"material_items",
			{
				"item_code": "Cap",
				"item_group": "Tools & Accessories",
				"qty": re.physical_quantity - re.system_quantity,
				"source_type": "Scrap",
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

	# --- Work Order source resolution (RFN-MWO-26-00114) ---
	#
	# Work Order Refining takes its required qty from the MOP Log running balance, a
	# VIRTUAL ledger, and then allocates it from PHYSICAL batch stock. When several
	# batches of one item were summed into a single batch-less line pointed at one
	# reservation warehouse, the transfer demanded more than that warehouse held and
	# aborted the submit ("Required: 0.629, Missing: 0.005"). These cover the pieces
	# that keep the two ledgers reconciled.

	def test_mwo_source_warehouse_follows_physical_batch_stock(self):
		"""A batch whose reservation warehouse is empty is sourced from the warehouse
		that physically holds it, not from the (stale) reservation."""
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Work Order Refining"
		re.company = "Test_Company"
		re.warehouse = "MPM WO - T"

		stock = {("Hammer WIP - T", "BATCH-A"): 0.629}

		def fake_physical_qty(item_code, batch_no, warehouse):
			return stock.get((warehouse, batch_no), 0.0)

		sre_map = {
			# The reservation still names the stale warehouse.
			("MWO-1", "D-NT-RO-6B-+8-8.5", "BATCH-A"): ["Stale WIP - T"],
			("MWO-1", "D-NT-RO-6B-+8-8.5", None): ["Stale WIP - T", "Hammer WIP - T"],
		}

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._eod_physical_batch_qty",
			side_effect=fake_physical_qty,
		):
			resolved = re._resolve_mwo_source_warehouse(
				"MWO-1",
				"D-NT-RO-6B-+8-8.5",
				"BATCH-A",
				0.629,
				sre_map,
				"MPM WO - T",
				"MPM WO - T",
			)

		self.assertEqual(resolved, "Hammer WIP - T")

	def test_mwo_source_warehouse_falls_back_when_nothing_covers(self):
		"""No warehouse covers the qty -> keep the reservation warehouse, so the
		shortfall is reported against the warehouse the reservation actually names."""
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Work Order Refining"
		re.company = "Test_Company"
		re.warehouse = "MPM WO - T"

		sre_map = {("MWO-1", "D-NT-RO-6B-+8-8.5", "BATCH-A"): ["Hammer WIP - T"]}

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._eod_physical_batch_qty",
			return_value=0.624,
		):
			resolved = re._resolve_mwo_source_warehouse(
				"MWO-1",
				"D-NT-RO-6B-+8-8.5",
				"BATCH-A",
				0.629,
				sre_map,
				None,
				None,
			)

		self.assertEqual(resolved, "Hammer WIP - T")

	def test_sales_order_sre_narrowing(self):
		"""Sales-Order-matched reservations are admitted only for material this entry
		actually moves — a Sales Order is shared across many MWOs."""
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Work Order Refining"
		wanted_items = {"D-NT-RO-6B-+8-8.5"}
		wanted_batches = {("D-NT-RO-6B-+8-8.5", "BATCH-A")}

		qty_based = frappe._dict(
			item_code="D-NT-RO-6B-+8-8.5", has_batch_no=1, reservation_based_on="Qty"
		)
		batch_based = frappe._dict(
			item_code="D-NT-RO-6B-+8-8.5",
			has_batch_no=1,
			reservation_based_on="Serial and Batch",
		)
		other_item = frappe._dict(
			item_code="M-G-22KT-91.9-Y",
			has_batch_no=1,
			reservation_based_on="Serial and Batch",
		)

		# Qty-based reservation: only the item has to match.
		self.assertTrue(
			re._sre_matches_keys(qty_based, wanted_items, wanted_batches, [])
		)
		# Batch-based: must name a batch we are moving.
		self.assertTrue(
			re._sre_matches_keys(batch_based, wanted_items, wanted_batches, ["BATCH-A"])
		)
		self.assertFalse(
			re._sre_matches_keys(batch_based, wanted_items, wanted_batches, ["BATCH-Z"])
		)
		# A different item is never in scope.
		self.assertFalse(
			re._sre_matches_keys(other_item, wanted_items, wanted_batches, ["BATCH-A"])
		)

	def test_shortfall_message_is_diagnostic(self):
		"""The submit-time throw must name the batch, the numbers and where the stock
		actually is — it surfaces from a background Submission Queue job, where a bare
		item/warehouse message left the operator with nothing to act on."""
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Work Order Refining"
		re.company = "Test_Company"

		msg = re._batch_shortfall_message(
			item_code="D-NT-RO-6B-+8-8.5",
			warehouse="Hammer WIP - T",
			required_qty=0.629,
			shortfall=0.005,
			allocated_qty=0.624,
			batches=[{"batch_no": "BATCH-A", "qty": 0.624}],
			bin_qty=0.624,
			diagnostics={
				"batch_no": "BATCH-A",
				"manufacturing_work_order": "MWO-1",
				"company": "Test_Company",
			},
		)

		self.assertIn("D-NT-RO-6B-+8-8.5", msg)
		self.assertIn("Hammer WIP - T", msg)
		self.assertIn("BATCH-A", msg)
		self.assertIn("0.629", msg)
		self.assertIn("0.005", msg)
		self.assertIn("MWO-1", msg)

	def test_mwo_sres_survive_an_allocation_failure(self):
		"""Reservations are released only once every transfer row is resolved. Cancelling
		first meant an allocation throw left the stock unreserved with no transfer to
		show for it — the path RFN-MWO-26-00114 took through the Submission Queue."""
		mwo = mwo_semi_finished_goods(self)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Work Order Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.scan_mwo_action(mwo.name)
		re.save()

		# One unsatisfiable batched row is enough: the real FIFO allocation runs, finds
		# nothing, and throws from inside the row loop.
		re.set(
			"material_items",
			[
				{
					"item_code": "D-NT-RO-6B-+9-9.5",
					"qty": 9999.0,
					"warehouse": "Refining RM - T",
					"source_type": "MWO",
					"manufacturing_work_order": mwo.name,
				}
			],
		)

		with patch.object(type(re), "_cancel_source_mwo_sres", autospec=True) as cancel:
			self.assertRaises(frappe.ValidationError, re.create_material_transfer_se)

		cancel.assert_not_called()

	def test_mwo_material_rows_are_not_merged_across_batches(self):
		"""One material row per MOP balance row. Merging them into a batch-less line
		summed quantities that can live in DIFFERENT warehouses, and the transfer then
		re-FIFO'd the total against whichever single warehouse the merged line got."""
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			get_current_mop_balance_rows,
		)

		mwo = mwo_semi_finished_goods(self)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Work Order Refining"
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.scan_mwo_action(mwo.name)
		re.save()

		# Same MOP selection build_material_table uses.
		last_mop = (
			frappe.db.get_value(
				"Manufacturing Operation",
				{"manufacturing_work_order": mwo.name, "status": "Not Started"},
				"name",
				order_by="creation desc",
			)
			or re.mwo_details[0].manufacturing_operation
		)
		balance_rows = [
			row
			for row in get_current_mop_balance_rows(
				last_mop,
				include_fields=[
					"item_code",
					"qty_after_transaction_batch_based as qty",
					"batch_no",
				],
			)
			if flt(row.get("qty"), 3) > 0
		]

		self.assertTrue(balance_rows, "fixture produced no MOP balance")
		self.assertEqual(len(re.material_items), len(balance_rows))
		self.assertEqual(
			{(row.item_code, row.batch_no) for row in re.material_items},
			{(row.get("item_code"), row.get("batch_no")) for row in balance_rows},
		)

	# --- External refining: batch reuse, no loss output, no design code ---

	def _external_unused_material_entry(self, qty=5.0):
		"""Submitted external Scrap entry, ready to receive from the supplier."""
		gold_receipt("Waxing RM - T", "ML-G-18KT-75.4-P", qty)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Unused/Loose Material Refining"
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.department = "Waxing - T"
		re.warehouse = "Waxing RM - T"
		re.refining_department = "Refinery - T"
		re.append(
			"material_items",
			{
				"item_code": "ML-G-18KT-75.4-P",
				"warehouse": "Waxing RM - T",
				"qty": qty,
				"source_type": "Unused/Loose Material",
			},
		)
		re.save()
		apply_workflow(re, "Submit")
		re.reload()
		return re

	def test_recovered_metal_reuses_existing_batch(self):
		"""The refined 24KT belongs in a batch the item already has — one the batch
		selector shows — not in a fresh per-receipt batch."""
		re = self._external_unused_material_entry()

		# Seed an existing batch of the recovered metal item in the receiving warehouse,
		# so there is a candidate even when this test runs on its own.
		gold_receipt("Waxing RM - T", re.refined_metal_item, 1.0)
		existing = set(
			frappe.get_all(
				"Batch", filters={"item": re.refined_metal_item}, pluck="name"
			)
		)
		self.assertTrue(existing, "fixture produced no batch for the recovered metal")

		re.receive_from_supplier(recovery_weight=3.6)
		re.reload()

		repack = frappe.get_doc("Stock Entry", re.repack_se)
		metal_row = next(
			row for row in repack.items if row.item_code == re.refined_metal_item
		)
		# WHICH existing batch wins is FIFO's call (get_batch_qty returns the site's
		# configured pick order), and earlier tests in this suite have already received
		# 24KT into this same warehouse — so pin the requirement itself: the row lands in
		# a batch that already existed, and no new batch is minted for the item.
		self.assertIn(metal_row.batch_no, existing)
		self.assertEqual(
			set(
				frappe.get_all(
					"Batch", filters={"item": re.refined_metal_item}, pluck="name"
				)
			),
			existing,
		)

	def test_recovered_metal_skips_a_customer_owned_batch(self):
		"""Company recovery must never land in a customer's batch — ownership never
		crosses, so a fresh batch is minted instead."""
		re = self._external_unused_material_entry()

		gold_receipt("Waxing RM - T", re.refined_metal_item, 1.0)
		seeded = frappe.db.get_value(
			"Stock Entry Detail",
			{"item_code": re.refined_metal_item, "t_warehouse": "Waxing RM - T"},
			"batch_no",
			order_by="creation desc",
		)
		self.assertTrue(seeded)
		frappe.db.set_value(
			"Batch",
			seeded,
			{
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "Test_Customer",
			},
		)

		re.receive_from_supplier(recovery_weight=3.6)
		re.reload()

		repack = frappe.get_doc("Stock Entry", re.repack_se)
		metal_row = next(
			row for row in repack.items if row.item_code == re.refined_metal_item
		)
		self.assertNotEqual(metal_row.batch_no, seeded)

	def test_external_receive_books_no_loss_item(self):
		"""Send 22KT-equivalent scrap, get 24KT back — and nothing else. The loss is a
		number on the Recovery Summary, never a stock-increasing row."""
		re = self._external_unused_material_entry()
		re.receive_from_supplier(recovery_weight=3.6)
		re.reload()

		self.assertGreater(re.refining_loss, 0)

		repack = frappe.get_doc("Stock Entry", re.repack_se)
		received = [
			row for row in repack.items if row.t_warehouse and not row.s_warehouse
		]
		self.assertTrue(received)
		for row in received:
			self.assertFalse(
				row.item_code.upper().startswith(("ML-", "FL-")),
				f"{row.item_code} is a loss item received into stock",
			)

	def test_loss_guard_survives_a_site_without_the_pure_loss_item(self):
		"""Regression: resolving the loss set through get_dust_item() would, on a site
		with no ML-G-24KT-99.9-Y, fall back to the recovered PURE GOLD item — making the
		guard throw on the legitimate metal row and block every external receive."""
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Unused/Loose Material Refining"
		re.is_external = 1
		re.company = "Test_Company"
		re.refined_metal_item = "M-G-24KT-99.9-Y"

		se = frappe.new_doc("Stock Entry")
		se.append(
			"items",
			{
				"item_code": "M-G-24KT-99.9-Y",
				"qty": 3.6,
				"t_warehouse": "Waxing RM - T",
			},
		)

		with patch.object(type(re), "get_dust_item", return_value="M-G-24KT-99.9-Y"):
			# Must not raise: the recovered metal is never an offender.
			re._assert_no_loss_output(se)

	def test_external_serial_material_table_excludes_the_design_code(self):
		"""The design code is a PIECE COUNT, not refinable grams. External serial
		refining keeps it out of Material Items entirely — including the self-referential
		BOM row that produced the duplicate."""
		snc = create_snc(self)
		snc.submit()
		sn = frappe.get_doc("Serial No", snc.fg_serial_no)

		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Serial Number Refining"
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.department = "Tagging - T"
		re.warehouse = "Tagging FG - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.scan_serial_no_action(sn.name)

		self.assertTrue(re.material_items, "no BOM components were fetched")
		for row in re.material_items:
			self.assertNotEqual(
				row.item_code,
				sn.item_code,
				"the design code is still in the Material Items table",
			)
			self.assertEqual(row.source_type, "BOM Component")

		# The serial still has to reach the refiner — its movement now comes from
		# serial_no_details instead of the material table.
		movement = re._serial_movement_rows()
		self.assertEqual(len(movement), 1)
		self.assertEqual(movement[0].serial_no, sn.name)
		self.assertEqual(movement[0].item_code, sn.item_code)

	def test_serial_movement_rows_skip_serials_already_in_the_table(self):
		"""An entry submitted BEFORE this change still carries its FG row. Synthesising
		a movement row for it too would transfer/consume the piece twice."""
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Serial Number Refining"
		re.is_external = 1
		re.company = "Test_Company"
		re.warehouse = "Tagging FG - T"
		re.append(
			"serial_no_details",
			{"serial_number": "SN-LEGACY-1", "item_code": "Test Design", "pcs": 1},
		)

		self.assertEqual(len(re._serial_movement_rows()), 1)

		re.append(
			"material_items",
			{
				"item_code": "Test Design",
				"qty": 1,
				"serial_no": "SN-LEGACY-1",
				"source_type": "Serial Number",
			},
		)
		self.assertEqual(re._serial_movement_rows(), [])

	def test_internal_serial_refining_keeps_the_design_code(self):
		"""Scope guard: only external refining changed."""
		re = frappe.new_doc("Refining Entry")
		re.refining_type = "Serial Number Refining"
		re.company = "Test_Company"
		re.warehouse = "Tagging FG - T"
		re.append(
			"serial_no_details",
			{"serial_number": "SN-LEGACY-1", "item_code": "Test Design", "pcs": 1},
		)
		self.assertEqual(re._serial_movement_rows(), [])

	# --- Renamed vocabulary: every copy of the option list must agree with the DocField,
	# and no stale Property Setter may shadow it. ---

	def test_refining_type_options_are_the_renamed_set(self):
		self.assertEqual(
			frappe.get_meta("Refining Entry").get_field("refining_type").options,
			REFINING_TYPE_OPTIONS,
		)

	def test_source_type_options_are_the_renamed_set(self):
		self.assertEqual(
			frappe.get_meta("Refining Material Line").get_field("source_type").options,
			"\n".join(
				[
					"",
					"MWO",
					"Serial Number",
					SOURCE_TYPE_SCRAP,
					SOURCE_TYPE_UNUSED,
					"Loss Item",
					"Consumable",
					"BOM Component",
				]
			),
		)

	def test_batch_type_options_are_the_renamed_set(self):
		"""Also the regression guard on the external git_action_v16 fixture branch, which
		still ships the pre-rename "\\nScrap" options for this Custom Field."""
		self.assertEqual(
			frappe.get_meta("Batch").get_field("custom_batch_type").options,
			"\n" + BATCH_TYPE_UNUSED,
		)

	def test_no_stale_select_options_property_setter(self):
		for doctype, fieldname in RENAMED_SELECTS:
			self.assertEqual(
				frappe.db.count(
					"Property Setter",
					{
						"doc_type": doctype,
						"field_name": fieldname,
						"property": ["in", ["options", "default"]],
					},
				),
				0,
				f"a Property Setter is shadowing {doctype}.{fieldname}",
			)

	def test_report_filter_options_match_the_doctype(self):
		"""The two query reports duplicate the refining_type option list in JS, where it
		cannot be derived. A drifted copy silently returns zero rows, so pin it here."""
		canonical = frappe.get_meta("Refining Entry").get_field("refining_type").options
		for report in ("weekly_recovery_efficiency", "daily_refining_recovery_report"):
			path = frappe.get_app_path(
				"jewellery_erpnext", "refining", "report", report, f"{report}.js"
			)
			with open(path) as fh:
				source = fh.read()
			found = re.findall(r'options:\s*"((?:[^"\\]|\\.)*)"', source)
			self.assertTrue(found, f"{report}.js declares no options string")
			self.assertIn(
				canonical,
				[opts.encode().decode("unicode_escape") for opts in found],
				f"{report}.js refining_type options have drifted from the DocField",
			)

	def test_terminology_data_patch_swaps_without_collapsing(self):
		"""The swap is the risky part: Dust->Scrap while Scrap->Unused/Loose Material, in
		one statement. Plant the PRE-rename state with raw SQL (save() can no longer accept
		the old literals), run the patch, and assert a 1:1 mapping — then run it again and
		assert the fence held."""
		scrap = frappe.get_doc(
			{
				"doctype": "Refining Entry",
				"company": "Test_Company",
				"refining_type": REFINING_TYPE_SCRAP,
				"posting_date": today(),
				"department": "Waxing - T",
				# ignore_mandatory skips reqd fields, not validate() — and Scrap Refining
				# still has to clear the Physical Quantity guard.
				"physical_quantity": PLACEHOLDER_PHYSICAL_QTY,
			}
		).insert(ignore_mandatory=True)
		unused = frappe.get_doc(
			{
				"doctype": "Refining Entry",
				"company": "Test_Company",
				"refining_type": REFINING_TYPE_UNUSED,
				"posting_date": today(),
				"department": "Waxing - T",
			}
		).insert(ignore_mandatory=True)

		frappe.db.set_value(
			"Refining Entry",
			scrap.name,
			"refining_type",
			"Dust Refining",
			update_modified=False,
		)
		frappe.db.set_value(
			"Refining Entry",
			unused.name,
			"refining_type",
			"Scrap Refining",
			update_modified=False,
		)
		clear_swap_sentinel()
		self.addCleanup(set_swap_sentinel)

		# Driven through _apply_swap, not execute(): this site has already migrated, so
		# post-rename values exist all over the table (every other test makes them) and
		# execute()'s fail-closed pre-flight would — correctly — refuse. Both of its fences
		# are asserted below instead.
		apply_terminology_swap(frappe.db.has_column("Batch", "custom_batch_type"))

		self.assertEqual(
			frappe.db.get_value("Refining Entry", scrap.name, "refining_type"),
			REFINING_TYPE_SCRAP,
		)
		self.assertEqual(
			frappe.db.get_value("Refining Entry", unused.name, "refining_type"),
			REFINING_TYPE_UNUSED,
		)
		self.assertNotEqual(scrap.name, unused.name)

		# Fence 1 — pre-flight: the sentinel is still clear, but the rows above are now
		# post-rename, so execute() must refuse rather than swap them a second time.
		swap_terminology()
		self.assertEqual(
			frappe.db.get_value("Refining Entry", scrap.name, "refining_type"),
			REFINING_TYPE_SCRAP,
		)
		self.assertEqual(
			frappe.db.get_value("Refining Entry", unused.name, "refining_type"),
			REFINING_TYPE_UNUSED,
		)

		# Fence 2 — sentinel: refusing above also SETS it, so the next run short-circuits
		# before the pre-flight even runs.
		swap_terminology()
		self.assertEqual(
			frappe.db.get_value("Refining Entry", scrap.name, "refining_type"),
			REFINING_TYPE_SCRAP,
		)
		self.assertEqual(
			frappe.db.get_value("Refining Entry", unused.name, "refining_type"),
			REFINING_TYPE_UNUSED,
		)

	# --- Variant restrictions (Manufacturing Setting -> Refining Variant Restrictions) ---

	def test_restriction_excludes_variant_from_autofetch(self):
		"""Auto-fetched rows are DROPPED with a notice, not thrown on: the operator never
		chose them. System Quantity must drop in lockstep, or difference_quantity is wrong
		and validate_dust_opening_material throws on a difference they cannot fill."""
		add_variant_restriction(self, "ML", REFINING_TYPE_SCRAP)
		loss_entry()

		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.from_date = today()
		re.to_date = today()
		re.physical_quantity = PLACEHOLDER_PHYSICAL_QTY
		re.save()

		self.assertEqual(
			[r.item_code for r in re.material_items if r.item_code.startswith("ML-")],
			[],
		)
		self.assertEqual(
			flt(re.system_quantity, 3),
			flt(sum(flt(r.qty) for r in re.material_items), 3),
			"System Quantity must equal the sum of the surviving rows",
		)

	def test_restriction_scoped_to_its_refining_type(self):
		"""A restriction on another type must not touch this one."""
		add_variant_restriction(self, "ML", REFINING_TYPE_UNUSED)
		loss_entry()

		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.from_date = today()
		re.to_date = today()
		re.physical_quantity = PLACEHOLDER_PHYSICAL_QTY
		re.save()

		self.assertTrue(
			[r for r in re.material_items if (r.item_code or "").startswith("ML-")],
			"ML rows must still be fetched when the restriction names a different type",
		)

	def test_blank_refining_type_blocks_every_type(self):
		add_variant_restriction(self, "ML", None)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		self.assertEqual(re._restricted_variants(), frozenset({"ML"}))

		re.refining_type = REFINING_TYPE_UNUSED
		re.__dict__.pop("_refining_restriction_cache", None)
		self.assertEqual(re._restricted_variants(), frozenset({"ML"}))

	def test_manual_material_row_of_restricted_variant_throws(self):
		"""Hand-added rows DO throw — the operator chose them, so silence would hide it."""
		add_variant_restriction(self, "ML", REFINING_TYPE_SCRAP)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.from_date = today()
		re.to_date = today()
		re.append(
			"material_items",
			{
				"item_code": "ML-G-22KT-91.9-Y",
				"qty": 1.0,
				"source_type": SOURCE_TYPE_SCRAP,
			},
		)
		self.assertRaises(frappe.ValidationError, re.save)

	def test_restriction_matches_the_template_item_code_directly(self):
		"""Second match arm: the item IS the restricted template, not a variant of it."""
		add_variant_restriction(self, "ML", REFINING_TYPE_SCRAP)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.company = "Test_Company"
		re.manufacturer = "Shubh"
		self.assertEqual(
			re._blocked_item_codes(["ML", "ML-G-22KT-91.9-Y"]),
			{"ML", "ML-G-22KT-91.9-Y"},
		)

	def test_pure_loss_item_is_exempt_from_restriction(self):
		"""Restricting ML must not make the mandatory physical-difference row unenterable."""
		add_variant_restriction(self, "ML", REFINING_TYPE_SCRAP)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.company = "Test_Company"
		re.manufacturer = "Shubh"
		self.assertIn(PURE_LOSS_ITEM, re._restriction_exempt_items())

	def test_restriction_fails_open_without_a_manufacturing_setting(self):
		"""THE critical property: no resolvable setting means no restrictions and refining
		behaves exactly as it did before the feature existed."""
		add_variant_restriction(self, "ML", REFINING_TYPE_SCRAP)
		loss_entry()

		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.from_date = today()
		re.to_date = today()
		re.physical_quantity = PLACEHOLDER_PHYSICAL_QTY
		with patch(
			"jewellery_erpnext.refining.doctype.refining_entry.refining_entry.resolve_manufacturing_setting",
			return_value=None,
		):
			re.save()
			self.assertEqual(re._restricted_variants(), frozenset())
		self.assertTrue(
			[r for r in re.material_items if (r.item_code or "").startswith("ML-")],
			"fail-open must fetch the ML rows exactly as an unrestricted site does",
		)

	def test_restriction_applies_to_external_refining_too(self):
		add_variant_restriction(self, "ML", REFINING_TYPE_SCRAP)
		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.is_external = 1
		re.company = "Test_Company"
		re.department = "Waxing - T"
		re.manufacturer = "Shubh"
		re.append(
			"material_items",
			{
				"item_code": "ML-G-22KT-91.9-Y",
				"qty": 1.0,
				"source_type": SOURCE_TYPE_SCRAP,
			},
		)
		self.assertRaises(frappe.ValidationError, re.save)

	def test_manufacturing_setting_rejects_unknown_refining_type(self):
		"""Guards against a stale type surviving a future rename via bulk edit / the API."""
		ms = frappe.get_doc("Manufacturing Setting", "Shubh")
		ms.append(
			"refining_variant_restrictions",
			{"variant": "ML", "refining_type": "Dust Refining"},
		)
		self.assertRaises(frappe.ValidationError, ms.save)

	def tearDown(self):
		return super().tearDown()


def add_variant_restriction(test, variant, refining_type=None, manufacturer="Shubh"):
	"""Append a (variant, refining_type) restriction to a Manufacturing Setting, and clear
	the table again on teardown — a leaked row would poison every other test in the class,
	several of which refine ML-* material."""
	ms = frappe.get_doc("Manufacturing Setting", manufacturer)
	ms.append(
		"refining_variant_restrictions",
		{"variant": variant, "refining_type": refining_type},
	)
	ms.save()

	def _clear():
		doc = frappe.get_doc("Manufacturing Setting", manufacturer)
		doc.set("refining_variant_restrictions", [])
		doc.save()

	test.addCleanup(_clear)


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


def gold_receipt(warehouse, item_code, qty):
	se = frappe.new_doc("Stock Entry")
	se.company = "Test_Company"
	se.manufacturer = "Shubh"
	se.branch = frappe.db.exists("Branch", {"branch_name": "Test Branch"})
	se.stock_entry_type = "Material Receipt"
	se.append("items", {"t_warehouse": warehouse, "item_code": item_code, "qty": qty})
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


class TestRefiningNamingSeries(IntegrationTestCase):
	"""The series letters must track the current type names.

	They used to trail the Dust->Scrap / Scrap->Unused-Loose rename, so the actual scrap
	type minted RFN-DST- ("dust") while SCP- ("scrap") went to unused/loose material.
	"""

	@classmethod
	def setUpClass(cls):
		return

	def _series_for(self, refining_type):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = refining_type
		re.set_naming_series()
		return re.naming_series

	def test_scrap_refining_mints_scp(self):
		self.assertEqual(self._series_for(REFINING_TYPE_SCRAP), "RFN-SCP-.YY.-.#####")

	def test_unused_loose_material_mints_ulm(self):
		self.assertEqual(self._series_for(REFINING_TYPE_UNUSED), "RFN-ULM-.YY.-.#####")

	def test_work_order_and_serial_are_unchanged(self):
		self.assertEqual(
			self._series_for(REFINING_TYPE_WORK_ORDER), "RFN-MWO-.YY.-.#####"
		)
		self.assertEqual(self._series_for(REFINING_TYPE_SERIAL), "RFN-SRN-.YY.-.#####")

	def test_every_minted_series_is_a_valid_option(self):
		options = frappe.get_meta("Refining Entry").get_field("naming_series").options
		allowed = set(options.split("\n"))
		for refining_type in REFINING_TYPES:
			self.assertIn(self._series_for(refining_type), allowed)

	def test_options_are_exactly_the_minted_set(self):
		# The option list and set_naming_series must not drift apart: an option nothing
		# mints is dead, and a minted series that is not an option fails Select validation
		# (which is how RFN-ULM- first broke, shadowed by a stale Property Setter).
		options = frappe.get_meta("Refining Entry").get_field("naming_series").options
		self.assertEqual(
			set(options.split("\n")),
			{self._series_for(t) for t in REFINING_TYPES},
		)

	def test_pre_rename_documents_still_save(self):
		# 112 documents are stored on the retired RFN-DST- series. The option is gone from
		# the dropdown, so guard that reading and re-saving one is still possible --
		# naming_series is read-only, so the value is never re-validated on the way in.
		name = frappe.db.get_value(
			"Refining Entry",
			{"naming_series": "RFN-DST-.YY.-.#####", "docstatus": 0},
			"name",
		)
		if not name:
			self.skipTest("no pre-rename RFN-DST- draft on this site")
		frappe.get_doc("Refining Entry", name).save(ignore_permissions=True)

	def test_blank_type_does_not_default_to_a_type_series(self):
		# set_naming_series is a no-op for a blank type, so Frappe falls back to the FIRST
		# option -- which must not be one that reads as a specific refining type.
		options = frappe.get_meta("Refining Entry").get_field("naming_series").options
		self.assertEqual(options.split("\n")[0], "RFN-MWO-.YY.-.#####")


class TestReceiveFromSupplierSignature(IntegrationTestCase):
	"""``Received Quantity (if applicable)`` was write-only and is gone."""

	@classmethod
	def setUpClass(cls):
		return

	def test_received_qty_is_not_a_parameter(self):
		import inspect

		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			RefiningEntry,
		)

		params = inspect.signature(RefiningEntry.receive_from_supplier).parameters
		self.assertNotIn("received_qty", params)
		self.assertIn("recovery_weight", params)

	def test_dialog_no_longer_asks_for_it(self):
		import pathlib

		js = pathlib.Path(frappe.get_app_path("jewellery_erpnext")) / (
			"refining/doctype/refining_entry/refining_entry.js"
		)
		self.assertNotIn("received_qty", js.read_text())


class TestExternalPoBillableItem(IntegrationTestCase):
	"""The PO line bills the matched Refinery Price List's own item.

	Every line used to go out on the generic REF-SVC-001 charge item, so a Purchase Order
	never said what was actually being refined. No test pinned item_code, which is why it
	went unnoticed.
	"""

	@classmethod
	def setUpClass(cls):
		return

	def _entry(self, pricing_item="REF-MD-001", item_code="ML-G-22KT-91.9-Y"):
		re = frappe.new_doc("Refining Entry")
		re.refining_type = REFINING_TYPE_SCRAP
		re.is_external = 1
		re.company = "Test_Company"
		re.supplier = "Test_Supplier"
		re.manufacturer = "Shubh"
		re.pricing_item = pricing_item
		re.supplier_warehouse = frappe.db.get_value(
			"Warehouse",
			{"company": "Test_Company", "is_group": 0, "disabled": 0},
			"name",
		)
		re.append(
			"material_items",
			{
				"item_code": item_code,
				"qty": 20.0,
				"uom": "Gram",
				"source_type": SOURCE_TYPE_SCRAP,
			},
		)
		return re

	def test_line_uses_the_price_lists_item(self):
		price_list = frappe.get_doc(
			{
				"doctype": "Refinery Price List",
				"item": "REF-RMS-001",
				"refining_type": REFINING_TYPE_SCRAP,
			}
		)
		price_list.append(
			"slabs",
			{
				"from_weight": 0,
				"to_weight": 0,
				"charge_type": "Flat Charge",
				"rate": 750,
				"weight_basis": "Gross Weight",
			},
		)
		price_list.append("covered_items", {"item_code": "ML-G-22KT-91.9-Y"})
		price_list.insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc,
			"Refinery Price List",
			price_list.name,
			force=1,
			ignore_permissions=True,
			delete_permanently=True,
		)

		lines, _unpriced = self._entry()._external_po_lines()
		self.assertEqual(len(lines), 1)
		self.assertEqual(lines[0]["item_code"], "REF-RMS-001")
		self.assertEqual(lines[0]["rate"], 750)

	def test_stock_item_line_carries_a_warehouse(self):
		# REF-* categories are stock items; ERPNext rejects a stock PO line without one.
		lines, _unpriced = self._entry()._external_po_lines()
		self.assertTrue(
			frappe.db.get_value("Item", lines[0]["item_code"], "is_stock_item"),
			"this test is only meaningful for a stock item",
		)
		self.assertTrue(lines[0].get("warehouse"))

	def test_uncovered_material_still_falls_back_to_the_pricing_item(self):
		# Nothing covers ML-G-18KT-75.4-P directly, so the group falls back to the entry's
		# own pricing category (REF-MD-001) -- and bills under THAT, not the generic charge
		# item. The fallback category has its own price list, so the line is priced, not
		# unpriced; the uncovered-and-unpriceable case is
		# test_external_refining_po_unpriced_category_line.
		lines, unpriced = self._entry(item_code="ML-G-18KT-75.4-P")._external_po_lines()
		self.assertEqual(len(lines), 1)
		self.assertEqual(lines[0]["item_code"], "REF-MD-001")
		self.assertEqual(unpriced, [])
		self.assertTrue(lines[0]["custom_refining_price_list"])

	def test_billable_items_all_have_the_uoms_a_flat_charge_needs(self):
		# A Flat Charge bills 1 Nos, but the categories are seeded with only Gram/Litre.
		from jewellery_erpnext.patches.seed_refining_masters import (
			DUST_ITEMS,
			SERVICE_ITEM,
		)

		for item_code in [SERVICE_ITEM] + [code for code, *_ in DUST_ITEMS]:
			if not frappe.db.exists("Item", item_code):
				continue
			uoms = set(
				frappe.db.get_all(
					"UOM Conversion Detail",
					filters={"parent": item_code},
					pluck="uom",
				)
			)
			self.assertIn("Nos", uoms, f"{item_code} cannot bill a flat charge")
			self.assertIn("Gram", uoms, f"{item_code} cannot bill a per-gram charge")
