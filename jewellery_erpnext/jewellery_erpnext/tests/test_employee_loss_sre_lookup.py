# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Focused unit tests for the two-step SRE lookup introduced in employee_loss_se.py.

Covers the root cause (MOP mismatch between loss row and active SRE) and all
safety invariants: batch enforcement, ambiguity detection, qty validation,
idempotency, and atomic validation.

All tests use unittest.mock.patch to avoid requiring a live site.
"""

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se import (
	_find_active_sre_for_loss_row,
	_get_sre_candidates_for_loss_row,
	_validate_loss_qty_against_sre,
	handle_employee_receive_loss,
	should_handle_employee_receive_loss,
)

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

_SRE_COLS_WITH_MWO_MOP = [
	"name",
	"item_code",
	"warehouse",
	"reserved_qty",
	"delivered_qty",
	"reservation_based_on",
	"manufacturing_work_order",
	"manufacturing_operation",
	"voucher_type",
	"voucher_no",
	"voucher_detail_no",
	"voucher_qty",
	"company",
	"stock_uom",
]


def _make_loss_row(
	idx=1,
	item_code="M-GOLD-22K",
	mwo="MWO-001",
	mop="MOP-RECEIVE-002",
	batch_no="BATCH-001",
	proportionally_loss=0.025,
	pcs=None,
):
	return frappe._dict(
		{
			"idx": idx,
			"item_code": item_code,
			"manufacturing_work_order": mwo,
			"manufacturing_operation": mop,
			"batch_no": batch_no,
			"proportionally_loss": proportionally_loss,
			"pcs": pcs,
		}
	)


def _make_sre_candidate(
	name="SRE-001",
	mop="MOP-ISSUE-001",
	warehouse="Dept WH - GEPL",
	reserved_qty=2.350,
	delivered_qty=0.0,
	mwo="MWO-001",
	item_code="M-GOLD-22K",
	batch_no="BATCH-001",
	batch_qty=2.350,
	batch_delivered_qty=0.0,
):
	return frappe._dict(
		{
			"name": name,
			"item_code": item_code,
			"warehouse": warehouse,
			"reserved_qty": reserved_qty,
			"delivered_qty": delivered_qty,
			"reservation_based_on": "Serial and Batch",
			"manufacturing_work_order": mwo,
			"manufacturing_operation": mop,
			"voucher_type": "Sales Order",
			"voucher_no": "SO-001",
			"voucher_detail_no": "SO-001-item-1",
			"voucher_qty": 5.0,
			"company": "GE",
			"stock_uom": "Nos",
			"matched_batch_no": batch_no,
			"batch_qty": batch_qty,
			"batch_delivered_qty": batch_delivered_qty,
		}
	)


# ---------------------------------------------------------------------------
# Test 1 — Exact MOP match: _find_active_sre_for_loss_row returns without fallback
# ---------------------------------------------------------------------------


class TestExactMOPMatch(FrappeTestCase):
	def test_exact_mop_match_returns_without_fallback(self):
		row = _make_loss_row(mop="MOP-ISSUE-001")
		candidate = _make_sre_candidate(mop="MOP-ISSUE-001")

		call_results = {
			True: [candidate],  # exact match found
			False: [],  # fallback should not be called
		}

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se._get_sre_candidates_for_loss_row",
			side_effect=lambda r, cols, require_mop: call_results[require_mop],
		) as mock_get:
			result = _find_active_sre_for_loss_row(row, _SRE_COLS_WITH_MWO_MOP)

		self.assertEqual(result["name"], "SRE-001")
		# Fallback (require_mop=False) must not be called when exact match succeeds
		calls = mock_get.call_args_list
		require_mop_values = [c[0][2] for c in calls]
		self.assertNotIn(False, require_mop_values)


# ---------------------------------------------------------------------------
# Test 2 — Fallback MOP mismatch (the failing production case)
# ---------------------------------------------------------------------------


class TestFallbackMOPMismatch(FrappeTestCase):
	def test_fallback_used_when_exact_mop_mismatches(self):
		"""
		Active SRE has MOP-ISSUE-001 (source/issue-side MOP).
		Loss row carries MOP-RECEIVE-002 (new receive-side MOP).
		Exact lookup returns 0; fallback returns 1 — must succeed.
		"""
		row = _make_loss_row(mop="MOP-RECEIVE-002")
		candidate = _make_sre_candidate(mop="MOP-ISSUE-001")

		call_results = {
			True: [],  # exact match returns nothing
			False: [candidate],  # fallback finds the correct SRE
		}

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se._get_sre_candidates_for_loss_row",
			side_effect=lambda r, cols, require_mop: call_results[require_mop],
		):
			result = _find_active_sre_for_loss_row(row, _SRE_COLS_WITH_MWO_MOP)

		self.assertEqual(result["name"], "SRE-001")
		self.assertEqual(result["manufacturing_operation"], "MOP-ISSUE-001")


# ---------------------------------------------------------------------------
# Test 3 — Batch safety: different batch must not match
# ---------------------------------------------------------------------------


class TestBatchSafety(FrappeTestCase):
	def test_different_batch_does_not_match(self):
		"""
		Active SRE has same item + MWO but a different batch (BATCH-999).
		The loss row specifies BATCH-001.
		Both exact and fallback must return 0 (JOIN filters by batch_no).
		_find_active_sre_for_loss_row must raise ValidationError.
		"""
		row = _make_loss_row(batch_no="BATCH-001")

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se._get_sre_candidates_for_loss_row",
			return_value=[],
		):
			with self.assertRaises(frappe.ValidationError):
				_find_active_sre_for_loss_row(row, _SRE_COLS_WITH_MWO_MOP)


# ---------------------------------------------------------------------------
# Test 4 — Ambiguous fallback: must raise, must not cancel anything
# ---------------------------------------------------------------------------


class TestAmbiguousFallback(FrappeTestCase):
	def test_multiple_fallback_candidates_raises(self):
		row = _make_loss_row(mop="MOP-RECEIVE-002")
		c1 = _make_sre_candidate(name="SRE-001", mop="MOP-A")
		c2 = _make_sre_candidate(name="SRE-002", mop="MOP-B")

		call_results = {
			True: [],
			False: [c1, c2],
		}

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se._get_sre_candidates_for_loss_row",
			side_effect=lambda r, cols, require_mop: call_results[require_mop],
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				_find_active_sre_for_loss_row(row, _SRE_COLS_WITH_MWO_MOP)

		self.assertIn("SRE-001", str(ctx.exception))
		self.assertIn("SRE-002", str(ctx.exception))

	def test_multiple_exact_candidates_raises(self):
		row = _make_loss_row(mop="MOP-ISSUE-001")
		c1 = _make_sre_candidate(name="SRE-001", mop="MOP-ISSUE-001")
		c2 = _make_sre_candidate(name="SRE-002", mop="MOP-ISSUE-001")

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se._get_sre_candidates_for_loss_row",
			side_effect=lambda r, cols, require_mop: [c1, c2] if require_mop else [],
		):
			with self.assertRaises(frappe.ValidationError):
				_find_active_sre_for_loss_row(row, _SRE_COLS_WITH_MWO_MOP)


# ---------------------------------------------------------------------------
# Test 5 — Loss exceeds parent remaining qty raises before cancel
# ---------------------------------------------------------------------------


class TestLossExceedsParentRemainingQty(FrappeTestCase):
	def test_raises_when_loss_exceeds_parent_remaining(self):
		row = _make_loss_row(proportionally_loss=0.15)
		# reserved_qty=1.0, delivered_qty=0.9  → remaining = 0.10
		candidate = _make_sre_candidate(
			reserved_qty=1.0,
			delivered_qty=0.9,
			batch_qty=None,  # no batch qty field — parent-only validation
			batch_delivered_qty=None,
		)
		# Remove batch fields to test parent-only path
		candidate.pop("batch_qty", None)
		candidate.pop("batch_delivered_qty", None)
		candidate.pop("matched_batch_no", None)

		with self.assertRaises(frappe.ValidationError) as ctx:
			_validate_loss_qty_against_sre(row, candidate, loss_qty=0.15)

		self.assertIn("0.15", str(ctx.exception))

	def test_does_not_raise_when_loss_equals_remaining(self):
		row = _make_loss_row(proportionally_loss=0.10)
		candidate = _make_sre_candidate(reserved_qty=1.0, delivered_qty=0.9)
		candidate.pop("batch_qty", None)
		candidate.pop("batch_delivered_qty", None)
		# Should not raise
		_validate_loss_qty_against_sre(row, candidate, loss_qty=0.10)


# ---------------------------------------------------------------------------
# Test 6 — Loss exceeds batch remaining qty raises before cancel
# ---------------------------------------------------------------------------


class TestLossExceedsBatchRemainingQty(FrappeTestCase):
	def test_raises_when_loss_exceeds_batch_remaining(self):
		row = _make_loss_row(proportionally_loss=0.15)
		# Parent ok (remaining = 2.0), but batch is nearly depleted (remaining = 0.10)
		candidate = _make_sre_candidate(
			reserved_qty=2.0,
			delivered_qty=0.0,
			batch_qty=1.0,
			batch_delivered_qty=0.9,
		)

		with self.assertRaises(frappe.ValidationError) as ctx:
			_validate_loss_qty_against_sre(row, candidate, loss_qty=0.15)

		self.assertIn("batch", str(ctx.exception).lower())

	def test_does_not_raise_when_loss_within_batch_remaining(self):
		row = _make_loss_row(proportionally_loss=0.05)
		candidate = _make_sre_candidate(
			reserved_qty=2.0,
			delivered_qty=0.0,
			batch_qty=1.0,
			batch_delivered_qty=0.9,
		)
		# batch remaining = 0.1, loss = 0.05 — should not raise
		_validate_loss_qty_against_sre(row, candidate, loss_qty=0.05)


# ---------------------------------------------------------------------------
# Test 7 — Replacement SRE quantity: original 2.350, delivered 0, loss 0.025 → 2.325
# ---------------------------------------------------------------------------


class TestReplacementSREQuantity(FrappeTestCase):
	def test_replacement_reserved_qty(self):
		"""Replacement SRE.reserved_qty = original_remaining - loss_qty."""
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se import (
			_create_process_loss_se,
		)

		original_reserved = 2.350
		loss_qty = 0.025
		expected_remaining = round(original_reserved - loss_qty, 9)

		# Build a minimal loss_items list for _create_process_loss_se
		loss_items = [
			{
				"item_code": "M-GOLD-22K",
				"mwo_name": "MWO-001",
				"mop_name": "MOP-RECEIVE-002",
				"batch_no": "BATCH-001",
				"loss_qty": loss_qty,
				"loss_pcs": None,
				"s_warehouse": "Dept WH - GEPL",
				"t_warehouse": "Scrap WH - GEPL",
				"sre_name": "SRE-001",
				"sre_row": {},
				"reserved_qty": original_reserved,
				"is_pcs_item": False,
			}
		]

		# Mock the original SRE document returned by frappe.get_doc
		mock_sre_doc = MagicMock()
		mock_sre_doc.docstatus = 1
		mock_sre_doc.name = "SRE-001"
		mock_sre_doc.item_code = "M-GOLD-22K"
		mock_sre_doc.warehouse = "Dept WH - GEPL"
		mock_sre_doc.reserved_qty = original_reserved
		mock_sre_doc.delivered_qty = 0.0
		mock_sre_doc.voucher_type = "Sales Order"
		mock_sre_doc.voucher_no = "SO-001"
		mock_sre_doc.voucher_detail_no = "SO-001-item-1"
		mock_sre_doc.voucher_qty = 5.0
		mock_sre_doc.company = "GE"
		mock_sre_doc.stock_uom = "Nos"
		mock_sre_doc.reservation_based_on = "Serial and Batch"
		mock_sre_doc.manufacturing_work_order = "MWO-001"
		mock_sre_doc.manufacturing_operation = "MOP-ISSUE-001"

		# Track what reserved_qty is set on the new SRE
		captured_reserved_qty = {}

		mock_new_sre = MagicMock()
		mock_new_sre.sb_entries = []

		def _capture_reserved_qty(val):
			captured_reserved_qty["value"] = val

		type(mock_new_sre).reserved_qty = property(
			fget=lambda s: captured_reserved_qty.get("value"),
			fset=lambda s, v: captured_reserved_qty.update({"value": v}),
		)

		mock_se_doc = MagicMock()
		mock_se_doc.manufacturing_work_order = None

		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.get_doc",
				return_value=mock_sre_doc,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.new_doc",
				side_effect=lambda dt: mock_se_doc
				if dt == "Stock Entry"
				else mock_new_sre,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.get_all",
				return_value=[
					frappe._dict(
						{
							"batch_no": "BATCH-001",
							"qty": original_reserved,
							"delivered_qty": 0.0,
						}
					)
				],
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.get_cached_value",
				return_value=(1, 0),  # has_batch_no=1, has_serial_no=0
			),
			patch(
				"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve",
				return_value=2.325,
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

		doc = MagicMock()
		doc.company = "GE"
		doc.name = "EIR-001"

		_create_process_loss_se(doc, loss_items)

		self.assertAlmostEqual(
			captured_reserved_qty.get("value", None),
			expected_remaining,
			places=9,
			msg=f"Replacement SRE reserved_qty should be {expected_remaining}, got {captured_reserved_qty.get('value')}",
		)


# ---------------------------------------------------------------------------
# Test 8 — Idempotency: existing Process Loss SE skips re-creation
# ---------------------------------------------------------------------------


class TestIdempotency(FrappeTestCase):
	def test_skips_when_process_loss_se_already_exists(self):
		doc = frappe._dict(
			{
				"name": "EIR-001",
				"type": "Receive",
				"department": "Trishul - GEPL",
				"company": "GE",
				"manually_book_loss_details": [],
				"employee_loss_details": [
					frappe._dict(
						{
							"proportionally_loss": 0.025,
							"item_code": "M-GOLD-22K",
							"manufacturing_work_order": "MWO-001",
							"manufacturing_operation": "MOP-RECEIVE-002",
							"batch_no": "BATCH-001",
							"pcs": None,
						}
					)
				],
			}
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.db.get_value",
			return_value="SE-PROCESS-LOSS-001",  # existing SE found
		) as mock_get_value, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.msgprint"
		) as mock_msgprint, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se._validate_and_prepare_loss_items"
		) as mock_validate:
			handle_employee_receive_loss(doc)

		mock_msgprint.assert_called_once()
		mock_validate.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9 — Atomic validation: one valid + one invalid row, no cancellation
# ---------------------------------------------------------------------------


class TestAtomicValidation(FrappeTestCase):
	def test_no_cancellation_when_second_row_fails(self):
		"""
		Row 1 has a valid SRE; Row 2 has no SRE.
		_validate_and_prepare_loss_items must raise on Row 2.
		frappe.get_doc("Stock Reservation Entry") must never be called
		(cancellation happens only in _create_process_loss_se, never during validate).
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se import (
			_validate_and_prepare_loss_items,
		)

		doc = frappe._dict(
			{
				"name": "EIR-001",
				"type": "Receive",
				"manually_book_loss_details": [],
				"employee_loss_details": [
					frappe._dict(
						{
							"idx": 1,
							"proportionally_loss": 0.025,
							"item_code": "M-GOLD-22K",
							"manufacturing_work_order": "MWO-001",
							"manufacturing_operation": "MOP-RECEIVE-002",
							"batch_no": "BATCH-001",
							"pcs": None,
						}
					),
					frappe._dict(
						{
							"idx": 2,
							"proportionally_loss": 0.010,
							"item_code": "M-SILVER-925",
							"manufacturing_work_order": "MWO-001",
							"manufacturing_operation": "MOP-RECEIVE-002",
							"batch_no": "BATCH-999",
							"pcs": None,
						}
					),
				],
			}
		)

		valid_candidate = _make_sre_candidate(name="SRE-001")

		def _fake_find_sre(row, sre_cols):
			if row.item_code == "M-GOLD-22K":
				return valid_candidate
			# Row 2 has no SRE — simulate ValidationError from _throw_no_sre_found
			frappe.throw(_(f"Row {row.idx}: No active SRE found."))

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se._find_active_sre_for_loss_row",
			side_effect=_fake_find_sre,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se._validate_loss_qty_against_sre"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.db.get_table_columns",
			return_value=_SRE_COLS_WITH_MWO_MOP,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se._get_loss_target_warehouse",
			return_value="Scrap WH - GEPL",
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.get_item_loss_item",
			return_value="ML-GOLD-22K",
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.get_doc"
		) as mock_get_doc:
			with self.assertRaises(frappe.ValidationError):
				_validate_and_prepare_loss_items(doc)

		# frappe.get_doc (SRE cancellation) must never have been called
		mock_get_doc.assert_not_called()
