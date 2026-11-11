# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""One change of a work order's process: the history behind ``Manufacturing Work Order.process_transition``.

Written only by ``doc_events.process_control.apply_transition`` (Process Transfer submit and cancel,
Department IR Issue and its cancel). Users can read it but not create or change it. A
cancellation is a new row that ``reverses`` the original; nothing is ever edited or deleted.
"""

import frappe
from frappe.model.document import Document


class ProcessTransitionLog(Document):
	pass


def on_doctype_update():
	# One row per voucher, work order and event: a retried or repeated submit cannot write the
	# same history twice, whatever the application code does.
	frappe.db.add_unique(
		"Process Transition Log",
		["voucher_type", "voucher_no", "manufacturing_work_order", "event"],
		constraint_name="unique_process_transition_voucher_event",
	)
