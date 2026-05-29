# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


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
