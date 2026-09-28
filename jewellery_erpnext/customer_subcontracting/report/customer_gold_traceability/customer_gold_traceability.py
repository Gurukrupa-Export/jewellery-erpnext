# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Customer Gold Traceability -- receipt by receipt: where the gold is, what left, what is owed.

Four views over one read-only replay (``customer_gold_trace``):

* **Receipt Settlement** -- one row per receipt row: received, delivered as FG, returned raw,
  lost, still held (by stage), and, for users who may see money, the nominal amount, what the
  settlement vouchers released against THIS receipt, and what is still pending.
* **Material Position** -- where each receipt's metal physically is now: batch, warehouse,
  stage, its share of the holding, and how much of the holding is free.
* **Movements** -- the receipt's trail through transfers, conversions and production.
* **FG Valuation** -- one Manufacture entry, row by row, from consumed amounts to the finished
  piece's value, beside the running rate the Stock Ledger report shows.

The existing Subcontracting Report is untouched; this answers a different question.

Nothing here writes. Physical figures come from the stock ledger via the replay; money comes
from ``Customer Gold Allocation`` where it exists and is otherwise derived and labelled so.
Physical and financial closure are separate columns because they are separate facts.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, getdate

from jewellery_erpnext.customer_subcontracting.customer_gold_trace import (
	DISPOSITION_DELIVERED,
	DISPOSITION_LOSS,
	DISPOSITION_OTHER,
	DISPOSITION_RETURNED,
	SERIAL_PREFIX,
	stage_of,
	trace,
)

VIEW_SETTLEMENT = "Receipt Settlement"
VIEW_POSITION = "Material Position"
VIEW_MOVEMENTS = "Movements"
VIEW_FG = "FG Valuation"
VIEWS = (VIEW_SETTLEMENT, VIEW_POSITION, VIEW_MOVEMENTS, VIEW_FG)

#: Roles that may see nominal rates and amounts. Everyone the report is shared with sees
#: quantities; money is withheld server-side, so an export or an API call obeys it too.
MONEY_ROLES = ("System Manager", "Accounts Manager", "Accounts User")

QTY_TOLERANCE = 0.0005
AMOUNT_TOLERANCE = 0.01


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_validate_filters(filters)
	show_money = can_see_money()
	view = filters.view or VIEW_SETTLEMENT

	if view == VIEW_FG:
		return _fg_valuation(filters, show_money)

	receipts, replay, _scope = trace(
		filters.company,
		customer=filters.customer,
		receipt=filters.receipt,
		to_datetime=_cutoff(filters),
	)
	receipts = _filter_receipts(receipts, filters, replay)

	if view == VIEW_POSITION:
		return _position_columns(), _position_rows(receipts, replay, filters)
	if view == VIEW_MOVEMENTS:
		return _movement_columns(), _movement_rows(receipts, replay, filters)

	rows = _settlement_rows(receipts, replay, filters, show_money)
	return (
		_settlement_columns(show_money),
		rows,
		None,
		None,
		_settlement_summary(rows, show_money),
	)


def can_see_money(user=None):
	return bool(set(frappe.get_roles(user)) & set(MONEY_ROLES))


def _validate_filters(filters):
	if not filters.get("company"):
		frappe.throw(_("Company is required."))
	if not frappe.has_permission("Company", doc=filters.company):
		frappe.throw(
			_("Not permitted for company {0}.").format(filters.company),
			frappe.PermissionError,
		)
	if filters.get("view") and filters.view not in VIEWS:
		frappe.throw(_("Unknown view {0}.").format(filters.view))
	if filters.get("view") == VIEW_FG and not (
		filters.get("stock_entry") or filters.get("serial_number_creator")
	):
		frappe.throw(
			_(
				"FG Valuation needs a Manufacture Stock Entry or a Serial Number Creator."
			)
		)
	if filters.get("from_date") and filters.get("to_date"):
		if getdate(filters.from_date) > getdate(filters.to_date):
			frappe.throw(_("From Date cannot be after To Date."))


def _cutoff(filters):
	return (
		get_datetime(f"{filters.to_date} 23:59:59.999999")
		if filters.get("to_date")
		else None
	)


def _filter_receipts(receipts, filters, replay):
	result = []
	cutoff = getdate(filters.to_date) if filters.get("to_date") else None
	scoped_batch_keys = None
	if filters.get("batch"):
		scoped_batch_keys = {
			key
			for batch_no, _warehouse, _qty, shares in replay.positions()
			if batch_no == filters.batch
			for key in shares
		} | {r.key for r in receipts if r.batch_no == filters.batch}
		scoped_batch_keys |= {
			step["receipt_key"]
			for step in replay.trail
			if step["batch_no"] == filters.batch
		}
	for receipt in receipts:
		if filters.get("receipt") and receipt.reference_docname != filters.receipt:
			continue
		if filters.get("receipt_row") and receipt.cg_source_row != filters.receipt_row:
			continue
		if filters.get("item_code") and receipt.item_code != filters.item_code:
			continue
		if cutoff and getdate(receipt.posting_date) > cutoff:
			continue
		if scoped_batch_keys is not None and receipt.key not in scoped_batch_keys:
			continue
		result.append(receipt)
	return result


# ------------------------------------------------------------------------------------------
# Receipt Settlement
# ------------------------------------------------------------------------------------------


def _settlement_columns(show_money):
	columns = [
		_col("receipt", _("Receipt"), "Link", "Stock Entry", 170),
		_col("receipt_row", _("Receipt Row"), "Data", width=95),
		_col("posting_date", _("Receipt Date"), "Date", width=100),
		_col("customer", _("Customer"), "Link", "Customer", 120),
		_col("item_code", _("Item"), "Link", "Item", 160),
		_col("receipt_batch", _("Receipt Batch"), "Link", "Batch", 190),
		_col("measure", _("Measured In"), "Data", width=90),
		_col("received", _("Received"), "Float", width=95),
		_col("delivered", _("Delivered in FG"), "Float", width=110),
		_col("returned", _("Returned Raw"), "Float", width=100),
		_col("owed_back", _("Still Owed"), "Float", width=95),
		_col("held_rm", _("Held: RM / Transit"), "Float", width=120),
		_col("held_wip", _("Held: WIP"), "Float", width=90),
		_col("held_fg", _("Held: FG"), "Float", width=90),
		_col("held_scrap", _("Held: Loss / Scrap"), "Float", width=120),
		_col("loss", _("Consumed as Loss"), "Float", width=115),
		_col("unexplained", _("Other / Unexplained"), "Float", width=130),
		_col("material_status", _("Material Status"), "Data", width=150),
	]
	if show_money:
		columns += [
			_col(
				"booked_rate",
				_("Booked Rate (per receipt unit)"),
				"Currency",
				width=150,
			),
			_col("nominal_amount", _("Nominal Amount"), "Currency", width=130),
			_col("revaluation", _("Revaluation"), "Currency", width=110),
			_col("released_fg", _("Released by FG Delivery"), "Currency", width=160),
			_col("released_raw", _("Released by Raw Return"), "Currency", width=160),
			_col("pending_amount", _("Pending Liability"), "Currency", width=130),
			_col("settlement_vouchers", _("Settlement Vouchers"), "Data", width=220),
			_col("financial_status", _("Financial Status"), "Data", width=170),
			_col("basis", _("Money Basis"), "Data", width=120),
		]
	columns.append(_col("flags", _("Flags"), "Data", width=260))
	return columns


def _settlement_rows(receipts, replay, filters, show_money):
	held = _held_by_stage(replay)
	serial_held = _serial_only_holdings(replay)
	money = _money_by_receipt(receipts, replay, filters) if show_money else {}
	outliers = _rate_outliers(receipts) if show_money else {}

	rows = []
	for receipt in receipts:
		key = receipt.key
		unit = "Fine g" if receipt.unit == "fine" else (receipt.stock_uom or "Qty")
		dispositions = replay.dispositions.get(key, {})
		received = flt(replay.received.get(key))
		delivered = flt(dispositions.get(DISPOSITION_DELIVERED))
		returned = flt(dispositions.get(DISPOSITION_RETURNED))
		stages = held.get(key, {})
		owed = received - delivered - returned
		row = {
			"receipt": receipt.reference_docname,
			"receipt_row": receipt.cg_source_row,
			"posting_date": receipt.posting_date,
			"customer": receipt.customer,
			"item_code": receipt.item_code,
			"receipt_batch": receipt.batch_no,
			"measure": unit,
			"received": _q(received),
			"delivered": _q(delivered),
			"returned": _q(returned),
			"owed_back": _q(owed),
			"held_rm": _q(stages.get("RM", 0.0) + stages.get("Transit", 0.0)),
			"held_wip": _q(stages.get("WIP", 0.0)),
			"held_fg": _q(stages.get("FG", 0.0)),
			"held_scrap": _q(
				stages.get("Loss / Scrap", 0.0) + stages.get("Recoverable Scrap", 0.0)
			),
			"loss": _q(dispositions.get(DISPOSITION_LOSS)),
			"unexplained": _q(dispositions.get(DISPOSITION_OTHER)),
			"material_status": _material_status(
				received, delivered, returned, sum(stages.values())
			),
		}
		flags = []
		if key in outliers:
			flags.append(outliers[key])
		serial_only = sum(
			amount
			for (batch_no, _warehouse), amount in serial_held.get(key, {}).items()
		)
		if serial_only > QTY_TOLERANCE:
			flags.append(
				_(
					"{0} sits in finished pieces with no batch -- their delivery will not "
					"release this receipt's liability (R-2)"
				).format(_q(serial_only))
			)
		if abs(replay.balance(key)) > QTY_TOLERANCE:
			flags.append(
				_("trace does not balance by {0}").format(_q(replay.balance(key)))
			)
		if show_money:
			m = money.get(receipt.name) or frappe._dict()
			row.update(
				{
					"booked_rate": m.booked_rate,
					"nominal_amount": m.nominal,
					"revaluation": m.revaluation,
					"released_fg": m.released_fg,
					"released_raw": m.released_raw,
					"pending_amount": m.pending,
					"settlement_vouchers": ", ".join(sorted(m.vouchers or [])),
					"financial_status": _financial_status(m, owed),
					"basis": m.basis,
				}
			)
			flags += m.flags or []
		row["flags"] = "; ".join(flags)
		rows.append(row)
	return rows


def _material_status(received, delivered, returned, held):
	owed = received - delivered - returned
	if received <= QTY_TOLERANCE:
		return _("Nothing received")
	if owed <= QTY_TOLERANCE:
		return _("Closed")
	if held <= QTY_TOLERANCE:
		return _("Owed, no metal held")
	if delivered + returned > QTY_TOLERANCE:
		return _("Partly disposed")
	return _("Open")


def _financial_status(money, owed):
	if money.nominal is None:
		return _("Not valued")
	if (
		abs(flt(money.nominal)) <= AMOUNT_TOLERANCE
		and abs(flt(money.revaluation)) <= AMOUNT_TOLERANCE
	):
		# A stone typed at 0: nothing was booked, so there is nothing to settle -- "Settled" would
		# claim a settlement that never happened.
		return _("Nothing booked")
	pending = flt(money.pending)
	if abs(pending) <= AMOUNT_TOLERANCE:
		status = _("Settled")
	elif owed <= QTY_TOLERANCE:
		status = _("Material closed, settlement pending")
	elif flt(money.released_fg) + flt(money.released_raw) > AMOUNT_TOLERANCE:
		status = _("Partly settled")
	else:
		status = _("Open")
	if money.basis and money.basis != _("Allocated"):
		status += " ({0})".format(money.basis.lower())
	return status


def _serial_only_holdings(replay):
	"""{key: {(holding, warehouse): share}} for pieces minted with a Serial No and no batch."""
	result = defaultdict(dict)
	for batch_no, warehouse, _qty, shares in replay.positions():
		if str(batch_no).startswith(SERIAL_PREFIX):
			for key, amount in shares.items():
				result[key][(batch_no, warehouse)] = amount
	return result


def _held_by_stage(replay):
	held = defaultdict(lambda: defaultdict(float))
	for batch_no, warehouse, _qty, shares in replay.positions():
		stage = stage_of(warehouse, batch_no, replay)
		for key, amount in shares.items():
			held[key][stage] += amount
	return held


def _rate_outliers(receipts):
	"""{receipt key: reason} for receipts whose booked per-gram rate is outside the band the
	receipt rate check uses, against an INDEPENDENT reference: the company's latest purchase of
	the item, else the feed's own rate on an earlier day (``customer_gold_rate.reference_rate``).

	Never the customer's other receipts: on kg-gk seven of eleven were booked at ten times the
	per-gram rate, so their median IS the error and a peer comparison flags the correct ones.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_rate import (
		is_outlier,
		rate_ratio,
		reference_rate,
	)
	from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
		get_customer_gold_settings,
	)

	settings = get_customer_gold_settings()
	flagged = {}
	for receipt in receipts:
		if not receipt.cg_currency or receipt.unit != "fine":
			continue
		qty = flt(receipt.cg_gross_qty_delta)
		if qty <= 0 or flt(receipt.cg_carrying_value_delta) <= 0:
			continue
		per_gram = flt(receipt.cg_carrying_value_delta) / qty
		try:
			reference = reference_rate(
				receipt.item_code, receipt.company, receipt.posting_date, settings
			)
		except Exception:
			reference = None
		ratio = rate_ratio(per_gram, reference)
		if is_outlier(ratio):
			flagged[receipt.key] = _(
				"booked {0}/g is {1}x the reference {2} ({3})"
			).format(
				flt(per_gram, 2),
				flt(ratio, 2),
				flt(reference.rate, 2),
				reference.source,
			)
	return flagged


def _money_by_receipt(receipts, replay, filters):
	"""Nominal, revaluation and released amounts per receipt event.

	Released comes from ``Customer Gold Allocation`` for every disposition that has rows. A
	legacy disposition -- written before allocations existed -- is split across receipts by the
	replay's attribution of the same voucher row, priced at each receipt's own booked rate and
	scaled to the event's recorded value, and the row says ``derived``.
	"""
	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		ALLOCATION_DOCTYPE,
		DISPOSITION_RAW_RETURN,
		DISPOSITION_REVERSAL,
		allocated_events,
		booked_rate_of,
		is_allocation_schema_ready,
	)

	by_event = {r.name: r for r in receipts}
	result = {}
	for receipt in receipts:
		booked = booked_rate_of(receipt)
		result[receipt.name] = frappe._dict(
			booked_rate=booked,
			nominal=flt(receipt.cg_carrying_value_delta)
			if receipt.cg_currency
			else None,
			revaluation=0.0,
			released_fg=0.0,
			released_raw=0.0,
			vouchers=set(),
			flags=[],
			basis=_("Allocated"),
		)
	if not receipts:
		return result

	cutoff = _cutoff(filters)
	company = receipts[0].company
	customers = list({r.customer for r in receipts})

	if is_allocation_schema_ready():
		allocations = frappe.get_all(
			ALLOCATION_DOCTYPE,
			filters={"receipt_event": ["in", list(by_event)]},
			fields=[
				"name",
				"receipt_event",
				"disposition",
				"reversal_of",
				"reference_doctype",
				"reference_docname",
				"settlement_voucher",
				"amount",
				"recorded_at",
			],
		)
		original_disposition = {a.name: a.disposition for a in allocations}
		posting = _posting_dates(allocations)
		for a in allocations:
			if (
				cutoff
				and posting.get((a.reference_doctype, a.reference_docname), cutoff)
				> cutoff
			):
				continue
			m = result[a.receipt_event]
			disposition = (
				original_disposition.get(a.reversal_of, a.disposition)
				if a.disposition == DISPOSITION_REVERSAL
				else a.disposition
			)
			if disposition == DISPOSITION_RAW_RETURN:
				m.released_raw += flt(a.amount)
				m.vouchers.add(a.reference_docname)
			else:
				m.released_fg += flt(a.amount)
				if a.settlement_voucher:
					m.vouchers.add(a.settlement_voucher)
		_flag_cancelled_vouchers(result, allocations)

	_legacy_releases(
		result, receipts, replay, allocated_events(), company, customers, cutoff
	)
	_revaluations(result, receipts, replay, company, customers, cutoff)

	for m in result.values():
		if m.nominal is None:
			m.pending = None
			continue
		m.pending = flt(m.nominal + m.revaluation - m.released_fg - m.released_raw, 2)
		m.revaluation = flt(m.revaluation, 2)
		m.released_fg = flt(m.released_fg, 2)
		m.released_raw = flt(m.released_raw, 2)
	return result


def _posting_dates(allocations):
	by_type = defaultdict(set)
	for a in allocations:
		if a.reference_doctype and a.reference_docname:
			by_type[a.reference_doctype].add(a.reference_docname)
	dates = {}
	for doctype, names in by_type.items():
		for row in frappe.get_all(
			doctype,
			filters={"name": ["in", list(names)]},
			fields=["name", "posting_date", "posting_time"],
		):
			dates[(doctype, row.name)] = get_datetime(
				f"{row.posting_date} {row.posting_time or '00:00:00'}"
			)
	return dates


def _flag_cancelled_vouchers(result, allocations):
	reversed_ = {a.reversal_of for a in allocations if a.reversal_of}
	live_jes = {
		a.settlement_voucher
		for a in allocations
		if a.settlement_voucher and a.name not in reversed_
	}
	if not live_jes:
		return
	cancelled = set(
		frappe.get_all(
			"Journal Entry",
			filters={"name": ["in", list(live_jes)], "docstatus": 2},
			pluck="name",
		)
	)
	for a in allocations:
		if a.settlement_voucher in cancelled and a.name not in reversed_:
			result[a.receipt_event].flags.append(
				_("settlement {0} is cancelled but its allocation stands").format(
					a.settlement_voucher
				)
			)


def _legacy_releases(result, receipts, replay, allocated, company, customers, cutoff):
	from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
		booked_rate_of,
	)
	from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
		EVENT_DELIVERY,
		EVENT_DELIVERY_RETURN,
		EVENT_RETURN,
		EVENT_REVERSAL,
		LEDGER_DOCTYPE,
	)

	events = frappe.get_all(
		LEDGER_DOCTYPE,
		filters={
			"company": company,
			"customer": ["in", customers],
			"cg_event_kind": [
				"in",
				[EVENT_DELIVERY, EVENT_DELIVERY_RETURN, EVENT_RETURN],
			],
			"cg_currency": ["is", "set"],
		},
		fields=[
			"name",
			"cg_event_kind",
			"reference_doctype",
			"reference_docname",
			"cg_source_row",
			"cg_carrying_value_delta",
			"cg_settlement_voucher",
		],
	)
	events = [e for e in events if e.name not in allocated]
	if not events:
		return
	reversed_ = set(
		frappe.get_all(
			LEDGER_DOCTYPE,
			filters={
				"cg_event_kind": EVENT_REVERSAL,
				"cg_reversal_of": ["in", [e.name for e in events]],
			},
			pluck="cg_reversal_of",
		)
	)
	by_key = {r.key: r for r in receipts}
	posting = _posting_dates(events)
	for event in events:
		if event.name in reversed_:
			continue
		if (
			cutoff
			and posting.get((event.reference_doctype, event.reference_docname), cutoff)
			> cutoff
		):
			continue
		shares = defaultdict(float)
		for step in replay.trail:
			if (
				step["voucher_no"] == event.reference_docname
				and step["detail_no"] == event.cg_source_row
				and step["action"] in ("Delivered", "Returned", "Delivery Return")
				and step["receipt_key"] in by_key
			):
				shares[step["receipt_key"]] += abs(step["amount"])
		if not shares:
			continue
		weights = {}
		for key, amount in shares.items():
			receipt = by_key[key]
			rate = booked_rate_of(receipt)
			per_measure = (
				rate / (flt(receipt.purity) / 100.0)
				if rate and receipt.unit == "fine"
				else rate
			)
			weights[key] = amount * flt(per_measure)
		total_weight = sum(weights.values())
		released = -flt(event.cg_carrying_value_delta)
		for key, weight in weights.items():
			receipt = by_key[key]
			if receipt.name not in result:
				continue
			m = result[receipt.name]
			part = released * (weight / total_weight) if total_weight else 0.0
			if event.cg_event_kind == EVENT_RETURN:
				m.released_raw += part
				m.vouchers.add(event.reference_docname)
			else:
				m.released_fg += part
				if event.cg_settlement_voucher:
					m.vouchers.add(event.cg_settlement_voucher)
			m.basis = _("Derived")


def _revaluations(result, receipts, replay, company, customers, cutoff):
	from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
		EVENT_REVALUATION,
		EVENT_REVERSAL,
		LEDGER_DOCTYPE,
	)

	events = frappe.get_all(
		LEDGER_DOCTYPE,
		filters={
			"company": company,
			"customer": ["in", customers],
			"cg_event_kind": EVENT_REVALUATION,
		},
		fields=[
			"name",
			"reference_doctype",
			"reference_docname",
			"batch_no",
			"cg_carrying_value_delta",
		],
	)
	if not events:
		return
	reversed_ = set(
		frappe.get_all(
			LEDGER_DOCTYPE,
			filters={
				"cg_event_kind": EVENT_REVERSAL,
				"cg_reversal_of": ["in", [e.name for e in events]],
			},
			pluck="cg_reversal_of",
		)
	)
	by_key = {r.key: r for r in receipts}
	posting = _posting_dates(events)
	for event in events:
		if event.name in reversed_:
			continue
		if (
			cutoff
			and posting.get((event.reference_doctype, event.reference_docname), cutoff)
			> cutoff
		):
			continue
		fractions = (
			replay.revaluation_fractions.get(event.reference_docname) or {}
		).get(event.batch_no)
		if not fractions:
			continue
		total = sum(fractions.values())
		for key, fraction in fractions.items():
			receipt = by_key.get(key)
			if receipt and receipt.name in result and total:
				result[receipt.name].revaluation += (
					flt(event.cg_carrying_value_delta) * fraction / total
				)


def _settlement_summary(rows, show_money):
	fine_rows = [r for r in rows if r["measure"] == "Fine g"]
	summary = [
		{"label": _("Receipt rows"), "value": len(rows), "datatype": "Int"},
		{
			"label": _("Received (fine g)"),
			"value": _q(sum(r["received"] for r in fine_rows)),
			"datatype": "Float",
		},
		{
			"label": _("Still owed (fine g)"),
			"value": _q(sum(r["owed_back"] for r in fine_rows)),
			"datatype": "Float",
		},
	]
	if show_money:
		summary.append(
			{
				"label": _("Pending liability"),
				"value": flt(sum(flt(r.get("pending_amount")) for r in rows), 2),
				"datatype": "Currency",
			}
		)
	return summary


# ------------------------------------------------------------------------------------------
# Material Position
# ------------------------------------------------------------------------------------------


def _position_columns():
	return [
		_col("receipt", _("Receipt"), "Link", "Stock Entry", 170),
		_col("receipt_row", _("Receipt Row"), "Data", width=95),
		_col("batch_no", _("Batch"), "Link", "Batch", 200),
		_col("serial_no", _("Serial No (no batch)"), "Link", "Serial No", 150),
		_col("item_code", _("Item"), "Link", "Item", 160),
		_col("generation", _("Generation"), "Int", width=90),
		_col("produced_by", _("Produced By"), "Link", "Stock Entry", 150),
		_col("warehouse", _("Warehouse"), "Link", "Warehouse", 190),
		_col("stage", _("Stage"), "Data", width=100),
		_col("holding_qty", _("Holding Qty"), "Float", width=100),
		_col("free_qty", _("Free (unreserved)"), "Float", width=120),
		_col("measure", _("Measured In"), "Data", width=90),
		_col("receipt_share", _("Receipt's Share"), "Float", width=120),
		_col("receipt_equivalent", _("Receipt-Item Equivalent"), "Float", width=160),
	]


def _position_rows(receipts, replay, filters):
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	by_key = {r.key: r for r in receipts}
	items = {}
	rows = []
	for batch_no, warehouse, qty, shares in sorted(replay.positions()):
		if filters.get("warehouse") and warehouse != filters.warehouse:
			continue
		for key, share in shares.items():
			receipt = by_key.get(key)
			if not receipt:
				continue
			serial_no = _serial_of(batch_no)
			if batch_no not in items:
				items[batch_no] = (
					frappe.db.get_value("Serial No", serial_no, "item_code")
					if serial_no
					else frappe.db.get_value("Batch", batch_no, "item")
				)
			origin = replay.batch_origin.get(batch_no) or (None, None, None)
			rows.append(
				{
					"receipt": receipt.reference_docname,
					"receipt_row": receipt.cg_source_row,
					"batch_no": None if serial_no else batch_no,
					"serial_no": serial_no,
					"item_code": items[batch_no],
					"generation": origin[1],
					"produced_by": origin[0] if origin[1] else None,
					"warehouse": warehouse,
					"stage": stage_of(warehouse, batch_no, replay),
					"holding_qty": _q(qty),
					"free_qty": _q(
						qty
						if serial_no
						else min(
							flt(get_batch_qty(batch_no=batch_no, warehouse=warehouse)),
							qty,
						)
					),
					"measure": "Fine g"
					if receipt.unit == "fine"
					else (receipt.stock_uom or "Qty"),
					"receipt_share": _q(share),
					"receipt_equivalent": _q(_receipt_equivalent(receipt, share)),
				}
			)
	return rows


def _serial_of(holding):
	holding = str(holding or "")
	return holding[len(SERIAL_PREFIX) :] if holding.startswith(SERIAL_PREFIX) else None


def _receipt_equivalent(receipt, share):
	"""A share expressed in the receipt item's own unit, at the fine-per-unit it recorded."""
	if receipt.unit != "fine":
		return share
	per_unit = flt(receipt.get("per_unit")) or flt(receipt.purity) / 100.0
	return share / per_unit if per_unit else 0.0


# ------------------------------------------------------------------------------------------
# Movements
# ------------------------------------------------------------------------------------------


def _movement_columns():
	return [
		_col("posting", _("Posted At"), "Datetime", width=160),
		_col("receipt", _("Receipt"), "Link", "Stock Entry", 170),
		_col("receipt_row", _("Receipt Row"), "Data", width=95),
		_col("action", _("Movement"), "Data", width=120),
		_col("voucher_type", _("Voucher Type"), "Link", "DocType", 120),
		_col("voucher_no", _("Voucher"), "Dynamic Link", "voucher_type", 170),
		_col("batch_no", _("Batch"), "Link", "Batch", 200),
		_col("serial_no", _("Serial No (no batch)"), "Link", "Serial No", 150),
		_col("item_code", _("Item"), "Link", "Item", 160),
		_col("from_warehouse", _("From"), "Link", "Warehouse", 170),
		_col("to_warehouse", _("To"), "Link", "Warehouse", 170),
		_col("voucher_qty", _("Voucher Row Qty"), "Float", width=120),
		_col("measure", _("Measured In"), "Data", width=90),
		_col("receipt_share", _("Receipt's Share Moved"), "Float", width=150),
	]


def _movement_rows(receipts, replay, filters):
	by_key = {r.key: r for r in receipts}
	start = (
		get_datetime(f"{filters.from_date} 00:00:00")
		if filters.get("from_date")
		else None
	)
	rows = []
	for step in replay.trail:
		receipt = by_key.get(step["receipt_key"])
		if not receipt:
			continue
		if start and step["posting"] and step["posting"] < start:
			continue
		if filters.get("warehouse") and filters.warehouse not in (
			step["from_warehouse"],
			step["to_warehouse"],
		):
			continue
		if filters.get("batch") and step["batch_no"] != filters.batch:
			continue
		rows.append(
			{
				"posting": step["posting"],
				"receipt": receipt.reference_docname,
				"receipt_row": receipt.cg_source_row,
				"action": step["action"],
				"voucher_type": step["voucher_type"],
				"voucher_no": step["voucher_no"],
				"batch_no": None if _serial_of(step["batch_no"]) else step["batch_no"],
				"serial_no": _serial_of(step["batch_no"]),
				"item_code": step["item_code"],
				"from_warehouse": step["from_warehouse"],
				"to_warehouse": step["to_warehouse"],
				"voucher_qty": _q(step["qty"]),
				"measure": "Fine g"
				if receipt.unit == "fine"
				else (receipt.stock_uom or "Qty"),
				"receipt_share": _q(step["amount"]),
			}
		)
	return rows


# ------------------------------------------------------------------------------------------
# FG Valuation
# ------------------------------------------------------------------------------------------


def _fg_valuation(filters, show_money):
	"""A Manufacture entry from consumed amounts to the finished piece, row by row.

	The two numbers users compare and should not: a row's own rate (what this voucher consumed
	or produced at) and the Stock Ledger report's running valuation rate (the warehouse's average
	after the voucher, across every other piece and batch in it).
	"""
	name = filters.get("stock_entry")
	if not name and filters.get("serial_number_creator"):
		name = frappe.db.get_value(
			"Stock Entry",
			{
				"custom_serial_number_creator": filters.serial_number_creator,
				"purpose": "Manufacture",
				"docstatus": 1,
			},
			"name",
		)
	if not name:
		frappe.throw(_("No submitted Manufacture Stock Entry found for the selection."))

	entry = frappe.get_doc("Stock Entry", name)
	entry.check_permission("read")
	if entry.company != filters.company:
		frappe.throw(_("{0} belongs to another company.").format(name))

	sle = {
		row.voucher_detail_no: row
		for row in frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": name, "is_cancelled": 0},
			fields=[
				"voucher_detail_no",
				"stock_value_difference",
				"valuation_rate",
				"qty_after_transaction",
			],
		)
	}

	columns = [
		_col("idx", _("Row"), "Int", width=60),
		_col("side", _("Side"), "Data", width=90),
		_col("item_code", _("Item"), "Link", "Item", 190),
		_col("batch_no", _("Batch"), "Link", "Batch", 200),
		_col("warehouse", _("Warehouse"), "Link", "Warehouse", 170),
		_col("owner", _("Owner"), "Data", width=170),
		_col("qty", _("Stock Qty"), "Float", width=90),
		_col("uom", _("UOM"), "Link", "UOM", 70),
	]
	if show_money:
		columns += [
			_col("basic_rate", _("Row Rate"), "Currency", width=110),
			_col("basic_amount", _("Row Amount"), "Currency", width=120),
			_col("additional_cost", _("Additional Cost"), "Currency", width=120),
			_col("valuation_amount", _("Valuation Amount"), "Currency", width=130),
			_col("ledger_value", _("Ledger Value Change"), "Currency", width=140),
			_col("running_rate", _("Stock Ledger Running Rate"), "Currency", width=170),
		]
	columns.append(_col("note", _("Note"), "Data", width=300))

	rows = []
	consumed = 0.0
	consumed_by_owner = defaultdict(float)
	produced = 0.0
	for item in entry.items:
		side = _("Consumed") if item.s_warehouse else _("Produced")
		owner = (
			f"{item.get('inventory_type')}: {item.get('customer')}"
			if item.get("customer")
			else (item.get("inventory_type") or "Regular Stock")
		)
		ledger = sle.get(item.name) or frappe._dict()
		row = {
			"idx": item.idx,
			"side": side,
			"item_code": item.item_code,
			"batch_no": item.batch_no,
			"warehouse": item.s_warehouse or item.t_warehouse,
			"owner": owner,
			"qty": _q(item.transfer_qty),
			"uom": item.stock_uom,
			"note": "",
		}
		if item.s_warehouse:
			consumed += flt(item.basic_amount)
			consumed_by_owner[owner] += flt(item.basic_amount)
		else:
			produced += flt(item.amount)
		if show_money:
			row.update(
				{
					"basic_rate": flt(item.basic_rate, 4),
					"basic_amount": flt(item.basic_amount, 2),
					"additional_cost": flt(item.additional_cost, 2),
					"valuation_amount": flt(item.amount, 2),
					"ledger_value": flt(ledger.stock_value_difference, 2),
					"running_rate": flt(ledger.valuation_rate, 4),
				}
			)
			if (
				ledger
				and abs(flt(ledger.valuation_rate) - flt(item.valuation_rate)) > 0.01
			):
				row["note"] = _(
					"Running rate is the warehouse average after this entry ({0} units held), "
					"not this row's rate."
				).format(_q(ledger.qty_after_transaction))
		rows.append(row)

	additional = sum(flt(c.amount) for c in entry.get("additional_costs") or [])
	if show_money:
		for owner, amount in sorted(consumed_by_owner.items()):
			rows.append(_bridge_row(_("Consumed: {0}").format(owner), amount))
		rows.append(_bridge_row(_("Consumed materials, total"), consumed))
		rows.append(_bridge_row(_("Additional costs"), additional))
		rows.append(
			_bridge_row(
				_("Expected FG value (consumed + additional)"), consumed + additional
			)
		)
		rows.append(_bridge_row(_("Posted FG value"), produced))
		rows.append(_bridge_row(_("Difference"), produced - consumed - additional))
	return columns, rows


def _bridge_row(label, amount):
	return {
		"side": _("Bridge"),
		"item_code": None,
		"note": label,
		"valuation_amount": flt(amount, 2),
	}


# ------------------------------------------------------------------------------------------


def _col(fieldname, label, fieldtype, options=None, width=120):
	column = {
		"fieldname": fieldname,
		"label": label,
		"fieldtype": fieldtype,
		"width": width,
	}
	if options:
		column["options"] = options
	return column


def _q(value):
	return flt(value, 3)
