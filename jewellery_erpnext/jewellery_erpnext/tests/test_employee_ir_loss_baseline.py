# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for Employee IR loss baseline + precision-3 reconciliation.

Covers:
- validate_process_loss: ``mop_loss_details_total`` is the pre-deduction MOP
  baseline (sum of gross_wt - received_gross_wt across employee_ir_operations),
  not the post-deduction auto-distributed total.
- book_metal_loss: independently rounded proportional rows are reconciled so
  that sum(employee_loss_details.proportionally_loss) == flt(loss, 3) exactly.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


class _StubEIR:
	"""Minimal stand-in that supports the pieces validate_process_loss touches.

	Avoids the full Document machinery so the baseline calculation can be
	verified in isolation without a site-level fixture.
	"""

	def __init__(self, ops, book_metal_loss_returns=None):
		self.docstatus = 0
		self.type = "Receive"
		self.company = "GE"
		self.department = "Trishul - GEPL"
		self.employee_ir_operations = ops
		self.employee_loss_details = []
		self.manually_book_loss_details = []
		self.mop_loss_details_total = 0
		self._bml_returns = book_metal_loss_returns or []

	def append(self, table, row):
		assert table == "employee_loss_details"
		self.employee_loss_details.append(frappe._dict(row))

	def book_metal_loss(self, *args, **kwargs):
		# `validate_process_loss` invokes `self.book_metal_loss(...)`; bind it
		# to a stub return so the baseline calculation under test runs in
		# isolation from the proportional-distribution algorithm.
		return self._bml_returns


class TestMopLossDetailsTotalBaseline(FrappeTestCase):
	"""mop_loss_details_total reflects the MOP baseline available for loss,
	independent of how that loss is later split between auto and manual rows.
	"""

	def _run(self, ops, book_metal_loss_returns=None):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			EmployeeIR,
		)

		stub = _StubEIR(ops, book_metal_loss_returns=book_metal_loss_returns)

		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.get_cached_value",
				return_value=None,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.db.get_value",
				return_value=None,
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

		EmployeeIR.validate_process_loss(stub)
		return stub

	def test_baseline_is_sum_of_gwt_minus_rgwt(self):
		ops = [
			frappe._dict(
				{
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
					"gross_wt": 4.6528,
					"received_gross_wt": 4.0,
				}
			),
			frappe._dict(
				{
					"manufacturing_work_order": "MWO-2",
					"manufacturing_operation": "MOP-2",
					"gross_wt": 5.0,
					"received_gross_wt": 4.5,
				}
			),
		]
		stub = self._run(ops)
		self.assertAlmostEqual(stub.mop_loss_details_total, 1.153, places=3)

	def test_baseline_ignores_gain_rows(self):
		"""Rows where received >= gross don't contribute to the loss baseline."""
		ops = [
			frappe._dict(
				{
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
					"gross_wt": 4.0,
					"received_gross_wt": 4.5,
				}
			),
			frappe._dict(
				{
					"manufacturing_work_order": "MWO-2",
					"manufacturing_operation": "MOP-2",
					"gross_wt": 5.0,
					"received_gross_wt": 4.5,
				}
			),
		]
		stub = self._run(ops)
		self.assertAlmostEqual(stub.mop_loss_details_total, 0.5, places=3)

	def test_baseline_independent_of_manual_loss(self):
		"""Adding manual loss rows must NOT shrink mop_loss_details_total —
		that's the whole point of the pre-deduction semantic.
		"""
		ops = [
			frappe._dict(
				{
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
					"gross_wt": 10.0,
					"received_gross_wt": 7.0,
				}
			),
		]
		stub = self._run(ops)
		stub.manually_book_loss_details = [
			frappe._dict(
				{"proportionally_loss": 1.0, "manufacturing_work_order": "MWO-1"}
			)
		]
		# Re-running validate_process_loss with a manual row present must not
		# change the baseline; only the auto-distributed total would shrink.
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			EmployeeIR,
		)

		EmployeeIR.validate_process_loss(stub)
		self.assertAlmostEqual(stub.mop_loss_details_total, 3.0, places=3)


class TestBookMetalLossPrecisionResidual(FrappeTestCase):
	"""Independently-rounded rows must reconcile to flt(loss, 3) exactly."""

	def _run(self, mop_log_rows, gwt, r_gwt, manual_loss_rows=None):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			EmployeeIR,
		)

		doc = MagicMock()
		doc.manually_book_loss_details = manual_loss_rows or []

		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.db.get_all",
				return_value=mop_log_rows,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.db.get_value",
				return_value=frappe._dict(
					{
						"metal_type": "Gold",
						"metal_touch": "22KT",
						"metal_purity": "91.9",
						"master_bom": "BOM-X",
						"is_finding_mwo": 0,
					}
				),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.get_item_from_attribute_full",
				return_value=frappe._dict({"name": "M-G-22KT-91.9-Y"}),
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

		return EmployeeIR.book_metal_loss(
			doc,
			mwo="MWO-1",
			opt="MOP-1",
			gwt=gwt,
			r_gwt=r_gwt,
			allowed_loss_percentage=None,
		)

	def test_three_equal_rows_residual_anchored(self):
		"""3 rows × 1.0g, loss 1.0g => each rounds to 0.333 (sum 0.999).
		The residual 0.001 must be added to one row so the sum is 1.000.
		"""
		rows = [
			frappe._dict(
				{
					"item_code": "M-G-22KT-91.9-Y",
					"batch_no": f"B{i}",
					"qty": 1.0,
					"pcs": 0,
				}
			)
			for i in range(3)
		]
		result = self._run(rows, gwt=4.0, r_gwt=3.0)
		total = sum(flt(e["proportionally_loss"], 3) for e in result)
		self.assertAlmostEqual(total, 1.000, places=3)
		# Each row is rounded to 3 dp; one carries the residual.
		for entry in result:
			self.assertEqual(
				entry["proportionally_loss"], flt(entry["proportionally_loss"], 3)
			)

	def test_sum_matches_loss_after_manual_deduction(self):
		"""Manual loss reduces the auto baseline; the auto rows still
		reconcile exactly to (gwt - r_gwt - manual) at precision 3.
		"""
		rows = [
			frappe._dict(
				{"item_code": "M-G-22KT-91.9-Y", "batch_no": "B1", "qty": 1.7, "pcs": 0}
			),
			frappe._dict(
				{"item_code": "F-G-18KT-75.4-Y", "batch_no": "B2", "qty": 1.3, "pcs": 0}
			),
		]
		manual = [
			frappe._dict(
				{
					"manufacturing_work_order": "MWO-1",
					"proportionally_loss": 0.5,
					"stock_uom": "Gram",
				}
			)
		]
		# gwt 5.0 - r_gwt 4.0 - manual 0.5 => loss 0.5 distributed across rows.
		result = self._run(rows, gwt=5.0, r_gwt=4.0, manual_loss_rows=manual)
		total = sum(flt(e["proportionally_loss"], 3) for e in result)
		self.assertAlmostEqual(total, 0.500, places=3)


# Test imports use the same `flt` ERPNext does so the rounding semantics
# match between code-under-test and assertions.
from frappe.utils import flt  # noqa: E402
