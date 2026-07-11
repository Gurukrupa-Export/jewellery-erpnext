# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the deferred Material Request -> 'Material Transfer From Reserve'
Stock Entry (material_request.on_submit / materialize_transfer_se / _create_transfer_se)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext import bounded_retry, serialize
from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_mod
from jewellery_erpnext.jewellery_erpnext.serialize import LockTimeoutError

_MR = "jewellery_erpnext.jewellery_erpnext.doc_events.material_request"


class TestOnSubmitDefersTransferSE(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MR}.frappe.enqueue")
	def test_enqueues_with_dedup_and_job_id(self, mock_enqueue):
		mr = MagicMock(name="MR")
		mr.name = "MR-001"
		mr.custom_reserve_se = "SE-RESERVE"
		mr.custom_transfer_se = None
		mr_mod.on_submit(mr)

		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertIs(args[0], mr_mod.materialize_transfer_se)
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["job_id"], "mr_transfer_se::MR-001")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["mr_name"], "MR-001")
		mr.db_set.assert_called_once_with(
			"custom_transfer_se_state", "Pending", update_modified=False
		)

	@patch(f"{_MR}.frappe.enqueue")
	def test_skips_when_no_reserve_se(self, mock_enqueue):
		mr = MagicMock()
		mr.custom_reserve_se = None
		mr_mod.on_submit(mr)
		mock_enqueue.assert_not_called()

	@patch(f"{_MR}.frappe.enqueue")
	def test_skips_when_transfer_already_materialized(self, mock_enqueue):
		mr = MagicMock()
		mr.custom_reserve_se = "SE-RESERVE"
		mr.custom_transfer_se = "SE-TRANSFER"
		mr_mod.on_submit(mr)
		mock_enqueue.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestMaterializeTransferSE(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_lock_timeout_is_swallowed(self):
		@contextmanager
		def _raise_lock(*a, **k):
			raise LockTimeoutError("held")
			yield  # pragma: no cover

		with patch.object(serialize, "conflict_lock", _raise_lock), patch.object(
			bounded_retry, "run_with_retry"
		) as mock_run:
			# Should NOT raise.
			mr_mod.materialize_transfer_se("MR-001")
		mock_run.assert_not_called()

	def test_generic_error_marks_failed_and_reraises(self):
		@contextmanager
		def _noop(*a, **k):
			yield

		with patch.object(serialize, "conflict_lock", _noop), patch.object(
			bounded_retry, "run_with_retry", side_effect=ValueError("boom")
		), patch(f"{_MR}.frappe.db") as mock_db, patch(f"{_MR}.frappe.log_error"):
			with self.assertRaises(ValueError):
				mr_mod.materialize_transfer_se("MR-001")

		# Failure recorded on the MR for reconciliation.
		mock_db.set_value.assert_called_once()
		args = mock_db.set_value.call_args[0]
		self.assertEqual(args[0], "Material Request")
		self.assertEqual(args[1], "MR-001")
		self.assertEqual(args[2]["custom_transfer_se_state"], "Failed")
		self.assertIn("boom", args[2]["custom_transfer_se_error"])

	def tearDown(self):
		return super().tearDown()


class TestCreateTransferSEIdempotency(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MR}.frappe.copy_doc")
	@patch(f"{_MR}.frappe.db.sql")
	@patch(f"{_MR}.frappe.get_doc")
	def test_returns_when_transfer_se_already_set(
		self, mock_get_doc, mock_sql, mock_copy
	):
		mr = MagicMock()
		mr.custom_reserve_se = "SE-RESERVE"
		mr.get = MagicMock(return_value="SE-TRANSFER")  # already linked
		mock_get_doc.return_value = mr

		mr_mod._create_transfer_se("MR-001")

		mock_sql.assert_not_called()
		mock_copy.assert_not_called()

	@patch(f"{_MR}.frappe.copy_doc")
	@patch(f"{_MR}.frappe.db.sql")
	@patch(f"{_MR}.frappe.get_doc")
	def test_links_existing_submitted_transfer_se(
		self, mock_get_doc, mock_sql, mock_copy
	):
		mr = MagicMock()
		mr.custom_reserve_se = "SE-RESERVE"
		mr.get = MagicMock(return_value=None)
		mock_get_doc.return_value = mr
		mock_sql.return_value = [("SE-TRANSFER-9",)]  # an existing transfer SE

		mr_mod._create_transfer_se("MR-001")

		# Linked + marked Done, without copying/creating a new SE.
		mr.db_set.assert_any_call(
			"custom_transfer_se", "SE-TRANSFER-9", update_modified=False
		)
		mr.db_set.assert_any_call(
			"custom_transfer_se_state", "Done", update_modified=False
		)
		mock_copy.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class MockMaterialRequest:
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
		mr = MockMaterialRequest(
			set_from_warehouse="Central RM - KGJPL",
			set_warehouse="Waxing RM - KGJPL",
			custom_transfer_type="Transfer To Department",
		)
		result = self._run(mr, {"Central RM - KGJPL": "", "Waxing RM - KGJPL": None})
		self.assertEqual(result, "Transfer To Department")

	def test_blank_same_branch(self):
		mr = MockMaterialRequest(set_from_warehouse="W1", set_warehouse="W2")
		result = self._run(mr, {"W1": "BR-A", "W2": "BR-A"})
		self.assertEqual(result, "Transfer To Department")

	def test_blank_different_branches(self):
		mr = MockMaterialRequest(set_from_warehouse="W1", set_warehouse="W2")
		result = self._run(mr, {"W1": "BR-A", "W2": "BR-B"})
		self.assertEqual(result, "Transfer To Branch")

	def test_blank_branches_empty_vs_null_treated_as_same(self):
		"""Regression: "" and NULL branch are normalised so two no-branch
		warehouses default to Transfer To Department, not Transfer To Branch."""
		mr = MockMaterialRequest(set_from_warehouse="W1", set_warehouse="W2")
		result = self._run(mr, {"W1": "", "W2": None})
		self.assertEqual(result, "Transfer To Department")

	def test_manufacture_defaults_to_reserve(self):
		mr = MockMaterialRequest(material_request_type="Manufacture")
		result = self._run(mr, {})
		self.assertEqual(result, "Transfer to Reserve")

	def tearDown(self):
		return super().tearDown()
