# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for EmployeeIR._refresh_msl_tracking — the auto-refresh that keeps the
employee (MSL) warehouse's ``custom_msl_tracking`` cache in sync with the ledger
after an Employee IR Issue/Receive posts stock.

Pure-logic: the warehouse resolver and the ledger recompute are patched, so no
DB / Stock Ledger is touched. ``_refresh_msl_tracking`` does lazy imports, so the
patches target the source modules (the attributes fetched at call time). Lives
under ``tests/`` (not the doctype folder) so the runner does not auto-infer a
``doctype`` and drag in the ERPNext/GST test-record bootstrap.
"""

from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	EmployeeIR,
)

_RESOLVE = (
	"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events."
	"main_slip_inject._resolve_source_warehouse_raw_material"
)
_RECALC = (
	"jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking."
	"recalculate_msl_tracking"
)


class TestEmployeeIRMSLTracking(IntegrationTestCase):
	def _make_eir(self):
		# Bare instance is enough: _refresh_msl_tracking only passes ``self`` to
		# the (patched) resolver and never touches other attributes.
		return EmployeeIR.__new__(EmployeeIR)

	def test_refreshes_resolved_msl_warehouse(self):
		"""When the employee's Raw Material (MSL) warehouse resolves, the cache is
		recomputed for exactly that warehouse."""
		with patch(
			_RESOLVE, return_value="Assembly MSL WH - 6 - GEPL"
		) as resolve, patch(_RECALC) as recalc:
			self._make_eir()._refresh_msl_tracking()

		resolve.assert_called_once()
		recalc.assert_called_once_with("Assembly MSL WH - 6 - GEPL")

	def test_no_warehouse_is_a_no_op(self):
		"""No employee/subcontractor Raw Material warehouse -> nothing to refresh
		(e.g. a subcontractor with no MSL warehouse); recompute is skipped."""
		with patch(_RESOLVE, return_value=None), patch(_RECALC) as recalc:
			self._make_eir()._refresh_msl_tracking()

		recalc.assert_not_called()

	def test_refresh_failure_is_swallowed(self):
		"""A tracking-refresh failure must never propagate out of on_submit /
		on_cancel and roll back the stock posting — it is logged, not raised."""
		with patch(_RESOLVE, return_value="MSL-WH"), patch(
			_RECALC, side_effect=RuntimeError("boom")
		), patch("frappe.log_error") as log_error:
			# Must not raise.
			self._make_eir()._refresh_msl_tracking()

		log_error.assert_called_once()
		self.assertEqual(
			log_error.call_args.kwargs.get("title"),
			"Employee IR: MSL tracking refresh failed",
		)
