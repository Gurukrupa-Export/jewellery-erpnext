"""Clean up the Refinery Price Slabs left ambiguous by dropping the Refining Process column.

Slabs used to be distinguished by ``refining_process`` as well as by weight band, so one
price list could legitimately hold two rows covering the same band under different
processes. Without that column those rows are indistinguishable, and ``pick_price_slab``
resolves them by row order — an arbitrary tie-break on money.

Two cases, handled differently on purpose:

* **Byte-identical duplicates** (same from/to weight, charge type, weight basis and rate)
  carry no information at all, so the later rows are deleted. On gk.site this is RFP-00001,
  whose "Filing/Setting/Grinding" and "Chemical Process" rows are the same 0-50 g Flat 800.

* **Genuine overlaps** (same band, DIFFERENT basis or rate) are a pricing decision — e.g.
  RFP-00001 bills 50 g-∞ at 18/g on Received Fine Weight in one row and on Gross Weight in
  another. Those are REPORTED, never touched: picking one would silently change what the
  refinery is paid. Until an operator resolves them, the existing first-by-row-order
  behaviour continues, and ``validate_slab_bands`` will refuse the next manual save of that
  document — which is the prompt to fix it.

Idempotent: deleting duplicates is a no-op once they are gone, and reporting writes nothing.

    bench --site <site> execute jewellery_erpnext.patches.dedupe_refinery_price_slabs.execute
"""

import frappe
from frappe.utils import flt

FIELDS = ("from_weight", "to_weight", "charge_type", "weight_basis", "rate")


def _identity(slab):
	return (
		flt(slab.from_weight),
		flt(slab.to_weight),
		slab.charge_type,
		slab.weight_basis,
		flt(slab.rate),
	)


def _overlaps(a, b):
	# STRICT inequality, matching validate_slab_bands: bands that merely touch at a
	# boundary ("0.0 gm - 50 gm" + "Above 50 gm") are contiguous, not overlapping, and
	# pick_price_slab resolves the shared endpoint to the first row. Comparing with <=
	# reported every ordinary contiguous pair, burying the genuine conflicts this patch
	# exists to surface. Must stay in step with validate_slab_bands, which the docstring
	# above points operators at.
	a_hi = flt(a.to_weight) or float("inf")
	b_hi = flt(b.to_weight) or float("inf")
	return flt(a.from_weight) < b_hi and flt(b.from_weight) < a_hi


def execute():
	if not frappe.db.table_exists("Refinery Price Slab"):
		return

	slabs = frappe.get_all(
		"Refinery Price Slab",
		filters={"parenttype": "Refinery Price List"},
		fields=["name", "parent", "idx", *FIELDS],
		order_by="parent asc, idx asc",
	)
	by_parent = {}
	for slab in slabs:
		by_parent.setdefault(slab.parent, []).append(slab)

	deleted = 0
	overlaps = []
	for parent, rows in by_parent.items():
		seen = {}
		survivors = []
		for slab in rows:
			key = _identity(slab)
			if key in seen:
				frappe.db.delete("Refinery Price Slab", {"name": slab.name})
				deleted += 1
				continue
			seen[key] = slab.name
			survivors.append(slab)

		for i, a in enumerate(survivors):
			for b in survivors[i + 1 :]:
				if _overlaps(a, b):
					overlaps.append(
						f"{parent}: rows #{a.idx} and #{b.idx} both cover "
						f"{flt(a.from_weight)}-{a.to_weight or '∞'} g "
						f"({a.charge_type}/{a.weight_basis}/{a.rate} vs "
						f"{b.charge_type}/{b.weight_basis}/{b.rate})"
					)

	frappe.logger().info(
		f"dedupe_refinery_price_slabs: deleted {deleted} identical duplicate slab(s)"
	)
	if overlaps:
		message = (
			"dedupe_refinery_price_slabs: UNRESOLVED overlapping bands — "
			+ "; ".join(overlaps)
		)
		frappe.logger().warning(message)
		print(message)
