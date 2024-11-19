import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)


def update_pure_qty(self):
	self.custom_total_quantity = 0
	pure_item_purity = None
	for row in self.items:
		if row.custom_variant_of in ["M", "F"]:

			if not pure_item_purity:
				pure_item = frappe.db.get_value("Manufacturing Setting", self.company, "pure_gold_item")

				if not pure_item:
					frappe.throw(_("Pure Item not mentioned in Manufacturing Setting"))

				pure_item_purity = get_purity_percentage(pure_item)

			item_purity = get_purity_percentage(row.custom_alternative_item or row.item_code)

			if not item_purity:
				continue

			if pure_item_purity == item_purity:
				row.custom_pure_qty = row.qty

			else:
				row.custom_pure_qty = flt((item_purity * row.qty) / pure_item_purity, 3)

		self.custom_total_quantity += row.qty
