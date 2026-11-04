import frappe
from frappe import _

CLOSED_MWO_STATUS = "Closed"
HOLD_MWO_STATUS = "Hold"
# Work orders that take no new work: Closed (with their PMO, or split) or on Hold with their
# PMO (doctype/parent_manufacturing_order/doc_events/close_open.py).
BLOCKED_MWO_STATUSES = (CLOSED_MWO_STATUS, HOLD_MWO_STATUS)


def closed_mwo_operations(operations):
	"""Of these Manufacturing Operations, those whose work order is Closed or on Hold:
	{operation: (work order, status)}."""
	operations = [o for o in set(operations) if o]
	if not operations:
		return {}
	Mop = frappe.qb.DocType("Manufacturing Operation")
	Mwo = frappe.qb.DocType("Manufacturing Work Order")
	rows = (
		frappe.qb.from_(Mop)
		.join(Mwo)
		.on(Mop.manufacturing_work_order == Mwo.name)
		.select(Mop.name, Mwo.name.as_("mwo"), Mwo.status)
		.where(Mop.name.isin(operations))
		.where(Mwo.status.isin(BLOCKED_MWO_STATUSES))
		.run(as_dict=True)
	)
	return {r.name: (r.mwo, r.status) for r in rows}


def block_closed_mwo_operations(doc, table):
	"""Refuse a Department IR / Employee IR that works on a Closed or held work order's operations.

	Only drafts are checked, on save and submit. Frappe sets docstatus to 1 before validate on
	every submit path, so a submit arrives here as docstatus 1; validate never runs on cancel.
	Cancelling a document that was submitted before the work order was closed or held is the
	cancel guard's call (current_operation_guard.guard_cancel), as is anything already posted.
	"""
	if doc.docstatus == 2:
		return
	rows = doc.get(table) or []
	closed = closed_mwo_operations(r.get("manufacturing_operation") for r in rows)
	# An employee row also names its work order; a row whose own work order is Closed or held
	# is refused even when its operation field is blank or points somewhere else.
	named = {
		r.get("manufacturing_work_order")
		for r in rows
		if r.get("manufacturing_work_order")
	}
	closed_named = (
		dict(
			frappe.get_all(
				"Manufacturing Work Order",
				filters={
					"name": ["in", list(named)],
					"status": ["in", BLOCKED_MWO_STATUSES],
				},
				fields=["name", "status"],
				as_list=True,
			)
		)
		if named
		else {}
	)
	problems = []
	for r in rows:
		mwo, status = closed.get(r.get("manufacturing_operation")) or (None, None)
		if not mwo and r.get("manufacturing_work_order") in closed_named:
			mwo = r.get("manufacturing_work_order")
			status = closed_named[mwo]
		if mwo:
			problems.append(
				_("Row {0}: {1} belongs to work order {2}, which is {3}.").format(
					r.idx,
					frappe.bold(r.get("manufacturing_operation") or "-"),
					frappe.bold(mwo),
					_status_phrase(status),
				)
			)
	if problems:
		frappe.throw(
			"<br>".join(problems)
			+ "<br>"
			+ _(
				"A held work order takes work again once its Parent Manufacturing Order is Opened; a Closed one takes no more."
			),
			title=_("Work Order Closed or on Hold"),
		)


def _status_phrase(status):
	return _("on Hold") if status == HOLD_MWO_STATUS else _("Closed")


def without_closed_mwo_operations(records):
	"""Drop the records (dicts with a name) whose work order is Closed or on Hold, for the row
	pickers."""
	closed = closed_mwo_operations(r.name for r in records)
	return [r for r in records if r.name not in closed]
