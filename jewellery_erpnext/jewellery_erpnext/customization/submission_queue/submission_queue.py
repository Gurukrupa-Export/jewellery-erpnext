from frappe.core.doctype.submission_queue.submission_queue import SubmissionQueue
import frappe
from frappe import _
from frappe.model.document import Document


class CustomSubmissionQueue(SubmissionQueue):
	def insert(self, to_be_queued_doc: Document, action: str):
		queue =frappe.db.get_value("Submission Queue",{"ref_doctype":self.ref_doctype,"ref_docname":self.ref_docname,"status":["in",["Queued","Finished"]]})

		if self.ref_doctype in ["Employee IR","Department IR","Product Certification","Stock Entry","Main Slip"] and queue:
			frappe.msgprint(
			_("Queued for Submission. You can track the progress over {0}.").format(
				f"<a href='/app/submission-queue/{queue}'><b>here</b></a>"
			),
			indicator="red",
			raise_exception = 1
		)
		else:
			super().insert(to_be_queued_doc,action)

	def after_insert(self):
		if self.ref_doctype in ["Employee IR","Department IR","Product Certification","Stock Entry","Main Slip"]:
			self.queue_action(
				"background_submission",
				to_be_queued_doc=self.queued_doc,
				action_for_queuing=self.action_for_queuing,
				timeout=4500,
				enqueue_after_commit=True,
				queue="long"
			)
		else:
			super().after_insert()
