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

import frappe
from frappe import _
from frappe.utils import flt, getdate

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


def convert_gold_rate_to_per_gram(raw_rate, unit):
	"""Convert a raw Gold Rates value to a per-gram rate.

	The only place a unit divisor appears. Deliberately does not round -- the caller
	stores the result in a field with its own precision.
	"""
	if unit == PER_GRAM:
		return flt(raw_rate)
	if unit == PER_10_GRAM:
		return flt(raw_rate) / GRAMS_PER_10_GRAM

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
		per_gram_rate=convert_gold_rate_to_per_gram(raw_rate, unit),
	)


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

	name = frappe.db.get_value(GOLD_RATES_DOCTYPE, {"date": rate_date}, "name")
	if not name:
		frappe.throw(
			_(
				"{0} is not available for Posting Date {1}. Please create the {0} record before submitting the Customer Gold Receipt."
			).format(
				GOLD_RATES_DOCTYPE,
				frappe.bold(frappe.format(rate_date, {"fieldtype": "Date"})),
			),
			title=_("Customer Gold Rate Unavailable"),
		)
	return name


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

	if flt(raw_rate) <= 0:
		frappe.throw(
			_(
				"Gold Rate {0} for source {1} in {2} is {3}. A positive rate is required."
			).format(
				frappe.bold(rate_field),
				frappe.bold(source),
				frappe.bold(gold_rates_name),
				frappe.bold(flt(raw_rate)),
			),
			title=_("Customer Gold Rate Unavailable"),
		)

	return raw_rate
