"""Supplier-wise pure metal weight limit -- ``Supplier.custom_metal_weight_limit``.

A supplier (subcontractor, hallmarking / certification centre) may hold at most
``custom_metal_weight_limit`` grams of PURE metal across every warehouse linked to it
(``Warehouse.subcontractor``). Pure weight is ``stock qty x purity % / 100``, summed over all
metal and finding items whatever their touch, so 60 g @ 90 + 40 g @ 80 + 20 g @ 70 = 100 g pure.

Default: a blank / 0 limit means no limit -- the guard costs two ``bulk_map`` reads and returns.

Rule (spec "Projected = Current + Incoming - Removed"): per supplier, the entry's NET pure delta
into that supplier's warehouses is computed; only a positive delta can breach, so a move between
two of the same supplier's warehouses (delta 0) or a return out of an over-limit supplier always
passes.

Two entry points, because one MWO path posts no Stock Entry when the metal leaves:

* **Stock Entry** ``before_submit`` -- every purpose (Material Transfer, Repack, Material Issue
  for Hallmarking / Certification, Receipt, the MWO / Main Slip / Metal Conversion / Refining
  transfers, ...). :func:`lock_supplier_limits` runs BEFORE ``prelock_bins`` and
  :func:`validate_supplier_metal_limit` AFTER it. EOD MOP sync entries are waived: their metal was
  checked when the Employee IR issued it, and refusing them would strand the work order's MOP Logs
  and fail again every night.
* **Employee IR Issue with subcontracting** -- it only writes unsynced MOP Logs; the EOD sync moves
  the stock later. :func:`validate_employee_ir_supplier_limit` prices the operations' current
  balances (the very rows the Issue clones).

Both paths measure the supplier's holding the same way: its Bins PLUS the net pure grams its
pending, not-yet-synced MOP Logs will still move in / out at the next EOD sync
(:func:`pending_sync_pure`). So neither an Issue nor a Stock Entry can pass against pre-sync Bins
that an earlier same-day Issue has already promised away, and an unsynced Receive back frees room.
Stock Entries write their own MOP Logs ``is_synced=1``, so nothing in the Bins is counted twice.

Locking (``lock_order``: parent control row -> Series -> Bin):

* Stock Entry: the Supplier rows are taken ``FOR UPDATE`` in sorted order before ``prelock_bins``
  (the "parent control row" slot), so two entries raising the same supplier serialise -- including
  ones that add an item the supplier has never held (no Bin yet to lock). The supplier's existing
  metal Bins are then added to ``prelock_bins``' single sorted batch (RULE B), and the holding is
  read back from exactly those already-held rows, i.e. a current read, never this transaction's
  older snapshot.
* Employee IR: the work-order block (RULE D) is already held there and RULE E forbids waiting, so
  the Supplier row is taken ``NOWAIT`` and a busy supplier is refused with "retry". Its Bins are
  then read with non-waiting share locks (a covered index read of the supplier's warehouses, then
  the rows by primary key), so they too are the latest committed values.
* The pending MOP Logs cannot be read with locks (the unsynced range of ``mop_eod_sync_idx`` is
  shared by every work order on the site), so they come from the transaction's snapshot. What could
  make that snapshot too low is an Issue to the same supplier committed after it was taken. Every
  Issue that passes therefore bumps the supplier's issue counter (one ``tabSeries`` row, touched
  only while holding the Supplier row), and both paths compare the snapshot's counter with the
  locked one right after taking the Supplier row: a difference is refused with "retry", whose
  fresh transaction sees that Issue. Stock Entries do not bump it, so a second Stock Entry still
  waits for the first and is judged on its current Bins.
* Residual, safe side: the EOD sync committing between a Stock Entry's snapshot and its Bin locks
  shows the synced metal in both the Bins and the snapshot's pending logs -- a refusal a retry
  clears. (Employee IR submits are refused during the sync by ``current_operation_guard``.)

Out of scope (v1): cancelling an entry that moved metal OUT of a supplier, or a subcontract
Employee IR Receive (both bring the metal back to the supplier); a cancel restores a state that was
already allowed, and refusing it would leave the document stuck as submitted.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import cstr, flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
	prefetch_purity_percentages,
)
from jewellery_erpnext.utils import bulk_map

LIMIT_FIELD = "custom_metal_weight_limit"

# Exact ``Item.variant_of`` families that carry metal (``material_weights.VARIANT_BUCKETS``). Never a
# first-letter match: ``MU...`` design / finished-good templates also start with "M".
METAL_FAMILIES = ("M", "ML", "F", "FL")

PRECISION = 3
EPS = 0.001

_PLAN_FLAG = "supplier_metal_limit_plan"
BIN_PAIRS_FLAG = "supplier_limit_bin_pairs"
LOCKED_BINS_FLAG = "supplier_limit_locked_bins"


class SupplierMetalLimitError(frappe.ValidationError):
	pass


# ------------------------------------------------------------------------------------------------
# purity / pure weight
# ------------------------------------------------------------------------------------------------


def purity_of(item_code):
	"""Purity % of a metal / finding item.

	``Attribute Value.purity_percentage`` first (it maps 99.9 -> 100, the app's "24KT is pure"
	convention). Some purities carry 0 there (58.65, 75.15 on kggk-prod), so fall back to the
	Metal Purity attribute value itself, which is the purity number. ``None`` when the item has no
	purity at all (alloys, refining scrap) -- such items do not count.
	"""
	purity = flt(get_purity_percentage(item_code))
	if purity > 0:
		return purity
	value = frappe.db.get_value(
		"Item Variant Attribute",
		{"parent": item_code, "attribute": "Metal Purity"},
		"attribute_value",
	)
	try:
		purity = float(cstr(value).strip())
	except (TypeError, ValueError):
		return None
	return purity if purity > 0 else None


def _metal_items(item_codes):
	"""The subset of ``item_codes`` that is metal / finding, with its purity: ``{item: purity}``."""
	item_codes = sorted({cstr(i).strip() for i in item_codes if cstr(i).strip()})
	if not item_codes:
		return {}
	items = bulk_map("Item", item_codes, ["variant_of"])
	metal = [
		i
		for i in item_codes
		if (items.get(i) or {}).get("variant_of") in METAL_FAMILIES
	]
	if not metal:
		return {}
	prefetch_purity_percentages(metal)
	out = {}
	for item_code in metal:
		purity = purity_of(item_code)
		if purity:
			out[item_code] = purity
	return out


def pure_weight(qty, purity):
	return flt(flt(qty) * flt(purity) / 100, PRECISION)


def _stock_qty(row):
	return flt(row.get("transfer_qty")) or flt(row.get("qty")) * flt(
		row.get("conversion_factor") or 1
	)


# ------------------------------------------------------------------------------------------------
# supplier lookups
# ------------------------------------------------------------------------------------------------


def _warehouse_suppliers(warehouses):
	"""``{warehouse: supplier}`` for the warehouses linked to a supplier."""
	warehouses = sorted({cstr(w).strip() for w in warehouses if cstr(w).strip()})
	if not warehouses:
		return {}
	rows = bulk_map("Warehouse", warehouses, ["subcontractor"])
	return {
		wh: row.get("subcontractor")
		for wh, row in rows.items()
		if row.get("subcontractor")
	}


def _limit_field_exists():
	"""False until ``add_supplier_metal_weight_limit`` has run on the site: code deployed ahead of
	its migrate must not break every Stock Entry into a supplier warehouse on "Unknown column".
	``has_column`` is served from the cached table description."""
	return frappe.db.has_column("Supplier", LIMIT_FIELD)


def _limits(suppliers):
	"""``{supplier: (limit, supplier_name)}`` for the suppliers that have a limit set."""
	suppliers = sorted({s for s in suppliers if s})
	if not suppliers or not _limit_field_exists():
		return {}
	rows = bulk_map("Supplier", suppliers, [LIMIT_FIELD, "supplier_name"])
	out = {}
	for supplier, row in rows.items():
		limit = flt(row.get(LIMIT_FIELD), PRECISION)
		if limit > 0:
			out[supplier] = (limit, row.get("supplier_name") or supplier)
	return out


def supplier_warehouses(suppliers):
	"""``{supplier: [warehouse, ...]}`` -- every warehouse linked to each supplier."""
	suppliers = sorted({s for s in suppliers if s})
	if not suppliers:
		return {}
	out = {}
	for row in frappe.get_all(
		"Warehouse",
		filters={"subcontractor": ["in", suppliers]},
		fields=["name", "subcontractor"],
		order_by=None,
	):
		out.setdefault(row.subcontractor, []).append(row.name)
	return out


def _supplier_metal_bins(warehouses):
	"""``[(item_code, warehouse)]`` of every metal / finding Bin in ``warehouses``."""
	if not warehouses:
		return []
	return [
		(r[0], r[1])
		for r in frappe.db.sql(
			"""
			SELECT b.item_code, b.warehouse
			FROM `tabBin` b
			JOIN `tabItem` i ON i.name = b.item_code
			WHERE b.warehouse IN %(warehouses)s AND i.variant_of IN %(families)s
			""",
			{"warehouses": tuple(warehouses), "families": METAL_FAMILIES},
		)
	]


def _bin_holdings(warehouse_supplier, locked_bins=None):
	"""``{supplier: pure grams}`` currently in the Bins of ``warehouse_supplier``'s warehouses.

	With ``locked_bins`` (names ``prelock_bins`` already holds) the read is ``FOR UPDATE`` on just
	those rows -- re-entrant, so it waits on nothing, and it returns the latest committed qty rather
	than this transaction's snapshot. Without it, a plain read (preflight / Employee IR).
	"""
	warehouses = tuple(sorted(warehouse_supplier))
	if not warehouses:
		return {}
	if locked_bins is not None:
		if not locked_bins:
			return {}
		rows = frappe.db.sql(
			"""
			SELECT item_code, warehouse, actual_qty FROM `tabBin`
			WHERE name IN %(names)s AND warehouse IN %(warehouses)s
			FOR UPDATE
			""",
			{"names": tuple(sorted(locked_bins)), "warehouses": warehouses},
			as_dict=True,
		)
	else:
		rows = frappe.db.sql(
			"""
			SELECT b.item_code, b.warehouse, b.actual_qty
			FROM `tabBin` b
			JOIN `tabItem` i ON i.name = b.item_code
			WHERE b.warehouse IN %(warehouses)s AND i.variant_of IN %(families)s
			  AND b.actual_qty != 0
			""",
			{"warehouses": warehouses, "families": METAL_FAMILIES},
			as_dict=True,
		)
	return _price_bins(rows, warehouse_supplier)


_BIN_WAREHOUSE_INDEX = {}  # site -> name of the single-column index on tabBin.warehouse


def _bin_warehouse_index():
	"""Name of the ``tabBin.warehouse`` index (``search_index`` in ERPNext's Bin JSON), resolved
	by column like ``current_operation_guard._draft_index_name`` -- or ``None``. Cached per site."""
	site = getattr(frappe.local, "site", None)
	if site not in _BIN_WAREHOUSE_INDEX:
		try:
			index = frappe.db.get_column_index("tabBin", "warehouse")
		except Exception:
			index = None
		_BIN_WAREHOUSE_INDEX[site] = index.get("Key_name") if index else None
	return _BIN_WAREHOUSE_INDEX[site]


def _current_bin_holdings(warehouse_supplier):
	"""Like :func:`_bin_holdings`, but the LATEST committed Bins, read without ever waiting (RULE
	E -- the Employee IR path holds the work-order block):

	1. ``SELECT name ... FORCE INDEX (warehouse) ... LOCK IN SHARE MODE NOWAIT``: covered by the
	   index, so it locks index entries only, and it also finds a Bin created after this
	   transaction's snapshot;
	2. those rows by primary key, ``LOCK IN SHARE MODE NOWAIT``.

	A Bin being written right now raises ``QueryTimeoutError`` / ``QueryDeadlockError`` -- the
	caller says "retry". Without the index (never seen) it falls back to the plain read.
	"""
	warehouses = tuple(sorted(warehouse_supplier))
	if not warehouses:
		return {}
	index_name = _bin_warehouse_index()
	if not index_name:
		return _bin_holdings(warehouse_supplier)
	names = [
		r[0]
		for r in frappe.db.sql(
			f"""
			SELECT name FROM `tabBin` FORCE INDEX (`{index_name}`)
			WHERE warehouse IN %(warehouses)s
			LOCK IN SHARE MODE NOWAIT
			""",
			{"warehouses": warehouses},
		)
	]
	if not names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT item_code, warehouse, actual_qty FROM `tabBin`
		WHERE name IN %(names)s
		LOCK IN SHARE MODE NOWAIT
		""",
		{"names": tuple(sorted(names))},
		as_dict=True,
	)
	return _price_bins(rows, warehouse_supplier)


def _price_bins(rows, warehouse_supplier):
	"""``{supplier: pure grams}`` of Bin ``rows`` (``item_code``, ``warehouse``, ``actual_qty``);
	non-metal rows and rows without a purity count nothing."""
	purities = _metal_items(r.item_code for r in rows if flt(r.actual_qty))
	held = {}
	for r in rows:
		purity = purities.get(r.item_code)
		if not purity:
			continue
		supplier = warehouse_supplier.get(r.warehouse)
		held[supplier] = held.get(supplier, 0.0) + flt(r.actual_qty) * purity / 100
	return {s: flt(q, PRECISION) for s, q in held.items()}


# ------------------------------------------------------------------------------------------------
# Stock Entry
# ------------------------------------------------------------------------------------------------


def _is_waived(doc):
	return bool(getattr(frappe.flags, "in_eod_mop_sync", False))


def stock_entry_pure_deltas(doc):
	"""``{supplier: net pure grams}`` this Stock Entry moves into each supplier's warehouses.

	A row into a supplier warehouse adds, a row out of one subtracts, so an internal move between
	two warehouses of the same supplier nets to 0.
	"""
	rows = [r for r in doc.get("items") or [] if r.get("item_code")]
	warehouse_supplier = _warehouse_suppliers(
		[r.get("s_warehouse") for r in rows] + [r.get("t_warehouse") for r in rows]
	)
	if not warehouse_supplier:
		return {}
	purities = _metal_items(r.item_code for r in rows)
	deltas = {}
	for row in rows:
		purity = purities.get(row.item_code)
		if not purity:
			continue
		pure = flt(_stock_qty(row)) * purity / 100
		target = warehouse_supplier.get(cstr(row.get("t_warehouse")).strip())
		source = warehouse_supplier.get(cstr(row.get("s_warehouse")).strip())
		if target:
			deltas[target] = deltas.get(target, 0.0) + pure
		if source:
			deltas[source] = deltas.get(source, 0.0) - pure
	return {s: flt(d, PRECISION) for s, d in deltas.items()}


def _plan(doc):
	"""``{supplier: {"delta", "limit", "supplier_name"}}`` for the suppliers this entry raises AND
	that have a limit. Computed once per submit and kept on ``doc.flags``."""
	plan = doc.flags.get(_PLAN_FLAG)
	if plan is not None:
		return plan
	plan = {}
	if not _is_waived(doc):
		deltas = {s: d for s, d in stock_entry_pure_deltas(doc).items() if d > EPS}
		for supplier, (limit, name) in _limits(deltas).items():
			plan[supplier] = {
				"delta": deltas[supplier],
				"limit": limit,
				"supplier_name": name,
			}
	doc.flags[_PLAN_FLAG] = plan
	return plan


def _lock_suppliers(suppliers, nowait=False):
	frappe.db.sql(
		f"""
		SELECT name FROM `tabSupplier`
		WHERE name IN %(suppliers)s
		ORDER BY name
		FOR UPDATE{" NOWAIT" if nowait else ""}
		""",
		{"suppliers": tuple(sorted(suppliers))},
	)


def _throw_busy(supplier_name):
	frappe.throw(
		_(
			"Another transfer to supplier {0} is being submitted right now. Please retry in a "
			"moment."
		).format(frappe.bold(supplier_name)),
		title=_("Supplier Busy"),
	)


def _issue_seq_key(supplier):
	"""``tabSeries`` key of ``supplier``'s Employee IR issue counter (``name`` is varchar 100)."""
	return "SML-" + hashlib.sha1(cstr(supplier).encode("utf-8")).hexdigest()[:24]


def _issue_seq_moved(suppliers, nowait=False):
	"""The suppliers whose issue counter moved after this transaction's snapshot was taken, i.e.
	an Employee IR Issue to them committed that the snapshot's MOP Logs do not show.

	Call while holding the Supplier rows: only their holders touch the counter, so the locking
	read never waits on anything (``nowait`` keeps RULE E explicit on the Employee IR path). The
	plain read goes first -- it is the snapshot's value (or, in a transaction with no read yet,
	starts the snapshot now and is then equal by construction)."""
	moved = []
	for supplier in sorted({s for s in suppliers if s}):
		key = _issue_seq_key(supplier)
		snapshot = frappe.db.sql(
			"SELECT `current` FROM `tabSeries` WHERE `name` = %s", (key,)
		)
		latest = frappe.db.sql(
			f"SELECT `current` FROM `tabSeries` WHERE `name` = %s FOR UPDATE"
			f"{' NOWAIT' if nowait else ''}",
			(key,),
		)
		if (snapshot[0][0] if snapshot else 0) != (latest[0][0] if latest else 0):
			moved.append(supplier)
	return moved


def _bump_issue_seq(supplier):
	"""Mark an Employee IR Issue to ``supplier`` -- in the Issue's own transaction, so a rollback
	takes the mark back with it (``serial_no.reserve_stamping_sequence``'s atomic upsert)."""
	frappe.db.sql(
		"""
		INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 1)
		ON DUPLICATE KEY UPDATE `current` = `current` + 1
		""",
		(_issue_seq_key(supplier),),
	)


def lock_supplier_limits(doc, method=None):
	"""Stock Entry ``before_submit``, BEFORE ``prelock_bins``: lock the limited suppliers' rows,
	refuse (retry) when an Employee IR Issue to one of them committed after this transaction's
	snapshot, and hand ``prelock_bins`` their metal Bins so they join its single sorted Bin batch."""
	# Fresh per submit attempt: a retried submit of the same document object must not reuse the
	# previous attempt's rows or Bin names.
	for flag in (_PLAN_FLAG, BIN_PAIRS_FLAG, LOCKED_BINS_FLAG):
		doc.flags.pop(flag, None)
	plan = _plan(doc)
	if not plan:
		return
	_lock_suppliers(plan)
	moved = _issue_seq_moved(plan)
	if moved:
		_throw_busy(plan[moved[0]]["supplier_name"])
	warehouses = [wh for whs in supplier_warehouses(plan).values() for wh in whs]
	doc.flags[BIN_PAIRS_FLAG] = _supplier_metal_bins(warehouses)


def validate_supplier_metal_limit(doc, method=None):
	"""Stock Entry ``before_submit``, AFTER ``prelock_bins``: refuse an entry that would take a
	supplier's pure metal holding (its locked Bins + its pending EOD-sync MOP Logs) over its
	limit."""
	plan = _plan(doc)
	if not plan:
		return
	warehouses_of = supplier_warehouses(plan)
	warehouse_supplier = {
		wh: supplier for supplier, whs in warehouses_of.items() for wh in whs
	}
	held = _bin_holdings(
		warehouse_supplier, locked_bins=doc.flags.get(LOCKED_BINS_FLAG)
	)
	_throw_if_breached(
		{
			supplier: dict(
				entry,
				current=flt(
					held.get(supplier, 0.0)
					+ pending_sync_pure(warehouses_of.get(supplier) or []),
					PRECISION,
				),
			)
			for supplier, entry in plan.items()
		}
	)


# ------------------------------------------------------------------------------------------------
# Employee IR Issue (subcontracting)
# ------------------------------------------------------------------------------------------------


def _employee_ir_incoming(doc):
	"""Pure grams the Issue will hand over: the operations' current MOP balances -- the rows
	``creste_mop_log_for_employee_ir`` clones to the supplier's warehouse."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		drop_pre_refining_rows,
		get_current_mop_balance_rows,
		select_fields,
	)

	balances = []
	for row in doc.get("employee_ir_operations") or []:
		if not row.get("manufacturing_operation"):
			continue
		balances.extend(
			drop_pre_refining_rows(
				get_current_mop_balance_rows(
					row.manufacturing_operation, include_fields=select_fields
				),
				row.get("manufacturing_work_order"),
			)
		)
	purities = _metal_items(b.get("item_code") for b in balances)
	return flt(
		sum(
			flt(b.get("qty_after_transaction_batch_based"))
			* purities[b.get("item_code")]
			/ 100
			for b in balances
			if purities.get(b.get("item_code"))
		),
		PRECISION,
	)


def pending_sync_pure(warehouses, exclude_operations=()):
	"""Net pure grams the next EOD sync will move into (+) / out of (-) ``warehouses``.

	Mirrors ``mop_eod_sync``: per work order with unsynced logs, the operation owning the newest
	log is the last one, its latest ``to_warehouse`` is where the stock lands and its latest
	balance per (item, batch) is what moves. Where it moves from is proxied by the
	``from_warehouse`` of the work order's oldest unsynced log -- where the stock sat when the
	unsynced chain began. ``exclude_operations``: the operations being issued right now, priced
	separately as incoming.
	"""
	warehouses = set(warehouses)
	if not warehouses:
		return 0.0
	mwos = [
		r[0]
		for r in frappe.db.sql(
			"""
			SELECT DISTINCT manufacturing_work_order FROM `tabMOP Log`
			WHERE is_synced = 0 AND is_cancelled = 0
			  AND (to_warehouse IN %(warehouses)s OR from_warehouse IN %(warehouses)s)
			""",
			{"warehouses": tuple(sorted(warehouses))},
		)
		if r[0]
	]
	if not mwos:
		return 0.0
	logs = frappe.db.sql(
		"""
		SELECT manufacturing_work_order, manufacturing_operation, item_code, batch_no,
		       from_warehouse, to_warehouse, qty_after_transaction_batch_based, creation, name
		FROM `tabMOP Log`
		WHERE is_synced = 0 AND is_cancelled = 0 AND manufacturing_work_order IN %(mwos)s
		ORDER BY creation, name
		""",
		{"mwos": tuple(sorted(set(mwos)))},
		as_dict=True,
	)
	by_mwo = {}
	for log in logs:
		by_mwo.setdefault(log.manufacturing_work_order, []).append(log)

	excluded = set(exclude_operations or ())
	moves = []  # (sign, item_code, qty)
	for mwo_logs in by_mwo.values():
		last_op = mwo_logs[-1].manufacturing_operation
		if last_op in excluded:
			continue
		source = next(
			(log.from_warehouse for log in mwo_logs if log.from_warehouse), None
		)
		op_logs = [log for log in mwo_logs if log.manufacturing_operation == last_op]
		target = next(
			(log.to_warehouse for log in reversed(op_logs) if log.to_warehouse), None
		)
		into, out_of = target in warehouses, source in warehouses
		if into == out_of:
			continue
		latest = {}
		for log in op_logs:
			latest[(log.item_code, log.batch_no)] = log
		sign = 1 if into else -1
		moves.extend(
			(sign, log.item_code, flt(log.qty_after_transaction_batch_based))
			for log in latest.values()
		)
	purities = _metal_items(item for _sign, item, _qty in moves)
	return flt(
		sum(
			sign * qty * purities[item] / 100
			for sign, item, qty in moves
			if purities.get(item)
		),
		PRECISION,
	)


def _is_subcontract_issue(doc):
	return (
		doc.get("type") == "Issue"
		and doc.get("subcontracting") == "Yes"
		and doc.get("subcontractor")
	)


def validate_employee_ir_supplier_limit(doc, locking=True):
	"""Employee IR Issue (subcontracting) submit: refuse handing a supplier more pure metal than
	its limit allows. ``locking=False`` for the lock-free Submission Queue preflight."""
	if not _is_subcontract_issue(doc):
		return
	supplier = doc.subcontractor
	limits = _limits([supplier])
	if not limits:
		return
	limit, supplier_name = limits[supplier]
	incoming = _employee_ir_incoming(doc)
	if incoming <= EPS:
		return
	warehouses = supplier_warehouses([supplier]).get(supplier) or []
	warehouse_supplier = {wh: supplier for wh in warehouses}
	if locking:
		# Supplier row, then its issue counter, then its Bins (parent -> Series -> Bin); all NOWAIT.
		try:
			_lock_suppliers([supplier], nowait=True)
			if _issue_seq_moved([supplier], nowait=True):
				_throw_busy(supplier_name)
			held = _current_bin_holdings(warehouse_supplier).get(supplier, 0.0)
		except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
			_throw_busy(supplier_name)
	else:
		held = _bin_holdings(warehouse_supplier).get(supplier, 0.0)
	current = flt(
		held
		+ pending_sync_pure(
			warehouses,
			exclude_operations=[
				r.manufacturing_operation
				for r in doc.get("employee_ir_operations") or []
			],
		),
		PRECISION,
	)
	_throw_if_breached(
		{
			supplier: {
				"delta": incoming,
				"limit": limit,
				"supplier_name": supplier_name,
				"current": current,
			}
		}
	)
	if locking:
		_bump_issue_seq(supplier)


def employee_ir_preflight(doc):
	"""Lock-free check at enqueue time, so a refused submit fails in the user's own request."""
	validate_employee_ir_supplier_limit(doc, locking=False)


# ------------------------------------------------------------------------------------------------
# message
# ------------------------------------------------------------------------------------------------


def _throw_if_breached(entries):
	"""``entries``: ``{supplier: {"delta", "limit", "current", "supplier_name"}}``."""
	problems = []
	for supplier in sorted(entries):
		e = entries[supplier]
		current = flt(e["current"], PRECISION)
		delta = flt(e["delta"], PRECISION)
		projected = flt(current + delta, PRECISION)
		if delta <= EPS or projected <= flt(e["limit"], PRECISION) + EPS:
			continue
		problems.append(
			_(
				"Supplier {0}'s maximum allowable pure metal weight is {1} grams. The current pure "
				"metal weight is {2} grams, and the proposed transfer will add {3} grams of pure "
				"metal, bringing the total to {4} grams. The transfer cannot be completed because "
				"it exceeds the configured limit."
			).format(
				frappe.bold(e["supplier_name"]),
				frappe.bold(flt(e["limit"], PRECISION)),
				current,
				delta,
				frappe.bold(projected),
			)
		)
	if problems:
		frappe.throw(
			"<br><br>".join(problems),
			title=_("Supplier Metal Weight Limit Exceeded"),
			exc=SupplierMetalLimitError,
		)


@frappe.whitelist()
def get_supplier_pure_holding(supplier):
	"""Supplier form indicator: pure grams in the supplier's warehouses' Bins, the net pure grams
	unsynced MOP Logs will still move in / out at the next EOD sync, and the limit."""
	frappe.has_permission("Supplier", "read", supplier, throw=True)
	warehouses = supplier_warehouses([supplier]).get(supplier) or []
	held = _bin_holdings({wh: supplier for wh in warehouses}).get(supplier, 0.0)
	pending = pending_sync_pure(warehouses)
	return {
		"held": held,
		"pending": pending,
		"limit": flt((_limits([supplier]).get(supplier) or (0, None))[0], PRECISION),
	}
