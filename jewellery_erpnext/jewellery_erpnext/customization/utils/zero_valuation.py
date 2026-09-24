# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Which Stock Entry rows may carry ``allow_zero_valuation_rate``.

One rule, shared by every writer, so no builder can re-arm a defect another path has fixed.

The flag exists so a CUSTOMER-OWNED row may carry a zero value: under the Zero Value policy a
customer's metal costs the company nothing. It is correct on rows that bring customer material into
an entry, and on receipts.

It is wrong on a row whose rate ERPNext DERIVES from what the entry consumed -- a Manufacture or
Repack finished good, or a secondary/scrap row. ``StockEntry.set_basic_rate`` tests the flag BEFORE
its derivation branches::

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

* rows with ``set_basic_rate_manually`` -- ERPNext skips them, and the Process Loss / conversion
  pricers in ``loss_valuation`` depend on the flag to permit a legitimately zero value;
* non-customer rows -- company-only manufacturing is unchanged, including the unconditional flag on
  scrap rows built in ``doc_events/stock_entry.py``;
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
	"""Whether ERPNext derives this row's rate from what ``se`` consumed.

	Mirrors ``StockEntry.set_basic_rate``: it skips rows with a source warehouse or a manual rate,
	and only its finished-good and secondary branches derive a value. ``is_finished_item`` and
	``secondary_item_type`` are set by ``mark_finished_and_secondary_items`` during ``validate``, so
	they are reliable inside ``set_basic_rate``; on a freshly built Repack row they may not be set
	yet in ``before_validate``, which is why ``set_basic_rate`` is the authoritative enforcement point.
	"""
	return bool(
		_get(se, "purpose") in DERIVED_OUTPUT_PURPOSES
		and not _get(row, "s_warehouse")
		and _get(row, "t_warehouse")
		and not _get(row, "set_basic_rate_manually")
		and (_get(row, "is_finished_item") or _get(row, "secondary_item_type"))
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


def should_allow_zero_valuation(row, se):
	"""Whether ``before_validate`` may stamp ``allow_zero_valuation_rate`` on ``row``.

	Customer-owned rows, except a derived output this module governs -- ``set_basic_rate`` decides
	that row's flag after ERPNext has valued it. An entry with no consumed row keeps the old
	behaviour, flag included, because there ERPNext's fallback is not suppressed.
	"""
	return _get(row, "inventory_type") in CUSTOMER_INVENTORY_TYPES and not _is_governed(
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
	"""Give each governed row the flag if and only if ERPNext derived exactly zero for it.

	The end state is decided here, not by whichever builder or hook touched the row first. Under Zero
	Value a Manufacture whose every input is customer metal legitimately derives 0, and the flag is
	what lets the ledger accept that zero. A non-zero derivation stays unflagged, so no later pass or
	repost can wipe it.
	"""
	for row in governed:
		row.allow_zero_valuation_rate = 0 if flt(row.get("basic_rate")) else 1
