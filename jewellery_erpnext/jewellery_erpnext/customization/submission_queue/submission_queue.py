import frappe
from frappe import _
from frappe.core.doctype.submission_queue.submission_queue import SubmissionQueue
from frappe.model.document import Document

#: Doctypes whose background submit fans out into many Stock Entries / Bin / Series / SRE
#: writes in one transaction. They get the long queue, a timeout that fits the cascade, and
#: RQ-level de-duplication so two workers can never submit the same document concurrently.
#:
#: Serial Number Creator and Refining Entry are ``queue_in_background`` too, and were falling
#: through to the base path -- default queue, 600s timeout, NO de-duplication -- while holding
#: Bin locks for the whole cascade. That is a direct 1205/1213 source.
SERIALIZED_SUBMIT_DOCTYPES = [
	"Employee IR",
	"Department IR",
	"Product Certification",
	"Stock Entry",
	"Main Slip",
	"Serial Number Creator",
	"Refining Entry",
]


class CustomSubmissionQueue(SubmissionQueue):
	def insert(self, to_be_queued_doc: Document, action: str):
		queue = frappe.db.get_value(
			"Submission Queue",
			{
				"ref_doctype": self.ref_doctype,
				"ref_docname": self.ref_docname,
				"status": ["in", ["Queued", "Finished"]],
			},
		)

		# Deliberately NOT SERIALIZED_SUBMIT_DOCTYPES: this branch is user-visible (it
		# refuses the submit outright), so the newer entries in that list keep the stock
		# behaviour here and get de-duplication only at the RQ layer in after_insert.
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
		if self.ref_doctype in SERIALIZED_SUBMIT_DOCTYPES:
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
