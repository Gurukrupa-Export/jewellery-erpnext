# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Lineage discovery for customer-gold batches: every batch produced from a given one.

This is the lineage part of kg-gk's ``customer_gold_trace`` -- ``discover_scope`` and the
stock-ledger readers it uses, unchanged. The rest of that module (the receipt replay behind the
Customer Gold Traceability report) reads the Customer Gold custody ledger
(``customer_gold_fulfilment``), which this branch does not have. Settle is the caller
(``sub_utils/cg_settle._batch_lineage``).

Read-only: it queries the stock ledger and writes nothing.
"""

from collections import defaultdict

import frappe

REGULAR_STOCK = "Regular Stock"
# kg-gk imports this from customer_gold_fulfilment, which is not on this branch.
CUSTOMER_GOODS = "Customer Goods"
#: Prefix of the holding id used for a finished piece that has a Serial No but no batch.
SERIAL_PREFIX = "SN::"
PRODUCING_PURPOSES = ("Repack", "Manufacture")


def lane_key(inventory_type, customer):
	"""Same string ``metal_conversions.lane_tag`` stamps, so an explicit tag and an owner-derived
	fallback compare equal."""
	return f"{inventory_type or REGULAR_STOCK}|{customer or ''}"


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

		# Grouped once per round: filtering the whole entry list per voucher was quadratic.
		by_voucher = defaultdict(list)
		for e in entries:
			if e.batch_no:
				by_voucher[e.voucher_no].append(e)

		new = set()
		known = scope | frontier
		for rows in by_voucher.values():
			lanes = [
				(e, _row_lane(sed.get(e.voucher_detail_no), owners.get(e.batch_no)))
				for e in rows
			]
			in_scope_lanes = {
				lane for e, lane in lanes if e.qty < 0 and e.batch_no in known
			}
			for e, lane in lanes:
				if e.qty > 0 and lane in in_scope_lanes and e.batch_no not in scope:
					new.add(e.batch_no)
		scope |= new
		frontier = new
	return scope
