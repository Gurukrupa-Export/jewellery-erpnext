# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Which Stock Entry rows may carry ``allow_zero_valuation_rate``.

One rule, shared by every writer, so no builder can re-arm a defect another path has fixed.

The flag exists so a CUSTOMER-OWNED row may carry a zero value: under the Zero Value policy a
customer's metal costs the company nothing. It is correct on rows that bring customer material into
an entry, and on receipts.

It is wrong on a row whose rate ERPNext DERIVES from what the entry consumed -- a Manufacture or
Repack finished good. ``StockEntry.set_basic_rate`` tests the flag BEFORE its derivation branches::

    if d.allow_zero_valuation_rate and d.basic_rate and purpose != "Receive from Customer":
        d.basic_rate = 0.0
    elif d.is_finished_item:
        d.basic_rate = self.get_basic_rate_for_manufactured_item(...)

On the first pass ``basic_rate`` is 0, so the flag is inert and the finished good is valued. On any
later pass -- ``save()`` then ``submit()``, or a repost -- the row already carries that value, so it
is zeroed before it can be recomputed. That is F3: MAT-STE-18661's finished piece went from
8,01,654.99 to 0 at submit and every rupee of input, the company's own diamond included, was
expensed to Stock Adjustment. All five customer-gold Manufacture entries on kg-gk were wiped; four
survive only because a repost happened to recompute them, and the next repost would wipe them again.

The derived value is right under BOTH valuation policies, because ERPNext builds it from what was
consumed at each input's own carrying value: customer metal at 0 under Zero Value, at its receipt
rate under Nominal, company material at cost. The flag has nothing to protect on an output row.

Deliberately left alone:

* secondary / scrap rows. ERPNext does not derive them from the inputs; it prices them from the
  target warehouse's valuation rate. Refining returns a customer's own stones as Customer Goods
  scrap that must land at zero value, and releasing the flag there values them at a company rate
  -- or throws "Valuation Rate Missing" when the warehouse has none;
* rows with ``set_basic_rate_manually`` -- ERPNext skips them, and the Process Loss / conversion
  pricers in ``loss_valuation`` depend on the flag to permit a legitimately zero value;
* non-customer rows -- company-only manufacturing is unchanged, including the unconditional flag on
  scrap rows built in ``doc_events/stock_entry.py``. Stamping stays on "Customer Goods" only, as
  before; a "Customer Stock" row is governed like any customer output but is not newly stamped;
* entries with no consumed row -- there ERPNext's fallback is not suppressed, and releasing the flag
  could let it value a zero output from the item's valuation rate instead.
"""

from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_INVENTORY_TYPES,
	_get,
)

# Purposes whose incoming rows ERPNext values itself, from the rows the entry consumed.
DERIVED_OUTPUT_PURPOSES = ("Manufacture", "Repack")


def is_derived_output_row(row, se):
	"""Whether ERPNext derives this row's rate from what ``se`` consumed: a finished good.

	Mirrors ``StockEntry.set_basic_rate``: it skips rows with a source warehouse or a manual rate,
	and its finished-good branch derives the value from the consumed rows. ``is_finished_item`` is
	set by ``mark_finished_and_secondary_items`` during ``validate``, so it is reliable inside
	``set_basic_rate``; on a freshly built Repack row it may not be set yet in ``before_validate``,
	which is why ``set_basic_rate`` is the authoritative enforcement point.
	"""
	return bool(
		_get(se, "purpose") in DERIVED_OUTPUT_PURPOSES
		and not _get(row, "s_warehouse")
		and _get(row, "t_warehouse")
		and not _get(row, "set_basic_rate_manually")
		and _get(row, "is_finished_item")
	)


def _has_consumed_row(se):
	return any(_get(row, "s_warehouse") for row in se.get("items") or [])


def _is_governed(row, se):
	"""A customer-owned derived output on an entry that consumes something -- the rows this module owns."""
	return (
		_get(row, "inventory_type") in CUSTOMER_INVENTORY_TYPES
		and is_derived_output_row(row, se)
		and _has_consumed_row(se)
	)


#: The only lane ``before_validate`` ever stamped, and still the only one it stamps.
STAMPED_INVENTORY_TYPE = "Customer Goods"


def should_allow_zero_valuation(row, se):
	"""Whether ``before_validate`` may stamp ``allow_zero_valuation_rate`` on ``row``.

	Customer Goods rows, as before, except a derived output this module governs --
	``set_basic_rate`` decides that row's flag after ERPNext has valued it. An entry with no
	consumed row keeps the old behaviour, flag included, because there ERPNext's fallback is not
	suppressed.
	"""
	return _get(row, "inventory_type") == STAMPED_INVENTORY_TYPE and not _is_governed(
		row, se
	)


def release_derived_outputs(se):
	"""Clear the flag on every governed row before ERPNext values it; return those rows.

	Call ahead of ``super().set_basic_rate()`` and ahead of ``capture_entered_metal_rates``, so a
	derived rate is never mistaken for one the user typed and parked on ``custom_metal_rate`` (F8).

	Governed rows exist only on an entry that consumes something. ``has_consumption_basis`` is then
	True, so ERPNext marks the finished good's rate as derived and suppresses its item-valuation
	fallback: a legitimately zero output stays zero instead of being invented or raising.
	"""
	governed = [row for row in se.get("items") or [] if _is_governed(row, se)]
	for row in governed:
		row.allow_zero_valuation_rate = 0
	return governed


def settle_derived_outputs(governed):
	"""Give each governed row the flag if and only if its final value is not positive.

	The end state is decided here, not by whichever builder or hook touched the row first, and after
	every pricer has run (``set_process_loss_produce_rates`` re-prices conversion lanes). Under Zero
	Value a Manufacture whose every input is customer metal legitimately derives 0, and the flag is
	what lets the ledger accept that zero. A positive derivation stays unflagged, so no later pass or
	repost can wipe it.

	A negative derivation -- scrap valued above the inputs it came from, e.g. a customer's refining
	under Zero Value -- is held at 0, flagged: what the old flag produced on the second pass. A
	negative incoming rate is never right, and ERPNext would not refuse one.
	"""
	for row in governed:
		if flt(row.get("basic_rate")) > 0:
			row.allow_zero_valuation_rate = 0
			continue
		if flt(row.get("basic_rate")) < 0:
			row.basic_rate = 0.0
			row.basic_amount = 0.0
		row.allow_zero_valuation_rate = 1
