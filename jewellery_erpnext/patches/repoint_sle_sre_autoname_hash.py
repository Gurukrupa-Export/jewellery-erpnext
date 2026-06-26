"""Repoint Stock Ledger Entry and Stock Reservation Entry naming to ``hash`` to
eliminate ``tabSeries`` counter-row lock contention.

Why
---
The single hottest lock under concurrent stock posting was the per-prefix
``tabSeries`` counter row, taken with a ``SELECT current ... FOR UPDATE`` on every
insert and held until COMMIT. Two of these counters fire constantly:

* **Stock Reservation Entry** (``MAT-SRE-.YYYY.-``) — created and submitted *per row*
  inside the Stock Entry submit cascade (``stock_reservation_entry_for_mwo``). It has
  no deferred-rename optimisation, so it locks ``tabSeries[MAT-SRE-...]`` **inside the
  user's submit transaction**, for the whole cascade. This is a direct contributor to
  the 1205/1213 errors. Repointing to ``hash`` removes the counter row entirely — there
  is nothing left to lock.
* **Stock Ledger Entry** (``MAT-SLE-.YYYY.-``) — already inserts with a *temporary*
  hash name (``StockLedgerEntry.autoname``) and is renamed later by the scheduled
  ``rename_gle_sle_docs`` job. ERPNext explicitly supports permanent hash naming: when
  ``meta.autoname == "hash"`` the controller sets ``to_rename = 0``, so the deferred
  rename (and its ``tabSeries[MAT-SLE-...]`` locking + bulk UPDATE load) is skipped
  altogether.

Both names are internal ledger keys — never shown to users and never parsed/sorted by
code (verified: only test fixtures reference the old ``MAT-SLE-``/``MAT-SRE-`` formats;
SLE ordering everywhere is by ``posting_date, posting_time, creation``).

Mechanism
---------
Applied as reversible **Property Setter** rows on the ``autoname`` (and ``naming_rule``)
DocType properties, so erpnext core JSON is untouched and survives app upgrades. Existing
``MAT-SLE-``/``MAT-SRE-`` named documents keep their names (no rename); only newly created
docs get hash names. The two formats coexist safely.

Reversible: delete the Property Setter rows (or this patch's effect) to restore the core
``MAT-SLE-.YYYY.-.#####`` / ``MAT-SRE-.YYYY.-.#####`` autoname.

Idempotent: deletes any prior matching DocType-level Property Setter before re-creating it,
so re-running (or running after more docs exist) is safe.
"""

import frappe

_DOCTYPES = ("Stock Ledger Entry", "Stock Reservation Entry")
# (property, value) pairs applied at DocType level.
_PROPS = (
	("autoname", "hash"),
	("naming_rule", "Random"),
)


def execute():
	for doctype in _DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue

		for prop, value in _PROPS:
			# Idempotent: a Property Setter's name is derived from (doc_type, field, property),
			# so a second insert would collide — drop the prior DocType-level row first.
			frappe.db.delete(
				"Property Setter",
				{"doc_type": doctype, "doctype_or_field": "DocType", "property": prop},
			)
			# ``frappe.make_property_setter`` is the dict-based public API; the positional
			# ``for_doctype=`` form lives on the lower-level
			# ``frappe.custom...property_setter.make_property_setter`` helper, not here.
			frappe.make_property_setter(
				{
					"doctype_or_field": "DocType",  # DocType-level property, no field
					"doctype": doctype,
					"property": prop,
					"value": value,
					"property_type": "Data",  # canonical for autoname & naming_rule
				},
			)

		frappe.clear_cache(doctype=doctype)

	frappe.db.commit()
