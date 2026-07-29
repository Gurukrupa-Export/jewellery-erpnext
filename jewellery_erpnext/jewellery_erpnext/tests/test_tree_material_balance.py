# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Pure arithmetic for the Tree Number material ledger (the T-series).

``tree_material_balance`` is the single source of truth for precision, the pending
formula, the row invariants and the status state machine. These tests pin that
arithmetic directly -- no DB, no documents -- so a regression here is unambiguous.

The invariant under test, for every row::

    issue_qty >= 0, receive_qty >= 0, loss_qty >= 0
    receive_qty + loss_qty <= issue_qty
    pending_qty == issue_qty - receive_qty - loss_qty      (UNFLOORED)
"""

from types import SimpleNamespace

from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tb,
)

ITEM_A = "M-G-18KT-75.4-Y"
ITEM_B = "M-G-22KT-91.9-Y"


def md(issue, receive, loss, item=ITEM_A, idx=1, name=None):
	"""One Tree Material Detail row with pending derived the way the server derives it."""
	row = SimpleNamespace(
		name=name or f"row{idx}",
		idx=idx,
		item_code=item,
		issue_qty=issue,
		receive_qty=receive,
		loss_qty=loss,
	)
	row.pending_qty = tb.calculate_pending(issue, receive, loss)
	return row


def tree(*rows, name="TREE-0001"):
	return SimpleNamespace(name=name, material_details=list(rows))


class TestPendingFormula(IntegrationTestCase):
	"""T01-T06, T14, T15: pending = issue - receive - loss."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_t01_all_zero(self):
		self.assertEqual(tb.calculate_pending(0, 0, 0), 0.0)

	def test_t02_issue_only(self):
		self.assertAlmostEqual(tb.calculate_pending(2.900, 0, 0), 2.900, places=3)

	def test_t03_partial_receive(self):
		self.assertAlmostEqual(tb.calculate_pending(2.900, 2.390, 0), 0.510, places=3)

	def test_t04_full_receive(self):
		self.assertEqual(tb.calculate_pending(2.900, 2.900, 0), 0.0)

	def test_t05_receive_plus_loss_closes(self):
		self.assertEqual(tb.calculate_pending(2.900, 2.800, 0.100), 0.0)

	def test_t06_partial_receive_plus_partial_loss(self):
		self.assertAlmostEqual(
			tb.calculate_pending(2.900, 2.300, 0.100), 0.500, places=3
		)

	def test_float_dust_collapses_to_zero(self):
		# 3 - 2.9 - 0.1 is ~8e-17 in raw float; rounding to the ledger precision must land on 0.
		self.assertEqual(tb.calculate_pending(3, 2.9, 0.1), 0.0)

	def test_t14_precision_boundary(self):
		# One unit at the configured precision must survive; anything below the rounding
		# boundary must round away.
		#
		# Deliberately NOT asserted at exactly half a unit. That is a TIE, and which way a tie
		# breaks is System Settings > Rounding Method — site configuration, not a property of
		# calculate_pending, which simply delegates to flt(). Only "Banker's Rounding" sends
		# 0.0005 down to 0. Frappe's default, "Banker's Rounding (legacy)", applies its
		# banker tie-break ONLY at precision 0 (`if not precision and decimal_part == 0.5`)
		# and rounds a tie up at precision 3, exactly like "Commercial Rounding". So the tie
		# assertion passed on a dev site set to "Banker's Rounding" and failed on CI's
		# freshly created test_site, which carries the default — 0.001 != 0.0.
		prec = tb.qty_precision()
		unit = 10**-prec
		self.assertAlmostEqual(tb.calculate_pending(unit, 0, 0), unit, places=prec)
		# A quarter unit is unambiguously below the boundary under all three methods.
		self.assertEqual(tb.calculate_pending(unit / 4, 0, 0), 0.0)

	def test_t15_negative_zero_normalised(self):
		result = tb.calculate_pending(0.0, 0.0, 0.0)
		self.assertEqual(result, 0.0)
		# -0.0 == 0.0 compares equal, so assert the sign bit explicitly.
		self.assertNotEqual(str(result), "-0.0")

	def test_negative_zero_from_subtraction_is_normalised(self):
		result = tb.calculate_pending(0.0, 0.0, 0.0000001)
		self.assertNotEqual(str(result), "-0.0")

	def test_pending_is_unfloored(self):
		# An over-drawn row must stay visibly negative, not be clamped to zero.
		self.assertAlmostEqual(tb.calculate_pending(0, 2.36, 0), -2.360, places=3)

	def tearDown(self):
		return super().tearDown()


class TestAvailableToDraw(IntegrationTestCase):
	"""available_to_draw floors at zero even though pending does not."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_available_matches_pending_when_positive(self):
		self.assertAlmostEqual(
			tb.available_to_draw(md(2.900, 2.390, 0)), 0.510, places=3
		)

	def test_available_floors_a_negative_pending(self):
		# 155 legacy rows store a negative pending; capacity must read 0, never a negative
		# that a `draw - available` comparison would turn into phantom headroom.
		self.assertEqual(tb.available_to_draw(md(0, 37000.0, 0)), 0.0)

	def tearDown(self):
		return super().tearDown()


class TestRowInvariants(IntegrationTestCase):
	"""T07-T13: what validate_row_balance rejects."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_t07_receive_without_issue_throws(self):
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(md(0, 2.390, 0)))

	def test_t08_loss_without_issue_throws(self):
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(md(0, 0, 0.100)))

	def test_t09_over_receive_throws(self):
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(md(2.900, 3.000, 0)))

	def test_t10_receive_plus_loss_exceeds_issue_throws(self):
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(md(2.900, 2.850, 0.100)))

	def test_t11_negative_issue_throws(self):
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(md(-1.0, 0, 0)))

	def test_t12_negative_receive_throws(self):
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(md(5.0, -1.0, 0)))

	def test_t13_negative_loss_throws(self):
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(md(5.0, 0, -1.0)))

	def test_balanced_rows_pass(self):
		tb.validate_row_balance(
			tree(md(2.900, 2.390, 0), md(5.0, 4.0, 1.0, item=ITEM_B, idx=2))
		)

	def test_exactly_closed_row_passes(self):
		tb.validate_row_balance(tree(md(2.900, 2.800, 0.100)))

	def test_duplicate_item_rows_throw(self):
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(md(5.0, 0, 0), md(3.0, 0, 0, idx=2)))

	def tearDown(self):
		return super().tearDown()


class TestNonWorseningRule(IntegrationTestCase):
	"""A historically over-drawn row must stay saveable, but must never get worse.

	Without this the 154 legacy rows would be unsavable, which would in turn block
	submit_tree, reverse_tree_stock_entries and every Employee IR cancel touching them."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_existing_violation_may_be_saved_unchanged(self):
		row = md(0, 2.360, 0, name="legacy")
		tb.validate_row_balance(
			tree(row), previous_violations={"legacy": tb.row_violation(row)}
		)

	def test_existing_violation_may_be_reduced(self):
		row = md(0, 1.000, 0, name="legacy")
		tb.validate_row_balance(tree(row), previous_violations={"legacy": 2.360})

	def test_existing_violation_may_not_be_worsened(self):
		row = md(0, 3.000, 0, name="legacy")
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(row), previous_violations={"legacy": 2.360})

	def test_new_violation_is_always_rejected(self):
		with self.assertRaises(ValidationError):
			tb.validate_row_balance(tree(md(0, 2.360, 0, name="fresh")))

	def tearDown(self):
		return super().tearDown()


class TestStatusMachine(IntegrationTestCase):
	"""T16-T18 plus the state machine as a whole."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_no_rows_is_draft(self):
		# Bare Main Slip-created trees carry no ledger.
		self.assertEqual(tb.tree_status(tree()), tb.STATUS_DRAFT)

	def test_t18_all_rows_zero_is_draft_never_received(self):
		status = tb.tree_status(tree(md(0, 0, 0), md(0, 0, 0, item=ITEM_B, idx=2)))
		self.assertEqual(status, tb.STATUS_DRAFT)
		self.assertNotEqual(status, tb.STATUS_RECEIVED)

	def test_issued(self):
		self.assertEqual(tb.tree_status(tree(md(2.900, 0, 0))), tb.STATUS_ISSUED)

	def test_partially_received(self):
		self.assertEqual(
			tb.tree_status(tree(md(2.900, 2.390, 0))), tb.STATUS_PARTIALLY_RECEIVED
		)

	def test_received(self):
		self.assertEqual(tb.tree_status(tree(md(2.900, 2.900, 0))), tb.STATUS_RECEIVED)

	def test_received_via_receive_plus_loss(self):
		self.assertEqual(
			tb.tree_status(tree(md(2.900, 2.800, 0.100))), tb.STATUS_RECEIVED
		)

	def test_t16_multiple_rows_all_complete_is_received(self):
		status = tb.tree_status(
			tree(md(2.900, 2.900, 0), md(5.0, 4.0, 1.0, item=ITEM_B, idx=2))
		)
		self.assertEqual(status, tb.STATUS_RECEIVED)

	def test_t17_multiple_rows_one_pending_is_partially_received(self):
		status = tb.tree_status(
			tree(md(2.900, 2.900, 0), md(5.0, 2.0, 0, item=ITEM_B, idx=2))
		)
		self.assertEqual(status, tb.STATUS_PARTIALLY_RECEIVED)

	def test_never_issued_row_blocks_received(self):
		# An untouched seed row (e.g. an unreceived multicolour colour) still owes metal.
		status = tb.tree_status(
			tree(md(2.900, 2.900, 0), md(0, 0, 0, item=ITEM_B, idx=2))
		)
		self.assertEqual(status, tb.STATUS_PARTIALLY_RECEIVED)

	def test_receive_without_issue_is_never_received(self):
		# The GEPL-TR-26-00154 shape. Nothing was issued, so nothing can be "Received".
		status = tb.tree_status(tree(md(0, 2.360, 0)))
		self.assertNotEqual(status, tb.STATUS_RECEIVED)
		self.assertEqual(status, tb.STATUS_PARTIALLY_RECEIVED)

	def test_status_never_returns_submitted(self):
		# "Submitted" is terminal and set only by TreeNumber.submit_tree.
		for rows in ([md(0, 0, 0)], [md(5, 0, 0)], [md(5, 5, 0)], [md(5, 2, 0)]):
			self.assertNotEqual(tb.tree_status(tree(*rows)), tb.STATUS_SUBMITTED)

	def test_float_dust_reads_as_received(self):
		self.assertEqual(tb.tree_status(tree(md(3, 2.9, 0.1))), tb.STATUS_RECEIVED)

	def tearDown(self):
		return super().tearDown()


class TestPrecisionSource(IntegrationTestCase):
	"""Precision comes from the live DocField, never a hardcoded 3."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_precision_matches_stock_entry_detail(self):
		import frappe

		self.assertEqual(
			tb.qty_precision(),
			frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3,
		)

	def test_eps_is_half_a_unit(self):
		self.assertAlmostEqual(tb.pending_eps(), (10 ** -tb.qty_precision()) / 2)

	def tearDown(self):
		return super().tearDown()
