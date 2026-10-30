"""Add the gold/silver catalogue image fields to Item.

Item already carries ``custom_catalogue_image`` ("Gold Catalogue Image", Attach) under the
Images tab. This adds its preview and a silver counterpart in a second column:

    images tab -> custom_catalogue_image -> custom_gold_catalogue_image_preview
                  | custom_column_break_jn90p | custom_silver_image

``custom_column_break_jn90p`` is the anchor the requested ``custom_silver_image`` names, but
no such field exists in the app or on the site, so it is created here as well.

``custom_catalogue_image`` is re-asserted (not just assumed) so a fresh install, where only
this patch and ``custom_fields/item.json`` provision it, lands on the same definition.

Idempotent: ``create_custom_fields`` is keyed on ``(dt, fieldname)``. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_item_catalogue_image_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPE = "Item"
MODULE = "GKE Order Forms"

FIELDS = (
	{
		"fieldname": "custom_catalogue_image",
		"fieldtype": "Attach",
		"label": "Gold Catalogue Image",
		"insert_after": "images",
		"module": MODULE,
	},
	{
		"fieldname": "custom_gold_catalogue_image_preview",
		"fieldtype": "Image",
		"label": "Gold Catalogue Image Preview",
		"insert_after": "custom_catalogue_image",
		"options": "custom_catalogue_image",
	},
	{
		"fieldname": "custom_column_break_jn90p",
		"fieldtype": "Column Break",
		"insert_after": "custom_gold_catalogue_image_preview",
		"module": MODULE,
	},
	{
		"fieldname": "custom_silver_image",
		"fieldtype": "Attach",
		"label": "Silver Catalogue Image",
		"insert_after": "custom_column_break_jn90p",
		"module": MODULE,
	},
)


def execute():
	create_custom_fields({DOCTYPE: list(FIELDS)}, ignore_validate=True)
	frappe.clear_cache(doctype=DOCTYPE)
