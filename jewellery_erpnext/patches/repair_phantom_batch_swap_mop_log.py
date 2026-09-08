"""Re-attribute a return booked against a batch the work order never held.

A ``Material Receive (WORK ORDER)`` fills an empty ``batch_no`` through
``CustomStockEntry.update_batches`` -> ``get_fifo_batches`` -> ``get_auto_batch_nos``,
which is plain **warehouse** FIFO with no work-order awareness. Department WIP
warehouses are shared by every job in the department, so FIFO can land on another job's
metal. The return is then real -- the weight genuinely left -- but it is debited from a
batch this work order was never issued, which writes a negative ``(item, batch)`` balance
and leaves the job's own batches overstated by exactly the returned qty.

Observed on ``MWO-KGJPL-PE00081-001-1-91.75-Y-01``: ``MAT-STE-06205`` returned 0.280 g of
``M-G-22KT-91.75-Y`` against ``KG2F081-MGL229175Y0-P29A8``, a shared casting batch held by
20+ other work orders. The −0.280 was cloned onto ten operations, while the job's own five
batches still summed to 16.236 g against an operator-weighed 15.956 g. Serial Number
Creator drops non-positive rows, so it read 16.720 g where the header read 16.440 g.

**Why this is not a stock correction.** No backdated ledger fix exists: every candidate
``(batch, warehouse)`` drains to exactly 0.000 later, and ``allow_negative_stock = 0``, so
a backdated −qty throws ``NegativeStockError``. Cancelling is worse -- the inward leg
feeds a downstream Repack, and the cancel leg only flips rows matching its own
``voucher_no``, leaving every Employee IR / Department IR / baseline clone untouched and
unregenerable (``update_new_mop_wtg`` is one-shot). Squaring the stock ledger is a
separate, forward-dated decision; this script repairs the MOP Log tier only.

**Append-only.** It never updates or deletes an existing row. Each correction is a NEW row
carrying the delta as ``qty_change`` and the corrected absolute balance, tagged
``row_name = REPAIR_ROW_TAG_BATCH_SWAP``, so history stays intact, the change is reversible
by cancelling the appended rows, and re-runs are no-ops.

**Header-neutral, and it refuses to run if it would not be.** Replacing
``(right_batch, wrong_batch) = (x, -qty)`` with ``(x - qty, 0)`` leaves the RAW sum
unchanged *and* makes the CLAMPED sum equal it -- the stored headers were written before
``clamp_negative_balance`` landed, so they already hold the raw figure. Every operation is
checked against that invariant before anything is written; a mismatch routes the operation
to review rather than guessing.

**It only repairs a matched pair.** ``audit_negative_batch_balances`` refuses to raise a
negative to zero without ``allow_increase`` because that "adds metal no Stock Ledger Entry
ever created". That warning is correct and this script does not bypass it: every ``+qty``
on the wrong batch is paired with a ``-qty`` on the right batch at the SAME operation, so
the operation's total is untouched. There is deliberately no ``allow_increase`` flag here
-- an unpaired leg is refused, never written.

The finished-goods operation is the exception that proves the rule: the FG-MWO seed's
``HAVING SUM(qty_change) > 0`` DROPS a net-negative key rather than applying it, so an FG
operation carries the right batch overstated with no wrong-batch row to pair against. That
shape is accepted only when ``for_fg`` is set.

NOT registered in patches.txt -- run it manually.

Dry run (prints every proposed correction, writes nothing)::

    bench --site gk execute \\
        jewellery_erpnext.patches.repair_phantom_batch_swap_mop_log.execute \\
        --kwargs "{'mwo': 'MWO-KGJPL-PE00081-001-1-91.75-Y-01',
                   'item_code': 'M-G-22KT-91.75-Y',
                   'wrong_batch': 'KG2F081-MGL229175Y0-P29A8',
                   'right_batch': 'KG2F081-MGL229175Y0-12L9U',
                   'qty': 0.28}"

Apply by adding ``'dry_run': False``.

Take a backup first (``bench --site <site> backup --with-files``), and do not run while an
EOD sync is in flight: MOP Log carries the EOD lock validator on ``before_save``, so the
inserts would be rejected mid-window and leave a half-written repair.
"""

import frappe
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	FIELD_MAP,
	get_current_mop_balance_rows,
	get_last_mop_index,
	recalculate_manufacturing_operation_weights,
)
from jewellery_erpnext.mop_lineage_audit import (
	REPAIR_ROW_TAG_BATCH_SWAP,
	negative_balance_findings,
)
from jewellery_erpnext.utils import clamp_negative_balance

# Same constant the sibling header repairs compare with -- every operand is a precision-3
# field, so anything finer compares rounding artefacts. Do not mint a third tolerance.
TOLERANCE = 0.0005

_BALANCE_FIELDS = [
	"item_code",
	"batch_no",
	"qty_after_transaction_batch_based",
	"pcs_after_transaction_batch_based",
	"manufacturing_work_order",
	"from_warehouse",
	"to_warehouse",
]


def _latest_row(mop, item_code, batch_no):
	"""The operation's latest non-cancelled row for one ``(item, batch)`` key."""
	for bal in get_current_mop_balance_rows(
		mop, include_fields=_BALANCE_FIELDS, keys=[(item_code, batch_no)]
	):
		if bal.get("item_code") == item_code and bal.get("batch_no") == batch_no:
			return bal
	return None


def _family_of(item_code):
	return FIELD_MAP.get((item_code or "")[:1])


def _bucket_after(mop, legs):
	"""The clamped weight bucket the recompute will produce once ``legs`` are applied.

	Mirrors ``recalculate_manufacturing_operation_weights`` -- latest row per key,
	bucketed by item-code prefix, each key clamped at zero INDEPENDENTLY -- so the
	invariant check cannot disagree with the writer it is predicting.

	It does NOT apply ``drop_pre_refining_rows``. On an operation behind a Work Order
	Refining cutoff the writer would drop rows this prediction keeps, the two figures
	would differ, and the operation is routed to review instead of written. That is the
	safe direction: a cutoff mismatch costs a manual look, never a bad write.
	"""
	family = _family_of(legs[0]["item_code"])
	overrides = {(leg["item_code"], leg["batch_no"]): leg["expected"] for leg in legs}

	total = 0.0
	for bal in get_current_mop_balance_rows(mop, include_fields=_BALANCE_FIELDS):
		code = bal.get("item_code")
		if _family_of(code) != family:
			continue
		key = (code, bal.get("batch_no"))
		value = (
			overrides[key]
			if key in overrides
			else flt(bal.get("qty_after_transaction_batch_based"))
		)
		total += clamp_negative_balance(value)[0]
	return flt(total, 3)


def _stored_bucket(mop, item_code):
	"""The header bucket this correction must leave untouched."""
	field = f"{_family_of(item_code)}_wt"
	return flt(frappe.db.get_value("Manufacturing Operation", mop, field), 3)


def _plan_operation(mop, item_code, wrong_batch, right_batch, qty, for_fg):
	"""Build the legs for one operation, or explain why it cannot be repaired.

	Returns ``(legs, reason)`` -- exactly one of the two is falsy.
	"""
	wrong_row = _latest_row(mop, item_code, wrong_batch)
	right_row = _latest_row(mop, item_code, right_batch)

	if not wrong_row and not right_row:
		return None, "neither batch present"

	legs = []

	if wrong_row:
		balance = flt(wrong_row.get("qty_after_transaction_batch_based"), 3)
		if abs(balance + qty) > TOLERANCE:
			return None, f"wrong-batch balance is {balance}, expected {flt(-qty, 3)}"
		legs.append(
			{
				"manufacturing_operation": mop,
				"manufacturing_work_order": wrong_row.get("manufacturing_work_order"),
				"item_code": item_code,
				"batch_no": wrong_batch,
				"before": balance,
				"expected": 0.0,
				"expected_pcs": 0,
				"row": wrong_row,
			}
		)

	if right_row:
		balance = flt(right_row.get("qty_after_transaction_batch_based"), 3)
		if balance + TOLERANCE < qty:
			return None, f"right-batch balance {balance} cannot absorb {qty}"
		legs.append(
			{
				"manufacturing_operation": mop,
				"manufacturing_work_order": right_row.get("manufacturing_work_order"),
				"item_code": item_code,
				"batch_no": right_batch,
				"before": balance,
				"expected": flt(balance - qty, 3),
				"expected_pcs": cint(
					right_row.get("pcs_after_transaction_batch_based")
				),
				"row": right_row,
			}
		)

	# The pairing rule. An unpaired +qty invents metal; an unpaired -qty destroys it.
	if len(legs) != 2:
		only = legs[0]["batch_no"] if legs else None
		if not (for_fg and only == right_batch):
			return None, (
				f"unpaired leg on {only} -- only an FG operation may carry the right "
				"batch alone (its seed drops net-negative keys)"
			)

	stored = _stored_bucket(mop, item_code)
	after = _bucket_after(mop, legs)
	if abs(after - stored) > TOLERANCE:
		return None, (
			f"not header-neutral: recompute would write {after}, header holds {stored}"
		)

	for leg in legs:
		leg["delta"] = flt(leg["expected"] - leg["before"], 3)
	return legs, None


def _summarise(corrections):
	"""Return payload without the source rows -- ``bench execute`` prints this."""
	return [
		{
			"mop": c["mop"],
			"legs": [
				{
					"batch_no": leg["batch_no"],
					"before": leg["before"],
					"expected": leg["expected"],
					"delta": leg["delta"],
				}
				for leg in c["legs"]
			],
		}
		for c in corrections
	]


def _append_correction(leg):
	"""Insert ONE correcting MOP Log row. Never mutates an existing row."""
	mop = leg["manufacturing_operation"]
	source = leg["row"]
	correct = flt(leg["expected"], 3)
	pcs = cint(leg["expected_pcs"])

	ml = frappe.new_doc("MOP Log")
	ml.item_code = leg["item_code"]
	ml.batch_no = leg["batch_no"]
	ml.manufacturing_operation = mop
	ml.manufacturing_work_order = leg["manufacturing_work_order"]
	ml.from_warehouse = source.get("from_warehouse")
	ml.to_warehouse = source.get("to_warehouse")
	ml.voucher_type = "Manufacturing Operation"
	ml.voucher_no = mop
	ml.row_name = REPAIR_ROW_TAG_BATCH_SWAP

	ml.qty_change = flt(leg["delta"], 3)
	ml.pcs_change = 0
	# All three tiers land on the corrected absolute balance: they are per-operation
	# views of the same figure and this row IS the operation's new balance.
	ml.qty_after_transaction = correct
	ml.qty_after_transaction_item_based = correct
	ml.qty_after_transaction_batch_based = correct
	ml.pcs_after_transaction = pcs
	ml.pcs_after_transaction_item_based = pcs
	ml.pcs_after_transaction_batch_based = pcs

	ml.is_synced = 0
	ml.is_cancelled = 0
	ml.flow_index = (get_last_mop_index(mop) or 0) + 1
	ml.flags.ignore_permissions = True
	ml.insert(ignore_permissions=True)
	return ml.name


def _resolve_operations(mwo, item_code, wrong_batch):
	"""Operations carrying the phantom, plus the FG operations of the same order.

	The negative keys come from the shared detector so audit and repair cannot disagree.
	The FG operations are added separately because the FG-MWO seed drops the negative
	key outright -- they carry the overstatement with nothing negative to detect, and
	they are the operations the Serial Number Creator actually reads.
	"""
	payload = negative_balance_findings(mwos=[mwo])
	ops = {
		f["manufacturing_operation"]
		for f in payload["findings"]
		if f["item_code"] == item_code and f["batch_no"] == wrong_batch
	}

	fg_ops = set()
	pmo = frappe.db.get_value("Manufacturing Work Order", mwo, "manufacturing_order")
	if pmo:
		fg_mwos = frappe.get_all(
			"Manufacturing Work Order",
			filters={"manufacturing_order": pmo, "for_fg": 1},
			pluck="name",
		)
		if fg_mwos:
			fg_ops = set(
				frappe.get_all(
					"Manufacturing Operation",
					filters={"manufacturing_work_order": ["in", fg_mwos]},
					pluck="name",
				)
			)
	return sorted(ops | fg_ops), fg_ops


def _refuse(message, corrections=None, review=None):
	"""Print a refusal and return it. Never raise -- see the note inside ``execute``."""
	print(f"[repair-batch-swap] REFUSED: {message}")
	return {
		"corrections": _summarise(corrections or []),
		"review": review or [],
		"refused": message,
	}


def execute(
	dry_run=True,
	mwo=None,
	item_code=None,
	wrong_batch=None,
	right_batch=None,
	qty=None,
):
	"""Report, and optionally apply, a paired batch re-attribution across a work order."""
	# Every rejection below PRINTS and returns rather than raising. ``bench execute`` --
	# the invocation this module documents -- wraps the call in
	# ``try: get_attr(method)(...) except Exception: ret = eval(method)``
	# (frappe/commands/utils.py), so ANY exception is swallowed and replaced by
	# ``NameError: name 'jewellery_erpnext' is not defined``. A raise here would make
	# every guard rail invisible at the one surface an operator actually uses, and a
	# refusal indistinguishable from a crash.
	missing = [
		name
		for name, value in (
			("mwo", mwo),
			("item_code", item_code),
			("wrong_batch", wrong_batch),
			("right_batch", right_batch),
			# qty is checked separately: ``not 0`` is True, so a caller passing 0 would
			# be told the argument is missing rather than that it must be positive.
			("qty", None if qty is None else (qty or "0")),
		)
		if not value
	]
	if missing:
		return _refuse(f"missing required argument(s): {', '.join(missing)}")
	if wrong_batch == right_batch:
		return _refuse("wrong_batch and right_batch must differ")

	qty = flt(qty, 3)
	if qty <= 0:
		return _refuse(
			f"qty must be positive (got {qty}) -- it is the weight to move off the phantom"
		)
	if not _family_of(item_code):
		return _refuse(
			f"{item_code} has no weight family (item-code prefix outside FIELD_MAP); "
			"it never reaches a header bucket, so there is nothing to repair"
		)

	operations, fg_ops = _resolve_operations(mwo, item_code, wrong_batch)
	if not operations:
		print(f"[repair-batch-swap] No operations carry {wrong_batch} on {mwo}.")
		return {"corrections": [], "review": []}

	corrections, review = [], []
	for mop in operations:
		legs, reason = _plan_operation(
			mop, item_code, wrong_batch, right_batch, qty, mop in fg_ops
		)
		if legs:
			corrections.append({"mop": mop, "legs": legs})
		elif reason != "neither batch present":
			review.append({"mop": mop, "reason": reason})

	print(
		f"[repair-batch-swap] {len(corrections)} operation(s) repairable, "
		f"{len(review)} needing review."
	)
	for c in corrections:
		for leg in c["legs"]:
			print(
				"  {mop}  {batch}  {before} -> {expected}  (delta {delta})".format(
					mop=c["mop"],
					batch=leg["batch_no"],
					before=leg["before"],
					expected=leg["expected"],
					delta=leg["delta"],
				)
			)
	for r in review:
		print(f"  REVIEW  {r['mop']}  {r['reason']}")

	if dry_run:
		print(
			"[repair-batch-swap] DRY RUN - nothing written. Pass dry_run=False to apply."
		)
		return {"corrections": _summarise(corrections), "review": review}

	if review:
		return _refuse(
			f"{len(review)} operation(s) failed their invariant; refusing a partial "
			"repair. A half-corrected chain is harder to reason about than an "
			"uncorrected one -- resolve the review rows first.",
			corrections=corrections,
			review=review,
		)

	written = []
	for c in corrections:
		# Wrong batch first: MOPLog.validate recomputes the header after every row, so
		# this ordering makes the transient state an overstate rather than an
		# understate. Under the clamped rule the phantom contributes 0 either way.
		for leg in sorted(c["legs"], key=lambda leg: leg["batch_no"] != wrong_batch):
			written.append(_append_correction(leg))

	for c in corrections:
		# The FG header is force-written MWO-wide by sync_mwo_weights, not by its own
		# ledger, so this loop leaves it to the sync below. (MOPLog.validate still ran
		# a narrowed recompute when the row was inserted; that is harmless precisely
		# because the invariant above proved the FG ledger now agrees with the header.)
		if c["mop"] in fg_ops:
			continue
		recalculate_manufacturing_operation_weights(
			c["mop"], prefixes=(_family_of(item_code),)
		)

	for fg_mwo in sorted(
		{
			frappe.db.get_value(
				"Manufacturing Operation", mop, "manufacturing_work_order"
			)
			for mop in fg_ops
		}
		- {None}
	):
		frappe.get_doc("Manufacturing Work Order", fg_mwo).sync_mwo_weights()

	frappe.db.commit()
	print(f"[repair-batch-swap] Wrote {len(written)} correcting MOP Log row(s).")
	return {
		"corrections": _summarise(corrections),
		"review": review,
		"written": written,
	}
