# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Seedless fixtures for the work-order current-operation guard (``current_operation_guard``).

WHAT THIS BUILDS
----------------
A complete, self-contained manufacturing "world" on an EMPTY site -- Company, Manufacturer,
departments with their Manufacturing and in-transit warehouses, employees with their own
Manufacturing warehouses, one Department Operation per department and three raw-material items --
plus submitted work orders whose ``manufacturing_operation`` pointer names a seeded Manufacturing
Operation that carries a real MOP Log balance. On top of it, helpers drive Employee IR and
Department IR through the REAL controllers (``insert`` / ``submit`` / ``cancel`` / ``discard``),
so tests exercise the production lifecycle rather than a model of it.

WHY db_insert
-------------
Masters and seeds are written with ``db_insert()``: it runs no hooks and validates nothing, so a
world does not depend on ERPNext setup data (the disposable site has no Company, Item Group or
UOM) and does not drag in controllers that are not under test. Everything the guard reads is a
real row; everything the IR controllers write goes through their real code.

NAMES
-----
Every name carries the world's ``prefix`` (``TCOG-<token>``): masters and fixture operations start
with it, items embed it after their family letter (``M-TCOG-...``: the first letter is the MOP Log
weight family), and IR documents are named ``<prefix>-EIR-001`` / ``<prefix>-DIR-001`` through
``insert(set_name=...)``, so no shared naming series is touched. Operations the controllers mint
(``MOP-YYMM-XXXXXX``) and their MOP Logs are reached through the prefixed work orders.
:func:`purge` deletes all of it.

TRANSACTIONS
------------
Nothing here commits. ``tests/test_current_operation_guard.py`` builds a world per test and rolls
the whole transaction back afterwards. A script that must commit (multi-process race tests) calls
:func:`purge` with the same prefix when it is done, commits, and should check that a second purge
reports nothing left. :func:`purge` covers every row a world plus a full IR lifecycle writes --
including the controller-minted operations' time logs, Versions, Deleted Documents and "Deleted"
feed Comments, Submission Queue rows and the world's own naming-series rows (checked 2026-10-06
by listing, for every table, the rows created during a lifecycle: none left after a purge).

This module imports nothing from the guard, so the same builder also runs against code that has no
guard at all (the incident reproduction on the original ``kggk_prod`` code loads it by path).

LEGACY STATES
-------------
Two helpers reach states the guard itself now refuses to create. Use them ONLY for those:

* :func:`hidden` hides a saved document while other transactions run, then restores it verbatim.
  It emulates the 2026-10-01 REPEATABLE READ race (Employee Issue 43408 could not see draft
  43405) and history written while a draft sat open on code without the guard: the draft is
  created by the real controller, and so is everything that happens while it is hidden.
* :func:`raw_employee_ir` writes an Employee IR straight to the database: a draft left over from
  before the deploy, or two submitted Issues on one operation.

Anything creatable today goes through the controllers.
"""

from contextlib import contextmanager

import frappe
from frappe.utils import add_to_date, cstr, flt, now_datetime, nowdate, random_string

BASE_PREFIX = "TCOG"

# Department key -> department label. Keys are what every helper takes.
DEPARTMENTS = {
	"MM": "Model Making",
	"PP": "Pre Polish",
	"DS": "Diamond Setting",
	"FP": "Final Polish",
}

# Employee key -> home department key.
EMPLOYEES = {"MM1": "MM", "MM2": "MM", "PP1": "PP", "FP1": "FP"}

# Same values as current_operation_guard.OPEN_STATUSES (kept local: see the module docstring).
OPEN_STATUSES = ("Not Started", "On Hold", "WIP", "QC Pending", "QC Completed")

SEED_METAL_QTY = 5.0
SEED_FINDING_QTY = 0.5


# ----------------------------------------------------------------------------------------------
# world
# ----------------------------------------------------------------------------------------------


def new_prefix(base=BASE_PREFIX):
	"""``TCOG-<6 random lowercase alphanumerics>`` -- unique per world."""
	return f"{base}-{random_string(6).lower()}"


def next_name(world, kind):
	"""``<prefix>-<KIND>-<NNN>``: a fresh name for a fixture row or an IR document."""
	world.counters[kind] = world.counters.get(kind, 0) + 1
	return f"{world.prefix}-{kind}-{world.counters[kind]:03d}"


def _insert(world, doctype, values):
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.db_insert()
	world.created.append((doctype, doc.name))
	return doc.name


def make_world(prefix=None, departments=None, employees=None, subcontractors=("SC1",)):
	"""Masters for one isolated test world. Returns a ``frappe._dict``:

	``prefix, abbr, company, manufacturer, item.metal / item.alloy / item.finding,
	departments[key], department_warehouse[key], transit_warehouse[key], operations[key],
	employees[key], employee_department[key], employee_warehouse[key],
	subcontractors[key], subcontractor_warehouse[key]``.

	Each department gets a Manufacturing warehouse (``department`` set) whose
	``default_in_transit_warehouse`` is the department's own in-transit warehouse, and one
	Department Operation with every optional behaviour off (no tree, no QC, no raw material,
	no finding repack, no mould, no receive delay). Employee warehouses are Manufacturing
	warehouses keyed by ``employee`` only: giving them a department would let the department
	warehouse lookup (``{department, warehouse_type}``) return the employee's instead.
	Subcontractors are Suppliers with a Manufacturing warehouse keyed by ``subcontractor``.
	"""
	prefix = prefix or new_prefix()
	departments = departments or tuple(DEPARTMENTS)
	employees = employees or dict(EMPLOYEES)
	token = prefix.rsplit("-", 1)[-1]
	world = frappe._dict(
		prefix=prefix,
		abbr=f"T{token}".upper(),
		counters={},
		created=[],
		item=frappe._dict(),
		subcontractors=frappe._dict(),
		subcontractor_warehouse=frappe._dict(),
		departments=frappe._dict(),
		department_warehouse=frappe._dict(),
		transit_warehouse=frappe._dict(),
		operations=frappe._dict(),
		employees=frappe._dict(),
		employee_department=frappe._dict(),
		employee_warehouse=frappe._dict(),
	)
	abbr = world.abbr

	world.company = _insert(
		world,
		"Company",
		{
			"name": f"{prefix} Jewels",
			"company_name": f"{prefix} Jewels",
			"abbr": abbr,
			"default_currency": "INR",
			"country": "India",
		},
	)
	world.manufacturer = _insert(
		world,
		"Manufacturer",
		{
			"name": f"{prefix}-MFR",
			"short_name": f"{prefix}-MFR",
			"full_name": f"{prefix} Manufacturer",
			"company": world.company,
		},
	)
	for key, item_code in (
		("metal", f"M-{prefix}-GOLD"),
		("alloy", f"M-{prefix}-ALLOY"),
		("finding", f"F-{prefix}-HOOK"),
	):
		world.item[key] = _insert(
			world,
			"Item",
			{
				"name": item_code,
				"item_code": item_code,
				"item_name": item_code,
				"is_stock_item": 1,
				"stock_uom": "Gram",
			},
		)

	for key in departments:
		label = DEPARTMENTS.get(key, key)
		department = _insert(
			world,
			"Department",
			{
				"name": f"{prefix} {label} - {abbr}",
				"department_name": f"{prefix} {label}",
				"company": world.company,
				"manufacturer": world.manufacturer,
				"is_group": 0,
			},
		)
		world.departments[key] = department
		transit = _insert(
			world,
			"Warehouse",
			{
				"name": f"{prefix} {label} Transit - {abbr}",
				"warehouse_name": f"{prefix} {label} Transit",
				"company": world.company,
				"warehouse_type": "Transit",
				"is_group": 0,
				"disabled": 0,
			},
		)
		world.transit_warehouse[key] = transit
		world.department_warehouse[key] = _insert(
			world,
			"Warehouse",
			{
				"name": f"{prefix} {label} Mfg - {abbr}",
				"warehouse_name": f"{prefix} {label} Mfg",
				"company": world.company,
				"department": department,
				"warehouse_type": "Manufacturing",
				"default_in_transit_warehouse": transit,
				"is_group": 0,
				"disabled": 0,
			},
		)
		operation = f"{prefix} {key} Op"
		world.operations[key] = _insert(
			world,
			"Department Operation",
			{
				"name": operation,
				"operation": operation,
				"department": department,
				"company": world.company,
				"manufacturer": world.manufacturer,
				"tree_no_reqd": 0,
				"is_qc_reqd": 0,
				"is_raw_material": 0,
				"is_finding_repack_requirement": 0,
				"is_mould_manufacturer": 0,
				"allow_finding_mwo": 0,
				"employee_ir_receive_delay": 0,
			},
		)

	for key, department_key in employees.items():
		employee = _insert(
			world,
			"Employee",
			{
				"name": f"{prefix}-EMP-{key}",
				"first_name": f"{prefix} {key}",
				"employee_name": f"{prefix} {key}",
				"company": world.company,
				"department": world.departments.get(department_key),
				"status": "Active",
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
			},
		)
		world.employees[key] = employee
		world.employee_department[key] = department_key
		world.employee_warehouse[key] = _insert(
			world,
			"Warehouse",
			{
				"name": f"{prefix} {key} Mfg - {abbr}",
				"warehouse_name": f"{prefix} {key} Mfg",
				"company": world.company,
				"employee": employee,
				"warehouse_type": "Manufacturing",
				"is_group": 0,
				"disabled": 0,
			},
		)

	for key in subcontractors or ():
		supplier = _insert(
			world,
			"Supplier",
			{
				"name": f"{prefix}-SUP-{key}",
				"supplier_name": f"{prefix} {key}",
				"supplier_type": "Company",
				"disabled": 0,
			},
		)
		world.subcontractors[key] = supplier
		world.subcontractor_warehouse[key] = _insert(
			world,
			"Warehouse",
			{
				"name": f"{prefix} {key} Mfg - {abbr}",
				"warehouse_name": f"{prefix} {key} Mfg",
				"company": world.company,
				"subcontractor": supplier,
				"warehouse_type": "Manufacturing",
				"is_group": 0,
				"disabled": 0,
			},
		)
	return world


# ----------------------------------------------------------------------------------------------
# work orders, operations, balances
# ----------------------------------------------------------------------------------------------


def make_work_order(
	world, department="MM", *, seed=True, docstatus=1, operation_status="Not Started"
):
	"""A work order (``docstatus`` 1 by default) whose pointer is a fresh operation in
	``department``. Returns ``(mwo, mop)``. ``seed`` gives the operation a MOP Log balance."""
	mwo = _insert(
		world,
		"Manufacturing Work Order",
		{
			"name": next_name(world, "MWO"),
			"docstatus": docstatus,
			"company": world.company,
			"manufacturer": world.manufacturer,
			"department": world.departments[department],
			"status": "Not Started" if docstatus == 1 else "Draft",
			"posting_date": nowdate(),
			"qty": 1,
			"is_finding_mwo": 0,
			"for_fg": 0,
		},
	)
	mop = make_operation(world, mwo, department, status=operation_status)
	set_pointer(mwo, mop)
	if seed:
		seed_balance(world, mop, mwo)
	return mwo, mop


def make_operation(
	world,
	mwo,
	department,
	*,
	status="Not Started",
	previous_mop=None,
	department_ir_status=None,
	operation=None,
	employee=None,
	department_issue_id=None,
	department_receive_id=None,
	creation=None,
):
	"""A Manufacturing Operation row written directly (no controller).

	``operation`` and ``employee`` are world keys (``"MM"``, ``"MM1"``) or ``None``.
	"""
	values = {
		"name": next_name(world, "MOP"),
		"company": world.company,
		"manufacturer": world.manufacturer,
		"department": world.departments[department],
		"manufacturing_work_order": mwo,
		"status": status,
		"previous_mop": previous_mop,
		"department_ir_status": department_ir_status,
		"operation": world.operations[operation] if operation else None,
		"employee": world.employees[employee] if employee else None,
		"department_issue_id": department_issue_id,
		"department_receive_id": department_receive_id,
		"type": "Manufacturing Work Order",
		"qty": 1,
	}
	if creation:
		values["creation"] = values["modified"] = creation
		values["owner"] = values["modified_by"] = "Administrator"
	return _insert(world, "Manufacturing Operation", values)


def set_pointer(mwo, mop):
	frappe.db.set_value(
		"Manufacturing Work Order",
		mwo,
		"manufacturing_operation",
		mop,
		update_modified=False,
	)


def seed_balance(
	world,
	mop,
	mwo,
	*,
	balance=None,
	is_synced=1,
	creation=None,
	voucher_type="Manufacturing Operation",
	voucher_no=None,
	flow_index=0,
	warehouse=None,
	set_header=True,
):
	"""MOP Log balance rows for ``mop`` (default: metal 5.0 g + finding 0.5 g).

	``set_header`` writes the matching weight buckets onto the operation, the state
	``MOPLog.validate`` would leave behind.
	"""
	balance = balance or {
		world.item.metal: SEED_METAL_QTY,
		world.item.finding: SEED_FINDING_QTY,
	}
	department = frappe.db.get_value("Manufacturing Operation", mop, "department")
	warehouse = warehouse or frappe.db.get_value(
		"Warehouse",
		{"department": department, "warehouse_type": "Manufacturing", "disabled": 0},
	)
	names = []
	for item_code, qty in balance.items():
		values = {
			"name": next_name(world, "LOG"),
			"item_code": item_code,
			"qty_change": qty,
			"qty_after_transaction": qty,
			"qty_after_transaction_item_based": qty,
			"qty_after_transaction_batch_based": qty,
			"pcs_change": 0,
			"pcs_after_transaction": 0,
			"pcs_after_transaction_item_based": 0,
			"pcs_after_transaction_batch_based": 0,
			"to_warehouse": warehouse,
			"voucher_type": voucher_type,
			"voucher_no": voucher_no or mop,
			"manufacturing_operation": mop,
			"manufacturing_work_order": mwo,
			"is_synced": is_synced,
			"is_cancelled": 0,
			"flow_index": flow_index,
		}
		if creation:
			values["creation"] = values["modified"] = creation
			values["owner"] = values["modified_by"] = "Administrator"
		names.append(_insert(world, "MOP Log", values))
	if set_header:
		metal = sum(q for i, q in balance.items() if cstr(i).startswith("M"))
		finding = sum(q for i, q in balance.items() if cstr(i).startswith("F"))
		frappe.db.set_value(
			"Manufacturing Operation",
			mop,
			{
				"net_wt": flt(metal, 3),
				"finding_wt": flt(finding, 3),
				"gross_wt": flt(metal + finding, 3),
			},
			update_modified=False,
		)
	return names


# ----------------------------------------------------------------------------------------------
# Employee IR / Department IR through the real controllers
# ----------------------------------------------------------------------------------------------


def _pairs(rows):
	"""``[(mop, mwo)]`` from ``(mop, mwo)`` tuples or bare work-order names (their pointer)."""
	pairs = []
	for row in rows:
		if isinstance(row, list | tuple):
			pairs.append((row[0], row[1]))
		else:
			pairs.append((pointer(row), row))
	return pairs


def employee_ir(
	world,
	typ,
	rows,
	*,
	department="MM",
	employee="MM1",
	operation=None,
	received=None,
	subcontractor=None,
	**fields,
):
	"""An UNSAVED Employee IR. Save it with :func:`insert_ir` (fixture name, no naming series).

	A Receive row's ``received_gross_wt`` defaults to the operation's ``gross_wt`` (no loss).
	``subcontractor`` (a world key) makes it a subcontracting document with no employee.
	``custom_transfer_type`` is blanked: its Link default ``Next Operation`` is an Attribute
	Value the disposable site does not have.
	"""
	doc = frappe.new_doc("Employee IR")
	doc.update(
		{
			"type": typ,
			"company": world.company,
			"manufacturer": world.manufacturer,
			"department": world.departments[department],
			"operation": world.operations[operation or department],
			"employee": world.employees[employee]
			if employee and not subcontractor
			else None,
			"subcontracting": "Yes" if subcontractor else "No",
			"subcontractor": world.subcontractors[subcontractor]
			if subcontractor
			else None,
			"custom_transfer_type": "",
		}
	)
	doc.update(fields)
	for mop, mwo in _pairs(rows):
		row = {"manufacturing_operation": mop, "manufacturing_work_order": mwo}
		if typ == "Receive":
			gross = flt(
				frappe.db.get_value("Manufacturing Operation", mop, "gross_wt"), 3
			)
			row["received_gross_wt"] = gross if received is None else received
		doc.append("employee_ir_operations", row)
	doc.flags.fixture_name = next_name(world, "EIR")
	return doc


def department_ir(
	world,
	typ,
	rows,
	*,
	current,
	to=None,
	previous=None,
	receive_against=None,
	**fields,
):
	"""An UNSAVED Department IR (``to`` = next department key, ``previous`` for a Receive)."""
	doc = frappe.new_doc("Department IR")
	doc.update(
		{
			"type": typ,
			"company": world.company,
			"manufacturer": world.manufacturer,
			"current_department": world.departments[current],
			"next_department": world.departments[to] if to else None,
			"previous_department": world.departments[previous] if previous else None,
			"receive_against": receive_against,
		}
	)
	doc.update(fields)
	for mop, mwo in _pairs(rows):
		doc.append(
			"department_ir_operation",
			{"manufacturing_operation": mop, "manufacturing_work_order": mwo},
		)
	doc.flags.fixture_name = next_name(world, "DIR")
	return doc


def insert_ir(doc):
	"""``doc.insert()`` under the fixture name (``set_name``), i.e. without a naming series."""
	return doc.insert(set_name=doc.flags.get("fixture_name"))


def reload(doc_or_name, doctype=None):
	"""A freshly loaded copy -- what desk, ``frappe.client`` and the Submission Queue worker
	submit. An in-memory document that was just inserted still carries ``None`` in Float
	fields the database stores as 0 (``rpt_wt_issue``), which the Issue bulk update cannot
	write into its NOT NULL column."""
	if isinstance(doc_or_name, str):
		return frappe.get_doc(doctype, doc_or_name)
	return frappe.get_doc(doc_or_name.doctype, doc_or_name.name)


def submit_ir(doc_or_name, doctype=None):
	"""Submit a freshly loaded copy (see :func:`reload`); returns it."""
	doc = reload(doc_or_name, doctype)
	doc.submit()
	return doc


def _as_rows(mwos):
	"""One work order (its pointer), a ``(mop, mwo)`` pair, or a list of either."""
	return list(mwos) if isinstance(mwos, list) else [mwos]


def issue(world, mwos, *, department="MM", employee="MM1", submit=True):
	"""Employee Issue of the work order(s)' current operation (submitted by default)."""
	doc = insert_ir(
		employee_ir(
			world, "Issue", _as_rows(mwos), department=department, employee=employee
		)
	)
	return submit_ir(doc) if submit else doc


def receive(world, mwos, *, department="MM", employee="MM1", submit=True):
	"""Employee Receive of the work order(s)' current operation, received at full weight."""
	doc = insert_ir(
		employee_ir(
			world, "Receive", _as_rows(mwos), department=department, employee=employee
		)
	)
	return submit_ir(doc) if submit else doc


def work_cycle(world, mwos, *, department="MM", employee="MM1"):
	"""Issue then Receive in one department: the operation finishes, a successor is minted."""
	return (
		issue(world, mwos, department=department, employee=employee),
		receive(world, mwos, department=department, employee=employee),
	)


def transfer(world, mwos, *, current, to, receive=True):
	"""Department Issue (``current`` -> ``to``) and, by default, its Department Receive."""
	dir_issue = submit_ir(
		insert_ir(department_ir(world, "Issue", _as_rows(mwos), current=current, to=to))
	)
	if not receive:
		return dir_issue, None
	dir_receive = submit_ir(
		insert_ir(
			department_ir(
				world,
				"Receive",
				# the In-Transit operations the Issue minted are now the pointers
				[
					row[1] if isinstance(row, list | tuple) else row
					for row in _as_rows(mwos)
				],
				current=to,
				previous=current,
				receive_against=dir_issue.name,
			)
		)
	)
	return dir_issue, dir_receive


@contextmanager
def hidden(doctype, name):
	"""Make a saved document invisible inside the block, then put it back exactly as it was.

	This emulates a transaction that cannot see the document. On 2026-10-01 Employee Issue
	43408's REPEATABLE READ snapshot predated draft 43405's commit, so its duplicate check missed
	the draft; everything that followed ran on code that never looked for drafts. Under the guard
	the open draft would refuse all of that (``OutstandingDraftError``), so the only way to reach
	the incident's end state through the real controllers is to hide the draft while the history
	is written. The parent row and every child row are captured as stored (``creation`` and
	``modified`` included), deleted, and re-inserted unchanged on exit.
	"""
	captured = []
	tables = [doctype] + _child_tables(doctype)
	for table in tables:
		where = (
			"name = %(name)s"
			if table == doctype
			else "parent = %(name)s AND parenttype = %(dt)s"
		)
		values = {"name": name, "dt": doctype}
		rows = frappe.db.sql(
			f"SELECT * FROM `tab{table}` WHERE {where}", values, as_dict=True
		)
		captured.append((table, where, rows))
	if not captured[0][2]:
		raise ValueError(f"{doctype} {name} does not exist")
	for table, where, _rows in captured:
		frappe.db.sql(f"DELETE FROM `tab{table}` WHERE {where}", values)
	frappe.clear_document_cache(doctype, name)
	try:
		yield
	finally:
		for table, _where, rows in captured:
			for row in rows:
				columns = list(row)
				frappe.db.sql(
					"INSERT INTO `tab{0}` ({1}) VALUES ({2})".format(
						table,
						", ".join(f"`{c}`" for c in columns),
						", ".join(["%s"] * len(columns)),
					),
					[row[c] for c in columns],
				)
		frappe.clear_document_cache(doctype, name)


def raw_employee_ir(
	world,
	typ,
	rows,
	*,
	department="MM",
	employee="MM1",
	operation=None,
	docstatus=0,
	creation=None,
):
	"""Write an Employee IR (parent + rows) straight to the database; returns its name.

	Only for LEGACY states (see the module docstring). ``creation`` backdates it.
	"""
	name = next_name(world, "EIR")
	creation = creation or now_datetime()
	frappe.get_doc(
		{
			"doctype": "Employee IR",
			"name": name,
			"docstatus": docstatus,
			"creation": creation,
			"modified": creation,
			"owner": "Administrator",
			"modified_by": "Administrator",
			"type": typ,
			"company": world.company,
			"manufacturer": world.manufacturer,
			"department": world.departments[department],
			"operation": world.operations[operation or department],
			"employee": world.employees[employee] if employee else None,
			"subcontracting": "No",
			"date_time": creation,
			"issue_submitted_on": creation
			if docstatus == 1 and typ == "Issue"
			else None,
		}
	).db_insert()
	world.created.append(("Employee IR", name))
	for idx, (mop, mwo) in enumerate(_pairs(rows), 1):
		gross = flt(frappe.db.get_value("Manufacturing Operation", mop, "gross_wt"), 3)
		frappe.get_doc(
			{
				"doctype": "Employee IR Operation",
				"name": f"{name}-ROW-{idx}",
				"parent": name,
				"parenttype": "Employee IR",
				"parentfield": "employee_ir_operations",
				"idx": idx,
				"docstatus": docstatus,
				"creation": creation,
				"modified": creation,
				"owner": "Administrator",
				"modified_by": "Administrator",
				"manufacturing_operation": mop,
				"manufacturing_work_order": mwo,
				"gross_wt": gross,
			}
		).db_insert()
	return name


def days_ago(days):
	return add_to_date(now_datetime(), days=-days)


# ----------------------------------------------------------------------------------------------
# state readers
# ----------------------------------------------------------------------------------------------


def pointer(mwo):
	return frappe.db.get_value(
		"Manufacturing Work Order", mwo, "manufacturing_operation"
	)


def operations(mwo):
	"""Every operation of the work order, oldest first."""
	return frappe.get_all(
		"Manufacturing Operation",
		filters={"manufacturing_work_order": mwo},
		fields=[
			"name",
			"status",
			"department",
			"department_ir_status",
			"operation",
			"employee",
			"previous_mop",
			"department_issue_id",
			"department_receive_id",
			"employee_ir",
		],
		order_by="creation asc, name asc",
		limit_page_length=0,
	)


def operation(mop):
	return frappe.db.get_value(
		"Manufacturing Operation",
		mop,
		[
			"name",
			"status",
			"department",
			"department_ir_status",
			"operation",
			"employee",
			"previous_mop",
			"department_issue_id",
			"department_receive_id",
		],
		as_dict=True,
	)


def open_operations(mwo):
	"""Operations that are still live work: an open status and not marked Revert.

	kggk_uat keeps the operation a cancelled Department Issue / Employee Receive minted, marked
	``department_ir_status = "Revert"`` with status Not Started. That row is history, not an open
	operation -- the rule of ``current_operation_guard.is_open_operation`` (not imported here:
	see the module docstring).
	"""
	return [
		r.name
		for r in operations(mwo)
		if r.status in OPEN_STATUSES and r.department_ir_status != "Revert"
	]


def reverted_operations(mwo):
	"""Operations a cancel left behind as Revert history (kggk_uat), oldest first."""
	return [
		r.name
		for r in operations(mwo)
		if r.department_ir_status == "Revert" or r.status == "Revert"
	]


def time_logs(mop):
	return frappe.get_all(
		"Manufacturing Operation Time Log",
		filters={"parent": mop, "parenttype": "Manufacturing Operation"},
		fields=["name", "from_time", "to_time", "employee"],
		order_by="idx asc",
		limit_page_length=0,
	)


def mop_logs(**filters):
	return frappe.get_all(
		"MOP Log",
		filters=filters,
		fields=[
			"name",
			"manufacturing_operation",
			"manufacturing_work_order",
			"item_code",
			"voucher_type",
			"voucher_no",
			"is_cancelled",
			"is_synced",
			"qty_after_transaction_batch_based",
			"flow_index",
		],
		order_by="creation asc, name asc",
		limit_page_length=0,
	)


def snapshot(*mwos):
	"""Everything the guard protects, per work order: the pointer and department, every
	operation's state, its time logs and every MOP Log row. Equal snapshots before and after
	a refused transaction prove it had no side effect."""
	state = {}
	for mwo in mwos:
		ops = operations(mwo)
		state[mwo] = {
			"pointer": pointer(mwo),
			"department": frappe.db.get_value(
				"Manufacturing Work Order", mwo, "department"
			),
			"operations": [dict(r) for r in ops],
			"time_logs": {r.name: [dict(t) for t in time_logs(r.name)] for r in ops},
			"mop_logs": [dict(r) for r in mop_logs(manufacturing_work_order=mwo)],
		}
	return state


# ----------------------------------------------------------------------------------------------
# cleanup
# ----------------------------------------------------------------------------------------------


def clear_caches(world):
	"""Drop the Redis / request document cache entries of a rolled-back world."""
	for doctype, name in world.get("created") or []:
		frappe.clear_document_cache(doctype, name)


def _child_tables(doctype):
	return [df.options for df in frappe.get_meta(doctype).get_table_fields()]


def _delete(table, condition, values):
	frappe.db.sql(f"DELETE FROM `{table}` WHERE {condition}", values)
	return frappe.db._cursor.rowcount or 0


def purge(prefix):
	"""Delete every row a world with ``prefix`` wrote -- and everything the controllers wrote
	for it -- in the current transaction. Returns ``{table: rows_deleted}`` (non-zero only).

	Call it on a COMMITTED world (the caller commits afterwards); on an uncommitted one a
	rollback is cheaper and complete. ``tabError Log`` is MyISAM, so its rows go immediately.
	"""
	if not prefix or len(prefix) < 6 or "%" in prefix or "_" in prefix:
		raise ValueError(f"refusing to purge an unsafe prefix: {prefix!r}")
	start = f"{prefix}%"
	anywhere = f"%{prefix}%"
	deleted = {}

	def count(table, n):
		if n:
			deleted[table] = deleted.get(table, 0) + n

	mops = frappe.db.sql_list(
		"""
		SELECT name FROM `tabManufacturing Operation`
		WHERE name LIKE %(start)s OR manufacturing_work_order LIKE %(start)s
		""",
		{"start": start},
	)
	# Operations the controllers minted are named MOP-YYMM-XXXXXX, not by prefix, and a cancel
	# may already have deleted one: its Deleted Document still names the prefixed work order.
	deleted_mops = frappe.db.sql_list(
		"""
		SELECT deleted_name FROM `tabDeleted Document`
		WHERE deleted_doctype = 'Manufacturing Operation' AND data LIKE %(anywhere)s
		""",
		{"anywhere": anywhere},
	)
	every_mop = sorted(set(mops) | set(deleted_mops))
	if every_mop:
		# Manufacturing Operation tracks changes (Version) and every delete leaves a "Deleted"
		# feed Comment whose subject is "<doctype> <name>".
		count(
			"tabVersion",
			_delete(
				"tabVersion",
				"ref_doctype = 'Manufacturing Operation' AND docname IN %(mops)s",
				{"mops": every_mop},
			),
		)
		count(
			"tabComment",
			_delete(
				"tabComment",
				"reference_doctype = 'Manufacturing Operation' AND ("
				" reference_name IN %(mops)s"
				" OR (comment_type = 'Deleted' AND SUBSTRING_INDEX(subject, ' ', -1) IN %(mops)s))",
				{"mops": every_mop},
			),
		)
	count(
		"tabMOP Log",
		_delete(
			"tabMOP Log",
			"name LIKE %(start)s OR manufacturing_work_order LIKE %(start)s"
			" OR voucher_no LIKE %(anywhere)s",
			{"start": start, "anywhere": anywhere},
		),
	)
	if mops:
		for child in _child_tables("Manufacturing Operation"):
			count(
				f"tab{child}",
				_delete(
					f"tab{child}",
					"parent IN %(mops)s AND parenttype = 'Manufacturing Operation'",
					{"mops": mops},
				),
			)
		count(
			"tabMOP Log",
			_delete(
				"tabMOP Log", "manufacturing_operation IN %(mops)s", {"mops": mops}
			),
		)
		count(
			"tabManufacturing Operation",
			_delete("tabManufacturing Operation", "name IN %(mops)s", {"mops": mops}),
		)

	for doctype in ("Employee IR", "Department IR", "Manufacturing Work Order"):
		for child in _child_tables(doctype):
			count(
				f"tab{child}",
				_delete(
					f"tab{child}", "parent LIKE %(anywhere)s", {"anywhere": anywhere}
				),
			)
		count(
			f"tab{doctype}",
			_delete(f"tab{doctype}", "name LIKE %(anywhere)s", {"anywhere": anywhere}),
		)

	for table, column in (
		("tabSubmission Queue", "ref_docname"),
		("tabVersion", "docname"),
		("tabComment", "reference_name"),
		("tabComment", "subject"),  # "Deleted" feed of a prefixed document
		("tabDeleted Document", "deleted_name"),
		("tabDeleted Document", "data"),  # e.g. a minted operation deleted by a cancel
		("tabSeries", "name"),
		("tabError Log", "reference_name"),
		("tabError Log", "method"),
	):
		count(
			table,
			_delete(table, f"`{column}` LIKE %(anywhere)s", {"anywhere": anywhere}),
		)

	for doctype in (
		"Department Operation",
		"Warehouse",
		"Employee",
		"Supplier",
		"Department",
		"Item",
		"Manufacturer",
		"Company",
	):
		count(
			f"tab{doctype}",
			_delete(f"tab{doctype}", "name LIKE %(anywhere)s", {"anywhere": anywhere}),
		)
	return deleted
