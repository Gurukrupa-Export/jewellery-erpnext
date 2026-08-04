# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

from frappe.utils import flt

# ERPNext exempts this purpose from the allow-zero-valuation wipe, so a rate
# entered on such an entry survives on basic_rate and needs no rescue.
_ZERO_VALUATION_EXEMPT_PURPOSE = "Receive from Customer"


def capture_entered_metal_rates(se):
	"""Rates ERPNext's ``set_basic_rate`` is about to discard, as ``[(row, rate)]``.

	Mirrors the guard clauses of that method so nothing is captured which it
	would have left alone: a row with a source warehouse or
	``set_basic_rate_manually`` is skipped outright, and the wipe itself only
	fires for an allow-zero-valuation row that carries a rate on a purpose other
	than "Receive from Customer".
	"""
	if getattr(se, "purpose", None) == _ZERO_VALUATION_EXEMPT_PURPOSE:
		return []

	captured = []
	for row in se.get("items") or []:
		if row.get("s_warehouse") or row.get("set_basic_rate_manually"):
			continue

		if not row.get("allow_zero_valuation_rate"):
			continue

		rate = flt(row.get("basic_rate"))
		if rate:
			captured.append((row, rate))

	return captured


def restore_entered_metal_rates(captured):
	"""Park each captured rate on its row's ``custom_metal_rate``.

	Runs after ``super().set_basic_rate()``. A row whose ``basic_rate`` survived
	was not zeroed after all (a future ERPNext could narrow the condition), so it
	is left alone -- the existing fallback already reaches the right number.
	"""
	for row, rate in captured:
		if flt(row.get("basic_rate")):
			continue

		if flt(row.get("custom_metal_rate")):
			continue

		row.custom_metal_rate = rate
