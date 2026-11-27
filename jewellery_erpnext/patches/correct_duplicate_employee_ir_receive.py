"""One-off: undo the loss a duplicate Employee IR Receive booked, and give the pieces their metal back.

A stale draft Receive submitted after another Receive of the same operations books the
karigar's loss a second time (EMP-IR-Labh-2026-62146 after 62150, 17 Nov 2026). The
second Process Loss entry takes the metal out of WIP into the karigar's warehouse, and its
MOP Log rows put the loss on orphan operations. The Tagging work order then seeds its
opening from the sum of every live MOP Log change, so the Serial Number Creator consumes
less metal than the piece holds and its FG BOM is short by the same amount.

This script corrects one such pair. It never takes a number from the operator: every
quantity and value is read from the documents or computed by ERPNext and the app's own code.

  1. Cancel the duplicate's Process Loss entry. The metal returns to WIP and the karigar is
     no longer charged.
  2. Mark the duplicate's MOP Log rows cancelled and recompute those operations, as the
     Employee IR cancel does. The Employee IR itself is NOT cancelled: its cancel would
     rewind the work orders onto operations the original Receive already moved past.
  3. Re-seed each Tagging operation's opening rows with the work order's own query.
  4. Add the metal to each finished serial with one Repack entry (serial out, metal out,
     same serial in). ERPNext values the serial at its old value plus the metal's value.
  5. Rebuild each FG BOM with the Serial Number Creator's own builder, repoint the
     Tracking BOM, and deactivate the short BOM.
  6. Check that the operation balance, the new BOM and the metal now in the serial agree,
     that the karigar's loss batches are empty and that WIP is back where it started.

The serial and its stamping number are kept. Anything that fails a check rolls back.
Run outside the EOD sync window (Stock Entry refuses to cancel while it runs).

NOT registered in patches.txt; run it manually. Dry run (does everything, prints, rolls back):

    bench --site <site> execute \
        jewellery_erpnext.patches.correct_duplicate_employee_ir_receive.execute

Apply:

    bench --site <site> execute \
        jewellery_erpnext.patches.correct_duplicate_employee_ir_receive.execute \
        --kwargs "{'dry_run': False}"

Another pair: ``--kwargs "{'duplicate': '<EIR>', 'original': '<EIR>', 'dry_run': False}"``.
"""

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe.utils import flt

DEFAULT_DUPLICATE = "EMP-IR-Labh-2026-62146"
DEFAULT_ORIGINAL = "EMP-IR-Labh-2026-62150"
TOLERANCE = 0.001


class Refused(Exception):
	pass


def execute(duplicate=DEFAULT_DUPLICATE, original=DEFAULT_ORIGINAL, dry_run=True):
	frappe.db.rollback()
	real_commit, real_begin, real_enqueue = (
		frappe.db.commit,
		frappe.db.begin,
		frappe.enqueue,
	)
	blocked = {"commit": 0, "enqueue": 0}

	def _no_commit(*args, **kwargs):
		blocked["commit"] += 1

	def _no_enqueue(*args, **kwargs):
		blocked["enqueue"] += 1

	# One transaction for the whole correction: nested hooks must not commit half of it.
	frappe.db.commit = _no_commit
	frappe.db.begin = lambda *args, **kwargs: None
	if dry_run:
		frappe.enqueue = _no_enqueue

	ok = False
	try:
		plan = _plan(duplicate, original)
		_print_plan(plan)
		before = _snapshot(plan)
		_correct(plan)
		after = _snapshot(plan)
		ok = _check(plan, before, after)
	except Refused as e:
		print(f"REFUSED, nothing changed: {e}")
	finally:
		frappe.db.commit, frappe.db.begin, frappe.enqueue = (
			real_commit,
			real_begin,
			real_enqueue,
		)
		if ok and not dry_run:
			frappe.db.commit()
			print("COMMITTED.")
		else:
			frappe.db.rollback()
			print(
				"DRY RUN: rolled back."
				if ok
				else "Checks failed or refused: rolled back.",
				f"(intercepted {blocked['commit']} nested commit(s), {blocked['enqueue']} job(s))",
			)
	return ok


# --------------------------------------------------------------------------- plan


def _plan(duplicate, original):
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
		PROCESS_LOSS_SE_TYPE,
	)

	dup, orig = (frappe.get_doc("Employee IR", n) for n in (duplicate, original))
	for d in (dup, orig):
		if d.docstatus != 1 or d.type != "Receive":
			raise Refused(f"{d.name} is not a submitted Receive")
	dup_mops = {r.manufacturing_operation for r in dup.employee_ir_operations}
	if dup_mops != {r.manufacturing_operation for r in orig.employee_ir_operations}:
		raise Refused(f"{dup.name} and {orig.name} do not receive the same operations")

	loss_ses = frappe.get_all(
		"Stock Entry",
		{
			"employee_ir": dup.name,
			"docstatus": 1,
			"stock_entry_type": PROCESS_LOSS_SE_TYPE,
		},
		pluck="name",
	)
	if not loss_ses:
		raise Refused(f"{dup.name} has no submitted {PROCESS_LOSS_SE_TYPE} entry")

	log_rows = frappe.get_all(
		"MOP Log",
		{"voucher_type": "Employee IR", "voucher_no": dup.name, "is_cancelled": 0},
		["name", "manufacturing_operation"],
	)
	if not log_rows:
		raise Refused(f"{dup.name} has no live MOP Log rows")

	pieces = {}  # metal work order -> piece
	for se_name in loss_ses:
		for row in frappe.get_doc("Stock Entry", se_name).items:
			if row.manufacturing_operation not in dup_mops:
				raise Refused(
					f"{se_name} row {row.idx} is not on an operation of {dup.name}"
				)
			mwo = row.custom_manufacturing_work_order
			piece = pieces.setdefault(
				mwo,
				frappe._dict(
					metal_mwo=mwo, duplicate=dup.name, add=[], loss_batches=[]
				),
			)
			if row.s_warehouse and not row.t_warehouse:
				piece.add.append(
					frappe._dict(
						item_code=row.item_code,
						batch_no=row.batch_no,
						qty=row.qty,
						warehouse=row.s_warehouse,
					)
				)
			elif row.t_warehouse and not row.s_warehouse:
				held = flt(get_batch_qty(row.batch_no, row.t_warehouse, row.item_code))
				if held + TOLERANCE < flt(row.qty):
					raise Refused(
						f"loss batch {row.batch_no} holds {held} in {row.t_warehouse}, needs {row.qty}: already used"
					)
				piece.loss_batches.append(
					frappe._dict(
						item_code=row.item_code,
						batch_no=row.batch_no,
						qty=row.qty,
						warehouse=row.t_warehouse,
					)
				)

	for piece in pieces.values():
		pmo = frappe.db.get_value(
			"Manufacturing Work Order", piece.metal_mwo, "manufacturing_order"
		)
		fg_mwo = frappe.db.get_value(
			"Manufacturing Work Order",
			{"manufacturing_order": pmo, "for_fg": 1, "docstatus": 1},
			["name", "manufacturing_operation"],
			as_dict=True,
		)
		if not fg_mwo:
			raise Refused(f"{pmo} has no submitted Tagging work order")
		snc = frappe.db.get_value(
			"Serial Number Creator",
			{"manufacturing_work_order": fg_mwo.name, "docstatus": 1},
			["name", "fg_serial_no", "fg_bom"],
			as_dict=True,
		)
		if not snc or not snc.fg_serial_no or not snc.fg_bom:
			raise Refused(
				f"{fg_mwo.name} has no submitted Serial Number Creator with a serial and FG BOM"
			)
		snc_se = frappe.db.get_value(
			"Stock Entry",
			{
				"custom_serial_number_creator": snc.name,
				"docstatus": 1,
				"purpose": "Manufacture",
			},
		)
		serial = frappe.db.get_value(
			"Serial No",
			snc.fg_serial_no,
			["name", "status", "warehouse", "item_code", "custom_stamping_no"],
			as_dict=True,
		)
		if serial.status != "Active" or not serial.warehouse:
			raise Refused(
				f"serial {serial.name} is {serial.status}, not Active in a warehouse"
			)
		sold = frappe.db.sql(
			"""select distinct b.voucher_type from `tabSerial and Batch Entry` e
			join `tabSerial and Batch Bundle` b on b.name = e.parent
			where e.serial_no = %s and b.docstatus = 1 and b.voucher_type != 'Stock Entry'""",
			serial.name,
			pluck=True,
		)
		if sold:
			raise Refused(f"serial {serial.name} is already on {', '.join(sold)}")
		piece.update(
			pmo=pmo,
			fg_mwo=fg_mwo.name,
			fg_mop=fg_mwo.manufacturing_operation,
			snc=snc.name,
			snc_se=snc_se,
			old_bom=snc.fg_bom,
			serial=serial,
		)

	return frappe._dict(
		duplicate=dup.name,
		loss_ses=loss_ses,
		log_rows=log_rows,
		pieces=sorted(pieces.values(), key=lambda p: p.pmo),
	)


def _print_plan(plan):
	print(
		f"Duplicate {plan.duplicate}: cancel {', '.join(plan.loss_ses)}; cancel {len(plan.log_rows)} MOP Log row(s)"
	)
	for p in plan.pieces:
		adds = ", ".join(f"{a.qty} {a.item_code} ({a.batch_no})" for a in p.add)
		print(
			f"  {p.pmo}: serial {p.serial.name} (stamp {p.serial.custom_stamping_no}) in {p.serial.warehouse};"
			f" add {adds}; rebuild {p.old_bom} from {p.snc}"
		)


# --------------------------------------------------------------------------- correct


def _correct(plan):
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		recalculate_manufacturing_operation_weights,
	)

	# 1. the duplicate loss
	for se_name in plan.loss_ses:
		frappe.get_doc("Stock Entry", se_name).cancel()

	# 2. the duplicate's MOP Log rows, exactly as the Employee IR cancel leg does it
	frappe.db.set_value(
		"MOP Log",
		{
			"voucher_type": "Employee IR",
			"voucher_no": plan.duplicate,
			"is_cancelled": 0,
		},
		"is_cancelled",
		1,
	)
	for mop in sorted({r.manufacturing_operation for r in plan.log_rows}):
		recalculate_manufacturing_operation_weights(mop)

	for p in plan.pieces:
		_reseed_fg_opening(p)
		recalculate_manufacturing_operation_weights(p.fg_mop)
		p.repack = _add_metal_to_serial(p)
		p.new_bom = _rebuild_fg_bom(p)


def _reseed_fg_opening(p):
	"""The Tagging operation's opening rows, recomputed with the work order's own query."""
	siblings = frappe.get_all(
		"Manufacturing Work Order",
		{"manufacturing_order": p.pmo, "docstatus": 1, "name": ["!=", p.fg_mwo]},
		pluck="name",
	)
	totals = {
		(r.item_code, r.batch_no): r
		for r in frappe.db.sql(
			"""select item_code, batch_no, sum(qty_change) as total_qty, sum(pcs_change) as total_pcs
			from `tabMOP Log` where manufacturing_work_order in %s and is_cancelled = 0
			group by item_code, batch_no""",
			(tuple(siblings),),
			as_dict=True,
		)
	}
	p.reseeded = []
	for row in frappe.get_all(
		"MOP Log",
		{
			"manufacturing_operation": p.fg_mop,
			"voucher_type": "Manufacturing Work Order",
			"voucher_no": p.fg_mwo,
			"flow_index": 0,
			"is_cancelled": 0,
		},
		["name", "item_code", "batch_no", "qty_after_transaction_batch_based"],
	):
		total = totals.get((row.item_code, row.batch_no))
		if (
			not total
			or abs(flt(total.total_qty) - flt(row.qty_after_transaction_batch_based))
			<= TOLERANCE
		):
			continue
		qty = flt(total.total_qty)
		frappe.db.set_value(
			"MOP Log",
			row.name,
			{
				"qty_after_transaction_batch_based": qty,
				"qty_after_transaction": qty,
				"qty_after_transaction_item_based": qty,
			},
		)
		p.reseeded.append(
			(row.item_code, row.batch_no, row.qty_after_transaction_batch_based, qty)
		)


def _add_metal_to_serial(p):
	"""Serial out, metal out, the same serial back in: ERPNext adds the metal's value to the serial."""
	snc_se = frappe.get_doc("Stock Entry", p.snc_se)
	fg_row = next(r for r in snc_se.items if r.is_finished_item)
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Repack"
	se.purpose = "Repack"
	for field in (
		"company",
		"manufacturer",
		"branch",
		"department",
		"to_department",
		"manufacturing_order",
		"manufacturing_work_order",
		"manufacturing_operation",
	):
		if se.meta.has_field(field):
			se.set(field, snc_se.get(field))
	se.remarks = (
		f"Adds the metal that duplicate Employee IR Receive {p.duplicate} wrongly booked as loss"
		f" back into {p.serial.name} ({p.snc})."
	)
	common = {
		"inventory_type": fg_row.get("inventory_type"),
		"use_serial_batch_fields": 1,
	}
	se.append(
		"items",
		{
			**common,
			"item_code": p.serial.item_code,
			"qty": 1,
			"s_warehouse": p.serial.warehouse,
			"serial_no": p.serial.name,
		},
	)
	for a in p.add:
		se.append(
			"items",
			{
				**common,
				"item_code": a.item_code,
				"qty": a.qty,
				"s_warehouse": a.warehouse,
				"batch_no": a.batch_no,
			},
		)
	se.append(
		"items",
		{
			**common,
			"item_code": p.serial.item_code,
			"qty": 1,
			"t_warehouse": p.serial.warehouse,
			"serial_no": p.serial.name,
			"is_finished_item": 1,
		},
	)
	se.insert(ignore_permissions=True)
	se.submit()
	return se.name


def _rebuild_fg_bom(p):
	"""The SNC's own FG BOM builder, fed the corrected piece weights from the operation."""
	from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
		create_finished_goods_bom,
	)
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)
	from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
		_resolve_snc_mnf_qty,
	)

	balance = {}
	for r in get_current_mop_balance_rows(
		p.fg_mop, include_fields=["item_code", "qty_after_transaction_batch_based"]
	):
		balance[r["item_code"]] = flt(balance.get(r["item_code"])) + flt(
			r["qty_after_transaction_batch_based"]
		)

	snc = frappe.get_doc("Serial Number Creator", p.snc)
	mnf_qty = _resolve_snc_mnf_qty(snc) or 1
	for row in snc.fg_details:
		new_qty = (
			flt(balance.get(row.row_material) / mnf_qty, 3)
			if row.row_material in balance
			else row.qty
		)
		if abs(flt(new_qty) - flt(row.qty)) > TOLERANCE:
			row.qty = new_qty
			row.db_set("qty", new_qty, update_modified=False)
	snc._compute_total_weight()
	snc.db_set("total_weight", snc.total_weight, update_modified=False)

	operation_data = frappe.get_all(
		"Manufacturing Operation",
		{"manufacturing_order": p.pmo, "docstatus": ["!=", 2]},
		["name as manufacturing_operation", "employee", "total_minutes", "operation"],
	)
	create_finished_goods_bom(snc, p.snc_se, operation_data)
	new_bom = snc.fg_bom

	for name in frappe.get_all(
		"Tracking Bom", {"reference_docname": p.old_bom}, pluck="name"
	):
		frappe.db.set_value("Tracking Bom", name, "reference_docname", new_bom)

	old = frappe.get_doc("BOM", p.old_bom)
	old.db_set("is_active", 0)
	if old.is_default:
		old._unset_default_bom()
	return new_bom


# --------------------------------------------------------------------------- check


def _snapshot(plan):
	snap = {}
	for p in plan.pieces:
		serial = frappe.db.get_value(
			"Serial No",
			p.serial.name,
			["status", "warehouse", "custom_stamping_no", "custom_bom_no"],
			as_dict=True,
		)
		value = frappe.db.sql(
			"""select coalesce(sum(sle.stock_value_difference), 0) from `tabStock Ledger Entry` sle
			join `tabSerial and Batch Entry` e on e.parent = sle.serial_and_batch_bundle
			where sle.is_cancelled = 0 and e.serial_no = %s""",
			p.serial.name,
		)[0][0]
		snap[p.pmo] = frappe._dict(
			serial=serial,
			serial_value=flt(value, 2),
			wip={
				(a.batch_no, a.warehouse): flt(
					get_batch_qty(a.batch_no, a.warehouse, a.item_code), 3
				)
				for a in p.add
			},
			loss={
				(b.batch_no, b.warehouse): flt(
					get_batch_qty(b.batch_no, b.warehouse, b.item_code), 3
				)
				for b in p.loss_batches
			},
			mop_gross=flt(
				frappe.db.get_value("Manufacturing Operation", p.fg_mop, "gross_wt"), 3
			),
		)
	return snap


def _metal_in_serial(p):
	"""Metal consumed into the serial: the SNC Manufacture entry plus the Repack."""
	items = {a.item_code for a in p.add}
	return flt(
		frappe.db.sql(
			"""select coalesce(sum(qty), 0) from `tabStock Entry Detail`
			where parent in %s and item_code in %s and s_warehouse is not null""",
			((p.snc_se, p.repack), tuple(items)),
		)[0][0],
		3,
	)


def _check(plan, before, after):
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	ok = True

	def check(label, good, detail):
		nonlocal ok
		ok = ok and good
		print(f"    [{'ok' if good else 'FAIL'}] {label}: {detail}")

	for p in plan.pieces:
		b, a = before[p.pmo], after[p.pmo]
		bom = frappe.db.get_value(
			"BOM",
			p.new_bom,
			["metal_weight", "gross_weight", "tag_no", "is_active"],
			as_dict=True,
		)
		metal_items = {x.item_code for x in p.add}
		balance = flt(
			sum(
				flt(r["qty_after_transaction_batch_based"])
				for r in get_current_mop_balance_rows(
					p.fg_mop,
					include_fields=["item_code", "qty_after_transaction_batch_based"],
				)
				if r["item_code"] in metal_items
			),
			3,
		)
		in_serial = _metal_in_serial(p)
		print(
			f"  {p.pmo} / {p.serial.name}: Repack {p.repack}, FG BOM {p.old_bom} -> {p.new_bom}; reseeded {p.reseeded}"
		)
		check(
			"operation balance = metal in serial = new BOM metal",
			abs(balance - in_serial) <= TOLERANCE
			and abs(balance - flt(bom.metal_weight, 3)) <= TOLERANCE,
			f"{balance} / {in_serial} / {flt(bom.metal_weight, 3)}",
		)
		check(
			"new BOM gross = operation gross",
			abs(flt(bom.gross_weight, 3) - a.mop_gross) <= TOLERANCE,
			f"{flt(bom.gross_weight, 3)} / {a.mop_gross} (was {b.mop_gross})",
		)
		check(
			"karigar loss batches emptied by the cancel",
			all(
				abs(a.loss[k] - (b.loss[k] - x.qty)) <= TOLERANCE
				for x in p.loss_batches
				for k in [(x.batch_no, x.warehouse)]
			),
			f"{b.loss} -> {a.loss}",
		)
		check(
			"WIP batch back to its starting balance",
			all(abs(a.wip[k] - b.wip[k]) <= TOLERANCE for k in b.wip),
			f"{b.wip} -> {a.wip}",
		)
		check(
			"serial, stamping number and warehouse unchanged",
			(a.serial.status, a.serial.warehouse, a.serial.custom_stamping_no)
			== (b.serial.status, b.serial.warehouse, b.serial.custom_stamping_no),
			f"{a.serial.status} {a.serial.warehouse} {a.serial.custom_stamping_no}",
		)
		check(
			"serial points at the new BOM, tagged to it",
			a.serial.custom_bom_no == p.new_bom
			and bom.tag_no == p.serial.name
			and bom.is_active,
			f"{b.serial.custom_bom_no} -> {a.serial.custom_bom_no}",
		)
		check(
			"serial value went up",
			a.serial_value > b.serial_value,
			f"{b.serial_value} -> {a.serial_value}",
		)
	return ok
