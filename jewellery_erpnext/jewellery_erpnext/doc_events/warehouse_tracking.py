# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Employee MSL warehouse tracking (req #7 / #8).

Single source of truth for the per (employee warehouse x item) Issue / Receive /
Loss / Pending figures, where Pending = Issue - Receive - Loss. Everything is
derived on demand from Stock Ledger Entry (item-level qty, so the v16
NULL-batch_no-in-SLE caveat does not apply), so the maintained copy on the
Warehouse form can never drift from the ledger.

Consumers:
  * ``report/employee_warehouse_tracking`` — cross-warehouse / monthly reporting.
  * ``recalculate_msl_tracking`` — materializes the rows into the
    ``Warehouse.custom_msl_tracking`` child table (button + auto after an
    Employee Loss Entry).
"""

import frappe
from frappe import _
from frappe.utils import (
	add_months,
	flt,
	formatdate,
	get_first_day,
	get_last_day,
	getdate,
	today,
)

# Stock Entry types that represent a booked loss (not a return).
LOSS_STOCK_ENTRY_TYPES = ("Process Loss",)

# MOP Settings field that arms the month-start gate. See
# ``validate_no_prior_period_pending`` for the three states it encodes.
GATE_ENFORCE_FROM_FIELD = "msl_pending_gate_enforce_from"

# Any cutover earlier than this counts as "never configured". An unset Date on a
# Single does NOT read back as None -- frappe returns ``datetime.date(1, 1, 1)``,
# which is perfectly truthy, so a bare ``if not enforce_from`` would arm the gate
# on every site that had simply never touched the field and block every MSL issue
# on day one. This is the guard against exactly that.
GATE_UNSET_BEFORE_YEAR = 1900

# First day of the SLE's own posting month, as a real DATE.
#
# Deliberately NOT DATE_FORMAT(posting_date, '%Y-%m-01'): this query is an f-string
# passed to frappe.db.sql together with a params dict, and pymysql then runs
# ``query % escaped_args`` on it. A literal ``%`` in the SQL is read as a format
# placeholder and raises ``ValueError: unsupported format character 'Y'`` -- even
# when the filter dict is empty. This form is %-free.
MONTH_START_EXPR = (
	"DATE_SUB(sle.posting_date, INTERVAL DAYOFMONTH(sle.posting_date) - 1 DAY)"
)


def is_msl_warehouse(warehouse):
	"""True for an employee (MSL) Raw-Material warehouse.

	The scope both the maintained child table and the month-start gate share: the
	employee's metal pool (a handful of items). Employee Manufacturing/WIP
	warehouses can hold hundreds of items and are covered by the report instead.
	"""
	if not warehouse:
		return False
	meta = frappe.db.get_value(
		"Warehouse", warehouse, ["employee", "warehouse_type"], as_dict=True
	)
	return bool(meta and meta.employee and meta.warehouse_type == "Raw Material")


def _tracking_precision():
	"""Qty precision for the tracking figures — the Stock Entry's own."""
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


def _tracking_eps(precision=None):
	"""Half a unit at qty precision.

	``carry_in - settled`` is a subtraction of two already-rounded numbers and
	routinely lands ~1e-12 above zero, so the gate must compare against this and
	not ``> 0`` — otherwise a fully settled warehouse stays blocked forever behind
	a message that reads ``0.0``.
	"""
	return (10 ** -int(precision or _tracking_precision())) / 2


def get_warehouse_item_tracking(filters=None, group_by_month=False):
	"""Return per (employee warehouse x item) Issue/Receive/Loss/Pending rows.

	``filters`` (all optional): company, employee, warehouse, department,
	item_code, from_date, to_date. Only warehouses with an ``employee`` set are
	considered. Loss = outward movements whose voucher is a Process Loss Stock
	Entry; every other outward movement is a Receive; every inward is an Issue.

	``group_by_month`` adds a ``month_start`` column and buckets by it. Note the
	semantic shift this creates: a monthly ``pending_qty`` is that month's
	``issue - receive - loss``, a DELTA, not a carry-forward balance — metal
	issued in June and returned in July makes June positive and July negative.
	Anything that needs a true running balance (the Receive dialog, the month-start
	gate) must keep calling this WITHOUT ``group_by_month``, bounding the period
	with ``to_date`` alone.
	"""
	filters = frappe._dict(filters or {})

	conditions = ["sle.is_cancelled = 0", "wh.employee IS NOT NULL"]
	if filters.get("company"):
		conditions.append("sle.company = %(company)s")
	if filters.get("employee"):
		conditions.append("wh.employee = %(employee)s")
	if filters.get("warehouse"):
		conditions.append("sle.warehouse = %(warehouse)s")
	if filters.get("department"):
		conditions.append("emp.department = %(department)s")
	if filters.get("item_code"):
		conditions.append("sle.item_code = %(item_code)s")
	if filters.get("from_date"):
		conditions.append("sle.posting_date >= %(from_date)s")
	if filters.get("to_date"):
		conditions.append("sle.posting_date <= %(to_date)s")

	where = " AND ".join(conditions)
	loss_types = ", ".join(frappe.db.escape(t) for t in LOSS_STOCK_ENTRY_TYPES)
	month_select = f"{MONTH_START_EXPR} AS month_start," if group_by_month else ""
	month_group = f", {MONTH_START_EXPR}" if group_by_month else ""

	rows = frappe.db.sql(
		f"""
		SELECT
			sle.warehouse AS warehouse,
			wh.employee AS employee,
			emp.employee_name AS employee_name,
			emp.department AS department,
			{month_select}
			sle.item_code AS item_code,
			SUM(CASE WHEN sle.actual_qty > 0 THEN sle.actual_qty ELSE 0 END) AS issue_qty,
			SUM(CASE WHEN sle.actual_qty < 0
					 AND se.stock_entry_type IN ({loss_types})
					 THEN -sle.actual_qty ELSE 0 END) AS loss_qty,
			SUM(CASE WHEN sle.actual_qty < 0
					 AND (se.stock_entry_type IS NULL OR se.stock_entry_type NOT IN ({loss_types}))
					 THEN -sle.actual_qty ELSE 0 END) AS receive_qty
		FROM `tabStock Ledger Entry` sle
		INNER JOIN `tabWarehouse` wh ON wh.name = sle.warehouse
		LEFT JOIN `tabEmployee` emp ON emp.name = wh.employee
		LEFT JOIN `tabStock Entry` se
			ON sle.voucher_type = 'Stock Entry' AND se.name = sle.voucher_no
		WHERE {where}
		GROUP BY sle.warehouse, sle.item_code{month_group}
		HAVING issue_qty != 0 OR receive_qty != 0 OR loss_qty != 0
		ORDER BY wh.employee, sle.warehouse, sle.item_code{month_group}
		""",
		filters,
		as_dict=True,
	)

	prec = _tracking_precision()
	for r in rows:
		r["issue_qty"] = flt(r.get("issue_qty"), prec)
		r["receive_qty"] = flt(r.get("receive_qty"), prec)
		r["loss_qty"] = flt(r.get("loss_qty"), prec)
		r["pending_qty"] = flt(r["issue_qty"] - r["receive_qty"] - r["loss_qty"], prec)

	return rows


# ---------------------------------------------------------------------------
# Month-start close
# ---------------------------------------------------------------------------


def gate_cutover_date():
	"""The configured month-start cutover, or ``None`` when the gate is off.

	``frappe.db.get_single_value`` returns ``datetime.date(1, 1, 1)`` for a Date
	Single that was never set -- truthy, and earlier than every real posting date,
	so treating it as a configured cutover would silently enforce the gate
	everywhere. Anything before ``GATE_UNSET_BEFORE_YEAR`` is read as "not
	configured".
	"""
	raw = frappe.db.get_single_value("MOP Settings", GATE_ENFORCE_FROM_FIELD)
	if not raw:
		return None
	try:
		cutover = getdate(raw)
	except Exception:
		return None
	# getdate returns None for an unparseable/empty value rather than raising.
	if not cutover or cutover.year < GATE_UNSET_BEFORE_YEAR:
		return None
	return cutover


def _month_boundaries(posting_date=None):
	"""``(first day of this month, last day of the previous month)``."""
	month_start = get_first_day(getdate(posting_date or today()))
	return month_start, get_last_day(add_months(month_start, -1))


def get_prior_period_pending(warehouse, posting_date=None):
	"""``{item_code: pending}`` carried in from before this month.

	CUMULATIVE, deliberately: bounded by ``to_date`` alone with no ``from_date``
	and no month grouping, so it is the running balance as at the close of the
	previous month rather than that month's movement.
	"""
	_month_start, prior_end = _month_boundaries(posting_date)
	rows = get_warehouse_item_tracking({"warehouse": warehouse, "to_date": prior_end})
	prec = _tracking_precision()
	eps = _tracking_eps(prec)
	return {
		r["item_code"]: flt(r["pending_qty"], prec)
		for r in rows
		if flt(r["pending_qty"], prec) > eps
	}


def get_outstanding_prior_period_pending(warehouse, posting_date=None):
	"""``{item_code: qty}`` still owed from a previous month, after this month's returns.

	The raw carry-in alone is not gate-able. It is frozen the moment the month
	closes, so a March receive can never move a 28-Feb figure and a warehouse
	blocked on 1 March would stay blocked for all of March with no operator
	remedy. Draw the carry-in down FIFO-style against what is still physically
	held:

	    outstanding = min(carry_in, live_pending)

	Returns settle the oldest issue first, so the old debt survives only to the
	extent the warehouse still holds anything at all. Both directions matter:
	a warehouse drained to zero owes nothing however large its carry-in was, and
	fresh material issued this month cannot mask an old debt, because it only ever
	raises ``live_pending``.

	Deliberately NOT ``carry_in - settled_this_month``: that subtracts *all* of the
	month's returns, including returns of metal issued within the same month, so a
	warehouse with normal monthly churn would report no debt no matter how much it
	actually carried over.
	"""
	carry_in = get_prior_period_pending(warehouse, posting_date)
	if not carry_in:
		return {}

	prec = _tracking_precision()
	eps = _tracking_eps(prec)
	live = {
		r["item_code"]: flt(r["pending_qty"], prec)
		for r in get_warehouse_item_tracking({"warehouse": warehouse})
	}

	out = {}
	for item, opening in carry_in.items():
		remaining = flt(min(opening, live.get(item, 0.0)), prec)
		if remaining > eps:
			out[item] = remaining
	return out


def validate_no_prior_period_pending(warehouse, posting_date=None):
	"""Block issuing into an employee (MSL) warehouse that still owes last month's metal.

	Three states, all driven by the single Date ``MOP Settings.msl_pending_gate_enforce_from``:

	* **blank** -- dormant. Also the state of any site that has the code but has not
	  migrated or configured it, so a deploy can never brick the Issue button.
	* **a future date** -- warn only, so a plant can see its backlog before the rule bites.
	* **today or past** -- throw.

	Skipped entirely for non-employee / non-Raw-Material warehouses (the same scope
	guard ``recalculate_msl_tracking`` uses) and for privileged roles -- the same
	roles that may re-enable a month-end-closed warehouse.
	"""
	if not is_msl_warehouse(warehouse):
		return

	from jewellery_erpnext.jewellery_erpnext.doc_events.warehouse import _is_privileged

	if _is_privileged(frappe.session.user):
		return

	enforce_from = gate_cutover_date()
	if not enforce_from:
		return

	pending = get_outstanding_prior_period_pending(warehouse, posting_date)
	if not pending:
		return

	prec = _tracking_precision()
	month_start, prior_end = _month_boundaries(posting_date)
	msg = (
		_(
			"Warehouse {0} still carries pending qty from on or before {1}. "
			"Receive it back or book its Loss before issuing more material."
		).format(frappe.bold(warehouse), formatdate(prior_end))
		+ "<br><br>"
		+ "<br>".join(
			"{0} &mdash; {1}".format(item, flt(qty, prec))
			for item, qty in sorted(pending.items())
		)
	)

	if getdate(month_start) < getdate(enforce_from):
		frappe.msgprint(msg, title=_("Prior-Month Pending"), indicator="orange")
		return
	frappe.throw(msg, title=_("Prior-Month Pending"))


@frappe.whitelist()
def recalculate_msl_tracking(warehouse):
	"""Recompute and materialize the ``custom_msl_tracking`` child table on an
	employee (MSL) warehouse from the ledger. Returns the row count.

	The grid shows the CURRENT MONTH's movement over a cumulative opening and
	closing balance. Month-by-month history lives in the Employee Warehouse
	Tracking report, which takes the same helper with ``group_by_month``.

	Always a full recompute from Stock Ledger Entry — never an incremental
	mutation — so the maintained copy cannot drift. Bypasses the MSL guards
	(``ignore_msl_guards``) since this is a system-driven derived-cache write.
	"""
	if not is_msl_warehouse(warehouse):
		return 0

	# Two views of the same ledger:
	#   * the three MOVEMENT columns are scoped to the current month, which is what
	#     the operator wants to see day to day;
	#   * pending_qty stays CUMULATIVE, and `opening_qty` carries what came in from
	#     earlier months, so the grid reconciles as
	#     opening + (issue - receive - loss) = pending.
	#
	# pending_qty must not be month-scoped. `_pending_by_item` and
	# `get_receivable_items` (warehouse_stock_entry) read the cumulative figure
	# live, and `receive_material` computes `loss = pending - returned` from it. An
	# operator reading a month-scoped 2 g and returning 2 against a cumulative 12
	# would book 10 g as irreversible Process Loss. A month-only figure also goes
	# negative for metal issued in June and returned in July.
	prec = _tracking_precision()
	month_start = get_first_day(getdate(today()))
	month_rows = {
		r["item_code"]: r
		for r in get_warehouse_item_tracking(
			{"warehouse": warehouse, "from_date": month_start}
		)
	}
	cumulative = {
		r["item_code"]: r for r in get_warehouse_item_tracking({"warehouse": warehouse})
	}

	doc = frappe.get_doc("Warehouse", warehouse)
	doc.set("custom_msl_tracking", [])
	rows = []
	for item_code in sorted(set(month_rows) | set(cumulative)):
		month = month_rows.get(item_code) or {}
		total = cumulative.get(item_code) or {}
		pending = flt(total.get("pending_qty"), prec)
		issue = flt(month.get("issue_qty"), prec)
		receive = flt(month.get("receive_qty"), prec)
		loss = flt(month.get("loss_qty"), prec)
		opening = flt(pending - (issue - receive - loss), prec)
		if not any((opening, issue, receive, loss, pending)):
			continue
		rows.append(
			{
				"item_code": item_code,
				"opening_qty": opening,
				"issue_qty": issue,
				"receive_qty": receive,
				"loss_qty": loss,
				"pending_qty": pending,
			}
		)
	for r in rows:
		doc.append("custom_msl_tracking", r)
	doc.flags.ignore_msl_guards = True
	doc.flags.ignore_version = True
	doc.save(ignore_permissions=True)
	return len(rows)
