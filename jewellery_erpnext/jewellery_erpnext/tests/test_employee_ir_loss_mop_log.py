# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for Employee IR Receive loss MOP Log writes and the manual loss cap.

Covers:
- create_mop_log_for_employee_ir_loss (helper) — gram conversion, idempotency,
  negative qty_change contract that reduces the MOP weight bucket.
- validate_manually_book_loss_details — true-baseline cap.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestCreateMopLogForEmployeeIrLoss(FrappeTestCase):
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
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_employee_ir_loss,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_employee_ir_loss,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_employee_ir_loss,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_employee_ir_loss,
		)

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


class TestManualLossCap(FrappeTestCase):
	"""Per-MWO total cap: total manual loss cannot exceed (gwt - r_gwt)."""

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value",
		return_value=100.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	def test_manual_loss_cap_against_true_baseline(self, *_):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
			validate_manually_book_loss_details,
		)

		doc = MagicMock()
		doc.docstatus = 0

		# Operation: gwt 10 g, received 9 g => baseline 1 g for MWO-1.
		op = MagicMock()
		op.gross_wt = 10.0
		op.received_gross_wt = 9.0
		op.manufacturing_work_order = "MWO-1"
		doc.employee_ir_operations = [op]

		# Manual loss row attempting 1.5 g (exceeds 1 g baseline).
		mbl = MagicMock()
		mbl.idx = 1
		mbl.item_code = "M-X"
		mbl.batch_no = "B-1"
		mbl.manufacturing_operation = "MOP-1"
		mbl.manufacturing_work_order = "MWO-1"
		mbl.proportionally_loss = 1.5
		doc.manually_book_loss_details = [mbl]

		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_manually_book_loss_details(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.get_cached_value",
		return_value="Carat",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_value",
		return_value=100.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils.frappe.db.get_single_value",
		return_value=3,
	)
	def test_manual_loss_carat_converted_to_grams_for_cap(self, *_):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
			validate_manually_book_loss_details,
		)

		doc = MagicMock()
		doc.docstatus = 0

		# Baseline 0.5 g.
		op = MagicMock()
		op.gross_wt = 10.0
		op.received_gross_wt = 9.5
		op.manufacturing_work_order = "MWO-1"
		doc.employee_ir_operations = [op]

		# 5 carats == 1 g (after ×0.2). Exceeds 0.5 g cap.
		mbl = MagicMock()
		mbl.idx = 1
		mbl.item_code = "D-X"
		mbl.batch_no = "B-D"
		mbl.manufacturing_operation = "MOP-1"
		mbl.manufacturing_work_order = "MWO-1"
		mbl.proportionally_loss = 5.0
		# Pin pcs to 0 so the new D/G PCS reconciliation skips the helper
		# (helper is mocked to return [] anyway, but explicit is clearer).
		mbl.pcs = 0
		doc.manually_book_loss_details = [mbl]

		with self.assertRaises(frappe.exceptions.ValidationError):
			validate_manually_book_loss_details(doc)


class TestBookMetalLossSpecExamples(FrappeTestCase):
	"""Run the spec's worked examples directly through book_metal_loss and
	assert the proportional loss values. Verifies the C4 fix is correct.
	"""

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


class TestLossLogIncludedInBalance(FrappeTestCase):
	"""Loss attribution rows post a real qty_change reduction, so the balance
	helper MUST include them so downstream consumers (Make Receive Entry
	availability, manual loss validation, EOD SRE reconcile) see the
	post-loss balance.
	"""

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all"
	)
	def test_get_current_mop_balance_rows_does_not_filter_log_category(
		self, mock_get_all
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			get_current_mop_balance_rows,
		)

		mock_get_all.return_value = []

		get_current_mop_balance_rows("MOP-1")

		_args, kwargs = mock_get_all.call_args
		filters = kwargs["filters"]
		# Filter must NOT exclude loss-attribution rows — they affect balance.
		self.assertNotIn("log_category", filters)
		self.assertEqual(filters["is_cancelled"], 0)
		self.assertEqual(filters["manufacturing_operation"], "MOP-1")


class TestLossMopLogReducesBalance(FrappeTestCase):
	"""Negative qty_change must reduce qty_after_transaction across the three
	balance views (overall prefix, item-based, batch-based) and PCS balances
	must be preserved (loss is recorded by weight, not by piece count).
	"""

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.get_cached_value",
		return_value="Gram",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value"
	)
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_qty_after_transaction_drops_by_loss_weight(
		self, mock_new_doc, mock_get_value, _mock_exists, _mock_uom
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_employee_ir_loss,
		)

		# Pre-loss balance for (item, batch) on the MOP.
		mock_get_value.return_value = {
			"qty_after_transaction": 10.0,
			"qty_after_transaction_item_based": 8.0,
			"qty_after_transaction_batch_based": 6.0,
			"pcs_after_transaction": 4,
			"pcs_after_transaction_item_based": 4,
			"pcs_after_transaction_batch_based": 4,
			"flow_index": 3,
		}

		recorded = {}
		mop_log_doc = MagicMock()

		def _set(name, value):
			recorded[name] = value

		mop_log_doc.set.side_effect = _set
		mock_new_doc.return_value = mop_log_doc

		eir = MagicMock()
		eir.name = "EIR-X"
		loss_row = MagicMock()
		loss_row.name = "ELD-X"
		loss_row.item_code = "M-X"
		loss_row.batch_no = "B-X"
		loss_row.proportionally_loss = 1.5
		loss_row.manufacturing_operation = "MOP-X"
		loss_row.manufacturing_work_order = "MWO-X"

		create_mop_log_for_employee_ir_loss(eir, loss_row, "Auto Employee Loss", 1.5)

		# Each qty_after_transaction view drops by 1.5 g.
		self.assertAlmostEqual(recorded["qty_after_transaction"], 8.5, places=3)
		self.assertAlmostEqual(
			recorded["qty_after_transaction_item_based"], 6.5, places=3
		)
		self.assertAlmostEqual(
			recorded["qty_after_transaction_batch_based"], 4.5, places=3
		)
		# PCS balances preserved at the prior values.
		self.assertEqual(recorded["pcs_after_transaction"], 4)
		self.assertEqual(recorded["pcs_after_transaction_item_based"], 4)
		self.assertEqual(recorded["pcs_after_transaction_batch_based"], 4)
		# flow_index does not advance — loss is booked at the same tier.
		self.assertEqual(mop_log_doc.flow_index, 3)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_employee_ir_loss,
		)

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
