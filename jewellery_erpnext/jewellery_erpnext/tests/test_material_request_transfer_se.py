# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the deferred Material Request -> 'Material Transfer From Reserve'
Stock Entry (material_request.on_submit / materialize_transfer_se / _create_transfer_se)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext import bounded_retry, serialize
from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_mod
from jewellery_erpnext.jewellery_erpnext.serialize import LockTimeoutError

_MR = "jewellery_erpnext.jewellery_erpnext.doc_events.material_request"


class TestOnSubmitDefersTransferSE(FrappeTestCase):
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


class TestMaterializeTransferSE(FrappeTestCase):
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


class TestCreateTransferSEIdempotency(FrappeTestCase):
	@patch(f"{_MR}.frappe.copy_doc")
	@patch(f"{_MR}.frappe.db.sql")
	@patch(f"{_MR}.frappe.get_doc")
	def test_returns_when_transfer_se_already_set(self, mock_get_doc, mock_sql, mock_copy):
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
	def test_links_existing_submitted_transfer_se(self, mock_get_doc, mock_sql, mock_copy):
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
