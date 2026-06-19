# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for CustomSubmissionQueue.after_insert deduplication routing."""

from unittest.mock import MagicMock, PropertyMock, patch

from frappe.core.doctype.submission_queue.submission_queue import SubmissionQueue
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.customization.submission_queue.submission_queue import (
	CustomSubmissionQueue,
)


def _bare_queue(**fields):
	# Bypass Document.__init__ — only exercise the after_insert routing branch.
	# (queued_doc is a read-only property, patched separately at class level.)
	sq = CustomSubmissionQueue.__new__(CustomSubmissionQueue)
	sq.action_for_queuing = "submit"
	for k, v in fields.items():
		setattr(sq, k, v)
	sq.queue_action = MagicMock()
	return sq


# queued_doc is a property on SubmissionQueue; stub it so after_insert can read it.
@patch.object(SubmissionQueue, "queued_doc", new_callable=PropertyMock, return_value=MagicMock())
class TestCustomSubmissionQueueDedup(FrappeTestCase):
	def test_hot_doctype_routes_with_job_id_and_deduplicate(self, _qd):
		for dt in ["Employee IR", "Department IR", "Product Certification", "Stock Entry", "Main Slip"]:
			sq = _bare_queue(ref_doctype=dt, ref_docname="DOC-1")
			sq.after_insert()
			sq.queue_action.assert_called_once()
			kwargs = sq.queue_action.call_args.kwargs
			self.assertEqual(kwargs["job_id"], f"submit::{dt}::DOC-1")
			self.assertTrue(kwargs["deduplicate"])
			self.assertEqual(kwargs["queue"], "long")
			self.assertTrue(kwargs["enqueue_after_commit"])

	def test_non_hot_doctype_falls_back_to_super(self, _qd):
		with patch.object(SubmissionQueue, "after_insert") as mock_super:
			sq = _bare_queue(ref_doctype="Sales Order", ref_docname="SO-1")
			sq.after_insert()
			mock_super.assert_called_once()
			sq.queue_action.assert_not_called()
