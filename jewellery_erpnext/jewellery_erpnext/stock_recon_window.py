"""Stock Reconciliation Time Window & Transaction Restriction.

Each Department carries a daily window on two custom Time fields:
``custom_stock_reconciliation_from_time`` -> ``custom_stock_reconciliation_to_time``
(installed by ``patches.add_department_stock_recon_window_fields``). While a
department's window is OPEN:

  * Stock Reconciliation for that department is ALLOWED -- and only then.
  * every OTHER stock-movement transaction touching that department is BLOCKED.

Once ``to_time`` passes the window closes on its own (it is a pure clock check,
so nothing has to be released), and stock movement is allowed again.

Department scoping (strictest): a transaction is mapped to the departments it
touches -- direct Department link fields plus the departments of every source /
target warehouse on the header and item rows (``Warehouse.department``). If ANY
touched department's window is open, movement is blocked; a reconciliation is
allowed only if it is inside the window of every touched department that has one.
A department with no window configured is unrestricted (feature inert for it).

GATED, default OFF: fires only when ``MOP Settings.enforce_stock_recon_window`` is
checked, so it can be enabled / rolled back WITHOUT a code change and soaked on a
copy site first. The switch deliberately lives on the MOP Settings Single rather than
site_config (the app's usual flag home) because managed / cloud sites cannot edit
``site_config.json`` -- an admin needs to be able to flip this from the UI. Bypassed
when ``frappe.flags.in_stock_recon_window_sync`` is set so a system-driven
reconciliation flow can run its own bookkeeping unhindered.

Reads use ``frappe.db.get_value`` (not a cached doc) so the latest committed
window config is always seen. Modeled on the EOD lock (``mop_settings/eod_lock.py``),
which fans a single validator across the same stock-touching doctypes.
"""

import frappe
from frappe import _
from frappe.utils import get_time, now_datetime

_FROM_FIELD = "custom_stock_reconciliation_from_time"
_TO_FIELD = "custom_stock_reconciliation_to_time"
_SETTINGS = "MOP Settings"
_FLAG = "enforce_stock_recon_window"
_BYPASS_FLAG = "in_stock_recon_window_sync"

# Parent-level Link-to-Department fields across the gated doctypes
# (Employee IR.department, Department IR.current/next/previous_department, MOP.department).
_DEPARTMENT_FIELDS = (
	"department",
	"current_department",
	"next_department",
	"previous_department",
)
# Parent-level Link-to-Warehouse fields (Stock Entry, MOP Log, Stock Reconciliation).
_PARENT_WAREHOUSE_FIELDS = ("from_warehouse", "to_warehouse", "set_warehouse")
# Item-row Link-to-Warehouse fields (Stock Entry Detail, Stock Reconciliation Item).
_CHILD_WAREHOUSE_FIELDS = ("s_warehouse", "t_warehouse", "warehouse")


def _enabled():
	"""True when the master switch on MOP Settings is checked (default OFF).

	The switch lives on the ``MOP Settings`` Single rather than site_config so it can
	be toggled from the UI on managed / cloud sites where ``site_config.json`` is not
	editable. ``MOP Settings`` is an app-owned doctype, so the field ships via
	``bench migrate`` -- no custom-field patch and no migrate trap.
	"""
	return bool(frappe.db.get_single_value(_SETTINGS, _FLAG))


def _bypassed():
	"""True when a system-driven reconciliation flow marked itself exempt."""
	return bool(getattr(frappe.flags, _BYPASS_FLAG, False))


def _departments_touched(doc):
	"""Return the set of Departments a transaction touches.

	Direct Department link fields on the header, plus the department of every
	source/target warehouse referenced on the header and item rows. Works on real
	Frappe documents and on the ``SimpleNamespace`` docs used by the DB-free tests
	(pure ``getattr``; a single bulk warehouse->department read).
	"""
	departments = set()
	warehouses = set()

	for field in _DEPARTMENT_FIELDS:
		value = getattr(doc, field, None)
		if value:
			departments.add(value)

	for field in _PARENT_WAREHOUSE_FIELDS:
		value = getattr(doc, field, None)
		if value:
			warehouses.add(value)

	for row in getattr(doc, "items", None) or []:
		for field in _CHILD_WAREHOUSE_FIELDS:
			value = getattr(row, field, None)
			if value:
				warehouses.add(value)

	if warehouses:
		for row in frappe.db.get_all(
			"Warehouse",
			filters={"name": ["in", list(warehouses)]},
			fields=["department"],
		):
			if row.get("department"):
				departments.add(row["department"])

	return departments


def _time_in_window(now_t, from_t, to_t):
	"""True when ``now_t`` falls inside [from_t, to_t], handling a window that
	wraps past midnight (from_t > to_t, e.g. 22:00 -> 06:00)."""
	if from_t <= to_t:
		return from_t <= now_t <= to_t
	return now_t >= from_t or now_t <= to_t


def _department_window_status(department, now_t=None):
	"""Return 'open', 'closed' or 'no_window' for a department right now.

	'no_window' when the department has no record or either bound is unset -- such a
	department is unrestricted by this feature.
	"""
	row = frappe.db.get_value(
		"Department", department, [_FROM_FIELD, _TO_FIELD], as_dict=True
	)
	if not row:
		return "no_window"
	from_value, to_value = row.get(_FROM_FIELD), row.get(_TO_FIELD)
	if not from_value or not to_value:
		return "no_window"

	now_t = now_t or now_datetime().time()
	if _time_in_window(now_t, get_time(from_value), get_time(to_value)):
		return "open"
	return "closed"


def validate_stock_movement_allowed(doc, method=None):
	"""doc-event hook -- block save/submit/cancel of a stock-movement transaction
	while ANY department it touches is inside its reconciliation window."""
	if not _enabled() or _bypassed():
		return

	open_departments = sorted(
		d for d in _departments_touched(doc) if _department_window_status(d) == "open"
	)
	if open_departments:
		frappe.throw(
			_(
				"Stock transactions are temporarily blocked for department(s) {0} "
				"because a Stock Reconciliation window is currently in progress. "
				"Please try again once the reconciliation window has closed."
			).format(", ".join(open_departments)),
			title=_("Stock Reconciliation In Progress"),
		)


def validate_reconciliation_within_window(doc, method=None):
	"""doc-event hook -- allow a Stock Reconciliation only while every department it
	touches (that has a window configured) is inside its window."""
	if not _enabled() or _bypassed():
		return

	blocked_departments = sorted(
		d for d in _departments_touched(doc) if _department_window_status(d) == "closed"
	)
	if blocked_departments:
		frappe.throw(
			_(
				"Stock Reconciliation for department(s) {0} can only be performed "
				"within the configured reconciliation time window. It is currently "
				"outside that window."
			).format(", ".join(blocked_departments)),
			title=_("Outside Reconciliation Window"),
		)
