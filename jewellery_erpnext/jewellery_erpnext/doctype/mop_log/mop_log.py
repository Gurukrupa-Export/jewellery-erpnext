# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.query_builder import DocType
from frappe.query_builder.functions import Max
from frappe.utils import cint, cstr, flt, get_datetime

from jewellery_erpnext.utils import (
	carat_to_gram,
	clamp_negative_balance,
	get_mwo_refining_cutoff,
)

FIELD_MAP = {"M": "net", "F": "finding", "D": "diamond", "G": "gemstone", "O": "other"}
select_fields = [
	"item_code",
	"pcs_after_transaction",
	"pcs_after_transaction_item_based",
	"pcs_after_transaction_batch_based",
	"qty_after_transaction",
	"qty_after_transaction_item_based",
	"qty_after_transaction_batch_based",
	"serial_and_batch_bundle",
	"batch_no",
	"flow_index",
	"voucher_type",
	"voucher_no",
]
current_balance_fields = select_fields + [
	"name",
	"creation",
	"from_warehouse",
	"to_warehouse",
	"row_name",
	"manufacturing_work_order",
	"manufacturing_operation",
]


class MOPLog(Document):
	def validate(self):
		first_char = self.item_code[0] if self.item_code else None
		prefix = FIELD_MAP.get(first_char)
		if not (prefix and self.manufacturing_operation):
			return

		# Canonical lock order: take the Manufacturing Operation row lock up front
		# (it is the terminal sink) so concurrent MOP-Log writers and Department-IR
		# weight recompute acquire the MO in a consistent position relative to
		# Bin/SLE — this breaks the MO<->Bin deadlock cycle. The set_value inside the
		# recompute below would lock the same row a moment later anyway, so this adds
		# no new lock.
		frappe.db.get_value(
			"Manufacturing Operation",
			self.manufacturing_operation,
			"name",
			for_update=True,
		)

		# Derive every bucket from the batch tier instead of stamping this one row's
		# ``qty_after_transaction``. That field is a *family-wide* running total (every
		# ``F-`` item shares the ``finding`` bucket), and only the last row written by
		# create_mop_log_for_stock_transfer_to_mo actually holds it correctly: the clone
		# writers copy it verbatim per row, and update_new_mop_wtg decrements it by a
		# per-(item, batch) loss. Stamping it made the header last-writer-wins, so on an
		# operation carrying two items of the same family every row but the last had its
		# movement silently dropped from the header — MOP-7Q48F read finding_wt 1.945
		# against a ledger of 0.608 + 1.323 = 1.931, hiding 0.014g of booked loss.
		# Narrowed to this row's own family so a per-row save cannot touch a bucket
		# authored outside MOP Log (the MWO->MOP seed, the Refining zero-out).
		recalculate_manufacturing_operation_weights(
			self.manufacturing_operation, pending=self, prefixes=(prefix,)
		)


def update_wt_detail(manufacturing_operation):
	(
		net_wt,
		finding_wt,
		diamond_wt_in_gram,
		gemstone_wt_in_gram,
		other_wt,
		previous_mop,
		loss_wt,
	) = frappe.db.get_value(
		"Manufacturing Operation",
		manufacturing_operation,
		[
			"net_wt",
			"finding_wt",
			"diamond_wt_in_gram",
			"gemstone_wt_in_gram",
			"other_wt",
			"previous_mop",
			"loss_wt",
		],
	)
	prev_gross_wt = 0
	if previous_mop:
		prev_gross_wt = (
			frappe.db.get_value("Manufacturing Operation", previous_mop, "gross_wt")
			or 0
		)
	# Round once: every component is a precision-3 field, so float residue in the
	# sum must not leak out as a sub-milligram delta against prev_gross_wt.
	gross_wt = flt(
		flt(net_wt)
		+ flt(finding_wt)
		+ flt(diamond_wt_in_gram)
		+ flt(gemstone_wt_in_gram)
		+ flt(other_wt),
		3,
	)
	# if loss_wt:
	# 	if loss_wt > 0:
	# 		gross_wt += flt(loss_wt)
	# 	elif loss_wt < 0:
	# 		gross_wt -= abs(flt(loss_wt))

	frappe.db.set_value(
		"Manufacturing Operation",
		manufacturing_operation,
		{
			"gross_wt": gross_wt,
			"prev_gross_wt": prev_gross_wt,
		},
	)


def recalculate_manufacturing_operation_weights(mop_name, pending=None, prefixes=None):
	"""Recompute all weight buckets on a Manufacturing Operation from its
	active MOP Log rows.

	This is the authoritative header writer. ``MOPLog.validate`` calls it for every
	row it saves and the Department/Employee IR cancel legs call it after a bulk
	is_cancelled flip, so the header is always the sum of the batch tier rather than
	one row's stale snapshot of the family-wide tier.

	``pending`` is the in-flight MOPLog row (self) when invoked from
	MOPLog.validate — it overrides the DB entry for the same (item, batch) key,
	or drops it when is_cancelled=1.

	Rows at or before the MWO's Work Order Refining cutoff are ignored: refining
	zeroes the MWO, so a surviving pre-refining row must not rebuild a header that
	the Refining Entry deliberately set to 0. See :func:`drop_pre_refining_rows`.

	``prefixes`` narrows the write to the named weight families (values of
	``FIELD_MAP``, e.g. ``("finding",)``). MOPLog.validate passes the single family
	its row belongs to, so a per-row save can only touch the bucket that row
	actually moves and never clobbers a bucket authored outside MOP Log -- the
	MWO->MOP weight copy in ``create_manufacturing_operation`` seeds diamond/gemstone
	weights before any ledger row exists, and an unnarrowed recompute on the first
	metal row would zero them. Omit it to rewrite every bucket (the cancel legs and
	the repair patch do, deliberately).

	NEGATIVE BALANCES ARE CLAMPED OUT of the buckets (see
	``jewellery_erpnext.utils.clamp_negative_balance``). A negative ``(item, batch)``
	balance is ledger corruption, not stock, and every other reader of this tier already
	refuses to count it -- this function summing it raw is what made MOP-3DP57 report
	gross_wt 16.440 against a Serial Number Creator total_weight of 16.720. The clamp is
	HEADER-ONLY: the MOP Log rows keep their negative values, so
	``audit_negative_batch_balances`` still finds them and ``update_new_mop_wtg`` still
	clones an inherited negative forward rather than inventing metal. Anything that
	replays this ledger to compare against a header must clamp identically --
	``audit_mop_balance_drift`` and ``patches/repair_mop_header_weight_buckets`` do.
	"""
	rows = frappe.db.sql(
		"""
		SELECT
		    item_code,
		    batch_no,
		    qty_after_transaction_batch_based  AS qaf_batch,
		    pcs_after_transaction_batch_based  AS pcs_batch,
		    manufacturing_work_order,
		    name,
		    creation
		FROM `tabMOP Log`
		WHERE manufacturing_operation = %s
		  AND is_cancelled = 0
		ORDER BY creation ASC
		""",
		(mop_name,),
		as_dict=True,
	)

	# Latest by (item, batch): ASC order means later rows overwrite earlier.
	latest = {}
	for row in rows:
		key = (row["item_code"], row["batch_no"])
		latest[key] = row

	# Dedup first, then drop by cutoff: a key whose latest row is post-refining
	# carries forward; a key with only pre-refining rows drops out entirely.
	mwo = next(
		(
			row.get("manufacturing_work_order")
			for row in latest.values()
			if row.get("manufacturing_work_order")
		),
		None,
	) or getattr(pending, "manufacturing_work_order", None)
	surviving = {
		(row.get("item_code"), row.get("batch_no")): row
		for row in drop_pre_refining_rows(list(latest.values()), mwo)
	}

	# Overlay the in-flight row (from MOPLog.validate) AFTER the cutoff: it is being
	# written now, so it is post-refining by construction and needs no timestamp of
	# its own -- which also keeps this path off the system time zone.
	if pending is not None:
		key = (pending.item_code, pending.batch_no)
		if cint(pending.is_cancelled):
			surviving.pop(key, None)
		else:
			surviving[key] = {
				"item_code": pending.item_code,
				"batch_no": pending.batch_no,
				"qaf_batch": flt(pending.qty_after_transaction_batch_based),
				"pcs_batch": flt(pending.pcs_after_transaction_batch_based),
			}

	entries = list(surviving.values())

	# Aggregate into weight buckets per item-type prefix.
	buckets = {
		"net_wt": 0.0,
		"finding_wt": 0.0,
		"diamond_wt": 0.0,
		"diamond_wt_in_gram": 0.0,
		"diamond_pcs": 0.0,
		"gemstone_wt": 0.0,
		"gemstone_wt_in_gram": 0.0,
		"gemstone_pcs": 0.0,
		"other_wt": 0.0,
	}
	for entry in entries:
		first_char = (entry.get("item_code") or "")[:1]
		prefix = FIELD_MAP.get(first_char)
		if not prefix:
			continue
		# A negative batch balance is corruption, not stock -- an operation cannot hold
		# less than none of a batch. Clamped HERE, and only here, for three reasons:
		# this sits BELOW the latest-per-key dedup, the refining cutoff and the pending
		# overlay (clamping in the SQL above would drop the LATEST row for a negative
		# key and silently promote an earlier, superseded positive one to "latest"),
		# and ABOVE both the carat->gram derivation and the ``prefixes`` narrowing, so
		# the gram twin stays a pure function of the CLAMPED carat bucket -- which is
		# the invariant normalize_mop_carat_to_gram_buckets detects on.
		# HEADER-ONLY: the ledger row keeps its negative value so
		# audit_negative_batch_balances still reports it. See clamp_negative_balance.
		qty, pcs = clamp_negative_balance(
			entry.get("qaf_batch"), entry.get("pcs_batch")
		)
		buckets[f"{prefix}_wt"] += qty
		if prefix in ("diamond", "gemstone"):
			buckets[f"{prefix}_pcs"] += pcs

	# Grams is a DERIVED view of the carat bucket, never its own tally. Rounding
	# every (item, batch) row to 3 dp before summing let the two disagree:
	# MOP-050YL carried flt(0.497 * 0.2, 3) + flt(0.067 * 0.2, 3) = 0.099 + 0.013
	# = 0.112 g for 0.564 ct, where the carat total converts to 0.113. update_wt_detail
	# then folded that half-milligram into gross_wt, so the operation opened 0.001 g
	# short of prev_gross_wt with no physical loss behind it -- and the drift runs
	# both ways (MOP-IN870 rounded UP, opening as an unbacked gain). Convert once --
	# every other carat->gram site in the app already does (SerialNumberCreator's
	# _compute_total_weight, the Department IR SUM(IF(uom = 'Carat', ...)) queries).
	#
	# This MUST stay ABOVE the ``prefixes`` filter below. Past that point the carat
	# bucket a gram is derived from has already been dropped for every family the
	# caller did not name, so a metal-only save would derive 0 and wipe a gram
	# authored outside MOP Log. The carat buckets are deliberately left as summed:
	# ``diamond_wt`` / ``gemstone_wt`` must read byte-identical to before, which is
	# what keeps the product-tolerance and Employee IR carat surfaces untouched.
	for prefix in ("diamond", "gemstone"):
		buckets[f"{prefix}_wt_in_gram"] = carat_to_gram(buckets[f"{prefix}_wt"])

	if prefixes:
		wanted = set()
		for prefix in prefixes:
			wanted.update({f"{prefix}_wt", f"{prefix}_wt_in_gram", f"{prefix}_pcs"})
		buckets = {k: v for k, v in buckets.items() if k in wanted}
		if not buckets:
			return

	frappe.db.set_value("Manufacturing Operation", mop_name, buckets)
	update_wt_detail(mop_name)


def get_mop_opening_balances(manufacturing_operation, item_code, batch_no, mwo=None):
	"""Opening balance of ONE operation, for the three tiers MOP Log tracks.

	The ``qty_after_transaction*`` / ``pcs_after_transaction*`` tiers are per-OPERATION
	running balances -- that is how every reader treats them:
	:func:`get_current_mop_balance_rows`,
	:func:`recalculate_manufacturing_operation_weights`, :func:`update_wt_detail`, the
	Make Receive Entry availability popup and the balance-details report. So the opening
	balance is derived here from the operation's OWN latest row per
	``(item_code, batch_no)``.

	It is deliberately NOT ``SUM(qty_change)`` over the Manufacturing Work Order. A MWO
	routinely carries residue stranded on a finished operation -- metal returned short of
	the balance, a refined MWO, a rework loop -- and a MWO-wide sum folds that residue
	into the next operation's opening balance, inflating it. That produced gross weights
	0.01g above the received weight on re-cast operations.

	Scoping the sum to the operation instead is NOT a fix on its own: baseline clone rows
	carry ``qty_change = 0`` while carrying a non-zero balance, so summing changes within
	one operation reads 0 for an operation that legitimately inherited a balance. The
	inherited balance is written explicitly by the clone writers
	(:func:`update_new_mop_wtg`, :func:`create_mop_log_for_department_ir`,
	:func:`creste_mop_log_for_employee_ir`), and read back from here as an absolute value.

	Rows at or before the MWO's Work Order Refining submit time are ignored: refining
	zeroes the MWO, so a surviving pre-refining row must not resurrect a dead balance.
	Recasting a refined MWO is an expected manual flow, so this only clamps the opening
	balance -- the movement itself is still ledgered. See
	:func:`~jewellery_erpnext.utils.get_mwo_refining_cutoff`.

	A fresh operation has no rows and opens at zero, which is exactly right.
	"""
	empty = {
		"qty_prefix": 0.0,
		"qty_item": 0.0,
		"qty_batch": 0.0,
		"pcs_prefix": 0,
		"pcs_item": 0,
		"pcs_batch": 0,
	}
	if not (manufacturing_operation and item_code):
		return empty

	rows = get_current_mop_balance_rows(
		manufacturing_operation,
		include_fields=[
			"item_code",
			"batch_no",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
		],
	)
	if not rows:
		return empty

	# Only pay for the refining lookup when there is actually a balance to clamp.
	cutoff = get_mwo_refining_cutoff(mwo)

	first_char = item_code[0]
	totals = dict(empty)
	for log in rows:
		row_item = log.get("item_code") or ""
		if not row_item.startswith(first_char):
			continue
		if cutoff and get_datetime(log.get("creation")) <= get_datetime(cutoff):
			continue
		qty = flt(log.get("qty_after_transaction_batch_based"))
		pcs = cint(log.get("pcs_after_transaction_batch_based"))
		totals["qty_prefix"] += qty
		totals["pcs_prefix"] += pcs
		if row_item != item_code:
			continue
		totals["qty_item"] += qty
		totals["pcs_item"] += pcs
		if log.get("batch_no") == batch_no:
			totals["qty_batch"] += qty
			totals["pcs_batch"] += pcs

	totals["qty_prefix"] = flt(totals["qty_prefix"], 3)
	totals["qty_item"] = flt(totals["qty_item"], 3)
	totals["qty_batch"] = flt(totals["qty_batch"], 3)
	return totals


def drop_pre_refining_rows(rows, manufacturing_work_order):
	"""Drop source rows that pre-date the MWO's Work Order Refining cutoff.

	Refining zeroes the MWO, so a pre-refining row describes metal that no longer
	exists and must never be cloned into a downstream operation. Rows written AFTER
	the cutoff describe a recast -- an expected manual flow, since
	``complete_refining`` only advises the Work Order action -- and must be.

	The clone writers used to answer this with a blanket ``is_mwo_refined`` early
	return, which threw away the post-refining balance too: a Department IR handoff
	after a recast wrote no ledger rows at all, so the receiving operation opened at
	0 while the metal stayed stranded on the source operation. Filtering by the
	cutoff keeps the guard's intent (no stale pre-refining figures) without deleting
	the live balance.

	Callers must include ``creation`` in the fields they fetch.
	"""
	if not rows:
		return rows
	cutoff = get_mwo_refining_cutoff(manufacturing_work_order)
	if not cutoff:
		return rows
	cutoff = get_datetime(cutoff)
	return [r for r in rows if get_datetime(r.get("creation")) > cutoff]


def create_mop_log_for_stock_transfer_to_mo(doc, row, is_synced=False):
	item_code = row.get("item_code") or ""
	if not item_code:
		return

	first_char = item_code[0]
	if doc.doctype == "Employee IR":
		if first_char not in ("D", "G"):
			pcs = 0
		else:
			pcs = row.get("pcs_change") or 0
		qty = row.get("qty_change") or 0
	else:
		if doc.stock_entry_type == "Material Receive (WORK ORDER)":
			if first_char not in ("D", "G"):
				pcs = 0
			else:
				pcs = -cint(row.get("pcs") or 0)
			qty = -flt((row.get("qty") or 0.0), 3)
		else:
			pcs = cint(row.get("pcs") or 0)
			qty = flt((row.get("qty") or 0.0), 3)
	batch_no = row.get("batch_no")
	mwo = (
		row.get("manufacturing_work_order")
		if doc.doctype == "Employee IR"
		else doc.get("manufacturing_work_order")
	)
	opening = get_mop_opening_balances(
		row.get("manufacturing_operation"), item_code, batch_no, mwo
	)

	last_mop_index = get_last_mop_index(row.manufacturing_operation)
	# compute fields
	pcs_after_prefix = pcs + cint(opening["pcs_prefix"])
	pcs_after_item = pcs + cint(opening["pcs_item"])
	pcs_after_batch = pcs + cint(opening["pcs_batch"])

	qty_after_prefix = flt(qty + flt(opening["qty_prefix"]), 3)
	qty_after_item = flt(qty + flt(opening["qty_item"]), 3)
	qty_after_batch = flt(qty + flt(opening["qty_batch"]), 3)
	# create doc
	mop_log = frappe.new_doc("MOP Log")
	mop_log.item_code = item_code
	mop_log.pcs_change = pcs
	mop_log.pcs_after_transaction = pcs_after_prefix
	mop_log.pcs_after_transaction_item_based = pcs_after_item
	mop_log.pcs_after_transaction_batch_based = pcs_after_batch

	# Stock Entry Detail rows use s_warehouse/t_warehouse; MOP-Log-shaped dicts
	# (e.g. from update_new_mop_wtg) use from_warehouse/to_warehouse. Accept both
	# so next-operation baseline rows keep their warehouses.
	mop_log.from_warehouse = row.get("s_warehouse") or row.get("from_warehouse")
	mop_log.to_warehouse = row.get("t_warehouse") or row.get("to_warehouse")
	mop_log.voucher_type = doc.doctype
	mop_log.voucher_no = doc.name
	mop_log.manufacturing_work_order = mwo
	mop_log.manufacturing_operation = row.get("manufacturing_operation")
	mop_log.row_name = row.name
	mop_log.qty_change = qty
	mop_log.qty_after_transaction = qty_after_prefix
	mop_log.qty_after_transaction_item_based = qty_after_item
	mop_log.qty_after_transaction_batch_based = qty_after_batch

	mop_log.is_synced = is_synced
	mop_log.serial_and_batch_bundle = row.get("serial_and_batch_bundle")
	mop_log.batch_no = batch_no
	mop_log.flow_index = last_mop_index + 1 if last_mop_index == 0 else 0
	mop_log.save()


def get_last_mop_index(manufacturing_operation, voucher_type=None, voucher_no=None):
	MOPLog = DocType("MOP Log")
	query = (
		frappe.qb.from_(MOPLog)
		.select(Max(MOPLog.flow_index))
		.where(
			(MOPLog.manufacturing_operation == manufacturing_operation)
			& (MOPLog.is_cancelled == 0)
		)
	)
	if voucher_type:
		query = query.where(MOPLog.voucher_type == voucher_type)
	if voucher_no:
		query = query.where(MOPLog.voucher_no == voucher_no)

	result = query.run(as_list=True)
	return result[0][0] if result and result[0] else None


def get_current_mop_balance_rows(
	manufacturing_operation, include_fields=None, keys=None, exclude_voucher_no=None
):
	"""Return the latest non-cancelled MOP Log row per item/batch for a MOP.

	Loss-attribution rows (log_category="Loss Attribution") ARE included —
	they post a real qty_change reduction so the balance after loss must be
	reflected to downstream readers (e.g. Make Receive Entry availability,
	manual loss validation, EOD SRE reconciliation).

	When ``keys`` is provided as a list of ``(item_code, batch_no)`` tuples,
	the underlying ``frappe.db.get_all`` is narrowed by the distinct
	``item_code`` set so popups never scan unrelated items. The composite
	index ``mop_balance_idx`` (added by ``add_make_receive_entry_indexes``)
	covers ``(manufacturing_operation, is_cancelled, item_code, batch_no,
	creation)`` so the narrowed filter is index-served. The Python-side
	dedup picks the latest row per ``(item_code, batch_no)``.
	"""
	fields = list(
		dict.fromkeys((include_fields or current_balance_fields) + ["name", "creation"])
	)
	filters = {
		"manufacturing_operation": manufacturing_operation,
		"is_cancelled": 0,
	}
	if exclude_voucher_no:
		filters["voucher_no"] = ["!=", exclude_voucher_no]
	if keys:
		item_codes = sorted({k[0] for k in keys if k and k[0]})
		if not item_codes:
			return []
		filters["item_code"] = ["in", item_codes]
	mop_logs = frappe.db.get_all(
		"MOP Log",
		filters=filters,
		fields=fields,
		order_by="creation desc",
	)
	if not mop_logs:
		return []

	latest_by_key = {}
	for log in mop_logs:
		key = (log.get("item_code"), log.get("batch_no"))
		if key not in latest_by_key:
			latest_by_key[key] = log
	return list(reversed(list(latest_by_key.values())))


def get_mwo_balance_rows(manufacturing_work_order, include_fields=None, keys=None):
	"""Return the latest non-cancelled MOP Log row per item/batch for a whole MWO.

	:func:`get_current_mop_balance_rows` answers "what did operation X last
	record?". This answers "what does this Manufacturing Work Order hold right
	now?" — and for availability checks the second question is the correct one.

	The distinction is forced by how the number is written.
	:func:`create_mop_log_for_stock_transfer_to_mo` computes
	``qty_after_transaction_batch_based`` as an MWO-wide running sum
	(``WHERE manufacturing_work_order = %s AND is_cancelled = 0``; the per-MOP
	narrowing right below it is commented out) and only then stamps the row with
	a single operation. **A per-MOP balance does not exist in this field.** A
	MOP-scoped read returns the MWO-wide total frozen at whenever THAT operation
	last wrote, and goes stale the moment any other operation under the same MWO
	posts a row — a Department/Employee IR handoff clone, a loss attribution, a
	receive.

	That staleness is not hypothetical: on handoff the source operation is
	explicitly zeroed and the destination carries the balance forward, while
	``Stock Reservation Entry.manufacturing_operation`` keeps pointing at the
	operation the reservation was created against. Reading the SRE's stamp
	therefore returns the zeroed row and blocks a receive the operator can
	legitimately make.

	Reading the latest row MWO-wide is the only scope invariant to which
	operation the reader happens to be standing on, and it always reflects every
	non-cancelled delta — including a loss booked at a sibling operation, which
	an opened-MOP-scoped read would miss.

	Operations under one MWO form a linear chain (``previous_mop``), so "latest
	across the MWO" is a well-defined current state and not a merge of
	concurrent branches.

	Index-served by ``mop_mwo_idx`` (manufacturing_work_order, is_cancelled,
	item_code, batch_no) from ``add_make_receive_entry_indexes``.

	Dedup rule, ordering and return shape are deliberately identical to
	:func:`get_current_mop_balance_rows` so the two stay drop-in
	interchangeable. ``creation desc`` is kept bare on purpose — MOP Log has no
	``autoname``, so ``name`` is a hash and would be a false tiebreak.
	"""
	fields = list(
		dict.fromkeys((include_fields or current_balance_fields) + ["name", "creation"])
	)
	filters = {
		"manufacturing_work_order": manufacturing_work_order,
		"is_cancelled": 0,
	}
	if keys:
		item_codes = sorted({k[0] for k in keys if k and k[0]})
		if not item_codes:
			return []
		filters["item_code"] = ["in", item_codes]
	mop_logs = frappe.db.get_all(
		"MOP Log",
		filters=filters,
		fields=fields,
		order_by="creation desc",
	)
	if not mop_logs:
		return []

	latest_by_key = {}
	for log in mop_logs:
		key = (log.get("item_code"), log.get("batch_no"))
		if key not in latest_by_key:
			latest_by_key[key] = log
	return list(reversed(list(latest_by_key.values())))


def get_mop_transfer_pcs_rows(manufacturing_work_order, keys=None):
	"""Return per-row incoming-transfer PCS rows for a MWO, grouped by item/batch.

	Unlike :func:`get_current_mop_balance_rows` (which dedups to the single
	latest *running balance* per ``(item_code, batch_no)``), this keeps EVERY
	incoming Material Transfer row so the Make Receive Entry popup can show the
	PCS that belongs to each individual reserved batch line instead of the
	batch-wide aggregate. Each Material Transfer (WORK ORDER) row posts one MOP
	Log row carrying that row's own ``pcs_change``/``qty_change``; receiving and
	loss rows post ``pcs_change <= 0`` and are excluded here.

	Scoped to the **Manufacturing Work Order**, not a single MOP: the original
	per-row transfer (with ``pcs_change > 0``) is logged once at the operation
	that first received the material (e.g. Diamond Bagging). When work hands off
	to the next operation, MOP Log carries only a balance *clone* with
	``pcs_change = 0`` (qty/pcs change attributable to a handoff is zero), so a
	MOP-scoped query at a downstream operation would find no per-row rows and the
	popup would fall back to the batch aggregate. The MWO is constant across all
	its operations, so this recovers the original 24/8 style breakdown regardless
	of which operation opens the popup.

	Returns ``{(item_code, batch_no): [{"qty_change", "pcs_change", "name"}, ...]}``
	ordered oldest-first within each key. Rows missing ``pcs_change``/``qty_change``
	(e.g. unit-test fixtures that mock unrelated docs through the same patched
	``frappe.db.get_all``) are skipped so callers degrade to their own fallback.
	"""
	filters = {
		"manufacturing_work_order": manufacturing_work_order,
		"is_cancelled": 0,
		"pcs_change": [">", 0],
	}
	if keys:
		item_codes = sorted({k[0] for k in keys if k and k[0]})
		if not item_codes:
			return {}
		filters["item_code"] = ["in", item_codes]
	rows = frappe.db.get_all(
		"MOP Log",
		filters=filters,
		fields=[
			"item_code",
			"batch_no",
			"qty_change",
			"pcs_change",
			"name",
			"creation",
		],
		order_by="creation asc",
	)
	transfer_by_key: dict[tuple, list] = {}
	for row in rows:
		# Defensive: the unit tests patch frappe.db.get_all globally and feed
		# back SRE dicts; those lack pcs_change/qty_change, so skip them.
		if row.get("pcs_change") is None or row.get("qty_change") is None:
			continue
		key = (row.get("item_code"), row.get("batch_no"))
		transfer_by_key.setdefault(key, []).append(
			{
				"qty_change": flt(row.get("qty_change")),
				"pcs_change": cint(row.get("pcs_change")),
				"name": row.get("name"),
			}
		)
	return transfer_by_key


def get_available_qty_pcs_for_mop_item(
	manufacturing_operation,
	item_code,
	batch_no=None,
	warehouse=None,
	stock_reservation_entry=None,
	stock_reservation_entry_detail=None,
	manufacturing_work_order=None,
	sre_remaining_qty=None,
	already_received_qty=0,
	already_received_pcs=0,
	mop_log_balance_map=None,
):
	"""Reconcile Qty/PCS for a single MOP item/batch row.

	Cross-checks Stock Reservation Entry, MOP Log, and Stock Entry to produce
	a single dict the Make Receive Entry popup, the server validator
	(``create_mr_wo_stock_entry``) and Employee IR manual-loss validation
	can all consume.

	is_pcs_item is gated by FIELD_MAP membership AND item_code[0] in (D, G).

	available_qty = min(positive authoritative values among SRE remaining qty
	and MOP Log batch-based qty). Missing values are treated as "no signal"
	(spec: ``If one source does not store PCS, do not treat missing PCS as
	zero. Treat it as unknown.`` — same rule applied to qty).

	available_pcs:
	  * 0 for non-D/G items.
	  * For D/G, MOP Log batch-based PCS is the authoritative source. SRE has
	    no PCS field, so it is excluded from the candidate set (treating it
	    as 0 would force Available PCS to 0 incorrectly per spec).
	  * 0 falls through when no MOP Log row exists for (item, batch).

	The default lookup is scoped to the single ``manufacturing_operation``.
	Callers that must agree with each other about availability — the Make
	Receive Entry popup and its server validator — instead build an MWO-scoped
	map with :func:`get_mwo_balance_rows` and pass it as
	``mop_log_balance_map``, because the underlying balance is an MWO-wide
	running sum with no per-operation meaning.
	"""
	is_pcs_item = bool(item_code) and item_code[0] in ("D", "G")

	if mop_log_balance_map is None:
		rows = get_current_mop_balance_rows(manufacturing_operation)

		mop_log_balance_map = {
			(row.get("item_code"), row.get("batch_no")): row for row in rows
		}
	mop_row = mop_log_balance_map.get((item_code, batch_no))
	mop_qty_raw = mop_row.get("qty_after_transaction_batch_based") if mop_row else None
	mop_pcs_raw = mop_row.get("pcs_after_transaction_batch_based") if mop_row else None
	mop_log_reference = mop_row.get("name") if mop_row else None
	qty_candidates = []
	if sre_remaining_qty is not None:
		qty_candidates.append(flt(sre_remaining_qty))
	if mop_qty_raw is not None:
		qty_candidates.append(flt(mop_qty_raw))
	# Clamp negative balances to 0; downstream callers/UI round for display.
	available_qty = max(0.0, min(qty_candidates)) if qty_candidates else 0.0

	if not is_pcs_item:
		available_pcs = 0
	else:
		pcs_candidates = []
		if mop_pcs_raw is not None:
			pcs_candidates.append(cint(mop_pcs_raw))
		# SRE never stores PCS, so it does NOT enter the candidate set.
		available_pcs = max(0, min(pcs_candidates)) if pcs_candidates else 0

	return {
		"item_code": item_code,
		"batch_no": batch_no,
		"source_warehouse": warehouse,
		"stock_reservation_entry": stock_reservation_entry,
		"stock_reservation_entry_detail": stock_reservation_entry_detail,
		"manufacturing_work_order": manufacturing_work_order,
		"reserved_qty": flt(sre_remaining_qty or 0),
		"reserved_pcs": None,
		"mop_log_balance_qty": flt(mop_qty_raw or 0),
		"mop_log_balance_pcs": cint(mop_pcs_raw or 0),
		"stock_entry_transferred_qty": 0,
		"stock_entry_transferred_pcs": 0,
		"already_received_qty": flt(already_received_qty or 0),
		"already_received_pcs": cint(already_received_pcs or 0),
		"available_qty": available_qty,
		"available_pcs": available_pcs,
		"is_pcs_item": is_pcs_item,
		"mop_log_reference": mop_log_reference,
		"mop_data_present": mop_log_reference is not None,
	}


def create_mop_log_for_department_ir(
	self, row, to_warehouse, from_warehouse, operation
):
	# A refined MWO's PRE-refining rows are gone (the Refining Entry zeroed the
	# balance); cloning them forward would recompute the new operation's weight
	# buckets back to the pre-refining figures and give EOD sync phantom stock to
	# move. Post-refining rows are a recast and must still be carried -- see
	# drop_pre_refining_rows. This used to early-return on is_mwo_refined, which
	# dropped the recast balance too and opened the receiving operation at 0.
	mop_logs = []
	is_receive = getattr(self, "type", None) == "Receive" and getattr(
		self, "receive_against", None
	)

	if is_receive:
		mop_logs = frappe.db.get_all(
			"MOP Log",
			filters={
				"manufacturing_operation": row.manufacturing_operation,
				"is_cancelled": 0,
				"voucher_type": "Department IR",
				"voucher_no": self.receive_against,
			},
			fields=select_fields + ["creation"],
			order_by="creation asc",
		)

	else:
		# Latest row per (item, batch), NOT every historical row. Cloning the whole
		# history doubled the row count at every Department IR hop (2 -> 4 -> 8 ...)
		# and left stale pre-loss balances interleaved with the true one, so which
		# figure a reader saw depended on how it happened to dedup. Mirrors
		# creste_mop_log_for_employee_ir, which already sources the same snapshot.
		mop_logs = get_current_mop_balance_rows(
			row.manufacturing_operation,
			include_fields=select_fields,
		)

	# Dedup first, then drop by cutoff: a key whose latest row is post-refining
	# carries forward; a key with only pre-refining rows drops out entirely.
	mop_logs = drop_pre_refining_rows(mop_logs, row.manufacturing_work_order)

	for log in mop_logs:
		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = log.item_code
		mop_log.pcs_after_transaction = log.pcs_after_transaction
		mop_log.pcs_after_transaction_item_based = log.pcs_after_transaction_item_based
		mop_log.pcs_after_transaction_batch_based = (
			log.pcs_after_transaction_batch_based
		)
		mop_log.from_warehouse = from_warehouse
		mop_log.to_warehouse = to_warehouse
		mop_log.voucher_type = "Department IR"
		mop_log.voucher_no = self.name
		mop_log.row_name = row.name
		mop_log.qty_after_transaction = log.qty_after_transaction
		mop_log.qty_after_transaction_item_based = log.qty_after_transaction_item_based
		mop_log.qty_after_transaction_batch_based = (
			log.qty_after_transaction_batch_based
		)
		mop_log.is_synced = 0
		mop_log.manufacturing_operation = operation
		mop_log.manufacturing_work_order = row.manufacturing_work_order
		mop_log.serial_and_batch_bundle = log.serial_and_batch_bundle
		mop_log.batch_no = log.batch_no
		mop_log.flow_index = log.flow_index + 1
		mop_log.save()


def _get_mop_logs_for_employee_ir_issue(row, department_receive_id):
	"""Source rows for Employee IR Issue MOP Log cloning.

	Uses the canonical current-balance snapshot so bagging/material-request additions
	already written into MOP Log are issued alongside department-transferred metal.
	"""
	return get_current_mop_balance_rows(
		row.manufacturing_operation,
		include_fields=select_fields,
	)


def creste_mop_log_for_employee_ir(self, row, from_warehouse, to_warehouse):
	# Same treatment as the Department IR clone above: a refined MWO's pre-refining
	# balance must not be issued to an employee, but its post-refining recast
	# balance must be. A blanket refusal here stranded the metal on the source
	# operation.
	department_receive_id = frappe.db.get_value(
		"Manufacturing Operation", row.manufacturing_operation, "department_receive_id"
	)
	mop_logs = drop_pre_refining_rows(
		_get_mop_logs_for_employee_ir_issue(row, department_receive_id),
		row.manufacturing_work_order,
	)
	for log in mop_logs:
		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = log.item_code
		mop_log.pcs_after_transaction = log.pcs_after_transaction
		mop_log.pcs_after_transaction_item_based = log.pcs_after_transaction_item_based
		mop_log.pcs_after_transaction_batch_based = (
			log.pcs_after_transaction_batch_based
		)
		mop_log.from_warehouse = from_warehouse
		mop_log.to_warehouse = to_warehouse
		mop_log.voucher_type = self.doctype
		mop_log.voucher_no = self.name
		mop_log.row_name = row.name
		mop_log.qty_after_transaction = log.qty_after_transaction
		mop_log.qty_after_transaction_item_based = log.qty_after_transaction_item_based
		mop_log.qty_after_transaction_batch_based = (
			log.qty_after_transaction_batch_based
		)
		mop_log.is_synced = 0
		mop_log.manufacturing_operation = row.manufacturing_operation
		mop_log.manufacturing_work_order = row.manufacturing_work_order
		mop_log.serial_and_batch_bundle = log.serial_and_batch_bundle
		mop_log.batch_no = log.batch_no
		mop_log.flow_index = log.flow_index + 1
		mop_log.save()


def resolve_employee_ir_issue_voucher_for_receive(doc, row):
	"""Employee IR Issue name whose MOP Logs this Receive must clone (voucher_no on Issue logs).

	Uses ``emp_ir_id`` when it points to a submitted Issue that includes this MOP;
	otherwise the latest submitted Employee IR Issue containing ``row.manufacturing_operation``.
	"""
	emp_ir_id = cstr(getattr(doc, "emp_ir_id", None) or "").strip()
	if emp_ir_id:
		meta = frappe.db.get_value(
			"Employee IR",
			emp_ir_id,
			["docstatus", "type"],
			as_dict=True,
		)
		if (
			meta
			and meta.type == "Issue"
			and cint(meta.docstatus) == 1
			and frappe.db.exists(
				"Employee IR Operation",
				{
					"parent": emp_ir_id,
					"manufacturing_operation": row.manufacturing_operation,
				},
			)
		):
			return emp_ir_id

	rows = frappe.db.sql(
		"""
		SELECT eir.name
		FROM `tabEmployee IR` eir
		INNER JOIN `tabEmployee IR Operation` op ON op.parent = eir.name
		WHERE eir.docstatus = 1
		  AND eir.type = 'Issue'
		  AND op.manufacturing_operation = %s
		ORDER BY eir.modified DESC, eir.name DESC
		LIMIT 1
		""",
		row.manufacturing_operation,
	)
	return rows[0][0] if rows else None


def get_employee_ir_loss_map(eir_doc):
	"""Build the (mop, mwo, item_code, batch_no) → loss bucket map.

	The bucket records loss in the *MOP Log UOM* (carats for D/G, grams for
	M/F/O) — there is NO carat→gram conversion at this layer. Conversion to
	grams happens once, downstream, in MOPLog.validate when it writes
	``diamond_wt_in_gram`` / ``gemstone_wt_in_gram`` as ``qty_after_transaction
	* 0.2``.

	Both ``employee_loss_details`` (auto, M/F-only) and
	``manually_book_loss_details`` (any prefix) feed the map. The bucket
	carries enough audit data to populate ``loss_weight`` (grams, for
	display), ``loss_source_row``, and ``loss_type`` on the combined receive
	MOP Log row downstream.
	"""
	loss_map = {}

	def _add(row, loss_type):
		if not (row.manufacturing_operation and row.item_code):
			return
		key = (
			row.manufacturing_operation,
			row.manufacturing_work_order,
			row.item_code,
			row.batch_no,
		)
		bucket = loss_map.setdefault(
			key,
			{
				"loss_qty": 0.0,
				"loss_pcs": 0,
				"loss_types": set(),
				"source_rows": [],
				"loss_weight_grams": 0.0,
			},
		)
		qty = flt(row.proportionally_loss)
		bucket["loss_qty"] += qty
		# loss_weight_grams is for AUDIT only; convert D/G carat→gram here.
		first_char = row.item_code[0] if row.item_code else ""
		if first_char in ("D", "G"):
			bucket["loss_weight_grams"] += qty * 0.2
			bucket["loss_pcs"] += cint(getattr(row, "pcs", 0) or 0)
		else:
			bucket["loss_weight_grams"] += qty
		bucket["loss_types"].add(loss_type)
		if row.name:
			bucket["source_rows"].append(row.name)

	for r in eir_doc.get("employee_loss_details") or []:
		_add(r, "Auto Employee Loss")
	for r in eir_doc.get("manually_book_loss_details") or []:
		_add(r, "Manually Booked Loss")

	# Sets aren't JSON-stable; flatten to a sorted list.
	for v in loss_map.values():
		v["loss_types"] = sorted(v["loss_types"])
	return loss_map


def create_mop_log_for_employee_ir_receive(
	doc, row, from_warehouse, to_warehouse, stock_entry_name=[]
):
	"""Audit-only MOP Log clones on the SOURCE MOP for Employee IR Receive.

	Reads the MOP Logs created during the matching Employee IR **Issue** only
	(``voucher_no`` = Issue name), not every historical Employee IR log on
	the MOP.

	**Source MOP is left unchanged.** Per the new contract, loss-driven
	weight reductions land on the NEW Manufacturing Operation only — see
	``update_new_mop_wtg``, which both clones the source baseline AND
	subtracts loss in-place per ``(item, batch)``. The rows written here
	are pure clones (qty_change=0) of the issue-tier balance so audit
	metadata (loss_weight, loss_type, loss_source_row) stays attached to a
	real MOP Log row on the source MOP for traceability, but no balance
	shift happens.

	UOM rule: ``qty_after_transaction*`` stay in the item's stock UOM
	(carats for D/G, grams for M/F/O), copied verbatim from the source log.
	MOPLog.validate's prefix-bucket write is a no-op here because the qty
	matches what is already on the source MOP.
	"""
	issue_voucher = resolve_employee_ir_issue_voucher_for_receive(doc, row)
	mop_logs = []
	mop_logs = (
		frappe.db.get_all(
			"MOP Log",
			{
				"manufacturing_operation": row.manufacturing_operation,
				"is_cancelled": 0,
				"voucher_type": "Employee IR",
				"voucher_no": issue_voucher,
			},
			select_fields,
			order_by="creation asc",
		)
		or []
	)
	if stock_entry_name:
		mop_logs += (
			frappe.db.get_all(
				"MOP Log",
				{
					"manufacturing_operation": row.manufacturing_operation,
					"is_cancelled": 0,
					"voucher_type": "Stock Entry",
					"voucher_no": ["in", stock_entry_name],
				},
				select_fields,
				order_by="creation asc",
			)
			or []
		)

	mop_logs += (
		frappe.db.get_all(
			"MOP Log",
			{
				"manufacturing_operation": row.manufacturing_operation,
				"is_cancelled": 0,
				"voucher_type": "Stock Entry",
				"voucher_no": [
					"in",
					frappe.db.get_all(
						"Stock Entry",
						filters={
							"stock_entry_type": "Material Transfer (WORK ORDER)",
							"employee_ir": ["is", "not set"],
							"manufacturing_operation": row.manufacturing_operation,
							"docstatus": 1,
							"to_employee": ["is", "set"],
						},
						pluck="name",
					),
				],
			},
			select_fields,
			order_by="creation asc",
		)
		or []
	)
	mop_logs += (
		frappe.db.get_all(
			"MOP Log",
			{
				"manufacturing_operation": row.manufacturing_operation,
				"is_cancelled": 0,
				"voucher_type": "Stock Entry",
				"voucher_no": [
					"in",
					frappe.db.get_all(
						"Stock Entry",
						filters={
							"stock_entry_type": "Material Receive (WORK ORDER)",
							"manufacturing_operation": row.manufacturing_operation,
							"docstatus": 1,
						},
						pluck="name",
					),
				],
			},
			select_fields,
			order_by="creation asc",
		)
		or []
	)

	# Build the EIR-wide loss map once, then narrow to entries that match
	# THIS receive row's MOP+MWO. Prior loss buckets get consumed exactly
	# once across the source-log loop; if multiple source logs match the
	# same (item, batch), only the first one absorbs the loss to prevent
	# double-subtraction.
	full_loss_map = get_employee_ir_loss_map(doc)
	consumed_loss_keys = set()

	for log in mop_logs:
		loss_key = (
			row.manufacturing_operation,
			row.manufacturing_work_order,
			log.item_code,
			log.batch_no,
		)
		loss = (
			full_loss_map.get(loss_key) if loss_key not in consumed_loss_keys else None
		)

		# SOURCE MOP audit clone: qty_change=0, balances copied verbatim.
		# Loss is applied to the NEW MOP inside update_new_mop_wtg's
		# baseline-clone loop (one row per item/batch, already reduced).
		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = log.item_code
		mop_log.qty_change = 0
		mop_log.pcs_change = 0

		mop_log.qty_after_transaction = flt(log.qty_after_transaction)
		mop_log.qty_after_transaction_item_based = flt(
			log.qty_after_transaction_item_based
		)
		mop_log.qty_after_transaction_batch_based = flt(
			log.qty_after_transaction_batch_based
		)

		mop_log.pcs_after_transaction = cint(log.pcs_after_transaction)
		mop_log.pcs_after_transaction_item_based = cint(
			log.pcs_after_transaction_item_based
		)
		mop_log.pcs_after_transaction_batch_based = cint(
			log.pcs_after_transaction_batch_based
		)

		mop_log.from_warehouse = from_warehouse
		mop_log.to_warehouse = to_warehouse
		mop_log.voucher_type = "Employee IR"
		mop_log.voucher_no = doc.name
		mop_log.row_name = row.name
		mop_log.is_synced = 0
		mop_log.manufacturing_operation = row.manufacturing_operation
		mop_log.manufacturing_work_order = row.manufacturing_work_order
		mop_log.serial_and_batch_bundle = log.serial_and_batch_bundle
		mop_log.batch_no = log.batch_no
		mop_log.flow_index = log.flow_index + 1

		# Loss is applied to the NEW operation by update_new_mop_wtg; nothing to
		# carry here beyond marking the bucket consumed so a second source log
		# matching the same (item, batch) does not absorb it again.
		# NOTE: MOP Log has no loss_weight / loss_type / loss_source_row /
		# log_category fields (neither in mop_log.json nor on the live site), so
		# the assignments that used to sit here were silent no-ops.
		if loss:
			consumed_loss_keys.add(loss_key)

		mop_log.save()


def create_mop_log_for_employee_ir_loss(
	eir_doc, loss_row, loss_type, total_loss_for_mwo, from_wh=None, to_wh=None
):
	"""Bridge writer for Employee IR Receive loss attribution.

	Posts each loss detail as a real MOP Log movement so the Manufacturing
	Operation weight bucket is reduced by the loss amount. Concretely:

	  qty_change                       = -loss_weight (gram)
	  qty_after_transaction*           = previous balance - loss_weight

	When MOPLog.validate() runs it writes ``qty_after_transaction`` into the
	prefix bucket (net_wt / finding_wt / diamond_wt / gemstone_wt / other_wt)
	on Manufacturing Operation, so the post-loss weight is reflected without
	any additional bookkeeping here.

	Carat-denominated rows (typical for D / G items) are converted to grams
	via x0.2 so the MOP Log balance — which is gram-based — stays consistent.

	Idempotent on (voucher_type, voucher_no, manufacturing_operation,
	loss_source_row, loss_type, is_cancelled=0). is_synced=1 keeps the EOD
	sync from re-materializing it.
	"""
	raw = flt(loss_row.proportionally_loss)
	stock_uom = frappe.get_cached_value("Item", loss_row.item_code, "stock_uom")
	loss_weight = raw * 0.2 if stock_uom == "Carat" else raw
	if loss_weight <= 0:
		return None

	if frappe.db.exists(
		"MOP Log",
		{
			"voucher_type": "Employee IR",
			"voucher_no": eir_doc.name,
			"manufacturing_operation": loss_row.manufacturing_operation,
			"loss_source_row": loss_row.name,
			"loss_type": loss_type,
			"is_cancelled": 0,
		},
	):
		return None

	pct = (loss_weight / total_loss_for_mwo) if total_loss_for_mwo else 0

	# Latest balance for this (item, batch) on the same MOP. Includes any
	# prior loss-attribution rows so successive loss postings stack.
	latest = (
		frappe.db.get_value(
			"MOP Log",
			{
				"manufacturing_operation": loss_row.manufacturing_operation,
				"item_code": loss_row.item_code,
				"batch_no": loss_row.batch_no,
				"is_cancelled": 0,
			},
			[
				"qty_after_transaction",
				"qty_after_transaction_item_based",
				"qty_after_transaction_batch_based",
				"pcs_after_transaction",
				"pcs_after_transaction_item_based",
				"pcs_after_transaction_batch_based",
				"flow_index",
			],
			order_by="creation desc",
			as_dict=True,
		)
		or {}
	)

	# Loss reduces qty balance by loss_weight; PCS balance is preserved (loss
	# is recorded by weight, not by piece count).
	pcs_change = 0
	if loss_row.item_code[0] in ("D", "G"):
		pcs_change = -cint(loss_row.pcs or 0)
	mop_log = frappe.new_doc("MOP Log")
	mop_log.item_code = loss_row.item_code
	mop_log.batch_no = loss_row.batch_no
	mop_log.qty_change = -flt(loss_weight, 3)
	mop_log.pcs_change = pcs_change
	for k in (
		"qty_after_transaction",
		"qty_after_transaction_item_based",
		"qty_after_transaction_batch_based",
	):
		mop_log.set(k, flt(latest.get(k) or 0) - flt(loss_weight, 3))
	for k in (
		"pcs_after_transaction",
		"pcs_after_transaction_item_based",
		"pcs_after_transaction_batch_based",
	):
		mop_log.set(k, latest.get(k) or 0)
	# Do not advance flow_index — loss attribution is booked at the same
	# materialization tier as the receive that triggered it.
	mop_log.flow_index = latest.get("flow_index") or 0
	mop_log.from_warehouse = from_wh
	mop_log.to_warehouse = to_wh
	mop_log.voucher_type = "Employee IR"
	mop_log.voucher_no = eir_doc.name
	mop_log.manufacturing_operation = loss_row.manufacturing_operation
	mop_log.manufacturing_work_order = loss_row.manufacturing_work_order
	mop_log.row_name = loss_row.name
	mop_log.is_synced = 1
	# Loss attribution fields (custom_fields/mop_log.json)
	mop_log.log_category = "Loss Attribution"
	mop_log.loss_type = loss_type
	mop_log.loss_weight = flt(loss_weight, 3)
	mop_log.loss_percentage = flt(pct * 100, 4)
	mop_log.loss_source_row = loss_row.name
	mop_log.save()
	return mop_log.name
