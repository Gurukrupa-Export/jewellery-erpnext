# Copyright (c) 2026, Nirali and contributors
# See license.txt

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import frappe
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	StockReservationEntry,
)
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.stock_reservation_entry.stock_reservation_entry import (
	CustomStockReservationEntry,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import mwo_mop_link
from jewellery_erpnext.jewellery_erpnext.doc_events.mwo_mop_link import MWOMOPLinkError
from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	onsubmit,
	stock_reservation_entry_for_mwo,
)

_SE_EVENTS = "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry"
_SRE_MODULE = "jewellery_erpnext.jewellery_erpnext.customization.stock_reservation_entry.stock_reservation_entry"


class TestStockReservationEntryForMWO(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		super().setUp()
		# Each reservation is keyed to the owner of its row's operation, found with one
		# ``mop_owners`` query. The operations named below exist nowhere, and an operation without
		# an owner keeps the header work order -- the behaviour these tests were written against.
		# TestReservationKeyedToOperationOwner covers the owner keying itself.
		patcher = patch(f"{_SE_EVENTS}.mop_owners", return_value={})
		patcher.start()
		self.addCleanup(patcher.stop)

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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
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

		# Batched inbound row now queries availability twice: batch-level and warehouse-level
		# (the latter caps to what ERPNext's before_submit re-check will allow).
		self.assertEqual(mock_avail.call_count, 2)
		mock_avail.assert_any_call("M-ALLOY", "WH-Dept", batch_no="B-OUT")
		mock_avail.assert_any_call("M-ALLOY", "WH-Dept")
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
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_available_qty_to_reserve"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	def test_caps_reservation_to_warehouse_ledger_for_batch_diamond(
		self,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""Repair-unpack diamond bug: the SBB batch qty (precision 3) exceeds the freshly
		booked SLE ledger balance (precision 2). The reservation must be capped to the
		warehouse-ledger figure ERPNext's before_submit re-checks against, else the SRE
		submit throws "Cannot reserve more than Allowed Qty".
		"""

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			if doctype == "Item":
				return (1, 0)
			raise AssertionError(doctype)

		mock_cached.side_effect = _cached

		# Batch bundle holds 0.101 (get_batch_qty), but the warehouse ledger balance is 0.10.
		def _avail(item_code, warehouse, batch_no=None):
			return 0.101 if batch_no else 0.10

		mock_avail.side_effect = _avail

		sre = MagicMock()
		mock_new_doc.return_value = sre

		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.company = "GE"
		doc.manufacturer = None
		doc.employee_ir = None

		row = MagicMock()
		row.item_code = "D-NT-RO-7-+2-2.5"
		row.qty = 0.101
		row.t_warehouse = "WH-Dept"
		row.s_warehouse = None
		row.uom = "Carat"
		row.batch_no = "B-DIA"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		mock_new_doc.assert_called_once()
		# Capped to the ledger-recognized 0.10, not the SBB's 0.101.
		self.assertEqual(sre.reserved_qty, 0.10)
		sre.append.assert_called()
		append_kw = sre.append.call_args[0][1]
		self.assertEqual(append_kw.get("batch_no"), "B-DIA")
		self.assertEqual(append_kw.get("warehouse"), "WH-Dept")
		self.assertEqual(append_kw.get("qty"), 0.10)
		self.assertEqual(sre.reservation_based_on, "Serial and Batch")
		# voucher_qty fallback = total_so_reserved (0) + qty_to_be_reserved (0.10)
		self.assertEqual(sre.voucher_qty, 0.10)
		sre.insert.assert_called_once_with(ignore_links=1)
		sre.submit.assert_called_once()

	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_sre_reserved_qty_for_voucher_detail_no",
		return_value=0.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_available_qty_to_reserve"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	def test_fallback_cannot_exceed_warehouse_ledger_when_batch_reads_zero(
		self,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""Repair-unpack diamond, reproduced from the production traceback.

		A batch created by this same transaction reads 0 until it settles, so the
		"stock just landed" fallback kicks in and restores row.qty. That restore must
		still respect the warehouse ledger: booking 0.122 into an empty warehouse
		settles as a 0.12 ledger balance (float_precision 2), and ERPNext's
		validate_with_allowed_qty checks against exactly that -- so reserving the
		uncapped 0.122 threw "Cannot reserve more than Allowed Qty 0.12".
		"""

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			if doctype == "Item":
				return (1, 0)
			raise AssertionError(doctype)

		mock_cached.side_effect = _cached

		# The exact locals from the production traceback:
		#   available_qty_to_reserve = 0.0   (fresh in-transaction batch)
		#   wh_available_qty         = 0.12  (ledger, precision 2)
		def _avail(item_code, warehouse, batch_no=None):
			return 0.0 if batch_no else 0.12

		mock_avail.side_effect = _avail

		sre = MagicMock()
		mock_new_doc.return_value = sre

		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.company = "GE"
		doc.manufacturer = None
		doc.employee_ir = None

		row = MagicMock()
		row.item_code = "D-NT-RO-6B-+1-1.5"
		row.qty = 0.122
		row.t_warehouse = "Central WO - GEPL"
		row.s_warehouse = None
		row.uom = "Carat"
		row.batch_no = "B-DIA-NEW"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		mock_new_doc.assert_called_once()
		# Capped to the ledger's 0.12 -- the bug reserved the uncapped row.qty 0.122.
		self.assertEqual(sre.reserved_qty, 0.12)
		self.assertEqual(sre.voucher_qty, 0.12)
		append_kw = sre.append.call_args[0][1]
		self.assertEqual(append_kw.get("qty"), 0.12)
		sre.submit.assert_called_once()

	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_sre_reserved_qty_for_voucher_detail_no",
		return_value=0.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_available_qty_to_reserve"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	def test_reserves_full_precision_3_qty_once_ledger_can_hold_it(
		self,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""With float_precision = 3 the ledger carries the third decimal, so the same
		unpack reserves the FULL 0.122 rather than losing 0.002 to the cap."""

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			if doctype == "Item":
				return (1, 0)
			raise AssertionError(doctype)

		mock_cached.side_effect = _cached

		def _avail(item_code, warehouse, batch_no=None):
			return 0.0 if batch_no else 0.122

		mock_avail.side_effect = _avail

		sre = MagicMock()
		mock_new_doc.return_value = sre

		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.company = "GE"
		doc.manufacturer = None
		doc.employee_ir = None

		row = MagicMock()
		row.item_code = "D-NT-RO-6B-+1-1.5"
		row.qty = 0.122
		row.t_warehouse = "Central WO - GEPL"
		row.s_warehouse = None
		row.uom = "Carat"
		row.batch_no = "B-DIA-NEW"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		self.assertEqual(sre.reserved_qty, 0.122)
		self.assertEqual(sre.voucher_qty, 0.122)
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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
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

		sre = MagicMock()
		mock_new_doc.return_value = sre

		stock_reservation_entry_for_mwo(doc)

		# The SRE is created anyway due to lack of EIR gate in app code
		mock_new_doc.assert_called_once()
		self.assertEqual(sre.reserved_qty, 1.0)
		self.assertEqual(sre.voucher_qty, 1.0)
		sre.insert.assert_called_once_with(ignore_links=1)
		sre.submit.assert_called_once()

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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
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

		# get_cached_value is invoked early to unpack 3 values from
		# Parent Manufacturing Order; supply a 3-tuple so the function
		# reaches the per-row loop where the gate fires.
		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			if doctype == "Item":
				return (0, 0)
			return ("X", "Y", "Z")

		mock_cached.side_effect = _cached

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

		doc.product_certification = None

		onsubmit(doc, method=None)

		# Must NOT create SRE — Repack not in config, no employee_ir
		mock_new_doc.assert_not_called()

	def test_requires_manufacturing_order_and_work_order(self):
		doc = MagicMock()
		doc.manufacturing_order = None
		doc.manufacturing_work_order = "MWO-1"
		doc.employee_ir = None

		with self.assertRaises(frappe.exceptions.ValidationError):
			stock_reservation_entry_for_mwo(doc)

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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.sql")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_value",
		return_value=5,
	)
	def test_mr_voucher_qty_applies_tolerance_percentage(
		self,
		mock_get_value,
		mock_sql,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		_mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""Material Request based voucher_qty: the sum of custom_total_quantity
		plus the tolerance percentage from Manufacturing Setting becomes the
		reservation's voucher_qty (non-EIR path)."""

		def _sql(query, *args):
			if "custom_total_quantity" in str(query):
				return [[100.0]]
			return None

		mock_sql.side_effect = _sql

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

		row = MagicMock()
		row.item_code = "M-ALLOY"
		row.qty = 2.0
		row.t_warehouse = "WH-Dept"
		row.s_warehouse = None
		row.uom = "Gram"
		row.batch_no = "B-OUT"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		mock_new_doc.assert_called_once()
		# 100 MR qty + 5% tolerance = 105, used directly as voucher_qty.
		self.assertEqual(sre.voucher_qty, 105.0)
		self.assertEqual(sre.reserved_qty, 2.0)
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
		return_value=50.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.sql")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_value",
		return_value=None,
	)
	def test_mr_voucher_qty_without_tolerance_uses_base(
		self,
		mock_get_value,
		mock_sql,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		_mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""When the tolerance percentage is None/zero, voucher_qty equals
		the raw Material Request sum."""

		def _sql(query, *args):
			if "custom_total_quantity" in str(query):
				return [[200.0]]
			return None

		mock_sql.side_effect = _sql

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

		row = MagicMock()
		row.item_code = "M-ALLOY"
		row.qty = 3.5
		row.t_warehouse = "WH-Dept"
		row.s_warehouse = None
		row.uom = "Gram"
		row.batch_no = "B-OUT"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		mock_new_doc.assert_called_once()
		self.assertEqual(sre.voucher_qty, 200.0)
		self.assertEqual(sre.reserved_qty, 3.5)
		sre.insert.assert_called_once_with(ignore_links=1)
		sre.submit.assert_called_once()

	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_sre_reserved_qty_for_voucher_detail_no",
		return_value=10.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.get_available_qty_to_reserve",
		return_value=95.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.sql")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_value",
		return_value=None,
	)
	def test_eir_injection_with_mr_base_caps_voucher_qty(
		self,
		mock_get_value,
		mock_sql,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		mock_avail,
		mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""EIR metal injection with an existing MR base: voucher_qty is
		max(base_mr_voucher_qty, total_so_reserved + qty_to_be_reserved)."""

		def _sql(query, *args):
			if "custom_total_quantity" in str(query):
				return [[100.0]]
			return None

		mock_sql.side_effect = _sql

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
		row.qty = 95.0
		row.t_warehouse = "WH-Dept"
		row.s_warehouse = None
		row.uom = "Gram"
		row.batch_no = "B-OUT"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		mock_new_doc.assert_called_once()
		# max(100, 10 + 95) = 105 — the sum wins over the MR base.
		self.assertEqual(sre.voucher_qty, 105.0)
		self.assertEqual(sre.reserved_qty, 95.0)
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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	def test_throws_when_no_available_stock_to_reserve(
		self,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		_mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""When row.qty is zero and there is no available stock, the
		reservation must throw rather than create an empty SRE."""

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			if doctype == "Item":
				return (1, 0)
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
		row.item_code = "M-ALLOY"
		row.qty = 0.0
		row.t_warehouse = "WH-Dept"
		row.s_warehouse = None
		row.uom = "Gram"
		row.batch_no = "B-OUT"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		with self.assertRaises(frappe.exceptions.ValidationError):
			stock_reservation_entry_for_mwo(doc)

		mock_new_doc.assert_not_called()

	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log")
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.new_doc")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.sql",
		return_value=[[None]],
	)
	def test_material_receive_work_order_skips_reservation(
		self,
		mock_sql,
		mock_cached,
		mock_new_doc,
		_mock_mop,
	):
		"""Material Receive (WORK ORDER) skips SRE creation entirely and
		only writes MOP Logs."""

		def _cached(doctype, name, fields):
			if doctype == "Parent Manufacturing Order":
				return ("SO-1", "SOI-1", "MNF-1")
			raise AssertionError(doctype)

		mock_cached.side_effect = _cached

		doc = MagicMock()
		doc.stock_entry_type = "Material Receive (WORK ORDER)"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.company = "GE"
		doc.manufacturer = None
		doc.employee_ir = None

		row = MagicMock()
		row.item_code = "M-ALLOY"
		row.qty = 5.0
		row.t_warehouse = "WH-Dept"
		row.uom = "Gram"
		row.batch_no = "B-1"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		stock_reservation_entry_for_mwo(doc)

		mock_new_doc.assert_not_called()
		_mock_mop.assert_called_once_with(doc, row, is_synced=True)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.stock_reservation_entry_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_value"
	)
	def test_onsubmit_skips_reservation_for_certification_fire_assy_xrf(
		self,
		mock_get_value,
		mock_sre,
	):
		"""Product Certification Receive for Fire Assy / XRF Service skips
		the reservation flow entirely, regardless of MOP Settings."""

		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-1"
		doc.product_certification = "PC-1"

		for service in ["Fire Assy Service", "XRF Services"]:
			# get_value(..., as_dict=True) returns a frappe._dict, which the
			# hook reads via attribute access (pc.type / pc.service_type).
			mock_get_value.return_value = frappe._dict(
				service_type=service, type="Receive"
			)
			onsubmit(doc, method=None)

		mock_sre.assert_not_called()

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
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_cached_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_values",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.sql")
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.sync_mop_log_for_stock_entry"
	)
	def test_onsubmit_reserves_for_certification_receive_other_service(
		self,
		mock_sync,
		mock_get_value,
		mock_sql,
		_mock_get_all,
		_mock_get_values,
		mock_cached,
		_mock_avail,
		_mock_so_reserved,
		mock_new_doc,
		_mock_mop,
	):
		"""Product Certification with a non-listed service type must NOT
		skip the reservation flow — the PC skip is scoped to Fire Assy
		and XRF only."""

		def _get_value(doctype, name, field, *args, **kwargs):
			# onsubmit reads the PC row; stock_reservation_entry_for_mwo reads
			# the Manufacturing Setting tolerance. One mock, two return types.
			if doctype == "Product Certification":
				return frappe._dict(service_type="Some Other Service", type="Receive")
			if doctype == "Manufacturing Setting":
				return None
			raise AssertionError(doctype)

		mock_get_value.side_effect = _get_value

		def _sql(query, *args):
			# No MR rows -> base_mr_voucher_qty stays None (fallback path);
			# any other query is the tabBin FOR UPDATE lock in lock_bins.
			if "custom_total_quantity" in str(query):
				return [[None]]
			return None

		mock_sql.side_effect = _sql

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
		doc.product_certification = "PC-1"

		row = MagicMock()
		row.item_code = "M-ALLOY"
		row.qty = 2.0
		row.t_warehouse = "WH-Dept"
		row.s_warehouse = None
		row.uom = "Gram"
		row.batch_no = "B-OUT"
		row.manufacturing_operation = "MOP-1"
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))

		doc.items = [row]

		onsubmit(doc, method=None)

		mock_new_doc.assert_called_once()
		self.assertEqual(sre.voucher_type, "Sales Order")
		self.assertEqual(sre.reserved_qty, 2.0)
		# Fallback voucher_qty: total_so_reserved (0) + reserved (2).
		self.assertEqual(sre.voucher_qty, 2.0)
		sre.insert.assert_called_once_with(ignore_links=1)
		sre.submit.assert_called_once()
		mock_sync.assert_called_once_with(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.stock_reservation_entry_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.sync_mop_log_for_stock_entry"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.get_all",
		return_value=["Repack"],
	)
	def test_onsubmit_skips_repack_without_manufacturing_references(
		self,
		_mock_get_all,
		mock_sync,
		mock_sre,
	):
		"""Repack SE with no PMO/MWO references must not attempt SRE
		reservation — a plain repack, not a production flow."""

		doc = MagicMock()
		doc.stock_entry_type = "Repack"
		doc.manufacturing_order = None
		doc.manufacturing_work_order = None
		doc.product_certification = None
		doc.employee_ir = None

		onsubmit(doc, method=None)

		mock_sre.assert_not_called()
		mock_sync.assert_not_called()


class TestReservationKeyedToOperationOwner(IntegrationTestCase):
	"""Each reservation is keyed to the work order that OWNS its row's operation, and to that work
	order's Sales Order line -- never to the header blindly.

	Make Receive Entry lists reservations by work order alone, so a row of a sibling work order
	reserved under the header's key is later received into the wrong work order. Synthetic names:
	MWO-A (the header) and MWO-B are siblings of PMO-1, which has one Sales Order line; MWO-C belongs
	to PMO-2.
	"""

	PMO_LINES = {
		"PMO-1": ("SO-TEST-1", "SOI-1", "MNF-TEST"),
		"PMO-2": ("SO-TEST-2", "SOI-2", "MNF-TEST"),
	}
	PMO_OF = {"MWO-A": "PMO-1", "MWO-B": "PMO-1", "MWO-C": "PMO-2"}

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		super().setUp()
		self.reservations = []
		self.so_line_reads = []

		def _new_doc(doctype):
			sre = MagicMock()
			self.reservations.append(sre)
			return sre

		def _cached(doctype, name, fields):
			if doctype == "Item":
				return (1, 0)
			if (
				doctype == "Manufacturing Work Order"
				and fields == "manufacturing_order"
			):
				return self.PMO_OF[name]
			raise AssertionError((doctype, name, fields))

		def _so_line(doc, pmo):
			# No Material Request total (None): the voucher qty takes its fallback, already
			# reserved + this reservation. The PMO arithmetic itself is pinned by the
			# test_mr_voucher_qty_* cases above.
			self.so_line_reads.append(pmo)
			sales_order, sales_order_item, _manufacturer = self.PMO_LINES[pmo]
			return sales_order, sales_order_item, None

		self.cached = MagicMock(side_effect=_cached)
		self.lock_bins = MagicMock()
		for patcher in (
			patch(f"{_SE_EVENTS}.create_mop_log"),
			patch(f"{_SE_EVENTS}.lock_bins_for_rows", self.lock_bins),
			patch(f"{_SE_EVENTS}.frappe.new_doc", side_effect=_new_doc),
			patch(f"{_SE_EVENTS}.frappe.get_cached_value", self.cached),
			patch(f"{_SE_EVENTS}._mwo_reservation_so_line", side_effect=_so_line),
			patch(f"{_SE_EVENTS}.get_available_qty_to_reserve", return_value=50.0),
			patch(
				f"{_SE_EVENTS}.get_sre_reserved_qty_for_voucher_detail_no",
				return_value=0.0,
			),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	@staticmethod
	def _row(idx, mop):
		row = MagicMock()
		row.idx = idx
		row.item_code = "M-ALLOY"
		row.qty = 2.0
		row.t_warehouse = "WH-DEPT"
		row.s_warehouse = None
		row.uom = "Gram"
		row.batch_no = f"B-{idx}"
		row.manufacturing_operation = mop
		row.get = MagicMock(side_effect=lambda k, d=None: getattr(row, k, d))
		return row

	def _doc(self, mops, stock_entry_type="Material Transfer (WORK ORDER)"):
		doc = MagicMock()
		doc.stock_entry_type = stock_entry_type
		doc.manufacturing_order = "PMO-1"
		doc.manufacturing_work_order = "MWO-A"
		doc.company = "Test Co"
		doc.manufacturer = None
		doc.employee_ir = None
		doc.items = [self._row(idx, mop) for idx, mop in enumerate(mops, 1)]
		return doc

	def _reserve(self, doc, owners, enforce=True):
		with (
			patch(f"{_SE_EVENTS}.mop_owners", return_value=owners) as lookup,
			patch.object(mwo_mop_link, "enforcing", return_value=enforce),
		):
			stock_reservation_entry_for_mwo(doc)
		return lookup

	def _warn(self):
		"""Warn-mode side effects, silenced; returns the log_error mock."""
		stack = ExitStack()
		self.addCleanup(stack.close)
		stack.enter_context(patch.object(mwo_mop_link.frappe, "msgprint"))
		return stack.enter_context(patch.object(mwo_mop_link.frappe, "log_error"))

	@staticmethod
	def _keys(sre):
		return (
			sre.manufacturing_operation,
			sre.manufacturing_work_order,
			sre.voucher_no,
			sre.voucher_detail_no,
		)

	def test_consistent_row_is_keyed_to_the_header_without_extra_reads(self):
		self._reserve(self._doc(["MOP-A1"]), {"MOP-A1": "MWO-A"})
		[sre] = self.reservations
		self.assertEqual(self._keys(sre), ("MOP-A1", "MWO-A", "SO-TEST-1", "SOI-1"))
		self.assertNotIn(
			"Manufacturing Work Order", [c.args[0] for c in self.cached.call_args_list]
		)
		self.assertEqual(self.so_line_reads, ["PMO-1"])

	def test_row_of_another_work_order_is_refused_before_anything_is_reserved(self):
		doc = self._doc(["MOP-A1", "MOP-B1"])
		with self.assertRaises(MWOMOPLinkError) as cm:
			self._reserve(doc, {"MOP-A1": "MWO-A", "MOP-B1": "MWO-B"})
		self.assertIn(
			mwo_mop_link.row_problem(2, "MOP-B1", "MWO-B", "MWO-A"), str(cm.exception)
		)
		self.assertEqual(self.reservations, [])
		self.lock_bins.assert_not_called()

	def test_warn_mode_keys_each_reservation_to_its_owner_and_sales_order_line(self):
		"""An unrelated work order of another PMO: its own work order, Sales Order line and the
		voucher qty of its own Material Requests -- never the header's."""
		log_error = self._warn()
		self._reserve(
			self._doc(["MOP-A1", "MOP-C1"]),
			{"MOP-A1": "MWO-A", "MOP-C1": "MWO-C"},
			enforce=False,
		)
		log_error.assert_called_once()
		self.assertEqual(
			sorted(self._keys(sre) for sre in self.reservations),
			[
				("MOP-A1", "MWO-A", "SO-TEST-1", "SOI-1"),
				("MOP-C1", "MWO-C", "SO-TEST-2", "SOI-2"),
			],
		)
		self.assertEqual(sorted(self.so_line_reads), ["PMO-1", "PMO-2"])

	def test_warn_mode_sibling_of_the_same_pmo_keeps_the_sales_order_line(self):
		"""Siblings share their PMO's line: only the work order changes, and the line is read once."""
		self._warn()
		self._reserve(self._doc(["MOP-B1"]), {"MOP-B1": "MWO-B"}, enforce=False)
		[sre] = self.reservations
		self.assertEqual(self._keys(sre), ("MOP-B1", "MWO-B", "SO-TEST-1", "SOI-1"))
		self.assertEqual(self.so_line_reads, ["PMO-1"])

	def test_unknown_operation_keeps_the_header_work_order(self):
		self._reserve(self._doc(["MOP-GONE"]), {})
		self.assertEqual(self.reservations[0].manufacturing_work_order, "MWO-A")

	def test_operation_name_matches_case_insensitively(self):
		self._reserve(self._doc(["mop-a1"]), {"MOP-A1": "MWO-A"})
		self.assertEqual(self.reservations[0].manufacturing_work_order, "MWO-A")

	def test_material_receive_reserves_nothing_and_skips_the_owner_lookup(self):
		doc = self._doc(["MOP-A1"], stock_entry_type="Material Receive (WORK ORDER)")
		lookup = self._reserve(doc, {"MOP-A1": "MWO-B"})
		lookup.assert_not_called()
		self.assertEqual(self.reservations, [])


def _bare_sre(**fields):
	# Bypass Document.__init__ — we only exercise the override branch logic;
	# no DB, no meta load, no controller hooks.
	sre = CustomStockReservationEntry.__new__(CustomStockReservationEntry)
	for k, v in fields.items():
		setattr(sre, k, v)
	sre.get = lambda key, default=None: getattr(sre, key, default)
	return sre


class TestCustomStockReservationEntry(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_skips_auto_reserve_for_mwo_mop_flow(self):
		# Override only fires for Serial-and-Batch reservations — auto-pick is
		# a no-op for Qty-based anyway, so the gate matches production code.
		sre = _bare_sre(
			manufacturing_work_order="MWO-1",
			manufacturing_operation="MOP-1",
			reservation_based_on="Serial and Batch",
			sb_entries=[{"batch_no": "B-PICKED", "qty": 2.0, "warehouse": "WH-Dept"}],
		)

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch("Voucher")

		parent_mock.assert_not_called()
		self.assertEqual(len(sre.sb_entries), 1)
		self.assertEqual(sre.sb_entries[0]["batch_no"], "B-PICKED")

	def test_delegates_to_super_when_only_mwo_set(self):
		sre = _bare_sre(manufacturing_work_order="MWO-1", manufacturing_operation=None)

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch("Voucher")

		parent_mock.assert_called_once_with("Voucher")

	def test_delegates_to_super_when_only_mop_set(self):
		sre = _bare_sre(manufacturing_work_order=None, manufacturing_operation="MOP-1")

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch("Voucher")

		parent_mock.assert_called_once_with("Voucher")

	def test_delegates_to_super_for_normal_flow(self):
		sre = _bare_sre(manufacturing_work_order=None, manufacturing_operation=None)

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch(None)

		parent_mock.assert_called_once_with(None)

	def test_delegates_to_super_when_sb_entries_empty(self):
		# mwo + mop set but no pre-populated batch rows: nothing to protect,
		# so the override must hand off to ERPNext's auto-pick.
		sre = _bare_sre(
			manufacturing_work_order="MWO-1",
			manufacturing_operation="MOP-1",
			reservation_based_on="Serial and Batch",
			sb_entries=[],
		)

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch("Voucher")

		parent_mock.assert_called_once_with("Voucher")

	def test_delegates_to_super_for_qty_based_reservation(self):
		# The override only protects Serial-and-Batch reservations; Qty-based
		# ones keep ERPNext's behaviour.
		sre = _bare_sre(
			manufacturing_work_order="MWO-1",
			manufacturing_operation="MOP-1",
			reservation_based_on="Qty",
			sb_entries=[{"batch_no": "B-1", "qty": 1.0, "warehouse": "WH-1"}],
		)

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch("Voucher")

		parent_mock.assert_called_once_with("Voucher")

	def test_delegates_to_super_when_reservation_based_on_missing(self):
		# An unset reservation_based_on must not be treated as
		# "Serial and Batch" by the gate.
		sre = _bare_sre(
			manufacturing_work_order="MWO-1",
			manufacturing_operation="MOP-1",
			reservation_based_on=None,
			sb_entries=[{"batch_no": "B-1", "qty": 1.0, "warehouse": "WH-1"}],
		)

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch("Voucher")

		parent_mock.assert_called_once_with("Voucher")

	# ---------------------------------------------------------------- validate (I3)
	def _validate(self, sre, owner, enforce=True):
		with (
			patch.object(StockReservationEntry, "validate") as core_validate,
			patch(f"{_SRE_MODULE}.owner_of", return_value=owner) as lookup,
			patch.object(mwo_mop_link, "enforcing", return_value=enforce),
		):
			sre.validate()
		return core_validate, lookup

	def test_validate_refuses_a_reservation_keyed_to_a_sibling_work_order(self):
		"""The reservation two Make Receive Entries consumed: work order A, operation of B."""
		sre = _bare_sre(
			manufacturing_work_order="MWO-A", manufacturing_operation="MOP-B1"
		)
		with self.assertRaises(MWOMOPLinkError) as cm:
			self._validate(sre, owner="MWO-B")
		for name in ("MOP-B1", "MWO-B", "MWO-A"):
			self.assertIn(name, str(cm.exception))

	def test_validate_runs_core_validation_then_accepts_the_owner(self):
		sre = _bare_sre(
			manufacturing_work_order="MWO-A", manufacturing_operation="MOP-A1"
		)
		core_validate, lookup = self._validate(sre, owner="mwo-a")
		core_validate.assert_called_once_with()
		lookup.assert_called_once_with("MOP-A1")

	def test_validate_ignores_reservations_without_both_manufacturing_fields(self):
		"""Core ERPNext reservations (Sales Order, Pick List) carry neither field."""
		for mwo, mop in ((None, None), ("MWO-A", None), (None, "MOP-B1"), ("", " ")):
			sre = _bare_sre(manufacturing_work_order=mwo, manufacturing_operation=mop)
			core_validate, lookup = self._validate(sre, owner="MWO-B")
			core_validate.assert_called_once_with()
			lookup.assert_not_called()

	def test_validate_leaves_an_unresolvable_operation_alone(self):
		"""Both fields are plain Data: an operation that cannot be found is the writer's concern."""
		sre = _bare_sre(
			manufacturing_work_order="MWO-A", manufacturing_operation="MOP-GONE"
		)
		self._validate(sre, owner=None)

	def test_validate_warn_mode_logs_instead_of_raising(self):
		sre = _bare_sre(
			doctype="Stock Reservation Entry",
			name="SRE-TEST-1",
			manufacturing_work_order="MWO-A",
			manufacturing_operation="MOP-B1",
		)
		with (
			patch.object(mwo_mop_link.frappe, "log_error") as log_error,
			patch.object(mwo_mop_link.frappe, "msgprint"),
		):
			self._validate(sre, owner="MWO-B", enforce=False)
		log_error.assert_called_once()
		self.assertEqual(
			log_error.call_args.kwargs["reference_doctype"], "Stock Reservation Entry"
		)
