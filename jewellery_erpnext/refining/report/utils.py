import frappe
from frappe.utils import add_days, flt, getdate, nowdate


def get_period(filters=None, default_days=7):
	filters = frappe._dict(filters or {})
	if filters.get("posting_date"):
		date = getdate(filters.posting_date)
		return date, date
	if filters.get("from_date") or filters.get("to_date"):
		to_date = getdate(filters.get("to_date") or nowdate())
		from_date = getdate(
			filters.get("from_date") or add_days(to_date, -default_days + 1)
		)
		return from_date, to_date
	to_date = getdate(nowdate())
	from_date = add_days(to_date, -default_days + 1)
	return from_date, to_date


def entry_conditions(filters=None, default_days=7, refining_type=None):
	filters = frappe._dict(filters or {})
	from_date, to_date = get_period(filters, default_days)
	conditions = [
		"re.docstatus = 1",
		"re.posting_date BETWEEN %(from_date)s AND %(to_date)s",
	]
	values = {"from_date": from_date, "to_date": to_date}

	if refining_type:
		conditions.append("re.refining_type = %(refining_type)s")
		values["refining_type"] = refining_type
	elif filters.get("refining_type"):
		conditions.append("re.refining_type = %(refining_type)s")
		values["refining_type"] = filters.refining_type

	if filters.get("department"):
		conditions.append("re.department = %(department)s")
		values["department"] = filters.department

	return " AND ".join(conditions), values


def recovery_percent(recovered, expected):
	return (flt(recovered) / flt(expected) * 100.0) if flt(expected) else 0.0
