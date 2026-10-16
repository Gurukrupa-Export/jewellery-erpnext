# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Repack on Employee IR Receive for Finding Manufacturing - Without SO work orders.

When a Receive brings metal back for a Without-SO work order, that metal is turned into the
work order's finding item in the same department warehouse:

    metal batch(es) received through the Employee IR  --Repack-->  finding item, new batch

**Source.** The metal rows the Receive's own Material Transfer (WORK ORDER) just moved into the
department warehouse for this operation (``main_slip_inject.inject_extra_metal_for_eir_receive``
returns those Stock Entries). That is "the batch received through the Employee IR"; anything the
operation held before this Receive is not touched.

**Reservations.** That metal is reserved for the work order by the transfer, and ERPNext refuses
to consume reserved stock, so its reservations are released first (cancelled, or cancelled and
re-created for a partial remainder with ``manufacturing_operation._build_replacement_sre``). The
finding batch the Repack produces is then reserved for the work order with the same
``stock_reservation_entry_for_mwo`` every work-order transfer uses.

**Operation weights.** The Repack writes its own MOP Log rows -- metal out (negative), finding in
(positive) -- on the operation, so the balance ``update_new_mop_wtg`` carries to the next
operation is the finding, not the metal. The generic Stock Entry bridge is switched off for this
entry (``flags.skip_mwo_reservation``): it would log the consumed metal as a positive row.

**References.** The Repack carries the Employee IR (``employee_ir``), work order, Parent
Manufacturing Order and operation on its header and rows; Stock Entry has no Manufacturing Plan
field, so the plan goes in ``remarks``. ``custom_eir_operation_row`` is deliberately left blank:
Main Slip injection keys its own idempotency on it and would mistake this Repack for one of its
purity conversions.

**Cancel.** ``cancel_injections_for_eir`` cancels this Repack (an auto-created Repack carrying the
Employee IR) before the transfer that fed it, newest first. Its on_cancel flips its MOP Log rows.
The finding batch's reservation has to go first or ERPNext refuses to take the batch back out, so
``release_without_so_repack_reservations`` runs ahead of the Receive cancel.
"""

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	normalize_ownership,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.finding_repack import (
	_create_finding_batch,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.without_so_costing import (
	apply_worker_cost_to_batch,
	get_mwo_worker_cost,
	is_without_so_mwo,
	recompute_without_so_costing,
)

REPACK = "Repack"
QTY_PRECISION = 3


def create_without_so_finding_repack(eir, row, injected_se_names):
	"""Repack the metal this Receive row brought in into the work order's finding item.

	Returns the Repack's name, or None when the row is not a Without-SO work order or brought
	in no metal."""
	if not injected_se_names or not is_without_so_mwo(row.manufacturing_work_order):
		return None

	existing = _existing_repack(eir.name, row.manufacturing_operation)
	if existing:
		return existing

	metal_rows = _received_metal_rows(injected_se_names, row.manufacturing_operation)
	if not metal_rows:
		return None

	mwo = frappe.get_cached_doc(
		"Manufacturing Work Order", row.manufacturing_work_order
	)
	finding_item = mwo.item_code
	if not finding_item or not finding_item.startswith("F"):
		frappe.throw(
			_(
				"Row #{0}: work order {1} is not for a finding item, so nothing can be repacked."
			).format(row.idx, mwo.name)
		)

	for metal in metal_rows:
		_release_reservation(mwo.name, row.manufacturing_operation, metal)

	se = _new_repack(eir, mwo, row.manufacturing_operation)
	by_owner = {}
	for metal in metal_rows:
		owner = normalize_ownership(
			metal.inventory_type,
			metal.customer,
			batch_no=metal.batch_no,
			item_code=metal.item_code,
		)
		by_owner.setdefault(owner, []).append(metal)

	consume_rows = []
	for (inventory_type, customer), metals in by_owner.items():
		for metal in metals:
			consume_rows.append(
				se.append(
					"items",
					{
						"item_code": metal.item_code,
						"qty": metal.qty,
						"transfer_qty": metal.qty,
						"conversion_factor": 1,
						"uom": metal.uom or "Gram",
						"stock_uom": metal.uom or "Gram",
						"s_warehouse": metal.t_warehouse,
						"batch_no": metal.batch_no,
						"use_serial_batch_fields": 1,
						"inventory_type": inventory_type,
						"customer": customer,
						"manufacturing_operation": row.manufacturing_operation,
						"custom_manufacturing_work_order": mwo.name,
					},
				)
			)
		qty = flt(sum(flt(m.qty) for m in metals), QTY_PRECISION)
		batch_no = _create_finding_batch(
			se,
			finding_item,
			inventory_type,
			customer,
			[(m.batch_no, m.qty) for m in metals],
		)
		uom = frappe.db.get_value("Item", finding_item, "stock_uom") or "Gram"
		se.append(
			"items",
			{
				"item_code": finding_item,
				"qty": qty,
				"transfer_qty": qty,
				"conversion_factor": 1,
				"uom": uom,
				"stock_uom": uom,
				"t_warehouse": metals[0].t_warehouse,
				"batch_no": batch_no,
				"use_serial_batch_fields": 1,
				"is_finished_item": 1,
				"set_basic_rate_manually": 1,
				"inventory_type": inventory_type,
				"customer": customer,
				"manufacturing_operation": row.manufacturing_operation,
				"custom_manufacturing_work_order": mwo.name,
			},
		)

	# Reservation and MOP Log for this entry are written below, explicitly.
	se.flags.skip_mwo_reservation = True
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()

	from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
		create_mop_log,
		stock_reservation_entry_for_mwo,
	)

	# Reserves the produced finding rows (the only rows with a target) and logs them (+qty).
	stock_reservation_entry_for_mwo(se)
	for consumed in consume_rows:
		create_mop_log(
			se,
			frappe._dict(
				name=consumed.name,
				item_code=consumed.item_code,
				qty=-flt(consumed.qty),
				batch_no=consumed.batch_no,
				s_warehouse=consumed.s_warehouse,
				manufacturing_operation=consumed.manufacturing_operation,
			),
			is_synced=True,
		)
	return se.name


def stamp_without_so_worker_cost(eir):
	"""Worker Cost / Total Cost on every finding batch the work order's Repacks produced.

	Runs after the Receive closed the operation's timer. The operation's hours and cost are
	re-derived from its closed time logs first, so the work order's worker cost includes this
	operation however its timer was closed."""
	for row in eir.employee_ir_operations:
		if not is_without_so_mwo(row.manufacturing_work_order):
			continue
		recompute_without_so_costing(row.manufacturing_operation, eir.employee)
		worker_cost = get_mwo_worker_cost(row.manufacturing_work_order)
		for batch_no in _repack_finding_batches(row.manufacturing_work_order):
			apply_worker_cost_to_batch(batch_no, worker_cost)


def restamp_without_so_batches_on_receive_cancel(eir):
	"""Re-stamp the finding batches that survive a cancelled Receive.

	Every Receive stamps the work order's cumulative worker cost on all its finding batches, so
	once the cancelled Receive's operation cost is reset, the earlier batches still carry the
	cost it added. Run after that reset, when the work order's worker cost is the surviving one."""
	for mwo in {row.manufacturing_work_order for row in eir.employee_ir_operations}:
		if not is_without_so_mwo(mwo):
			continue
		worker_cost = get_mwo_worker_cost(mwo)
		for batch_no in _repack_finding_batches(mwo):
			apply_worker_cost_to_batch(batch_no, worker_cost)


def release_without_so_repack_reservations(eir):
	"""Cancel the reservations on the finding batches this Receive's Repacks produced.

	Must run before the Receive cancel reverses the Repack: ERPNext will not take a reserved
	batch back out of the warehouse."""
	repacks = frappe.get_all(
		"Stock Entry",
		filters={
			"employee_ir": eir.name,
			"stock_entry_type": REPACK,
			"auto_created": 1,
			"docstatus": 1,
			"manufacturing_work_order": ["is", "set"],
			"custom_eir_operation_row": ["is", "not set"],
		},
		fields=["name", "manufacturing_work_order"],
	)
	for repack in repacks:
		if not is_without_so_mwo(repack.manufacturing_work_order):
			continue
		produced = frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": repack.name, "t_warehouse": ["is", "set"]},
			fields=["item_code", "batch_no", "t_warehouse"],
		)
		for prod in produced:
			for sre in _active_sres(
				repack.manufacturing_work_order,
				prod.item_code,
				prod.t_warehouse,
				prod.batch_no,
			):
				frappe.get_doc("Stock Reservation Entry", sre).cancel()


def _existing_repack(eir_name, mop):
	return frappe.db.get_value(
		"Stock Entry",
		{
			"employee_ir": eir_name,
			"manufacturing_operation": mop,
			"stock_entry_type": REPACK,
			"auto_created": 1,
			"docstatus": 1,
			"manufacturing_work_order": ["is", "set"],
			"custom_eir_operation_row": ["is", "not set"],
		},
		"name",
	)


def _received_metal_rows(se_names, mop):
	"""Metal the Receive's own Stock Entries put into the department for this operation."""
	return frappe.db.sql(
		"""
		SELECT sed.item_code, sed.batch_no, sed.qty, sed.uom, sed.t_warehouse,
			sed.inventory_type, sed.customer
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE se.name IN %(names)s
			AND se.docstatus = 1
			AND sed.manufacturing_operation = %(mop)s
			AND IFNULL(sed.t_warehouse, '') != ''
			AND sed.item_code LIKE 'M-%%'
		ORDER BY sed.idx
		""",
		{"names": tuple(se_names), "mop": mop},
		as_dict=True,
	)


def _active_sres(mwo, item_code, warehouse, batch_no):
	return frappe.db.sql_list(
		"""
		SELECT DISTINCT sre.name
		FROM `tabStock Reservation Entry` sre
		LEFT JOIN `tabSerial and Batch Entry` sb ON sb.parent = sre.name
		WHERE sre.docstatus = 1
			AND sre.manufacturing_work_order = %(mwo)s
			AND sre.item_code = %(item)s
			AND sre.warehouse = %(wh)s
			AND sre.status NOT IN ('Delivered', 'Cancelled')
			AND (%(batch)s IS NULL OR sb.batch_no = %(batch)s)
		ORDER BY sre.creation
		""",
		{"mwo": mwo, "item": item_code, "wh": warehouse, "batch": batch_no},
	)


def _release_reservation(mwo, mop, metal):
	"""Free ``metal.qty`` of the reservation on this metal batch so the Repack can consume it."""
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
		_build_replacement_sre,
	)

	need = flt(metal.qty, QTY_PRECISION)
	for name in _active_sres(mwo, metal.item_code, metal.t_warehouse, metal.batch_no):
		if need <= 0:
			break
		sre = frappe.get_doc("Stock Reservation Entry", name)
		if sre.manufacturing_operation and sre.manufacturing_operation != mop:
			continue
		remaining = flt(
			flt(sre.reserved_qty)
			- flt(sre.delivered_qty)
			- flt(sre.get("consumed_qty")),
			QTY_PRECISION,
		)
		if remaining <= 0:
			continue
		take = min(remaining, need)
		sre.flags.ignore_permissions = True
		sre.cancel()
		leftover = flt(remaining - take, QTY_PRECISION)
		if leftover > 0:
			sb_remaining = (
				[{"batch_no": metal.batch_no, "qty": leftover}]
				if sre.reservation_based_on == "Serial and Batch"
				else None
			)
			_build_replacement_sre(sre, leftover, sb_remaining)
		need = flt(need - take, QTY_PRECISION)


def _new_repack(eir, mwo, mop):
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = REPACK
	se.purpose = REPACK
	se.company = mwo.company
	se.auto_created = 1
	se.employee_ir = eir.name
	se.manufacturing_order = mwo.manufacturing_order
	se.manufacturing_work_order = mwo.name
	se.manufacturing_operation = mop
	if eir.get("subcontracting") == "Yes":
		se.subcontractor = eir.subcontractor
	else:
		se.employee = eir.employee
	se.remarks = _(
		"Finding Manufacturing - Without SO repack. Employee IR {0}, Manufacturing Work Order {1}, "
		"Parent Manufacturing Order {2}, Manufacturing Plan {3}."
	).format(eir.name, mwo.name, mwo.manufacturing_order, mwo.manufacturing_plan or "-")
	return se


def _repack_finding_batches(mwo):
	return frappe.db.sql_list(
		"""
		SELECT DISTINCT sed.batch_no
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE se.manufacturing_work_order = %(mwo)s
			AND se.stock_entry_type = %(type)s
			AND se.auto_created = 1
			AND se.docstatus = 1
			AND IFNULL(se.custom_eir_operation_row, '') = ''
			AND IFNULL(sed.t_warehouse, '') != ''
			AND IFNULL(sed.batch_no, '') != ''
		""",
		{"mwo": mwo, "type": REPACK},
	)
