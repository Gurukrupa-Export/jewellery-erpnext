# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Resolve the configured Customer Gold rate for a transaction's posting date.

The returned data is intended to be snapshotted onto the receipt and **must not be
re-resolved for historical documents**. A submitted Customer Gold receipt keeps the
evidence it froze, whatever later happens to the Gold Rates master.

The lookup is deliberately keyed on the caller's ``posting_date`` and never on
``frappe.utils.today()``: a backdated receipt must value at the rate of the day it is
posted for. The date policy is EXACT -- if no ``Gold Rates`` document exists for that
date the resolution blocks rather than silently borrowing a neighbouring day's rate,
which would be financially unsafe and unauditable. The result carries both the requested
date and the resolved ``gold_rate_date`` so a configurable fallback could be introduced
later without a schema change.

Everything the service reads is configuration from ``Subcontracting Settings`` -- the
dealer row (``gold_rate_source``), the rate column (``gold_rate_field``) and the unit
(``gold_rate_unit``). Nothing here is hardcoded to a dealer, a column or a divisor, and
the service is strictly read-only: it never writes to Gold Rates, never commits, and
never calls out to a bullion feed.

This module resolves a rate. It does NOT apply purity, GST, valuation or GL -- those are
separate concerns and separate days.
"""

from math import isfinite

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, to_timedelta

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	GOLD_RATE_FIELDS,
	GOLD_RATE_UNITS,
	get_customer_gold_settings,
)

GOLD_RATES_DOCTYPE = "Gold Rates"
GOLD_RATES_ROW_DOCTYPE = "Gold Rates branchs"

PER_GRAM = "Per Gram"
PER_10_GRAM = "Per 10 Gram"
GRAMS_PER_10_GRAM = 10.0

#: How many grams one quoted unit covers -- the normalisation factor frozen on the receipt.
GRAMS_PER_UNIT = {PER_GRAM: 1.0, PER_10_GRAM: GRAMS_PER_10_GRAM}

#: A frozen per-gram rate outside this multiple of its reference is refused unless approved.
#: Wide on purpose: gold does not move 50% inside the reference window, while the failure this
#: exists for is a factor of ten (Rs.1,57,655 booked against Rs.15,504.85 paid).
OUTLIER_BAND = (0.5, 2.0)
#: How far back a purchase of the same item still counts as a reference.
PURCHASE_REFERENCE_DAYS = 90
#: How far back the feed's own earlier rate still counts, when there is no purchase.
FEED_REFERENCE_DAYS = 7


def convert_gold_rate_to_per_gram(raw_rate, unit):
	"""Convert a raw Gold Rates value to a per-gram rate.

	The only place a unit divisor appears. Deliberately does not round -- the caller
	stores the result in a field with its own precision.
	"""
	if unit in GRAMS_PER_UNIT:
		return flt(raw_rate) / GRAMS_PER_UNIT[unit]

	frappe.throw(
		_("Gold Rate Unit {0} is not supported. Allowed: {1}.").format(
			frappe.bold(unit or _("not set")), ", ".join(GOLD_RATE_UNITS)
		),
		title=_("Customer Gold Rate Unavailable"),
	)


def resolve_customer_gold_rate_for_date(posting_date, settings=None):
	"""Resolve the configured gold rate for ``posting_date``.

	Returns a ``frappe._dict`` carrying the full derivation, so the receipt can freeze
	evidence rather than a bare number. Throws a business-readable ValidationError for
	every missing or unusable-rate case.
	"""
	if not posting_date:
		frappe.throw(
			_("Posting Date is required to resolve the Customer Gold rate."),
			title=_("Customer Gold Rate Unavailable"),
		)

	rate_date = getdate(posting_date)
	settings = settings or get_customer_gold_settings()

	source = settings.get("gold_rate_source")
	rate_field = settings.get("gold_rate_field")
	unit = settings.get("gold_rate_unit")

	_validate_rate_configuration(source, rate_field, unit)

	gold_rates_name = _get_gold_rates_document(rate_date)
	row = _get_source_row(gold_rates_name, source, rate_date)
	raw_rate = _get_raw_rate(row, rate_field, source, gold_rates_name)

	return frappe._dict(
		gold_rate_reference=gold_rates_name,
		gold_rate_date=rate_date,
		requested_date=rate_date,
		rate_source=source,
		rate_field=rate_field,
		raw_rate=flt(raw_rate),
		rate_unit=unit,
		rate_factor=GRAMS_PER_UNIT[unit],
		per_gram_rate=convert_gold_rate_to_per_gram(raw_rate, unit),
	)


def reference_rate(item_code, company, posting_date, settings):
	"""An independent per-gram rate to check a frozen rate against, or ``None``.

	F1: the feed quotes per 10 g on some days and per gram on others, so the configured unit
	is right on some days and ten times wrong on others. A check against the feed's own unit
	cannot see that; it needs a number the feed did not produce.

	1. **The company's latest purchase of the same item** within ``PURCHASE_REFERENCE_DAYS``.
	   It is what the company actually paid, in the item's own stock unit, and the feed plays
	   no part in it. On kg-gk it is Rs.15,504.85/g (``PR-26-00171``) -- the number that shows
	   ``KGJPL-SE-CGR-26-00011``'s Rs.1,57,655/g is ten times too high.
	2. **The same feed's own rate on an earlier day** within ``FEED_REFERENCE_DAYS``, normalised
	   with the same unit. It catches the feed changing scale overnight, though not a feed that
	   has always been wrong -- which is why a purchase is preferred.

	Earlier customer-gold receipts are NOT a reference: every one booked since the rate engine
	landed carries the ten-times rate, and checking against them would pass the error and flag
	the correction.

	Returns ``frappe._dict(rate=..., source=...)``.
	"""
	return _purchase_reference(item_code, company, posting_date) or _feed_reference(
		posting_date, settings
	)


def _purchase_reference(item_code, company, posting_date):
	if not item_code or not company or not posting_date:
		return None

	since = add_days(getdate(posting_date), -PURCHASE_REFERENCE_DAYS)
	candidates = []
	for parent_doctype, child_doctype, stock_filter in (
		("Purchase Receipt", "Purchase Receipt Item", None),
		("Purchase Invoice", "Purchase Invoice Item", "update_stock"),
	):
		parent = frappe.qb.DocType(parent_doctype)
		child = frappe.qb.DocType(child_doctype)
		query = (
			frappe.qb.from_(child)
			.join(parent)
			.on(child.parent == parent.name)
			.select(
				parent.name,
				parent.posting_date,
				parent.posting_time,
				child.base_net_rate,
				child.conversion_factor,
			)
			.where(
				(parent.docstatus == 1)
				& (parent.company == company)
				& (parent.is_return == 0)
				& (child.item_code == item_code)
				& (child.base_net_rate > 0)
				& (parent.posting_date >= since)
				& (parent.posting_date <= getdate(posting_date))
			)
			.orderby(parent.posting_date, order=frappe.qb.desc)
			.orderby(parent.posting_time, order=frappe.qb.desc)
			.limit(1)
		)
		if stock_filter:
			query = query.where(getattr(parent, stock_filter) == 1)
		for name, date, time, rate, factor in query.run():
			candidates.append(
				(
					getdate(date),
					to_timedelta(time or "0:00:00"),
					parent_doctype,
					name,
					flt(rate) / (flt(factor) or 1.0),
				)
			)

	if not candidates:
		return None

	date, _time, doctype, name, rate = max(candidates)
	return frappe._dict(rate=rate, source=f"{doctype} {name} ({date})")


def _feed_reference(posting_date, settings):
	source = settings.get("gold_rate_source")
	rate_field = settings.get("gold_rate_field")
	unit = settings.get("gold_rate_unit")
	if not (
		source
		and rate_field in GOLD_RATE_FIELDS
		and unit in GRAMS_PER_UNIT
		and posting_date
	):
		return None

	day = getdate(posting_date)
	earlier = frappe.get_all(
		GOLD_RATES_DOCTYPE,
		filters=[
			["date", "<", day],
			["date", ">=", add_days(day, -FEED_REFERENCE_DAYS)],
		],
		fields=["name", "date"],
		order_by="date desc",
	)
	for record in earlier:
		rows = frappe.get_all(
			GOLD_RATES_ROW_DOCTYPE,
			filters={
				"parent": record.name,
				"parenttype": GOLD_RATES_DOCTYPE,
				"particulars": source,
			},
			fields=["*"],
		)
		if len(rows) != 1:
			continue
		raw = flt(rows[0].get(rate_field))
		if isfinite(raw) and raw > 0:
			return frappe._dict(
				rate=convert_gold_rate_to_per_gram(raw, unit),
				source=f"Gold Rates {record.name} ({source}, {rate_field}, {record.date})",
			)

	return None


def rate_ratio(per_gram_rate, reference):
	"""``per_gram_rate`` as a multiple of the reference, or ``None`` when there is none."""
	if not reference or flt(reference.rate) <= 0:
		return None
	return flt(per_gram_rate) / flt(reference.rate)


def is_outlier(ratio):
	return ratio is not None and not (OUTLIER_BAND[0] <= ratio <= OUTLIER_BAND[1])


def _validate_rate_configuration(source, rate_field, unit):
	"""Defend the service even if Settings were bypassed or the DB is stale."""
	if not source:
		frappe.throw(
			_("Customer Gold {0} is not configured.").format(
				frappe.bold(_("Gold Rate Source"))
			),
			title=_("Customer Gold Configuration Incomplete"),
		)
	if not rate_field:
		frappe.throw(
			_("Customer Gold {0} is not configured.").format(
				frappe.bold(_("Gold Rate Field"))
			),
			title=_("Customer Gold Configuration Incomplete"),
		)
	if rate_field not in GOLD_RATE_FIELDS:
		frappe.throw(
			_("Gold Rate Field {0} is not a rate column on {1}. Allowed: {2}.").format(
				frappe.bold(rate_field),
				GOLD_RATES_ROW_DOCTYPE,
				", ".join(GOLD_RATE_FIELDS),
			),
			title=_("Customer Gold Configuration Invalid"),
		)
	if not unit:
		frappe.throw(
			_("Customer Gold {0} is not configured.").format(
				frappe.bold(_("Gold Rate Unit"))
			),
			title=_("Customer Gold Configuration Incomplete"),
		)
	if unit not in GOLD_RATE_UNITS:
		frappe.throw(
			_("Gold Rate Unit {0} is not supported. Allowed: {1}.").format(
				frappe.bold(unit), ", ".join(GOLD_RATE_UNITS)
			),
			title=_("Customer Gold Configuration Invalid"),
		)


def _get_gold_rates_document(rate_date):
	if not frappe.db.exists("DocType", GOLD_RATES_DOCTYPE):
		frappe.throw(
			_(
				"Customer Gold Flow requires the {0} DocType, which is not installed on this site."
			).format(frappe.bold(GOLD_RATES_DOCTYPE)),
			title=_("Customer Gold Rate Unavailable"),
		)

	# Fetch ALL records for the date, not the first match. ``Gold Rates`` autonames
	# ``format:R-{date}``, which makes a second same-date record awkward but NOT
	# impossible: the doctype sets ``allow_rename: 1`` and puts no ``unique`` on the
	# ``date`` field, so renaming R-2026-09-14 out of the way frees the name for a
	# second record carrying the same date. ``frappe.db.get_value`` would silently
	# return whichever the DB yielded first and the receipt would freeze a rate chosen
	# by row order. Ambiguity must block -- picking one is unauditable.
	names = [
		row.name
		for row in frappe.get_all(
			GOLD_RATES_DOCTYPE,
			filters={"date": rate_date},
			fields=["name"],
			order_by="name",
		)
	]

	if not names:
		frappe.throw(
			_(
				"{0} is not available for Posting Date {1}. Please create the {0} record before submitting the Customer Gold Receipt."
			).format(
				GOLD_RATES_DOCTYPE,
				frappe.bold(frappe.format(rate_date, {"fieldtype": "Date"})),
			),
			title=_("Customer Gold Rate Unavailable"),
		)

	if len(names) > 1:
		frappe.throw(
			_(
				"{0} records exist for Posting Date {1} ({2}). Exactly one is required, so the rate cannot be resolved unambiguously. Please remove or correct the duplicates."
			).format(
				len(names),
				frappe.bold(rate_date),
				", ".join(frappe.bold(n) for n in names),
			),
			title=_("Customer Gold Rate Ambiguous"),
		)

	return names[0]


def _get_source_row(gold_rates_name, source, rate_date):
	"""Return the single child row for the configured source.

	Fetches whole rows rather than naming the rate column in the query: the rate columns
	include ``9_am`` / ``3_pm`` / ``11_pm``, which are not safe to interpolate, and the
	value is read from the resulting dict instead.
	"""
	rows = frappe.get_all(
		GOLD_RATES_ROW_DOCTYPE,
		filters={
			"parent": gold_rates_name,
			"parenttype": GOLD_RATES_DOCTYPE,
			"particulars": source,
		},
		fields=["*"],
	)

	if not rows:
		frappe.throw(
			_("Gold Rate source {0} was not found in {1} (Posting Date {2}).").format(
				frappe.bold(source),
				frappe.bold(gold_rates_name),
				frappe.format(rate_date, {"fieldtype": "Date"}),
			),
			title=_("Customer Gold Rate Unavailable"),
		)

	if len(rows) > 1:
		frappe.throw(
			_(
				"{0} contains {1} rows for Gold Rate source {2}. Exactly one is required. Please correct the {3} record."
			).format(
				frappe.bold(gold_rates_name),
				len(rows),
				frappe.bold(source),
				GOLD_RATES_DOCTYPE,
			),
			title=_("Customer Gold Rate Ambiguous"),
		)

	return rows[0]


def _get_raw_rate(row, rate_field, source, gold_rates_name):
	raw_rate = row.get(rate_field)

	if raw_rate is None:
		frappe.throw(
			_("Gold Rate field {0} is not available for source {1} in {2}.").format(
				frappe.bold(rate_field),
				frappe.bold(source),
				frappe.bold(gold_rates_name),
			),
			title=_("Customer Gold Rate Unavailable"),
		)

	# Finiteness is checked BEFORE the positive test, because ``nan`` and ``inf`` do not
	# fail it: ``float("nan") <= 0`` and ``float("inf") <= 0`` are both False, so a
	# non-finite quote would sail through a bare positive guard and be frozen onto the
	# receipt -- and ``nan / 10`` is still ``nan``, so the per-gram conversion would carry
	# it into valuation. Ordering matters; a positive-only guard is not sufficient.
	numeric_rate = flt(raw_rate)
	if not isfinite(numeric_rate):
		frappe.throw(
			_(
				"Gold Rate {0} for source {1} in {2} is not a finite number ({3}). A positive, finite rate is required."
			).format(
				frappe.bold(rate_field),
				frappe.bold(source),
				frappe.bold(gold_rates_name),
				frappe.bold(raw_rate),
			),
			title=_("Customer Gold Rate Unavailable"),
		)

	if numeric_rate <= 0:
		frappe.throw(
			_(
				"Gold Rate {0} for source {1} in {2} is {3}. A positive rate is required."
			).format(
				frappe.bold(rate_field),
				frappe.bold(source),
				frappe.bold(gold_rates_name),
				frappe.bold(numeric_rate),
			),
			title=_("Customer Gold Rate Unavailable"),
		)

	return raw_rate
