import frappe
from frappe import _

CLOSED_MWO_STATUS = "Closed"


def closed_mwo_operations(operations):
	"""Of these Manufacturing Operations, those whose work order is Closed: {operation: work order}."""
	operations = [o for o in set(operations) if o]
	if not operations:
		return {}
	Mop = frappe.qb.DocType("Manufacturing Operation")
	Mwo = frappe.qb.DocType("Manufacturing Work Order")
	rows = (
		frappe.qb.from_(Mop)
		.join(Mwo)
		.on(Mop.manufacturing_work_order == Mwo.name)
		.select(Mop.name, Mwo.name.as_("mwo"))
		.where(Mop.name.isin(operations))
		.where(Mwo.status == CLOSED_MWO_STATUS)
		.run(as_dict=True)
	)
	return {r.name: r.mwo for r in rows}


def block_closed_mwo_operations(doc, table):
	"""Refuse a Department IR / Employee IR that works on a Closed work order's operations.

	Only drafts are checked, on save and submit. Frappe sets docstatus to 1 before validate on
	every submit path, so a submit arrives here as docstatus 1; validate never runs on cancel.
	Cancelling a document that was submitted before the work order was closed is left alone,
	as is anything already posted.
	"""
	if doc.docstatus == 2:
		return
	rows = doc.get(table) or []
	closed = closed_mwo_operations(r.get("manufacturing_operation") for r in rows)
	# An employee row also names its work order; a row whose own work order is Closed is
	# refused even when its operation field is blank or points somewhere else.
	named = {
		r.get("manufacturing_work_order")
		for r in rows
		if r.get("manufacturing_work_order")
	}
	closed_named = set(
		frappe.get_all(
			"Manufacturing Work Order",
			filters={"name": ["in", list(named)], "status": CLOSED_MWO_STATUS},
			pluck="name",
		)
		if named
		else []
	)
	problems = []
	for r in rows:
		mwo = closed.get(r.get("manufacturing_operation"))
		if not mwo and r.get("manufacturing_work_order") in closed_named:
			mwo = r.get("manufacturing_work_order")
		if mwo:
			problems.append(
				_("Row {0}: {1} belongs to work order {2}, which is Closed.").format(
					r.idx,
					frappe.bold(r.get("manufacturing_operation") or "-"),
					frappe.bold(mwo),
				)
			)
	if problems:
		frappe.throw(
			"<br>".join(problems)
			+ "<br>"
			+ _("Reopen its Parent Manufacturing Order to work on it again."),
			title=_("Work Order Closed"),
		)


def without_closed_mwo_operations(records):
	"""Drop the records (dicts with a name) whose work order is Closed, for the row pickers."""
	closed = closed_mwo_operations(r.name for r in records)
	return [r for r in records if r.name not in closed]
