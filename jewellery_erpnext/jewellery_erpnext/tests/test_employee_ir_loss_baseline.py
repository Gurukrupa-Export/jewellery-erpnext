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

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events import (
	utils as batch_utils,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	loss_stock_entry,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.precision import (
	EIR_OPERATION_WEIGHT_FIELDS,
	LOSS_DETAIL_WEIGHT_FIELDS,
	round_employee_ir_weights_to_precision,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
	get_loss_qty_in_grams,
	validate_loss_tables_required,
	validate_manually_book_loss_details,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	EmployeeIR,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_loss_entry import (
	employee_loss_entry as ele,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	create_mop_log_for_employee_ir_loss,
	get_current_mop_balance_rows,
)


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


class TestMopLossDetailsTotalBaseline(IntegrationTestCase):
	"""mop_loss_details_total reflects the MOP baseline available for loss,
	independent of how that loss is later split between auto and manual rows.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, ops, book_metal_loss_returns=None):
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


class TestBookMetalLossPrecisionResidual(IntegrationTestCase):
	"""Independently-rounded rows must reconcile to flt(loss, 3) exactly."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, mop_log_rows, gwt, r_gwt, manual_loss_rows=None):
		# Use a simple dict-like object instead of MagicMock to ensure proper iteration
		class DocStub:
			def __init__(self, manual_rows):
				self.manually_book_loss_details = manual_rows or []

		doc = DocStub(manual_loss_rows)

		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.get_cached_value",
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
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.get_system_settings",
				return_value="Banker's Rounding (legacy)",
			),
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


class TestCreateMopLogForEmployeeIrLoss(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
		return_value=None,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_auto_loss_emits_negative_qty_change(
		self, mock_new_doc, _mock_value, _mock_exists, _mock_uom
	):
		"""Loss row writes qty_change = -loss_weight so MOPLog.validate
		propagates the reduced qty_after_transaction into the prefix bucket.
		"""

		eir = MagicMock()
		eir.name = "EIR-1"

		loss_row = MagicMock()
		loss_row.name = "ELD-row-1"
		loss_row.item_code = "M-G-22KT-91.9-Y"
		loss_row.batch_no = "B-1"
		loss_row.proportionally_loss = 1.126
		loss_row.manufacturing_operation = "MOP-1"
		loss_row.manufacturing_work_order = "MWO-1"

		recorded = {}
		mop_log_doc = MagicMock()

		def _set(name, value):
			recorded[name] = value
			setattr(mop_log_doc, name, value)

		mop_log_doc.set.side_effect = _set
		mock_new_doc.return_value = mop_log_doc

		create_mop_log_for_employee_ir_loss(eir, loss_row, "Auto Employee Loss", 1.130)

		# Real loss delta — qty_change is the negative gram-equivalent.
		self.assertAlmostEqual(mop_log_doc.qty_change, -1.126, places=3)
		# PCS not tracked for metal/finding loss.
		self.assertEqual(mop_log_doc.pcs_change, 0)
		# bridge writer marker
		self.assertEqual(mop_log_doc.is_synced, 1)
		# attribution fields
		self.assertEqual(mop_log_doc.log_category, "Loss Attribution")
		self.assertEqual(mop_log_doc.loss_type, "Auto Employee Loss")
		self.assertAlmostEqual(mop_log_doc.loss_weight, 1.126, places=3)
		self.assertEqual(mop_log_doc.loss_source_row, "ELD-row-1")
		# voucher linkage
		self.assertEqual(mop_log_doc.voucher_type, "Employee IR")
		self.assertEqual(mop_log_doc.voucher_no, "EIR-1")
		mop_log_doc.save.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_cached_value",
		return_value="Carat",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
		return_value=None,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_carat_to_gram_manual_loss(
		self, mock_new_doc, _mock_value, _mock_exists, _mock_uom
	):
		eir = MagicMock()
		eir.name = "EIR-2"
		loss_row = MagicMock()
		loss_row.name = "MBL-1"
		loss_row.item_code = "D-X"
		loss_row.batch_no = "B-D-1"
		loss_row.proportionally_loss = 5.0
		loss_row.manufacturing_operation = "MOP-1"
		loss_row.manufacturing_work_order = "MWO-1"

		mop_log_doc = MagicMock()
		mock_new_doc.return_value = mop_log_doc

		create_mop_log_for_employee_ir_loss(eir, loss_row, "Manually Booked Loss", 5.0)

		# 5 carats × 0.2 = 1 g
		self.assertAlmostEqual(mop_log_doc.loss_weight, 1.0, places=3)
		# qty_change carries the gram-equivalent loss as a negative delta.
		self.assertAlmostEqual(mop_log_doc.qty_change, -1.0, places=3)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=True,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_loss_log_idempotency(self, mock_new_doc, _mock_exists, _mock_uom):
		eir = MagicMock()
		eir.name = "EIR-3"
		loss_row = MagicMock()
		loss_row.name = "ELD-1"
		loss_row.item_code = "M-X"
		loss_row.batch_no = "B-1"
		loss_row.proportionally_loss = 1.0
		loss_row.manufacturing_operation = "MOP-1"
		loss_row.manufacturing_work_order = "MWO-1"

		out = create_mop_log_for_employee_ir_loss(
			eir, loss_row, "Auto Employee Loss", 1.0
		)

		self.assertIsNone(out)
		mock_new_doc.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_no_loss_when_zero_or_negative(self, mock_new_doc, _mock_uom):
		eir = MagicMock()
		eir.name = "EIR-4"
		loss_row = MagicMock()
		loss_row.name = "ELD-0"
		loss_row.item_code = "M-X"
		loss_row.batch_no = "B-1"
		loss_row.proportionally_loss = 0
		loss_row.manufacturing_operation = "MOP-1"
		loss_row.manufacturing_work_order = "MWO-1"

		out = create_mop_log_for_employee_ir_loss(
			eir, loss_row, "Auto Employee Loss", 0
		)
		self.assertIsNone(out)
		mock_new_doc.assert_not_called()


class TestManualLossCap(IntegrationTestCase):
	"""Per-MWO total cap: total manual loss cannot exceed (gwt - r_gwt)."""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	def test_manual_loss_valid_within_baseline_cap(self, *_):
		"""Manual loss within baseline cap should pass validation."""
		doc = MagicMock()
		doc.docstatus = 0

		# Baseline 1.0 g.
		op = MagicMock()
		op.gross_wt = 10.0
		op.received_gross_wt = 9.0
		op.manufacturing_work_order = "MWO-1"
		doc.employee_ir_operations = [op]

		# 0.5 g manual loss (within 1.0 g baseline).
		mbl = MagicMock()
		mbl.idx = 1
		mbl.item_code = "M-G-22KT-91.9-Y"
		mbl.batch_no = "B-1"
		mbl.manufacturing_operation = "MOP-1"
		mbl.manufacturing_work_order = "MWO-1"
		mbl.proportionally_loss = 0.5
		mbl.pcs = 0
		doc.manually_book_loss_details = [mbl]
		doc.employee_loss_details = []

		try:
			validate_manually_book_loss_details(doc)
		except frappe.exceptions.ValidationError:
			self.fail(
				"validate_manually_book_loss_details raised ValidationError unexpectedly"
			)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	def test_manual_loss_combined_with_employee_loss(self, *_):
		"""Employee + manual loss combined cannot exceed baseline."""
		doc = MagicMock()
		doc.docstatus = 0

		# Baseline 1.0 g.
		op = MagicMock()
		op.gross_wt = 10.0
		op.received_gross_wt = 9.0
		op.manufacturing_work_order = "MWO-1"
		doc.employee_ir_operations = [op]

		# Employee loss 0.7 g.
		eld = MagicMock()
		eld.idx = 1
		eld.item_code = "M-X"
		eld.batch_no = "B-1"
		eld.manufacturing_operation = "MOP-1"
		eld.manufacturing_work_order = "MWO-1"
		eld.proportionally_loss = 0.7
		eld.pcs = 0
		doc.employee_loss_details = [eld]

		# Manual loss 0.5 g (0.7 + 0.5 = 1.2 g, exceeds 1.0 g baseline).
		mbl = MagicMock()
		mbl.idx = 1
		mbl.item_code = "M-Y"
		mbl.batch_no = "B-2"
		mbl.manufacturing_operation = "MOP-1"
		mbl.manufacturing_work_order = "MWO-1"
		mbl.proportionally_loss = 0.5
		mbl.pcs = 0
		doc.manually_book_loss_details = [mbl]

		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_manually_book_loss_details(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	def test_manual_loss_docstatus_submitted_skips_validation(self, *_):
		"""When docstatus != 0, validation should return early."""
		doc = MagicMock()
		doc.docstatus = 1  # Submitted

		result = validate_manually_book_loss_details(doc)

		self.assertIsNone(result)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value={"available_pcs": 10},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Carat",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	def test_manual_loss_diamond_pcs_validation_negative_pcs_throws(self, *_):
		"""Diamond item with negative PCS should throw error."""
		doc = MagicMock()
		doc.docstatus = 0

		op = MagicMock()
		op.gross_wt = 10.0
		op.received_gross_wt = 9.5
		op.manufacturing_work_order = "MWO-1"
		doc.employee_ir_operations = [op]

		# Diamond item with negative PCS.
		mbl = MagicMock()
		mbl.idx = 1
		mbl.item_code = "D-X"
		mbl.batch_no = "B-D"
		mbl.manufacturing_operation = "MOP-1"
		mbl.manufacturing_work_order = "MWO-1"
		mbl.proportionally_loss = 0.5
		mbl.pcs = -5
		doc.manually_book_loss_details = [mbl]
		doc.employee_loss_details = []

		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_manually_book_loss_details(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_available_qty_pcs_for_mop_item"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Carat",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	def test_manual_loss_diamond_pcs_exceeds_available(
		self,
		mock_get_single_value,
		mock_get_cached_value,
		mock_get_all,
		mock_pcs_helper,
	):
		"""Diamond item PCS to book exceeds available PCS should throw."""
		mock_pcs_helper.return_value = {"available_pcs": 5}

		doc = MagicMock()
		doc.docstatus = 0

		op = MagicMock()
		op.gross_wt = 10.0
		op.received_gross_wt = 9.5
		op.manufacturing_work_order = "MWO-1"
		doc.employee_ir_operations = [op]

		# Diamond item attempting to book 10 PCS when only 5 available.
		mbl = MagicMock()
		mbl.idx = 1
		mbl.item_code = "D-X"
		mbl.batch_no = "B-D"
		mbl.manufacturing_operation = "MOP-1"
		mbl.manufacturing_work_order = "MWO-1"
		mbl.proportionally_loss = 0.5
		mbl.pcs = 10
		doc.manually_book_loss_details = [mbl]
		doc.employee_loss_details = []

		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_manually_book_loss_details(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	def test_manual_loss_multiple_mwos_independent_baselines(self, *_):
		"""Multiple MWOs should have independent baseline caps."""
		doc = MagicMock()
		doc.docstatus = 0

		# MWO-1: baseline 0.5 g
		op1 = MagicMock()
		op1.gross_wt = 10.0
		op1.received_gross_wt = 9.5
		op1.manufacturing_work_order = "MWO-1"

		# MWO-2: baseline 1.0 g
		op2 = MagicMock()
		op2.gross_wt = 20.0
		op2.received_gross_wt = 19.0
		op2.manufacturing_work_order = "MWO-2"

		doc.employee_ir_operations = [op1, op2]

		# Manual loss for MWO-1: 0.3 g (within 0.5 g baseline).
		mbl1 = MagicMock()
		mbl1.idx = 1
		mbl1.item_code = "M-X"
		mbl1.batch_no = "B-1"
		mbl1.manufacturing_operation = "MOP-1"
		mbl1.manufacturing_work_order = "MWO-1"
		mbl1.proportionally_loss = 0.3
		mbl1.pcs = 0

		# Manual loss for MWO-2: 0.8 g (within 1.0 g baseline).
		mbl2 = MagicMock()
		mbl2.idx = 2
		mbl2.item_code = "M-Y"
		mbl2.batch_no = "B-2"
		mbl2.manufacturing_operation = "MOP-2"
		mbl2.manufacturing_work_order = "MWO-2"
		mbl2.proportionally_loss = 0.8
		mbl2.pcs = 0

		doc.manually_book_loss_details = [mbl1, mbl2]
		doc.employee_loss_details = []

		try:
			validate_manually_book_loss_details(doc)
		except frappe.exceptions.ValidationError:
			self.fail(
				"validate_manually_book_loss_details raised ValidationError unexpectedly"
			)


class TestBookMetalLossSpecExamples(IntegrationTestCase):
	"""Run the spec's worked examples directly through book_metal_loss and
	assert the proportional loss values. Verifies the C4 fix is correct.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, mop_log_rows, gwt, r_gwt, manual_loss_rows=None):
		class DocStub:
			def __init__(self, manual_rows):
				self.manually_book_loss_details = manual_rows or []

		doc = DocStub(manual_loss_rows)

		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.get_cached_value",
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
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.get_system_settings",
				return_value="Banker's Rounding (legacy)",
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.db.get_all",
				return_value=mop_log_rows,
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

	def test_example1_same_metal_multiple_batches(self):
		"""Example 1 from the spec:
		25.75 + 0.09 = 25.84, total loss 1.130
		Proportional => 1.126 / 0.004
		"""
		rows = [
			frappe._dict(
				{
					"item_code": "M-G-22KT-91.9-Y",
					"batch_no": "B1",
					"qty": 25.75,
					"pcs": 0,
				}
			),
			frappe._dict(
				{
					"item_code": "M-G-22KT-91.9-Y",
					"batch_no": "B2",
					"qty": 0.09,
					"pcs": 0,
				}
			),
		]
		result = self._run(rows, gwt=25.84, r_gwt=24.71)

		by_batch = {entry["batch_no"]: entry for entry in result}
		self.assertAlmostEqual(by_batch["B1"]["proportionally_loss"], 1.126, places=2)
		self.assertAlmostEqual(by_batch["B2"]["proportionally_loss"], 0.004, places=2)
		total_loss = sum(e["proportionally_loss"] for e in result)
		self.assertAlmostEqual(total_loss, 1.130, places=2)

	def test_example2_metal_and_finding(self):
		"""Example 2 from the spec:
		Items 2, 0.5, 1.5, 1 (total 5), loss 1
		Expected: 0.4, 0.1, 0.3, 0.2
		"""
		rows = [
			frappe._dict(
				{"item_code": "M-G-22KT-91.9-Y", "batch_no": "B1", "qty": 2.0, "pcs": 0}
			),
			frappe._dict(
				{"item_code": "M-G-22KT-91.9-Y", "batch_no": "B2", "qty": 0.5, "pcs": 0}
			),
			frappe._dict(
				{
					"item_code": "F-G-18KT-75.4-Y-BL",
					"batch_no": "B3",
					"qty": 1.5,
					"pcs": 0,
				}
			),
			frappe._dict(
				{
					"item_code": "F-G-18KT-75.4-Y-CHA",
					"batch_no": "B4",
					"qty": 1.0,
					"pcs": 0,
				}
			),
		]
		result = self._run(rows, gwt=5.0, r_gwt=4.0)

		by_batch = {entry["batch_no"]: entry for entry in result}
		self.assertAlmostEqual(by_batch["B1"]["proportionally_loss"], 0.4, places=2)
		self.assertAlmostEqual(by_batch["B2"]["proportionally_loss"], 0.1, places=2)
		self.assertAlmostEqual(by_batch["B3"]["proportionally_loss"], 0.3, places=2)
		self.assertAlmostEqual(by_batch["B4"]["proportionally_loss"], 0.2, places=2)

	def test_no_loss_when_gwt_equals_r_gwt(self):
		"""gwt == r_gwt => no loss, empty result."""
		rows = [
			frappe._dict(
				{"item_code": "M-G-22KT-91.9-Y", "batch_no": "B1", "qty": 5.0, "pcs": 0}
			),
		]
		result = self._run(rows, gwt=5.0, r_gwt=5.0)
		self.assertEqual(result, [])

	def test_dgo_rows_excluded_from_auto_loss(self):
		"""Rows with item_code prefix not in M/F must not be allocated loss."""
		rows = [
			frappe._dict(
				{"item_code": "M-G-22KT-91.9-Y", "batch_no": "B1", "qty": 1.0, "pcs": 0}
			),
			frappe._dict({"item_code": "D-X", "batch_no": "BD1", "qty": 2.0, "pcs": 5}),
			frappe._dict(
				{"item_code": "G-X", "batch_no": "BG1", "qty": 3.0, "pcs": 10}
			),
			frappe._dict({"item_code": "O-X", "batch_no": "BO1", "qty": 4.0, "pcs": 0}),
		]
		result = self._run(rows, gwt=1.0, r_gwt=0.5)

		batches = {entry["batch_no"] for entry in result}
		self.assertEqual(batches, {"B1"})


class TestLossLogIncludedInBalance(IntegrationTestCase):
	"""Loss attribution rows post a real qty_change reduction, so the balance
	helper MUST include them so downstream consumers (Make Receive Entry
	availability, manual loss validation, EOD SRE reconcile) see the
	post-loss balance.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	def test_get_current_mop_balance_rows_does_not_filter_log_category(
		self, mock_get_all
	):
		mock_get_all.return_value = []

		get_current_mop_balance_rows("MOP-1")

		_args, kwargs = mock_get_all.call_args
		filters = kwargs["filters"]
		# Filter must NOT exclude loss-attribution rows — they affect balance.
		self.assertNotIn("log_category", filters)
		self.assertEqual(filters["is_cancelled"], 0)
		self.assertEqual(filters["manufacturing_operation"], "MOP-1")


class TestLossMopLogReducesBalance(IntegrationTestCase):
	"""Negative qty_change must reduce qty_after_transaction across the three
	balance views (overall prefix, item-based, batch-based) and PCS balances
	must be preserved (loss is recorded by weight, not by piece count).
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
		return_value=None,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_no_prior_balance_yields_negative_qty_after_transaction(
		self, mock_new_doc, _mock_value, _mock_exists, _mock_uom
	):
		"""Edge case: no prior MOP Log row for this (item, batch). Loss still
		records the delta so MOP Log + Manufacturing Operation reflect the
		discrepancy (a missing prior balance is itself a problem the operator
		needs to see).
		"""

		recorded = {}
		mop_log_doc = MagicMock()

		def _set(name, value):
			recorded[name] = value

		mop_log_doc.set.side_effect = _set
		mock_new_doc.return_value = mop_log_doc

		eir = MagicMock()
		eir.name = "EIR-EMPTY"
		loss_row = MagicMock()
		loss_row.name = "ELD-EMPTY"
		loss_row.item_code = "M-X"
		loss_row.batch_no = "B-X"
		loss_row.proportionally_loss = 0.5
		loss_row.manufacturing_operation = "MOP-EMPTY"
		loss_row.manufacturing_work_order = "MWO-EMPTY"

		create_mop_log_for_employee_ir_loss(eir, loss_row, "Auto Employee Loss", 0.5)

		# qty_change is -0.5 regardless of prior balance.
		self.assertAlmostEqual(mop_log_doc.qty_change, -0.5, places=3)
		# qty_after_transaction = 0 - 0.5 = -0.5 (balance now negative; flag
		# to operator that prior balance was missing).
		self.assertAlmostEqual(recorded["qty_after_transaction"], -0.5, places=3)


class TestManualLossDgPcsValidation(IntegrationTestCase):
	"""validate_manually_book_loss_details adds a D/G PCS cap that consults
	the same MOP Log balance helper used by Make Receive Entry.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _make_doc(self, manual_rows, op_baseline=None):
		"""Compose the minimum doc-shape needed for validate_*."""
		doc = MagicMock()
		doc.docstatus = 0
		doc.manually_book_loss_details = manual_rows
		# Baseline source for the per-MWO cap; default = generous (10g).
		op = MagicMock()
		if op_baseline:
			op.gross_wt, op.received_gross_wt, op.manufacturing_work_order = op_baseline
		else:
			op.gross_wt = 100.0
			op.received_gross_wt = 90.0
			op.manufacturing_work_order = "MWO-1"
		doc.employee_ir_operations = [op]
		return doc

	def _mbl(self, item_code, batch_no, loss_qty, pcs):
		row = MagicMock()
		row.idx = 1
		row.item_code = item_code
		row.batch_no = batch_no
		row.proportionally_loss = loss_qty
		row.pcs = pcs
		row.manufacturing_operation = "MOP-1"
		row.manufacturing_work_order = "MWO-1"
		row.variant_of = item_code[0]
		return row

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Carat",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value",
		# qty_after_transaction_batch_based balance lookup → 5g (covers loss).
		return_value=5.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	def test_t17_d_pcs_within_available_passes(self, mock_get_all, *_):
		"""T17 — D row with PCS <= available_pcs passes validation."""

		# Helper sees MOP Log batch with 10 PCS available.
		mock_get_all.return_value = [
			frappe._dict(
				{
					"item_code": "D-X",
					"batch_no": "BD1",
					"qty_after_transaction_batch_based": 5.0,
					"pcs_after_transaction_batch_based": 10,
					"name": "MOPLOG-D17",
				}
			)
		]
		doc = self._make_doc([self._mbl("D-X", "BD1", 1.0, 5)])
		# 1 carat == 0.2g loss; baseline 10g → cap fine; PCS 5 ≤ 10 → fine.
		validate_manually_book_loss_details(doc)  # must not raise

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Carat",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value",
		return_value=5.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	def test_t18_d_pcs_over_available_throws(self, mock_get_all, *_):
		"""T18 — D row with PCS > available_pcs throws."""

		mock_get_all.return_value = [
			frappe._dict(
				{
					"item_code": "D-X",
					"batch_no": "BD1",
					"qty_after_transaction_batch_based": 5.0,
					"pcs_after_transaction_batch_based": 3,
					"name": "MOPLOG-D18",
				}
			)
		]
		doc = self._make_doc([self._mbl("D-X", "BD1", 1.0, 5)])
		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_manually_book_loss_details(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Carat",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value",
		return_value=5.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	def test_t19_g_pcs_over_available_throws(self, mock_get_all, *_):
		"""T19 — G row with PCS > available_pcs throws (same rule as D)."""

		mock_get_all.return_value = [
			frappe._dict(
				{
					"item_code": "G-X",
					"batch_no": "BG1",
					"qty_after_transaction_batch_based": 5.0,
					"pcs_after_transaction_batch_based": 2,
					"name": "MOPLOG-G19",
				}
			)
		]
		doc = self._make_doc([self._mbl("G-X", "BG1", 1.0, 5)])
		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_manually_book_loss_details(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Carat",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value",
		return_value=5.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[],
	)
	def test_t20_d_negative_pcs_throws(self, *_):
		"""T20 — D row with negative pcs throws."""

		doc = self._make_doc([self._mbl("D-X", "BD1", 1.0, -1)])
		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_manually_book_loss_details(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value",
		return_value=5.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	def test_t21_mf_loss_skips_pcs_check(self, mock_get_all, *_):
		"""T21 — M/F manual loss does not invoke PCS gate even when pcs is 0
		on a balance that would otherwise cap to 0; i.e. no spurious throw.
		"""

		mock_get_all.return_value = []
		doc = self._make_doc([self._mbl("M-X", "B1", 1.0, 0)])
		validate_manually_book_loss_details(doc)


class TestLossMopLogPcsRegression(IntegrationTestCase):
	"""Regression guards for the existing D/G negative pcs_change contract
	in create_mop_log_for_employee_ir_loss. PCS reconciliation must NOT
	change the loss MOP Log emission rules.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _setup(self, item_code, pcs):
		eir = MagicMock()
		eir.name = "EIR-PCS"
		loss_row = MagicMock()
		loss_row.name = "MBL-PCS"
		loss_row.item_code = item_code
		loss_row.batch_no = "B1"
		loss_row.proportionally_loss = 1.0
		loss_row.pcs = pcs
		loss_row.manufacturing_operation = "MOP-1"
		loss_row.manufacturing_work_order = "MWO-1"
		return eir, loss_row

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
		return_value=None,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_t22_d_loss_emits_negative_pcs_change(self, mock_new_doc, *_):
		"""T22 — D loss MOP Log must emit pcs_change = -loss_row.pcs."""

		eir, loss_row = self._setup("D-X", 4)
		mop_log_doc = MagicMock()
		mock_new_doc.return_value = mop_log_doc
		create_mop_log_for_employee_ir_loss(eir, loss_row, "Manually Booked Loss", 1.0)
		self.assertEqual(mop_log_doc.pcs_change, -4)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
		return_value=None,
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_t23_m_loss_emits_zero_pcs_change(self, mock_new_doc, *_):
		"""T23 — M loss MOP Log keeps pcs_change=0 even when loss_row.pcs is set."""

		eir, loss_row = self._setup("M-X", 4)  # pcs would be ignored for M
		mop_log_doc = MagicMock()
		mock_new_doc.return_value = mop_log_doc
		create_mop_log_for_employee_ir_loss(eir, loss_row, "Manually Booked Loss", 1.0)
		self.assertEqual(mop_log_doc.pcs_change, 0)


# Resolve to the app root so the tests stay path-independent.
_APP_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _load_doctype_json(rel_path: str) -> dict:
	with open(os.path.join(_APP_ROOT, rel_path)) as f:
		return json.load(f)


def _float_fields_with_precision(doctype_json: dict) -> dict[str, str | None]:
	return {
		f["fieldname"]: f.get("precision")
		for f in doctype_json["fields"]
		if f.get("fieldtype") == "Float"
	}


class TestSchemaPrecision(IntegrationTestCase):
	"""DocType JSON precision attribute is the source of truth at migrate
	time — every covered Float weight field must declare precision=3.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_employee_ir_operation_weight_fields_precision_3(self):
		j = _load_doctype_json(
			"doctype/employee_ir_operation/employee_ir_operation.json"
		)
		fields = _float_fields_with_precision(j)
		for f in EIR_OPERATION_WEIGHT_FIELDS:
			self.assertEqual(
				fields.get(f),
				"3",
				f"{f} on Employee IR Operation must have precision=3",
			)

	def test_employee_loss_details_precision_3(self):
		j = _load_doctype_json(
			"doctype/employee_loss_details/employee_loss_details.json"
		)
		fields = _float_fields_with_precision(j)
		for f in LOSS_DETAIL_WEIGHT_FIELDS:
			self.assertEqual(fields.get(f), "3", f"{f} must have precision=3")

	def test_manually_book_loss_details_precision_3(self):
		j = _load_doctype_json(
			"doctype/manually_book_loss_details/manually_book_loss_details.json"
		)
		fields = _float_fields_with_precision(j)
		for f in LOSS_DETAIL_WEIGHT_FIELDS:
			self.assertEqual(fields.get(f), "3", f"{f} must have precision=3")

	def test_employee_ir_mop_loss_details_total_precision_3(self):
		j = _load_doctype_json("doctype/employee_ir/employee_ir.json")
		fields = _float_fields_with_precision(j)
		self.assertEqual(fields.get("mop_loss_details_total"), "3")

	def test_manufacturing_operation_weight_fields_precision_3(self):
		j = _load_doctype_json(
			"doctype/manufacturing_operation/manufacturing_operation.json"
		)
		fields = _float_fields_with_precision(j)
		# Subset directly tied to weight bookkeeping; Int / time / pcs
		# fields are intentionally excluded from precision=3.
		for f in (
			"gross_wt",
			"net_wt",
			"finding_wt",
			"diamond_wt",
			"gemstone_wt",
			"diamond_wt_in_gram",
			"gemstone_wt_in_gram",
			"received_gross_wt",
			"received_net_wt",
			"loss_wt",
			"prev_gross_wt",
			"other_wt",
		):
			self.assertEqual(
				fields.get(f),
				"3",
				f"{f} on Manufacturing Operation must have precision=3",
			)


class TestRuntimePrecisionRounder(IntegrationTestCase):
	"""``round_employee_ir_weights_to_precision`` rounds in-memory doc
	values before validate. Any field with sub-3-decimal input loses tail
	digits.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _doc_with_op(self, **op_overrides):
		doc = MagicMock()
		op = frappe._dict({f: 0.0 for f in EIR_OPERATION_WEIGHT_FIELDS})
		op.update(op_overrides)
		# child.set must mutate the underlying dict.
		op_mock = MagicMock()
		for k, v in op.items():
			setattr(op_mock, k, v)
		op_mock.set.side_effect = lambda k, v: setattr(op_mock, k, v)
		doc.employee_ir_operations = [op_mock]
		doc.mop_loss_details_total = None
		doc.employee_loss_details = []
		doc.manually_book_loss_details = []
		return doc, op_mock

	def test_employee_ir_operation_gross_wt_rounded_to_3(self):
		doc, op = self._doc_with_op(gross_wt=1.234567)
		round_employee_ir_weights_to_precision(doc)
		self.assertAlmostEqual(op.gross_wt, 1.235, places=6)

	def test_received_gross_wt_rounded_to_3(self):
		doc, op = self._doc_with_op(received_gross_wt=2.111199)
		round_employee_ir_weights_to_precision(doc)
		self.assertAlmostEqual(op.received_gross_wt, 2.111, places=6)

	def test_mop_loss_details_total_rounded_when_present(self):
		doc, _op = self._doc_with_op()
		doc.mop_loss_details_total = 5.6789
		round_employee_ir_weights_to_precision(doc)
		self.assertAlmostEqual(doc.mop_loss_details_total, 5.679, places=6)

	def test_loss_detail_proportionally_loss_rounded(self):
		doc, _op = self._doc_with_op()
		row = MagicMock()
		row.proportionally_loss = 0.123456
		row.net_weight = 0.0
		row.received_gross_weight = 0.0
		row.main_slip_consumption = 0.0
		row.set.side_effect = lambda k, v: setattr(row, k, v)
		doc.manually_book_loss_details = [row]
		round_employee_ir_weights_to_precision(doc)
		self.assertAlmostEqual(row.proportionally_loss, 0.123, places=6)


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


class TestValidateLossTablesRequired(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestGetLossQtyInGrams(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		self.norm = get_loss_qty_in_grams

	def test_diamond_carat_to_gram(self):
		# 1 carat = 0.2 g
		self.assertAlmostEqual(self.norm("D-G-18KT-75.4-Y", 1.0), 0.200, places=3)

	def test_gemstone_carat_to_gram(self):
		# 2 carat = 0.4 g
		self.assertAlmostEqual(self.norm("G-G-18KT-75.4-Y", 2.0), 0.400, places=3)

	def test_metal_unchanged(self):
		self.assertAlmostEqual(self.norm("M-G-22KT-91.9-Y", 1.0), 1.000, places=3)

	def test_finding_unchanged(self):
		self.assertAlmostEqual(self.norm("F-G-18KT-75.4-Y", 1.0), 1.000, places=3)

	def test_other_unchanged(self):
		self.assertAlmostEqual(self.norm("O-MISC-001", 1.0), 1.000, places=3)

	def test_zero_qty(self):
		self.assertEqual(self.norm("D-G-18KT-75.4-Y", 0), 0.0)
		self.assertEqual(self.norm("M-G-22KT-91.9-Y", 0), 0.0)

	def test_none_item_code_returns_qty_unchanged(self):
		# Defensive path for callers that hand in a missing item_code.
		self.assertAlmostEqual(self.norm(None, 1.5), 1.500, places=3)
		self.assertAlmostEqual(self.norm("", 1.5), 1.500, places=3)

	def test_precision_3_rounding(self):
		# 5 carat -> 1 g exactly; ensure we round to 3 dp not extend precision.
		self.assertEqual(self.norm("D-X", 5.0), 1.000)
		# 0.333... carat -> 0.0666... g -> rounded to 0.067
		self.assertAlmostEqual(self.norm("D-X", 1.0 / 3), 0.067, places=3)

	def test_dg_prefix_decided_by_first_char_only(self):
		# Items with prefix Dx-... or Gx-... still convert (single-char rule).
		self.assertAlmostEqual(self.norm("D2-FOO", 5.0), 1.000, places=3)
		# An item code starting with another letter does NOT convert even if
		# 'D' or 'G' appears later.
		self.assertAlmostEqual(self.norm("MD-FOO", 5.0), 5.000, places=3)

	def test_negative_qty_preserved(self):
		# Gain rows (negative loss) should normalize the sign too.
		self.assertAlmostEqual(self.norm("D-X", -1.0), -0.200, places=3)

	def test_mixed_basket_sum_matches_expected(self):
		# Smoke test for the pattern callers will use: sum a heterogeneous
		# loss basket and verify the gram-normalized total.
		basket = [
			("M-G-22KT-91.9-Y", 0.300),
			("F-G-18KT-75.4-Y", 0.200),
			("D-G-18KT-75.4-Y", 1.000),  # 1 ct -> 0.2 g
			("G-G-18KT-75.4-Y", 0.500),  # 0.5 ct -> 0.1 g
		]
		total = sum(self.norm(code, qty) for code, qty in basket)
		self.assertAlmostEqual(total, 0.800, places=3)


# The bug scenario: the operation-matched (Grinding) SRE holds only 0.010 while
# the sibling Casting SRE holds 5.395, both in "Waxing WO - T". A 0.015 loss
# exceeds the op-matched SRE alone but fits the 5.405 batch aggregate.
_WAREHOUSE = "Waxing WO - T"


def _sre_row(name, reserved_qty, mop, warehouse=_WAREHOUSE):
	return {
		"name": name,
		"warehouse": warehouse,
		"reserved_qty": reserved_qty,
		"available_qty": reserved_qty,
		"voucher_qty": 0,
		"reservation_based_on": "Serial and Batch",
		"has_batch_no": 1,
		"company": "KG GK Jewellers Private Limited",
		"voucher_type": "Sales Order",
		"voucher_no": "SAL-ORD-2026-00001",
		"voucher_detail_no": "soi-1",
		"stock_uom": "Gram",
		"manufacturing_operation": mop,
	}


def _row(**fields):
	defaults = {
		"idx": 1,
		"item_code": "M-G-18KT-75.4-Y",
		"batch_no": "KG2F054-MGL18754Y0-F874H",
		"manufacturing_operation": "MOP-525LX",  # Grinding (current op)
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


def _mock_sre_eir(**fields):
	defaults = {"name": "gugsvhhbcc"}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


class TestLossSreSelection(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _find_sre(self, rows, row, qty):
		"""Run _find_sre with frappe.db.sql + frappe.get_doc patched."""

		def fake_get_doc(_doctype, name):
			match = next(r for r in rows if r["name"] == name)
			return SimpleNamespace(
				name=match["name"],
				warehouse=match["warehouse"],
				reserved_qty=match["reserved_qty"],
			)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry.frappe"
		) as mock_frappe:
			mock_frappe.db.sql.return_value = rows
			mock_frappe.get_doc.side_effect = fake_get_doc
			return loss_stock_entry._find_sre(
				_mock_sre_eir(),
				row,
				"MWO-T-NE01084-007-14-75.4-Y-01",
				"employee_loss_details",
				qty,
			)

	def test_find_sre_picks_covering_sibling_over_tiny_op_match(self):
		# Op-matched SRE (0.010) cannot cover 0.015; the 5.395 sibling can.
		rows = [
			_sre_row("MAT-SRE-2026-88092", 0.010, "MOP-525LX"),
			_sre_row("MAT-SRE-2026-88093", 5.395, "MOP-UG559"),
		]
		sre_doc, candidates = self._find_sre(rows, _row(), 0.015)

		self.assertEqual(sre_doc.name, "MAT-SRE-2026-88093")
		# Candidates confined to one warehouse, op-match ordered first.
		self.assertEqual(
			[c["name"] for c in candidates],
			["MAT-SRE-2026-88092", "MAT-SRE-2026-88093"],
		)
		self.assertEqual({c["warehouse"] for c in candidates}, {_WAREHOUSE})

	def test_find_sre_keeps_op_match_when_it_covers(self):
		# Op-matched SRE (1.0) covers 0.015, so it wins over the larger sibling.
		rows = [
			_sre_row("MAT-SRE-OPMATCH", 1.0, "MOP-525LX"),
			_sre_row("MAT-SRE-BIG", 5.395, "MOP-UG559"),
		]
		sre_doc, _candidates = self._find_sre(rows, _row(), 0.015)
		self.assertEqual(sre_doc.name, "MAT-SRE-OPMATCH")

	def test_find_sre_confines_to_op_match_warehouse(self):
		# A same-batch SRE in another warehouse must be excluded from candidates.
		rows = [
			_sre_row("MAT-SRE-88092", 0.010, "MOP-525LX", warehouse=_WAREHOUSE),
			_sre_row("MAT-SRE-88093", 5.395, "MOP-UG559", warehouse=_WAREHOUSE),
			_sre_row("MAT-SRE-OTHER", 9.0, "MOP-OTHER", warehouse="Some Other WH"),
		]
		sre_doc, candidates = self._find_sre(rows, _row(), 0.015)
		self.assertEqual(sre_doc.name, "MAT-SRE-88093")
		self.assertEqual({c["warehouse"] for c in candidates}, {_WAREHOUSE})

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry._"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry.frappe"
	)
	def test_validate_passes_when_selected_sre_covers(
		self, mock_frappe, mock_underscore
	):
		mock_underscore.side_effect = lambda x: x
		candidates = [
			_sre_row("MAT-SRE-2026-88092", 0.010, "MOP-525LX"),
			_sre_row("MAT-SRE-2026-88093", 5.395, "MOP-UG559"),
		]
		sre_doc = SimpleNamespace(
			name="MAT-SRE-2026-88093", warehouse=_WAREHOUSE, reserved_qty=5.395
		)
		# 0.015 <= 5.395 -> no exception.
		loss_stock_entry._validate_sre_qty(
			_mock_sre_eir(), _row(), sre_doc, candidates, 0.015, "employee_loss_details"
		)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry._"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry.frappe"
	)
	def test_validate_throws_when_no_single_sre_covers(
		self, mock_frappe, mock_underscore
	):
		mock_underscore.side_effect = lambda x: x
		mock_frappe.throw.side_effect = ValidationError
		# 0.010 + 0.008 = 0.018 >= 0.015 in aggregate, but no single SRE covers
		# 0.015 -> must throw (single-covering-SRE policy).
		candidates = [
			_sre_row("A", 0.010, "MOP-525LX"),
			_sre_row("B", 0.008, "MOP-UG559"),
		]
		sre_doc = SimpleNamespace(name="A", warehouse=_WAREHOUSE, reserved_qty=0.010)
		with self.assertRaises(ValidationError):
			loss_stock_entry._validate_sre_qty(
				_mock_sre_eir(),
				_row(),
				sre_doc,
				candidates,
				0.015,
				"employee_loss_details",
			)

	def tearDown(self):
		return super().tearDown()


_TOTAL_RESERVED_PATH = (
	"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry"
	".get_sre_reserved_qty_for_voucher_detail_no"
)


def _sre(**fields):
	defaults = {
		"voucher_type": "Sales Order",
		"voucher_no": "SAL-ORD-2026-00956",
		"voucher_detail_no": "soi-1",
		"item_code": "M-G-22KT-91.9-Y",
		"name": "MAT-SRE-2026-73342",
		"voucher_qty": 28.2675,
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


class TestReservationVoucherQty(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_over_reserved_so_lifts_voucher_qty(self):
		# Other active SREs already hold 22.2 against a 28.2675 SO line; the
		# reduced reservation of 7.76 would otherwise exceed the 6.068 allowance.
		sre = _sre()
		with patch(_TOTAL_RESERVED_PATH, return_value=22.2) as mock_total:
			result = loss_stock_entry._reservation_voucher_qty(sre, 7.76)

		self.assertAlmostEqual(result, 29.96)
		mock_total.assert_called_once_with(
			"M-G-22KT-91.9-Y",
			"Sales Order",
			"SAL-ORD-2026-00956",
			"soi-1",
			ignore_sre="MAT-SRE-2026-73342",
		)

	def test_keeps_base_when_not_over_reserved(self):
		# total + qty (5.0 + 7.76 = 12.76) is below the SO qty → keep base.
		sre = _sre()
		with patch(_TOTAL_RESERVED_PATH, return_value=5.0):
			result = loss_stock_entry._reservation_voucher_qty(sre, 7.76)

		self.assertAlmostEqual(result, 28.2675)

	def test_non_sales_order_returns_base(self):
		sre = _sre(voucher_type="Work Order")
		with patch(_TOTAL_RESERVED_PATH) as mock_total:
			result = loss_stock_entry._reservation_voucher_qty(sre, 7.76)

		self.assertAlmostEqual(result, 28.2675)
		mock_total.assert_not_called()

	def test_missing_voucher_detail_returns_base(self):
		sre = _sre(voucher_detail_no=None)
		with patch(_TOTAL_RESERVED_PATH) as mock_total:
			result = loss_stock_entry._reservation_voucher_qty(sre, 7.76)

		self.assertAlmostEqual(result, 28.2675)
		mock_total.assert_not_called()


def _det_flt(value, precision=None, rounding_method=None):
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


def _se_row(item_code, qty, batch_no=None, idx=1):
	return SimpleNamespace(item_code=item_code, qty=qty, batch_no=batch_no, idx=idx)


def _doc(**fields):
	defaults = {
		"name": "EMP-LOSS-00001",
		"company": "GK",
		"branch": "Main",
		"department": "Casting - GK",
		"manufacturer": "Shubh",
		"employee": "EMP-0001",
		"posting_date": "2026-07-10",
		"posting_time": "10:00:00",
		"msl_warehouse": "EMP-0001 RM - GK",
		"scrap_warehouse": "Casting Scrap - GK",
		"stock_entry": None,
		"items": [_se_row("M-G-18KT-75.4-Y", 5.0)],
	}
	defaults.update(fields)
	d = SimpleNamespace(**defaults)
	d.db_set = MagicMock()
	return d


class _FakeSE:
	"""Captures the Stock Entry payload the builder constructs."""

	def __init__(self, payload):
		self.payload = payload
		self.items = []
		self.name = "SE-LOSS-0001"
		self.saved = False
		self.submitted = False
		self.flags = SimpleNamespace()

	def append(self, table, row):
		self.items.append(row)

	def save(self):
		self.saved = True

	def submit(self):
		self.submitted = True


class TestResolvers(IntegrationTestCase):
	def test_msl_warehouse_found(self):
		with patch("frappe.db.get_value", return_value="EMP RM - GK"):
			self.assertEqual(ele._resolve_msl_warehouse(_doc()), "EMP RM - GK")

	def test_msl_warehouse_missing_throws(self):
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaises(ValidationError):
				ele._resolve_msl_warehouse(_doc())

	def test_msl_warehouse_no_employee_throws(self):
		with self.assertRaises(ValidationError):
			ele._resolve_msl_warehouse(_doc(employee=None))

	def test_scrap_warehouse(self):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_scrap_warehouse",
			return_value="Scrap - GK",
		):
			self.assertEqual(ele._resolve_scrap_warehouse(_doc()), "Scrap - GK")

	def test_scrap_warehouse_no_department_throws(self):
		with self.assertRaises(ValidationError):
			ele._resolve_scrap_warehouse(_doc(department=None))

	def test_loss_item_no_variant_throws(self):
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaises(ValidationError):
				ele._resolve_loss_item(_doc(), "ITEM-X")

	def test_loss_item_no_manufacturer_throws(self):
		with patch("frappe.db.get_value", return_value="M"):
			with self.assertRaises(ValidationError):
				ele._resolve_loss_item(_doc(manufacturer=None), "M-G-18KT-75.4-Y")

	def test_loss_item_none_throws(self):
		with patch("frappe.db.get_value", return_value="M"), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_loss_item",
			return_value=None,
		):
			with self.assertRaises(ValidationError):
				ele._resolve_loss_item(_doc(), "M-G-18KT-75.4-Y")

	def test_loss_item_happy(self):
		with patch("frappe.db.get_value", return_value="M"), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_loss_item",
			return_value="ML-G-18KT-75.4-Y",
		):
			self.assertEqual(
				ele._resolve_loss_item(_doc(), "M-G-18KT-75.4-Y"), "ML-G-18KT-75.4-Y"
			)


class TestFifoBatches(IntegrationTestCase):
	def setUp(self):
		self._patches = [
			patch.object(ele, "_loss_precision", return_value=3),
			patch.object(ele, "flt", _det_flt),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def test_non_batch_item(self):
		with patch("frappe.get_cached_value", return_value=0):
			out = ele._fifo_batches(_doc(), "PURE-G", "WH", 5.0)
		self.assertEqual(len(out), 1)
		self.assertIsNone(out[0].batch_no)
		self.assertEqual(out[0].qty, 5.0)

	def test_allocates(self):
		batches = [
			SimpleNamespace(batch_no="B1", qty=3.0),
			SimpleNamespace(batch_no="B2", qty=2.0),
		]
		with patch("frappe.get_cached_value", return_value=1), patch(
			"jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger.capped_auto_batch_nos",
			return_value=batches,
		):
			out = ele._fifo_batches(_doc(), "M-G", "WH", 5.0)
		self.assertEqual([b.batch_no for b in out], ["B1", "B2"])

	def test_insufficient_throws(self):
		batches = [SimpleNamespace(batch_no="B1", qty=3.0)]
		with patch("frappe.get_cached_value", return_value=1), patch(
			"jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger.capped_auto_batch_nos",
			return_value=batches,
		):
			with self.assertRaises(ValidationError):
				ele._fifo_batches(_doc(), "M-G", "WH", 5.0)


class TestBuilder(IntegrationTestCase):
	def setUp(self):
		self._patches = [
			patch.object(ele, "_loss_precision", return_value=3),
			patch.object(ele, "flt", _det_flt),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def _build(self, doc, existing=False, fifo=None):
		captured = {}

		def _get_doc(payload):
			se = _FakeSE(payload)
			captured["se"] = se
			return se

		default_fifo = fifo or [SimpleNamespace(batch_no="B-001", qty=5.0)]

		with patch("frappe.db.exists", return_value=existing), patch(
			"frappe.new_doc", return_value=SimpleNamespace()
		), patch("frappe.get_doc", side_effect=_get_doc), patch.object(
			ele, "_resolve_loss_item", return_value="ML-G-18KT-75.4-Y"
		), patch.object(ele, "_fifo_batches", return_value=default_fifo), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.stock_lock_key",
			side_effect=lambda i, w, b=None: (i, w, b or ""),
		):
			ele.make_employee_loss_stock_entry(doc)
		return captured.get("se")

	def test_idempotency_guard_skips(self):
		doc = _doc(stock_entry="SE-OLD")
		se = self._build(doc, existing=True)
		self.assertIsNone(se)
		doc.db_set.assert_not_called()

	def test_se_header_and_rows(self):
		doc = _doc()
		se = self._build(doc)
		self.assertEqual(se.payload["stock_entry_type"], "Process Loss")
		self.assertEqual(se.payload["purpose"], "Repack")
		self.assertEqual(se.payload["auto_created"], 1)
		self.assertEqual(se.payload["manufacturer"], "Shubh")
		self.assertEqual(se.payload["company"], "GK")
		self.assertNotIn("_customer", se.payload)
		# 1 consume + 1 produce
		self.assertEqual(len(se.items), 2)
		consume, produce = se.items[0], se.items[1]
		self.assertEqual(consume["item_code"], "M-G-18KT-75.4-Y")
		self.assertEqual(consume["s_warehouse"], "EMP-0001 RM - GK")
		self.assertEqual(consume["batch_no"], "B-001")
		self.assertEqual(consume["use_serial_batch_fields"], 1)
		self.assertNotIn("t_warehouse", consume)
		self.assertEqual(produce["item_code"], "ML-G-18KT-75.4-Y")
		self.assertEqual(produce["qty"], 5.0)
		self.assertEqual(produce["t_warehouse"], "Casting Scrap - GK")
		self.assertEqual(produce["is_finished_item"], 1)
		self.assertEqual(produce["set_basic_rate_manually"], 1)
		self.assertEqual(produce["inventory_type"], "Regular Stock")
		self.assertNotIn("batch_no", produce)
		self.assertTrue(se.saved and se.submitted)
		doc.db_set.assert_called_once_with("stock_entry", "SE-LOSS-0001")

	def test_multiple_items_produce_per_row(self):
		doc = _doc(
			items=[
				_se_row("M-G-18KT-75.4-Y", 5.0),
				_se_row("F-G-18KT-75.4-Y", 2.0, idx=2),
			]
		)
		se = self._build(doc)
		# 2 consume + 2 produce
		self.assertEqual(len(se.items), 4)
		produce_rows = [r for r in se.items if r.get("t_warehouse")]
		self.assertEqual(len(produce_rows), 2)

	def test_batch_specified_on_row_used_directly(self):
		doc = _doc(items=[_se_row("M-G-18KT-75.4-Y", 4.0, batch_no="B-USER")])
		captured = {}

		def _get_doc(payload):
			se = _FakeSE(payload)
			captured["se"] = se
			return se

		with patch("frappe.db.exists", return_value=False), patch(
			"frappe.new_doc", return_value=SimpleNamespace()
		), patch("frappe.get_doc", side_effect=_get_doc), patch.object(
			ele, "_resolve_loss_item", return_value="ML-G-18KT-75.4-Y"
		), patch.object(
			ele, "_fifo_batches", side_effect=AssertionError("FIFO must not run")
		), patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.stock_lock_key",
			side_effect=lambda i, w, b=None: (i, w, b or ""),
		):
			ele.make_employee_loss_stock_entry(doc)
		se = captured["se"]
		consume = se.items[0]
		self.assertEqual(consume["batch_no"], "B-USER")
		self.assertEqual(consume["qty"], 4.0)


class TestCancel(IntegrationTestCase):
	def test_noop_when_no_se(self):
		doc = _doc(stock_entry=None)
		with patch("frappe.get_doc") as m:
			ele.cancel_employee_loss_stock_entries(doc)
			m.assert_not_called()

	def test_cancels_linked_se(self):
		doc = _doc(stock_entry="SE-LOSS-0001")
		cancelled = []
		fake = SimpleNamespace(cancel=lambda: cancelled.append(True))
		with patch(
			"frappe.db.get_value",
			return_value=SimpleNamespace(name="SE-LOSS-0001", docstatus=1),
		), patch("frappe.get_doc", return_value=fake):
			ele.cancel_employee_loss_stock_entries(doc)
		self.assertEqual(len(cancelled), 1)

	def test_skips_already_cancelled_se(self):
		doc = _doc(stock_entry="SE-LOSS-0001")
		called = []
		with patch(
			"frappe.db.get_value",
			return_value=SimpleNamespace(name="SE-LOSS-0001", docstatus=2),
		), patch("frappe.get_doc", side_effect=lambda *a, **k: called.append(1)):
			ele.cancel_employee_loss_stock_entries(doc)
		self.assertEqual(called, [])


def _inv_row(**fields):
	defaults = {
		"idx": 1,
		"item_code": "M-G-18KT-75.4-Y",
		"batch_no": "Kanish Ext-2F05-M-G-18KT-75.4-Y-01-A",
		"inventory_type": None,
		"customer": None,
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


class TestLossBatchInventoryResolution(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _resolve(self, row, batch_value):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir."
			"doc_events.loss_stock_entry.frappe"
		) as mock_frappe:
			mock_frappe.db.get_value.return_value = batch_value
			return loss_stock_entry._resolve_batch_inventory(row)

	def test_customer_goods_batch_flows_through(self):
		# The reported bug: source batch is Customer Goods -> row must be too.
		inv, cust = self._resolve(
			_inv_row(),
			{
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "Kanish Ext",
			},
		)
		self.assertEqual(inv, "Customer Goods")
		self.assertEqual(cust, "Kanish Ext")

	def test_regular_stock_batch_yields_no_customer(self):
		# Batch has no custom_inventory_type -> Regular Stock and no stray customer.
		inv, cust = self._resolve(
			_inv_row(), {"custom_inventory_type": None, "custom_customer": None}
		)
		self.assertEqual(inv, "Regular Stock")
		self.assertIsNone(cust)

	def test_batch_wins_over_row_value(self):
		# The batch is the physical truth; a stale value on the loss row must not win.
		inv, cust = self._resolve(
			_inv_row(inventory_type="Regular Stock", customer="Someone Else"),
			{
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "Kanish Ext",
			},
		)
		self.assertEqual(inv, "Customer Goods")
		self.assertEqual(cust, "Kanish Ext")

	def test_regular_stock_drops_stray_batch_customer(self):
		# Defensive coherence: a Regular Stock row never carries a customer, even
		# if the batch record somehow has one.
		inv, cust = self._resolve(
			_inv_row(),
			{"custom_inventory_type": "Regular Stock", "custom_customer": "X"},
		)
		self.assertEqual(inv, "Regular Stock")
		self.assertIsNone(cust)

	def test_row_value_used_when_batch_missing_the_field(self):
		# Batch exists but custom_inventory_type unset -> fall back to the row's
		# own value (e.g. a manually_book_loss_details row the user tagged).
		inv, cust = self._resolve(
			_inv_row(inventory_type="Customer Stock", customer="Kanish Ext"),
			{"custom_inventory_type": None, "custom_customer": None},
		)
		self.assertEqual(inv, "Customer Stock")
		self.assertEqual(cust, "Kanish Ext")

	def test_customer_type_without_customer_downgrades_to_regular(self):
		# Malformed batch: Customer Goods but no customer anywhere. Emitting a
		# customer type with no customer would defeat the scrap-batch guard
		# exemption and hard-fail the submit, so it downgrades to Regular Stock.
		inv, cust = self._resolve(
			_inv_row(),
			{"custom_inventory_type": "Customer Goods", "custom_customer": None},
		)
		self.assertEqual(inv, "Regular Stock")
		self.assertIsNone(cust)

	def test_no_batch_no_defaults_to_regular(self):
		inv, cust = self._resolve(_inv_row(batch_no=None), None)
		self.assertEqual(inv, "Regular Stock")
		self.assertIsNone(cust)


class TestProcessLossCustomerGoodsExemption(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _is_exempt(self, batch, se_type):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.customization.batch."
			"doc_events.utils.frappe"
		) as mock_frappe:
			mock_frappe.db.get_value.return_value = se_type
			return batch_utils.is_process_loss_repack(batch)

	def _batch(self, **fields):
		defaults = {
			"reference_doctype": "Stock Entry",
			"reference_name": "MAT-STE-48001",
			"custom_customer": "Kanish Ext",
			"item": "ML-G-18KT-75.4-Y",
		}
		defaults.update(fields)
		return SimpleNamespace(**defaults)

	def test_process_loss_with_customer_is_exempt(self):
		self.assertTrue(self._is_exempt(self._batch(), "Process Loss"))

	def test_non_stock_entry_not_exempt(self):
		# Returns before any get_value; se_type is irrelevant.
		self.assertFalse(
			self._is_exempt(
				self._batch(reference_doctype="Purchase Receipt"), "Process Loss"
			)
		)

	def test_no_customer_not_exempt(self):
		self.assertFalse(
			self._is_exempt(self._batch(custom_customer=None), "Process Loss")
		)

	def test_other_stock_entry_type_not_exempt(self):
		# A non-loss SE must still be blocked when the item disallows customer goods.
		self.assertFalse(self._is_exempt(self._batch(), "Repack"))
