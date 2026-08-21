"""Resolving the Customer linked to a Supplier through a Party Link.

ERPNext stores a Party Link in EITHER orientation. ``PartyLink.validate`` only
requires ``primary_role`` to be "Customer" or "Supplier", so the same pair reaches
the database two different ways:

    primary_role=Customer, primary_party=<customer>, secondary_party=<supplier>
    primary_role=Supplier, primary_party=<supplier>, secondary_party=<customer>

Both are produced by the same UI, and nothing normalises them. Querying only one
orientation silently returns nothing for a link stored the other way -- there is no
error, the customer just comes back empty.

That is not hypothetical: Party Link ``ACC-PT-LNK-022`` stores supplier
``DLSU0006`` as the *primary* party. A Purchase Receipt against that supplier --
which is ticked ``custom_consider_purchase_receipt_as_customergoods`` -- therefore
found no customer and booked its rows as Regular Stock with a blank customer
(PR-26-00005), instead of that customer's Customer Goods.
"""

import frappe


def get_linked_customer(supplier):
	"""The Customer linked to ``supplier``, whichever way the Party Link is stored.

	Checks the Customer-primary orientation first (the one the app originally
	assumed), then the Supplier-primary orientation. Returns None when the supplier
	has no Party Link at all.
	"""
	if not supplier:
		return None

	customer = frappe.db.get_value(
		"Party Link",
		{
			"primary_role": "Customer",
			"secondary_role": "Supplier",
			"secondary_party": supplier,
		},
		"primary_party",
	)
	if customer:
		return customer

	return frappe.db.get_value(
		"Party Link",
		{
			"primary_role": "Supplier",
			"secondary_role": "Customer",
			"primary_party": supplier,
		},
		"secondary_party",
	)
