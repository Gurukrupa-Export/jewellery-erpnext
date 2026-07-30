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
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	main_slip_inject as msi,
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
from jewellery_erpnext.property_setter_guard import (
	ensure_field_precision_property_setters,
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
		# Department Operation the receive belongs to; None configures no
		# finding-category loss gate, so the baseline behaviour is unchanged.
		self.operation = None
		self.employee_ir_operations = ops
		self.employee_loss_details = []
		self.manually_book_loss_details = []
		self.mop_loss_details_total = 0
		# Every real Document carries `flags`; validate_process_loss resets the
		# customer-loss spill collector on it before booking.
		self.flags = frappe._dict()
		self._bml_returns = book_metal_loss_returns or []

	def append(self, table, row):
		assert table == "employee_loss_details"
		self.employee_loss_details.append(frappe._dict(row))

	def book_metal_loss(self, *args, **kwargs):
		# `validate_process_loss` invokes `self.book_metal_loss(...)`; bind it
		# to a stub return so the baseline calculation under test runs in
		# isolation from the proportional-distribution algorithm.
		return self._bml_returns

	def _warn_customer_loss_spill(self):
		# Real method lives on EmployeeIR; the stub only needs it to be callable.
		EmployeeIR._warn_customer_loss_spill(self)


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
				# No finding-category loss gate configured for these baselines.
				self.operation = None

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
			# Pin ONE ownership tier and no reservation cap, so these examples keep
			# asserting the flat proportional split. Without this they would still
			# pass -- batch_priority_map tolerates the MOP Log stub rows above and
			# returns {}, ranking everything as Regular Stock -- but only by
			# accident, and get_batch_sre_headroom would hit the real database.
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.batch_priority_map",
				return_value={},
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.get_batch_sre_headroom",
				return_value={},
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
				# No finding-category loss gate configured for these baselines.
				self.operation = None

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
			# Pin ONE ownership tier and no reservation cap, so these examples keep
			# asserting the flat proportional split. Without this they would still
			# pass -- batch_priority_map tolerates the MOP Log stub rows above and
			# returns {}, ranking everything as Regular Stock -- but only by
			# accident, and get_batch_sre_headroom would hit the real database.
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.batch_priority_map",
				return_value={},
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.get_batch_sre_headroom",
				return_value={},
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

	def test_single_tier_still_splits_proportionally(self):
		"""Explicit regression: with one ownership tier the waterfall IS the old split."""
		rows = [
			frappe._dict(
				{"item_code": "M-G-22KT-91.9-Y", "batch_no": "B1", "qty": 3.0, "pcs": 0}
			),
			frappe._dict(
				{"item_code": "M-G-22KT-91.9-Y", "batch_no": "B2", "qty": 1.0, "pcs": 0}
			),
		]
		result = self._run(rows, gwt=10.0, r_gwt=9.6)
		by_batch = {e["batch_no"]: e for e in result}
		self.assertAlmostEqual(by_batch["B1"]["proportionally_loss"], 0.3, places=3)
		self.assertAlmostEqual(by_batch["B2"]["proportionally_loss"], 0.1, places=3)

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
		# basic_rate is deliberately NOT set by the builder: CustomStockEntry.set_basic_rate
		# assigns it from the consumed rows once ERPNext has resolved their outgoing rates
		# (customization/utils/loss_valuation) -- see test_process_loss_valuation.py.
		self.assertNotIn("basic_rate", produce)
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
		# The rules moved to customization/utils/row_ownership (shared with the
		# tree and warehouse loss builders); loss_stock_entry._resolve_batch_inventory
		# now delegates there, so `frappe` must be patched where the lookup happens.
		with patch(
			"jewellery_erpnext.jewellery_erpnext.customization.utils."
			"row_ownership.frappe"
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


def _eir_msi(**overrides):
	base = dict(
		name="EIR-R-001",
		doctype="Employee IR",
		company="GE",
		department="Trishul - GE",
		employee="EMP-001",
		subcontractor=None,
		subcontracting="No",
		main_slip="MS-001",
		is_main_slip_required=1,
	)
	base.update(overrides)
	return SimpleNamespace(**base)


def _row_msi(**overrides):
	base = dict(
		name="eiro-001",
		manufacturing_operation="MOP-1",
		manufacturing_work_order="MWO-1",
		gross_wt=10.0,
		received_gross_wt=12.0,
	)
	base.update(overrides)
	return SimpleNamespace(**base)


class TestMainSlipInjectGate(IntegrationTestCase):
	def test_skips_when_is_main_slip_required_false(self):
		eir = _eir_msi(is_main_slip_required=0)
		row = _row_msi()
		with patch.object(msi, "_existing_injection_se") as mock_exists:
			out = msi.inject_extra_metal_for_eir_receive(eir, row)
		self.assertEqual(out, [])
		mock_exists.assert_not_called()

	def test_skips_when_no_extra_qty(self):
		eir = _eir_msi()
		row = _row_msi(received_gross_wt=10.0, gross_wt=10.0)
		with patch.object(msi, "_existing_injection_se") as mock_exists:
			out = msi.inject_extra_metal_for_eir_receive(eir, row)
		self.assertEqual(out, [])
		mock_exists.assert_not_called()

	def test_skips_when_negative_delta(self):
		eir = _eir_msi()
		row = _row_msi(received_gross_wt=8.0, gross_wt=10.0)
		with patch.object(msi, "_existing_injection_se") as mock_exists:
			out = msi.inject_extra_metal_for_eir_receive(eir, row)
		self.assertEqual(out, [])
		mock_exists.assert_not_called()


class TestMainSlipInjectIdempotency(IntegrationTestCase):
	def test_existing_repack_se_short_circuits(self):
		eir = _eir_msi()
		row = _row_msi()
		with (
			patch.object(msi, "_resolve_department_warehouse", return_value="WH-Dept"),
			patch.object(
				msi, "_existing_injection_se", return_value=True
			) as mock_exists,
			patch.object(msi, "_resolve_inject_metal_items") as mock_resolve,
			patch.object(msi, "_inject_via_main_slip_batches") as mock_inject_ms,
		):
			out = msi.inject_extra_metal_for_eir_receive(eir, row)
		self.assertEqual(out, [])
		mock_exists.assert_called_once()
		mock_resolve.assert_not_called()
		mock_inject_ms.assert_not_called()


_MSI_PATH = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject"


class TestMainSlipInjectMetalResolution(IntegrationTestCase):
	@patch(f"{_MSI_PATH}.get_item_from_attribute")
	@patch(f"{_MSI_PATH}.frappe.get_cached_value")
	def test_single_colour_emits_one_item(self, mock_get_value, mock_get_item):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": "18KT",
			"metal_purity": "75.4",
			"metal_colour": "Yellow",
			"multicolour": 0,
			"allowed_colours": "",
		}
		mock_get_item.return_value = "M-G-18KT-Y"
		items = msi._resolve_inject_metal_items("MWO-1", 2.0)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["item_code"], "M-G-18KT-Y")
		self.assertEqual(items[0]["qty"], 2.0)

	@patch(f"{_MSI_PATH}.get_item_from_attribute")
	@patch(f"{_MSI_PATH}.frappe.get_cached_value")
	def test_multicolour_even_splits(self, mock_get_value, mock_get_item):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": "18KT",
			"metal_purity": "75.4",
			"metal_colour": None,
			"multicolour": 1,
			"allowed_colours": "Yellow, White, Rose",
		}

		def _item_for(mt, mtc, mp, colour):
			return f"M-G-18KT-{colour[0]}"

		mock_get_item.side_effect = _item_for
		items = msi._resolve_inject_metal_items("MWO-1", 3.0)
		self.assertEqual(len(items), 3)
		self.assertEqual(
			[i["item_code"] for i in items], ["M-G-18KT-Y", "M-G-18KT-W", "M-G-18KT-R"]
		)
		for i in items:
			self.assertAlmostEqual(i["qty"], 1.0)

	@patch(f"{_MSI_PATH}.frappe.get_cached_value")
	def test_throws_when_mwo_missing_attributes(self, mock_get_value):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": None,
			"metal_purity": "75.4",
			"metal_colour": "Yellow",
			"multicolour": 0,
			"allowed_colours": "",
		}
		with self.assertRaises(frappe.ValidationError):
			msi._resolve_inject_metal_items("MWO-1", 2.0)

	@patch(f"{_MSI_PATH}.get_item_from_attribute", return_value=None)
	@patch(f"{_MSI_PATH}.frappe.get_cached_value")
	def test_throws_when_metal_item_not_resolvable(self, mock_get_value, mock_get_item):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": "18KT",
			"metal_purity": "75.4",
			"metal_colour": "Yellow",
			"multicolour": 0,
			"allowed_colours": "",
		}
		with self.assertRaises(frappe.ValidationError):
			msi._resolve_inject_metal_items("MWO-1", 2.0)


class TestMainSlipInjectWarehouseResolution(IntegrationTestCase):
	@patch(f"{_MSI_PATH}.frappe.get_cached_value")
	def test_employee_warehouse_for_non_subcontracting(self, mock_get_value):
		mock_get_value.return_value = "WH-Emp"
		out = msi._resolve_source_warehouse(
			_eir_msi(subcontracting="No", employee="EMP-7")
		)
		self.assertEqual(out, "WH-Emp")
		args = mock_get_value.call_args
		self.assertEqual(args[0][0], "Warehouse")
		self.assertEqual(args[0][1].get("employee"), "EMP-7")
		self.assertNotIn("subcontractor", args[0][1])

	@patch(f"{_MSI_PATH}.frappe.get_cached_value")
	def test_subcontractor_warehouse_for_subcontracting(self, mock_get_value):
		mock_get_value.return_value = "WH-Sub"
		out = msi._resolve_source_warehouse(
			_eir_msi(subcontracting="Yes", subcontractor="SUB-1", employee=None)
		)
		self.assertEqual(out, "WH-Sub")
		args = mock_get_value.call_args
		self.assertEqual(args[0][1].get("subcontractor"), "SUB-1")


class TestMainSlipInjectStockCheck(IntegrationTestCase):
	@patch(
		f"{_MSI_PATH}.frappe.db.get_all",
		return_value=[{"item_code": "M-G-18KT-Y", "actual_qty": 0.5}],
	)
	def test_insufficient_stock_throws(self, mock_get_all):
		transfer_segs = [{"item_code": "M-G-18KT-Y", "qty": 2.0}]
		with self.assertRaises(frappe.ValidationError):
			msi._validate_fallback_segments_against_source_bin(
				transfer_segs, [], "WH-Emp"
			)

	@patch(
		f"{_MSI_PATH}.frappe.db.get_all",
		return_value=[{"item_code": "M-G-18KT-Y", "actual_qty": 50.0}],
	)
	def test_sufficient_stock_passes_silently(self, mock_get_all):
		transfer_segs = [{"item_code": "M-G-18KT-Y", "qty": 2.0}]
		# No exception = pass.
		msi._validate_fallback_segments_against_source_bin(transfer_segs, [], "WH-Emp")


class TestFallbackInjectSegments(IntegrationTestCase):
	@patch(f"{_MSI_PATH}._resolve_source_warehouse_raw_material", return_value="WH-Emp")
	@patch(f"{_MSI_PATH}._get_bin_qty", return_value=100.0)
	@patch(f"{_MSI_PATH}.get_item_from_attribute", return_value="M-G-18KT-Y")
	@patch(f"{_MSI_PATH}.frappe.get_cached_value")
	def test_sufficient_alloy_stock_uses_transfer_mode(self, mock_get_value, *_):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": "18KT",
			"metal_purity": "75.4",
			"metal_colour": "Yellow",
			"multicolour": 0,
			"allowed_colours": "",
		}
		segs = msi._resolve_fallback_inject_segments(
			_eir_msi(main_slip=None), "MWO-1", 2.0, "WH-Dept"
		)
		self.assertEqual(len(segs), 1)
		self.assertEqual(segs[0]["mode"], "transfer")
		self.assertEqual(segs[0]["item_code"], "M-G-18KT-Y")
		self.assertEqual(segs[0]["qty"], 2.0)

	@patch(f"{_MSI_PATH}._resolve_source_warehouse_raw_material", return_value="WH-Emp")
	@patch(f"{_MSI_PATH}._get_bin_qty", return_value=0.0)
	@patch(f"{_MSI_PATH}.get_item_from_attribute", return_value="M-G-18KT-Y")
	@patch(f"{_MSI_PATH}._pure_metal_item_for_mwo", return_value="M-G-24KT")
	@patch(f"{_MSI_PATH}._get_item_metal_purity")
	@patch(f"{_MSI_PATH}.frappe.get_cached_value")
	def test_no_alloy_stock_uses_purity_mode(self, mock_get_value, mock_purity, *_):
		mock_get_value.return_value = {
			"metal_type": "Gold",
			"metal_touch": "18KT",
			"metal_purity": "75.4",
			"metal_colour": "Yellow",
			"multicolour": 0,
			"allowed_colours": "",
		}

		def _purity(item):
			return {"M-G-24KT": 99.9, "M-G-18KT-Y": 75.4}.get(item)

		mock_purity.side_effect = lambda ic: _purity(ic)

		segs = msi._resolve_fallback_inject_segments(
			_eir_msi(main_slip=None), "MWO-1", 2.0, "WH-Dept"
		)
		self.assertEqual(len(segs), 1)
		self.assertEqual(segs[0]["mode"], "purity")
		self.assertEqual(segs[0]["source_item"], "M-G-24KT")
		self.assertEqual(segs[0]["target_item"], "M-G-18KT-Y")
		self.assertEqual(segs[0]["produce_qty"], 2.0)
		self.assertAlmostEqual(
			segs[0]["consume_qty"], round(2.0 * 75.4 / 99.9, 3), places=3
		)

	@patch(f"{_MSI_PATH}._resolve_source_warehouse_raw_material", return_value="WH-Emp")
	def test_partial_alloy_emits_transfer_plus_purity(self, mock_src_wh):
		mwo_row = {
			"metal_type": "Gold",
			"metal_touch": "18KT",
			"metal_purity": "75.4",
			"metal_colour": "Yellow",
			"multicolour": 0,
			"allowed_colours": "",
		}

		def _bin_qty(item_code, wh):
			if item_code == "M-G-18KT-Y":
				return 1.0
			return 0.0

		with (
			patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value=mwo_row),
			patch(f"{_MSI_PATH}.get_item_from_attribute", return_value="M-G-18KT-Y"),
			patch(f"{_MSI_PATH}._get_bin_qty", side_effect=_bin_qty),
			patch(f"{_MSI_PATH}._pure_metal_item_for_mwo", return_value="M-G-24KT"),
			patch(f"{_MSI_PATH}._get_item_metal_purity") as mock_purity,
		):

			def _purity(item):
				return {"M-G-24KT": 99.9, "M-G-18KT-Y": 75.4}.get(item)

			mock_purity.side_effect = lambda ic: _purity(ic)
			segs = msi._resolve_fallback_inject_segments(
				_eir_msi(main_slip=None), "MWO-1", 2.0, "WH-Dept"
			)

		self.assertEqual(len(segs), 2)
		self.assertEqual(segs[0]["mode"], "transfer")
		self.assertEqual(segs[0]["qty"], 1.0)
		self.assertEqual(segs[1]["mode"], "purity")
		self.assertEqual(segs[1]["produce_qty"], 1.0)


class TestBuildStockEntryFromFallbackSegments(IntegrationTestCase):
	@patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value="PMO-1")
	@patch(f"{_MSI_PATH}.frappe.new_doc")
	def test_material_transfer_segments_stamped_to_mop(
		self, mock_new_doc, mock_get_value
	):
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		eir = _eir_msi(subcontracting="No", employee="EMP-7", main_slip=None)
		row = _row_msi(manufacturing_operation="MOP-ABC")
		segments = [
			{
				"mode": "transfer",
				"item_code": "M-G-18KT-Y",
				"qty": 1.5,
				"s_warehouse": "WH-Emp",
				"t_warehouse": "WH-Dept",
			}
		]
		msi._build_material_transfer_from_segments(
			eir, row, segments, "WH-Emp", "WH-Dept"
		)

		self.assertEqual(se.stock_entry_type, "Material Transfer (WORK ORDER)")
		self.assertEqual(se.employee_ir, "EIR-R-001")
		self.assertEqual(len(se.items), 1)
		self.assertEqual(se.items[0]["s_warehouse"], "WH-Emp")
		self.assertEqual(se.items[0]["t_warehouse"], "WH-Dept")
		self.assertEqual(se.items[0]["manufacturing_operation"], "MOP-ABC")

	@patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value="PMO-1")
	@patch(f"{_MSI_PATH}.frappe.new_doc")
	def test_repack_purity_segments_consume_produce_stamped_to_mop(
		self, mock_new_doc, mock_get_value
	):
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		eir = _eir_msi(subcontracting="No", employee="EMP-7", main_slip=None)
		row = _row_msi(manufacturing_operation="MOP-ABC")
		segments = [
			{
				"mode": "purity",
				"source_item": "M-G-24KT",
				"target_item": "M-G-18KT-Y",
				"consume_qty": 1.5,
				"produce_qty": 1.0,
				"s_warehouse": "WH-Emp",
				"t_warehouse": "WH-Dept",
			}
		]
		msi._build_repack_from_purity_segments(eir, row, segments, "WH-Emp", "WH-Dept")

		self.assertEqual(se.stock_entry_type, "Repack")
		self.assertEqual(se.employee_ir, "EIR-R-001")
		self.assertEqual(len(se.items), 2)
		self.assertEqual(se.items[0]["s_warehouse"], "WH-Emp")
		self.assertIsNone(se.items[0].get("t_warehouse"))
		self.assertEqual(se.items[1]["t_warehouse"], "WH-Dept")
		self.assertEqual(se.items[1]["manufacturing_operation"], "MOP-ABC")
		self.assertEqual(se.items[1]["custom_manufacturing_work_order"], "MWO-1")


class TestMainSlipInjectCancel(IntegrationTestCase):
	@patch(f"{_MSI_PATH}.frappe.get_doc")
	@patch(f"{_MSI_PATH}.frappe.db.get_all", return_value=["SE-AUTO-1", "SE-AUTO-2"])
	def test_cancel_cancels_every_auto_se(self, mock_get_all, mock_get_doc):
		docs = [MagicMock(), MagicMock()]
		mock_get_doc.side_effect = docs
		out = msi.cancel_injections_for_eir("EIR-R-001")
		self.assertEqual(out, ["SE-AUTO-1", "SE-AUTO-2"])
		for d in docs:
			d.cancel.assert_called_once()
		# Filter now matches both Repack and Material Transfer (WORK ORDER).
		filters = mock_get_all.call_args[1]["filters"]
		self.assertIn("auto_created", filters)
		self.assertEqual(
			set(filters["stock_entry_type"][1]),
			{"Repack", "Material Transfer (WORK ORDER)"},
		)


# ---------------------------------------------------------------------------
# Main Slip batch-walking path
# ---------------------------------------------------------------------------


def _batch_row(**overrides):
	base = dict(
		name="MSSED-1",
		batch_no="BATCH-1",
		item_code="M-G-18KT-Y",
		qty=5.0,
		consume_qty=0.0,
		inventory_type="Regular Stock",
		customer=None,
		variant_of="M",
		creation="2026-04-19 09:00:00",
	)
	base.update(overrides)
	return base


class TestMainSlipBatchIterator(IntegrationTestCase):
	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	def test_priority_order_customer_then_regular_then_pure(self, mock_get_all):
		"""Consumption draws the customer's own metal down before the company's.

		Inverted from the original Regular -> Customer -> Pure order: the walk now
		uses the shared ``ownership_priority.CONSUME_PRIORITY``. Creation dates are
		deliberately the reverse of the expected order, so only the ownership rank
		can produce this result.
		"""
		mock_get_all.return_value = [
			_batch_row(
				name="B-PURE",
				inventory_type="Pure Metal",
				creation="2026-04-01 00:00:00",
			),
			_batch_row(
				name="B-REG",
				inventory_type="Regular Stock",
				creation="2026-04-02 00:00:00",
			),
			_batch_row(
				name="B-CUST",
				inventory_type="Customer Goods",
				creation="2026-04-03 00:00:00",
			),
		]
		ordered = [r["name"] for r in msi._iter_main_slip_batches("MS-001")]
		self.assertEqual(ordered, ["B-CUST", "B-REG", "B-PURE"])

	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	def test_customer_stock_ranks_with_customer_goods(self, mock_get_all):
		"""``Customer Stock`` used to fall to the old dict's ``99`` default.

		The retired module-local INVENTORY_TYPE_PRIORITY had no key for it, so it
		sorted behind even Pure Metal. CONSUME_PRIORITY ranks it with Customer Goods.
		"""
		mock_get_all.return_value = [
			_batch_row(
				name="B-PURE",
				inventory_type="Pure Metal",
				creation="2026-04-01 00:00:00",
			),
			_batch_row(
				name="B-CSTOCK",
				inventory_type="Customer Stock",
				creation="2026-04-02 00:00:00",
			),
			_batch_row(
				name="B-REG",
				inventory_type="Regular Stock",
				creation="2026-04-03 00:00:00",
			),
		]
		ordered = [r["name"] for r in msi._iter_main_slip_batches("MS-001")]
		self.assertEqual(ordered, ["B-CSTOCK", "B-REG", "B-PURE"])

	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	def test_skips_rows_with_no_available_qty(self, mock_get_all):
		mock_get_all.return_value = [
			_batch_row(name="B-FULL", qty=2.0, consume_qty=2.0),
			_batch_row(name="B-OK", qty=3.0, consume_qty=0.5),
		]
		ordered = [r["name"] for r in msi._iter_main_slip_batches("MS-001")]
		self.assertEqual(ordered, ["B-OK"])


class TestMainSlipInjectViaBatches(IntegrationTestCase):
	def _setup_common_mocks(self, stack, target_item="M-G-18KT-Y"):
		"""Patch the helpers used by _inject_via_main_slip_batches and return
		a submit-recorder that logs each SE that was saved+submitted."""
		submitted = []

		def _recorder(se):
			se.flags = getattr(se, "flags", MagicMock())
			se.save = MagicMock()
			se.submit = MagicMock()
			se.name = f"SE-AUTO-{len(submitted) + 1}"
			submitted.append(se)
			return se

		# inject_extra_metal_for_eir_receive resolves target items and dept wh
		stack.append(
			patch(
				f"{_MSI_PATH}._resolve_inject_metal_items",
				return_value=[{"item_code": target_item, "qty": 2.0}],
			)
		)
		stack.append(
			patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept")
		)
		stack.append(
			patch(
				f"{_MSI_PATH}._resolve_source_warehouse_raw_material",
				return_value="WH-Src",
			)
		)
		stack.append(patch(f"{_MSI_PATH}._existing_injection_se", return_value=False))
		return submitted

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_regular_stock_emits_material_transfer(self, mock_iter, mock_new_doc):
		mock_iter.return_value = iter(
			[
				_batch_row(
					item_code="M-G-18KT-Y",
					inventory_type="Regular Stock",
					qty=3.0,
					consume_qty=0.0,
				)
				| {"available_qty": 3.0},
			]
		)
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		with (
			patch(
				f"{_MSI_PATH}._resolve_inject_metal_items",
				return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}],
			),
			patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"),
			patch(
				f"{_MSI_PATH}._resolve_source_warehouse_raw_material",
				return_value="WH-Src",
			),
			patch(f"{_MSI_PATH}._existing_injection_se", return_value=False),
			patch(f"{_MSI_PATH}._apply_fifo_batches_to_stock_entry"),
			patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value="PMO-1"),
		):
			out = msi.inject_extra_metal_for_eir_receive(
				_eir_msi(main_slip="MS-1"), _row_msi()
			)

		self.assertEqual(len(out), 1)
		self.assertEqual(se.stock_entry_type, "Material Transfer (WORK ORDER)")
		# One SE with one item row, stamped to MOP.
		self.assertEqual(len(se.items), 1)
		self.assertEqual(se.items[0]["qty"], 2.0)
		self.assertEqual(se.items[0]["t_warehouse"], "WH-Dept")
		self.assertEqual(
			se.items[0]["manufacturing_operation"], _row_msi().manufacturing_operation
		)

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_subcontracting_pure_metal_emits_repack_with_purity_conversion(
		self, mock_iter, mock_new_doc
	):
		# Source 24KT pure, target 18KT (75%) alloy.
		mock_iter.return_value = iter(
			[
				_batch_row(
					item_code="M-G-24KT",
					inventory_type="Pure Metal",
					qty=5.0,
					consume_qty=0.0,
				)
				| {"available_qty": 5.0},
			]
		)
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		def _fake_cached_value(doctype, filters_or_name, fieldname=None, **_kw):
			if doctype == "Item Variant Attribute":
				if filters_or_name.get("parent") == "M-G-24KT":
					return "99.9"
				if filters_or_name.get("parent") == "M-G-18KT-Y":
					return "75.4"
			if doctype == "Manufacturing Work Order":
				return "PMO-1"
			return None

		with (
			patch(
				f"{_MSI_PATH}._resolve_inject_metal_items",
				return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}],
			),
			patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"),
			patch(
				f"{_MSI_PATH}._resolve_source_warehouse_raw_material",
				return_value="WH-Sub",
			),
			patch(f"{_MSI_PATH}._existing_injection_se", return_value=False),
			patch(f"{_MSI_PATH}._apply_fifo_batches_to_stock_entry"),
			patch(
				f"{_MSI_PATH}.frappe.get_cached_value", side_effect=_fake_cached_value
			),
		):
			out = msi.inject_extra_metal_for_eir_receive(
				_eir_msi(
					main_slip="MS-1",
					subcontracting="Yes",
					subcontractor="SUB-1",
					employee=None,
				),
				_row_msi(),
			)

		self.assertEqual(len(out), 1)
		self.assertEqual(se.stock_entry_type, "Repack")
		self.assertEqual(len(se.items), 2)
		consume = se.items[0]
		produce = se.items[1]
		self.assertEqual(consume["item_code"], "M-G-24KT")
		self.assertEqual(consume["s_warehouse"], "WH-Sub")
		self.assertEqual(produce["item_code"], "M-G-18KT-Y")
		self.assertEqual(produce["t_warehouse"], "WH-Dept")
		self.assertEqual(
			produce["manufacturing_operation"], _row_msi().manufacturing_operation
		)
		# produce_qty = consume_qty * 75.4 / 99.9 -> we requested 2.0 produced
		# so consume should be 2.0 * 75.4 / 99.9
		self.assertAlmostEqual(produce["qty"], 2.0, places=3)
		self.assertAlmostEqual(consume["qty"], round(2.0 * 75.4 / 99.9, 3), places=3)

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_insufficient_batches_throws(self, mock_iter, mock_new_doc):
		# Only 1g available but we need 2g.
		mock_iter.return_value = iter(
			[
				_batch_row(
					item_code="M-G-18KT-Y",
					inventory_type="Regular Stock",
					qty=1.0,
					consume_qty=0.0,
				)
				| {"available_qty": 1.0},
			]
		)
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		with (
			patch(
				f"{_MSI_PATH}._resolve_inject_metal_items",
				return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}],
			),
			patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"),
			patch(
				f"{_MSI_PATH}._resolve_source_warehouse_raw_material",
				return_value="WH-Src",
			),
			patch(f"{_MSI_PATH}._existing_injection_se", return_value=False),
			patch(f"{_MSI_PATH}._apply_fifo_batches_to_stock_entry"),
			patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value="PMO-1"),
		):
			with self.assertRaises(frappe.ValidationError):
				msi.inject_extra_metal_for_eir_receive(
					_eir_msi(main_slip="MS-1"), _row_msi()
				)

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_batch_item_mismatch_skips_non_pure_batches(self, mock_iter, mock_new_doc):
		# Wrong alloy in Main Slip and nothing matching -> throws short.
		mock_iter.return_value = iter(
			[
				_batch_row(
					item_code="M-G-22KT-Y",
					inventory_type="Regular Stock",
					qty=5.0,
					consume_qty=0.0,
				)
				| {"available_qty": 5.0},
			]
		)
		se = MagicMock()
		mock_new_doc.return_value = se
		with (
			patch(
				f"{_MSI_PATH}._resolve_inject_metal_items",
				return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}],
			),
			patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"),
			patch(
				f"{_MSI_PATH}._resolve_source_warehouse_raw_material",
				return_value="WH-Src",
			),
			patch(f"{_MSI_PATH}._existing_injection_se", return_value=False),
			patch(f"{_MSI_PATH}._apply_fifo_batches_to_stock_entry"),
			patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value="PMO-1"),
		):
			with self.assertRaises(frappe.ValidationError):
				msi.inject_extra_metal_for_eir_receive(
					_eir_msi(main_slip="MS-1"), _row_msi()
				)

	@patch(f"{_MSI_PATH}.frappe.new_doc")
	@patch(f"{_MSI_PATH}._iter_main_slip_batches")
	def test_non_subcontracting_pure_metal_falls_to_material_transfer(
		self, mock_iter, mock_new_doc
	):
		# Non-subcontracting: Pure Metal is treated as a direct Material Transfer
		# (no purity conversion). Pure Metal item MUST match target item; if it
		# does not, the batch is skipped and the helper throws short.
		mock_iter.return_value = iter(
			[
				_batch_row(
					item_code="M-G-18KT-Y",
					inventory_type="Pure Metal",
					qty=3.0,
					consume_qty=0.0,
				)
				| {"available_qty": 3.0},
			]
		)
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _, payload: se.items.append(payload)
		mock_new_doc.return_value = se

		with (
			patch(
				f"{_MSI_PATH}._resolve_inject_metal_items",
				return_value=[{"item_code": "M-G-18KT-Y", "qty": 2.0}],
			),
			patch(f"{_MSI_PATH}._resolve_department_warehouse", return_value="WH-Dept"),
			patch(
				f"{_MSI_PATH}._resolve_source_warehouse_raw_material",
				return_value="WH-Emp",
			),
			patch(f"{_MSI_PATH}._existing_injection_se", return_value=False),
			patch(f"{_MSI_PATH}._apply_fifo_batches_to_stock_entry"),
			patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value="PMO-1"),
		):
			out = msi.inject_extra_metal_for_eir_receive(
				_eir_msi(main_slip="MS-1", subcontracting="No"), _row_msi()
			)
		self.assertEqual(len(out), 1)
		self.assertEqual(se.stock_entry_type, "Material Transfer (WORK ORDER)")


# ---------------------------------------------------------------------------
# Destination-reservable FIFO batch selection
# ---------------------------------------------------------------------------


class TestSelectFifoBatchesReservableAtDest(IntegrationTestCase):
	def _se(self):
		return SimpleNamespace(posting_date="2026-06-04", posting_time="10:00:00")

	@patch(f"{_MSI_PATH}.frappe.db.sql")
	@patch(f"{_MSI_PATH}.get_batch_qty")
	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	@patch(f"{_MSI_PATH}.capped_auto_batch_nos")
	def test_skips_over_reserved_batch_and_picks_safe(
		self, mock_auto, mock_get_all, mock_batch_qty, mock_sql
	):
		# FIFO-first batch (oldest) holds the bulk of the source stock but is
		# over-reserved at the destination; the next batches are reservable.
		mock_auto.return_value = [
			frappe._dict(batch_no="B-OLD", qty=37.0),
			frappe._dict(batch_no="B-SAFE1", qty=0.5),
			frappe._dict(batch_no="B-SAFE2", qty=0.5),
		]
		mock_get_all.return_value = [
			{"name": "B-OLD", "creation": "2026-01-01 00:00:00"},
			{"name": "B-SAFE1", "creation": "2026-02-01 00:00:00"},
			{"name": "B-SAFE2", "creation": "2026-03-01 00:00:00"},
		]
		mock_batch_qty.return_value = []  # nothing of this item physically at dest
		mock_sql.return_value = [{"batch_no": "B-OLD", "qty": 3.8}]  # over-reserved

		out = msi._select_fifo_batches_reservable_at_dest(
			self._se(), "ITEM-1", "WH-Src", "WH-Dest", 0.035
		)
		self.assertIsNotNone(out)
		self.assertEqual([a.batch_no for a in out], ["B-SAFE1"])
		self.assertAlmostEqual(out[0].qty, 0.035)
		# Destination actual was queried ignoring reservations.
		self.assertEqual(mock_batch_qty.call_args[1]["warehouse"], "WH-Dest")
		self.assertTrue(mock_batch_qty.call_args[1]["ignore_reserved_stock"])

	@patch(f"{_MSI_PATH}.frappe.db.sql")
	@patch(f"{_MSI_PATH}.get_batch_qty")
	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	@patch(f"{_MSI_PATH}.capped_auto_batch_nos")
	def test_spills_across_multiple_safe_batches(
		self, mock_auto, mock_get_all, mock_batch_qty, mock_sql
	):
		mock_auto.return_value = [
			frappe._dict(batch_no="B-SAFE1", qty=0.02),
			frappe._dict(batch_no="B-SAFE2", qty=0.05),
		]
		mock_get_all.return_value = [
			{"name": "B-SAFE1", "creation": "2026-02-01 00:00:00"},
			{"name": "B-SAFE2", "creation": "2026-03-01 00:00:00"},
		]
		mock_batch_qty.return_value = []
		mock_sql.return_value = []

		out = msi._select_fifo_batches_reservable_at_dest(
			self._se(), "ITEM-1", "WH-Src", "WH-Dest", 0.035
		)
		self.assertEqual([a.batch_no for a in out], ["B-SAFE1", "B-SAFE2"])
		self.assertAlmostEqual(out[0].qty, 0.02)
		self.assertAlmostEqual(out[1].qty, 0.015)

	@patch(f"{_MSI_PATH}.frappe.db.sql")
	@patch(f"{_MSI_PATH}.get_batch_qty")
	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	@patch(f"{_MSI_PATH}.capped_auto_batch_nos")
	def test_returns_none_when_no_safe_batch_covers_need(
		self, mock_auto, mock_get_all, mock_batch_qty, mock_sql
	):
		mock_auto.return_value = [frappe._dict(batch_no="B-OLD", qty=37.0)]
		mock_get_all.return_value = [
			{"name": "B-OLD", "creation": "2026-01-01 00:00:00"}
		]
		mock_batch_qty.return_value = []
		mock_sql.return_value = [{"batch_no": "B-OLD", "qty": 3.8}]

		out = msi._select_fifo_batches_reservable_at_dest(
			self._se(), "ITEM-1", "WH-Src", "WH-Dest", 0.035
		)
		self.assertIsNone(out)

	@patch(f"{_MSI_PATH}.capped_auto_batch_nos", return_value=[])
	def test_returns_none_when_no_source_batches(self, _mock_auto):
		out = msi._select_fifo_batches_reservable_at_dest(
			self._se(), "ITEM-1", "WH-Src", "WH-Dest", 0.035
		)
		self.assertIsNone(out)


class TestExpandSourceRowsPrefersReservableBatch(IntegrationTestCase):
	@patch(f"{_MSI_PATH}._select_fifo_batches_reservable_at_dest")
	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	@patch(f"{_MSI_PATH}.capped_auto_batch_nos")
	@patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value=1)
	def test_transfer_row_uses_dest_reservable_selection(
		self, _mock_item, mock_auto, mock_get_all, mock_select
	):
		# Plain FIFO would pick the over-reserved batch; the dest-aware selector
		# overrides it with a reservable one.
		mock_auto.return_value = [frappe._dict(batch_no="B-OLD", qty=0.035)]
		mock_select.return_value = [frappe._dict(batch_no="B-SAFE1", qty=0.035)]
		mock_get_all.return_value = [
			{
				"name": "B-SAFE1",
				"custom_inventory_type": "Regular Stock",
				"custom_customer": None,
			}
		]

		se = SimpleNamespace(posting_date="2026-06-04", posting_time="10:00:00")
		row = {
			"item_code": "ITEM-1",
			"s_warehouse": "WH-Src",
			"t_warehouse": "WH-Dest",
			"qty": 0.035,
			"serial_no": None,
			"batch_no": None,
		}
		out = msi._expand_source_rows_for_fifo(se, row)
		self.assertEqual([r["batch_no"] for r in out], ["B-SAFE1"])
		self.assertEqual(out[0]["qty"], 0.035)
		mock_select.assert_called_once()

	@patch(f"{_MSI_PATH}._select_fifo_batches_reservable_at_dest")
	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	@patch(f"{_MSI_PATH}.capped_auto_batch_nos")
	@patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value=1)
	def test_falls_back_to_plain_fifo_when_selector_returns_none(
		self, _mock_item, mock_auto, mock_get_all, mock_select
	):
		mock_auto.return_value = [frappe._dict(batch_no="B-OLD", qty=0.035)]
		mock_select.return_value = None  # no reservable allocation -> fall back
		mock_get_all.return_value = [
			{
				"name": "B-OLD",
				"custom_inventory_type": "Regular Stock",
				"custom_customer": None,
			}
		]

		se = SimpleNamespace(posting_date="2026-06-04", posting_time="10:00:00")
		row = {
			"item_code": "ITEM-1",
			"s_warehouse": "WH-Src",
			"t_warehouse": "WH-Dest",
			"qty": 0.035,
			"serial_no": None,
			"batch_no": None,
		}
		out = msi._expand_source_rows_for_fifo(se, row)
		self.assertEqual([r["batch_no"] for r in out], ["B-OLD"])
		mock_select.assert_called_once()

	@patch(f"{_MSI_PATH}._select_fifo_batches_reservable_at_dest", return_value=None)
	@patch(f"{_MSI_PATH}.frappe.db.get_all")
	@patch(f"{_MSI_PATH}.capped_auto_batch_nos")
	@patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value=1)
	def test_phantom_batch_dropped_allocates_from_real_batches(
		self, _mock_item, mock_auto, mock_get_all, _mock_select
	):
		# capped_auto_batch_nos has already dropped the phantom (orphan-inflated)
		# batch, so the allocator only ever sees real batches and spreads `need`
		# across them instead of over-committing an empty batch and blowing up with
		# BatchNegativeStockError at submit.
		mock_auto.return_value = [
			frappe._dict(batch_no="B-REAL1", qty=0.02),
			frappe._dict(batch_no="B-REAL2", qty=0.015),
		]
		mock_get_all.return_value = [
			{
				"name": "B-REAL1",
				"custom_inventory_type": "Regular Stock",
				"custom_customer": None,
			},
			{
				"name": "B-REAL2",
				"custom_inventory_type": "Regular Stock",
				"custom_customer": None,
			},
		]
		se = SimpleNamespace(posting_date="2026-06-04", posting_time="10:00:00")
		row = {
			"item_code": "ITEM-1",
			"s_warehouse": "WH-Src",
			"t_warehouse": "WH-Dest",
			"qty": 0.035,
			"serial_no": None,
			"batch_no": None,
		}
		out = msi._expand_source_rows_for_fifo(se, row)
		self.assertEqual([r["batch_no"] for r in out], ["B-REAL1", "B-REAL2"])
		self.assertAlmostEqual(sum(r["qty"] for r in out), 0.035)

	@patch(f"{_MSI_PATH}._select_fifo_batches_reservable_at_dest", return_value=None)
	@patch(f"{_MSI_PATH}.capped_auto_batch_nos")
	@patch(f"{_MSI_PATH}.frappe.get_cached_value", return_value=1)
	def test_throws_cleanly_when_real_batches_short(
		self, _mock_item, mock_auto, _mock_select
	):
		# After the phantom is dropped, real stock genuinely cannot cover `need`:
		# expect the clean "insufficient FIFO batch stock" shortfall throw, NOT a raw
		# BatchNegativeStockError surfacing later at SLE submit.
		mock_auto.return_value = [frappe._dict(batch_no="B-REAL1", qty=0.01)]
		se = SimpleNamespace(posting_date="2026-06-04", posting_time="10:00:00")
		row = {
			"item_code": "ITEM-1",
			"s_warehouse": "WH-Src",
			"t_warehouse": "WH-Dest",
			"qty": 0.035,
			"serial_no": None,
			"batch_no": None,
		}
		with self.assertRaises(frappe.ValidationError):
			msi._expand_source_rows_for_fifo(se, row)


# from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
# 	_find_sre,
# )

_LOSS = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry"
_EOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync"


def _sre_row_heal(warehouse="WH-X"):
	return {
		"name": "SRE-NEW",
		"warehouse": warehouse,
		"reserved_qty": 5.0,
		"available_qty": 5.0,
		"voucher_qty": 100.0,
		"reservation_based_on": "Serial and Batch",
		"has_batch_no": 1,
		"company": "Co",
		"voucher_type": "Sales Order",
		"voucher_no": "SO-1",
		"voucher_detail_no": "SOI-1",
		"stock_uom": "Gram",
		"manufacturing_operation": "MOP-1",
	}


class TestWFindSreSelfHeal(IntegrationTestCase):
	"""_find_sre self-heals an EOD-orphaned WIP reservation instead of throwing.

	When EOD sync cancels a batch's source SREs and silently skips re-reserving (v16 SBB
	batch stock parked at a non-target warehouse), zero active SREs remain and Process Loss
	used to hard-fail with "No active Stock Reservation Entry found". _find_sre now re-creates
	the reservation at the batch's physical warehouse and re-queries; the loss then consumes
	from that warehouse (s_warehouse = sre_doc.warehouse), never negative.
	"""

	def _row(self):
		return SimpleNamespace(
			item_code="M-1", batch_no="B1", manufacturing_operation="MOP-1", idx=5
		)

	def _eir(self):
		return SimpleNamespace(name="EIR-1", company="Co")

	def test_orphaned_reservation_is_healed_and_consumes_from_physical_wh(self):
		# First lookup empty; after the heal the re-query returns the new SRE at WH-X.
		with patch(
			f"{_LOSS}._query_batch_and_qty_sres",
			side_effect=[[], [_sre_row_heal("WH-X")]],
		), patch(
			f"{_EOD}._reserve_batch_at_physical_warehouse", return_value=["SRE-NEW"]
		) as heal, patch(
			f"{_LOSS}.frappe.get_doc",
			return_value=SimpleNamespace(name="SRE-NEW", warehouse="WH-X"),
		):
			sre_doc, candidates = loss_stock_entry._find_sre(
				self._eir(), self._row(), "MWO-1", "employee_loss_details", 0.01
			)

		heal.assert_called_once_with("MWO-1", "M-1", "B1", 0.01, "MOP-1", "Co")
		# The loss will consume from the healed warehouse (physical truth), not negative stock.
		self.assertEqual(sre_doc.warehouse, "WH-X")
		self.assertEqual(candidates[0]["warehouse"], "WH-X")

	def test_still_missing_after_heal_throws_original_error(self):
		# Heal cannot reserve (no warehouse holds free batch qty) -> original throw preserved.
		with patch(f"{_LOSS}._query_batch_and_qty_sres", return_value=[]), patch(
			f"{_EOD}._reserve_batch_at_physical_warehouse", return_value=None
		):
			with self.assertRaises(frappe.exceptions.ValidationError):
				loss_stock_entry._find_sre(
					self._eir(), self._row(), "MWO-1", "employee_loss_details", 0.01
				)

	def test_healthy_lookup_never_calls_heal(self):
		# An SRE already exists -> the self-heal path is never reached (zero cost).
		with patch(
			f"{_LOSS}._query_batch_and_qty_sres", return_value=[_sre_row_heal("WH-X")]
		), patch(f"{_EOD}._reserve_batch_at_physical_warehouse") as heal, patch(
			f"{_LOSS}.frappe.get_doc",
			return_value=SimpleNamespace(name="SRE-NEW", warehouse="WH-X"),
		):
			loss_stock_entry._find_sre(
				self._eir(), self._row(), "MWO-1", "employee_loss_details", 0.01
			)

		heal.assert_not_called()


class TestWLossStockEntryPrecision(IntegrationTestCase):
	"""Regression guard for the Employee IR Process Loss SE sub-precision crash.

	Employee IR's auto-created Process Loss Stock Entry builds rows as small as 0.001 g. With
	System Settings float_precision = 2 and no per-field precision, ERPNext rounds flt(0.001, 2)
	= 0.0 at two layers and aborts the whole EIR submit:
	  * Stock Entry Detail.transfer_qty -- set_transfer_qty() throws
	    "Qty in Stock UOM can not be zero." on the SE row.
	  * Serial and Batch Entry.qty -- the Serial and Batch Bundle built on submit rounds the
	    batch qty to 0 and throws "At row 1: Qty is mandatory for the batch ...".
	The fix pins these fields to precision 3 via Property Setters (property_setter_guard). The
	same guard also pins the Stock Reservation Entry qty fields (reserved_qty, available_qty,
	delivered_qty, voucher_qty, consumed_qty, transferred_qty): the Transfer-to-Reserve Material
	Request flow submits SREs whose validate_with_allowed_qty rounds a genuine sub-0.01 ct
	available qty to 0 and throws "Cannot reserve more than Allowed Qty 0.0". These tests fail
	loudly if any of that provisioning ever regresses.
	"""

	def test_guard_provisions_transfer_qty_precision(self):
		# Idempotent: safe to run even if already provisioned.
		ensure_field_precision_property_setters()

		self.assertTrue(
			frappe.db.exists(
				"Property Setter", "Stock Entry Detail-transfer_qty-precision"
			),
			"transfer_qty precision Property Setter is missing -- Process Loss SE submit would "
			"round 0.001 g to 0 and crash the Employee IR submit",
		)

		self.assertGreaterEqual(
			frappe.get_precision("Stock Entry Detail", "transfer_qty"),
			3,
			"Stock Entry Detail.transfer_qty precision must be >= 3 so a 0.001 g process loss "
			"survives set_transfer_qty",
		)

	def test_guard_provisions_serial_and_batch_entry_qty_precision(self):
		# Idempotent: safe to run even if already provisioned.
		ensure_field_precision_property_setters()

		self.assertTrue(
			frappe.db.exists("Property Setter", "Serial and Batch Entry-qty-precision"),
			"Serial and Batch Entry.qty precision Property Setter is missing -- the Process Loss "
			"SE's Serial and Batch Bundle would round 0.001 g to 0 and throw 'Qty is mandatory "
			"for the batch', crashing the Employee IR submit",
		)

		self.assertGreaterEqual(
			frappe.get_precision("Serial and Batch Entry", "qty"),
			3,
			"Serial and Batch Entry.qty precision must be >= 3 so a 0.001 g batch loss survives "
			"SerialBatchCreation.set_serial_batch_entries",
		)

	def test_sub_precision_loss_qty_is_representable(self):
		ensure_field_precision_property_setters()
		# The exact failing value from the reported EIRs (uoq5um7vvt, 54hej2sdud). It must not
		# round to 0 at the live precision of EITHER precision-sensitive field.
		for doctype, fieldname in (
			("Stock Entry Detail", "transfer_qty"),
			("Serial and Batch Entry", "qty"),
		):
			precision = frappe.get_precision(doctype, fieldname)
			self.assertGreater(
				flt(0.001, precision),
				0,
				f"0.001 g must not round to 0 at the live {doctype}.{fieldname} precision",
			)

	def test_guard_provisions_stock_reservation_entry_qty_precision(self):
		# Idempotent: safe to run even if already provisioned.
		ensure_field_precision_property_setters()

		self.assertTrue(
			frappe.db.exists(
				"Property Setter", "Stock Reservation Entry-reserved_qty-precision"
			),
			"Stock Reservation Entry.reserved_qty precision Property Setter is missing -- SRE submit "
			"would round a sub-0.01 ct available qty to 0 in validate_with_allowed_qty and throw "
			"'Cannot reserve more than Allowed Qty 0.0', aborting the Transfer-to-Reserve MR flow",
		)

		for fieldname in (
			"reserved_qty",
			"available_qty",
			"delivered_qty",
			"voucher_qty",
			"consumed_qty",
			"transferred_qty",
		):
			self.assertGreaterEqual(
				frappe.get_precision("Stock Reservation Entry", fieldname),
				3,
				f"Stock Reservation Entry.{fieldname} precision must be >= 3 so a sub-0.01 ct "
				"reservation qty survives the reserve->deliver->consume->transfer lifecycle",
			)

	def test_sub_precision_reserve_qty_is_representable(self):
		ensure_field_precision_property_setters()
		# The exact failing value from the reported SRE (Item D-NT-RO-7-+000-00 against
		# SAL-ORD-2026-01072): 0.005 ct available must not round to 0 at the live reserved_qty
		# precision, else allowed_qty collapses to 0.0 and the reservation is rejected.
		precision = frappe.get_precision("Stock Reservation Entry", "reserved_qty")
		self.assertGreater(
			flt(0.0050000000000000044, precision),
			0,
			"0.005 ct must not round to 0 at the live Stock Reservation Entry.reserved_qty precision",
		)


# ---------------------------------------------------------------------------
# Ownership waterfall: loss lands on company metal before customer metal
# ---------------------------------------------------------------------------

_EIR_PATH = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir"


class TestBookMetalLossWaterfall(IntegrationTestCase):
	"""Loss is booked Regular Stock -> Pure Metal -> Customer Goods.

	The customer's gold absorbs wastage only when nothing else on the operation
	has capacity left, and doing so warns rather than blocks.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, mop_log_rows, gwt, r_gwt, ownership=None, headroom=None):
		class DocStub:
			def __init__(self):
				self.manually_book_loss_details = []
				self.operation = None
				self.flags = frappe._dict()
				self.spills = []
				self.overflows = []

			def _collect_customer_loss_spill(self, entry, qty):
				self.spills.append((entry["batch_no"], flt(qty, 3)))

			def _collect_loss_overflow(self, mwo, opt, qty):
				self.overflows.append(flt(qty, 3))

		doc = DocStub()
		patches = [
			patch(
				f"{_EIR_PATH}.frappe.get_cached_value",
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
				f"{_EIR_PATH}.frappe.get_system_settings",
				return_value="Banker's Rounding (legacy)",
			),
			patch(f"{_EIR_PATH}.frappe.db.get_all", return_value=mop_log_rows),
			patch(
				f"{_EIR_PATH}.get_item_from_attribute_full",
				return_value=frappe._dict({"name": "M-G-22KT-91.9-Y"}),
			),
			patch(f"{_EIR_PATH}.batch_priority_map", return_value=ownership or {}),
			patch(f"{_EIR_PATH}.get_batch_sre_headroom", return_value=headroom or {}),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

		result = EmployeeIR.book_metal_loss(
			doc, mwo="MWO-1", opt="MOP-1", gwt=gwt, r_gwt=r_gwt
		)
		return doc, {e["batch_no"]: e for e in result}

	@staticmethod
	def _rows(*specs):
		return [
			frappe._dict(
				{"item_code": "M-G-22KT-91.9-Y", "batch_no": b, "qty": q, "pcs": 0}
			)
			for b, q in specs
		]

	@staticmethod
	def _own(**by_batch):
		return {
			b: frappe._dict(
				inventory_type=inv,
				customer=("CUST-1" if inv and inv.startswith("Customer") else None),
				creation="2026-01-01",
				no_wastage=False,
			)
			for b, inv in by_batch.items()
		}

	def test_regular_absorbs_the_whole_loss_customer_untouched(self):
		rows = self._rows(("BCG", 10.0), ("BRS", 4.0))
		own = self._own(BCG="Customer Goods", BRS="Regular Stock")
		doc, by = self._run(rows, gwt=20.0, r_gwt=17.0, ownership=own)
		self.assertAlmostEqual(by["BRS"]["proportionally_loss"], 3.0, places=3)
		self.assertAlmostEqual(by["BCG"]["proportionally_loss"], 0.0, places=3)
		self.assertEqual(doc.spills, [])

	def test_spills_to_customer_only_after_regular_is_exhausted(self):
		rows = self._rows(("BCG", 10.0), ("BRS", 4.0))
		own = self._own(BCG="Customer Goods", BRS="Regular Stock")
		doc, by = self._run(rows, gwt=20.0, r_gwt=14.0, ownership=own)
		self.assertAlmostEqual(by["BRS"]["proportionally_loss"], 4.0, places=3)
		self.assertAlmostEqual(by["BCG"]["proportionally_loss"], 2.0, places=3)
		self.assertEqual(doc.spills, [("BCG", 2.0)])

	def test_pure_metal_absorbs_before_customer(self):
		rows = self._rows(("BCG", 10.0), ("BRS", 2.0), ("BPM", 3.0))
		own = self._own(BCG="Customer Goods", BRS="Regular Stock", BPM="Pure Metal")
		doc, by = self._run(rows, gwt=20.0, r_gwt=15.0, ownership=own)
		self.assertAlmostEqual(by["BRS"]["proportionally_loss"], 2.0, places=3)
		self.assertAlmostEqual(by["BPM"]["proportionally_loss"], 3.0, places=3)
		self.assertAlmostEqual(by["BCG"]["proportionally_loss"], 0.0, places=3)
		self.assertEqual(doc.spills, [])

	def test_booked_total_always_equals_the_shortfall(self):
		rows = self._rows(("BCG", 7.0), ("BRS", 3.0))
		own = self._own(BCG="Customer Goods", BRS="Regular Stock")
		for shortfall in (0.001, 1.0, 3.0, 4.567, 9.999):
			_doc, by = self._run(rows, gwt=20.0, r_gwt=20.0 - shortfall, ownership=own)
			booked = flt(sum(e["proportionally_loss"] for e in by.values()), 3)
			self.assertEqual(booked, flt(shortfall, 3))

	def test_overflow_lands_on_regular_not_on_the_customer(self):
		# Loss exceeds every balance. The unattributable excess must go to company
		# metal -- never inflate the customer's write-off beyond what they hold.
		rows = self._rows(("BCG", 4.0), ("BRS", 2.0))
		own = self._own(BCG="Customer Goods", BRS="Regular Stock")
		doc, by = self._run(rows, gwt=20.0, r_gwt=10.0, ownership=own)
		self.assertAlmostEqual(by["BCG"]["proportionally_loss"], 4.0, places=3)
		self.assertAlmostEqual(by["BRS"]["proportionally_loss"], 6.0, places=3)
		self.assertEqual(doc.overflows, [4.0])

	def test_reservation_headroom_caps_a_tier_and_spills_the_excess(self):
		# BRS holds 4 g but its largest single SRE has only 1 g remaining;
		# _validate_sre_qty would throw on a 3 g row. The cap spills instead.
		rows = self._rows(("BCG", 10.0), ("BRS", 4.0))
		own = self._own(BCG="Customer Goods", BRS="Regular Stock")
		headroom = {("M-G-22KT-91.9-Y", "BRS"): 1.0}
		doc, by = self._run(
			rows, gwt=20.0, r_gwt=17.0, ownership=own, headroom=headroom
		)
		self.assertAlmostEqual(by["BRS"]["proportionally_loss"], 1.0, places=3)
		self.assertAlmostEqual(by["BCG"]["proportionally_loss"], 2.0, places=3)
		self.assertEqual(doc.spills, [("BCG", 2.0)])

	def test_no_wastage_batch_is_funded_last(self):
		rows = self._rows(("BNW", 10.0), ("BRS", 4.0))
		own = self._own(BNW="Customer Goods", BRS="Regular Stock")
		own["BNW"].no_wastage = True
		_doc, by = self._run(rows, gwt=20.0, r_gwt=17.0, ownership=own)
		self.assertAlmostEqual(by["BRS"]["proportionally_loss"], 3.0, places=3)
		self.assertAlmostEqual(by["BNW"]["proportionally_loss"], 0.0, places=3)

	def test_unresolvable_batch_ranks_as_company_metal(self):
		# MOP Log.batch_no has no referential integrity, so an unresolved batch is
		# routine. It must absorb loss like Regular Stock, not sort behind the customer.
		rows = self._rows(("BGHOST", 4.0), ("BCG", 10.0))
		own = self._own(BCG="Customer Goods")
		doc, by = self._run(rows, gwt=20.0, r_gwt=17.0, ownership=own)
		self.assertAlmostEqual(by["BGHOST"]["proportionally_loss"], 3.0, places=3)
		self.assertAlmostEqual(by["BCG"]["proportionally_loss"], 0.0, places=3)
		self.assertEqual(doc.spills, [])

	def test_received_gross_weight_uses_the_true_balance_not_the_cap(self):
		rows = self._rows(("BRS", 4.0))
		own = self._own(BRS="Regular Stock")
		headroom = {("M-G-22KT-91.9-Y", "BRS"): 1.0}
		_doc, by = self._run(
			rows, gwt=20.0, r_gwt=19.0, ownership=own, headroom=headroom
		)
		self.assertAlmostEqual(by["BRS"]["proportionally_loss"], 1.0, places=3)
		self.assertAlmostEqual(by["BRS"]["received_gross_weight"], 3.0, places=3)
