"""Seed the Stock Entry Type masters -- create-only, never overwrite.

These 47 masters used to ship as ``fixtures/stock_entry_type.json``. That was actively
harmful: ``import_fixtures`` walks the whole ``fixtures/`` directory on every migrate
regardless of the ``fixtures`` hook, and the import **deletes and re-creates** each listed
record (``delete_old_doc``'s child-preserving branch is guarded on ``not
reset_permissions``, which is False there). Stock Entry Type carries
``custom_allowed_roles`` -- the per-role visibility whitelist -- as child rows *on the
record*, so every migrate destroyed whatever roles an administrator had configured in the
desk. Under the strict whitelist that is not a cosmetic loss: a type with no roles is
selectable by nobody.

The tell was in the data: after the 2026-08-28 migrate all 46 fixture-listed types carried
a fresh ``creation``, while ``Repack-Gemstone Conversion`` -- the one type the fixture
never listed -- still had its original ``2026-06-03`` timestamp, untouched.

So the fixture is gone and the masters are seeded here instead, guarded on
``frappe.db.exists``. An existing record is never touched, which is the entire point:
roles set in the desk now survive migrate. The trade-off is that this patch cannot
*update* a definition -- changing a ``purpose`` or ``add_to_transit`` on a live site needs
a desk edit or its own patch.

``is_standard`` is set only for the five names that carry it, because
``StockEntryType.validate_standard_type()`` throws if it is set on a name outside the 13
ERPNext standards. (The other 8 standard names sit at ``is_standard = 0`` on this site --
the old fixture had been resetting them for years. This patch does not repair that; it
only stops it getting worse.)

Because ``install-app`` marks patches complete WITHOUT running them on fresh / CI sites,
this is wired in two idempotent places per the app convention: this ``post_model_sync``
patch and ``create_test_data.setup_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.seed_stock_entry_types.execute

Idempotent: guarded on ``frappe.db.exists``.
"""

import frappe

# (name, purpose, add_to_transit, is_standard)
STOCK_ENTRY_TYPES = [
	("Broken / Loss", "Material Transfer for Manufacture", 0, 0),
	("Customer Goods Issue", "Material Issue", 0, 0),
	("Customer Goods Received", "Material Receipt", 0, 0),
	("Customer Goods Transfer", "Material Transfer", 1, 0),
	("Disassemble", "Disassemble", 0, 1),
	("Manufacture", "Manufacture", 0, 0),
	("Material - Lost", "Material Issue", 0, 0),
	(
		"Material Consumption for Manufacture",
		"Material Consumption for Manufacture",
		0,
		0,
	),
	("Material Issue", "Material Issue", 0, 0),
	("Material Issue  - Consumables", "Material Issue", 0, 0),
	("Material Issue - Sales Person", "Material Transfer", 0, 0),
	("Material Issue for Certification", "Material Transfer", 0, 0),
	("Material Issue for Hallmarking", "Material Transfer", 0, 0),
	("Material Receipt", "Material Receipt", 0, 0),
	("Material Receipt - Sales Person", "Material Transfer", 0, 0),
	("Material Receipt for Certification", "Material Transfer", 0, 0),
	("Material Receipt for Hallmarking", "Material Transfer", 0, 0),
	("Material Receive (WORK ORDER)", "Material Transfer", 0, 0),
	("Material Transfer", "Material Transfer", 0, 0),
	("Material Transfer (DEPARTMENT)", "Material Transfer", 1, 0),
	("Material Transfer (MAIN SLIP)", "Material Transfer", 0, 0),
	("Material Transfer (Subcontracting Work Order)", "Material Transfer", 0, 0),
	("Material Transfer (WORK ORDER)", "Material Transfer", 0, 0),
	("Material Transfer for Manufacture", "Material Transfer for Manufacture", 0, 0),
	("Material Transfer From Reserve", "Material Transfer", 0, 0),
	("Material Transfer to Branch", "Material Transfer", 0, 0),
	("Material Transfer to Department", "Material Transfer", 0, 0),
	("Material Transfer to Employee", "Material Transfer", 0, 0),
	("Material transfer to Reserve", "Material Transfer", 0, 0),
	("Material Transfer to Subcontractor", "Material Transfer", 0, 0),
	("Metal Conversion Repack", "Repack", 0, 0),
	("Process Loss", "Repack", 0, 0),
	("Receive from Customer", "Receive from Customer", 0, 1),
	("Repack", "Repack", 0, 0),
	("Repack - Diamond Sieve Size", "Repack", 0, 0),
	("Repack-Diamond Conversion", "Repack", 0, 0),
	("Repack-Gemstone Conversion", "Repack", 0, 0),
	("Repack-Metal Conversion", "Repack", 0, 0),
	("Repack-Repair Unpack", "Repack", 0, 0),
	("Repair Unpack", "Repack", 0, 0),
	("Return Raw Material to Customer", "Return Raw Material to Customer", 0, 1),
	("Send to Subcontractor", "Send to Subcontractor", 0, 0),
	("Subcontracting Delivery", "Subcontracting Delivery", 0, 1),
	("Subcontracting Repack", "Repack", 0, 0),
	("Subcontracting Return", "Subcontracting Return", 0, 1),
	("Work Order for Customer\xa0Approval Issue", "Material Transfer", 0, 0),
	("Work Order for Customer\xa0Approval Receive", "Material Transfer", 0, 0),
]


def execute():
	created = []
	for name, purpose, add_to_transit, is_standard in STOCK_ENTRY_TYPES:
		# Never touch an existing record -- that is the whole point of this patch.
		if frappe.db.exists("Stock Entry Type", name):
			continue

		doc = frappe.get_doc(
			{
				"doctype": "Stock Entry Type",
				"name": name,
				"purpose": purpose,
				"add_to_transit": add_to_transit,
			}
		)
		if is_standard:
			doc.is_standard = 1
		doc.insert(ignore_permissions=True)
		created.append(name)

	frappe.logger().info(
		f"seed_stock_entry_types: created {len(created)} Stock Entry Type(s): {created}"
		if created
		else "seed_stock_entry_types: all Stock Entry Types already present"
	)
