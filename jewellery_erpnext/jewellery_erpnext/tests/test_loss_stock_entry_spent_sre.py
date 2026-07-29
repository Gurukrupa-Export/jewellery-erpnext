# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit tests for spent-reservation handling in the Employee IR Process Loss path.

The bug (EMP-IR-Labh-2026-00257): a Product Certification consumed the WIP reservations of
every MWO on the same Sales Order via ``consume_stock_reservation_entry``, which sets
``delivered_qty = reserved_qty`` and leaves ``docstatus = 1``. ``_find_sre`` filtered only on
``docstatus``, so it picked one of those spent SREs; ``_reduce_sre`` then cancelled it and
recreated it with ``frappe.copy_doc`` -- which keeps ``no_copy`` fields, so ``delivered_qty``
rode along while ``reserved_qty`` dropped, and ERPNext's ``validate_with_allowed_qty`` threw
"Reserved Qty should be greater than Delivered Qty", aborting the whole Employee IR submit.

A spent SRE nets to zero in ERPNext's Bin formula (reserved - delivered - transferred -
consumed), so it blocks nothing and has nothing left to release: the correct behaviour is to
skip the reduction and book the loss straight against physical stock.

Mocked/pure-logic style (see test_loss_row_ownership.py): SimpleNamespace fakes, no DB.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	loss_stock_entry as lse,
)

_LSE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry"


def _sre_row(name, warehouse, reserved, delivered=0.0, mop=None, **extra):
	row = {
		"name": name,
		"warehouse": warehouse,
		"reserved_qty": reserved,
		"delivered_qty": delivered,
		"transferred_qty": 0.0,
		"consumed_qty": 0.0,
		"available_qty": reserved,
		"voucher_qty": reserved,
		"reservation_based_on": "Serial and Batch",
		"manufacturing_operation": mop,
	}
	row.update(extra)
	return row


def _loss_row(**fields):
	base = {
		"idx": 1,
		"item_code": "M-G-22KT-91.9-Y",
		"batch_no": "BATCH-A",
		"manufacturing_operation": "MOP-CURRENT",
		"proportionally_loss": 0.143,
	}
	base.update(fields)
	return SimpleNamespace(**base)


def _sb(batch_no, qty, delivered_qty=0.0):
	return SimpleNamespace(
		batch_no=batch_no, qty=qty, delivered_qty=delivered_qty, idx=1
	)


class TestSreRemaining(IntegrationTestCase):
	"""_sre_remaining mirrors ERPNext's Bin formula for dicts and Documents alike."""

	def test_active_reservation_reports_remaining(self):
		self.assertEqual(
			lse._sre_remaining(_sre_row("A", "WH", 3.4, delivered=1.0)), 2.4
		)

	def test_fully_delivered_reports_zero(self):
		self.assertEqual(
			lse._sre_remaining(_sre_row("A", "WH", 3.396, delivered=3.396)), 0.0
		)

	def test_counts_transferred_and_consumed(self):
		row = _sre_row("A", "WH", 5.0)
		row["transferred_qty"] = 2.0
		row["consumed_qty"] = 1.0
		self.assertEqual(lse._sre_remaining(row), 2.0)

	def test_accepts_a_document_not_just_a_dict(self):
		doc = SimpleNamespace(
			reserved_qty=3.0, delivered_qty=0.5, transferred_qty=0.0, consumed_qty=0.0
		)
		self.assertEqual(lse._sre_remaining(doc), 2.5)


class TestFindSrePrefersActive(IntegrationTestCase):
	"""_find_sre must never pick a spent reservation while an active one exists."""

	def test_prefers_active_sre_over_delivered_sibling(self):
		rows = [
			_sre_row("SPENT", "Waxing WO", 3.396, delivered=3.396, mop="MOP-CURRENT"),
			_sre_row("ACTIVE", "Waxing WO", 3.400, delivered=0.0, mop="MOP-OTHER"),
		]
		got = MagicMock(
			return_value=SimpleNamespace(name="ACTIVE", warehouse="Waxing WO")
		)
		with (
			patch(f"{_LSE}._query_batch_and_qty_sres", return_value=rows),
			patch("frappe.get_doc", got),
		):
			doc, candidates = lse._find_sre(
				SimpleNamespace(name="EIR-1", company="C"),
				_loss_row(),
				"MWO-1",
				"employee_loss_details",
				0.143,
			)

		# Op-match would have chosen SPENT; remaining-awareness must reject it.
		got.assert_called_once_with("Stock Reservation Entry", "ACTIVE")
		self.assertEqual([c["name"] for c in candidates], ["ACTIVE"])

	def test_covering_test_uses_remaining_not_gross(self):
		"""A large-but-mostly-delivered SRE must not be treated as covering."""
		rows = [
			_sre_row("BIG", "WH", 5.0, delivered=4.95),  # remaining 0.05
			_sre_row("SMALL", "WH", 0.5, delivered=0.0),  # remaining 0.5
		]
		got = MagicMock(return_value=SimpleNamespace(name="SMALL", warehouse="WH"))
		with (
			patch(f"{_LSE}._query_batch_and_qty_sres", return_value=rows),
			patch("frappe.get_doc", got),
		):
			lse._find_sre(
				SimpleNamespace(name="EIR-1", company="C"),
				_loss_row(manufacturing_operation=None),
				"MWO-1",
				"employee_loss_details",
				0.2,
			)

		got.assert_called_once_with("Stock Reservation Entry", "SMALL")


class TestFindSreAllSpent(IntegrationTestCase):
	"""When every reservation is spent, the warehouse must come from physical stock."""

	def test_picks_warehouse_that_physically_holds_the_batch(self):
		rows = [
			# Largest reserved_qty, but the metal has left this warehouse.
			_sre_row("STALE", "Tagging Transit", 3.5, delivered=3.5),
			_sre_row("SPENT", "Waxing WO", 3.396, delivered=3.396),
		]
		physical = {"Tagging Transit": 0.0, "Waxing WO": 568.557}
		got = MagicMock(
			return_value=SimpleNamespace(name="SPENT", warehouse="Waxing WO")
		)
		with (
			patch(f"{_LSE}._query_batch_and_qty_sres", return_value=rows),
			patch(
				f"{_LSE}._physical_batch_qty", side_effect=lambda i, b, w: physical[w]
			),
			patch("frappe.get_doc", got),
		):
			doc, candidates = lse._find_sre(
				SimpleNamespace(name="EIR-1", company="C"),
				_loss_row(),
				"MWO-1",
				"employee_loss_details",
				0.143,
			)

		got.assert_called_once_with("Stock Reservation Entry", "SPENT")
		self.assertEqual([c["name"] for c in candidates], ["SPENT"])

	def test_throws_when_no_warehouse_physically_covers(self):
		rows = [_sre_row("SPENT", "Waxing WO", 3.396, delivered=3.396)]
		with (
			patch(f"{_LSE}._query_batch_and_qty_sres", return_value=rows),
			patch(f"{_LSE}._physical_batch_qty", return_value=0.0),
			patch(
				f"{_LSE}._warehouses_with_physical_batch",
				return_value=[("Final Polish RM", 2.5)],
			),
			self.assertRaises(frappe.ValidationError) as ctx,
		):
			lse._find_sre(
				SimpleNamespace(name="EIR-1", company="C"),
				_loss_row(),
				"MWO-1",
				"employee_loss_details",
				0.143,
			)

		msg = str(ctx.exception)
		self.assertIn("already fully consumed upstream", msg)
		self.assertIn("Final Polish RM", msg)

	def test_does_not_self_heal_a_spent_reservation(self):
		"""_reserve_batch_at_physical_warehouse is for ORPHANS, not released stock."""
		rows = [_sre_row("SPENT", "Waxing WO", 3.396, delivered=3.396)]
		heal = MagicMock()
		with (
			patch(f"{_LSE}._query_batch_and_qty_sres", return_value=rows),
			patch(f"{_LSE}._physical_batch_qty", return_value=500.0),
			patch(
				"frappe.get_doc",
				return_value=SimpleNamespace(name="SPENT", warehouse="Waxing WO"),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync."
				"_reserve_batch_at_physical_warehouse",
				heal,
			),
		):
			lse._find_sre(
				SimpleNamespace(name="EIR-1", company="C"),
				_loss_row(),
				"MWO-1",
				"employee_loss_details",
				0.143,
			)

		heal.assert_not_called()


class TestValidateSreQty(IntegrationTestCase):
	"""_validate_sre_qty gates on remaining reservation, or on physical stock when spent."""

	def _eir(self):
		return SimpleNamespace(name="EIR-1")

	def test_rejects_loss_exceeding_remaining_even_when_gross_covers(self):
		sre = SimpleNamespace(
			name="SRE-1",
			warehouse="WH",
			reserved_qty=5.0,
			delivered_qty=4.95,
			transferred_qty=0.0,
			consumed_qty=0.0,
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			lse._validate_sre_qty(
				self._eir(),
				_loss_row(),
				sre,
				[_sre_row("SRE-1", "WH", 5.0, 4.95)],
				0.2,
				"employee_loss_details",
			)
		self.assertIn("cannot be covered", str(ctx.exception))

	def test_spent_sre_passes_when_physical_stock_covers(self):
		sre = SimpleNamespace(
			name="SRE-1",
			warehouse="Waxing WO",
			reserved_qty=3.396,
			delivered_qty=3.396,
			transferred_qty=0.0,
			consumed_qty=0.0,
		)
		with patch(f"{_LSE}._physical_batch_qty", return_value=568.557):
			lse._validate_sre_qty(
				self._eir(), _loss_row(), sre, [], 0.143, "employee_loss_details"
			)  # must not raise

	def test_spent_sre_throws_when_physical_stock_short(self):
		sre = SimpleNamespace(
			name="SRE-1",
			warehouse="Waxing WO",
			reserved_qty=3.396,
			delivered_qty=3.396,
			transferred_qty=0.0,
			consumed_qty=0.0,
		)
		with (
			patch(f"{_LSE}._physical_batch_qty", return_value=0.05),
			self.assertRaises(frappe.ValidationError) as ctx,
		):
			lse._validate_sre_qty(
				self._eir(), _loss_row(), sre, [], 0.143, "employee_loss_details"
			)
		self.assertIn("exceeds the physical stock", str(ctx.exception))


class TestReduceSreSkipsSpent(IntegrationTestCase):
	"""The regression guard: never cancel/recreate a reservation that holds nothing."""

	def test_no_op_for_fully_delivered_sre(self):
		sre = MagicMock()
		sre.reserved_qty = 3.396
		sre.delivered_qty = 3.396
		sre.transferred_qty = 0.0
		sre.consumed_qty = 0.0

		lse._reduce_sre(
			SimpleNamespace(name="EIR-1"),
			_loss_row(),
			sre,
			0.143,
			"employee_loss_details",
		)

		sre.cancel.assert_not_called()

	def test_create_loss_stock_entries_skips_reduction_when_flagged(self):
		"""End-to-end wiring: a pending entry marked spent never reaches _reduce_sre."""
		eir = SimpleNamespace(
			name="EIR-1",
			employee_loss_details=[_loss_row()],
			manually_book_loss_details=[],
		)
		spent = {
			"row": _loss_row(),
			"table_name": "employee_loss_details",
			"qty": 0.143,
			"sre_doc": SimpleNamespace(warehouse="Waxing WO"),
			"needs_sre_reduction": False,
		}
		reduce_mock = MagicMock()
		db = MagicMock()
		db.exists.return_value = False
		with (
			patch("frappe.db", db),
			patch(f"{_LSE}._prepare_loss_row", side_effect=[spent]),
			patch(f"{_LSE}._reduce_sre", reduce_mock),
			patch(f"{_LSE}._build_combined_loss_se", return_value=MagicMock()),
		):
			lse.create_loss_stock_entries(eir)

		reduce_mock.assert_not_called()


class TestReduceSreBatchHandling(IntegrationTestCase):
	"""_reduce_sre must decrement only the affected batch and reset the no-copy counters."""

	def _sre(self, sb_entries, reserved):
		return SimpleNamespace(
			name="SRE-1",
			warehouse="WH",
			reserved_qty=reserved,
			delivered_qty=0.0,
			transferred_qty=0.0,
			consumed_qty=0.0,
			available_qty=reserved,
			voucher_qty=reserved,
			voucher_type="Material Request",
			voucher_no="MR-1",
			voucher_detail_no="MRI-1",
			reservation_based_on="Serial and Batch",
			sb_entries=sb_entries,
			cancel=MagicMock(),
		)

	def test_decrements_only_the_matching_batch_row(self):
		sre = self._sre([_sb("BATCH-A", 3.0), _sb("BATCH-B", 2.0)], 5.0)
		clone = SimpleNamespace(
			sb_entries=[_sb("BATCH-A", 3.0), _sb("BATCH-B", 2.0)],
			flags=SimpleNamespace(ignore_permissions=False),
			insert=MagicMock(),
			submit=MagicMock(),
		)
		with patch("frappe.copy_doc", return_value=clone):
			lse._reduce_sre(
				SimpleNamespace(name="EIR-1"),
				_loss_row(batch_no="BATCH-A"),
				sre,
				0.5,
				"employee_loss_details",
			)

		self.assertEqual(
			[(s.batch_no, s.qty) for s in clone.sb_entries],
			[("BATCH-A", 2.5), ("BATCH-B", 2.0)],
		)
		# ERPNext recomputes reserved_qty = sum(sb_entries.qty) on submit; agree with it.
		self.assertEqual(clone.reserved_qty, 4.5)
		# no_copy counters must not ride along onto a brand-new reservation.
		self.assertEqual(clone.delivered_qty, 0)
		self.assertEqual(clone.transferred_qty, 0)
		self.assertEqual(clone.consumed_qty, 0)
		clone.submit.assert_called_once()

	def test_throws_when_the_sre_does_not_reserve_this_batch(self):
		"""Silently no-op'ing would let ERPNext reset reserved_qty and lose the reduction."""
		sre = self._sre([_sb("BATCH-B", 2.0)], 2.0)
		with self.assertRaises(frappe.ValidationError) as ctx:
			lse._reduce_sre(
				SimpleNamespace(name="EIR-1"),
				_loss_row(batch_no="BATCH-A"),
				sre,
				0.5,
				"employee_loss_details",
			)
		self.assertIn("BATCH-A", str(ctx.exception))
		sre.cancel.assert_not_called()

	def test_partially_delivered_sre_is_rebased_on_remaining_not_gross(self):
		"""reserved 5 / delivered 4 nets to 1 reserved; the replacement must not re-reserve 4."""
		sre = self._sre([_sb("BATCH-A", 5.0, delivered_qty=4.0)], 5.0)
		sre.delivered_qty = 4.0
		clone = SimpleNamespace(
			sb_entries=[_sb("BATCH-A", 5.0, delivered_qty=4.0)],
			flags=SimpleNamespace(ignore_permissions=False),
			insert=MagicMock(),
			submit=MagicMock(),
		)
		with patch("frappe.copy_doc", return_value=clone):
			lse._reduce_sre(
				SimpleNamespace(name="EIR-1"),
				_loss_row(batch_no="BATCH-A"),
				sre,
				0.5,
				"employee_loss_details",
			)

		# remaining 1.0 - loss 0.5 = 0.5, and delivered restarts at 0.
		self.assertEqual(clone.reserved_qty, 0.5)
		self.assertEqual(clone.sb_entries[0].qty, 0.5)
		self.assertEqual(clone.sb_entries[0].delivered_qty, 0)
		self.assertEqual(clone.delivered_qty, 0)

	def test_drops_the_batch_row_when_fully_consumed(self):
		sre = self._sre([_sb("BATCH-A", 0.5), _sb("BATCH-B", 2.0)], 2.5)
		clone = SimpleNamespace(
			sb_entries=[_sb("BATCH-A", 0.5), _sb("BATCH-B", 2.0)],
			flags=SimpleNamespace(ignore_permissions=False),
			insert=MagicMock(),
			submit=MagicMock(),
		)
		with patch("frappe.copy_doc", return_value=clone):
			lse._reduce_sre(
				SimpleNamespace(name="EIR-1"),
				_loss_row(batch_no="BATCH-A"),
				sre,
				0.5,
				"employee_loss_details",
			)

		self.assertEqual([s.batch_no for s in clone.sb_entries], ["BATCH-B"])
		self.assertEqual(clone.reserved_qty, 2.0)
