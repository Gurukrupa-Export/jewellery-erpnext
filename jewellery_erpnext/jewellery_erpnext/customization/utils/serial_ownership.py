# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Book a serialized Stock Entry row in the lane its serial was last RECEIVED in.

``StockLedgerEntry.validate_serial_no_inventory_dimension`` (erpnext#58394, arrived with
erpnext 16.34.1) compares every outward serialized SLE against that serial's LAST INWARD SLE
and refuses any difference. A finished piece made from a customer's metal is now received as
that customer's goods (``_finished_goods_ownership``), so its last inward SLE reads
``Customer Goods`` / the customer -- correctly.

Nothing told the builders that move those pieces afterwards. Product Certification's
``create_stock_entry`` wrote ``"Inventory_type": "Regular Stock"`` (a capitalised key the
framework silently drops) and no customer, the blanket default in
``doc_events/stock_entry.before_validate`` then filled "Regular Stock", and the submit died::

    Serial No KLHGX62F1257 is not available in the selected inventory dimensions:
    Customer: expected "GJCU0009", got "None",
    Inventory Type: expected "Customer Goods", got "Regular Stock"

The lane a serial row must carry is not a choice: it is exactly what that validator will
demand. So it is read the same way -- the serial's last inward SLE for the row's item, as of
the entry's posting time -- and stamped before the blanket default runs. Every builder that
moves a finished piece (Product Certification's three, Department IR, hallmarking, allocation)
is covered at once, instead of each one learning to look it up.

Only a row whose lane is still the framework's own -- blank, or "Regular Stock" with no
customer -- is stamped. A row that names an owner keeps it, and a real conflict (the row says
one customer, the serial was received for another) is still the validator's to report. Rows
whose serials were received in different lanes, or in a lane that is not a coherent
(type, customer) pair, are left alone for the same reason: there is no single right answer to
write, and a wrong one would only move the error.
"""

import frappe
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos
from erpnext.stock.utils import get_combine_datetime
from frappe.query_builder import Order
from frappe.utils import cint, now_datetime

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_INVENTORY_TYPES,
	DEFAULT_INVENTORY_TYPE,
)


def stamp_serial_row_ownership(doc):
	"""Fill ``inventory_type`` / ``customer`` on consuming serial rows from each serial's history."""
	candidates = [
		row
		for row in doc.get("items") or []
		if row.get("s_warehouse") and _lane_is_unset(row)
	]
	if not candidates:
		return

	serials_by_row = _row_serials(candidates)
	if not serials_by_row:
		return

	lanes = last_inward_lanes(
		{
			(row.item_code, serial)
			for row in candidates
			for serial in serials_by_row.get(id(row), ())
		},
		_as_of(doc),
	)

	for row in candidates:
		serials = serials_by_row.get(id(row))
		if not serials:
			continue

		found = {lanes.get((row.item_code, serial)) for serial in serials}
		if len(found) != 1:
			continue

		lane = found.pop()
		if not lane or not _coherent(*lane):
			continue

		row.inventory_type, row.customer = lane


def last_inward_lanes(item_serials, as_of):
	"""``{(item_code, serial_no): (inventory_type, customer)}`` from each serial's last receipt.

	The same rows ``get_last_inward_dimensions`` reads (positive, uncancelled SLEs of that item
	up to the posting time, newest first), for every serial of the document in one query.
	"""
	if not item_serials:
		return {}

	sle = frappe.qb.DocType("Stock Ledger Entry")
	entry = frappe.qb.DocType("Serial and Batch Entry")
	rows = (
		frappe.qb.from_(sle)
		.join(entry)
		.on(entry.parent == sle.serial_and_batch_bundle)
		.select(entry.serial_no, sle.item_code, sle.inventory_type, sle.customer)
		.where(
			entry.serial_no.isin(sorted({serial for _item, serial in item_serials}))
			& sle.item_code.isin(sorted({item for item, _serial in item_serials}))
			& (sle.actual_qty > 0)
			& (sle.is_cancelled == 0)
			& (sle.posting_datetime <= as_of)
		)
		.orderby(sle.posting_datetime, order=Order.desc)
		.orderby(sle.creation, order=Order.desc)
	).run(as_dict=True)

	lanes = {}
	for row in rows:
		key = (row.item_code, row.serial_no)
		if key in item_serials and key not in lanes:
			lanes[key] = (row.inventory_type, row.customer)
	return lanes


def _lane_is_unset(row):
	"""Blank, or the "Regular Stock, nobody" the framework writes when nothing else did."""
	inventory_type = row.get("inventory_type")
	return not inventory_type or (
		inventory_type == DEFAULT_INVENTORY_TYPE and not row.get("customer")
	)


def _coherent(inventory_type, customer):
	"""A lane worth copying: a customer exactly when the type is a customer's."""
	if not inventory_type:
		return False
	if inventory_type in CUSTOMER_INVENTORY_TYPES:
		return bool(customer)
	return not customer


def _row_serials(rows):
	"""``{id(row): [serial_no, ...]}`` from the row's own field, else its existing bundle.

	Keyed by object identity: a row appended by a builder has no ``name`` before insert.
	"""
	serials = {}
	bundles = {}
	for row in rows:
		if row.get("serial_no"):
			serials[id(row)] = get_serial_nos(row.serial_no)
		elif row.get("serial_and_batch_bundle"):
			bundles[row.serial_and_batch_bundle] = id(row)

	if bundles:
		for entry in frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": ("in", list(bundles)), "serial_no": ("is", "set")},
			fields=["parent", "serial_no"],
			limit_page_length=0,
		):
			serials.setdefault(bundles[entry.parent], []).append(entry.serial_no)

	return {key: value for key, value in serials.items() if value}


def _as_of(doc):
	"""The posting time the validator will compare at. Unset posting times post at "now"."""
	if cint(doc.get("set_posting_time")) and doc.get("posting_date"):
		return get_combine_datetime(doc.posting_date, doc.get("posting_time") or "23:59:59")
	return now_datetime()
