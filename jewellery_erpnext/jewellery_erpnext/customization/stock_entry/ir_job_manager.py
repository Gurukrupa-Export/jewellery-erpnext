import frappe
from frappe import _, get_traceback, publish_realtime
from frappe.utils import now, time_diff_in_seconds, cint
from urllib.parse import quote
from frappe.utils.background_jobs import enqueue
from frappe.utils import get_link_to_form


class IRJobManager:
	def __init__(
		self,
		doc,
		create_mop_func: callable = None,
		create_stock_entry_func: callable = None,
	):
		self.doc = doc
		self.ref_doctype = self.doc.doctype
		self.ref_docname = self.doc.name
		self.create_stock_entry_func = create_stock_entry_func
		self.create_mop_func = create_mop_func
		self.created_at = now()

	def enqueue_mop_creation(self):
		if not self.create_mop_func:
			frappe.throw(_("No function provided to create Manufacturing Operation."))

		job_id = f"mop_creation::{self.ref_doctype}::{self.ref_docname}"
		enqueue(
			self.background_processing,
			to_be_queued_doc=self.doc,
			action_for_queuing=self.create_mop_func.__name__,
			queue="short",
			at_front=True,
			job_id=job_id,
			enqueue_after_commit=True,
		)
		self.add_job_comment(job_id, "Manufacturing Operation creation queued.")

	def enqueue_stock_entry_submission(self):
		if not self.create_stock_entry_func:
			frappe.throw(_("No function provided to create Stock Entry."))

		job_id = f"stock_entry_creation::{self.ref_doctype}::{self.ref_docname}"
		enqueue(
			self.background_processing,
			to_be_queued_doc=self.doc,
			action_for_queuing=self.create_stock_entry_func.__name__,
			queue="long",
			timeout=1500,
			at_front=True,
			job_id=job_id,
			enqueue_after_commit=True,
		)
		self.add_job_comment(job_id, "Stock Entry creation queued.")

	def background_processing(self, to_be_queued_doc, action_for_queuing: str):
		try:
			exec_start_time = now()
			getattr(to_be_queued_doc, action_for_queuing)()
			job_time = round(time_diff_in_seconds(now(), self.created_at), 2)
			exec_time = round(time_diff_in_seconds(now(), exec_start_time), 2)

			self.update_workflow_state(to_be_queued_doc, job_time, exec_time)

		except Exception:
			status = "Failed"
			trace = get_traceback(with_context=True)
			self.update_workflow_state(to_be_queued_doc, 0, 0, is_failed=True, trace=trace)

	def notify_ir_job_status(self, status: str, message: str):
		publish_realtime(
			"msgprint",
			{
				"message": f"{message} View it <a href='/app/{quote(self.ref_doctype.lower().replace(' ', '-'))}/{quote(self.ref_docname)}'><b>here</b></a>",
				"alert": True,
				"indicator": "red" if status == "Failed" else "green",
			},
			user=self.doc.owner,
		)

	def add_job_comment(self, job_id: str, message: str):
		self.doc.add_comment("Comment", text=f"{message}\nJob ID: {job_id}")

	def update_workflow_state(self, to_be_queued_doc, job_time, exec_time, is_failed=False, trace=None):
		status = "Finished"
		message = ""

		if is_failed and trace:
			status = "Failed"
			if to_be_queued_doc.workflow_state == "Queued MOP Creation":
				to_be_queued_doc.db_set("workflow_state", "MOP Failed")
				error_log = frappe.log_error(
					f"MOP Creation Failed for {to_be_queued_doc.doctype} {to_be_queued_doc.name}",
					trace,
				)
				message = f"Failed to create Manufacturing Operation. {get_link_to_form(error_log.doctype, error_log.name)}"

			elif to_be_queued_doc.workflow_state == "Queued Stock Entry Creation":
				to_be_queued_doc.db_set("workflow_state", "Stock Entry Failed")
				error_log = frappe.log_error(
					f"Stock Entry Creation Failed for {to_be_queued_doc.doctype} {to_be_queued_doc.name}",
					trace,
				)
				message = f"Failed to create Stock Entry. {get_link_to_form(error_log.doctype, error_log.name)}"

			to_be_queued_doc.add_comment("Comment", text=message)

		else:
			message = ""
			if to_be_queued_doc.workflow_state == "Queued MOP Creation":
					to_be_queued_doc.db_set("workflow_state", "Pending Stock Entry Creation")
					message = "Manufacturing Operation creation completed."

			elif to_be_queued_doc.workflow_state == "Queued Stock Entry Creation":
				to_be_queued_doc.db_set("workflow_state", "Stock Entry Created")
				message = "Stock Entry creation completed."

			exec_time_msg = f"(Job Time: {job_time}s Exec Time: {exec_time}s)"
			to_be_queued_doc.add_comment("Comment", text=message + exec_time_msg)

		self.notify_ir_job_status(status, message)