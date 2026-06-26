# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.tree_utils import (
	get_computed_gold_wt,
	get_flask_weights,
)


class TreeNumber(Document):
	def validate(self):
		self.calculate_tree_details()
		self.calculate_flask_details()
		self.calculate_material_pending()

	def calculate_tree_details(self):
		"""Wax tree weight -> computed gold weight using the KT conversion factor."""
		self.computed_gold_wt = get_computed_gold_wt(
			self.manufacturer, self.metal_touch, self.tree_wax_wt
		)

	def calculate_flask_details(self):
		"""Powder weight -> water / boric / special powder weights."""
		weights = get_flask_weights(
			self.manufacturer, self.powder_wt, self.is_wax_setting
		)
		self.water_weight = weights["water_weight"]
		if self.is_wax_setting:
			self.boric_powder_weight = weights["boric_powder_weight"]
			self.special_powder_weight = weights["special_powder_weight"]
		else:
			self.boric_powder_weight = 0
			self.special_powder_weight = 0

	def calculate_material_pending(self):
		"""Pending Qty = Issue Qty - Receive Qty - Loss Qty per material row."""
		for row in self.material_details:
			row.pending_qty = (
				flt(row.issue_qty) - flt(row.receive_qty) - flt(row.loss_qty)
			)

	def after_insert(self):
		counter = cint(frappe.db.sql("select max(counter) from `tabTree Number`")[0][0])
		self.db_set("counter", counter + 1)
