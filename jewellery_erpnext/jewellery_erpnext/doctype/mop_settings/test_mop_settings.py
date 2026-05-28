# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_settings import (
	assert_sync_not_running,
)


class TestMOPSettings(FrappeTestCase):
	"""Tests for MOP Settings validation and helpers."""

	def test_validate_warns_when_reservation_types_incomplete(self):
		doc = frappe.get_doc("MOP Settings")
		doc.stock_entry_type_to_reservation = []
		doc.append(
			"stock_entry_type_to_reservation",
			{"stock_entry_type_to_reservation": "Material Transfer (WORK ORDER)"},
		)
		with patch.object(frappe, "msgprint") as mock_msgprint:
			doc.validate()
		mock_msgprint.assert_called_once()
		kwargs = mock_msgprint.call_args[1]
		self.assertEqual(kwargs.get("indicator"), "orange")

	def test_validate_silent_when_all_eir_types_configured(self):
		# _RESERVATION_TYPES_FOR_EIR requires three SE types: Repack,
		# Material Transfer (WORK ORDER), Material Receive (WORK ORDER).
		doc = frappe.get_doc("MOP Settings")
		doc.stock_entry_type_to_reservation = []
		for se_type in (
			"Material Transfer (WORK ORDER)",
			"Repack",
			"Material Receive (WORK ORDER)",
		):
			doc.append(
				"stock_entry_type_to_reservation",
				{"stock_entry_type_to_reservation": se_type},
			)
		with patch.object(frappe, "msgprint") as mock_msgprint:
			doc.validate()
		mock_msgprint.assert_not_called()


class TestSyncLock(FrappeTestCase):
	"""H4/MOPSET-3: assert_sync_not_running lock behaviour."""

	def _set_sync(self, running, started_at=None):
		frappe.db.set_value(
			"MOP Settings",
			"MOP Settings",
			{"sync_running": 1 if running else 0, "sync_started_at": started_at},
			update_modified=False,
		)

	def tearDown(self):
		# Always clear lock after each test so state does not bleed
		self._set_sync(False)
		frappe.flags.pop("mop_sync_in_progress", None)

	def test_passes_when_sync_not_running(self):
		self._set_sync(False)
		assert_sync_not_running()  # must not raise

	def test_throws_when_sync_running(self):
		self._set_sync(True, frappe.utils.now())
		with self.assertRaises(frappe.ValidationError):
			assert_sync_not_running()

	def test_sync_flag_exempts_eod_sync_itself(self):
		self._set_sync(True, frappe.utils.now())
		frappe.flags.mop_sync_in_progress = True
		assert_sync_not_running()  # must not raise even though lock is set

	def test_stale_lock_auto_cleared(self):
		# Simulate a lock started 5 hours ago (> _STALE_LOCK_HOURS=4)
		stale_ts = frappe.utils.add_to_date(frappe.utils.now(), hours=-5)
		self._set_sync(True, stale_ts)
		assert_sync_not_running()  # stale — must not raise
		# Lock should be auto-cleared
		sync_running = frappe.db.get_single_value("MOP Settings", "sync_running")
		self.assertFalse(frappe.utils.cint(sync_running))
