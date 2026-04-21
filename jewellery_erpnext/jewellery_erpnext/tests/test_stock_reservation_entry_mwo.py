# Copyright (c) 2026, Nirali and contributors
# See license.txt

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase


class TestStockReservationEntryForMWO(FrappeTestCase):
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_sre_reserved_qty_for_voucher_detail_no",
		return_value=0.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_available_qty_to_reserve",
		return_value=50.0,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	def test_repack_skips_consume_only_rows_uses_batch_for_inbound(
		self,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			stock_reservation_entry_for_mwo,
		)

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			if doctype == "Item":
				return (1, 0)
			raise AssertionError(doctype)

		mock_cached.side_effect = _cached

		sre = MagicMock()
		mock_new_doc.return_value = sre

		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.company = "GE"
		doc.manufacturer = None
		doc.employee_ir = None

		consume = MagicMock()
		consume.item_code = "M-PURE"
		consume.qty = 2.0
		consume.t_warehouse = None
		consume.s_warehouse = "WH-Src"
		consume.uom = "Gram"
		consume.batch_no = "B-IN"
		consume.manufacturing_operation = None
		consume.get = MagicMock(side_effect=lambda k, d=None: getattr(consume, k, d))

		produce = MagicMock()
		produce.item_code = "M-ALLOY"
		produce.qty = 2.0
		produce.t_warehouse = "WH-Dept"
		produce.s_warehouse = None
		produce.uom = "Gram"
		produce.batch_no = "B-OUT"
		produce.manufacturing_operation = "MOP-1"
		produce.get = MagicMock(side_effect=lambda k, d=None: getattr(produce, k, d))

		doc.items = [consume, produce]

		stock_reservation_entry_for_mwo(doc)

		self.assertEqual(mock_avail.call_count, 1)
		mock_avail.assert_called_once_with("M-ALLOY", "WH-Dept", batch_no="B-OUT")
		mock_new_doc.assert_called_once()
		sre.append.assert_called()
		append_kw = sre.append.call_args[0][1]
		self.assertEqual(append_kw.get("batch_no"), "B-OUT")
		self.assertEqual(append_kw.get("warehouse"), "WH-Dept")
		self.assertEqual(append_kw.get("qty"), 2.0)
		self.assertEqual(sre.reservation_based_on, "Serial and Batch")
		sre.insert.assert_called_once_with(ignore_links=1)
		sre.submit.assert_called_once()

	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_sre_reserved_qty_for_voucher_detail_no",
		return_value=0.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_available_qty_to_reserve",
		return_value=0.0,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	def test_skips_when_no_reservable_qty(
		self,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		_mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			stock_reservation_entry_for_mwo,
		)

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			if doctype == "Item":
				return (0, 0)
			raise AssertionError(doctype)

		mock_cached.side_effect = _cached

		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.company = "GE"
		doc.manufacturer = None
		doc.employee_ir = None

		row = MagicMock()
		row.item_code = "X"
		row.qty = 1.0
		row.t_warehouse = "WH-Dept"
		row.uom = "Nos"
		row.batch_no = None
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		mock_new_doc.assert_not_called()

	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_sre_reserved_qty_for_voucher_detail_no",
		return_value=5.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_available_qty_to_reserve",
		return_value=0.0,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	def test_eir_injection_reserves_inbound_when_available_check_is_zero(
		self,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		_mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""Employee IR metal injection: reserve line qty even if availability reads 0 in the same pass."""
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			stock_reservation_entry_for_mwo,
		)

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			if doctype == "Item":
				return (1, 0)
			raise AssertionError(doctype)

		mock_cached.side_effect = _cached

		sre = MagicMock()
		mock_new_doc.return_value = sre

		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.company = "GE"
		doc.manufacturer = None
		doc.employee_ir = "EIR-001"

		row = MagicMock()
		row.item_code = "M-ALLOY"
		row.qty = 1.25
		row.t_warehouse = "WH-Dept"
		row.uom = "Gram"
		row.batch_no = "B-NEW"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		mock_new_doc.assert_called_once()
		self.assertEqual(sre.voucher_qty, 6.25)
		self.assertEqual(sre.reserved_qty, 1.25)
		sre.insert.assert_called_once_with(ignore_links=1)
		sre.submit.assert_called_once()

	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_sre_reserved_qty_for_voucher_detail_no",
		return_value=3.697,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_available_qty_to_reserve",
		return_value=0.0,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Material Transfer (WORK ORDER)"],
	)
	def test_eir_repack_bypasses_type_gate_when_not_in_config(
		self,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		_mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""EIR-injected Repack SE must reserve even if 'Repack' is NOT in MOP Settings.

		This is the exact scenario from the live bug: MOP Settings only has
		'Material Transfer (WORK ORDER)', but the EIR injection created a
		Repack SE with employee_ir set. The gate must be bypassed.
		"""
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			stock_reservation_entry_for_mwo,
		)

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			if doctype == "Item":
				return (1, 0)
			raise AssertionError(doctype)

		mock_cached.side_effect = _cached

		sre = MagicMock()
		mock_new_doc.return_value = sre

		# Repack SE created by EIR injection — employee_ir is set
		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.company = "GE"
		doc.manufacturer = None
		doc.employee_ir = "dipt8kitpq"  # EIR name from live data

		# Repack produce row (consume row has no t_warehouse → skipped)
		produce = MagicMock()
		produce.item_code = "M-G-18KT-75.4-Y"
		produce.qty = 6.303
		produce.t_warehouse = "Trishul WO - GEPL"
		produce.s_warehouse = None
		produce.uom = "Gram"
		produce.batch_no = "None043-MGL18754Y0-818XT"
		produce.manufacturing_operation = "MOP-26MR6"
		produce.get = MagicMock(side_effect=lambda k, d=None: getattr(produce, k, d))

		doc.items = [produce]

		stock_reservation_entry_for_mwo(doc)

		# SRE must be created despite "Repack" not being in config
		mock_new_doc.assert_called_once()
		self.assertEqual(sre.reserved_qty, 6.303)
		sre.append.assert_called()
		append_kw = sre.append.call_args[0][1]
		self.assertEqual(append_kw.get("batch_no"), "None043-MGL18754Y0-818XT")
		self.assertEqual(append_kw.get("warehouse"), "Trishul WO - GEPL")
		self.assertEqual(append_kw.get("qty"), 6.303)
		self.assertEqual(sre.reservation_based_on, "Serial and Batch")
		# voucher_qty must accommodate existing reserved + new qty
		self.assertEqual(sre.voucher_qty, 3.697 + 6.303)
		sre.insert.assert_called_once_with(ignore_links=1)
		sre.submit.assert_called_once()

	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_sre_reserved_qty_for_voucher_detail_no",
		return_value=0.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_available_qty_to_reserve",
		return_value=0.0,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Material Transfer (WORK ORDER)"],
	)
	def test_non_eir_repack_respects_config_gate(
		self,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		_mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""Non-EIR Repack SE must respect the config gate — no SRE if not in MOP Settings."""
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			stock_reservation_entry_for_mwo,
		)

		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.company = "GE"
		doc.manufacturer = None
		doc.employee_ir = None  # NOT an EIR injection

		row = MagicMock()
		row.item_code = "M-ALLOY"
		row.qty = 5.0
		row.t_warehouse = "WH-Dept"
		row.uom = "Gram"
		row.batch_no = "B-OUT"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		# Must NOT create SRE — Repack not in config, no employee_ir
		mock_new_doc.assert_not_called()
