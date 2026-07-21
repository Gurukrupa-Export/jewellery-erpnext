"""
Register the "Repair Unpack" stock entry type in MOP Settings' reservation table.

A Manufacturing Operation's weights (gross_wt etc.) are fed only by MOP Log rows,
and MOP Log rows are created on Stock Entry submit ONLY when the SE's
stock_entry_type is present in the "Stock Entry Type To Reservation" child table
of MOP Settings (jewellery_erpnext/doc_events/stock_entry.py:onsubmit gate).

The repair "Unpack Raw Material" flow submits a Stock Entry of type "Repair
Unpack", which was NOT in that table -- so the unpacked raw materials never
surfaced in the Manufacturing Operation and its gross_wt stayed 0. This patch
adds a "Repair Unpack" row (is_increase_weight, like "Material Transfer (WORK
ORDER)") so the unpack reserves + logs like any other work-order inward move.

MOP Settings is a Single whose child config is not auto-seeded on existing sites
(create_test_data only seeds an empty table on fresh installs), so it must be
provisioned by this patch. Idempotent. Ad-hoc entry point:
  bench --site <site> execute jewellery_erpnext.patches.add_repair_unpack_to_mop_reservation.execute
"""

import frappe


def execute():
	# The child row is a Link to Stock Entry Type; skip if the master isn't there yet.
	if not frappe.db.exists("Stock Entry Type", "Repair Unpack"):
		return

	settings = frappe.get_single("MOP Settings")
	existing = {
		row.stock_entry_type_to_reservation
		for row in settings.stock_entry_type_to_reservation
	}
	if "Repair Unpack" in existing:
		return

	settings.append(
		"stock_entry_type_to_reservation",
		{
			"stock_entry_type_to_reservation": "Repair Unpack",
			"is_increase_weight": 1,
		},
	)
	settings.save(ignore_permissions=True)
	frappe.db.commit()
