# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for ``validate_loss_tables_required``.

If a Receive shows loss (sum(gross_wt - received_gross_wt) > 0 rounded to 3)
but both ``employee_loss_details`` and ``manually_book_loss_details`` are
empty, submit must be rejected.
"""

from unittest.mock import MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
	validate_loss_tables_required,
)


def _eir(
	rows,
	employee_loss_details=None,
	manually_book_loss_details=None,
	type_="Receive",
	docstatus=0,
):
	doc = MagicMock()
	doc.docstatus = docstatus
	doc.type = type_
	doc.employee_ir_operations = [frappe._dict(r) for r in rows]
	doc.employee_loss_details = [frappe._dict(r) for r in (employee_loss_details or [])]
	doc.manually_book_loss_details = [
		frappe._dict(r) for r in (manually_book_loss_details or [])
	]
	return doc


class TestValidateLossTablesRequired(FrappeTestCase):
	def test_receive_loss_without_loss_tables_rejected(self):
		"""gross > received with both tables empty → throw."""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 8.5,
					"manufacturing_work_order": "MWO-1",
				}
			],
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_loss_tables_required(doc)

	def test_rounded_zero_loss_does_not_require_loss_tables(self):
		"""Loss = 0.0004 rounds to 0.000 → allowed even with empty tables."""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0004,
					"received_gross_wt": 10.0000,
					"manufacturing_work_order": "MWO-1",
				}
			],
		)
		validate_loss_tables_required(doc)  # must not throw

	def test_manual_loss_only_allowed(self):
		"""Only manually_book_loss_details populated → allowed."""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 9.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
			manually_book_loss_details=[
				{"item_code": "M-X", "proportionally_loss": 1.0}
			],
		)
		validate_loss_tables_required(doc)

	def test_employee_loss_only_allowed(self):
		"""Only employee_loss_details populated → allowed."""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 9.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
			employee_loss_details=[{"item_code": "M-X", "proportionally_loss": 1.0}],
		)
		validate_loss_tables_required(doc)

	def test_no_loss_no_tables_allowed(self):
		"""gross == received → no loss → tables not required."""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 10.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
		)
		validate_loss_tables_required(doc)

	def test_validator_runs_at_submit_docstatus_zero(self):
		"""Validator is now called from on_submit (docstatus still 0). With
		baseline > 0 and empty tables it must throw — no docstatus skip.
		"""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 8.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
			docstatus=0,
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_loss_tables_required(doc)

	def test_issue_type_skips_validation(self):
		"""type != "Receive" → validator no-ops."""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 8.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
			type_="Issue",
		)
		validate_loss_tables_required(doc)

	def test_loss_total_must_match_baseline_short_rejects(self):
		"""baseline=2 but tables sum to 1 → throw (under-allocated)."""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 8.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
			manually_book_loss_details=[
				{"item_code": "M-X", "proportionally_loss": 1.0}
			],
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_loss_tables_required(doc)

	def test_loss_total_must_match_baseline_over_rejects(self):
		"""baseline=1 but combined tables sum to 1.5 → throw (over-allocated)."""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 9.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
			employee_loss_details=[{"item_code": "M-X", "proportionally_loss": 1.0}],
			manually_book_loss_details=[
				{"item_code": "M-Y", "proportionally_loss": 0.5}
			],
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_loss_tables_required(doc)

	def test_loss_total_match_dg_carat_converted_to_grams(self):
		"""D-item proportionally_loss = 5 carats → 1.0 g; baseline = 1.0 g.
		Carat-to-gram conversion via get_loss_qty_in_grams must match."""
		doc = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 9.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
			manually_book_loss_details=[
				{"item_code": "D-BRI-VS1", "proportionally_loss": 5.0}
			],
		)
		validate_loss_tables_required(doc)  # 5 carat × 0.2 = 1.0 g == 1.0

	def test_loss_total_within_precision_tolerance(self):
		"""Sub-precision drift (< 0.0005) is allowed; >= 0.001 rejected."""
		# Within tolerance: baseline 1.000, total 1.0004 → flt(_,3) = 1.000.
		doc_ok = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 9.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
			manually_book_loss_details=[
				{"item_code": "M-X", "proportionally_loss": 1.0004}
			],
		)
		validate_loss_tables_required(doc_ok)

		# Beyond tolerance: baseline 1.000, total 1.0006 → rounds to 1.001 → throw.
		doc_bad = _eir(
			rows=[
				{
					"gross_wt": 10.0,
					"received_gross_wt": 9.0,
					"manufacturing_work_order": "MWO-1",
				}
			],
			manually_book_loss_details=[
				{"item_code": "M-X", "proportionally_loss": 1.0006}
			],
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_loss_tables_required(doc_bad)
