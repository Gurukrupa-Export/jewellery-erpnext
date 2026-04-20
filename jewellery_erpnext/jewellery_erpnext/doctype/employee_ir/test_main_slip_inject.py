# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	main_slip_inject as msi,
)


def _eir(**overrides):
	base = dict(
		name="EIR-R-001",
		doctype="Employee IR",
		company="GE",
		department="Trishul - GE",
		employee="EMP-001",
		subcontractor=None,
		subcontracting="No",
		main_slip="MS-001",
		is_main_slip_required=1,
	)
	base.update(overrides)
	return SimpleNamespace(**base)


def _row(**overrides):
	base = dict(
		name="eiro-001",
		manufacturing_operation="MOP-1",
		manufacturing_work_order="MWO-1",
		gross_wt=10.0,
		received_gross_wt=12.0,
	)
	base.update(overrides)
	return SimpleNamespace(**base)


class TestMainSlipInjectGate(FrappeTestCase):
	def test_skips_when_is_main_slip_required_false(self):
		eir = _eir(is_main_slip_required=0)
		row = _row()
		with patch.object(msi, "_existing_injection_se") as mock_exists:
			out = msi.inject_extra_metal_for_eir_receive(eir, row)
		self.assertEqual(out, [])
		mock_exists.assert_not_called()

	def test_skips_when_no_extra_qty(self):
		eir = _eir()
		row = _row(received_gross_wt=10.0, gross_wt=10.0)
		with patch.object(msi, "_existing_injection_se") as mock_exists:
			out = msi.inject_extra_metal_for_eir_receive(eir, row)
		self.assertEqual(out, [])
		mock_exists.assert_not_called()

	def test_skips_when_negative_delta(self):
		eir = _eir()
		row = _row(received_gross_wt=8.0, gross_wt=10.0)
		with patch.object(msi, "_existing_injection_se") as mock_exists:
			out = msi.inject_extra_metal_for_eir_receive(eir, row)
		self.assertEqual(out, [])
		mock_exists.assert_not_called()


class TestMainSlipInjectIdempotency(FrappeTestCase):
	def test_existing_repack_se_short_circuits(self):
		eir = _eir()
		row = _row()
		with patch.object(msi, "_existing_injection_se", return_value=True) as mock_exists, patch.object(
			msi, "_resolve_inject_metal_items"
		) as mock_resolve, patch.object(
			msi, "_build_repack_se"
		) as mock_build:
			out = msi.inject_extra_metal_for_eir_receive(eir, row)
		self.assertEqual(out, [])
		mock_exists.assert_called_once()
		mock_resolve.assert_not_called()
		mock_build.assert_not_called()


_MSI_PATH = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject"


class TestMainSlipInjectMetalResolution(FrappeTestCase):
	@patch(f"{_MSI_PATH}.get_item_from_attribute")
	@patch(f"{_MSI_PATH}.frappe.db.get_value")
	def test_single_colour_emits_one_item(self, mock_get_value, mock_get_item):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": "18KT",
			"metal_purity": "75.4",
			"metal_colour": "Yellow",
			"multicolour": 0,
			"allowed_colours": "",
		}
		mock_get_item.return_value = "M-G-18KT-Y"
		items = msi._resolve_inject_metal_items("MWO-1", 2.0)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["item_code"], "M-G-18KT-Y")
		self.assertEqual(items[0]["qty"], 2.0)

	@patch(f"{_MSI_PATH}.get_item_from_attribute")
	@patch(f"{_MSI_PATH}.frappe.db.get_value")
	def test_multicolour_even_splits(self, mock_get_value, mock_get_item):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": "18KT",
			"metal_purity": "75.4",
			"metal_colour": None,
			"multicolour": 1,
			"allowed_colours": "Yellow, White, Rose",
		}

		def _item_for(mt, mtc, mp, colour):
			return f"M-G-18KT-{colour[0]}"

		mock_get_item.side_effect = _item_for
		items = msi._resolve_inject_metal_items("MWO-1", 3.0)
		self.assertEqual(len(items), 3)
		self.assertEqual([i["item_code"] for i in items], ["M-G-18KT-Y", "M-G-18KT-W", "M-G-18KT-R"])
		for i in items:
			self.assertAlmostEqual(i["qty"], 1.0)

	@patch(f"{_MSI_PATH}.frappe.db.get_value")
	def test_throws_when_mwo_missing_attributes(self, mock_get_value):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": None,
			"metal_purity": "75.4",
			"metal_colour": "Yellow",
			"multicolour": 0,
			"allowed_colours": "",
		}
		with self.assertRaises(frappe.ValidationError):
			msi._resolve_inject_metal_items("MWO-1", 2.0)

	@patch(f"{_MSI_PATH}.get_item_from_attribute", return_value=None)
	@patch(f"{_MSI_PATH}.frappe.db.get_value")
	def test_throws_when_metal_item_not_resolvable(self, mock_get_value, mock_get_item):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": "18KT",
			"metal_purity": "75.4",
			"metal_colour": "Yellow",
			"multicolour": 0,
			"allowed_colours": "",
		}
		with self.assertRaises(frappe.ValidationError):
			msi._resolve_inject_metal_items("MWO-1", 2.0)


class TestMainSlipInjectWarehouseResolution(FrappeTestCase):
	@patch(f"{_MSI_PATH}.frappe.db.get_value")
	def test_employee_warehouse_for_non_subcontracting(self, mock_get_value):
		mock_get_value.return_value = "WH-Emp"
		out = msi._resolve_source_warehouse(_eir(subcontracting="No", employee="EMP-7"))
		self.assertEqual(out, "WH-Emp")
		args = mock_get_value.call_args
		self.assertEqual(args[0][0], "Warehouse")
		self.assertEqual(args[0][1].get("employee"), "EMP-7")
		self.assertNotIn("subcontractor", args[0][1])

	@patch(f"{_MSI_PATH}.frappe.db.get_value")
	def test_subcontractor_warehouse_for_subcontracting(self, mock_get_value):
		mock_get_value.return_value = "WH-Sub"
		out = msi._resolve_source_warehouse(
			_eir(subcontracting="Yes", subcontractor="SUB-1", employee=None)
		)
		self.assertEqual(out, "WH-Sub")
		args = mock_get_value.call_args
		self.assertEqual(args[0][1].get("subcontractor"), "SUB-1")


class TestMainSlipInjectStockCheck(FrappeTestCase):
	@patch(f"{_MSI_PATH}.frappe.db.get_value", return_value=0.5)
	def test_insufficient_stock_throws(self, mock_get_value):
		with self.assertRaises(frappe.ValidationError):
			msi._check_source_stock("M-G-18KT-Y", "WH-Emp", 2.0)

	@patch(f"{_MSI_PATH}.frappe.db.get_value", return_value=50.0)
	def test_sufficient_stock_passes_silently(self, mock_get_value):
		# No exception = pass.
		msi._check_source_stock("M-G-18KT-Y", "WH-Emp", 2.0)


class TestMainSlipInjectBuildRepackSE(FrappeTestCase):
	@patch(f"{_MSI_PATH}.frappe.db.get_value", return_value="PMO-1")
	@patch(f"{_MSI_PATH}.frappe.new_doc")
	def test_builds_consume_and_produce_rows_stamped_to_mop(self, mock_new_doc, mock_get_value):
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		eir = _eir(subcontracting="No", employee="EMP-7")
		row = _row(manufacturing_operation="MOP-ABC")
		items = [{"item_code": "M-G-18KT-Y", "qty": 1.5, "batch_no": None}]
		msi._build_repack_se(eir, row, items, "WH-Emp", "WH-Dept")

		self.assertEqual(se.stock_entry_type, "Repack")
		self.assertEqual(se.employee_ir, "EIR-R-001")
		self.assertEqual(se.custom_eir_operation_row, "eiro-001")
		self.assertEqual(se.auto_created, 1)
		self.assertEqual(len(se.items), 2)
		# first row is consume from source, second is produce to dept
		self.assertEqual(se.items[0]["s_warehouse"], "WH-Emp")
		self.assertIsNone(se.items[0].get("t_warehouse"))
		self.assertNotIn("manufacturing_operation", se.items[0])
		self.assertEqual(se.items[1]["t_warehouse"], "WH-Dept")
		self.assertEqual(se.items[1]["manufacturing_operation"], "MOP-ABC")


class TestMainSlipInjectCancel(FrappeTestCase):
	@patch(f"{_MSI_PATH}.frappe.get_doc")
	@patch(f"{_MSI_PATH}.frappe.db.get_all", return_value=["SE-AUTO-1", "SE-AUTO-2"])
	def test_cancel_cancels_every_auto_se(self, mock_get_all, mock_get_doc):
		docs = [MagicMock(), MagicMock()]
		mock_get_doc.side_effect = docs
		out = msi.cancel_injections_for_eir("EIR-R-001")
		self.assertEqual(out, ["SE-AUTO-1", "SE-AUTO-2"])
		for d in docs:
			d.cancel.assert_called_once()
		# Filter now matches both Repack and Material Transfer (WORK ORDER).
		filters = mock_get_all.call_args[1]["filters"]
		self.assertIn("auto_created", filters)
		self.assertEqual(
			set(filters["stock_entry_type"][1]),
			{"Repack", "Material Transfer (WORK ORDER)"},
		)


# ---------------------------------------------------------------------------
# Main Slip batch-walking path
# ---------------------------------------------------------------------------


def _batch_row(**overrides):
	base = dict(
		name="MSSED-1",
		batch_no="BATCH-1",
		item_code="M-G-18KT-Y",
		qty=5.0,
		consume_qty=0.0,
		inventory_type="Regular Stock",
		customer=None,
		variant_of="M",
		creation="2026-04-19 09:00:00",
	)
	base.update(overrides)
	return base


class TestMainSlipBatchIterator(FrappeTestCase):
	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	def test_priority_order_regular_then_customer_then_pure(self, mock_get_all):
		mock_get_all.return_value = [
			_batch_row(name="B-PURE", inventory_type="Pure Metal", creation="2026-04-01 00:00:00"),
			_batch_row(name="B-REG", inventory_type="Regular Stock", creation="2026-04-02 00:00:00"),
			_batch_row(name="B-CUST", inventory_type="Customer Goods", creation="2026-04-03 00:00:00"),
		]
		ordered = [r["name"] for r in msi._iter_main_slip_batches("MS-001")]
		self.assertEqual(ordered, ["B-REG", "B-CUST", "B-PURE"])

	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	def test_skips_rows_with_no_available_qty(self, mock_get_all):
		mock_get_all.return_value = [
			_batch_row(name="B-FULL", qty=2.0, consume_qty=2.0),
			_batch_row(name="B-OK", qty=3.0, consume_qty=0.5),
		]
		ordered = [r["name"] for r in msi._iter_main_slip_batches("MS-001")]
		self.assertEqual(ordered, ["B-OK"])


class TestMainSlipInjectViaBatches(FrappeTestCase):
	def _setup_common_mocks(self, stack, target_item="M-G-18KT-Y"):
		"""Patch the helpers used by _inject_via_main_slip_batches and return
		a submit-recorder that logs each SE that was saved+submitted."""
		submitted = []

		def _recorder(se):
			se.flags = getattr(se, "flags", MagicMock())
			se.save = MagicMock()
			se.submit = MagicMock()
			se.name = f"SE-AUTO-{len(submitted) + 1}"
			submitted.append(se)
			return se

		# inject_extra_metal_for_eir_receive resolves target items and dept wh
		stack.append(
			patch(f"{_MSI_PATH}._resolve_inject_metal_items", return_value=[{"item_code": target_item, "qty": 2.0}])
		)
		stack.append(patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"))
		stack.append(patch(f"{_MSI_PATH}._resolve_source_warehouse", return_value="WH-Src"))
		stack.append(patch(f"{_MSI_PATH}._existing_injection_se", return_value=False))
		return submitted

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_regular_stock_emits_material_transfer(self, mock_iter, mock_new_doc):
		mock_iter.return_value = iter([
			_batch_row(item_code="M-G-18KT-Y", inventory_type="Regular Stock", qty=3.0, consume_qty=0.0) | {"available_qty": 3.0},
		])
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		with patch(f"{_MSI_PATH}._resolve_inject_metal_items", return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}]), \
			 patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"), \
			 patch(f"{_MSI_PATH}._resolve_source_warehouse", return_value="WH-Src"), \
			 patch(f"{_MSI_PATH}._existing_injection_se", return_value=False), \
			 patch(f"{_MSI_PATH}.frappe.db.get_value", return_value="PMO-1"):
			out = msi.inject_extra_metal_for_eir_receive(_eir(main_slip="MS-1"), _row())

		self.assertEqual(len(out), 1)
		self.assertEqual(se.stock_entry_type, "Material Transfer (WORK ORDER)")
		# One SE with one item row, stamped to MOP.
		self.assertEqual(len(se.items), 1)
		self.assertEqual(se.items[0]["qty"], 2.0)
		self.assertEqual(se.items[0]["t_warehouse"], "WH-Dept")
		self.assertEqual(se.items[0]["manufacturing_operation"], _row().manufacturing_operation)

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_subcontracting_pure_metal_emits_repack_with_purity_conversion(self, mock_iter, mock_new_doc):
		# Source 24KT pure, target 18KT (75%) alloy.
		mock_iter.return_value = iter([
			_batch_row(item_code="M-G-24KT", inventory_type="Pure Metal", qty=5.0, consume_qty=0.0) | {"available_qty": 5.0},
		])
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		def _fake_get_value(doctype, filters_or_name, fieldname=None, **_kw):
			if doctype == "Item Variant Attribute":
				if filters_or_name.get("parent") == "M-G-24KT":
					return "99.9"
				if filters_or_name.get("parent") == "M-G-18KT-Y":
					return "75.4"
			return "PMO-1"

		with patch(f"{_MSI_PATH}._resolve_inject_metal_items", return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}]), \
			 patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"), \
			 patch(f"{_MSI_PATH}._resolve_source_warehouse", return_value="WH-Sub"), \
			 patch(f"{_MSI_PATH}._existing_injection_se", return_value=False), \
			 patch(f"{_MSI_PATH}.frappe.db.get_value", side_effect=_fake_get_value):
			out = msi.inject_extra_metal_for_eir_receive(
				_eir(main_slip="MS-1", subcontracting="Yes", subcontractor="SUB-1", employee=None),
				_row(),
			)

		self.assertEqual(len(out), 1)
		self.assertEqual(se.stock_entry_type, "Repack")
		self.assertEqual(len(se.items), 2)
		consume = se.items[0]
		produce = se.items[1]
		self.assertEqual(consume["item_code"], "M-G-24KT")
		self.assertEqual(consume["s_warehouse"], "WH-Sub")
		self.assertEqual(produce["item_code"], "M-G-18KT-Y")
		self.assertEqual(produce["t_warehouse"], "WH-Dept")
		self.assertEqual(produce["manufacturing_operation"], _row().manufacturing_operation)
		# produce_qty = consume_qty * 75.4 / 99.9 -> we requested 2.0 produced
		# so consume should be 2.0 * 75.4 / 99.9
		self.assertAlmostEqual(produce["qty"], 2.0, places=3)
		self.assertAlmostEqual(consume["qty"], round(2.0 * 75.4 / 99.9, 3), places=3)

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_insufficient_batches_throws(self, mock_iter, mock_new_doc):
		# Only 1g available but we need 2g.
		mock_iter.return_value = iter([
			_batch_row(item_code="M-G-18KT-Y", inventory_type="Regular Stock", qty=1.0, consume_qty=0.0) | {"available_qty": 1.0},
		])
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		with patch(f"{_MSI_PATH}._resolve_inject_metal_items", return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}]), \
			 patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"), \
			 patch(f"{_MSI_PATH}._resolve_source_warehouse", return_value="WH-Src"), \
			 patch(f"{_MSI_PATH}._existing_injection_se", return_value=False), \
			 patch(f"{_MSI_PATH}.frappe.db.get_value", return_value="PMO-1"):
			with self.assertRaises(frappe.ValidationError):
				msi.inject_extra_metal_for_eir_receive(_eir(main_slip="MS-1"), _row())

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_batch_item_mismatch_skips_non_pure_batches(self, mock_iter, mock_new_doc):
		# Wrong alloy in Main Slip and nothing matching -> throws short.
		mock_iter.return_value = iter([
			_batch_row(item_code="M-G-22KT-Y", inventory_type="Regular Stock", qty=5.0, consume_qty=0.0) | {"available_qty": 5.0},
		])
		se = MagicMock()
		mock_new_doc.return_value = se
		with patch(f"{_MSI_PATH}._resolve_inject_metal_items", return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}]), \
			 patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"), \
			 patch(f"{_MSI_PATH}._resolve_source_warehouse", return_value="WH-Src"), \
			 patch(f"{_MSI_PATH}._existing_injection_se", return_value=False), \
			 patch(f"{_MSI_PATH}.frappe.db.get_value", return_value="PMO-1"):
			with self.assertRaises(frappe.ValidationError):
				msi.inject_extra_metal_for_eir_receive(_eir(main_slip="MS-1"), _row())

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_non_subcontracting_pure_metal_falls_to_material_transfer(self, mock_iter, mock_new_doc):
		# Non-subcontracting: Pure Metal is treated as a direct Material Transfer
		# (no purity conversion). Pure Metal item MUST match target item; if it
		# does not, the batch is skipped and the helper throws short.
		mock_iter.return_value = iter([
			_batch_row(item_code="M-G-18KT-Y", inventory_type="Pure Metal", qty=3.0, consume_qty=0.0) | {"available_qty": 3.0},
		])
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		with patch(f"{_MSI_PATH}._resolve_inject_metal_items", return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}]), \
			 patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"), \
			 patch(f"{_MSI_PATH}._resolve_source_warehouse", return_value="WH-Emp"), \
			 patch(f"{_MSI_PATH}._existing_injection_se", return_value=False), \
			 patch(f"{_MSI_PATH}.frappe.db.get_value", return_value="PMO-1"):
			out = msi.inject_extra_metal_for_eir_receive(_eir(main_slip="MS-1", subcontracting="No"), _row())
		self.assertEqual(len(out), 1)
		self.assertEqual(se.stock_entry_type, "Material Transfer (WORK ORDER)")
