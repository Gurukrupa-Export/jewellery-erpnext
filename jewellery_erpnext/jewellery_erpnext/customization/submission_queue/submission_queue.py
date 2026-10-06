import contextlib

import frappe
from frappe import _
from frappe.core.doctype.submission_queue.submission_queue import SubmissionQueue
from frappe.model.document import Document
from frappe.utils import now

# Submissions that outrun frappe's 600 s Submission Queue default: run on the long queue
# with a 4500 s budget, one job per document.
LONG_SUBMISSION_DOCTYPES = (
	"Employee IR",
	"Department IR",
	"Product Certification",
	"Stock Entry",
	"Main Slip",
	# Scrap refining transfers run 7k-12k batch lines (10-40 min); at 600 s
	# RFN-SCP-26-00022 was killed mid-submit on every attempt.
	"Refining Entry",
)

# Submissions checked against the work order's current operation before they are queued.
CURRENT_OPERATION_PREFLIGHT_DOCTYPES = ("Employee IR", "Department IR")


class CustomSubmissionQueue(SubmissionQueue):
	def insert(self, to_be_queued_doc: Document, action: str):
		if (
			self.ref_doctype in CURRENT_OPERATION_PREFLIGHT_DOCTYPES
			and (action or "").lower() == "submit"
		):
			# Lock-free work-order current-operation check in the user's own request: a stale
			# draft is refused here with the real reason and no queue row is created. The
			# worker re-checks authoritatively under locks before any side effect.
			from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
				preflight,
			)

			preflight(to_be_queued_doc)

		queue = frappe.db.get_value(
			"Submission Queue",
			{
				"ref_doctype": self.ref_doctype,
				"ref_docname": self.ref_docname,
				"status": ["in", ["Queued", "Finished"]],
			},
		)

		if self.ref_doctype in LONG_SUBMISSION_DOCTYPES and queue:
			frappe.msgprint(
				_("Queued for Submission. You can track the progress over {0}.").format(
					f"<a href='/app/submission-queue/{queue}'><b>here</b></a>"
				),
				indicator="red",
				raise_exception=1,
			)
		else:
			super().insert(to_be_queued_doc, action)

	def after_insert(self):
		if self.ref_doctype in LONG_SUBMISSION_DOCTYPES:
			# deduplicate by target document: if a background submission for this exact
			# (doctype, name) is already queued/running, RQ drops the duplicate instead
			# of running two writers that collide on the same Series/Bin/SRE rows.
			self.queue_action(
				"background_submission",
				to_be_queued_doc=self.queued_doc,
				action_for_queuing=self.action_for_queuing,
				timeout=4500,
				enqueue_after_commit=True,
				queue="long",
				job_id=f"submit::{self.ref_doctype}::{self.ref_docname}",
				deduplicate=True,
			)
		else:
			super().after_insert()

	def background_submission(
		self, to_be_queued_doc: Document, action_for_queuing: str
	):
		try:
			super().background_submission(to_be_queued_doc, action_for_queuing)
		except Exception:
			# frappe marks the row Failed only after its own rollback succeeds. An RQ timeout
			# that lands mid-query leaves the connection out of sync (MySQL 2014), so that
			# rollback raises and the row stays "Queued" for good, which also blocks every
			# later submission of the document (queue_submission refuses while one is Queued).
			# Drop the broken connection (the server rolls the transaction back) and record the
			# failure on a fresh one.
			exception = frappe.get_traceback(with_context=True)
			with contextlib.suppress(Exception):
				frappe.db.close()
			frappe.db.connect()
			frappe.db.set_value(
				self.doctype,
				self.name,
				{"status": "Failed", "exception": exception, "ended_at": now()},
				update_modified=False,
			)
			frappe.db.commit()
			self.notify("Failed", action_for_queuing)
