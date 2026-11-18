import json
from contextlib import contextmanager

import frappe
from frappe.utils import flt


@contextmanager
def tracking_bom_run_cache():
	"""Share master-data lookups across every Tracking BOM built in one run.

	A Quotation builds one Tracking BOM per row, and each one used to re-read the same price
	lists, making charges, metal criteria and customer settings for every metal, finding,
	diamond and gemstone row. Nothing a run reads is written during it (only Tracking BOMs are
	inserted), so one fetch per key is enough. Outside this block ``run_cached`` fetches every
	time, exactly as before.
	"""
	previous = getattr(frappe.local, "tracking_bom_cache", None)
	frappe.local.tracking_bom_cache = previous if previous is not None else {}
	try:
		yield
	finally:
		frappe.local.tracking_bom_cache = previous


def run_cached(key, fetch):
	"""``fetch()`` once per ``key`` inside ``tracking_bom_run_cache``; every call outside it.

	Callers treat the result as read-only: it is shared by every Tracking BOM in the run.
	"""
	cache = getattr(frappe.local, "tracking_bom_cache", None)
	if cache is None:
		return fetch()
	if key not in cache:
		cache[key] = fetch()
	return cache[key]


def filters_key(filters):
	"""A hashable cache key for a filters dict (its values may hold lists)."""
	return json.dumps(filters, sort_keys=True, default=str)


def keep_po_rate(doc, row):
	"""True when the row's Purchase Order rate must survive the Tracking BOM rate.

	An Outwork quotation row pulled from a Purchase Order (``custom_po_details``) carries the
	rate agreed on that order. The customer supplies the materials, so the Tracking BOM total
	holds only making/handling charges and is 0 when those are not set up -- it would wipe the
	Purchase Order rate. Other sales types and rows without a Purchase Order are unaffected.
	"""
	return doc.get("custom_sales_type") == "Outwork" and bool(
		row.get("custom_po_details")
	)


def set_tracking_bom_rate_in_quotation(self):
	"""
	Fetch Tracking BOM Rates and replace the quotation item rate with Tracking BOM rate.
	Works like set_bom_rate_in_quotation but reads from Tracking Bom instead of BOM.
	"""
	field_list = [
		"gold_rate_with_gst",
		"gold_bom_amount",
		"making_charge",
		"finding_bom_amount",
		"diamond_bom_amount",
		"gemstone_bom_amount",
		"other_bom_amount",
		"total_bom_amount",
		"hallmarking_amount",
	]
	# One query for every row's Tracking BOM instead of a get_value per row.
	tracking_bom_names = list(
		{
			row.get("custom_tracking_bom")
			for row in self.items
			if row.get("custom_tracking_bom")
		}
	)
	tb_data_map = (
		{
			d.name: d
			for d in frappe.get_all(
				"Tracking Bom",
				filters={"name": ["in", tracking_bom_names]},
				fields=["name", *field_list],
			)
		}
		if tracking_bom_names
		else {}
	)

	for row in self.items:
		if row.get("custom_tracking_bom"):
			tb_data = tb_data_map.get(row.custom_tracking_bom)
			if tb_data is None:
				# Not returned by the bulk query (missing, or matched only by the database's
				# case-insensitive collation): look it up exactly as before.
				tb_data = frappe.db.get_value(
					"Tracking Bom", row.custom_tracking_bom, field_list, as_dict=1
				)
			if not tb_data:
				continue

			if self.gold_rate_with_gst > 0 and tb_data.gold_rate_with_gst > 0:
				row.metal_amount = (
					tb_data.gold_bom_amount / tb_data.gold_rate_with_gst
				) * self.gold_rate_with_gst
			else:
				row.metal_amount = tb_data.gold_bom_amount

			row.making_amount = tb_data.making_charge
			row.finding_amount = tb_data.finding_bom_amount
			row.diamond_amount = tb_data.diamond_bom_amount
			row.gemstone_amount = tb_data.gemstone_bom_amount
			row.custom_hallmarking_amount = tb_data.hallmarking_amount

			if keep_po_rate(self, row):
				continue

			row.rate = flt(
				row.metal_amount
				+ row.making_amount
				+ row.finding_amount
				+ row.diamond_amount
				+ row.gemstone_amount
				+ (row.custom_hallmarking_amount or 0),
				3,
			)
