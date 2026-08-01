"""Backfill the UOM rows the refining Purchase Order needs on the REF-* category items.

A PO line is now billed under the matched Refinery Price List's own item (REF-RMS-001,
REF-MD-001, ...) rather than the generic REF-SVC-001 charge item. Those categories are
seeded with only their stock UOM -- Gram, or Litre for REF-UL-001 -- while a Flat Charge
slab bills ``1 Nos``. Without a conversion row the Purchase Order is rejected on UOM.

``seed_refining_masters._ensure_service_item_uoms`` was widened to cover them, but that
patch is already in the Patch Log of every existing site, so it will never run again there.
This patch re-invokes it, which is idempotent: it only appends UOMs that are missing.
"""

import frappe

from jewellery_erpnext.patches.seed_refining_masters import _ensure_service_item_uoms


def execute():
	if not frappe.db.exists("DocType", "Item"):
		return
	_ensure_service_item_uoms()
