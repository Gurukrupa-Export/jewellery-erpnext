# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for EIR fallback injection flow (no frappe.db.commit inside lifecycle code).

All tests are mock-based and run without live DB access.
"""

import inspect
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
	_resolve_department_warehouse,
	_schedule_extra_metal_injection_after_commit,
	_verify_injection_outputs,
	process_extra_metal_injection_job,
)


class TestNoCommitInSourceFile(FrappeTestCase):
	"""Structural guard: the source file must not contain frappe.db.commit()."""

	def test_no_db_commit_in_injection_module(self):
		import jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject as mod

		source = inspect.getsource(mod)
		self.assertNotIn(
			"frappe.db.commit()",
			source,
			"frappe.db.commit() must not appear inside the injection module.",
		)


class TestScheduleEnqueue(FrappeTestCase):
	"""_schedule_extra_metal_injection_after_commit must call frappe.enqueue with correct args."""

	def test_enqueue_called_with_after_commit_and_deterministic_job_name(self):
		with patch("frappe.enqueue") as mock_enqueue:
			_schedule_extra_metal_injection_after_commit("EIR-001", "ROW-001")

		mock_enqueue.assert_called_once()
		_, kwargs = mock_enqueue.call_args
		self.assertTrue(
			kwargs.get("enqueue_after_commit"), "enqueue_after_commit must be True"
		)
		self.assertEqual(kwargs.get("queue"), "long")
		self.assertEqual(kwargs.get("eir_name"), "EIR-001")
		self.assertEqual(kwargs.get("row_name"), "ROW-001")
		job_name = kwargs.get("job_name", "")
		self.assertIn("EIR-001", job_name)
		self.assertIn("ROW-001", job_name)


class TestProcessInjectionJobGuards(FrappeTestCase):
	"""process_extra_metal_injection_job must exit early on bad state."""

	def _make_eir(self, docstatus=1, rows=None):
		eir = MagicMock()
		eir.docstatus = docstatus
		eir.name = "EIR-001"
		eir.department = "Waxing"
		eir.manufacturing_work_order = "MWO-001"
		eir.employee_ir_operations = rows or []
		return eir

	def test_returns_early_when_docstatus_not_1(self):
		eir = self._make_eir(docstatus=0)
		with patch("frappe.get_doc", return_value=eir), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject."
			"_inject_via_source_warehouse_fallback"
		) as mock_inject:
			process_extra_metal_injection_job("EIR-001", "ROW-001")
		mock_inject.assert_not_called()

	def test_logs_error_when_row_not_found(self):
		eir = self._make_eir(docstatus=1, rows=[])
		with patch("frappe.get_doc", return_value=eir), patch(
			"frappe.log_error"
		) as mock_log, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject."
			"_inject_via_source_warehouse_fallback"
		) as mock_inject:
			process_extra_metal_injection_job("EIR-001", "ROW-MISSING")
		mock_log.assert_called_once()
		mock_inject.assert_not_called()

	def test_returns_early_when_no_extra_weight(self):
		row = MagicMock()
		row.name = "ROW-001"
		row.received_gross_wt = 10.0
		row.gross_wt = 10.0  # no gain
		eir = self._make_eir(docstatus=1, rows=[row])
		with patch("frappe.get_doc", return_value=eir), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject."
			"_inject_via_source_warehouse_fallback"
		) as mock_inject:
			process_extra_metal_injection_job("EIR-001", "ROW-001")
		mock_inject.assert_not_called()

	def test_returns_early_when_fully_submitted(self):
		row = MagicMock()
		row.name = "ROW-001"
		row.received_gross_wt = 12.0
		row.gross_wt = 10.0
		row.manufacturing_work_order = "MWO-001"
		eir = self._make_eir(docstatus=1, rows=[row])
		with patch("frappe.get_doc", return_value=eir), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject."
			"_resolve_department_warehouse",
			return_value="Waxing WO - GEPL",
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject."
			"_resolve_fallback_inject_segments",
			return_value=[{"mode": "transfer", "item_code": "X", "qty": 2}],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject."
			"_existing_injection_se_types",
			return_value={"Material Transfer (WORK ORDER)"},
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject."
			"_fallback_injection_fully_submitted",
			return_value=True,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject."
			"_inject_via_source_warehouse_fallback"
		) as mock_inject:
			process_extra_metal_injection_job("EIR-001", "ROW-001")
		mock_inject.assert_not_called()


class TestVerifyInjectionOutputs(FrappeTestCase):
	"""_verify_injection_outputs must log errors for missing MOP Log or SRE."""

	def _make_eir_and_row(self):
		eir = MagicMock()
		eir.name = "EIR-001"
		row = MagicMock()
		row.name = "ROW-001"
		row.manufacturing_work_order = "MWO-001"
		row.manufacturing_operation = "MOP-001"
		return eir, row

	def test_logs_error_when_mop_log_missing(self):
		eir, row = self._make_eir_and_row()

		def db_exists(doctype, filters=None):
			if doctype == "MOP Log":
				return None  # missing
			return "SRE-001"

		with patch("frappe.db.exists", side_effect=db_exists), patch(
			"frappe.log_error"
		) as mock_log:
			_verify_injection_outputs(eir, row, ["SE-001"])

		calls = [str(c) for c in mock_log.call_args_list]
		self.assertTrue(
			any("MOP Log" in c for c in calls), "Expected MOP Log error to be logged"
		)

	def test_logs_error_when_sre_missing(self):
		eir, row = self._make_eir_and_row()

		def db_exists(doctype, filters=None):
			if doctype == "Stock Reservation Entry":
				return None  # missing
			return "MOP-LOG-001"

		with patch("frappe.db.exists", side_effect=db_exists), patch(
			"frappe.log_error"
		) as mock_log:
			_verify_injection_outputs(eir, row, ["SE-001"])

		calls = [str(c) for c in mock_log.call_args_list]
		self.assertTrue(
			any("Stock Reservation Entry" in c or "SRE" in c for c in calls),
			"Expected SRE error to be logged",
		)

	def test_no_error_when_both_exist(self):
		eir, row = self._make_eir_and_row()
		with patch("frappe.db.exists", return_value="EXISTS"), patch(
			"frappe.log_error"
		) as mock_log:
			_verify_injection_outputs(eir, row, ["SE-001"])
		mock_log.assert_not_called()


class TestMopSettingsPatchIdempotent(FrappeTestCase):
	"""ensure_mop_settings_reservation_types.execute() must not duplicate rows."""

	def test_patch_does_not_duplicate_existing_types(self):
		from jewellery_erpnext.patches.ensure_mop_settings_reservation_types import (
			execute,
		)

		existing_types = {"Repack", "Material Transfer (WORK ORDER)"}

		mock_doc = MagicMock()
		mock_doc.stock_entry_type_to_reservation = []

		with patch("frappe.db.exists", return_value="MOP Settings"), patch(
			"frappe.db.get_all", return_value=list(existing_types)
		), patch("frappe.get_doc", return_value=mock_doc):
			execute()

		mock_doc.append.assert_not_called()
		mock_doc.save.assert_not_called()

	def test_patch_adds_missing_types(self):
		from jewellery_erpnext.patches.ensure_mop_settings_reservation_types import (
			execute,
		)

		mock_doc = MagicMock()
		mock_doc.stock_entry_type_to_reservation = []

		with patch("frappe.db.exists", return_value="MOP Settings"), patch(
			"frappe.db.get_all", return_value=[]
		), patch("frappe.get_doc", return_value=mock_doc):
			execute()

		self.assertEqual(mock_doc.append.call_count, 2)
		mock_doc.save.assert_called_once()
