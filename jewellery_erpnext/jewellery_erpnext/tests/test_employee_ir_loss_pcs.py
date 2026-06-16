# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for D/G manual-loss PCS reconciliation in Employee IR.

Covers:
- validate_manually_book_loss_details — PCS cap on D/G rows.
- create_mop_log_for_employee_ir_loss — D/G negative pcs_change regression.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase


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
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
			validate_manually_book_loss_details,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
			validate_manually_book_loss_details,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
			validate_manually_book_loss_details,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
			validate_manually_book_loss_details,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
			validate_manually_book_loss_details,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_employee_ir_loss,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_employee_ir_loss,
		)

		eir, loss_row = self._setup("M-X", 4)  # pcs would be ignored for M
		mop_log_doc = MagicMock()
		mock_new_doc.return_value = mop_log_doc
		create_mop_log_for_employee_ir_loss(eir, loss_row, "Manually Booked Loss", 1.0)
		self.assertEqual(mop_log_doc.pcs_change, 0)
