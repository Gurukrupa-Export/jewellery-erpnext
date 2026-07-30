"""Seed the Refinery Price List from ``Process Dust With Rate.xlsx`` (Sheet 1).

Loads the pricing as DATA (records), not as code. The price list holds ONE document per
ITEM (Sheet 1 col B), with its weight-band rates in the ``slabs`` child table. The
sheet's Process column (col A) is no longer stored — it only labelled the row — so rows
differing by process alone are deduped into one slab. Each parent is idempotent (an
existing document for the item is skipped). Depends on
``seed_refining_masters`` for the dust items — an item that does not exist is skipped
with a warning rather than aborting the migrate.

``to_weight = 0`` encodes "no upper bound" ("Above N"). Weights are in grams. Charge/
rate/basis are normalised from the mixed sheet formats:
  * flat amount → Flat Charge; "₹N/gm" → Per Gram; "₹N/kg" → Per Kg
  * "fine" → Received Fine Weight; "burn" → After Burning Weight; else Gross Weight

Wired here (``post_model_sync``). Run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.seed_refinery_price_list.execute
"""

import frappe

# process, dust_item, band_label, from_g, to_g, charge_type, rate, weight_basis
ROWS = [
	(
		"Filing/Setting/Grinding",
		"REF-MD-001",
		"0.0 gm - 50 gm",
		0,
		50,
		"Flat Charge",
		800,
		"Gross Weight",
	),
	(
		"Filing/Setting/Grinding",
		"REF-MD-001",
		"Above 50 gm",
		50,
		0,
		"Per Gram",
		18,
		"Received Fine Weight",
	),
	(
		"Polishing/Vacuum",
		"REF-VB-001",
		"Below 1 kg",
		0,
		1000,
		"Per Kg",
		1800,
		"Gross Weight",
	),
	(
		"Polishing/Vacuum",
		"REF-VB-001",
		"Above 1 kg",
		1000,
		0,
		"Per Kg",
		1800,
		"After Burning Weight",
	),
	(
		"Setting Tank",
		"REF-ST-001",
		"After burn >1kg",
		1000,
		0,
		"Per Kg",
		2000,
		"After Burning Weight",
	),
	(
		"Refining Metal",
		"REF-RMS-001",
		"0.0 gm - 50 gm",
		0,
		50,
		"Flat Charge",
		750,
		"Gross Weight",
	),
	(
		"Refining Metal",
		"REF-RMS-001",
		"Above 50 gm",
		50,
		0,
		"Per Gram",
		15,
		"Gross Weight",
	),
	(
		"Refining Studded Jewellery",
		"REF-FSJ-001",
		"0.0 gm - 50 gm",
		0,
		50,
		"Flat Charge",
		800,
		"Gross Weight",
	),
	(
		"Refining Studded Jewellery",
		"REF-FSJ-001",
		"Above 50 gm",
		50,
		0,
		"Per Gram",
		18,
		"Gross Weight",
	),
	(
		"Floor/Vacuum Cleaning",
		"REF-VB-001",
		"Per kg",
		0,
		0,
		"Per Kg",
		2200,
		"Gross Weight",
	),
	("Burnout Residue", "REF-CF-001", "Per kg", 0, 0, "Per Kg", 2200, "Gross Weight"),
	(
		"Burnout Residue",
		"REF-NB-001",
		"After Burnout",
		0,
		0,
		"Per Kg",
		1800,
		"After Burning Weight",
	),
	(
		"Chemical Process",
		"REF-MD-001",
		"0.0 gm - 50 gm",
		0,
		50,
		"Flat Charge",
		800,
		"Gross Weight",
	),
	(
		"Chemical Process",
		"REF-MD-001",
		"Above 50 gm",
		50,
		0,
		"Per Gram",
		18,
		"Gross Weight",
	),
	(
		"Tools Scrap",
		"REF-TD-001",
		"Below 1kg/Above 1kg",
		0,
		0,
		"Per Kg",
		15000,
		"After Burning Weight",
	),
	(
		"Setting Tank",
		"REF-UL-001",
		"After burn >1kg",
		1000,
		0,
		"Per Kg",
		2000,
		"After Burning Weight",
	),
]


def execute():
	if not frappe.db.exists("DocType", "Refinery Price List"):
		return
	if not frappe.db.has_column("Refinery Price List", "item"):
		# Pre-restructure schema (shouldn't happen post model sync) — bail out.
		return

	# Group the sheet rows item-wise: one parent document per item, slabs as children.
	# The Refining Process and Band Label columns were removed from the slab — the process
	# only ever labelled the row, and two rows that differed by process alone now collapse
	# into ONE slab (deduped below), which is what the band-overlap guard requires.
	by_item = {}
	for (
		_process,
		dust_item,
		_band_label,
		from_g,
		to_g,
		charge_type,
		rate,
		basis,
	) in ROWS:
		slab = {
			"from_weight": from_g,
			"to_weight": to_g,
			"charge_type": charge_type,
			"rate": rate,
			"weight_basis": basis,
		}
		slabs = by_item.setdefault(dust_item, [])
		if slab not in slabs:
			slabs.append(slab)

	created = 0
	for item_code, slabs in by_item.items():
		if not frappe.db.exists("Item", item_code):
			frappe.logger().warning(
				f"seed_refinery_price_list: item {item_code} missing; skipped its price list"
			)
			continue
		if frappe.db.exists("Refinery Price List", {"item": item_code}):
			continue
		doc = frappe.get_doc({"doctype": "Refinery Price List", "item": item_code})
		for slab in slabs:
			doc.append("slabs", slab)
		# The sheet itself carries overlapping bands for a couple of items (same band, two
		# weight bases). Seeding must never abort the migrate over price-sheet hygiene, so
		# report and skip rather than throw.
		try:
			doc.insert(ignore_permissions=True)
		except frappe.ValidationError as e:
			frappe.logger().warning(
				f"seed_refinery_price_list: {item_code} not seeded — {e}"
			)
			continue
		created += 1
	frappe.logger().info(f"seed_refinery_price_list: created {created} price lists")
