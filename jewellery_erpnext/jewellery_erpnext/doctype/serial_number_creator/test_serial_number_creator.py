# Copyright (c) 2024, Nirali and Contributors
# See license.txt

from decimal import ROUND_HALF_UP, Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	_derive_ownership_tag,
	_snc_se_detail_maps,
	_stone_se_rate,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
	mop_log_creation,
	scan_mwo_eir,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.test_manufacturing_work_order import (
	create_pmo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
	_active_sres_for,
	_allocate_pcs_across_rows,
	_allocate_qty_across_warehouses,
	_physical_batch_qty,
	_pick_source_warehouse,
	_reserved_warehouse_caps,
	_sre_reserves_batch,
	_warehouse_has_batch_stock,
	calulate_id_wise_sum_up,
	split_source_rows_by_reservation,
	validate_qty,
)

_SNC_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator"
_MOP_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation"


class TestSerialNumberCreator(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		self.doc = frappe.new_doc("Serial Number Creator")
		self.doc.type = "Manufacturing"
		self.doc.company = "Test_Company"
		self.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		return super().setUp()

	def test_serial_number_creator(self):
		snc = create_snc(self)
		snc.submit()
		se = frappe.get_doc("Stock Entry", {"custom_serial_number_creator": snc.name})
		self.assertEqual(snc.name, se.custom_serial_number_creator)
		self.assertEqual("Manufacture", se.stock_entry_type)
		for idx in range(len(snc.source_table)):
			src = snc.source_table[idx]
			sed = se.items[idx]
			self.assertEqual(src.s_warehouse, sed.s_warehouse)
			self.assertEqual(src.row_material, sed.item_code)
			self.assertEqual(src.qty, sed.qty)
			self.assertEqual(src.batch_no, sed.batch_no)

		sn = frappe.get_doc("Serial No", snc.fg_serial_no)
		self.assertEqual(sn.name, snc.fg_serial_no)
		self.assertEqual(sn.custom_bom_no, snc.fg_bom)
		self.assertEqual(sn.reference_name, se.name)

	def test_validate_qty_valid_quantity(self):
		self.doc.append(
			"fg_details", {"row_material": "ITEM-001", "qty": 5.5, "uom": "kg"}
		)

		try:
			validate_qty(self.doc)
		except frappe.exceptions.ValidationError:
			self.fail("validate_qty() raised ValidationError with valid quantity")

	def test_calculate_id_wise_sum_up_multiple_fg_rows(self):
		self.doc.append(
			"fg_details", {"row_material": "ITEM-001", "id": 1, "qty": 2.5, "uom": "kg"}
		)
		self.doc.append(
			"fg_details", {"row_material": "ITEM-001", "id": 2, "qty": 2.5, "uom": "kg"}
		)

		self.doc.append(
			"source_table", {"row_material": "ITEM-001", "qty": 5.0, "uom": "kg"}
		)

		try:
			calulate_id_wise_sum_up(self.doc)
		except frappe.exceptions.ValidationError:
			self.fail("calulate_id_wise_sum_up() raised error with correct sum")

	def test_calculate_id_wise_sum_up_mismatch(self):
		self.doc.append(
			"fg_details", {"row_material": "ITEM-001", "id": 1, "qty": 2.5, "uom": "kg"}
		)

		self.doc.append(
			"source_table", {"row_material": "ITEM-001", "qty": 3.5, "uom": "kg"}
		)

		with self.assertRaises(frappe.exceptions.ValidationError):
			calulate_id_wise_sum_up(self.doc)

	def test_calculate_id_wise_sum_decimal_precision(self):
		self.doc.append(
			"fg_details",
			{"row_material": "ITEM-001", "id": 1, "qty": 2.5555, "uom": "kg"},
		)

		rounded_qty = float(
			Decimal(str(2.5555)).quantize(Decimal("0.000"), rounding=ROUND_HALF_UP)
		)

		self.doc.append(
			"source_table",
			{"row_material": "ITEM-001", "qty": rounded_qty, "uom": "kg"},
		)

		try:
			calulate_id_wise_sum_up(self.doc)
		except frappe.exceptions.ValidationError:
			self.fail("calulate_id_wise_sum_up() raised error with rounded quantities")

	def test_get_bom_summary_with_fg_bom(self):
		self.doc.fg_bom = (
			"BOM-TEST-001" if frappe.db.exists("BOM", "BOM-TEST-001") else None
		)

		if self.doc.fg_bom:
			summary = self.doc.get_bom_summary()
			self.assertIsNotNone(summary)
			self.assertIn("item_code", summary or "")

	def test_get_bom_summary_with_design_id_bom(self):
		self.doc.fg_bom = None
		self.doc.design_id_bom = (
			"BOM-TEST-001" if frappe.db.exists("BOM", "BOM-TEST-001") else None
		)

		if self.doc.design_id_bom:
			summary = self.doc.get_bom_summary()
			self.assertIsNotNone(summary)

	def test_get_serial_summary_structure(self):
		try:
			if self.doc.fg_serial_no:
				result = self.doc.get_serial_summary()
				self.assertIsNotNone(result)
		except Exception:
			pass

	def test_fg_details_row_structure(self):
		self.doc.append(
			"fg_details",
			{
				"row_material": "ITEM-001",
				"id": 1,
				"batch_no": "BATCH-001",
				"qty": 5.5,
				"uom": "kg",
				"gross_wt": 5.5,
				"inventory_type": "Stock",
				"pcs": 1,
			},
		)

		row = self.doc.fg_details[0]
		self.assertEqual(row.row_material, "ITEM-001")
		self.assertEqual(row.id, 1)
		self.assertEqual(row.batch_no, "BATCH-001")
		self.assertEqual(row.qty, 5.5)
		self.assertEqual(row.uom, "kg")

	def test_source_table_row_structure(self):
		self.doc.append(
			"source_table",
			{"row_material": "ITEM-001", "qty": 10.0, "uom": "kg", "pcs": 2},
		)

		row = self.doc.source_table[0]
		self.assertEqual(row.row_material, "ITEM-001")
		self.assertEqual(row.qty, 10.0)
		self.assertEqual(row.uom, "kg")
		self.assertEqual(row.pcs, 2)

	def test_serial_no_creator_document_fields(self):
		self.doc.type = "Manufacturing"
		self.doc.manufacturing_work_order = "MWO-001"
		self.doc.parent_manufacturing_order = "PMO-001"
		self.doc.company = "Test Company"

		self.assertEqual(self.doc.type, "Manufacturing")
		self.assertEqual(self.doc.manufacturing_work_order, "MWO-001")
		self.assertEqual(self.doc.parent_manufacturing_order, "PMO-001")

	def test_empty_fg_details(self):
		self.assertEqual(len(self.doc.fg_details), 0)

		try:
			validate_qty(self.doc)
		except frappe.exceptions.ValidationError:
			self.fail("validate_qty() should not fail with empty fg_details")

	def test_decimal_quantity_handling(self):
		quantities = [0.5, 1.25, 2.333, 5.9999]

		for qty in quantities:
			test_doc = frappe.new_doc("Serial Number Creator")
			test_doc.append(
				"fg_details", {"row_material": f"ITEM-{qty}", "qty": qty, "uom": "kg"}
			)

			self.assertEqual(test_doc.fg_details[0].qty, qty)

	def tearDown(self):
		return super().tearDown()


def create_snc(self):
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

			mo_wax = frappe.get_last_doc("Manufacturing Operation")

			dir_for_receive(dir_issue)

			eir_issue = frappe.new_doc("Employee IR")
			eir_issue.company = "Test_Company"
			eir_issue.department = "Waxing - T"
			eir_issue.operation = "Wax Pull Out"
			eir_issue.employee = "HR-EMP-00002"
			eir_issue.scan_mwo = mo_wax.manufacturing_work_order
			scan_mwo_eir(eir_issue)
			eir_issue.save()
			eir_issue.submit()

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

			mo_wax1 = frappe.get_last_doc("Manufacturing Operation")

			dir_issue = dir_for_issue("Waxing - T", "Tagging - T", mo_wax1)
			mo_wax1.reload()

			mo_tag = frappe.get_last_doc("Manufacturing Operation")
			dir_for_receive(dir_issue)
			mo_tag.reload()

		elif row.department == "Serial Number - T":
			serial_no_mwo = row

	mwo_serial_no = frappe.get_doc("Manufacturing Work Order", serial_no_mwo.name)
	mwo_serial_no.submit()

	return frappe.get_last_doc("Serial Number Creator")


class TestWarehouseHasBatchStockGuard(IntegrationTestCase):
	"""Regression for BatchNegativeStockError on Serial Number Creator submit.

	When an item has several active Stock Reservation Entries in different
	warehouses (e.g. 0.005 in Model Making WO and 5.203 in Waxing WO), the SRE
	warehouse-capture loop must NOT adopt a warehouse where the batch has no
	stock — otherwise the auto-created Manufacture Stock Entry consumes the batch
	from the wrong warehouse and submit fails with BatchNegativeStockError.

	These tests exercise the guard in isolation (no fixtures required).
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_SNC_MODULE}.get_batch_qty")
	def test_skips_warehouse_without_batch_stock(self, mock_get_batch_qty):
		mock_get_batch_qty.return_value = 0.0
		self.assertFalse(
			_warehouse_has_batch_stock(
				"M-G-18KT-75.4-Y", "KG2F054-MGL18754Y0-F874H", "Model Making WO - KGJPL"
			)
		)
		# Must query *physical* stock (ignore_reserved_stock=True): the batch being
		# consumed is itself reserved, and the negative-stock validator checks
		# physical qty, so reserved stock must not be subtracted here.
		mock_get_batch_qty.assert_called_once_with(
			"KG2F054-MGL18754Y0-F874H",
			"Model Making WO - KGJPL",
			"M-G-18KT-75.4-Y",
			ignore_reserved_stock=True,
		)

	@patch(f"{_SNC_MODULE}.get_batch_qty")
	def test_keeps_warehouse_with_batch_stock(self, mock_get_batch_qty):
		mock_get_batch_qty.return_value = 5.203
		self.assertTrue(
			_warehouse_has_batch_stock(
				"M-G-18KT-75.4-Y", "KG2F054-MGL18754Y0-F874H", "Waxing WO - KGJPL"
			)
		)

	@patch(f"{_SNC_MODULE}.get_batch_qty")
	def test_no_batch_is_allowed_without_querying_stock(self, mock_get_batch_qty):
		# A row without a batch has nothing to validate; do not query stock.
		self.assertTrue(
			_warehouse_has_batch_stock("M-G-18KT-75.4-Y", None, "Waxing WO - KGJPL")
		)
		mock_get_batch_qty.assert_not_called()


class TestSREReservesBatch(IntegrationTestCase):
	"""Each SRE reserves a specific batch; PRIORITY 1 must only adopt/cancel the
	SRE that reserves the current row's batch. Without this, the first batch row
	swallows every SRE for the item and later batch rows fall back to a stale
	warehouse -> BatchNegativeStockError (the -0.005 KG2F061 case).
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_SNC_MODULE}.frappe.get_all")
	def test_matches_when_batch_in_sre_children(self, mock_get_all):
		mock_get_all.return_value = ["KG2F061-MGL18754Y0-24H0B"]
		self.assertTrue(
			_sre_reserves_batch("MAT-SRE-2026-79547", "KG2F061-MGL18754Y0-24H0B")
		)

	@patch(f"{_SNC_MODULE}.frappe.get_all")
	def test_does_not_match_other_batch(self, mock_get_all):
		# SRE 79554 reserves KG2F054, so it must NOT match the KG2F061 row.
		mock_get_all.return_value = ["KG2F054-MGL18754Y0-F874H"]
		self.assertFalse(
			_sre_reserves_batch("MAT-SRE-2026-79554", "KG2F061-MGL18754Y0-24H0B")
		)

	@patch(f"{_SNC_MODULE}.frappe.get_all")
	def test_qty_based_sre_matches_any_batch(self, mock_get_all):
		# Qty-based SRE has no Serial and Batch Entry children -> item-level match.
		mock_get_all.return_value = []
		self.assertTrue(_sre_reserves_batch("MAT-SRE-QTY", "KG2F061-MGL18754Y0-24H0B"))


class TestPhysicalBatchQty(IntegrationTestCase):
	"""``_physical_batch_qty`` must query PHYSICAL stock (ignore_reserved_stock=True),
	round to 3, and never crash on non-batch lines or backend errors.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_SNC_MODULE}.get_batch_qty")
	def test_queries_physical_stock_rounded(self, mock_get_batch_qty):
		mock_get_batch_qty.return_value = 0.7040004
		self.assertEqual(
			_physical_batch_qty(
				"D-NT-RO-6B-+7-7.5",
				"GE2D075-DNTROX6G00G05-16",
				"Diamond Setting WO - GEPL",
			),
			0.704,
		)
		mock_get_batch_qty.assert_called_once_with(
			"GE2D075-DNTROX6G00G05-16",
			"Diamond Setting WO - GEPL",
			"D-NT-RO-6B-+7-7.5",
			ignore_reserved_stock=True,
		)

	@patch(f"{_SNC_MODULE}.get_batch_qty")
	def test_non_batch_returns_zero_without_query(self, mock_get_batch_qty):
		self.assertEqual(_physical_batch_qty("D-NT-RO-6B-+7-7.5", None, "WH-A"), 0.0)
		mock_get_batch_qty.assert_not_called()

	@patch(f"{_SNC_MODULE}.get_batch_qty", side_effect=Exception("boom"))
	def test_backend_error_returns_zero(self, _mock):
		self.assertEqual(_physical_batch_qty("D-NT-RO-6B-+7-7.5", "B1", "WH-A"), 0.0)


class TestPickSourceWarehouse(IntegrationTestCase):
	"""``_pick_source_warehouse`` returns the first candidate that physically covers
	the full requested qty. This is the guard that stops Serial Number Creator from
	adopting a stale reservation warehouse holding only partial stock.

	Live bug reproduced by ``test_falls_through_when_first_warehouse_partial``: batch
	GE2D075-DNTROX6G00G05-16 had 0.704 in "Diamond Setting WO - GEPL" (a Delivered
	SRE) but the full 0.72 in "Tagging WO - GEPL" (the live reservation). Picking the
	0.704 warehouse drove the Manufacture entry to -0.016.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_empty_candidates_returns_none(self):
		self.assertIsNone(
			_pick_source_warehouse(
				"D-NT-RO-6B-+7-7.5", "GE2D075-DNTROX6G00G05-16", 0.72, []
			)
		)

	@patch(f"{_SNC_MODULE}._physical_batch_qty")
	def test_non_batch_returns_first_candidate(self, mock_phys):
		wh = _pick_source_warehouse("M-G-22KT-91.9-Y", None, 5.0, ["WH-A", "WH-B"])
		self.assertEqual(wh, "WH-A")
		mock_phys.assert_not_called()

	@patch(f"{_SNC_MODULE}._physical_batch_qty")
	def test_first_covering_candidate_chosen(self, mock_phys):
		# Original source_table warehouse covers the full qty -> healthy path, the
		# SRE warehouse is never even checked.
		mock_phys.return_value = 0.72
		wh = _pick_source_warehouse(
			"D-NT-RO-6B-+7-7.5",
			"GE2D075-DNTROX6G00G05-16",
			0.72,
			["Tagging WO - GEPL", "Diamond Setting WO - GEPL"],
		)
		self.assertEqual(wh, "Tagging WO - GEPL")
		mock_phys.assert_called_once()

	@patch(f"{_SNC_MODULE}._physical_batch_qty")
	def test_falls_through_when_first_warehouse_partial(self, mock_phys):
		physical = {
			"Diamond Setting WO - GEPL": 0.704,
			"Tagging WO - GEPL": 0.72,
		}
		mock_phys.side_effect = lambda item, batch, wh: physical[wh]
		wh = _pick_source_warehouse(
			"D-NT-RO-6B-+7-7.5",
			"GE2D075-DNTROX6G00G05-16",
			0.72,
			["Diamond Setting WO - GEPL", "Tagging WO - GEPL"],
		)
		self.assertEqual(wh, "Tagging WO - GEPL")

	@patch(f"{_SNC_MODULE}._physical_batch_qty", return_value=0.5)
	def test_none_when_no_candidate_covers(self, _mock):
		self.assertIsNone(
			_pick_source_warehouse(
				"D-NT-RO-6B-+7-7.5",
				"GE2D075-DNTROX6G00G05-16",
				0.72,
				["Diamond Setting WO - GEPL", "Tagging WO - GEPL"],
			)
		)

	@patch(f"{_SNC_MODULE}._physical_batch_qty", return_value=0.7199)
	def test_within_tolerance_accepted(self, _mock):
		# 0.7199 + TOLERANCE (0.0001) >= 0.72 -> accepted.
		wh = _pick_source_warehouse(
			"D-NT-RO-6B-+7-7.5",
			"GE2D075-DNTROX6G00G05-16",
			0.72,
			["Tagging WO - GEPL"],
		)
		self.assertEqual(wh, "Tagging WO - GEPL")


class TestSNCSeDetailMaps(IntegrationTestCase):
	"""``_snc_se_detail_maps`` re-derives per-item rate and inventory type from the
	Manufacture SE, so the SNC-created FG BOM can set ``is_customer_item`` (the
	aggregated fg_details rows don't carry inventory_type). Regression: without the
	inventory-type map, is_customer_item was always 0 on SNC-created BOMs even though
	the Manufacture SE showed the material as Customer Goods.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MOP_MODULE}.frappe.db.sql")
	def test_customer_goods_and_rate_maps(self, mock_sql):
		# One customer-goods diamond, one regular metal, one item whose consumed rows
		# had no inventory_type (MAX(...) -> None). MySQL MAX(bool) yields 1/0/None.
		mock_sql.return_value = [
			frappe._dict(item_code="D-CG", rate=0.0, is_customer_goods=1),
			frappe._dict(item_code="M-REG", rate=4500.0, is_customer_goods=0),
			frappe._dict(item_code="D-NULL", rate=0.0, is_customer_goods=None),
		]
		rate_map, inv_map = _snc_se_detail_maps("MAT-STE-TEST")

		self.assertEqual(rate_map, {"D-CG": 0.0, "M-REG": 4500.0, "D-NULL": 0.0})
		# Any Customer-Goods batch -> "Customer Goods"; 0/None -> "Regular Stock".
		self.assertEqual(inv_map["D-CG"], "Customer Goods")
		self.assertEqual(inv_map["M-REG"], "Regular Stock")
		self.assertEqual(inv_map["D-NULL"], "Regular Stock")

	@patch(f"{_MOP_MODULE}.frappe.db.sql")
	def test_empty_se_returns_empty_maps(self, mock_sql):
		mock_sql.return_value = []
		rate_map, inv_map = _snc_se_detail_maps("MAT-STE-EMPTY")
		self.assertEqual(rate_map, {})
		self.assertEqual(inv_map, {})


class TestStoneSeRate(IntegrationTestCase):
	"""``_stone_se_rate`` fills diamond/gemstone se_rate from the Item's maintained
	valuation_rate when the qty-weighted consumed rate is 0. Regression: diamond/
	gemstone se_rate was blank whenever the Manufacture SE basic_rate was 0 (the
	common case, since custom_metal_rate is metal-only).
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_consumed_rate_wins_when_present(self):
		self.assertEqual(_stone_se_rate(4500.0, 1.0), 4500.0)

	def test_falls_back_to_valuation_when_rate_zero(self):
		self.assertEqual(_stone_se_rate(0.0, 25.5), 25.5)

	def test_falls_back_when_rate_none(self):
		self.assertEqual(_stone_se_rate(None, 25.5), 25.5)

	def test_zero_when_neither_available(self):
		self.assertEqual(_stone_se_rate(0.0, 0.0), 0.0)
		self.assertEqual(_stone_se_rate(None, None), 0.0)


class TestDeriveOwnershipTag(IntegrationTestCase):
	"""``_derive_ownership_tag`` classifies the FG serial as Outright / Outwork /
	Hybrid from the material the Manufacture SE consumed. It reads the batch-corrected
	``row_data`` (consumption rows only), NOT ``_snc_se_detail_maps``' inv_map — that
	one includes the finished-good row, whose inventory_type is hardcoded
	"Regular Stock", which would misreport every pure customer-material job as Hybrid.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_all_regular_stock_is_outright(self):
		row_data = [
			{"inventory_type": "Regular Stock"},
			{"inventory_type": "Regular Stock"},
		]
		self.assertEqual(_derive_ownership_tag(row_data), "Outright")

	def test_all_customer_goods_is_outwork(self):
		row_data = [
			{"inventory_type": "Customer Goods"},
			{"inventory_type": "Customer Goods"},
		]
		self.assertEqual(_derive_ownership_tag(row_data), "Outwork")

	def test_mixed_is_hybrid(self):
		row_data = [
			{"inventory_type": "Regular Stock"},
			{"inventory_type": "Customer Goods"},
		]
		self.assertEqual(_derive_ownership_tag(row_data), "Hybrid")

	def test_nothing_derivable_returns_none(self):
		# No rows, blank strings and missing keys all mean "cannot tell" — the caller
		# leaves the Serial No untagged rather than guessing.
		self.assertIsNone(_derive_ownership_tag([]))
		self.assertIsNone(_derive_ownership_tag(None))
		self.assertIsNone(
			_derive_ownership_tag(
				[{"inventory_type": ""}, {"inventory_type": None}, {}]
			)
		)

	def test_blank_rows_do_not_promote_outwork_to_hybrid(self):
		row_data = [{"inventory_type": "Customer Goods"}, {"inventory_type": ""}, {}]
		self.assertEqual(_derive_ownership_tag(row_data), "Outwork")

	def test_blank_rows_do_not_block_outright(self):
		row_data = [{"inventory_type": "Regular Stock"}, {"inventory_type": None}]
		self.assertEqual(_derive_ownership_tag(row_data), "Outright")


# ── Warehouse-split source rows ────────────────────────────────────────────────
#
# Live case behind the whole group: SNC 7e4huirltd, batch None2F074-MGL22919Y0-01VO7
# of M-G-22KT-91.9-Y. The job reserved 3.557 in "Waxing WO - KGJPL" and 0.02 in
# "Model Making WO - KGJPL" — 3.577 total, exactly the source row qty — but the single
# source row demanded all 3.577 from one warehouse and submit threw. The same batch also
# sits in four other warehouses (Casting MSL WH 1: 6.35, Model Making RM: 5.0, Assembly
# MSL WH 14: 4.98, Waxing RM: 1.763) reserved for OTHER jobs, which must never be drawn on.

_LIVE_BATCH = "None2F074-MGL22919Y0-01VO7"
_LIVE_ITEM = "M-G-22KT-91.9-Y"
_WAXING = "Waxing WO - KGJPL"
_MODEL_MAKING = "Model Making WO - KGJPL"


class TestAllocateQtyAcrossWarehouses(IntegrationTestCase):
	"""Greedy split of a row qty over the warehouses this job has reserved."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_live_case_splits_across_two_warehouses(self):
		caps = [(_WAXING, 3.557, ["a"]), (_MODEL_MAKING, 0.02, ["b"])]
		allocs = _allocate_qty_across_warehouses(3.577, caps)
		self.assertEqual(allocs, [(_WAXING, 3.557), (_MODEL_MAKING, 0.02)])
		self.assertEqual(sum(q for _wh, q in allocs), 3.577)

	def test_single_warehouse_covering_returns_one_row(self):
		caps = [("Diamond Setting WO - KGJPL", 0.29, ["c"])]
		self.assertEqual(
			_allocate_qty_across_warehouses(0.29, caps),
			[("Diamond Setting WO - KGJPL", 0.29)],
		)

	def test_partial_coverage_returns_none(self):
		# Reserved 3.5 but 3.577 needed: refuse to split rather than strand the
		# remainder on a warehouse with no reservation for this job.
		caps = [(_WAXING, 3.48, ["a"]), (_MODEL_MAKING, 0.02, ["b"])]
		self.assertIsNone(_allocate_qty_across_warehouses(3.577, caps))

	def test_no_caps_or_zero_qty_returns_none(self):
		self.assertIsNone(_allocate_qty_across_warehouses(3.577, []))
		self.assertIsNone(_allocate_qty_across_warehouses(0, [(_WAXING, 5.0, ["a"])]))

	def test_surplus_capacity_is_truncated(self):
		# 5.0 reserved, only 0.02 needed -> take 0.02 and stop; the second warehouse is
		# never reached, so its reservation is left alone.
		caps = [(_WAXING, 5.0, ["a"]), (_MODEL_MAKING, 1.0, ["b"])]
		self.assertEqual(_allocate_qty_across_warehouses(0.02, caps), [(_WAXING, 0.02)])

	def test_sum_is_exact_for_sub_milligram_qty(self):
		# calulate_id_wise_sum_up compares per-item totals; the split must not drift.
		caps = [(_WAXING, 3.557, ["a"]), (_MODEL_MAKING, 0.05, ["b"])]
		allocs = _allocate_qty_across_warehouses(3.5771, caps)
		self.assertEqual(sum(q for _wh, q in allocs), 3.5771)

	def test_zero_capacity_warehouse_never_emitted(self):
		caps = [(_WAXING, 3.577, ["a"]), (_MODEL_MAKING, 0.00005, ["b"])]
		self.assertEqual(
			_allocate_qty_across_warehouses(3.577, caps), [(_WAXING, 3.577)]
		)


class TestAllocatePcsAcrossRows(IntegrationTestCase):
	"""Pcs must survive the split: fg_details SUMs pcs for D/G items and takes max()
	for metal, so the distribution has to preserve whichever the item uses.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_metal_keeps_whole_count_on_largest_row(self):
		parts = _allocate_pcs_across_rows(
			_LIVE_ITEM, 2, [(_WAXING, 3.557), (_MODEL_MAKING, 0.02)]
		)
		self.assertEqual(parts, [2.0, 0.0])
		self.assertEqual(sum(parts), 2)  # SUM preserved
		self.assertEqual(max(parts), 2)  # MAX preserved

	def test_diamond_splits_pcs_and_preserves_total(self):
		parts = _allocate_pcs_across_rows(
			"D-NT-RO-6B-+1.5-2", 8, [("WH-A", 0.2), ("WH-B", 0.09)]
		)
		self.assertEqual(sum(parts), 8)
		self.assertTrue(all(p >= 1 for p in parts))

	def test_diamond_with_fewer_stones_than_rows_keeps_count_whole(self):
		parts = _allocate_pcs_across_rows(
			"D-NT-RO-6B-+1.5-2", 1, [("WH-A", 0.2), ("WH-B", 0.09)]
		)
		self.assertEqual(parts, [1.0, 0.0])

	def test_fractional_count_is_not_split_into_fractional_stones(self):
		parts = _allocate_pcs_across_rows(
			"D-NT-RO-6B-+1.5-2", 2.5, [("WH-A", 0.2), ("WH-B", 0.09)]
		)
		self.assertEqual(parts, [2.5, 0.0])

	def test_zero_pcs_and_single_row(self):
		self.assertEqual(
			_allocate_pcs_across_rows(_LIVE_ITEM, 0, [("WH-A", 1), ("WH-B", 1)]),
			[0.0, 0.0],
		)
		self.assertEqual(_allocate_pcs_across_rows(_LIVE_ITEM, 2, [("WH-A", 1)]), [2.0])

	def test_never_returns_none_for_a_row(self):
		# Stock Entry Detail.pcs is a Data field with default "1" — a None would
		# silently become 1 and inflate the count.
		for parts in (
			_allocate_pcs_across_rows(_LIVE_ITEM, 2, [("A", 1), ("B", 1)]),
			_allocate_pcs_across_rows("D-X", 3, [("A", 1), ("B", 1)]),
			_allocate_pcs_across_rows(_LIVE_ITEM, None, [("A", 1), ("B", 1)]),
		):
			self.assertTrue(all(p is not None for p in parts))


class TestActiveSresFor(IntegrationTestCase):
	"""Which reservations count as live, and how much of one belongs to a batch."""

	@classmethod
	def setUpClass(cls):
		pass

	@staticmethod
	def _mock_get_all(sres, children):
		def _inner(doctype, **kwargs):
			if doctype == "Stock Reservation Entry":
				return sres
			return children

		return _inner

	@patch(f"{_SNC_MODULE}.frappe.get_all")
	def test_batch_scoped_remaining_from_children(self, mock_get_all):
		# Header reserves 5.0 across two batches; only 3.557 of it is this batch's.
		mock_get_all.side_effect = self._mock_get_all(
			[
				{
					"name": "sre1",
					"warehouse": _WAXING,
					"reserved_qty": 5.0,
					"delivered_qty": 0.0,
				}
			],
			[
				{
					"parent": "sre1",
					"batch_no": _LIVE_BATCH,
					"qty": 3.557,
					"delivered_qty": 0.0,
				},
				{
					"parent": "sre1",
					"batch_no": "OTHER-BATCH",
					"qty": 1.443,
					"delivered_qty": 0.0,
				},
			],
		)
		out = _active_sres_for(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"])
		self.assertEqual([rem for _sre, rem in out], [3.557])

	@patch(f"{_SNC_MODULE}.frappe.get_all")
	def test_sre_on_another_batch_is_skipped(self, mock_get_all):
		mock_get_all.side_effect = self._mock_get_all(
			[
				{
					"name": "sre1",
					"warehouse": _WAXING,
					"reserved_qty": 5.0,
					"delivered_qty": 0.0,
				}
			],
			[
				{
					"parent": "sre1",
					"batch_no": "OTHER-BATCH",
					"qty": 5.0,
					"delivered_qty": 0.0,
				}
			],
		)
		self.assertEqual(_active_sres_for(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"]), [])

	@patch(f"{_SNC_MODULE}.frappe.get_all")
	def test_fully_delivered_child_is_skipped(self, mock_get_all):
		mock_get_all.side_effect = self._mock_get_all(
			[
				{
					"name": "sre1",
					"warehouse": _WAXING,
					"reserved_qty": 3.557,
					"delivered_qty": 0.0,
				}
			],
			[
				{
					"parent": "sre1",
					"batch_no": _LIVE_BATCH,
					"qty": 3.557,
					"delivered_qty": 3.557,
				}
			],
		)
		self.assertEqual(_active_sres_for(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"]), [])

	@patch(f"{_SNC_MODULE}.frappe.get_all")
	def test_qty_based_sre_uses_header_remaining(self, mock_get_all):
		mock_get_all.side_effect = self._mock_get_all(
			[
				{
					"name": "sre1",
					"warehouse": _WAXING,
					"reserved_qty": 4.0,
					"delivered_qty": 1.0,
				}
			],
			[],
		)
		out = _active_sres_for(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"])
		self.assertEqual([rem for _sre, rem in out], [3.0])

	@patch(f"{_SNC_MODULE}.frappe.get_all")
	def test_excluded_sre_is_dropped(self, mock_get_all):
		mock_get_all.side_effect = self._mock_get_all(
			[
				{
					"name": "sre1",
					"warehouse": _WAXING,
					"reserved_qty": 3.0,
					"delivered_qty": 0.0,
				}
			],
			[],
		)
		self.assertEqual(
			_active_sres_for(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"], exclude={"sre1"}), []
		)

	def test_no_mwos_returns_empty(self):
		self.assertEqual(_active_sres_for(_LIVE_ITEM, _LIVE_BATCH, []), [])


class TestReservedWarehouseCaps(IntegrationTestCase):
	"""Capacity is ``min(reserved-remaining, physical)`` per warehouse, and ONLY for
	warehouses this job actually holds a reservation in.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_SNC_MODULE}._physical_batch_qty")
	@patch(f"{_SNC_MODULE}._active_sres_for")
	def test_live_case_two_warehouses(self, mock_sres, mock_phys):
		mock_sres.return_value = [
			({"name": "a", "warehouse": _WAXING}, 3.557),
			({"name": "b", "warehouse": _MODEL_MAKING}, 0.02),
		]
		physical = {_WAXING: 3.557, _MODEL_MAKING: 0.02}
		mock_phys.side_effect = lambda item, batch, wh: physical[wh]
		self.assertEqual(
			_reserved_warehouse_caps(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"]),
			[(_WAXING, 3.557, ["a"]), (_MODEL_MAKING, 0.02, ["b"])],
		)

	@patch(f"{_SNC_MODULE}._physical_batch_qty")
	@patch(f"{_SNC_MODULE}._active_sres_for")
	def test_warehouse_holding_the_batch_without_a_reservation_is_excluded(
		self, mock_sres, mock_phys
	):
		# Casting MSL WH 1 physically holds 6.35 of the batch but belongs to another
		# job. It has no SRE for this PMO, so it can never become a candidate.
		mock_sres.return_value = [({"name": "a", "warehouse": _WAXING}, 3.557)]
		mock_phys.return_value = 3.557
		caps = _reserved_warehouse_caps(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"])
		self.assertEqual([wh for wh, _cap, _sres in caps], [_WAXING])

	@patch(f"{_SNC_MODULE}._physical_batch_qty", return_value=1.0)
	@patch(f"{_SNC_MODULE}._active_sres_for")
	def test_physical_stock_caps_an_over_reservation(self, mock_sres, _mock_phys):
		# Reserved 5.0 but only 1.0 physically left there: never promise the 5.0.
		mock_sres.return_value = [({"name": "a", "warehouse": _WAXING}, 5.0)]
		self.assertEqual(
			_reserved_warehouse_caps(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"]),
			[(_WAXING, 1.0, ["a"])],
		)

	@patch(f"{_SNC_MODULE}._physical_batch_qty", return_value=0.0)
	@patch(f"{_SNC_MODULE}._active_sres_for")
	def test_stale_reservation_with_no_physical_stock_is_dropped(
		self, mock_sres, _mock_phys
	):
		mock_sres.return_value = [({"name": "a", "warehouse": _WAXING}, 3.557)]
		self.assertEqual(
			_reserved_warehouse_caps(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"]), []
		)

	@patch(f"{_SNC_MODULE}._physical_batch_qty", return_value=10.0)
	@patch(f"{_SNC_MODULE}._active_sres_for")
	def test_two_reservations_in_one_warehouse_merge(self, mock_sres, _mock_phys):
		mock_sres.return_value = [
			({"name": "a", "warehouse": _WAXING}, 2.0),
			({"name": "b", "warehouse": _WAXING}, 1.5),
		]
		self.assertEqual(
			_reserved_warehouse_caps(_LIVE_ITEM, _LIVE_BATCH, ["MWO-1"]),
			[(_WAXING, 3.5, ["a", "b"])],
		)


class TestPickSourceWarehouseUsedLedger(IntegrationTestCase):
	"""The ``used`` ledger stops two rows of a split group both claiming the warehouse
	that only has enough for one of them.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_SNC_MODULE}._physical_batch_qty", return_value=3.557)
	def test_no_ledger_behaves_as_before(self, _mock):
		self.assertEqual(
			_pick_source_warehouse(_LIVE_ITEM, _LIVE_BATCH, 0.02, [_WAXING]), _WAXING
		)

	@patch(f"{_SNC_MODULE}._physical_batch_qty")
	def test_exhausted_warehouse_is_skipped(self, mock_phys):
		physical = {_WAXING: 3.557, _MODEL_MAKING: 0.02}
		mock_phys.side_effect = lambda item, batch, wh: physical[wh]
		# The 3.557 row already took all of Waxing; the 0.02 sibling must fall through.
		wh = _pick_source_warehouse(
			_LIVE_ITEM,
			_LIVE_BATCH,
			0.02,
			[_WAXING, _MODEL_MAKING],
			used={_WAXING: 3.557},
		)
		self.assertEqual(wh, _MODEL_MAKING)


class TestSplitSourceRowsByReservation(IntegrationTestCase):
	"""``source_table`` is keyed by (item, batch, warehouse); ``fg_details`` stays
	item-wise. The split preserves the per-item qty total, so calulate_id_wise_sum_up
	keeps balancing.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _snc(self, rows):
		doc = frappe.new_doc("Serial Number Creator")
		doc.manufacturing_work_order = "MWO-TEST-001"
		for row in rows:
			doc.append("source_table", row)
		return doc

	def _live_doc(self):
		return self._snc(
			[
				{
					"row_material": "D-NT-RO-6B-+1.5-2",
					"qty": 0.29,
					"pcs": 8,
					"uom": "Carat",
					"batch_no": "None2F073-DNTROX6A05B00-W04B5",
					"s_warehouse": "Diamond Setting WO - KGJPL",
				},
				{
					"row_material": _LIVE_ITEM,
					"qty": 3.577,
					"pcs": 2,
					"uom": "Gram",
					"batch_no": _LIVE_BATCH,
					"s_warehouse": _WAXING,
				},
			]
		)

	@staticmethod
	def _caps_for_live_case(item_code, batch_no, _mwos):
		if item_code == _LIVE_ITEM:
			return [(_WAXING, 3.557, ["a"]), (_MODEL_MAKING, 0.02, ["b"])]
		return [("Diamond Setting WO - KGJPL", 0.29, ["c"])]

	@patch(f"{_SNC_MODULE}._pmo_mwo_names", return_value=("PMO-1", ["MWO-TEST-001"]))
	@patch(f"{_SNC_MODULE}._reserved_warehouse_caps")
	def test_splits_metal_row_and_leaves_diamond_alone(self, mock_caps, _mock_mwos):
		mock_caps.side_effect = self._caps_for_live_case
		doc = self._live_doc()
		split_source_rows_by_reservation(doc)

		self.assertEqual(len(doc.source_table), 3)
		metal = [r for r in doc.source_table if r.row_material == _LIVE_ITEM]
		self.assertEqual(
			[(r.s_warehouse, r.qty) for r in metal],
			[(_WAXING, 3.557), (_MODEL_MAKING, 0.02)],
		)
		# Per-item totals unchanged -> calulate_id_wise_sum_up still balances.
		self.assertEqual(sum(r.qty for r in metal), 3.577)
		# Metal pcs stays whole on the larger row (fg_details takes max()).
		self.assertEqual([r.pcs for r in metal], [2.0, 0.0])
		# Non-warehouse detail is carried onto the new row.
		self.assertEqual([r.uom for r in metal], ["Gram", "Gram"])
		# The diamond row, which one warehouse fully covers, is untouched.
		diamond = [r for r in doc.source_table if r.row_material != _LIVE_ITEM]
		self.assertEqual(len(diamond), 1)
		self.assertEqual(diamond[0].qty, 0.29)
		self.assertEqual([r.idx for r in doc.source_table], [1, 2, 3])

	@patch(f"{_SNC_MODULE}._pmo_mwo_names", return_value=("PMO-1", ["MWO-TEST-001"]))
	@patch(f"{_SNC_MODULE}._reserved_warehouse_caps")
	def test_second_pass_is_a_no_op(self, mock_caps, _mock_mwos):
		mock_caps.side_effect = self._caps_for_live_case
		doc = self._live_doc()
		split_source_rows_by_reservation(doc)
		before = [
			(r.row_material, r.s_warehouse, r.qty, r.pcs) for r in doc.source_table
		]
		split_source_rows_by_reservation(doc)
		after = [
			(r.row_material, r.s_warehouse, r.qty, r.pcs) for r in doc.source_table
		]
		self.assertEqual(before, after)

	@patch(f"{_SNC_MODULE}._pmo_mwo_names", return_value=("PMO-1", ["MWO-TEST-001"]))
	@patch(f"{_SNC_MODULE}._reserved_warehouse_caps", return_value=[])
	def test_no_reservation_leaves_rows_untouched(self, _mock_caps, _mock_mwos):
		doc = self._live_doc()
		split_source_rows_by_reservation(doc)
		self.assertEqual(len(doc.source_table), 2)
		self.assertEqual(doc.source_table[1].s_warehouse, _WAXING)

	@patch(f"{_SNC_MODULE}._pmo_mwo_names", return_value=("PMO-1", ["MWO-TEST-001"]))
	@patch(f"{_SNC_MODULE}._reserved_warehouse_caps")
	def test_shortfall_leaves_rows_untouched(self, mock_caps, _mock_mwos):
		# Only 3.5 reserved for a 3.577 row -> no split; submit fails fast with the
		# reserved-vs-physical message instead of silently under-consuming.
		mock_caps.return_value = [(_WAXING, 3.48, ["a"]), (_MODEL_MAKING, 0.02, ["b"])]
		doc = self._snc(
			[
				{
					"row_material": _LIVE_ITEM,
					"qty": 3.577,
					"pcs": 2,
					"batch_no": _LIVE_BATCH,
					"s_warehouse": _WAXING,
				}
			]
		)
		split_source_rows_by_reservation(doc)
		self.assertEqual(len(doc.source_table), 1)
		self.assertEqual(doc.source_table[0].qty, 3.577)

	@patch(f"{_SNC_MODULE}._pmo_mwo_names", return_value=("PMO-1", ["MWO-TEST-001"]))
	@patch(f"{_SNC_MODULE}._reserved_warehouse_caps")
	def test_collapses_back_when_reservations_merge(self, mock_caps, _mock_mwos):
		# A draft split earlier; the reservations have since consolidated onto one
		# warehouse, so the group must merge back to a single row.
		mock_caps.return_value = [(_WAXING, 5.0, ["a"])]
		doc = self._snc(
			[
				{
					"row_material": _LIVE_ITEM,
					"qty": 3.557,
					"pcs": 2,
					"batch_no": _LIVE_BATCH,
					"s_warehouse": _WAXING,
				},
				{
					"row_material": _LIVE_ITEM,
					"qty": 0.02,
					"pcs": 0,
					"batch_no": _LIVE_BATCH,
					"s_warehouse": _MODEL_MAKING,
				},
			]
		)
		split_source_rows_by_reservation(doc)
		self.assertEqual(len(doc.source_table), 1)
		self.assertEqual(doc.source_table[0].s_warehouse, _WAXING)
		self.assertEqual(doc.source_table[0].qty, 3.577)

	@patch(f"{_SNC_MODULE}._pmo_mwo_names", return_value=("PMO-1", ["MWO-TEST-001"]))
	@patch(f"{_SNC_MODULE}._reserved_warehouse_caps")
	def test_corrects_a_stale_single_warehouse(self, mock_caps, _mock_mwos):
		mock_caps.return_value = [(_MODEL_MAKING, 3.577, ["a"])]
		doc = self._snc(
			[
				{
					"row_material": _LIVE_ITEM,
					"qty": 3.577,
					"pcs": 2,
					"batch_no": _LIVE_BATCH,
					"s_warehouse": _WAXING,
				}
			]
		)
		split_source_rows_by_reservation(doc)
		self.assertEqual(len(doc.source_table), 1)
		self.assertEqual(doc.source_table[0].s_warehouse, _MODEL_MAKING)

	@patch(f"{_SNC_MODULE}._reserved_warehouse_caps")
	def test_skipped_without_a_work_order(self, mock_caps):
		doc = frappe.new_doc("Serial Number Creator")
		doc.append(
			"source_table",
			{"row_material": _LIVE_ITEM, "qty": 3.577, "batch_no": _LIVE_BATCH},
		)
		split_source_rows_by_reservation(doc)
		mock_caps.assert_not_called()
