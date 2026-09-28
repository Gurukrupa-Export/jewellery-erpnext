# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Where each customer-gold receipt row is now, and what happened to it. Read-only.

WHY A REPLAY AND NOT A LOOKUP
-----------------------------
The custody ledger records events per BATCH. A receipt's gold does not stay in its batch: a
Metal Conversion consumes the 24KT receipt batch and mints a 22KT child, transfers split that
child across departments, and a Serial Number Creator folds part of it into a finished piece.
Nothing stored links the child back to the receipt row -- ``cg_source_event`` has no writer,
and ``Batch Component`` is replaced on every production -- so filtering the original batch shows
an empty batch while the customer's gold sits in three warehouses under two other names.

So this module rebuilds the answer from the stock ledger itself, which is immutable and already
carries every movement: it replays the Serial and Batch Entries of the receipt batches and of
every batch produced from them, in posting order, and follows each receipt row's share through
every hop.

THE ALLOCATION RULE, STATED ONCE
--------------------------------
Within one (batch, warehouse) holding, metal is treated as MIXED: an outflow of q from a holding
of Q carries q/Q of every receipt's share with it. A production voucher pools each ownership
lane's inputs and hands the pool to that lane's outputs, weighted by output fine gold (or by
quantity when an output has no purity -- a finished piece). This is a pro-rata rule, it is
deterministic, and it conserves every receipt's measure exactly; results built on it are labelled
``derived``. It is not FIFO and does not claim to know which atom went where.

Each receipt is measured in fine grams when its item has a purity, otherwise in its own stock
quantity (a customer's diamonds in carats). The two never mix: every share is keyed by receipt.

WHAT THIS MODULE NEVER DOES
---------------------------
It writes nothing. Opening a report, refreshing it or exporting it must not create a single
row anywhere, and nothing here calls a function that can.
"""

from collections import defaultdict

import frappe
from frappe.utils import flt, get_datetime

from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
	CUSTOMER_GOODS,
	EVENT_RECEIPT,
	EVENT_REVERSAL,
	LEDGER_DOCTYPE,
	PROCESS_LOSS_SE_TYPE,
	warehouse_stage,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)

REGULAR_STOCK = "Regular Stock"
#: Prefix of the holding id used for a finished piece that has a Serial No but no batch.
SERIAL_PREFIX = "SN::"
PRODUCING_PURPOSES = ("Repack", "Manufacture")

#: Below this, a share is rounding residue, not metal. Quantities are stored to 3 dp.
EPSILON = 1e-9

# Voucher classes.
KIND_RECEIPT = "receipt"
KIND_PRODUCTION = "production"
KIND_MOVEMENT = "movement"
KIND_RECONCILIATION = "reconciliation"

# Where metal went when it left the company's custody, or left the lane.
DISPOSITION_DELIVERED = "delivered"
DISPOSITION_RETURNED = "returned"
DISPOSITION_LOSS = "loss"
DISPOSITION_OTHER = "other"
DISPOSITIONS = (
	DISPOSITION_DELIVERED,
	DISPOSITION_RETURNED,
	DISPOSITION_LOSS,
	DISPOSITION_OTHER,
)

# Exceptions the replay surfaces instead of hiding.
EXC_UNKNOWN_OUTFLOW = "outflow exceeds traced stock"
EXC_LANE_WITHOUT_OUTPUT = "customer lane consumed with no output in the same lane"
EXC_UNATTRIBUTED_RETURN = "returned inflow with no earlier attribution for the batch"
EXC_RECONCILIATION = "stock reconciliation changed a traced holding"


def lane_key(inventory_type, customer):
	"""Same string ``metal_conversions.lane_tag`` stamps, so an explicit tag and an owner-derived
	fallback compare equal."""
	return f"{inventory_type or REGULAR_STOCK}|{customer or ''}"


class Holding:
	__slots__ = ("qty", "shares")

	def __init__(self):
		self.qty = 0.0
		self.shares = defaultdict(float)

	def take(self, qty):
		"""Remove ``qty`` pro-rata; return (shares removed, quantity not covered by the holding)."""
		if self.qty <= EPSILON:
			return {}, qty
		covered = min(qty, self.qty)
		fraction = covered / self.qty
		moved = {}
		for key, amount in list(self.shares.items()):
			part = amount * fraction
			if abs(part) > EPSILON:
				moved[key] = part
			self.shares[key] = amount - part
			if abs(self.shares[key]) <= EPSILON:
				del self.shares[key]
		self.qty -= covered
		if self.qty <= EPSILON:
			self.qty = 0.0
			self.shares.clear()
		return moved, qty - covered

	def take_preferring(self, qty, key, per_qty):
		"""Remove ``qty``, drawing on ``key``'s share first and only the rest pro-rata.

		A raw return names the receipt row it gives back. Out of a batch two receipts share, the
		metal returned is that receipt's -- charging part of it to the other receipt would cut an
		entitlement nobody drew on. ``per_qty`` converts stock quantity to the receipt's measure.
		"""
		if self.qty <= EPSILON:
			return {}, qty
		covered = min(qty, self.qty)
		moved = {}
		first = 0.0
		if per_qty and self.shares.get(key, 0.0) > EPSILON:
			first = min(covered, self.shares[key] / per_qty)
			moved[key] = first * per_qty
			self.shares[key] -= moved[key]
			if abs(self.shares[key]) <= EPSILON:
				del self.shares[key]
			self.qty -= first
		rest = covered - first
		if rest > EPSILON and self.qty > EPSILON:
			others, _ = self.take(min(rest, self.qty))
			for other, amount in others.items():
				moved[other] = moved.get(other, 0.0) + amount
		if self.qty <= EPSILON:
			self.qty = 0.0
			self.shares.clear()
		return moved, qty - covered

	def put(self, qty, shares):
		self.qty += qty
		for key, amount in (shares or {}).items():
			self.shares[key] += amount


class AttributionReplay:
	"""The pure engine. Knows nothing about the database; the loader feeds it movements.

	``receipts`` maps a receipt key to its measure (``unit`` = "fine" or "qty"). ``movements``
	is a list of dicts, already in posting order and grouped per voucher by the loader::

	    {voucher_type, voucher_no, detail_no, posting, batch_no, warehouse, item_code,
	     qty (signed stock qty), purity (or None), kind, lane, disposition, receipt_key,
	     is_return, is_loss}
	"""

	def __init__(self, receipts):
		self.receipts = receipts
		self.holdings = defaultdict(Holding)  # (batch, warehouse) -> Holding
		self.dispositions = defaultdict(
			lambda: defaultdict(float)
		)  # key -> kind -> amount
		self.trail = []  # every step that moved a receipt's share
		self.exceptions = []
		self.batch_origin = {}  # batch -> (voucher_no, generation, item_code)
		self.last_fraction = {}  # batch -> {key: share per unit} as it left on a DELIVERY
		self.loss_batches = set()
		self.lane_fine = []  # production data-quality: per voucher lane, input vs output fine
		self.received = defaultdict(float)
		self.revaluation_fractions = {}  # voucher_no -> batch -> {key: fraction}

	# -- measures ---------------------------------------------------------------------------

	def _measure(self, receipt_key, qty, purity):
		"""The receipt's measure for ``qty`` of its own item. Fine gold per unit comes from the
		receipt's ledger event when it was recorded -- the purity in force THEN -- so a later edit
		of a purity master does not silently restate history."""
		receipt = self.receipts[receipt_key]
		if receipt["unit"] != "fine":
			return flt(qty)
		per_unit = receipt.get("per_unit")
		if per_unit:
			return flt(qty) * flt(per_unit)
		return flt(qty) * flt(purity) / 100.0

	def _per_qty(self, receipt_key, purity):
		receipt = self.receipts.get(receipt_key) or {}
		if receipt.get("unit") != "fine":
			return 1.0
		if purity:
			return flt(purity) / 100.0
		return flt(receipt.get("per_unit")) or None

	# -- driver -----------------------------------------------------------------------------

	def run(self, movements):
		for voucher_rows in _group_by_voucher(movements):
			kind = voucher_rows[0]["kind"]
			if kind == KIND_RECEIPT:
				self._receipt(voucher_rows)
			elif kind == KIND_PRODUCTION:
				self._production(voucher_rows)
			elif kind == KIND_RECONCILIATION:
				self._reconciliation(voucher_rows)
			else:
				self._movement(voucher_rows)
		return self

	# -- voucher handlers -------------------------------------------------------------------

	def _receipt(self, rows):
		for row in rows:
			key = row.get("receipt_key")
			if row["qty"] > 0 and key in self.receipts:
				amount = self._measure(key, row["qty"], row.get("purity"))
				self._put(row, row["qty"], {key: amount})
				self.received[key] += amount
				self.batch_origin.setdefault(
					row["batch_no"], (row["voucher_no"], 0, row.get("item_code"))
				)
				self._log(row, "Receipt", {key: amount}, to_warehouse=row["warehouse"])
			elif row["qty"] > 0:
				# Another row of the same voucher (another customer, or a row whose receipt event
				# was reversed): stock arrives, owned by nobody this trace follows.
				self._put(row, row["qty"], {})
			else:
				self._outflow(row, DISPOSITION_OTHER)

	def _production(self, rows):
		inputs = defaultdict(list)
		outputs = defaultdict(list)
		for row in rows:
			(inputs if row["qty"] < 0 else outputs)[row.get("lane")].append(row)

		for lane in set(inputs) | set(outputs):
			pool = defaultdict(float)
			fine_in = 0.0
			fine_in_known = True
			for row in inputs.get(lane, []):
				moved = self._take(row, -row["qty"])
				for key, amount in moved.items():
					pool[key] += amount
				if row.get("purity"):
					fine_in += -row["qty"] * flt(row["purity"]) / 100.0
				else:
					fine_in_known = False
				if moved:
					self._log(row, "Consumed", moved, from_warehouse=row["warehouse"])

			lane_outputs = outputs.get(lane, [])
			if not lane_outputs:
				if pool:
					disposition = (
						DISPOSITION_LOSS
						if rows[0].get("is_loss")
						else DISPOSITION_OTHER
					)
					for key, amount in pool.items():
						self.dispositions[key][disposition] += amount
					if disposition == DISPOSITION_OTHER:
						self.exceptions.append(
							_exception(
								rows[0], EXC_LANE_WITHOUT_OUTPUT, pool, lane=lane
							)
						)
				continue

			weights = _output_weights(lane_outputs)
			total_weight = sum(weights) or 0.0
			fine_out = sum(
				row["qty"] * flt(row["purity"]) / 100.0
				for row in lane_outputs
				if row.get("purity")
			)
			if (
				pool
				and fine_in_known
				and all(row.get("purity") for row in lane_outputs)
			):
				self.lane_fine.append(
					{
						"voucher_no": rows[0]["voucher_no"],
						"lane": lane,
						"fine_in": fine_in,
						"fine_out": fine_out,
					}
				)

			for row, weight in zip(lane_outputs, weights, strict=True):
				share = (
					(weight / total_weight) if total_weight else 1.0 / len(lane_outputs)
				)
				given = {key: amount * share for key, amount in pool.items()}
				self._put(row, row["qty"], given)
				if rows[0].get("is_loss"):
					self.loss_batches.add(row["batch_no"])
				if given:
					parent = None
					for source in inputs.get(lane, []):
						parent = self.batch_origin.get(source["batch_no"])
						if parent:
							break
					generation = (parent[1] + 1) if parent else 1
					self.batch_origin.setdefault(
						row["batch_no"],
						(row["voucher_no"], generation, row.get("item_code")),
					)
					self._log(row, "Produced", given, to_warehouse=row["warehouse"])

	def _movement(self, rows):
		by_detail = defaultdict(list)
		for row in rows:
			by_detail[(row.get("detail_no"), row["batch_no"])].append(row)

		for group in by_detail.values():
			outs = [r for r in group if r["qty"] < 0]
			ins = [r for r in group if r["qty"] > 0]
			if outs and ins:
				# A transfer: the same batch leaves one warehouse and arrives in another, and
				# carries exactly its share with it.
				for out_row in outs:
					moved = self._take(out_row, -out_row["qty"])
					remaining = -out_row["qty"]
					for in_row in ins:
						if remaining <= EPSILON:
							break
						portion = min(in_row["qty"], remaining) / (-out_row["qty"])
						given = {k: v * portion for k, v in moved.items()}
						self._put(in_row, min(in_row["qty"], remaining), given)
						remaining -= min(in_row["qty"], remaining)
						if given:
							self._log(
								in_row,
								"Transfer",
								given,
								from_warehouse=out_row["warehouse"],
								to_warehouse=in_row["warehouse"],
							)
				continue

			for row in outs:
				self._outflow(row, row.get("disposition") or DISPOSITION_OTHER)
			for row in ins:
				self._inflow(row)

	def _reconciliation(self, rows):
		# A Stock Reconciliation rewrites a batch as outward-then-inward of the same quantity when
		# only its value changes -- a revaluation. That is no movement at all. Anything else is
		# a genuine quantity change nobody can attribute, and is said out loud.
		net = defaultdict(float)
		for row in rows:
			net[(row["batch_no"], row["warehouse"])] += row["qty"]
			holding = self.holdings.get((row["batch_no"], row["warehouse"]))
			if holding and holding.qty > EPSILON:
				self.revaluation_fractions.setdefault(row["voucher_no"], {})[
					row["batch_no"]
				] = {
					key: amount / holding.qty for key, amount in holding.shares.items()
				}
		for (batch_no, warehouse), qty in net.items():
			if abs(qty) <= EPSILON:
				continue
			row = next(
				r
				for r in rows
				if r["batch_no"] == batch_no and r["warehouse"] == warehouse
			)
			shaped = dict(row, qty=qty)
			if qty < 0:
				moved = self._outflow(shaped, DISPOSITION_OTHER)
			else:
				moved = self._put(shaped, qty, {})
			self.exceptions.append(
				_exception(row, EXC_RECONCILIATION, moved or {}, qty=qty)
			)

	# -- primitives -------------------------------------------------------------------------

	def _take(self, row, qty, prefer=None):
		holding = self.holdings[(row["batch_no"], row["warehouse"])]
		if prefer:
			moved, uncovered = holding.take_preferring(
				qty, prefer, self._per_qty(prefer, row.get("purity"))
			)
		else:
			moved, uncovered = holding.take(qty)
		if uncovered > 1e-6:
			self.exceptions.append(
				_exception(row, EXC_UNKNOWN_OUTFLOW, {}, qty=uncovered)
			)
		return moved

	def _put(self, row, qty, shares):
		self.holdings[(row["batch_no"], row["warehouse"])].put(qty, shares)
		return shares

	def _outflow(self, row, disposition):
		prefer = row.get("receipt_key") if disposition == DISPOSITION_RETURNED else None
		moved = self._take(
			row, -row["qty"], prefer=prefer if prefer in self.receipts else None
		)
		if disposition == DISPOSITION_DELIVERED and moved:
			# What a sales return of this batch restores: the attribution AS IT LEFT, not whatever
			# a later transfer or production of the same batch happened to take.
			self.last_fraction[row["batch_no"]] = {
				key: amount / -row["qty"] for key, amount in moved.items()
			}
		for key, amount in moved.items():
			self.dispositions[key][disposition] += amount
		if moved:
			self._log(row, disposition.title(), moved, from_warehouse=row["warehouse"])
		return moved

	def _inflow(self, row):
		"""Stock arriving from outside the traced custody -- a customer returning a delivered
		piece, or a correction. A sales return restores the batch's attribution as it stood
		when it left; anything else arrives unattributed rather than guessed."""
		if row.get("is_return"):
			fraction = self.last_fraction.get(row["batch_no"])
			if not fraction:
				self._put(row, row["qty"], {})
				self.exceptions.append(
					_exception(row, EXC_UNATTRIBUTED_RETURN, {}, qty=row["qty"])
				)
				return
			given = {key: per_unit * row["qty"] for key, per_unit in fraction.items()}
			self._put(row, row["qty"], given)
			for key, amount in given.items():
				self.dispositions[key][DISPOSITION_DELIVERED] -= amount
			self._log(row, "Delivery Return", given, to_warehouse=row["warehouse"])
			return
		self._put(row, row["qty"], {})

	def _log(self, row, action, shares, from_warehouse=None, to_warehouse=None):
		for key, amount in shares.items():
			self.trail.append(
				{
					"receipt_key": key,
					"posting": row.get("posting"),
					"voucher_type": row.get("voucher_type"),
					"voucher_no": row.get("voucher_no"),
					"detail_no": row.get("detail_no"),
					"action": action,
					"batch_no": row.get("batch_no"),
					"item_code": row.get("item_code"),
					"from_warehouse": from_warehouse,
					"to_warehouse": to_warehouse,
					"qty": abs(flt(row.get("qty"))),
					"amount": amount,
				}
			)

	# -- results ----------------------------------------------------------------------------

	def positions(self):
		"""(batch, warehouse, qty, shares) for every holding that still carries a receipt share."""
		result = []
		for (batch_no, warehouse), holding in self.holdings.items():
			if holding.qty <= EPSILON and not holding.shares:
				continue
			shares = {k: v for k, v in holding.shares.items() if abs(v) > EPSILON}
			if not shares:
				continue
			result.append((batch_no, warehouse, holding.qty, shares))
		return result

	def held(self, key):
		return sum(holding.shares.get(key, 0.0) for holding in self.holdings.values())

	def balance(self, key):
		"""received - held - disposed. Zero, by construction, unless an exception fired."""
		disposed = sum(self.dispositions[key].values())
		return self.received[key] - self.held(key) - disposed


def _group_by_voucher(movements):
	groups = []
	index = {}
	for row in movements:
		voucher = (row["voucher_type"], row["voucher_no"])
		if voucher not in index:
			index[voucher] = len(groups)
			groups.append([])
		groups[index[voucher]].append(row)
	return groups


def _output_weights(rows):
	if all(row.get("purity") for row in rows):
		return [row["qty"] * flt(row["purity"]) for row in rows]
	return [row["qty"] for row in rows]


def _exception(row, reason, shares, **extra):
	return {
		"reason": reason,
		"voucher_type": row.get("voucher_type"),
		"voucher_no": row.get("voucher_no"),
		"detail_no": row.get("detail_no"),
		"batch_no": row.get("batch_no"),
		"warehouse": row.get("warehouse"),
		"shares": dict(shares),
		**extra,
	}


# ------------------------------------------------------------------------------------------
# Loader: the database side. Everything below only READS.
# ------------------------------------------------------------------------------------------


def receipt_key(voucher_no, detail_no):
	return f"{voucher_no}|{detail_no}"


def load_receipts(company, customer=None, receipt=None):
	"""The effective Receipt events: not reversed, and their Stock Entry still submitted."""
	filters = {"company": company, "cg_event_kind": EVENT_RECEIPT}
	if customer:
		filters["customer"] = customer
	if receipt:
		filters["reference_docname"] = receipt

	events = frappe.get_all(
		LEDGER_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"company",
			"customer",
			"reference_doctype",
			"reference_docname",
			"cg_source_row",
			"item_code",
			"batch_no",
			"stock_uom",
			"cg_gross_qty_delta",
			"cg_fine_gold_delta",
			"cg_fine_measurement_status",
			"cg_carrying_value_delta",
			"cg_currency",
		],
		order_by="creation",
	)
	if not events:
		return []

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
	vouchers = {e.reference_docname for e in events}
	voucher_info = {
		row.name: row
		for row in frappe.get_all(
			"Stock Entry",
			filters={"name": ["in", list(vouchers)]},
			fields=[
				"name",
				"docstatus",
				"posting_date",
				"posting_time",
				"custom_gold_rate_per_gram",
			]
			if frappe.db.has_column("Stock Entry", "custom_gold_rate_per_gram")
			else ["name", "docstatus", "posting_date", "posting_time"],
		)
	}
	result = []
	for event in events:
		info = voucher_info.get(event.reference_docname)
		if event.name in reversed_ or not info or info.docstatus != 1:
			continue
		purity = get_purity_percentage(event.item_code)
		event.purity = flt(purity) if purity else None
		event.unit = "fine" if event.purity else "qty"
		# Fine gold per unit as the receipt RECORDED it -- the purity in force at the time.
		event.per_unit = (
			flt(event.cg_fine_gold_delta) / flt(event.cg_gross_qty_delta)
			if event.unit == "fine"
			and event.cg_fine_measurement_status == "Known"
			and flt(event.cg_gross_qty_delta) > 0
			else None
		)
		event.posting_date = info.posting_date
		event.key = receipt_key(event.reference_docname, event.cg_source_row)
		result.append(event)
	return result


def _stock_entry_rows(voucher_nos):
	if not voucher_nos:
		return {}
	fields = [
		"name",
		"parent",
		"s_warehouse",
		"t_warehouse",
		"inventory_type",
		"customer",
		"is_finished_item",
		"against_stock_entry",
		"ste_detail",
	]
	if frappe.db.has_column("Stock Entry Detail", "custom_conversion_lane"):
		fields.append("custom_conversion_lane")
	return {
		row.name: row
		for row in frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": ["in", list(voucher_nos)]},
			fields=fields,
		)
	}


def _batch_owners(batches):
	if not batches:
		return {}
	return {
		row.name: row
		for row in frappe.get_all(
			"Batch",
			filters={"name": ["in", list(batches)]},
			fields=["name", "item", "custom_inventory_type", "custom_customer"],
		)
	}


def _row_lane(sed_row, batch_owner):
	"""The ownership lane of one production row: the explicit tag when Metal Conversions (or
	Settle) stamped one, else the row's own ownership, else the batch master's."""
	if sed_row and sed_row.get("custom_conversion_lane"):
		return sed_row.custom_conversion_lane
	if sed_row and sed_row.get("inventory_type"):
		return lane_key(
			sed_row.inventory_type,
			sed_row.customer if sed_row.inventory_type == CUSTOMER_GOODS else None,
		)
	if batch_owner:
		return lane_key(
			batch_owner.custom_inventory_type,
			batch_owner.custom_customer
			if batch_owner.custom_inventory_type == CUSTOMER_GOODS
			else None,
		)
	return lane_key(None, None)


def _bundle_entries(batches, to_datetime=None, outward_only=False, voucher_nos=None):
	sbe = frappe.qb.DocType("Serial and Batch Entry")
	sbb = frappe.qb.DocType("Serial and Batch Bundle")
	query = (
		frappe.qb.from_(sbe)
		.join(sbb)
		.on(sbe.parent == sbb.name)
		.select(
			sbb.voucher_type,
			sbb.voucher_no,
			sbb.voucher_detail_no,
			sbb.posting_datetime,
			sbb.creation,
			sbb.item_code,
			sbe.batch_no,
			sbe.serial_no,
			sbe.warehouse,
			sbe.qty,
			sbe.stock_value_difference,
		)
		.where((sbb.is_cancelled == 0) & (sbb.docstatus == 1))
	)
	if batches is not None:
		real = [b for b in batches if not str(b).startswith(SERIAL_PREFIX)]
		serials = [
			str(b)[len(SERIAL_PREFIX) :]
			for b in batches
			if str(b).startswith(SERIAL_PREFIX)
		]
		condition = sbe.batch_no.isin(real or [""])
		if serials:
			condition = condition | (
				sbe.serial_no.isin(serials)
				& ((sbe.batch_no.isnull()) | (sbe.batch_no == ""))
			)
		query = query.where(condition)
	if voucher_nos is not None:
		query = query.where(sbb.voucher_no.isin(list(voucher_nos)))
	if outward_only:
		query = query.where(sbb.type_of_transaction == "Outward")
	if to_datetime:
		query = query.where(sbb.posting_datetime <= to_datetime)
	rows = query.run(as_dict=True)
	for row in rows:
		row.batch_no = holding_id(row)
	return rows


def holding_id(entry):
	"""The batch, or -- for a piece minted with a Serial No and no batch -- the serial.

	A customer's finding folded into such a piece still has to be found: without this the replay
	lost it at the finished-goods step and reported it as unexplained (R-2).
	"""
	if entry.get("batch_no"):
		return entry.get("batch_no")
	if entry.get("serial_no"):
		return f"{SERIAL_PREFIX}{entry.get('serial_no')}"
	return None


def discover_scope(root_batches, customer=None, max_rounds=50):
	"""Every batch produced, at any depth, from ``root_batches`` in a lane that carries them.

	Iterates to a fixed point. A voucher already expanded is never expanded twice, which is also
	what stops a cycle -- a batch converted back into its own ancestor -- from looping.
	"""
	scope = set(b for b in root_batches if b)
	frontier = set(scope)
	seen_vouchers = set()
	rounds = 0
	while frontier and rounds < max_rounds:
		rounds += 1
		outward = _bundle_entries(frontier, outward_only=True)
		candidate = {
			row.voucher_no
			for row in outward
			if row.voucher_type == "Stock Entry" and row.voucher_no not in seen_vouchers
		}
		if not candidate:
			break
		producing = set(
			frappe.get_all(
				"Stock Entry",
				filters={
					"name": ["in", list(candidate)],
					"docstatus": 1,
					"purpose": ["in", list(PRODUCING_PURPOSES)],
				},
				pluck="name",
			)
		)
		seen_vouchers |= candidate
		if not producing:
			break

		entries = _bundle_entries(None, voucher_nos=producing)
		sed = _stock_entry_rows(producing)
		owners = _batch_owners({e.batch_no for e in entries if e.batch_no})

		new = set()
		for voucher in producing:
			rows = [e for e in entries if e.voucher_no == voucher and e.batch_no]
			in_scope_lanes = {
				_row_lane(sed.get(e.voucher_detail_no), owners.get(e.batch_no))
				for e in rows
				if e.qty < 0 and e.batch_no in scope | frontier
			}
			for e in rows:
				if e.qty <= 0:
					continue
				lane = _row_lane(sed.get(e.voucher_detail_no), owners.get(e.batch_no))
				if lane in in_scope_lanes and e.batch_no not in scope:
					new.add(e.batch_no)
		scope |= new
		frontier = new
	return scope


def load_movements(scope, receipts, to_datetime=None):
	"""Every effective bundle entry of the scope batches, shaped for :class:`AttributionReplay`."""
	from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
		get_customer_gold_settings,
	)

	entries = _bundle_entries(scope, to_datetime=to_datetime)
	if not entries:
		return []

	receipt_rows = {(r.reference_docname, r.cg_source_row): r.key for r in receipts}
	receipt_vouchers = {r.reference_docname for r in receipts}

	by_type = defaultdict(set)
	for e in entries:
		by_type[e.voucher_type].add(e.voucher_no)

	se_info = {}
	if by_type.get("Stock Entry"):
		se_info = {
			row.name: row
			for row in frappe.get_all(
				"Stock Entry",
				filters={"name": ["in", list(by_type["Stock Entry"])]},
				fields=["name", "purpose", "stock_entry_type"],
			)
		}
	sed = _stock_entry_rows(by_type.get("Stock Entry"))
	is_return = {}
	for doctype in ("Delivery Note", "Sales Invoice"):
		if by_type.get(doctype):
			for row in frappe.get_all(
				doctype,
				filters={"name": ["in", list(by_type[doctype])]},
				fields=["name", "is_return"],
			):
				is_return[(doctype, row.name)] = bool(row.is_return)

	owners = _batch_owners(scope)
	return_type = get_customer_gold_settings().get(
		"customer_gold_return_stock_entry_type"
	)
	purity_cache = {}

	def purity(item_code):
		if item_code not in purity_cache:
			value = get_purity_percentage(item_code) if item_code else None
			purity_cache[item_code] = flt(value) if value else None
		return purity_cache[item_code]

	first_seen = {}
	for e in entries:
		key = (e.voucher_type, e.voucher_no)
		stamp = (get_datetime(e.posting_datetime), get_datetime(e.creation))
		if key not in first_seen or stamp < first_seen[key]:
			first_seen[key] = stamp

	movements = []
	for e in entries:
		row = {
			"voucher_type": e.voucher_type,
			"voucher_no": e.voucher_no,
			"detail_no": e.voucher_detail_no,
			"posting": get_datetime(e.posting_datetime),
			"batch_no": e.batch_no,
			"warehouse": e.warehouse,
			"item_code": e.item_code,
			"qty": flt(e.qty),
			"value": flt(e.stock_value_difference),
			"purity": purity(e.item_code),
			"kind": KIND_MOVEMENT,
			"disposition": DISPOSITION_OTHER,
		}
		if e.voucher_type == "Stock Entry":
			info = se_info.get(e.voucher_no) or frappe._dict()
			if e.voucher_no in receipt_vouchers:
				row["kind"] = KIND_RECEIPT
				row["receipt_key"] = receipt_rows.get(
					(e.voucher_no, e.voucher_detail_no)
				)
			elif info.purpose in PRODUCING_PURPOSES:
				row["kind"] = KIND_PRODUCTION
				row["lane"] = _row_lane(
					sed.get(e.voucher_detail_no), owners.get(e.batch_no)
				)
				row["is_loss"] = info.stock_entry_type == PROCESS_LOSS_SE_TYPE
			elif return_type and info.stock_entry_type == return_type:
				row["disposition"] = DISPOSITION_RETURNED
				# The receipt row the return names: it is drawn on first (see take_preferring).
				link = sed.get(e.voucher_detail_no)
				if link and link.get("against_stock_entry") and link.get("ste_detail"):
					row["receipt_key"] = receipt_key(
						link.against_stock_entry, link.ste_detail
					)
			elif info.stock_entry_type == PROCESS_LOSS_SE_TYPE:
				row["disposition"] = DISPOSITION_LOSS
		elif e.voucher_type in ("Delivery Note", "Sales Invoice"):
			row["disposition"] = DISPOSITION_DELIVERED
			row["is_return"] = is_return.get((e.voucher_type, e.voucher_no), False)
		elif e.voucher_type == "Stock Reconciliation":
			row["kind"] = KIND_RECONCILIATION
		movements.append(row)

	# Posting order, one voucher at a time. Within a voucher the outward rows go first, so a
	# production's inputs are pooled before its outputs are filled.
	movements.sort(
		key=lambda m: (
			first_seen[(m["voucher_type"], m["voucher_no"])],
			m["voucher_no"],
			m["qty"] > 0,
		)
	)
	return movements


def trace(company, customer=None, receipt=None, to_datetime=None):
	"""Replay a customer's receipts. Returns (receipts, replay, scope).

	All of the customer's receipts are replayed even when only one is asked for: two receipts
	that share or merge into one batch divide it between them, and a replay of one alone would
	hand it the other's share.
	"""
	receipts = load_receipts(company, customer=customer)
	if receipt:
		wanted = {r.customer for r in receipts if r.reference_docname == receipt}
		receipts = [r for r in receipts if r.customer in wanted]
	if not receipts:
		return [], AttributionReplay({}), set()

	scope = discover_scope({r.batch_no for r in receipts})
	movements = load_movements(scope, receipts, to_datetime=to_datetime)
	replay = AttributionReplay(
		{r.key: {"unit": r.unit, "per_unit": r.get("per_unit")} for r in receipts}
	).run(movements)
	return receipts, replay, scope


def stage_of(warehouse, batch_no, replay):
	if batch_no in replay.loss_batches:
		return "Loss / Scrap"
	return warehouse_stage(warehouse) or "RM"
