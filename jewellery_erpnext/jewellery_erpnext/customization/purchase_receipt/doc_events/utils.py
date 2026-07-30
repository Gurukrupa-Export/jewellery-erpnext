import frappe
from frappe import _

from jewellery_erpnext.jewellery_erpnext.customization.utils.party_link import (
	get_linked_customer,
)


def update_customer(self):
	if (
		frappe.db.get_value("Supplier", self.supplier, "custom_is_external_supplier")
		== 1
	):
		for row in self.items:
			row.inventory_type = "Customer Goods"
	else:
		for row in self.items:
			row.inventory_type = "Regular Stock"

	customer = get_linked_customer(self.supplier)

	if customer:
		for row in self.items:
			if row.inventory_type == "Customer Goods":
				row.customer = customer
	else:
		for row in self.items:
			if row.inventory_type == "Customer Goods" and not row.customer:
				frappe.throw(
					_("Customer is mandatory for Customer Goods inventory type")
				)


def update_inventory_type(self):
	primary_customer = get_linked_customer(self.supplier)
	if (
		frappe.db.get_value(
			"Supplier",
			self.supplier,
			"custom_consider_purchase_receipt_as_customergoods",
		)
		and primary_customer
	):
		for row in self.items:
			row.inventory_type = "Customer Goods"
			row.customer = primary_customer


def update_bundle_details(self):
	for row in self.items:
		bundle = frappe.db.get_value(
			"Serial and Batch Bundle",
			{
				"voucher_type": self.doctype,
				"voucher_no": self.name,
				"voucher_detail_no": row.name,
			},
		)
		if bundle:
			if frappe.db.get_value("Item", row.item_code, "has_batch_no"):
				row.db_set(
					"batch_no",
					frappe.db.get_value(
						"Serial and Batch Entry", {"parent": bundle}, "batch_no"
					),
				)
			elif frappe.db.get_value("Item", row.item_code, "has_serial_no"):
				row.db_set(
					"serial_no",
					frappe.db.get_value(
						"Serial and Batch Entry", {"parent": bundle}, "serial_no"
					),
				)
