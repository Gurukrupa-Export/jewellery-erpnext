# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pure math for the Gold Recovery Details per-karat split.

No Frappe document access, no DB — every function here takes and returns plain numbers, so the
invariants can be unit-tested without a site.

WHY THIS MODULE EXISTS
----------------------
The Recovery Summary (parent) and the per-karat Gold Recovery Details table must agree to the
milligram. They used to disagree because each row rounded its own share independently
(sum-of-rounded) while the parent rounded the totals once (round-of-sum) — a drift of roughly
1 mg per extra karat, the reported "1 mg variation with more than one touch of gold".

That was "fixed" by making the parent adopt the table's columns. It is the wrong direction:
the table is written once per lifecycle by two button actions that become unreachable when the
entry leaves "Refining In Progress", while the parent is recomputed on every save. Adopting the
table let rows written by a superseded formula redefine `refining_loss` — which is minted as
pure-24KT dust by the repack Stock Entry.

So the dependency runs the other way: the parent totals are authoritative, and the table is
apportioned FROM them such that each column sums back to its parent total EXACTLY, for any
number of karats. ``apportion`` is what makes that exact rather than approximate.

LOSS IS APPORTIONED, NOT CLAMPED PER ROW
----------------------------------------
``build_row_targets`` splits the loss proportionally to pure content instead of computing a
per-row ``max(pure - recovered, 0)``. On a consistent table the two are identical
(``pure_i * (1 - r) == loss * pure_i / P``), but the per-row clamp discards a row's negative
against another row's positive, so the column can sum to more than the gold that actually went
missing. That is what let one real entry read 0.860 g of loss where the mass balance said
0.033 g. Apportioning cannot do that: the parts always sum to the whole.
"""

from decimal import ROUND_HALF_UP, Decimal

# Deliberately NOT frappe.utils.flt. `flt(value, precision)` resolves the rounding method
# through frappe.local and returns 0.0 when there is no site bound — which would turn every
# percentage here into zero in any context that lacks one. This module is pure math by design,
# so it rounds itself, half-up, matching Frappe's default `rounded`.


def _round(value, precision):
	"""Half-up rounding, independent of any site context."""
	quantum = Decimal(1).scaleb(-precision)
	return float(Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP))


def flt(value):
	"""Coerce to float, treating None/"" as 0 — the only part of frappe's flt we need."""
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def apportion(total, weights, precision=3):
	"""Split ``total`` across ``weights`` so the parts sum to ``total`` EXACTLY.

	Largest-remainder (Hare quota) in integer units of ``10**-precision``: floor every share,
	then hand the leftover units out one at a time to the largest fractional remainders. Ties
	break on index so the result is deterministic.

	Returns a list of floats the same length as ``weights``, with
	``sum(result) == round(total, precision)`` to the last representable digit. A non-positive
	total or an all-zero weight vector yields zeros — callers treat that as "nothing to split".
	"""
	scale = 10**precision
	count = len(weights)
	if count == 0:
		return []

	total_units = int(_round(flt(total) * scale, 0))
	weight_sum = sum(flt(w) for w in weights)
	if total_units <= 0 or weight_sum <= 0:
		return [0.0] * count

	exact = [flt(w) / weight_sum * total_units for w in weights]
	units = [int(u) for u in exact]
	leftover = total_units - sum(units)

	if leftover:
		order = sorted(range(count), key=lambda i: (-(exact[i] - units[i]), i))
		for position in range(leftover):
			units[order[position % count]] += 1

	return [u / scale for u in units]


def build_row_targets(pure_weights, refined_fine_weight, refining_loss, precision=3):
	"""Canonical per-karat columns derived from the authoritative parent totals.

	``pure_weights`` is the per-karat pure gold content in input order. ``refined_fine_weight``
	and ``refining_loss`` are the parent's own figures — the ones the mass balance and the
	repack Stock Entry use.

	Returns a list of dicts with ``pure_gold_weight`` / ``recovered_weight`` / ``loss_weight`` /
	``recovery_pct``, guaranteeing:

	    sum(pure_gold_weight) == round(sum(pure_weights), precision)
	    sum(recovered_weight) == round(refined_fine_weight, precision)
	    sum(loss_weight)      == round(refining_loss, precision)

	``recovery_pct`` mirrors the parent's display rule: capped at 99.99 whenever the row still
	carries a loss, so a row never shows "100.00" next to a non-zero loss column.
	"""
	pure = [flt(p) for p in pure_weights]
	if not pure:
		return []

	pure_rounded = apportion(sum(pure), pure, precision)
	recovered = apportion(refined_fine_weight, pure, precision)
	loss = apportion(refining_loss, pure, precision)

	rows = []
	for index, pure_value in enumerate(pure_rounded):
		recovered_value = recovered[index]
		loss_value = loss[index]
		pct = _round((recovered_value / pure_value) * 100.0 if pure_value else 0.0, 2)
		pct = min(pct, 100.0)
		# Never show "100.00" next to a non-zero loss column — the same display rule the
		# Recovery Summary applies to recovery_percentage.
		if loss_value > 0 and pct > 99.99:
			pct = 99.99
		rows.append(
			{
				"pure_gold_weight": pure_value,
				"recovered_weight": recovered_value,
				"loss_weight": loss_value,
				"recovery_pct": pct,
			}
		)
	return rows
