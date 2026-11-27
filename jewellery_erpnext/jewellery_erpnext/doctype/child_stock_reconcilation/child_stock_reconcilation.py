# Copyright (c) 2024, 8848 Digital LLP and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from jewellery_erpnext.jewellery_erpnext.doc_events.supplier_metal_limit import (
	_metal_items,
	pure_weight,
)


class ChildStockReconcilation(Document):
	def validate(self):
		self.set_pure_weight()

	def set_pure_weight(self):
		"""Pure weight of each row: qty x purity % / 100 (metal and finding items, else 0)."""
		purities = _metal_items([row.item_code for row in self.items])
		for row in self.items:
			row.pure_weight = (
				pure_weight(row.qty, purities[row.item_code])
				if row.item_code in purities
				else 0.0
			)

	@frappe.whitelist()
	def fetch_previous_child_stock_reconcilation(self):
		if self.previous_child_stock_reconciliation == 1:
			for item in self.previous_child_stock:
				stock = frappe.get_doc(
					"Child Stock Reconcilation",
					{
						"box_number": item.box_number,
						"set_warehouse": self.set_warehouse,
						"docstatus": 1,
					},
				)
				if stock:
					return stock.name

	@frappe.whitelist()
	def fetch_stock_reconciliation_item(self):
		if self.previous_child_stock_reconciliation == 1:
			stock_reconciliation = frappe.get_doc(
				"Stock Reconciliation", self.stock_reconcillation
			)
			self.items = []
			for item in stock_reconciliation.items:
				self.append(
					"items",
					{
						"item_code": item.item_code,
						"warehouse": item.warehouse,
						"qty": item.qty,
						"valuation_rate": item.valuation_rate,
					},
				)

	# pass
