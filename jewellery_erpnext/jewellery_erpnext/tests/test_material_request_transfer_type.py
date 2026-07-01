# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the ``before_validate`` auto-derivation of ``custom_transfer_type``
on Material Request.

Rule: the transfer type is auto-derived ONLY when it is blank. A value already
present (set manually, or defaulted on a prior save) is preserved and never
overwritten. When it IS derived, the source/target warehouse ``custom_branch``
values are normalised so a blank ("") and a NULL branch count as the same branch
(both "no branch" -> Transfer To Department), instead of "" != None forcing
Transfer To Branch.
"""

from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_mod


class _MR:
	def __init__(
		self,
		set_from_warehouse=None,
		set_warehouse=None,
		custom_transfer_type=None,
		material_request_type=None,
	):
		self.set_from_warehouse = set_from_warehouse
		self.set_warehouse = set_warehouse
		self.custom_transfer_type = custom_transfer_type
		self.material_request_type = material_request_type
		self.custom_manufacturing_operation = None


class TestMaterialRequestTransferType(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, mr, branches):
		"""Call before_validate with branch lookups and the unrelated
		downstream validators stubbed out."""

		def _gv(doctype, name, field):
			return branches.get(name)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.material_request.frappe.db.get_value",
			side_effect=_gv,
		), patch.object(mr_mod, "update_pure_qty"), patch.object(
			mr_mod, "validate_target_item"
		), patch.object(mr_mod, "validate_warehouse"):
			mr_mod.before_validate(mr, None)
		return mr.custom_transfer_type

	def test_existing_value_preserved(self):
		"""A manually-chosen value is not overwritten even when the branches
		differ -- the original bug on KGJPL-MR-MT-26-02528."""
		mr = _MR(
			set_from_warehouse="Central RM - KGJPL",
			set_warehouse="Waxing RM - KGJPL",
			custom_transfer_type="Transfer To Department",
		)
		result = self._run(mr, {"Central RM - KGJPL": "", "Waxing RM - KGJPL": None})
		self.assertEqual(result, "Transfer To Department")

	def test_blank_same_branch(self):
		mr = _MR(set_from_warehouse="W1", set_warehouse="W2")
		result = self._run(mr, {"W1": "BR-A", "W2": "BR-A"})
		self.assertEqual(result, "Transfer To Department")

	def test_blank_different_branches(self):
		mr = _MR(set_from_warehouse="W1", set_warehouse="W2")
		result = self._run(mr, {"W1": "BR-A", "W2": "BR-B"})
		self.assertEqual(result, "Transfer To Branch")

	def test_blank_branches_empty_vs_null_treated_as_same(self):
		"""Regression: "" and NULL branch are normalised so two no-branch
		warehouses default to Transfer To Department, not Transfer To Branch."""
		mr = _MR(set_from_warehouse="W1", set_warehouse="W2")
		result = self._run(mr, {"W1": "", "W2": None})
		self.assertEqual(result, "Transfer To Department")

	def test_manufacture_defaults_to_reserve(self):
		mr = _MR(material_request_type="Manufacture")
		result = self._run(mr, {})
		self.assertEqual(result, "Transfer to Reserve")

	def tearDown(self):
		return super().tearDown()
