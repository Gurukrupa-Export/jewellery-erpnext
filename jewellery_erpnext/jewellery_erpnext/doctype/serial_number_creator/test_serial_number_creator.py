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
	_physical_batch_qty,
	_pick_source_warehouse,
	_sre_reserves_batch,
	_warehouse_has_batch_stock,
	calulate_id_wise_sum_up,
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
