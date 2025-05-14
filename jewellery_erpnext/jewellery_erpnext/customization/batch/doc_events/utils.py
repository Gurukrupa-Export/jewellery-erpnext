import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)


def update_inventory_dimentions(self):
	item_groups = frappe.db.get_all("Item Group", {"custom_is_alloy_group": 1}, pluck="name")
	alloy_item_list = frappe.db.get_all(
		"Item", {"item_group": ["in", item_groups], "variant_of": ["in", ["M", "F"]]}, pluck="name"
	)
	for row in frappe.db.get_all(
		"DocField", {"parent": self.reference_doctype, "fieldtype": "Table"}, ["options"]
	):
		if frappe.db.exists(row.options, self.custom_voucher_detail_no):
			self.custom_inventory_type = frappe.db.get_value(
				row.options, self.custom_voucher_detail_no, "inventory_type"
			)
			self.custom_customer = frappe.db.get_value(
				row.options, self.custom_voucher_detail_no, "customer"
			)
			attribute_value = frappe.db.get_value(
				"Item Variant Attribute", {"parent": self.item, "attribute": "Metal Type"}, "attribute_value"
			)
			if self.reference_doctype != "Stock Entry":
				if self.item in alloy_item_list:
					self.custom_alloy_rate = frappe.db.get_value(
						row.options, self.custom_voucher_detail_no, "rate"
					)
				elif self.item not in alloy_item_list and frappe.db.get_value(
					"Attribute Value", attribute_value, "is_metal_type"
				):
					self.custom_metal_rate = frappe.db.get_value(
						row.options, self.custom_voucher_detail_no, "rate"
					)
			else:
				if self.item in alloy_item_list:
					self.custom_alloy_rate = frappe.db.get_value(
						row.options, self.custom_voucher_detail_no, "custom_alloy_rate"
					)
				elif self.item not in alloy_item_list and frappe.db.get_value(
					"Attribute Value", attribute_value, "is_metal_type"
				):
					self.custom_metal_rate = frappe.db.get_value(
						row.options, self.custom_voucher_detail_no, "custom_metal_rate"
					)
			break

	if not frappe.db.get_value(
		"Item", self.item, "custom_inventory_type_can_be_customer_goods"
	) and self.custom_inventory_type in ["Customer Goods", "Customer Stock"]:
		frappe.throw(_("This item does not allowed as Customer Goods"))

	if self.reference_doctype == "Stock Entry" and self.custom_customer:
		self.custom_customer_voucher_type = frappe.db.get_value(
			"Stock Entry", self.reference_name, "customer_voucher_type"
		)


def update_pure_qty(self):
	if not self.batch_qty:
		return

	variant_of = frappe.db.get_value("Item", self.item, "variant_of")

	if variant_of not in ["M", "F"]:
		return

	if not self.reference_doctype:
		return

	# company = frappe.db.get_value(self.reference_doctype, self.reference_name, "company")

	# pure_item = frappe.db.get_value("Manufacturing Setting", company, "pure_gold_item")

	manufacturer = frappe.db.get_value(self.reference_doctype, self.reference_name, "manufacturer")

	pure_item = frappe.db.get_value("Manufacturing Setting", {"manufacturer":manufacturer}, "pure_gold_item")

	if not pure_item:
		return

	batch_item_purity = get_purity_percentage(self.item)
	pure_item_purity = get_purity_percentage(pure_item)

	if not batch_item_purity:
		return

	self.custom_pure_metal_qty = flt((batch_item_purity * self.batch_qty) / pure_item_purity, 3)
