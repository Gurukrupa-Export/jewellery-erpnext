# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Diamond / gemstone loss booked against an employee from a Manufacturing Operation.

The "Book Loss of Diamond/Gemstone" button on a Manufacturing Operation (shown when its
Department ticks ``custom_enable_loss_booking_for_diamond_gemstone``) opens a dialog over the
operation's diamond and gemstone balance. ``book_loss`` books the filled rows in that one
request, so either everything below happens or nothing does:

1. **Stock.** One "Process Loss" (Repack) Stock Entry per employee: the stone leaves the
   reservation's warehouse and comes back as its loss variant -- the Manufacturer's Variant
   Loss Table maps (variant, Loss Type) to the template, e.g. D + Burn -> DB, D + CVD
   Rejection -> DCVR -- in the department warehouse whose Warehouse Type the Loss Type names
   (``Loss Type.destination_warehouse_type``). The entry is the only record of the booking.
2. **Reservation.** The loss is taken off the work order's Stock Reservation Entry with the
   Employee IR helpers (``loss_stock_entry._find_sre`` / ``_reduce_sre``). The replacement
   reservation's ``employee_ir`` (a Data marker, no link) carries the Stock Entry's name; a
   reservation the loss used up entirely is stamped the same way after it is cancelled.
3. **Balance.** A negative MOP Log row per consumed stone on this operation, which also
   recomputes the operation's diamond / gemstone weights and pcs. "Process Loss" is not a
   reservation-tracked entry type, so the Stock Entry's own submit writes no MOP Log.

Cancelling the Stock Entry reverses all three (:func:`guard_loss_entry_cancel`,
:func:`restore_loss_entry_reservations`): its MOP Log rows go with the generic Stock Entry
cancel, the reservations are restored from the markers. Refused once the operation has moved
on -- its balance went with it -- or once a later posting reduced the same reservation again.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, nowtime, today

DEPARTMENT_FLAG = "custom_enable_loss_booking_for_diamond_gemstone"
PROCESS_LOSS_SE_TYPE = "Process Loss"
# Item.variant_of templates this booking handles.
LOSS_VARIANTS = ("D", "G")
# An operation in one of these no longer holds its material: Finished / Revert handed the
# balance on, Hold is the Parent Manufacturing Order's hold.
CLOSED_STATUSES = ("Finished", "Revert", "Hold")
# How the dialog's table reads in messages from the shared reservation helpers.
TABLE_LABEL = "Loss Details"
QTY_PRECISION = 3
TOLERANCE = 0.0001
# Set in before_cancel on a Stock Entry booked here; on_cancel runs after the generic cancel
# has already flagged the entry's MOP Log rows, so it can no longer tell by itself.
LOSS_ENTRY_FLAG = "is_mop_dg_loss_entry"


# ---------------------------------------------------------------------------
# Operation, balance and masters
# ---------------------------------------------------------------------------


def get_operation(name):
	mop = frappe.db.get_value(
		"Manufacturing Operation",
		name,
		[
			"name",
			"department",
			"status",
			"employee",
			"operation",
			"manufacturing_work_order",
			"manufacturing_order",
			"company",
			"manufacturer",
		],
		as_dict=True,
	)
	if not mop:
		frappe.throw(_("Manufacturing Operation {0} does not exist.").format(name))
	return mop


def is_department_enabled(department):
	if not department or not frappe.get_meta("Department").has_field(DEPARTMENT_FLAG):
		return False
	return bool(cint(frappe.db.get_value("Department", department, DEPARTMENT_FLAG)))


def get_operation_block_reason(mop):
	"""Why loss cannot be booked on this operation, or None when it can."""
	if not is_department_enabled(mop.department):
		return _(
			"Loss booking for diamond / gemstone is not enabled for department {0}."
		).format(mop.department or _("(none)"))
	if mop.status in CLOSED_STATUSES:
		return _(
			"Manufacturing Operation {0} is {1}. Loss can only be booked on an operation that "
			"still holds the material."
		).format(mop.name, mop.status)
	if not mop.manufacturing_work_order:
		return _("Manufacturing Operation {0} has no Manufacturing Work Order.").format(
			mop.name
		)
	current = frappe.db.get_value(
		"Manufacturing Work Order",
		mop.manufacturing_work_order,
		"manufacturing_operation",
	)
	if current != mop.name:
		return _(
			"Manufacturing Operation {0} is not the current operation of work order {1} "
			"(current: {2}). Book the loss on the current operation."
		).format(mop.name, mop.manufacturing_work_order, current or _("none"))
	return None


def get_dg_balance(manufacturing_operation):
	"""``{(item_code, batch_no): row}`` of the operation's diamonds / gemstones with qty left.

	Read from the MOP Log ledger, which already nets off every earlier loss booked on the
	operation, so a second booking only sees what remains.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_available_qty_pcs_for_mop_item,
		get_current_mop_balance_rows,
	)

	balance_map = {
		(r.get("item_code"), r.get("batch_no")): r
		for r in get_current_mop_balance_rows(manufacturing_operation)
	}
	item_codes = sorted({item for item, batch in balance_map if item and batch})
	if not item_codes:
		return {}
	items = {
		i.name: i
		for i in frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "variant_of", "stock_uom"],
		)
	}

	balance = {}
	for (item_code, batch_no), _row in balance_map.items():
		item = items.get(item_code)
		if not (batch_no and item and item.variant_of in LOSS_VARIANTS):
			continue
		available = get_available_qty_pcs_for_mop_item(
			manufacturing_operation,
			item_code,
			batch_no,
			mop_log_balance_map=balance_map,
		)
		qty = flt(available.get("available_qty"), QTY_PRECISION)
		if qty <= TOLERANCE:
			continue
		balance[(item_code, batch_no)] = frappe._dict(
			item_code=item_code,
			batch_no=batch_no,
			variant_of=item.variant_of,
			stock_uom=item.stock_uom,
			available_qty=qty,
			available_pcs=cint(available.get("available_pcs")),
		)
	return balance


def get_allowed_employees(mop):
	"""Employees who worked this operation's work order in its department, in a stable order.

	The operation's own employee and time logs, and every employee a submitted Employee IR
	Issue of this department handed the work order to (an operation not yet issued, or the
	successor of a received one, carries no employee of its own).
	"""
	employees = []

	def add(employee):
		if employee and employee not in employees:
			employees.append(employee)

	add(mop.employee)
	for employee in frappe.get_all(
		"Manufacturing Operation Time Log",
		filters={"parent": mop.name, "parenttype": "Manufacturing Operation"},
		pluck="employee",
		order_by="idx asc",
	):
		add(employee)
	if mop.department and mop.manufacturing_work_order:
		for (employee,) in frappe.db.sql(
			"""
			SELECT DISTINCT eir.employee
			FROM `tabEmployee IR` eir
			INNER JOIN `tabEmployee IR Operation` op
				ON op.parent = eir.name AND op.parenttype = 'Employee IR'
			WHERE eir.docstatus = 1
			  AND eir.type = 'Issue'
			  AND eir.department = %s
			  AND op.manufacturing_work_order = %s
			  AND IFNULL(eir.employee, '') != ''
			ORDER BY eir.employee
			""",
			(mop.department, mop.manufacturing_work_order),
		):
			add(employee)
	return employees


def get_loss_type_map(loss_types):
	"""``{loss_type: destination_warehouse_type}`` for the named Loss Types that exist."""
	if not loss_types:
		return {}
	return {
		row.name: row.destination_warehouse_type
		for row in frappe.get_all(
			"Loss Type",
			filters={"name": ["in", sorted(loss_types)]},
			fields=["name", "destination_warehouse_type"],
		)
	}


def get_loss_variant_template(manufacturer, variant_of, loss_type):
	"""The loss template the Manufacturer maps (variant, loss type) to, e.g. (D, Burn) -> DB."""
	if not (manufacturer and variant_of and loss_type):
		return None
	return frappe.db.get_value(
		"Variant Loss Table",
		{
			"parent": manufacturer,
			"parenttype": "Manufacturer",
			"parentfield": "custom_variant_loss_table",
			"variant": variant_of,
			"loss_type": loss_type,
		},
		"loss_variant",
	)


def get_department_warehouse(department, warehouse_type):
	"""``(warehouse, None)`` for the department's one warehouse of this type, else ``(None, error)``."""
	names = frappe.get_all(
		"Warehouse",
		filters={
			"disabled": 0,
			"is_group": 0,
			"department": department,
			"warehouse_type": warehouse_type,
			"employee": ["is", "not set"],
		},
		pluck="name",
		order_by="name asc",
	)
	if not names:
		return None, _("Department {0} has no {1} warehouse.").format(
			department, warehouse_type
		)
	if len(names) > 1:
		return None, _(
			"Department {0} has more than one {1} warehouse ({2}). Keep exactly one."
		).format(department, warehouse_type, ", ".join(names))
	return names[0], None


def resolve_loss_item(company, manufacturer, row):
	"""The loss variant item for this row, created from the template when it does not exist yet."""
	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
		ensure_loss_item_stockable,
		get_item_loss_item,
	)

	template = get_loss_variant_template(manufacturer, row.variant_of, row.loss_type)
	loss_item = template and get_item_loss_item(
		company,
		row.item_code,
		row.variant_of,
		row.loss_type,
		loss_variant=template,
	)
	if not loss_item:
		frappe.throw(
			_("Row {0}: could not resolve the {1} loss item for {2}.").format(
				row.idx, row.loss_type, row.item_code
			)
		)
	return ensure_loss_item_stockable(loss_item)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_loss_rows(rows, balance, allowed_employees, blocked_variants, loss_types):
	"""Check every row against the live balance and the masters; throw listing all problems.

	``balance`` is :func:`get_dg_balance`, ``loss_types`` is :func:`get_loss_type_map`.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.material_loss_gate import (
		VARIANT_FLAG_LABELS,
	)

	problems = []
	if not rows:
		problems.append(_("Add at least one diamond / gemstone row."))
	seen = {}
	for row in rows:
		label = _("Row {0}").format(row.idx)
		key = (row.item_code, row.batch_no)
		if not (row.item_code and row.batch_no):
			problems.append(
				_("{0}: Item Code and Batch No are required.").format(label)
			)
			continue
		bal = balance.get(key)
		if not bal:
			problems.append(
				_(
					"{0}: {1} batch {2} is not a diamond / gemstone available in this "
					"Manufacturing Operation."
				).format(label, row.item_code, row.batch_no)
			)
			continue
		if key in seen:
			problems.append(
				_(
					"{0}: {1} batch {2} is already on row {3}. Book another loss type for the same "
					"batch in a separate booking."
				).format(label, row.item_code, row.batch_no, seen[key])
			)
			continue
		seen[key] = row.idx

		if bal.variant_of in blocked_variants:
			problems.append(
				_(
					"{0}: this operation does not allow loss on {1} ('{2}' is ticked on its "
					"Department Operation)."
				).format(
					label, row.item_code, _(VARIANT_FLAG_LABELS.get(bal.variant_of, ""))
				)
			)

		if not row.loss_type:
			problems.append(_("{0}: Loss Type is required.").format(label))
		elif row.loss_type not in loss_types:
			problems.append(
				_("{0}: Loss Type {1} does not exist.").format(label, row.loss_type)
			)
		elif not loss_types[row.loss_type]:
			problems.append(
				_(
					"{0}: Loss Type {1} has no Destination Warehouse Type. Set it on the Loss Type."
				).format(label, row.loss_type)
			)

		qty = flt(row.loss_qty, QTY_PRECISION)
		if qty <= 0:
			problems.append(_("{0}: Loss Qty must be greater than 0.").format(label))
		elif qty > bal.available_qty + TOLERANCE:
			problems.append(
				_(
					"{0}: Loss Qty {1} exceeds the available qty {2} of {3} batch {4}."
				).format(label, qty, bal.available_qty, row.item_code, row.batch_no)
			)

		pcs = cint(row.loss_pcs)
		if flt(row.loss_pcs) != pcs or pcs < 0:
			problems.append(_("{0}: Loss PCS must be a whole number.").format(label))
		elif bal.available_pcs > 0:
			if pcs < 1:
				problems.append(_("{0}: Loss PCS is required.").format(label))
			elif pcs > bal.available_pcs:
				problems.append(
					_(
						"{0}: Loss PCS {1} exceeds the available PCS {2} of {3} batch {4}."
					).format(label, pcs, bal.available_pcs, row.item_code, row.batch_no)
				)
		elif pcs > 0:
			problems.append(
				_(
					"{0}: {1} batch {2} has no PCS left on this operation, so Loss PCS must be 0."
				).format(label, row.item_code, row.batch_no)
			)

		if not row.employee:
			problems.append(_("{0}: Employee is required.").format(label))
		elif row.employee not in allowed_employees:
			problems.append(
				_(
					"{0}: Employee {1} has not worked this work order in this department, so the loss "
					"cannot be booked against them."
				).format(label, row.employee)
			)

	if problems:
		frappe.throw("<br>".join(problems), title=_("Loss Booking"))


def validate_no_wastage(rows):
	"""Customer material of a no-wastage customer is never booked as loss (as in Employee IR)."""
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
		batch_owner_no_wastage,
	)

	for row in rows:
		if batch_owner_no_wastage(row.batch_no):
			frappe.throw(
				_(
					"Row {0}: batch {1} is material of a no-wastage customer, so no loss can be "
					"booked on it."
				).format(row.idx, row.batch_no),
				title=_("Loss Booking"),
			)


def prepare_rows(rows):
	"""The dialog rows as ``frappe._dict``\\ s numbered from 1, with only the fields it may send."""
	prepared = []
	for idx, row in enumerate(rows or [], start=1):
		row = frappe._dict(row)
		prepared.append(
			frappe._dict(
				idx=idx,
				item_code=cstr(row.item_code).strip(),
				batch_no=cstr(row.batch_no).strip(),
				loss_type=cstr(row.loss_type).strip(),
				loss_qty=flt(row.loss_qty, QTY_PRECISION),
				loss_pcs=row.loss_pcs or 0,
				employee=cstr(row.employee).strip(),
			)
		)
	return prepared


def set_row_snapshots(rows, balance):
	"""Copy the live balance onto each row: the variant decides the loss item, the uom the rows."""
	for row in rows:
		bal = balance.get((row.item_code, row.batch_no))
		if not bal:
			continue
		row.variant_of = bal.variant_of
		row.stock_uom = bal.stock_uom
		row.available_qty = bal.available_qty
		row.available_pcs = bal.available_pcs


def set_target_warehouses(rows, mop, loss_types):
	"""Check each row's loss variant is configured and resolve its department warehouse."""
	if not mop.manufacturer:
		frappe.throw(
			_(
				"Manufacturing Operation {0} has no Manufacturer, so the loss variant cannot be "
				"looked up."
			).format(mop.name)
		)
	problems = []
	warehouses = {}
	for row in rows:
		if not get_loss_variant_template(
			mop.manufacturer, row.variant_of, row.loss_type
		):
			problems.append(
				_(
					"Row {0}: Manufacturer {1} has no loss variant for {2} with Loss Type {3}. "
					"Add it to the Manufacturer's Variant Loss Table."
				).format(row.idx, mop.manufacturer, row.variant_of, row.loss_type)
			)
			continue
		warehouse_type = loss_types[row.loss_type]
		if warehouse_type not in warehouses:
			warehouses[warehouse_type] = get_department_warehouse(
				mop.department, warehouse_type
			)
		warehouse, error = warehouses[warehouse_type]
		if error:
			problems.append(_("Row {0}: {1}").format(row.idx, error))
			continue
		row.target_warehouse = warehouse
	if problems:
		frappe.throw("<br>".join(problems), title=_("Loss Booking"))


def validate_booking(mop, rows):
	"""Everything the dialog checks, re-checked against the live balance and the masters."""
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.material_loss_gate import (
		get_blocked_loss_variants,
	)

	balance = get_dg_balance(mop.name)
	set_row_snapshots(rows, balance)
	loss_types = get_loss_type_map({row.loss_type for row in rows if row.loss_type})
	validate_loss_rows(
		rows,
		balance,
		set(get_allowed_employees(mop)),
		get_blocked_loss_variants(mop.operation),
		loss_types,
	)
	validate_no_wastage(rows)
	set_target_warehouses(rows, mop, loss_types)


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


def find_reservations(mop, rows):
	"""Each row's reservation, in canonical lock order, before anything is changed.

	Returns one ``frappe._dict`` per row with the reservation and the source warehouse. The
	shared helpers only read ``name`` / ``company`` (and ``doctype`` for messages) off their
	owner; no Stock Entry exists yet, so the operation stands in.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
		_find_sre,
		_sre_remaining,
		_validate_sre_qty,
	)
	from jewellery_erpnext.jewellery_erpnext.lock_order import stock_lock_key

	owner = frappe._dict(
		doctype="Manufacturing Operation", name=mop.name, company=mop.company
	)
	found = []
	for row in rows:
		qty = flt(row.loss_qty, QTY_PRECISION)
		# The helpers prefer the reservation tagged with the row's operation.
		ref = frappe._dict(
			idx=row.idx,
			item_code=row.item_code,
			batch_no=row.batch_no,
			manufacturing_operation=mop.name,
		)
		sre_doc, candidates = _find_sre(
			owner, ref, mop.manufacturing_work_order, TABLE_LABEL, qty
		)
		_validate_sre_qty(owner, ref, sre_doc, candidates, qty, TABLE_LABEL)
		found.append(
			frappe._dict(
				row=row,
				ref=ref,
				qty=qty,
				sre_doc=sre_doc,
				s_warehouse=sre_doc.warehouse,
				# Spent upstream (e.g. by a Product Certification): nothing is reserved, so
				# there is nothing to release and the loss is covered by physical stock.
				spent=_sre_remaining(sre_doc) <= TOLERANCE,
			)
		)

	found.sort(
		key=lambda e: stock_lock_key(
			e.row.item_code, e.sre_doc.warehouse, e.row.batch_no
		)
	)
	used = {}
	for entry in found:
		if entry.spent:
			continue
		# Reducing one reservation twice would chain replacements, and cancel can only
		# restore the last link of a chain.
		if entry.sre_doc.name in used:
			frappe.throw(
				_(
					"Rows {0} and {1} are both reserved by Stock Reservation Entry {2}. Book "
					"them one at a time."
				).format(used[entry.sre_doc.name], entry.row.idx, entry.sre_doc.name),
				title=_("Loss Booking"),
			)
		used[entry.sre_doc.name] = entry.row.idx
	return found


def reduce_reservations(found):
	"""Release each row's loss from its reservation, in the lock order ``found`` is in.

	The replacement reservation carries its Stock Entry's name (``_reduce_sre`` stamps it).
	One the loss used up entirely leaves no replacement, so the cancelled original is stamped
	instead; cancel rebuilds it from there.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
		_reduce_sre,
	)

	for entry in found:
		if entry.spent:
			continue
		replacement = _reduce_sre(
			entry.se, entry.ref, entry.sre_doc, entry.qty, TABLE_LABEL
		)
		if not replacement:
			frappe.db.set_value(
				"Stock Reservation Entry",
				entry.sre_doc.name,
				"employee_ir",
				entry.se.name,
				update_modified=False,
			)


def recheck_balance(manufacturing_operation, pending):
	"""Re-read the balance under the operation lock; throw if another posting took the stones."""
	balance = get_dg_balance(manufacturing_operation)
	for entry in pending:
		row = entry.row
		bal = balance.get((row.item_code, row.batch_no))
		available = bal.available_qty if bal else 0
		if entry.qty > available + TOLERANCE:
			frappe.throw(
				_(
					"Row {0}: the balance of {1} batch {2} changed while the loss was being booked "
					"(now {3}). Reopen the dialog and book again."
				).format(row.idx, row.item_code, row.batch_no, available),
				title=_("Loss Booking"),
			)


# ---------------------------------------------------------------------------
# Stock Entry and MOP Log
# ---------------------------------------------------------------------------


def group_by_employee(pending):
	"""``[(employee, [entries])]`` in first-seen order, entries kept in lock order."""
	groups = {}
	for entry in pending:
		groups.setdefault(entry.row.employee, []).append(entry)
	return list(groups.items())


def build_loss_stock_entry(mop, employee, entries):
	"""One Process Loss Repack: each stone out of its reservation's warehouse, followed by its
	loss variant into the target warehouse (consume, produce pairs for ``loss_valuation``)."""
	from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
		resolve_batch_ownership,
	)

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = PROCESS_LOSS_SE_TYPE
	se.purpose = "Repack"
	se.company = mop.company
	se.posting_date = today()
	se.posting_time = nowtime()
	se.auto_created = 1
	se.manufacturing_work_order = mop.manufacturing_work_order
	se.manufacturing_operation = mop.name
	if mop.manufacturing_order:
		se.manufacturing_order = mop.manufacturing_order
	se.department = mop.department
	se.to_department = mop.department
	se.employee = employee
	se.remarks = _(
		"Diamond / gemstone loss booked on Manufacturing Operation {0} ({1})."
	).format(
		mop.name,
		", ".join(dict.fromkeys(entry.row.loss_type for entry in entries)),
	)

	for entry in entries:
		row = entry.row
		inventory_type, customer = resolve_batch_ownership(row)
		# The loss variant's batch is minted from the produce row; it inherits the customer
		# voucher type off the header (Batch.update_inventory_dimentions), as in Employee IR.
		if customer and not se.get("customer_voucher_type"):
			voucher_type = frappe.db.get_value(
				"Batch", row.batch_no, "custom_customer_voucher_type"
			)
			if voucher_type:
				se.customer_voucher_type = voucher_type

		uom = row.stock_uom or frappe.db.get_value("Item", row.item_code, "stock_uom")
		qty = entry.qty
		pcs = cstr(cint(row.loss_pcs))
		common = {
			"qty": qty,
			"transfer_qty": qty,
			"pcs": pcs,
			"uom": uom,
			"stock_uom": uom,
			"conversion_factor": 1,
			"manufacturing_operation": mop.name,
			"custom_manufacturing_work_order": mop.manufacturing_work_order,
			"employee": employee,
			"department": mop.department,
			"inventory_type": inventory_type,
			"use_serial_batch_fields": 1,
		}
		if customer:
			common["customer"] = customer
		se.append(
			"items",
			dict(
				common,
				item_code=row.item_code,
				s_warehouse=entry.s_warehouse,
				t_warehouse=None,
				batch_no=row.batch_no,
			),
		)
		se.append(
			"items",
			dict(
				common,
				item_code=entry.loss_item,
				s_warehouse=None,
				t_warehouse=row.target_warehouse,
				is_finished_item=1,
				set_basic_rate_manually=1,
			),
		)
	return se


def write_mop_logs(se, flow_index):
	"""A negative MOP Log row per consumed stone, on the operation, at its highest flow index."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		create_mop_log_for_stock_transfer_to_mo,
	)

	for row in se.items:
		if row.s_warehouse and not row.t_warehouse and row.manufacturing_operation:
			# is_synced: the stock has already moved, so the EOD sync must not post it again.
			create_mop_log_for_stock_transfer_to_mo(
				se, row, is_synced=True, outward=True, flow_index=flow_index
			)


def book(mop, rows):
	"""Create, reserve against and submit the loss entries; returns the submitted Stock Entries."""
	validate_booking(mop, rows)
	pending = find_reservations(mop, rows)

	# Insert every entry first: the reservations are stamped with the entry's name, and are
	# then reduced across all entries in one canonical lock order.
	entries = []
	for employee, group in group_by_employee(pending):
		for entry in group:
			entry.loss_item = resolve_loss_item(
				mop.company, mop.manufacturer, entry.row
			)
		se = build_loss_stock_entry(mop, employee, group)
		se.flags.ignore_permissions = True
		# The MOP Log rows are written below. Should a site add "Process Loss" to the MOP
		# Settings reservation list, the generic bridge would also reserve the rows and log
		# them as positive.
		se.flags.skip_mwo_reservation = True
		se.insert()
		for entry in group:
			entry.se = se
		entries.append(se)

	reduce_reservations(pending)
	for se in entries:
		se.submit()

	# The submits above locked the Bins (and, under process control, the operation), so
	# taking the operation row now keeps the canonical Bin -> operation order. Re-read the
	# balance under that lock: a posting that landed after the validation must not be lost
	# twice.
	frappe.db.get_value("Manufacturing Operation", mop.name, "name", for_update=True)
	recheck_balance(mop.name, pending)

	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_last_mop_index,
	)

	flow_index = cint(get_last_mop_index(mop.name))
	for se in entries:
		write_mop_logs(se, flow_index)
	return entries


# ---------------------------------------------------------------------------
# Stock Entry cancel hooks
# ---------------------------------------------------------------------------


def is_loss_entry(doc):
	"""Whether this Stock Entry was booked from the dialog.

	Main Slip and warehouse receive losses are Process Loss entries on an operation with no
	Employee IR as well, but write no MOP Log rows; Employee IR's own carry ``employee_ir``.
	Only this booking leaves negative MOP Log rows under a Process Loss entry.
	"""
	if (
		doc.get("stock_entry_type") != PROCESS_LOSS_SE_TYPE
		or doc.get("employee_ir")
		or not doc.get("manufacturing_operation")
	):
		return False
	return bool(
		frappe.db.sql(
			"""
			SELECT name FROM `tabMOP Log`
			WHERE voucher_type = 'Stock Entry'
			  AND voucher_no = %s
			  AND is_cancelled = 0
			  AND qty_change < 0
			LIMIT 1
			""",
			(doc.name,),
		)
	)


def _snapshot(sre_row):
	try:
		return json.loads(sre_row.custom_replaced_sre_snapshot or "{}") or {}
	except Exception:
		return {}


def _marked_reservations(se_name):
	"""``(live, superseded, used_up)`` reservations carrying this entry's markers.

	* live -- replacements this entry made that still stand (restored by
	  ``_restore_reduced_sres``), including copies a relocation carried the markers onto;
	* superseded -- replacements this entry made that were cancelled since, with no live copy
	  of the same item / batch left: a later posting reduced them again;
	* used_up -- originals this entry's loss used up entirely (stamped, snapshot not naming it).
	"""
	legacy = f'%"employee_ir": "{se_name}"%'
	rows = frappe.db.sql(
		"""
		SELECT name, docstatus, item_code, employee_ir, custom_replaced_sre_snapshot
		FROM `tabStock Reservation Entry`
		WHERE docstatus IN (1, 2)
		  AND (employee_ir = %(se)s OR custom_replaced_sre_snapshot LIKE %(legacy)s)
		ORDER BY creation
		""",
		{"se": se_name, "legacy": legacy},
		as_dict=True,
	)
	live, cancelled, used_up = [], [], []
	for row in rows:
		names_entry = _snapshot(row).get("employee_ir") == se_name
		if row.docstatus == 1:
			if names_entry or row.employee_ir == se_name:
				live.append(row)
		elif names_entry:
			cancelled.append(row)
		elif row.employee_ir == se_name:
			used_up.append(row)

	live_keys = {(row.item_code, _snapshot(row).get("batch_no")) for row in live}
	superseded = [
		row
		for row in cancelled
		if (row.item_code, _snapshot(row).get("batch_no")) not in live_keys
	]
	return live, superseded, used_up


def guard_loss_entry_cancel(doc, method=None):
	"""Stock Entry ``before_cancel``: a loss entry cancels only while it can be undone exactly."""
	if not is_loss_entry(doc):
		return
	doc.flags[LOSS_ENTRY_FLAG] = True

	mop = frappe.db.get_value(
		"Manufacturing Operation",
		doc.manufacturing_operation,
		["name", "status", "manufacturing_work_order"],
		as_dict=True,
	)
	current = mop and frappe.db.get_value(
		"Manufacturing Work Order",
		mop.manufacturing_work_order,
		"manufacturing_operation",
	)
	if not mop or mop.status in CLOSED_STATUSES or current != mop.name:
		frappe.throw(
			_(
				"Manufacturing Operation {0} has moved on ({1}), and its balance went with it. "
				"Cancelling this loss entry now would count the lost stones twice."
			).format(
				doc.manufacturing_operation,
				(mop.status if mop and mop.status in CLOSED_STATUSES else current)
				or _("none"),
			),
			title=_("Cannot Cancel"),
		)

	_live, superseded, _used_up = _marked_reservations(doc.name)
	if superseded:
		frappe.throw(
			_(
				"Stock Reservation Entry {0}, which this loss entry reduced, has since been "
				"reduced or replaced by another posting (for example a later loss on the same "
				"batch). Cancel that posting first, so the reservation can be restored."
			).format(", ".join(row.name for row in superseded)),
			title=_("Cannot Cancel"),
		)


def restore_loss_entry_reservations(doc, method=None):
	"""Stock Entry ``on_cancel``: give back what the loss took from the reservations.

	Runs after the generic cancel, which already flagged the entry's MOP Log rows (in bulk,
	so nothing recomputed the operation's header).
	"""
	if not doc.flags.get(LOSS_ENTRY_FLAG):
		return
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
		_restore_reduced_sres,
	)
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		FIELD_MAP,
		recalculate_manufacturing_operation_weights,
	)

	# Read the used-up originals first: _restore_reduced_sres cancels the replacements, which
	# keep this entry's marker and would read as used up afterwards.
	_live, _superseded, used_up = _marked_reservations(doc.name)
	_restore_reduced_sres(doc)
	for row in used_up:
		rebuild_used_up_reservation(row.name, doc.name)

	recalculate_manufacturing_operation_weights(
		doc.manufacturing_operation,
		prefixes=tuple(FIELD_MAP[v] for v in LOSS_VARIANTS),
	)


def rebuild_used_up_reservation(sre_name, se_name):
	"""Submit a copy of a reservation the loss used up, reserving what it held before."""
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
		_reservation_voucher_qty,
		_sre_remaining,
	)

	original = frappe.get_doc("Stock Reservation Entry", sre_name)
	if original.docstatus != 2 or original.get("employee_ir") != se_name:
		return
	rebuilt = frappe.copy_doc(original)
	rebuilt.docstatus = 0
	rebuilt.name = None
	rebuilt.amended_from = None
	rebuilt.status = "Draft"
	# The stamp was this entry's; the snapshot (if any) is an earlier owner's, whose marker
	# goes back with it.
	rebuilt.employee_ir = _snapshot(original).get("employee_ir") or None
	# copy_doc carries the no_copy consumption counters; the rebuilt entry has delivered
	# nothing, so it reserves what the original still held (as _reduce_sre sizes it).
	rebuilt.delivered_qty = 0
	rebuilt.transferred_qty = 0
	rebuilt.consumed_qty = 0
	if rebuilt.sb_entries:
		kept = []
		for sb in rebuilt.sb_entries:
			sb.qty = flt(flt(sb.qty, 3) - flt(sb.delivered_qty, 3), 3)
			sb.delivered_qty = 0
			if sb.qty > TOLERANCE:
				kept.append(sb)
		rebuilt.sb_entries = kept
		for idx, sb in enumerate(rebuilt.sb_entries, start=1):
			sb.idx = idx
		rebuilt.reserved_qty = flt(sum(flt(sb.qty, 3) for sb in rebuilt.sb_entries), 3)
	else:
		rebuilt.reserved_qty = _sre_remaining(original)
	if rebuilt.reserved_qty <= TOLERANCE:
		return
	rebuilt.voucher_qty = _reservation_voucher_qty(original, rebuilt.reserved_qty)
	rebuilt.available_qty = max(flt(original.available_qty), rebuilt.reserved_qty)
	rebuilt.flags.ignore_permissions = True
	rebuilt.insert(ignore_links=True)
	rebuilt.submit()
	# Once rebuilt, the original must not read as used up by this entry again.
	frappe.db.set_value(
		"Stock Reservation Entry",
		original.name,
		"employee_ir",
		rebuilt.employee_ir,
		update_modified=False,
	)


# ---------------------------------------------------------------------------
# Dialog endpoints
# ---------------------------------------------------------------------------


def has_booking_permission(manufacturing_operation, throw=False):
	"""Create Stock Entries, and read this operation.

	The entries are inserted with ``ignore_permissions`` and the operation's balance is read
	through ``frappe.db``, so the operation's own permission -- which carries the user's
	Department / Company user permissions -- is what keeps a user to the operations they may
	see. Without it, anyone able to create a Stock Entry could read the balance of, and book
	loss on, any operation by calling the method directly.
	"""
	return frappe.has_permission(
		"Stock Entry", "create", throw=throw
	) and frappe.has_permission(
		"Manufacturing Operation", "read", doc=manufacturing_operation, throw=throw
	)


@frappe.whitelist()
def can_book_loss(manufacturing_operation):
	"""Whether the operation's form shows the Book Loss of Diamond/Gemstone button."""
	if not has_booking_permission(manufacturing_operation):
		return False
	mop = get_operation(manufacturing_operation)
	return get_operation_block_reason(mop) is None


@frappe.whitelist()
def get_loss_booking_context(manufacturing_operation):
	"""What the dialog shows: the diamond / gemstone balance, the Loss Types and the employees."""
	has_booking_permission(manufacturing_operation, throw=True)
	mop = get_operation(manufacturing_operation)
	reason = get_operation_block_reason(mop)
	if reason:
		frappe.throw(reason, title=_("Loss Booking"))

	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.material_loss_gate import (
		get_blocked_loss_variants,
	)

	blocked = get_blocked_loss_variants(mop.operation)
	rows = [
		dict(row)
		for _key, row in sorted(get_dg_balance(mop.name).items())
		if row.variant_of not in blocked
	]
	employees = get_allowed_employees(mop)
	employee_names = (
		dict(
			frappe.get_all(
				"Employee",
				filters={"name": ["in", employees]},
				fields=["name", "employee_name"],
				as_list=True,
			)
		)
		if employees
		else {}
	)
	default_employee = mop.employee if mop.employee in employees else None
	if not default_employee and len(employees) == 1:
		default_employee = employees[0]

	return {
		"rows": rows,
		"loss_types": frappe.get_all(
			"Loss Type",
			filters={"destination_warehouse_type": ["is", "set"]},
			pluck="name",
			order_by="name asc",
		),
		"employees": [
			{"value": e, "label": employee_names.get(e) or e} for e in employees
		],
		"default_employee": default_employee,
		"department": mop.department,
	}


@frappe.whitelist(methods=["POST"])
def book_loss(manufacturing_operation, rows):
	"""Book the dialog rows as Process Loss Stock Entries, all in this request."""
	has_booking_permission(manufacturing_operation, throw=True)
	mop = get_operation(manufacturing_operation)
	reason = get_operation_block_reason(mop)
	if reason:
		frappe.throw(reason, title=_("Loss Booking"))
	entries = book(mop, prepare_rows(frappe.parse_json(rows) or []))
	return {"stock_entries": [se.name for se in entries]}
