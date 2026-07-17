"""Restructure Refinery Price List from one-row-per-(process, item, band) documents to
one document per ITEM with a ``slabs`` child table (Refinery Price Slab).

The price sheet's second column is the ITEM; every item is refined under multiple
processes (first column), so the natural shape is item-parent + process/band child
rows. Old-style documents (recognisable by their orphaned ``dust_item`` column being
set while the new ``item`` field is empty) are grouped by (dust_item, supplier,
company); the FIRST document of each group survives (keeping its name, so existing
``Purchase Order Item.custom_refining_price_list`` back-links stay resolvable), gains
``item`` + one slab per old row, and the rest are deleted after repointing any PO-line
back-links to the survivor.

Runs post_model_sync (the new ``item``/``slabs`` schema exists; the old columns
survive as orphans — Frappe never drops columns on field removal). Idempotent: a
converted survivor has ``item`` set and no longer matches the old-row filter; deleted
duplicates are gone.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Refinery Price List"):
		return
	if not frappe.db.has_column("Refinery Price List", "dust_item"):
		# Fresh site: never had the old schema, nothing to restructure.
		return

	old_rows = frappe.db.sql(
		"""
		SELECT name, refining_process, dust_item, band_label, from_weight, to_weight,
			charge_type, rate, weight_basis, service_item, supplier, company,
			currency, effective_from
		FROM `tabRefinery Price List`
		WHERE IFNULL(dust_item, '') != '' AND IFNULL(item, '') = ''
		ORDER BY creation ASC
		""",
		as_dict=True,
	)
	if not old_rows:
		return

	groups = {}
	for row in old_rows:
		key = (row.dust_item, row.supplier or "", row.company or "")
		groups.setdefault(key, []).append(row)

	converted = 0
	deleted = 0
	for (dust_item, supplier, company), rows in groups.items():
		survivor_name = rows[0].name

		# Repoint PO-line back-links from the soon-deleted duplicates to the survivor
		# BEFORE deleting them (they may sit on submitted POs, so direct SQL).
		duplicate_names = [r.name for r in rows[1:]]
		if duplicate_names:
			frappe.db.sql(
				"""
				UPDATE `tabPurchase Order Item`
				SET custom_refining_price_list = %s
				WHERE custom_refining_price_list IN %s
				""",
				(survivor_name, tuple(duplicate_names)),
			)

		survivor = frappe.get_doc("Refinery Price List", survivor_name)
		survivor.item = dust_item
		survivor.supplier = supplier or None
		survivor.company = company or None
		survivor.currency = rows[0].currency
		survivor.effective_from = rows[0].effective_from
		survivor.set("slabs", [])
		for row in rows:
			survivor.append(
				"slabs",
				{
					"refining_process": row.refining_process
					if row.refining_process
					and frappe.db.exists("Refining Process", row.refining_process)
					else None,
					"band_label": row.band_label,
					"from_weight": row.from_weight,
					"to_weight": row.to_weight,
					"charge_type": row.charge_type,
					"rate": row.rate,
					"weight_basis": row.weight_basis,
					"service_item": row.service_item,
				},
			)
		survivor.flags.ignore_permissions = True
		survivor.save(ignore_permissions=True)
		converted += 1

		for dup in duplicate_names:
			frappe.delete_doc(
				"Refinery Price List",
				dup,
				force=1,
				ignore_permissions=True,
				delete_permanently=True,
			)
			deleted += 1

	frappe.logger().info(
		f"restructure_refinery_price_list: converted {converted} item price lists, "
		f"deleted {deleted} old row-documents"
	)
