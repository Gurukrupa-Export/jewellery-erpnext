"""Provision the MOP EOD Sync reporting + allocation schema on existing sites.

Doctype JSON changes only reach a site when its DocType is reloaded, so every field
added here is wired in BOTH places, per the app convention: this ``post_model_sync``
patch (existing sites) and ``create_test_data.setup_data`` (fresh / CI sites).

What this provisions and why it matters:

* ``MOP EOD Sync Log Item.sync_stage`` gains ``WIP Reservation Healed`` and
  ``Allocate Bucket Stock``. The reservation healer wrote a ``sync_stage`` value that was
  not a valid Select option, so ``_insert_sync_log_item``'s ``doc.insert()`` raised from
  inside the Phase-2 savepoint and rolled the whole bucket back to a draft -- 741 child
  rows across 8 nightly runs. Until this reload runs, the live ``tabDocField.options``
  row still lacks the value and the code fix is inert.
* ``MOP EOD Sync Log Item.status`` gains ``Deferred``, and ``error_type`` gains
  ``Deferred - Bucket Stock Contention`` / ``Permanently Short``, so the bucket allocator
  can distinguish "lost tonight's race for shared stock" from "will never fit".
* ``MOP EOD Sync Log`` gains ``draft_items`` / ``draft_qty``. Draft rows used to be
  counted as failures, so a run reported ``failed_items = 1349`` when only 277 rows had
  actually failed and 1072 were recoverable drafts.
* ``MOP Settings`` gains the two feature flags, both shipping OFF.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	for doctype in (
		"MOP EOD Sync Log Item",
		"MOP EOD Sync Log",
		"MOP Settings",
	):
		if frappe.db.exists("DocType", doctype):
			frappe.reload_doctype(doctype, force=True)

	# ``Manufacturing Operation.last_eod_sync_on`` lives in
	# custom_fields/manufacturing_operation.json, but the after_migrate hook that would
	# install custom fields is commented out, so it is absent on sites that never got it
	# from a fixture. _stamp_last_eod_sync runs INSIDE the EOD Phase-2 savepoint, so a
	# 1054 there rolled back the whole bucket -- a sound transfer held as a draft for the
	# sake of an audit timestamp. Provision it here so the stamp works rather than being
	# skipped. Idempotent.
	if frappe.db.exists("DocType", "Manufacturing Operation"):
		create_custom_fields(
			{
				"Manufacturing Operation": [
					{
						"fieldname": "last_eod_sync_on",
						"fieldtype": "Datetime",
						"label": "Last EOD Sync On",
						"insert_after": "is_received_gross_greater_than",
						"is_system_generated": 1,
						"no_copy": 1,
						"read_only": 1,
						"module": "Jewellery Erpnext",
					}
				]
			},
			ignore_validate=True,
		)

	# Both flags ship OFF. The allocator changes which MWOs enter a transfer and the
	# plan-phase heal submits real Stock Reservation Entries, so each is enabled
	# deliberately after a verified staging run -- never silently by a migrate.
	#
	# Guarded: get_single_value THROWS InvalidColumnName for a field the site does not
	# have, so if the reload above could not run (a site missing core tables cannot
	# reload doctypes) this must not abort the whole migrate. The engine reads these
	# flags through _eod_feature_enabled, which treats an absent field as OFF.
	for fieldname in ("enable_eod_bucket_allocation", "enable_eod_plan_sre_heal"):
		try:
			if frappe.db.get_single_value("MOP Settings", fieldname) is None:
				frappe.db.set_single_value("MOP Settings", fieldname, 0)
		except Exception:
			frappe.log_error(
				title="MOP EOD Sync: could not seed feature flag",
				message=f"{fieldname} is absent from MOP Settings after reload_doctype.",
			)

	frappe.db.commit()
