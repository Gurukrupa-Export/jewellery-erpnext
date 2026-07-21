import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)


def _get_purity_percentage_map(items):
	"""Purity percentage for many items in one query.

	``get_purity_percentage`` builds and runs a three-table join on every call, so
	asking it once per row dominated the whole Material Request save. The join
	yields at most one row per item, so a flat dict carries the same answer.
	"""
	items = [item for item in items if item]
	if not items:
		return {}

	IVA = frappe.qb.DocType("Item Variant Attribute")
	ITEM = frappe.qb.DocType("Item")
	AV = frappe.qb.DocType("Attribute Value")

	rows = (
		frappe.qb.from_(IVA)
		.join(ITEM)
		.on(ITEM.name == IVA.parent)
		.join(AV)
		.on(IVA.attribute_value == AV.name)
		.select(ITEM.name, AV.purity_percentage)
		.where((IVA.attribute == "Metal Purity") & (ITEM.name.isin(items)))
	).run()

	purity_map = {}
	for item, purity_percentage in rows:
		# first row wins, mirroring get_purity_percentage's purity_percentage[0][0]
		purity_map.setdefault(item, purity_percentage)
	return purity_map


def update_pure_qty(self):
	self.custom_total_quantity = 0
	pure_item_purity = None
	purity_map = _get_purity_percentage_map(
		{
			row.custom_alternative_item or row.item_code
			for row in self.items
			if row.custom_variant_of in ["M", "F"]
			and self.custom_transfer_type != "Transfer To Branch"
		}
	)
	for row in self.items:
		if (
			row.custom_variant_of in ["M", "F"]
			and self.custom_transfer_type != "Transfer To Branch"
		):
			if not pure_item_purity:
				# pure_item = frappe.db.get_value("Manufacturing Setting", self.company, "pure_gold_item")

				pure_item = frappe.db.get_value(
					"Manufacturing Setting",
					{"manufacturer": self.custom_manufacturer},
					"pure_gold_item",
				)

				if not pure_item:
					# frappe.throw(_("Pure Item not mentioned in Manufacturing Setting"))
					frappe.throw(
						_("Select Manufacturer in session defaults or in Filed")
					)

				pure_item_purity = get_purity_percentage(pure_item)

			item_purity = purity_map.get(row.custom_alternative_item or row.item_code)

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
				frappe.throw(
					_(
						"The source warehouse and the target warehouse cannot be the same."
					)
				)

			for row in self.items:
				if row.from_warehouse == row.warehouse:
					frappe.throw(
						_(
							"The source warehouse and the target warehouse cannot be the same."
						)
					)
