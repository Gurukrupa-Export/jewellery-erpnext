# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the Process Loss Stock Entry Type patch and runtime guard.

Tests 1 and 2 are integration tests (FrappeTestCase, require a bench site).
Tests 3 and 4 are mock-based and run without live DB access.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se import (
	_ensure_process_loss_stock_entry_type_exists,
)


class TestProcessLossStockEntryTypePatch(FrappeTestCase):
	"""Integration tests — require a bench site with ERPNext installed."""

	def setUp(self):
		# Ensure a clean state: delete the type if it already exists so the patch
		# can create it from scratch. tearDown restores it.
		self._existed_before = frappe.db.exists("Stock Entry Type", "Process Loss")

	def tearDown(self):
		# Restore the pre-test state so other tests are not affected
		if not self._existed_before and frappe.db.exists(
			"Stock Entry Type", "Process Loss"
		):
			frappe.delete_doc(
				"Stock Entry Type", "Process Loss", force=True, ignore_permissions=True
			)
		elif self._existed_before and not frappe.db.exists(
			"Stock Entry Type", "Process Loss"
		):
			from jewellery_erpnext.patches.create_process_loss_stock_entry_type import (
				execute,
			)

			execute()

	def test_patch_creates_missing_type(self):
		"""Patch must create 'Process Loss' with purpose = 'Material Transfer' when absent."""
		# Ensure it is absent before calling the patch
		if frappe.db.exists("Stock Entry Type", "Process Loss"):
			frappe.delete_doc(
				"Stock Entry Type", "Process Loss", force=True, ignore_permissions=True
			)
		self.assertIsNone(frappe.db.exists("Stock Entry Type", "Process Loss"))

		from jewellery_erpnext.patches.create_process_loss_stock_entry_type import (
			execute,
		)

		execute()

		self.assertEqual(
			frappe.db.exists("Stock Entry Type", "Process Loss"), "Process Loss"
		)
		doc = frappe.get_doc("Stock Entry Type", "Process Loss")
		self.assertEqual(doc.purpose, "Repack")

	def test_patch_is_idempotent(self):
		"""Running the patch twice must not duplicate or raise."""
		from jewellery_erpnext.patches.create_process_loss_stock_entry_type import (
			execute,
		)

		execute()
		execute()  # second call must be a no-op

		count = frappe.db.count("Stock Entry Type", {"name": "Process Loss"})
		self.assertEqual(count, 1)


class TestProcessLossRuntimeGuard(FrappeTestCase):
	"""Mock-based tests for _ensure_process_loss_stock_entry_type_exists."""

	def test_guard_raises_clear_error_when_type_missing(self):
		"""Guard must raise ValidationError mentioning 'Process Loss' and 'bench migrate'."""
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.db.exists",
			return_value=None,
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				_ensure_process_loss_stock_entry_type_exists()

		error_text = str(ctx.exception)
		self.assertIn("Process Loss", error_text)
		self.assertIn("bench migrate", error_text)

	def test_guard_passes_when_type_exists(self):
		"""Guard must not raise when the Stock Entry Type exists."""
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_loss_se.frappe.db.exists",
			return_value="Process Loss",
		):
			# Must complete without raising
			_ensure_process_loss_stock_entry_type_exists()
