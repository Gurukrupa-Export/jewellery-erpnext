import frappe
from frappe import _
from frappe.utils import cint

# Roles that bypass the Employee (MSL) warehouse guards: they may create MSL
# warehouses for any department, see every warehouse, and re-enable a disabled
# (month-end-closed) MSL warehouse.
PRIVILEGED_ROLES = {"System Manager", "Stock Manager"}


def validate(doc, method=None):
	if doc.department and doc.employee:
		frappe.throw(
			_("You can fill in either the department or employee field, but not both.")
		)
	check_unique_multifield(
		doc,
		warehouse_type=doc.warehouse_type,
		department=doc.department,
		subcontractor=doc.subcontractor,
		employee=doc.employee,
	)
	# Employee MSL Warehouse guards (req #4 creation scope, req #5 re-enable).
	# Skipped for the system-driven tracking recompute (recalculate_msl_tracking),
	# which re-saves the warehouse only to materialize the derived child table.
	if not doc.flags.get("ignore_msl_guards"):
		validate_msl_department_scope(doc)
		guard_msl_reenable(doc)


def check_unique_multifield(doc, **kwargs):
	existing_type = [
		"Manufacturing",
		"Raw Material",
		"Reserve",
		"Transit",
		"Consumables",
		"Material",
		"Finished Goods",
		"Reserve",
		"Scrap",
	]
	if doc.department and doc.warehouse_type not in existing_type:
		frappe.throw(
			_(
				"Warehouse type must be set one of this <b>(Manufacturing or Raw Material)</b>"
			)
		)
	if doc.employee and doc.warehouse_type not in existing_type:
		frappe.throw(
			_(
				"Warehouse type must be set one of this <b>(Manufacturing or Raw Material)</b>"
			)
		)
	if not any(kwargs.values()):
		return
	if doc.department or doc.employee:
		existing_fields = [
			f"{field} = {value}" for field, value in kwargs.items() if value
		]
		if existing_fields:
			filters = {key: value for key, value in kwargs.items() if value}
			filters["name"] = ["!=", doc.name]
			if existing := frappe.db.exists("Warehouse", filters):
				# frappe.throw(
				# 	_(f"Warehouse: <b>{existing}</b> already has set </br><b>{', '.join(existing_fields)}</b>")
				# )
				frappe.throw(
					_("Warehouse: <b>{0}</b> already has set </br><b>{1}</b>").format(
						existing, ", ".join(existing_fields)
					)
				)


# ---------------------------------------------------------------------------
# Employee MSL warehouse — department scoping & lifecycle
# ---------------------------------------------------------------------------


def get_user_department(user=None):
	"""Department of the Employee linked to ``user`` (session user by default).

	Centralizes the ``Employee{user_id} -> department`` lookup duplicated across
	the app (utils.get_warehouse_from_user, stock_entry filters, refining_entry).
	Returns ``None`` when the user has no Employee record / no department.
	"""
	user = user or frappe.session.user
	return frappe.db.get_value("Employee", {"user_id": user}, "department")


def _is_privileged(user):
	if user == "Administrator":
		return True
	return bool(PRIVILEGED_ROLES.intersection(frappe.get_roles(user)))


def validate_msl_department_scope(doc):
	"""req #4 — a non-privileged user may only create/edit an Employee (MSL)
	warehouse for an employee in their OWN department.

	Only employee warehouses are guarded; shared / department / group / company
	warehouses are untouched.
	"""
	if not doc.employee:
		return
	user = frappe.session.user
	if _is_privileged(user):
		return

	user_dept = get_user_department(user)
	if not user_dept:
		frappe.throw(
			_(
				"Your Employee record has no Department set, so you cannot create an "
				"employee (MSL) warehouse. Ask a System Manager."
			)
		)

	warehouse_dept = frappe.db.get_value("Employee", doc.employee, "department")
	if warehouse_dept != user_dept:
		frappe.throw(
			_(
				"You can only create MSL warehouses for employees in your own "
				"department (<b>{0}</b>)."
			).format(user_dept)
		)


def guard_msl_reenable(doc):
	"""req #5 — once an Employee (MSL) warehouse is disabled (month-end close), a
	non-privileged user must not re-enable it. Only System Managers may.
	"""
	if doc.is_new():
		return
	if not doc.employee:
		return
	# Only guard the disabled 1 -> 0 (re-enable) transition.
	if cint(doc.disabled) != 0:
		return
	if _is_privileged(frappe.session.user):
		return

	before = doc.get_doc_before_save()
	if before and cint(before.disabled) == 1:
		frappe.throw(
			_(
				"MSL warehouse <b>{0}</b> was disabled (month-end close) and cannot be "
				"re-enabled. Only a System Manager can re-enable it."
			).format(doc.name)
		)


def get_permission_query_conditions(user=None):
	"""req #6 — department-wise visibility for Employee (MSL) warehouses.

	Non-privileged users see every non-employee warehouse (shared / department /
	group / company) plus only the employee warehouses whose employee is in their
	own department. Privileged roles and users without a resolvable department see
	no employee-scoping (the latter simply see non-employee warehouses).

	Deliberately scopes ONLY employee warehouses so cross-department stock
	operations and warehouse link-field dropdowns keep working.
	"""
	user = user or frappe.session.user
	if _is_privileged(user):
		return ""

	dept = get_user_department(user)
	if not dept:
		return "(`tabWarehouse`.employee IS NULL)"

	dept_esc = frappe.db.escape(dept)
	return (
		"(`tabWarehouse`.employee IS NULL "
		f"OR `tabWarehouse`.employee IN "
		f"(SELECT name FROM `tabEmployee` WHERE department = {dept_esc}))"
	)
