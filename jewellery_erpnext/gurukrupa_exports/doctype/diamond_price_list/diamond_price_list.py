# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class DiamondPriceList(Document):
	pass


def on_doctype_update():
	frappe.db.add_index(
		"Diamond Price List",
		["price_list", "diamond_type", "stone_shape", "diamond_quality", "price_list_type"],
		index_name="bom_rate",
	)
