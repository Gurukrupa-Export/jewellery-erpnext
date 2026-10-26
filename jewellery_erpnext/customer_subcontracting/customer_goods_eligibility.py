# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Whether an Item MAY be Customer Goods -- the one rule, read from the Item master.

CAPABILITY, NEVER OWNERSHIP
---------------------------
``Item.custom_inventory_type_can_be_customer_goods`` says an item is *permitted* to represent a
customer's goods. It does not say that any quantity of it *is* a customer's: ownership still
comes from the transaction -- the batch, the row's ``inventory_type`` and its customer (see
``customization/utils/row_ownership.py``). Nothing here may be read as "this stock belongs to a
customer".

WHY THIS IS THE ONLY SOURCE
---------------------------
Until 2026-09-28 a Customer Gold receipt also consulted a second whitelist,
``Subcontracting Settings.customer_gold_items``. Two lists for one fact disagreed in production
(the configured 24KT item was unflagged on the Item while flagged items were missing from the
list), so the Settings list was removed and every reader now comes here.

No cache is kept: a flag ticked or cleared on the Item is seen by the very next transaction, and
there is nothing to invalidate. An Item that does not exist is simply not eligible -- this never
fails open.
"""

import frappe

#: The Item Check field that grants the capability.
CUSTOMER_GOODS_FLAG = "custom_inventory_type_can_be_customer_goods"

#: The field's label, exactly as every source defines it (gke_customization's fixture, this app's
#: ``custom_fields/item.json`` and the Custom Field row on every site). A constant rather than a
#: ``get_meta`` lookup because the receipt suites patch ``frappe.db.get_value`` wholesale, and a
#: meta load resolves through that accessor.
CUSTOMER_GOODS_FLAG_LABEL = "Inventory Type Can be Customer Goods"

#: Item templates whose items are customer GOLD: metal and findings. Stones are ``D`` / ``G``.
#: Gold is priced from the gold rate; anything else a customer hands over is not.
CUSTOMER_GOLD_TEMPLATES = ("M", "F")


def get_customer_goods_eligible_items(item_codes, batch_controlled=False):
	"""The subset of ``item_codes`` whose Item master allows Customer Goods.

	One query for the whole document, never one per row. Blank and repeated codes are dropped,
	and an empty input costs no query at all.

	``batch_controlled`` narrows the answer to items that can also carry a batch -- for a caller
	about to MINT one, where a flagged item without batches would fail ERPNext's own check
	("The selected item cannot have Batch") instead of simply being skipped.
	"""
	codes = sorted({code for code in item_codes or () if code})
	if not codes:
		return set()

	filters = {"name": ["in", codes], CUSTOMER_GOODS_FLAG: 1}
	if batch_controlled:
		filters["has_batch_no"] = 1

	return set(frappe.get_all("Item", filters=filters, pluck="name"))


def can_be_customer_goods(item_code):
	"""Single-item form of :func:`get_customer_goods_eligible_items`.

	Reads through ``frappe.db.get_value`` on purpose: the batch, refining and manufacturing
	suites fake the flag by answering ``get_value("Item", name, fieldname)``.
	"""
	if not item_code:
		return False
	return bool(frappe.db.get_value("Item", item_code, CUSTOMER_GOODS_FLAG))
