import frappe
from frappe import _
from frappe.core.doctype.submission_queue.submission_queue import SubmissionQueue
from frappe.model.document import Document


class CustomSubmissionQueue(SubmissionQueue):
	def insert(self, to_be_queued_doc: Document, action: str):
		# Guard against re-enqueueing a parent that is already past Draft.
		# Without this check, a retried/duplicate enqueue (two users, network
		# retry, or stale UI) re-runs on_submit and creates duplicate
		# Stock Entries, which is the root cause behind EG-002/EG-003/EG-004.
		#
		# docstatus=1 is a HARMLESS duplicate — we must NOT raise an exception
		# here, otherwise every benign retry writes a new Error Log row and
		# re-creates exactly the EG-002 spam we are trying to eliminate.
		# docstatus=2 is a real operator error and remains a throw.
		parent_docstatus = frappe.db.get_value(
			self.ref_doctype, self.ref_docname, "docstatus"
		)
		if parent_docstatus == 1:
			frappe.msgprint(
				_(
					"{0} {1} is already submitted; skipping duplicate {2} request."
				).format(self.ref_doctype, self.ref_docname, action),
				indicator="blue",
				alert=True,
			)
			return
		if parent_docstatus == 2:
			frappe.throw(
				_("{0} {1} is cancelled; cannot {2}.").format(
					self.ref_doctype, self.ref_docname, action
				)
			)

		queue = frappe.db.get_value(
			"Submission Queue",
			{
				"ref_doctype": self.ref_doctype,
				"ref_docname": self.ref_docname,
				"status": ["in", ["Queued", "Finished"]],
			},
		)

		if (
			self.ref_doctype
			in [
				"Employee IR",
				"Department IR",
				"Product Certification",
				"Stock Entry",
				"Main Slip",
			]
			and queue
		):
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
		if self.ref_doctype in [
			"Employee IR",
			"Department IR",
			"Product Certification",
			"Stock Entry",
			"Main Slip",
		]:
			self.queue_action(
				"background_submission",
				to_be_queued_doc=self.queued_doc,
				action_for_queuing=self.action_for_queuing,
				timeout=4500,
				enqueue_after_commit=True,
				queue="long",
			)
		else:
			super().after_insert()
