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
		if row.custom_variant_of in ["M", "F"] and self.custom_transfer_type != 'Transfer To Branch':

			if not pure_item_purity:
				# pure_item = frappe.db.get_value("Manufacturing Setting", self.company, "pure_gold_item")

				pure_item = frappe.db.get_value("Manufacturing Setting", {"manufacturer":self.custom_manufacturer}, "pure_gold_item")

				if not pure_item:
					# frappe.throw(_("Pure Item not mentioned in Manufacturing Setting"))
					frappe.throw(_("Select Manufacturer in session defaults or in Filed"))

				pure_item_purity = get_purity_percentage(pure_item)

			item_purity = get_purity_percentage(row.custom_alternative_item or row.item_code)

			if not item_purity:
				continue

			if pure_item_purity == item_purity:
				row.custom_pure_qty = row.qty

			else:
				row.custom_pure_qty = flt((item_purity * row.qty) / pure_item_purity, 3)

		self.custom_total_quantity += row.qty


def validate_warehouse(self):
	if self.material_request_type == "Material Transfer":
		if self.set_from_warehouse and self.set_warehouse:
			if self.set_from_warehouse == self.set_warehouse:
				frappe.throw(_("The source warehouse and the target warehouse cannot be the same."))

			for row in self.items:
				if row.from_warehouse == row.warehouse:
					frappe.throw(_("The source warehouse and the target warehouse cannot be the same."))
