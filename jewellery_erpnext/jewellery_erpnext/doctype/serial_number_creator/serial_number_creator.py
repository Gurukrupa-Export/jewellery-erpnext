# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
	cint,
	cstr,
	date_diff,
	flt,
	get_first_day,
	get_last_day,
	nowdate,
)

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_current_mop_balance_rows,
)


class SerialNumberCreator(Document):
	def validate(self):
		# Runs on draft save AND on submit (_submit -> save), so a document created
		# before this existed repairs itself the next time it is saved or submitted.
		split_source_rows_by_reservation(self)

	def before_insert(self):
		validate_not_metal_only(self)
		self._render_fg_details()
		self._compute_total_weight()

	# 	if not self.fg_details:
	# 		self.load_raw_materials()

	def on_submit(self):
		validate_qty(self)
		calulate_id_wise_sum_up(self)

		# Run straight through — NO synchronous retry. The lock-contention root causes are
		# fixed at source (canonical series + Bin pre-locking in
		# to_prepare_data_for_make_mnf_stock_entry / prelock_bins, and SLE/SRE hash naming),
		# so this cascade no longer self-collides on 1205/1213. The old hand-rolled
		# 3-attempt loop also re-ran partial work (begin() + db_update re-entry) and masked
		# genuine NegativeStock / Validation errors by retrying them — both removed. The only
		# retry kept in the codebase is bounded_retry on idempotent *background* jobs.
		to_prepare_data_for_make_mnf_stock_entry(self)
		update_new_serial_no(self)

	def _render_fg_details(self):
		"""Build source_table (batch-wise) and fg_details (aggregated) from MOP Log."""
		mop_name = self.manufacturing_operation
		mwo_name = self.manufacturing_work_order

		if not mop_name:
			if mwo_name:
				frappe.throw(
					_(
						f"Manufacturing Operation is required to render FG Details for MWO: {mwo_name}"
					)
				)
			return

		# Only auto-populate if both tables are empty (first save / draft)
		if self.fg_details or self.source_table:
			return

		# Resolve manufacturing qty (number of IDs to split across)
		mnf_qty = _resolve_snc_mnf_qty(self)
		if mnf_qty <= 0:
			return

		# Get batch-wise source rows from MOP Log
		source_rows = _get_source_raw_materials(mop_name, self)
		if not source_rows:
			return

		self.set("fg_details", [])
		self.set("source_table", [])

		# -- Source Table: batch-wise rows with full detail --
		for row in source_rows:
			self.append(
				"source_table",
				{
					"row_material": row.get("item_code"),
					"qty": row.get("qty"),
					"uom": row.get("uom"),
					"pcs": row.get("pcs"),
					"batch_no": row.get("batch_no"),
					"inventory_type": row.get("inventory_type"),
					"customer": row.get("customer"),
					"s_warehouse": row.get("s_warehouse"),
					"sub_setting_type": row.get("sub_setting_type"),
					"sed_item": row.get("sed_item"),
				},
			)

		# -- FG Details: aggregated by item_code, split across mnf_qty IDs --
		_append_fg_rows_aggregated(self, source_rows, mnf_qty)

	def _compute_total_weight(self):
		"""Auto-compute total_weight (product weight / gross weight) from fg_details."""
		total = 0
		for row in self.fg_details or []:
			if not row.row_material:
				continue
			first_char = row.row_material[0] if row.row_material else ""
			if first_char in ("D", "G"):
				# Carat items → convert to grams
				total += flt(row.qty) * 0.2
			else:
				total += flt(row.qty)
		self.total_weight = flt(total, 3)

	@frappe.whitelist()
	def get_serial_summary(self):
		# Define the tables
		stock_entry = frappe.qb.DocType("Stock Entry")
		serial_no = frappe.qb.DocType("Serial No")
		bom = frappe.qb.DocType("BOM")

		# Build the query
		data = (
			frappe.qb.from_(stock_entry)
			.left_join(serial_no)
			.on(
				(stock_entry.name == serial_no.purchase_document_no)
				| (stock_entry.name == serial_no.reference_name)
			)
			.left_join(bom)
			.on(serial_no.name == bom.tag_no)
			.select(
				stock_entry.name.as_("stock_entry"),
				serial_no.name.as_("serial_no"),
				bom.name.as_("bom_name"),
				serial_no.purchase_document_no,
				serial_no.reference_name,
			)
			.where(stock_entry.custom_serial_number_creator == self.name)
		).run(as_dict=True)

		return frappe.render_template(
			"jewellery_erpnext/jewellery_erpnext/doctype/serial_number_creator/serial_summery.html",
			{"data": data},
		)

	@frappe.whitelist()
	def get_bom_summary(self):
		if self.design_id_bom:
			bom_data = frappe.get_doc("BOM", self.design_id_bom)
			item_records = []
			for bom_row in bom_data.items:
				item_record = {
					"item_code": bom_row.item_code,
					"qty": bom_row.qty,
					"uom": bom_row.uom,
				}
				item_records.append(item_record)
			return frappe.render_template(
				"jewellery_erpnext/jewellery_erpnext/doctype/serial_number_creator/bom_summery.html",
				{"data": item_records},
			)


# Floating-point slack for carat/gram comparisons (mirror pc_tagging_stock_sync).
TOLERANCE = 0.0001


def _physical_batch_qty(item_code, batch_no, warehouse):
	"""Physical SBB qty of ``(item, batch)`` in ``warehouse``, ignoring reservations.

	Mirror of ``pc_tagging_stock_sync._physical_batch_qty``. ``ignore_reserved_stock=True``
	is required: the batch we are about to consume is itself reserved by the SRE being
	processed, and the negative-stock validator checks *physical* qty — the default
	(reservation-subtracted) qty would understate the correct warehouse. Returns 0.0
	for non-batch lines / on error so callers never crash.
	"""
	if not batch_no or not warehouse:
		return 0.0
	try:
		return flt(
			get_batch_qty(batch_no, warehouse, item_code, ignore_reserved_stock=True),
			3,
		)
	except Exception:
		return 0.0


def _warehouses_with_physical_batch(item_code, batch_no):
	"""Return ``[(warehouse, qty)]`` for warehouses physically holding the batch.

	Sorted by qty descending. Used only for the fail-fast diagnostic message when no
	candidate warehouse can source the full qty. ``get_batch_qty`` with no warehouse
	returns a list of ``{batch_no, warehouse, qty}`` dicts (negative/zero batches are
	already filtered out by core).
	"""
	if not batch_no:
		return []
	try:
		rows = get_batch_qty(
			batch_no=batch_no, item_code=item_code, ignore_reserved_stock=True
		)
	except Exception:
		rows = None
	out = []
	for r in rows or []:
		if r.get("batch_no") == batch_no:
			q = flt(r.get("qty"), 3)
			if q > TOLERANCE:
				out.append((r.get("warehouse"), q))
	out.sort(key=lambda t: t[1], reverse=True)
	return out


def _pick_source_warehouse(item_code, batch_no, requested_qty, candidates, used=None):
	"""First candidate warehouse whose physical batch qty covers ``requested_qty``.

	Mirror of ``pc_tagging_stock_sync._pick_source_warehouse``. ``candidates`` is an
	ordered, de-duplicated, falsy-stripped list of warehouse names (highest priority
	first). For non-batch lines the first candidate is returned (nothing for the batch
	validator to check). For batch lines, returns the first candidate whose physical
	batch qty + ``TOLERANCE`` >= ``requested_qty``; ``None`` if none qualifies (the
	caller decides how to handle that).

	``used`` is an optional ``{warehouse: qty}`` ledger of what sibling rows of the same
	(item, batch) group have already claimed, subtracted from each candidate's physical
	qty. Two rows of a warehouse-split group would otherwise both resolve to the largest
	reserved warehouse and overdraw it. Left ``None`` the function behaves exactly as it
	did before the ledger existed.
	"""
	if not candidates:
		return None
	if not batch_no:
		return candidates[0]
	for wh in candidates:
		available = flt(_physical_batch_qty(item_code, batch_no, wh) or 0) - flt(
			(used or {}).get(wh, 0)
		)
		if available + TOLERANCE >= requested_qty:
			return wh
	return None


def _warehouse_has_batch_stock(item_code, batch_no, warehouse):
	"""Return True if ``batch_no`` of ``item_code`` physically has stock in ``warehouse``.

	Used when adopting a warehouse from a Stock Reservation Entry / Product
	Certification Receive. Multiple reservations can exist for the same item in
	different warehouses, and a later Stock Entry may have physically moved the
	batch elsewhere. Adopting a warehouse where the batch has no stock causes
	``BatchNegativeStockError`` when the auto-created Manufacture entry is
	submitted. A row without a batch has nothing to validate, so it is allowed.

	``ignore_reserved_stock=True`` is required: the negative-stock validator
	checks *physical* batch qty, while ``get_batch_qty`` subtracts reserved stock
	by default — and the batch we are about to consume is itself reserved by the
	SRE being processed, so the default would report 0 for the correct warehouse.
	"""
	if not batch_no:
		return True
	return (
		flt(get_batch_qty(batch_no, warehouse, item_code, ignore_reserved_stock=True))
		> 0
	)


def _sre_reserves_batch(sre_name, batch_no):
	"""Whether the Stock Reservation Entry reserves ``batch_no``.

	SREs reserve specific (item, batch) lots in their Serial and Batch Entry
	children. A Qty-based SRE has no batch children and matches any batch
	(item-level reservation, the original behaviour).
	"""
	sre_batches = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": sre_name, "parenttype": "Stock Reservation Entry"},
		pluck="batch_no",
	)
	return (not sre_batches) or (batch_no in sre_batches)


def _pmo_mwo_names(doc):
	"""``(pmo, [submitted MWO names])`` for this SNC's Parent Manufacturing Order.

	This is the ONLY scope reservations are ever read from. Stock reserved for a
	different job — even the same item and batch, even in the same warehouse — must
	never be drawn on here. Falls back to the SNC's own MWO when the PMO has no
	submitted work orders.
	"""
	mwo_name = cstr(getattr(doc, "manufacturing_work_order", None) or "").strip()
	pmo = (
		frappe.db.get_value("Manufacturing Work Order", mwo_name, "manufacturing_order")
		if mwo_name
		else None
	)
	names = (
		frappe.get_all(
			"Manufacturing Work Order",
			{"manufacturing_order": pmo, "docstatus": 1},
			pluck="name",
		)
		if pmo
		else []
	)
	return pmo, (names or ([mwo_name] if mwo_name else []))


def _active_sres_for(item_code, batch_no, mwo_names, warehouse=None, exclude=None):
	"""``[(sre_dict, remaining_qty)]`` for live reservations of ``(item, batch)``.

	Sorted remaining desc, then warehouse asc — deterministic regardless of DB row order.

	Only ACTIVE reservations are candidates. A fully-delivered SRE (status
	"Delivered"/"Cancelled" or ``delivered_qty >= reserved_qty``) is already consumed:
	its warehouse is stale (the batch was physically moved out), its qty must not be
	summed, and it must never be re-consumed. Remaining qty — not the status label — is
	the authoritative "still active" guard; it also covers the Partially Delivered /
	Partially Used / Closed states the coarse status filter does not catch.

	``remaining`` is BATCH-scoped: a Serial-and-Batch reservation is capped by that
	batch's own undelivered child qty, so an SRE covering three batches cannot lend its
	whole header qty to one of them. A Qty-based SRE has no batch children and reserves
	at item level, so it keeps the header remainder — the original behaviour, mirroring
	``_sre_reserves_batch``.
	"""
	if not mwo_names:
		return []

	filters = {
		"item_code": item_code,
		"docstatus": 1,
		"status": ["not in", ["Cancelled", "Delivered"]],
		"manufacturing_work_order": ["in", mwo_names],
	}
	if warehouse:
		filters["warehouse"] = warehouse

	sres = frappe.get_all(
		"Stock Reservation Entry",
		filters=filters,
		fields=["name", "warehouse", "reserved_qty", "delivered_qty"],
	)
	if exclude:
		sres = [s for s in sres if s["name"] not in exclude]
	if not sres:
		return []

	# One bulk child query for the whole set, instead of a _sre_reserves_batch
	# round-trip per SRE.
	children = {}
	for child in frappe.get_all(
		"Serial and Batch Entry",
		filters={
			"parent": ["in", [s["name"] for s in sres]],
			"parenttype": "Stock Reservation Entry",
		},
		fields=["parent", "batch_no", "qty", "delivered_qty"],
	):
		children.setdefault(child["parent"], []).append(child)

	out = []
	for sre in sres:
		header_remaining = flt(sre["reserved_qty"]) - flt(sre["delivered_qty"])
		kids = children.get(sre["name"])
		if kids:
			if batch_no and batch_no not in {k["batch_no"] for k in kids}:
				continue
			scoped = sum(
				flt(k["qty"]) - flt(k["delivered_qty"])
				for k in kids
				if not batch_no or k["batch_no"] == batch_no
			)
			remaining = min(header_remaining, scoped)
		else:
			remaining = header_remaining
		# Round to the site's 3dp stock precision BEFORE the liveness test, so the
		# threshold and the returned value agree. Filtering on the raw remainder and
		# returning the rounded one lets a remainder in (TOLERANCE, 0.0005) come back
		# as 0.0: the caller would see a "live" reservation that contributes no
		# capacity, marking sre_matched True — which consumes the SRE and skips the
		# PC-Receive / Stock-Entry warehouse fallbacks on the strength of nothing.
		# ``_reserved_warehouse_caps`` already rounds-then-filters; match it.
		remaining = flt(remaining, 3)
		if remaining <= TOLERANCE:
			continue
		out.append((sre, remaining))

	out.sort(key=lambda t: (-t[1], t[0]["warehouse"] or ""))
	return out


def _reserved_warehouse_caps(item_code, batch_no, mwo_names):
	"""``[(warehouse, cap_qty, [sre_names])]`` — where this job may draw the batch from.

	``cap = min(sum of reserved-remaining in that warehouse, physical batch qty there)``.
	The physical cap stops the allocator promising stock the negative-batch validator
	would reject at submit; the reservation cap stops it eating another job's metal. A
	warehouse that merely HOLDS the batch without a reservation for this PMO never
	appears in the result at all — that exclusion is structural, not a filter.

	Sorted capacity desc, then warehouse asc.
	"""
	per_wh = {}
	for sre, remaining in _active_sres_for(item_code, batch_no, mwo_names):
		wh = sre["warehouse"]
		if not wh:
			continue
		entry = per_wh.setdefault(wh, {"reserved": 0.0, "sres": []})
		entry["reserved"] += remaining
		entry["sres"].append(sre["name"])

	caps = []
	for wh, entry in per_wh.items():
		cap = flt(entry["reserved"], 3)
		if batch_no:
			cap = min(cap, _physical_batch_qty(item_code, batch_no, wh))
		cap = flt(cap, 3)
		if cap > TOLERANCE:
			caps.append((wh, cap, entry["sres"]))

	caps.sort(key=lambda t: (-t[1], t[0]))
	return caps


def _allocate_qty_across_warehouses(total_qty, caps):
	"""Greedy split of ``total_qty`` over ``caps`` → ``[(warehouse, qty)]`` or ``None``.

	``None`` means the reservations cannot cover the full qty. The caller then leaves the
	row alone so the existing single-warehouse resolution — and its fail-fast throw —
	still applies. Splitting a partially-reserved row would either strand the residue on
	an unreserved warehouse (another job's stock) or silently under-consume.

	Never emits a zero/negative row, and the allocations sum to the caller's
	``total_qty`` exactly: the sub-milligram residue between ``total_qty`` and its 3dp
	rounding is folded back into the first allocation, so the per-item totals
	``calulate_id_wise_sum_up`` checks cannot drift.
	"""
	total = flt(total_qty, 3)
	if total <= TOLERANCE or not caps:
		return None

	allocations = []
	need = total
	for wh, cap, _sres in caps:
		if need <= TOLERANCE:
			break
		take = flt(min(flt(cap, 3), need), 3)
		if take <= TOLERANCE:
			continue
		allocations.append([wh, take])
		need = flt(need - take, 3)

	if need > TOLERANCE or not allocations:
		return None

	residue = flt(total_qty) - sum(a[1] for a in allocations)
	if residue:
		allocations[0][1] = allocations[0][1] + residue

	return [(wh, qty) for wh, qty in allocations]


def _allocate_pcs_across_rows(item_code, total_pcs, allocations):
	"""Distribute ``total_pcs`` over split rows, preserving the per-item total.

	Two regimes, matching the D/G-vs-other boundary ``_append_fg_rows_aggregated``
	already draws:

	* Diamond/Gemstone (``D``/``G`` prefix) — fg_details SUMS pcs across rows, and each
	  warehouse holds a distinct set of physical stones, so split proportionally to the
	  allocated weight (integer largest-remainder, total preserved, and no qty-bearing
	  row left at 0 while the count allows).
	* Metal / findings / everything else — fg_details takes ``max()``, so the whole count
	  goes on the largest allocation and the rest get 0. That preserves both the sum and
	  the max, and it is physically honest: a 0.02 g residue in another warehouse is not
	  "a piece".

	Never returns ``None`` for a row: ``Stock Entry Detail.pcs`` is a Data field with
	``default: "1"``, so a ``None`` would silently become 1 and inflate the count.
	"""
	count = len(allocations)
	if count <= 1:
		return [flt(total_pcs)] * count

	total = flt(total_pcs)
	if total <= 0:
		return [0.0] * count

	is_stone = (item_code or "")[:1].upper() in ("D", "G")
	whole = cint(total)
	if not is_stone or whole != total or whole < count:
		# Metal/other, a fractional count, or fewer stones than rows: keep the count
		# whole on the largest allocation rather than inventing fractional stones.
		parts = [0.0] * count
		parts[0] = total
		return parts

	total_qty = sum(flt(qty) for _wh, qty in allocations) or 1
	exact = [whole * flt(qty) / total_qty for _wh, qty in allocations]
	parts = [int(x) for x in exact]
	for idx in sorted(range(count), key=lambda i: -(exact[i] - parts[i]))[
		: whole - sum(parts)
	]:
		parts[idx] += 1

	# A warehouse that physically holds part of the batch holds at least one stone.
	for idx in range(count):
		if parts[idx]:
			continue
		donor = max(range(count), key=lambda i: parts[i])
		if parts[donor] > 1:
			parts[donor] -= 1
			parts[idx] = 1

	return [flt(p) for p in parts]


def _reserved_summary_text(item_code, batch_no, mwo_names):
	"""``"WH A: 3.557, WH B: 0.02 (total 3.577)"`` — what THIS job has reserved."""
	caps = _reserved_warehouse_caps(item_code, batch_no, mwo_names)
	if not caps:
		return "none"
	total = flt(sum(cap for _wh, cap, _sres in caps), 3)
	return ", ".join(f"{wh}: {cap}" for wh, cap, _sres in caps) + f" (total {total})"


def _group_source_pcs(item_code, rows):
	"""Pcs total for an (item, batch) group, mirroring ``_append_fg_rows_aggregated``."""
	values = [flt(row.pcs or 0) for row in rows]
	if not values:
		return 0.0
	if (item_code or "")[:1].upper() in ("D", "G"):
		return flt(sum(values))
	return flt(max(values))


def _same_allocation(rows, allocations):
	"""True when the source rows already match the allocation exactly."""
	if len(rows) != len(allocations):
		return False
	for row, (wh, qty) in zip(rows, allocations):
		if row.s_warehouse != wh or flt(row.qty, 3) != flt(qty, 3):
			return False
	return True


def split_source_rows_by_reservation(doc):
	"""Key ``source_table`` by (item, batch, **warehouse**) instead of (item, batch).

	One (item, batch) can legitimately be reserved for this job across SEVERAL
	warehouses — e.g. 3.557 g of a 22KT batch in Waxing WO plus a 0.02 g remainder in
	Model Making WO. A single row carrying the combined 3.577 g cannot name one source
	warehouse, and ``_pick_source_warehouse`` rightly refuses to draw the whole amount
	from a warehouse holding only part of it. Splitting the row per reserved warehouse
	is the only representation that matches the physical stock.

	``fg_details`` is deliberately untouched: it stays aggregated per item, which is what
	``calulate_id_wise_sum_up`` and the FG BOM expect. Only the source rows change, and
	the split preserves the per-item qty total, so that check still balances.

	Runs from ``validate`` so it applies on a plain draft save — the operator sees the
	split before submitting — AND on submit. ``_save`` persists child rows through
	``update_children()`` before ``on_submit`` fires, so the split rows already carry
	names when ``to_prepare_data_for_make_mnf_stock_entry`` writes back to them.

	Idempotent: the allocation is recomputed from the GROUP total, never from the
	individual rows, and nothing is consumed until submit — so a second pass sees the
	same reservations and physical qtys, produces the same allocation, and writes
	nothing. Convergent too: if the reservations later collapse onto one warehouse the
	group merges back to a single row.
	"""
	if doc.docstatus == 2 or doc.flags.get("ignore_snc_source_split"):
		return
	if not doc.source_table or not doc.manufacturing_work_order:
		return

	_pmo, mwo_names = _pmo_mwo_names(doc)
	if not mwo_names:
		return

	groups = {}
	for row in doc.source_table:
		groups.setdefault((row.row_material, row.batch_no), []).append(row)

	new_rows = []
	changed = False
	for (item_code, batch_no), rows in groups.items():
		if not item_code:
			new_rows.extend(rows)
			continue

		group_qty = flt(sum(flt(row.qty) for row in rows), 3)
		caps = _reserved_warehouse_caps(item_code, batch_no, mwo_names)
		allocations = _allocate_qty_across_warehouses(group_qty, caps)

		if not allocations or _same_allocation(rows, allocations):
			new_rows.extend(rows)
			continue

		changed = True
		pcs_parts = _allocate_pcs_across_rows(
			item_code, _group_source_pcs(item_code, rows), allocations
		)
		template = rows[0]
		for idx, ((wh, qty), pcs) in enumerate(zip(allocations, pcs_parts)):
			if idx < len(rows):
				# Reuse the existing child so its name survives — an UPDATE rather than a
				# delete + re-insert, which keeps db_set write-backs and version history
				# addressing the same rows.
				row = rows[idx]
				row.qty = qty
				row.pcs = pcs
				row.s_warehouse = wh
			else:
				row = {
					"row_material": item_code,
					"batch_no": batch_no,
					"qty": qty,
					"pcs": pcs,
					"s_warehouse": wh,
					"uom": template.uom,
					"inventory_type": template.inventory_type,
					"customer": template.customer,
					"sub_setting_type": template.sub_setting_type,
					"sed_item": template.sed_item,
					"is_customer_item": template.is_customer_item,
					"default_bom_rm": template.default_bom_rm,
					"bom_qty": template.bom_qty,
					"bom_pcs": template.bom_pcs,
				}
			new_rows.append(row)
		# Surplus rows (reservations collapsed onto fewer warehouses) are dropped here.

	if not changed:
		return

	doc.set("source_table", [])
	for row in new_rows:
		doc.append("source_table", row)
	# Re-appended child Documents keep their old idx, so renumber explicitly.
	for idx, row in enumerate(doc.source_table, start=1):
		row.idx = idx


def to_prepare_data_for_make_mnf_stock_entry(self):
	"""Use source_table (batch-wise) for stock entry creation.

	source_table has one row per (item_code, batch_no, s_warehouse) with all batch
	detail needed for the manufacturing stock entry (inventory_type, pcs, etc.). One
	(item, batch) can span several rows when the job's reservation for it is spread over
	more than one warehouse — see ``split_source_rows_by_reservation``. fg_details is
	kept for BOM creation (aggregated item/qty/pcs).
	"""

	# Build row_data from source_table (batch-wise) for stock entry
	row_data = []
	for row in self.source_table:
		# Inventory type / customer must correspond to the batch actually being
		# consumed. source_table carries these from the upstream Stock Entry
		# Detail, which can disagree with the batch (e.g. a Customer Goods batch
		# stamped "Regular Stock"), producing an inventory-type mismatch on the
		# auto-created Manufacture entry. The Batch master is authoritative here,
		# mirroring the batch-selection convention (stock_entry.js) and repack.
		inventory_type = row.inventory_type
		customer = row.customer
		if row.batch_no:
			batch_inventory_type, batch_customer = frappe.db.get_value(
				"Batch", row.batch_no, ["custom_inventory_type", "custom_customer"]
			) or (None, None)
			if batch_inventory_type:
				inventory_type = batch_inventory_type
				customer = (
					batch_customer if batch_inventory_type == "Customer Goods" else None
				)

		row_data.append(
			{
				"item_code": row.row_material,
				"qty": row.qty,
				"uom": row.uom,
				"id": 1,  # single FG item
				"inventory_type": inventory_type,
				"customer": customer,
				"batch_no": row.batch_no,
				"pcs": row.pcs,
				"s_warehouse": row.s_warehouse,
				"sub_setting_type": row.sub_setting_type,
				# 1:1 back-link to the SNC Source Table child. Warehouse write-backs key
				# on this: sibling rows of a warehouse-split group share item AND batch,
				# so an (item, batch) match would overwrite every sibling's warehouse with
				# whichever row was resolved last. create_manufacturing_entry reads the
				# keys it needs individually (it never **-splats the dict), so the extra
				# key is inert downstream.
				"snc_source_row": row.name,
			}
		)

	pmo = frappe.db.get_value(
		"Manufacturing Work Order",
		self.manufacturing_work_order,
		"manufacturing_order",
	)

	operation_data = frappe.get_all(
		"Manufacturing Operation",
		{"manufacturing_order": pmo, "docstatus": ["!=", 2]},
		["name as manufacturing_operation", "employee", "total_minutes", "operation"],
	)

	if row_data:
		# Canonical lock order for the WHOLE SNC cascade. This one submit mints several
		# Stock Entries — a Repack(Loss) SE per loss row plus the main Manufacture SE in
		# create_manufacturing_entry — each of which would otherwise lock its Bins
		# independently. Two concurrent SNCs drawing on the same WIP stock then interleaved
		# their per-SE Bin locks into 1213 deadlock cycles. Pin the Stock Entry series first
		# (canonical position 2), then pre-lock every source Bin this cascade draws from, in
		# sorted order, BEFORE the first nested submit, so all concurrent SNCs acquire the
		# shared Bins in the identical sequence. Over-locking a Bin that ends up unused is
		# harmless (released on COMMIT); per-SE prelock_bins still locks each SE's own Bins.
		from jewellery_erpnext.jewellery_erpnext.lock_order import (
			lock_bins,
			preallocate_series_for_docs,
			series_stubs,
		)

		# Pin the naming counter of each nested SE type this cascade mints (the loss
		# Repack SEs and the Manufacture SE) -- per-(company x type) Document Naming
		# Rule counter post-reshard, or the tabSeries fallback. A blank stub matches
		# no rule and would pin the wrong (shared MAT-STE-) row instead.
		preallocate_series_for_docs(
			*series_stubs(self.company, "Repack", "Manufacture")
		)
		_pmo, all_pmo_mwos = _pmo_mwo_names(self)

		# Group the source rows by (item, batch). One (item, batch) can span several rows
		# when the job's reservation for it is spread over more than one warehouse. Every
		# row of such a group sees the SAME reservations, so reservation consumption and
		# loss must be accounted ONCE per group: per-row accounting would consume the same
		# SREs repeatedly and book a phantom Repack(Loss) for every sibling.
		row_groups = {}
		for row in row_data:
			if not row.get("s_warehouse"):
				continue
			row_groups.setdefault((row["item_code"], row.get("batch_no")), []).append(
				row
			)

		# Pre-lock every Bin this cascade may draw from — each row's own warehouse plus
		# every reserved warehouse a row could still be re-pointed at below.
		group_caps = {}
		lock_targets = [(r["item_code"], r.get("s_warehouse")) for r in row_data]
		for (item_code, batch_no), rows in row_groups.items():
			caps = _reserved_warehouse_caps(item_code, batch_no, all_pmo_mwos)
			group_caps[(item_code, batch_no)] = caps
			lock_targets.extend((item_code, wh) for wh, _cap, _sres in caps)
		lock_bins(lock_targets)

		src_by_name = {r.name: r for r in self.source_table}

		def _write_back_warehouse(row):
			"""Persist the resolved warehouse onto THIS row's source_table child.

			Keyed on the child row name, not (item, batch): sibling rows of a split group
			share item and batch, so an (item, batch) match would overwrite every sibling's
			warehouse with whichever row happened to be resolved last.
			"""
			st_row = src_by_name.get(row.get("snc_source_row"))
			if st_row and st_row.s_warehouse != row["s_warehouse"]:
				st_row.s_warehouse = row["s_warehouse"]
				st_row.db_set("s_warehouse", row["s_warehouse"])

		bins_to_update = set()
		consumed_sres = set()
		for (item_code, batch_no), rows in row_groups.items():
			group_qty = flt(sum(flt(r["qty"]) for r in rows), 3)
			sre_reserved_qty_total = 0.0
			used = {}

			# ── PRIORITY 1: SRE — capture warehouse + consume reservations ──
			# Scoped to the MWOs of this PMO only; stock reserved for another job is
			# never a candidate, however much of the batch it holds.
			active_sres = _active_sres_for(
				item_code, batch_no, all_pmo_mwos, exclude=consumed_sres
			)
			sre_matched = bool(active_sres)

			if sre_matched:
				sre_warehouses = [
					wh for wh, _cap, _sres in group_caps[(item_code, batch_no)]
				]

				# Largest row first, so the biggest requirement claims the warehouse that
				# can actually hold it before a small sibling nibbles at it.
				for row in sorted(rows, key=lambda r: -flt(r["qty"])):
					# Candidate order: the row's OWN warehouse first when it still carries
					# a live reservation — that is the allocation
					# split_source_rows_by_reservation sized this row against — then the
					# remaining reserved warehouses by capacity, then the row's own
					# warehouse as the last-resort fallback. Without the first entry a
					# 0.02 g sibling would resolve to the 3.557 g warehouse: both rows
					# would target it and the Manufacture entry would go negative. For
					# single-row groups the first entry never applies, so resolution is
					# exactly what it was before.
					own = row.get("s_warehouse")
					candidates = []
					if len(rows) > 1 and own in sre_warehouses:
						candidates.append(own)
					for wh in sre_warehouses:
						if wh not in candidates:
							candidates.append(wh)
					if own and own not in candidates:
						candidates.append(own)

					resolved_wh = _pick_source_warehouse(
						item_code, batch_no, flt(row["qty"], 3), candidates, used=used
					)
					if resolved_wh:
						row["s_warehouse"] = resolved_wh
						used[resolved_wh] = flt(
							flt(used.get(resolved_wh, 0)) + flt(row["qty"], 3), 3
						)
					elif batch_no:
						# No candidate physically covers the qty — fail fast with an
						# actionable message instead of a cryptic BatchNegativeStock error
						# on the auto-created Manufacture entry.
						holders = _warehouses_with_physical_batch(item_code, batch_no)
						holders_str = (
							", ".join(f"{wh}: {qty}" for wh, qty in holders) or "none"
						)
						frappe.throw(
							_(
								"Batch {0} of {1} does not have {2} available in any "
								"reserved/source warehouse.<br><br>"
								"<b>Reserved for this job</b> — {3}.<br>"
								"<b>Physical stock (all warehouses)</b> — {4}.<br><br>"
								"Stock in a warehouse without a reservation for this job "
								"cannot be consumed here."
							).format(
								batch_no,
								item_code,
								flt(row["qty"], 3),
								_reserved_summary_text(
									item_code, batch_no, all_pmo_mwos
								),
								holders_str,
							)
						)

				# Sum REMAINING qty (not reserved_qty) over the group's active SREs so a
				# partially-delivered reservation does not inflate loss_qty into a phantom
				# Repack(Loss). Summed once for the GROUP — the split rows share these
				# reservations, so per-row summing would multiply the loss by the number of
				# siblings.
				sre_reserved_qty_total = sum(rem for _sre, rem in active_sres)

				# Consume ONLY the active SREs (mark Delivered). A fully-delivered SRE is
				# excluded above and is never re-consumed; consumed_sres additionally stops
				# an item-level (Qty-based) reservation, which matches every batch, from
				# being consumed twice by two different batch groups.
				from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
					consume_stock_reservation_entry,
				)

				for sre, _rem in active_sres:
					frappe.clear_document_cache("Bin")
					sre_doc = frappe.get_doc("Stock Reservation Entry", sre["name"])
					consume_stock_reservation_entry(sre_doc, update_bin=False)
					consumed_sres.add(sre["name"])
					if sre_doc.item_code and sre_doc.warehouse:
						bins_to_update.add((sre_doc.item_code, sre_doc.warehouse))
					frappe.clear_document_cache("Bin")

				# Persist the corrected source warehouses back to source_table
				for row in rows:
					_write_back_warehouse(row)

			if not sre_matched:
				# ── PRIORITY 2: Product Certification Receive ──
				pc_receive_data = frappe.db.sql(
					"""
					SELECT se_item.t_warehouse, se_item.qty
					FROM `tabStock Entry` se
					JOIN `tabStock Entry Detail` se_item ON se.name = se_item.parent
					JOIN `tabProduct Certification` pc ON se.product_certification = pc.name
					WHERE pc.type = 'Receive'
					  AND se.docstatus = 1
					  AND EXISTS(
					      SELECT 1 FROM `tabProduct Details` pd
					      WHERE pd.parent = pc.name
					        AND (pd.manufacturing_work_order = %(mwo)s
					             OR pd.parent_manufacturing_order = %(pmo)s)
					  )
					  AND se_item.item_code = %(item_code)s
					ORDER BY se.creation DESC LIMIT 1
				""",
					{
						"mwo": self.manufacturing_work_order,
						"pmo": pmo,
						"item_code": item_code,
					},
					as_dict=1,
				)

				if pc_receive_data:
					candidate_wh = pc_receive_data[0].t_warehouse
					# Only use the PC Receive warehouse if the batch actually has stock
					# there. A later SE (e.g. pc_tagging_stock_sync return) may have
					# moved it back to Tagging, creating a different batch_no. Using
					# the stale PC WH in that case causes BatchNegativeStockError.
					batch_qty_at_candidate = (
						flt(get_batch_qty(batch_no, candidate_wh, item_code))
						if batch_no
						else flt(pc_receive_data[0].qty)
					)
					if batch_qty_at_candidate > 0:
						sre_reserved_qty_total = flt(pc_receive_data[0].qty)
						for row in rows:
							row["s_warehouse"] = candidate_wh
							_write_back_warehouse(row)
				else:
					# ── PRIORITY 3: Stock Entry linked to PMO ──
					se_wh = frappe.db.sql(
						"""
						SELECT sed.t_warehouse, sed.s_warehouse
						FROM `tabStock Entry Detail` sed
						JOIN `tabStock Entry` se ON se.name = sed.parent
						WHERE se.manufacturing_order = %s
						  AND sed.item_code = %s
						  AND se.docstatus = 1
						ORDER BY se.creation DESC
						LIMIT 1
					""",
						(self.parent_manufacturing_order, item_code),
						as_dict=True,
					)

					if se_wh:
						fallback_wh = se_wh[0].t_warehouse or se_wh[0].s_warehouse
						if fallback_wh:
							for row in rows:
								row["s_warehouse"] = fallback_wh
								_write_back_warehouse(row)

			# Group-level, so a warehouse split yields the SAME loss the single row would
			# have produced (3.557 + 0.02 consumed against 3.577 reserved -> zero loss).
			loss_qty = flt(sre_reserved_qty_total - group_qty, 3)

			if loss_qty > 0:
				variant_of = frappe.db.get_value("Item", item_code, "variant_of")
				loss_warehouse = None
				variant_loss_details = frappe.db.get_value(
					"Variant Loss Warehouse",
					{
						"parent": self.manufacturer,
						"variant": variant_of or item_code,
					},
					[
						"loss_warehouse",
						"consider_department_warehouse",
						"warehouse_type",
					],
					as_dict=1,
				)

				if variant_loss_details:
					if variant_loss_details.get("loss_warehouse"):
						loss_warehouse = variant_loss_details.get("loss_warehouse")
					elif variant_loss_details.get(
						"consider_department_warehouse"
					) and variant_loss_details.get("warehouse_type"):
						loss_warehouse = frappe.db.get_value(
							"Warehouse",
							{
								"disabled": 0,
								"department": self.department,
								"warehouse_type": variant_loss_details.get(
									"warehouse_type"
								),
							},
						)

				if loss_warehouse:
					# Duplicate guard: skip if a Repack entry already exists for this
					# SNC + item + batch. Keying on item alone would silently swallow a
					# genuine loss on a second batch of the same item and strand the metal
					# in WIP.
					existing_loss_se = frappe.db.sql(
						"""
						SELECT se.name
						FROM `tabStock Entry` se
						JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
						WHERE se.custom_serial_number_creator = %s
						  AND se.stock_entry_type = 'Repack'
						  AND se.docstatus != 2
						  AND sed.item_code = %s
						  AND sed.batch_no <=> %s
						LIMIT 1
						""",
						(self.name, item_code, batch_no),
					)
					if existing_loss_se:
						frappe.msgprint(
							_(
								"Repack (Loss) Stock Entry already exists for {0} (batch {1})"
							).format(item_code, batch_no or "-")
						)
					else:
						# Draw the loss from whatever reserved capacity the group did not
						# consume. A split group can leave residue in more than one
						# warehouse, and Repack cannot take more of a batch out of a
						# warehouse than it physically holds.
						residual = []
						for wh, cap, _sres in group_caps.get((item_code, batch_no), []):
							left = flt(flt(cap, 3) - flt(used.get(wh, 0)), 3)
							if left > TOLERANCE:
								residual.append((wh, left, _sres))
						loss_alloc = _allocate_qty_across_warehouses(
							loss_qty, residual
						) or [(rows[0]["s_warehouse"], loss_qty)]

						se_loss = frappe.new_doc("Stock Entry")
						se_loss.stock_entry_type = "Repack"
						se_loss.purpose = "Repack"
						se_loss.company = self.company
						se_loss.custom_serial_number_creator = self.name
						for loss_wh, loss_row_qty in loss_alloc:
							se_loss.append(
								"items",
								{
									"item_code": item_code,
									"qty": loss_row_qty,
									"s_warehouse": loss_wh,
									"t_warehouse": loss_warehouse,
									"batch_no": batch_no,
									"inventory_type": rows[0].get("inventory_type"),
									"customer": rows[0].get("customer"),
									"use_serial_batch_fields": 1,
								},
							)
						se_loss.insert(ignore_permissions=True)
						se_loss.submit()

			# (perf/lock-hold) Removed a per-row global frappe.clear_cache() here: it
			# wiped the entire cache on every source row, forcing cold re-reads for the
			# rest of the held-lock window. The only per-row staleness that matters (Bin
			# qty after SRE consume) is already handled by the scoped
			# clear_document_cache("Bin") at the consume step above.

		if bins_to_update:
			from erpnext.stock.utils import get_or_make_bin

			bin_names = sorted(
				list(set(get_or_make_bin(item, wh) for item, wh in bins_to_update))
			)
			for bin_name in bin_names:
				bin_doc = frappe.get_cached_doc("Bin", bin_name)
				bin_doc.update_reserved_stock()

		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_finished_goods_bom,
			create_manufacturing_entry,
		)

		se_name, fg_serial = create_manufacturing_entry(self, row_data, operation_data)

		self.fg_serial_no = fg_serial
		frappe.db.set_value(
			self.doctype,
			self.name,
			"fg_serial_no",
			fg_serial,
			update_modified=False,
		)
		create_finished_goods_bom(self, se_name, operation_data)
		submit_tracking_bom_for_finished_goods(self)

	if pmo:
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			set_values_in_bulk,
		)

		wo_list = frappe.get_all(
			"Manufacturing Work Order", {"manufacturing_order": pmo}, pluck="name"
		)
		set_values_in_bulk("Manufacturing Work Order", wo_list, {"status": "Completed"})

		# Mark all relevant virtual logs as synced now that they are physically consumed
		mop_names = [
			d.manufacturing_operation
			for d in operation_data
			if d.manufacturing_operation
		]
		if mop_names:
			frappe.db.sql(
				"""
				UPDATE `tabMOP Log`
				SET is_synced = 1
				WHERE manufacturing_operation IN %s
				  AND is_cancelled = 0
				  AND is_synced = 0
			""",
				(tuple(mop_names),),
			)


def get_shift(employee, start_date, end_date):
	Attendance = frappe.qb.DocType("Attendance")

	shift = (
		frappe.qb.from_(Attendance)
		.select(Attendance.shift)
		.distinct()
		.where(
			(Attendance.employee == employee)
			& (Attendance.attendance_date.between(start_date, end_date))
			& (Attendance.shift.notnull())
		)
	).run(pluck=True)

	if shift:
		return shift[0]

	return ""


def get_hourly_rate(employee):
	hourly_rate = 0
	start_date, end_date = get_first_day(nowdate()), get_last_day(nowdate())
	shift = get_shift(employee, start_date, end_date)
	shift_hours = (
		frappe.utils.flt(frappe.db.get_value("Shift Type", shift, "shift_hours")) or 10
	)

	base = frappe.db.get_value("Employee", employee, "ctc")

	holidays = get_holidays_for_employee(employee, start_date, end_date)
	working_days = date_diff(end_date, start_date) + 1

	working_days -= len(holidays)

	total_working_days = working_days
	target_working_hours = frappe.utils.flt(shift_hours * total_working_days)

	if target_working_hours:
		hourly_rate = frappe.utils.flt(base / target_working_hours)

	return hourly_rate


def get_holidays_for_employee(employee, start_date, end_date):
	from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
	from hrms.utils.holiday_list import get_holiday_dates_between

	HOLIDAYS_BETWEEN_DATES = "holidays_between_dates"

	holiday_list = get_holiday_list_for_employee(employee)
	key = f"{holiday_list}:{start_date}:{end_date}"
	holiday_dates = frappe.cache().hget(HOLIDAYS_BETWEEN_DATES, key)

	if not holiday_dates:
		holiday_dates = get_holiday_dates_between(holiday_list, start_date, end_date)
		frappe.cache().hset(HOLIDAYS_BETWEEN_DATES, key, holiday_dates)

	return holiday_dates


def validate_not_metal_only(doc):
	"""Prevent SNC submission when only metal items exist in source_table.

	A finished jewellery piece must contain additional materials (diamond,
	gemstone, finding, etc.) beyond just metal. This validation ensures
	incomplete compositions are caught before stock entries are created.
	"""
	has_metal = False
	has_non_metal = False
	for row in doc.source_table:
		if not row.row_material:
			continue
		qty = flt(row.qty)
		if qty <= 0:
			continue
		item_group = frappe.db.get_value("Item", row.row_material, "item_group") or ""
		if "Metal" in item_group:
			has_metal = True
		else:
			has_non_metal = True

	if has_metal and not has_non_metal:
		frappe.throw(
			_(
				"Submission not allowed. Only metal details are available. "
				"Additional manufacturing details (diamond, gemstone, finding) "
				"are required before submission."
			)
		)


def validate_qty(self):
	for row in self.source_table:
		if row.qty <= 0:
			frappe.throw(_("Source Table Quantity Zero or Negative Not Allowed"))
	for row in self.fg_details:
		if row.qty == 0:
			frappe.throw(_("FG Details Table Quantity Zero Not Allowed"))


@frappe.whitelist()
def get_operation_details(
	mwo,
	pmo,
	docname=None,
	company=None,
	mnf=None,
	dpt=None,
	for_fg=None,
	design_id_bom=None,
):
	exist_snc_doc = frappe.get_all(
		"Serial Number Creator",
		filters={"manufacturing_operation": docname, "docstatus": ["!=", 2]},
		fields=["name"],
	)
	if exist_snc_doc:
		frappe.throw(f"Document Already Created...! {exist_snc_doc[0]['name']}")
	snc_doc = frappe.new_doc("Serial Number Creator")
	snc_doc.type = "Manufacturing"
	snc_doc.manufacturing_work_order = mwo
	snc_doc.manufacturing_operation = docname
	snc_doc.parent_manufacturing_order = pmo
	snc_doc.company = company
	snc_doc.manufacturer = mnf
	snc_doc.department = dpt
	snc_doc.for_fg = for_fg
	snc_doc.design_id_bom = design_id_bom

	snc_doc.save(ignore_permissions=True)

	frappe.msgprint(
		f"<b>Serial Number Creator</b> Document Created...! <b>Doc NO:</b> {snc_doc.name}"
	)
	return snc_doc.name


def create_snc_from_mwo_submit(mwo_name: str) -> str:
	"""Automatically create SNC when a Work Order is submitted for the Serial No department."""
	mwo = frappe.get_doc("Manufacturing Work Order", mwo_name)
	if not cint(getattr(mwo, "for_fg", 0)):
		return ""

	# Check if SNC already exists for this MWO
	mop_name = cstr(getattr(mwo, "manufacturing_operation", None) or "").strip()
	if not mop_name:
		return ""

	exist_snc = frappe.db.get_value(
		"Serial Number Creator",
		{"manufacturing_work_order": mwo_name, "docstatus": ["!=", 2]},
		"name",
	)
	if exist_snc:
		return exist_snc

	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	balance_rows = get_current_mop_balance_rows(
		mop_name,
		include_fields=[
			"item_code",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
		],
	)

	has_metal = False
	has_non_metal = False

	if balance_rows:
		for r in balance_rows:
			item_code = r.get("item_code")
			qty = flt(r.get("qty_after_transaction_batch_based") or 0)
			pcs = flt(r.get("pcs_after_transaction_batch_based") or 0)
			if qty <= 0 and pcs <= 0:
				continue
			item_group = frappe.db.get_value("Item", item_code, "item_group") or ""
			if "Metal" in item_group:
				has_metal = True
			else:
				has_non_metal = True

		if has_metal and not has_non_metal:
			frappe.throw(
				_(
					"Only metal details available. Cannot create SNC because metal definitely combines with any of other items like diamond, gemstone, finding."
				)
			)

	pmo = frappe.db.get_value(
		"Manufacturing Work Order", mwo_name, "manufacturing_order"
	)
	snc = frappe.new_doc("Serial Number Creator")
	snc.type = "Manufacturing"
	snc.manufacturing_operation = mop_name
	snc.manufacturing_work_order = mwo_name
	snc.parent_manufacturing_order = pmo
	snc.company = mwo.company
	snc.manufacturer = mwo.manufacturer
	snc.department = mwo.department
	snc.for_fg = mwo.for_fg
	snc.design_id_bom = mwo.master_bom
	snc.total_weight = 0

	snc.flags.ignore_mandatory = True
	snc.insert(ignore_permissions=True)

	return snc.name


def calulate_id_wise_sum_up(self):
	"""Validate that fg_details totals per item match source_table totals per item.

	fg_details has aggregated qty per item_code (no batch split).
	source_table has batch-wise qty per (item_code, batch_no).
	The sum of qty per item_code in both tables must match.
	"""
	# Sum qty per item in fg_details
	fg_qty_sum = {}
	for row in self.fg_details:
		if row.row_material:
			key = row.row_material
			if key not in fg_qty_sum:
				fg_qty_sum[key] = float(Decimal("0.000"))
			fg_qty_sum[key] += float(
				Decimal(str(row.qty)).quantize(Decimal("0.000"), rounding=ROUND_HALF_UP)
			)
	fg_qty_sum = {key: round(float(value), 3) for key, value in fg_qty_sum.items()}

	# Sum qty per item in source_table (batch-wise rows aggregated by item)
	source_data = frappe._dict()
	for row in self.source_table:
		source_data.setdefault(row.get("row_material"), 0)
		source_data[row.row_material] += row.qty

	for row_material, qty_sum in fg_qty_sum.items():
		src_qty = flt(source_data.get(row_material), 3)
		if src_qty and flt(qty_sum, 3) != src_qty:
			frappe.throw(
				f"Row Material in FG Details <b>{row_material}</b> does not match </br></br>"
				f"FG Details SUM: <b>{round(qty_sum, 3)}</b></br>"
				f"Source Table SUM: <b>{src_qty}</b>"
			)


def update_new_serial_no(self):
	new_sn_doc = frappe.get_doc("Serial No", self.fg_serial_no)
	customer = frappe.db.get_value(
		"Parent Manufacturing Order", self.parent_manufacturing_order, "customer"
	)
	if customer:
		new_sn_doc.customer = customer
	existing_huid = []
	existing_certification = []

	for row in new_sn_doc.huid:
		if row.huid and row.huid not in existing_huid:
			existing_huid.append(row.huid)

		if row.certification_no and row.certification_no not in existing_certification:
			existing_certification.append(row.certification_no)

	pmo_data = frappe.db.get_all(
		"HUID Detail",
		{"parent": self.parent_manufacturing_order},
		["huid", "date", "certification_no", "certification_date"],
	)

	item_to_add = []
	for row in pmo_data:
		if row.huid and row.huid not in existing_huid:
			duplicate_row = deepcopy(row)
			duplicate_row["name"] = None
			item_to_add.append(duplicate_row)

	for row in item_to_add:
		new_sn_doc.append(
			"huid",
			{
				"huid": row.huid,
				"date": row.date,
				"certification_no": row.certification_no,
				"certification_date": row.certification_date,
			},
		)
	new_sn_doc.save()

	if self.serial_no and self.fg_details and self.fg_details[0].serial_no:
		serial_doc = frappe.get_doc("Serial No", self.fg_details[0].serial_no)
		previos_sr = frappe.db.get_value(
			"Serial No",
			self.serial_no,
			[
				"purchase_document_no",
				"item_code",
				"custom_repair_type",
				"custom_product_type",
			],
			as_dict=1,
		)

		huid_details = ""
		certificate_details = ""
		for row in frappe.db.get_all("HUID Detail", {"parent": self.serial_no}, ["*"]):
			if row.huid:
				huid_details += """
								{0} - {1}""".format(row.huid, row.date)
			if row.certification_no:
				certificate_details += """
								{0} - {1}""".format(
					row.certification_no, row.certification_date
				)

		for row in frappe.db.get_all(
			"Serial No Table", {"parent": self.serial_no}, ["*"]
		):
			temp_row = deepcopy(row)
			temp_row["name"] = None
			serial_doc.append("custom_serial_no_table", temp_row)

		serial_doc.append(
			"custom_serial_no_table",
			{
				"serial_no": self.serial_no,
				"item_code": previos_sr.item_code,
				"purchase_document_no": previos_sr.purchase_document_no,
				"pmo": self.parent_manufacturing_order,
				"mwo": self.manufacturing_work_order,
				"bom": self.design_id_bom,
				"huid_details": huid_details,
				"certification_details": certificate_details,
				"repair_type": previos_sr.get("repair_type"),
				"product_type": previos_sr.get("product_type"),
			},
		)
		serial_doc.save()


def submit_tracking_bom_for_finished_goods(doc):
	"""Update and submit linked Tracking BOM when SNC creates FG BOM."""
	if not doc.get("fg_bom"):
		return

	tracking_bom_name = frappe.db.get_value(
		"Manufacturing Work Order", doc.manufacturing_work_order, "custom_tracking_bom"
	)
	if not tracking_bom_name and doc.get("parent_manufacturing_order"):
		tracking_bom_name = frappe.db.get_value(
			"Parent Manufacturing Order",
			doc.parent_manufacturing_order,
			"custom_tracking_bom",
		)
	if not tracking_bom_name:
		return

	tracking_bom = frappe.get_doc("Tracking Bom", tracking_bom_name)
	if tracking_bom.docstatus == 0:
		tracking_bom.bom_type = "Finished Goods"
		tracking_bom.reference_doctype = "BOM"
		tracking_bom.reference_docname = doc.fg_bom
		tracking_bom.flags.ignore_validate_update_after_submit = True
		tracking_bom.save(ignore_permissions=True)
		tracking_bom.submit()
	else:
		frappe.db.set_value(
			"Tracking Bom",
			tracking_bom_name,
			{
				"bom_type": "Finished Goods",
				"reference_doctype": "BOM",
				"reference_docname": doc.fg_bom,
			},
			update_modified=True,
		)


# def _resolve_mwo_qty(mwo):
# 	# MWO.qty is the number of pieces / manufacturing qty used for SNC ID splits.
# 	return getattr(mwo, "qty", None)


# def _resolve_snc_mnf_qty(snc_doc):
# 	# Prefer MWO qty if possible
# 	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
# 	if mwo_name:
# 		qty = frappe.db.get_value("Manufacturing Work Order", mwo_name, "qty")
# 		if qty is not None:
# 			return qty

# 	ids = {cstr(r.get("id")) for r in (snc_doc.get("fg_details") or []) if r.get("id")}
# 	return len(ids) or 1


# def _resolve_snc_mop(snc_doc):
# 	# Prefer explicit field if present, else derive from MWO
# 	mop_name = cstr(getattr(snc_doc, "manufacturing_operation", None) or "").strip()
# 	if mop_name:
# 		return mop_name
# 	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
# 	if not mwo_name:
# 		return ""
# 	return cstr(
# 		frappe.db.get_value(
# 			"Manufacturing Work Order", mwo_name, "manufacturing_operation"
# 		)
# 		or ""
# 	).strip()


# def _get_mop_is_sync(mop_name: str) -> int:
# 	"""Check if there are any non-cancelled logs for this MOP that are marked as 'is_synced'."""
# 	if not mop_name:
# 		return 0
# 	return (
# 		1
# 		if frappe.db.exists(
# 			"MOP Log",
# 			{"manufacturing_operation": mop_name, "is_synced": 1, "is_cancelled": 0},
# 		)
# 		else 0
# 	)


def _get_source_raw_materials(mop_name, snc_doc):
	"""Get batch-wise source raw materials from MOP Log for a Manufacturing Operation.

	Monitors all MOP Log flow_index entries to capture intermediate Stock Entry
	additions. Checks Stock Reservation Entry for Sales Order warehouse.

	Returns a list of dicts with: item_code, batch_no, qty, uom, pcs,
	inventory_type, customer, s_warehouse, sub_setting_type, sed_item.
	"""
	if not mop_name:
		return []

	# Get current balance rows from MOP Log (latest per item/batch)
	balance_rows = get_current_mop_balance_rows(
		mop_name,
		include_fields=[
			"item_code",
			"batch_no",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
			"serial_and_batch_bundle",
			"voucher_type",
			"voucher_no",
			"row_name",
			"from_warehouse",
			"to_warehouse",
			"manufacturing_work_order",
			"flow_index",
		],
	)
	if not balance_rows:
		return []

	# Resolve PMO and Sales Order for SRE lookup
	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
	pmo = None
	sales_order = None
	if mwo_name:
		pmo = frappe.db.get_value(
			"Manufacturing Work Order", mwo_name, "manufacturing_order"
		)
	if pmo:
		sales_order = frappe.db.get_value(
			"Parent Manufacturing Order", pmo, "sales_order"
		)

	# Get all MWOs for the PMO (for physical warehouse fallback)
	# all_mwos = []
	# if pmo:
	# 	all_mwos = frappe.get_all(
	# 		"Manufacturing Work Order",
	# 		{"manufacturing_order": pmo, "docstatus": 1},
	# 		pluck="name",
	# 	)

	out = []
	for r in balance_rows:
		item_code = r.get("item_code")
		batch_no = r.get("batch_no")
		qty = flt(r.get("qty_after_transaction_batch_based") or 0)
		pcs = flt(r.get("pcs_after_transaction_batch_based") or 0)
		# Skip rows with no weight. A real consumable raw material always carries a
		# weight (gold in grams, diamonds in carats). A qty-0 / pcs>0 balance row is
		# a tracking artifact — e.g. a finished-gold finding piece that flowed
		# through an operation as 1 pcs with no weight of its own — and must not
		# become a source/FG line (it would produce a zero-qty Stock Entry item).
		if qty <= 0:
			continue

		uom = frappe.db.get_value("Item", item_code, "stock_uom") if item_code else None

		# Fetch attributes from source Stock Entry Detail if available
		sub_setting_type = None
		inventory_type = None
		customer = None
		if r.get("voucher_type") == "Stock Entry" and r.get("row_name"):
			sed_data = frappe.db.get_value(
				"Stock Entry Detail",
				r.get("row_name"),
				["inventory_type", "custom_sub_setting_type", "customer"],
				as_dict=1,
			)
			if sed_data and r.get("voucher_type") == "Stock Entry":
				sub_setting_type = sed_data.custom_sub_setting_type
				inventory_type = sed_data.inventory_type
				customer = sed_data.customer

		s_wh = None
		# ── Warehouse resolution for SNC fetch (same priorities as submit) ──

		# Priority 1: SRE — fetch from active Stock Reservation Entries
		# linked to all MWOs under this PMO for the given item
		if pmo:
			all_pmo_mwos = frappe.get_all(
				"Manufacturing Work Order",
				{"manufacturing_order": pmo, "docstatus": 1},
				pluck="name",
			)
			if not all_pmo_mwos:
				all_pmo_mwos = (
					[snc_doc.manufacturing_work_order]
					if snc_doc.manufacturing_work_order
					else []
				)

			if all_pmo_mwos:
				linked_sres = frappe.get_all(
					"Stock Reservation Entry",
					filters={
						"item_code": item_code,
						"docstatus": 1,
						"manufacturing_work_order": ["in", all_pmo_mwos],
					},
					fields=["warehouse"],
				)
				if linked_sres:
					for sre in linked_sres:
						if sre.warehouse:
							s_wh = sre.warehouse
							break

		if not s_wh:
			# Priority 2: PC Receive — check Product Certification receive entries
			pc_receive_data = frappe.db.sql(
				"""
				SELECT se_item.t_warehouse
				FROM `tabStock Entry` se
				JOIN `tabStock Entry Detail` se_item ON se.name = se_item.parent
				JOIN `tabProduct Certification` pc ON se.product_certification = pc.name
				WHERE pc.type = 'Receive'
				  AND se.docstatus = 1
				  AND EXISTS(
					  SELECT 1 FROM `tabProduct Details` pd
					  WHERE pd.parent = pc.name
						AND (pd.manufacturing_work_order = %(mwo)s
							 OR pd.parent_manufacturing_order = %(pmo)s)
				  )
				  AND se_item.item_code = %(item_code)s
				ORDER BY se.creation DESC LIMIT 1
			""",
				{
					"mwo": snc_doc.manufacturing_work_order,
					"pmo": pmo,
					"item_code": item_code,
				},
				as_dict=1,
			)
			if pc_receive_data and pc_receive_data[0].t_warehouse:
				s_wh = pc_receive_data[0].t_warehouse

		if not s_wh:
			# Priority 3: Stock Entry linked to PMO
			se_wh = frappe.db.sql(
				"""
				SELECT sed.t_warehouse, sed.s_warehouse
				FROM `tabStock Entry Detail` sed
				JOIN `tabStock Entry` se ON se.name = sed.parent
				WHERE se.manufacturing_order = %s
				  AND sed.item_code = %s
				  AND se.docstatus = 1
				ORDER BY se.creation DESC
				LIMIT 1
			""",
				(snc_doc.parent_manufacturing_order, item_code),
				as_dict=True,
			)
			if se_wh:
				s_wh = se_wh[0].t_warehouse or se_wh[0].s_warehouse

		if not s_wh:
			s_wh = resolve_and_validate(
				item_code=item_code,
				qty=qty,
				batch_no=batch_no,
				sales_order=sales_order,
				mwo=mwo_name,
				mop=mop_name,
			)

		if not s_wh:
			s_wh = r.get("to_warehouse")

		out.append(
			{
				"item_code": item_code,
				"batch_no": batch_no,
				"qty": qty,
				"uom": uom,
				"pcs": pcs,
				"inventory_type": inventory_type,
				"customer": customer,
				"sub_setting_type": sub_setting_type,
				"sed_item": r.get("row_name")
				if r.get("voucher_type") == "Stock Entry"
				else None,
				"s_warehouse": s_wh or r.get("to_warehouse"),
				"serial_and_batch_bundle": r.get("serial_and_batch_bundle"),
			}
		)
	return out


def _resolve_snc_mnf_qty(snc_doc):
	"""Resolve the manufacturing quantity for SNC ID splits.

	Prefers MWO qty if available, otherwise defaults to 1.
	"""
	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
	if mwo_name:
		qty = frappe.db.get_value("Manufacturing Work Order", mwo_name, "qty")
		if qty is not None:
			return int(flt(qty)) or 1
	return 1


def _append_fg_rows_aggregated(snc_doc, source_rows, mnf_qty: int):
	"""Append fg_details rows aggregated by item_code (no batch splitting).

	Each unique item_code gets one row per mnf_id with qty/pcs split evenly.
	The last ID gets the remainder to avoid rounding errors.
	"""
	# Aggregate by item_code
	item_agg = {}
	for row in source_rows:
		key = row.get("item_code")
		if key not in item_agg:
			item_agg[key] = {
				"qty": 0,
				"pcs": 0,
				"uom": row.get("uom"),
				"sub_setting_type": row.get("sub_setting_type"),
			}
		item_agg[key]["qty"] += flt(row.get("qty") or 0)

		# Diamond/Gemstone items (prefix D or G): each batch is a distinct physical
		# stone, so pcs should be summed across batches.
		# Metal and all other items: multiple batches are weight splits of the SAME
		# physical piece, so use max() to avoid double-counting the pcs.
		first_char = (key or "")[0].upper() if key else ""
		if first_char in ("D", "G"):
			item_agg[key]["pcs"] += flt(row.get("pcs") or 0)
		else:
			item_agg[key]["pcs"] = max(item_agg[key]["pcs"], flt(row.get("pcs") or 0))

	# Split across mnf_qty IDs
	for mnf_id in range(1, int(mnf_qty) + 1):
		for item_code, agg in item_agg.items():
			total_qty = flt(agg["qty"])
			total_pcs = flt(agg["pcs"])

			_qty = flt(total_qty / mnf_qty, 3)
			_pcs = flt(total_pcs / mnf_qty, 3)

			if mnf_id == mnf_qty:
				# Last ID gets remainder
				already_allocated_qty = flt(_qty * (mnf_qty - 1), 3)
				already_allocated_pcs = flt(_pcs * (mnf_qty - 1), 3)
				_qty = flt(total_qty - already_allocated_qty, 3)
				_pcs = flt(total_pcs - already_allocated_pcs, 3)

			snc_doc.append(
				"fg_details",
				{
					"row_material": item_code,
					"id": mnf_id,
					"qty": _qty,
					"uom": agg["uom"],
					"pcs": _pcs,
					"sub_setting_type": agg.get("sub_setting_type"),
				},
			)


def get_correct_source_warehouse(
	item_code, batch_no=None, sales_order=None, mwo=None, mop=None
):
	"""Priority-based warehouse resolution from SREs."""

	# Priority 1: SRE for Sales Order (Submitted)
	if sales_order:
		if batch_no:
			wh = frappe.db.sql(
				"""
				SELECT sre.warehouse
				FROM `tabSerial and Batch Entry` sbe
				JOIN `tabStock Reservation Entry` sre ON sre.name = sbe.parent
				WHERE sbe.parenttype = 'Stock Reservation Entry'
				  AND sbe.batch_no = %s
				  AND sre.item_code = %s
				  AND sre.voucher_no = %s
				  AND sre.docstatus = 1
				LIMIT 1
			""",
				(batch_no, item_code, sales_order),
			)
			if wh:
				return wh[0][0], "SRE"

		wh = frappe.db.get_value(
			"Stock Reservation Entry",
			{"item_code": item_code, "voucher_no": sales_order, "docstatus": 1},
			"warehouse",
		)
		if wh:
			return wh, "SRE"

	# Priority 2: Other specific links (MWO, MOP) (Submitted)
	for field, val in [
		("manufacturing_work_order", mwo),
		("manufacturing_operation", mop),
	]:
		if not val:
			continue
		if batch_no:
			wh = frappe.db.sql(
				f"""
				SELECT sre.warehouse
				FROM `tabSerial and Batch Entry` sbe
				JOIN `tabStock Reservation Entry` sre ON sre.name = sbe.parent
				WHERE sbe.parenttype = 'Stock Reservation Entry'
				  AND sbe.batch_no = %s
				  AND sre.item_code = %s
				  AND sre.{field} = %s
				  AND sre.docstatus = 1
				LIMIT 1
			""",
				(batch_no, item_code, val),
			)
			if wh:
				return wh[0][0], "SRE"

		wh = frappe.db.get_value(
			"Stock Reservation Entry",
			{"item_code": item_code, field: val, "docstatus": 1},
			"warehouse",
		)
		if wh:
			return wh, "SRE"

	# Priority 2.5: Product Certification Receive warehouse
	# When PC happens before SNC, SREs are cancelled during PC Issue and
	# stock is moved to a WIP warehouse, then back to the department
	# warehouse via PC Receive. The PC Receive t_warehouse is the
	# definitive location of the stock after certification.
	if mwo:
		pmo_for_pc = frappe.db.get_value(
			"Manufacturing Work Order", mwo, "manufacturing_order"
		)
		if pmo_for_pc:
			pc_wh = frappe.db.sql(
				"""
				SELECT se_item.t_warehouse
				FROM `tabStock Entry` se
				JOIN `tabStock Entry Detail` se_item ON se.name = se_item.parent
				JOIN `tabProduct Certification` pc ON se.product_certification = pc.name
				WHERE pc.type = 'Receive'
				  AND se.docstatus = 1
				  AND EXISTS(
				      SELECT 1 FROM `tabProduct Details` pd
				      WHERE pd.parent = pc.name
				        AND (pd.manufacturing_work_order = %s
				             OR pd.parent_manufacturing_order = %s)
				  )
				  AND se_item.item_code = %s
				ORDER BY se.creation DESC LIMIT 1
				""",
				(mwo, pmo_for_pc, item_code),
			)
			if pc_wh:
				return pc_wh[0][0], "PC_RECEIVE"

	# Priority 3: Cancelled SRE trace (Recently released stock)
	if sales_order:
		if batch_no:
			wh = frappe.db.sql(
				"""
				SELECT sre.warehouse
				FROM `tabSerial and Batch Entry` sbe
				JOIN `tabStock Reservation Entry` sre ON sre.name = sbe.parent
				WHERE sbe.parenttype = 'Stock Reservation Entry'
				  AND sbe.batch_no = %s
				  AND sre.item_code = %s
				  AND sre.voucher_no = %s
				  AND sre.docstatus = 2
				LIMIT 1
			""",
				(batch_no, item_code, sales_order),
			)
			if wh:
				return wh[0][0], "SRE"

		wh = frappe.db.get_value(
			"Stock Reservation Entry",
			{"item_code": item_code, "voucher_no": sales_order, "docstatus": 2},
			"warehouse",
		)
		if wh:
			return wh, "SRE"

	# Priority 4: Latest Stock Movement (Fallback if no reservation exists)
	sle_wh = frappe.db.sql(
		"""SELECT warehouse FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND (batch_no=%s OR %s IS NULL) AND is_cancelled=0
		ORDER BY posting_date DESC, posting_time DESC, creation DESC LIMIT 1""",
		(item_code, batch_no, batch_no),
	)
	if sle_wh:
		return sle_wh[0][0], "SLE"

	return None, None


def resolve_and_validate(
	item_code, qty, batch_no=None, sales_order=None, mwo=None, mop=None
):
	"""Combined resolution + stock validation with auto-recovery."""
	wh, source_type = get_correct_source_warehouse(
		item_code, batch_no, sales_order, mwo, mop
	)

	if not wh:
		return None

	# If resolved via SRE or PC Receive, we trust it as the source warehouse
	if source_type in ("SRE", "PC_RECEIVE"):
		return wh

	def get_available_qty(w):
		bin_data = frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": w},
			["actual_qty", "reserved_stock"],
			as_dict=1,
		)
		if not bin_data:
			return 0

		return flt(bin_data.actual_qty) - flt(bin_data.reserved_stock)

	if get_available_qty(wh) >= flt(qty):
		return wh

	# Search for any warehouse that has enough AVAILABLE stock of this BATCH
	if batch_no:
		alt_batch = frappe.db.sql(
			"""
			SELECT warehouse, SUM(actual_qty) as total_qty
			FROM `tabStock Ledger Entry`
			WHERE item_code = %s AND batch_no = %s AND is_cancelled = 0
			GROUP BY warehouse
			HAVING total_qty >= %s
			ORDER BY total_qty DESC
			LIMIT 1
		""",
			(item_code, batch_no, qty),
			as_dict=True,
		)
		if alt_batch:
			return alt_batch[0].warehouse

	# Fallback to any warehouse with available ITEM stock
	alt = frappe.db.sql(
		"""SELECT warehouse FROM `tabBin`
		WHERE item_code=%s AND (actual_qty - reserved_stock) >= %s
		ORDER BY (actual_qty - reserved_stock) DESC LIMIT 1""",
		(item_code, qty),
	)
	if alt:
		return alt[0][0]

	return wh  # Fallback to original even if short
