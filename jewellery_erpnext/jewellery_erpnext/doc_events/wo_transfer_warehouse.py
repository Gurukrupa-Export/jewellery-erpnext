"""Fixed warehouses and cell-process check for a manual "Material Transfer (WORK ORDER)".

Source: the user's department Raw Material warehouse. Target: by the operation's status -- Not
Started goes to the user's department Manufacturing (WO) warehouse, WIP to the operation's
employee's Manufacturing warehouse. For a cell department the user's Employee ``custom_operation``
must equal the operation's ``operation``. The form fills and locks the fields
(public/js/doctype_js/stock_entry.js); this module is the server-side enforcement.

Applies only to a person saving or submitting the entry itself (not auto-created entries, the
Material Request builder, or background jobs), same gate as ``validate_stock_entry_type_permission``.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr

from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry_type import (
	_is_direct_user_save,
)

SE_TYPE = "Material Transfer (WORK ORDER)"


def _user_employee(user):
	return frappe.db.get_value(
		"Employee",
		{"user_id": user, "status": "Active"},
		["name", "department", "custom_operation"],
		as_dict=True,
	)


def _is_cell_department(department):
	if not department or not frappe.get_meta("Department").has_field(
		"custom_is_cell_department"
	):
		return False
	return bool(
		cint(frappe.db.get_value("Department", department, "custom_is_cell_department"))
	)


def _single_warehouse(label, **filters):
	names = frappe.get_all(
		"Warehouse", filters={**filters, "disabled": 0}, pluck="name", order_by="name"
	)
	if len(names) == 1:
		return names[0]
	if not names:
		frappe.throw(
			_("No {0} is configured. Please contact the administrator.").format(label)
		)
	frappe.throw(
		_("More than one {0} is configured ({1}). Keep a single one.").format(
			label, ", ".join(names)
		)
	)


def _department_warehouse(department, warehouse_type, label):
	names = frappe.get_all(
		"Warehouse",
		filters={
			"department": department,
			"warehouse_type": warehouse_type,
			"disabled": 0,
			"employee": ("in", ("", None)),
		},
		pluck="name",
		order_by="name",
	)
	if len(names) == 1:
		return names[0]
	if not names:
		frappe.throw(
			_("No {0} is configured for department {1}.").format(label, department)
		)
	frappe.throw(
		_("More than one {0} is configured for department {1} ({2}).").format(
			label, department, ", ".join(names)
		)
	)


def _expected_target(mop, department):
	"""Target warehouse for an operation by status; None when the status carries no fixed target."""
	if mop.status == "Not Started":
		return _department_warehouse(department, "Manufacturing", _("WO warehouse"))
	if mop.status == "WIP":
		if not mop.employee:
			if mop.get("subcontractor"):
				frappe.throw(
					_(
						"Manufacturing Operation {0} is with a subcontractor; use the subcontracting transfer."
					).format(mop.name)
				)
			frappe.throw(
				_("Manufacturing Operation {0} is WIP but has no employee.").format(
					mop.name
				)
			)
		return _single_warehouse(
			_("Manufacturing warehouse of employee {0}").format(mop.employee),
			employee=mop.employee,
			warehouse_type="Manufacturing",
		)
	return None


@frappe.whitelist()
def get_transfer_warehouses(manufacturing_operation=None):
	"""For the form: the locked source and, for the given operation, the locked target."""
	emp = _user_employee(frappe.session.user)
	if not emp or not emp.department:
		return {}
	out = {
		"source": _department_warehouse(
			emp.department, "Raw Material", _("Raw Material warehouse")
		),
		"target": None,
	}
	if manufacturing_operation:
		mop = frappe.db.get_value(
			"Manufacturing Operation",
			manufacturing_operation,
			["name", "status", "employee"],
			as_dict=True,
		)
		if mop:
			out["target"] = _expected_target(mop, emp.department)
	return out


def _applies(doc):
	if doc.get("stock_entry_type") != SE_TYPE or not _is_direct_user_save(doc):
		return None
	emp = _user_employee(frappe.session.user)
	return emp if emp and emp.department else None


def _row_mop(doc, row):
	return (
		cstr(row.get("manufacturing_operation")).strip()
		or cstr(doc.get("manufacturing_operation")).strip()
	)


def _mops(doc):
	names = {m for m in (_row_mop(doc, r) for r in doc.get("items") or ()) if m}
	header = cstr(doc.get("manufacturing_operation")).strip()
	if header:
		names.add(header)
	if not names:
		return {}
	rows = frappe.get_all(
		"Manufacturing Operation",
		filters={"name": ("in", list(names))},
		fields=["name", "status", "employee", "operation"],
	)
	return {r.name: r for r in rows}


def validate_transfer_warehouses(doc, method=None):
	emp = _applies(doc)
	if not emp:
		return

	source = _department_warehouse(
		emp.department, "Raw Material", _("Raw Material warehouse")
	)
	mops = _mops(doc)
	targets = {}

	def target_for(name):
		if name not in targets:
			mop = mops.get(name)
			targets[name] = _expected_target(mop, emp.department) if mop else None
		return targets[name]

	doc.from_warehouse = source
	header_target = target_for(cstr(doc.get("manufacturing_operation")).strip())
	if header_target:
		doc.to_warehouse = header_target

	for row in doc.get("items") or ():
		if not row.get("s_warehouse"):
			row.s_warehouse = source
		elif row.s_warehouse != source:
			frappe.throw(
				_(
					"Row {0}: Source Warehouse must be {1}, the Raw Material warehouse of your department."
				).format(row.idx, source)
			)
		expected = target_for(_row_mop(doc, row))
		if not expected:
			continue
		if not row.get("t_warehouse"):
			row.t_warehouse = expected
		elif row.t_warehouse != expected:
			frappe.throw(
				_(
					"Row {0}: Target Warehouse must be {1} for Manufacturing Operation {2}."
				).format(row.idx, expected, _row_mop(doc, row))
			)


def validate_cell_process(doc, method=None):
	emp = _applies(doc)
	if not emp or not _is_cell_department(emp.department):
		return
	mops = _mops(doc)
	for row in doc.get("items") or ():
		name = _row_mop(doc, row)
		mop = mops.get(name)
		if mop and (not emp.custom_operation or emp.custom_operation != mop.operation):
			frappe.throw(
				_(
					"Your assigned process does not match the current process of the Manufacturing Operation. "
					"You cannot transfer material against this Work Order."
				),
				title=_("Process Mismatch"),
			)
