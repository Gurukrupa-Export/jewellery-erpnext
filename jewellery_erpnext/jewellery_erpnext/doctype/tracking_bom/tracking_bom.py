# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import (
	get_diamond_rate,
	get_gemstone_rate,
	get_gold_rate,
	get_making_charges,
	get_other_rate,
)


class TrackingBom(Document):
	def before_validate(self):
		self._set_precision()
		self._set_gemstone_price_list_type()

	def validate(self):
		self.calculate_weights()
		self.calculate_rates()

	def _set_precision(self):
		"""Set precision values based on customer settings."""
		if not self.customer:
			self.doc_pricision = 3
			self.diamond_pricision = 3
			self.gemstone_pricision = 3
			return

		precision_data = frappe.db.get_value(
			"Customer",
			self.customer,
			[
				"custom_consider_2_digit_for_bom",
				"custom_consider_2_digit_for_diamond",
				"custom_consider_2_digit_for_gemstone",
			],
			as_dict=1,
		)
		if precision_data:
			self.doc_pricision = (
				2 if precision_data.get("custom_consider_2_digit_for_bom") else 3
			)
			self.diamond_pricision = (
				2 if precision_data.get("custom_consider_2_digit_for_diamond") else 3
			)
			self.gemstone_pricision = (
				2 if precision_data.get("custom_consider_2_digit_for_gemstone") else 3
			)
		else:
			self.doc_pricision = 3
			self.diamond_pricision = 3
			self.gemstone_pricision = 3

	def _set_gemstone_price_list_type(self):
		"""Set gemstone price list type from customer on all gemstone detail rows."""
		if not self.customer:
			return

		gemstone_price_list_type = frappe.db.get_value(
			"Customer", self.customer, "custom_gemstone_price_list_type"
		)

		if gemstone_price_list_type:
			for row in self.gemstone_detail:
				row.price_list_type = gemstone_price_list_type

	def calculate_weights(self):
		"""Calculate total weights for metal, diamond, gemstone, finding."""
		self.total_metal_weight = sum(row.quantity for row in self.metal_detail)
		self.metal_weight = self.total_metal_weight

		self.diamond_weight = sum(row.quantity for row in self.diamond_detail)
		self.total_diamond_weight = self.diamond_weight
		total_diamond_weight_in_gms = flt(self.diamond_weight / 5, 3)
		self.total_diamond_weight_per_gram = total_diamond_weight_in_gms

		self.gemstone_weight = sum(row.quantity for row in self.gemstone_detail)
		self.finding_weight = sum(row.quantity for row in self.finding_detail)

		self.metal_and_finding_weight = flt(self.metal_weight) + flt(
			self.finding_weight
		)

		other_weight = sum(row.quantity for row in self.other_detail)

		self.gross_weight = (
			flt(self.metal_and_finding_weight)
			+ flt(total_diamond_weight_in_gms)
			+ flt(self.gemstone_weight / 5)  # gemstone weight in gms
			+ flt(other_weight)
		)

	def calculate_rates(self):
		"""Calculate all pricing: gold, diamond, gemstone, making charges, totals."""
		# Calculate gold/metal rate
		gold_amount = get_gold_rate(self)
		self.gold_bom_amount = gold_amount

		# Calculate diamond rate
		diamond_amount = get_diamond_rate(self)
		self.diamond_bom_amount = diamond_amount

		# Calculate gemstone rate
		gemstone_amount = get_gemstone_rate(self)
		self.gemstone_bom_amount = gemstone_amount

		# Calculate other rate
		other_amount = get_other_rate(self)
		self.other_bom_amount = other_amount

		# Calculate making charges
		making_charge = get_making_charges(self)
		self.making_charge = making_charge or 0

		# Set finding_bom_amount
		self.finding_bom_amount = sum(
			row.amount for row in self.finding_detail if row.amount
		)

		# Calculate total BOM amount
		self.total_bom_amount = (
			flt(self.gold_bom_amount)
			+ flt(self.making_charge)
			+ flt(self.diamond_bom_amount)
			+ flt(self.gemstone_bom_amount)
			+ flt(self.other_bom_amount)
		)

		# Calculate FG Purchase amounts
		self.making_fg_purchase = 0
		for row in self.metal_detail + self.finding_detail:
			self.making_fg_purchase += (
				row.fg_purchase_amount if row.fg_purchase_amount else 0
			)

		self.diamond_fg_purchase = 0
		for row in self.diamond_detail:
			self.diamond_fg_purchase += (
				row.fg_purchase_amount if row.fg_purchase_amount else 0
			)

		self.gemstone_fg_purchase = 0
		for row in self.gemstone_detail:
			self.gemstone_fg_purchase += (
				row.fg_purchase_amount if row.fg_purchase_amount else 0
			)


@frappe.whitelist()
def update_tracking_bom_detail(
	tracking_bom_name,
	metal_detail=None,
	diamond_detail=None,
	gemstone_detail=None,
	finding_detail=None,
	other_detail=None,
):
	"""Whitelisted method to update Tracking BOM detail tables."""
	import json

	doc = frappe.get_doc("Tracking Bom", tracking_bom_name)

	if metal_detail:
		_update_child_table(
			doc, "BOM Metal Detail", "metal_detail", json.loads(metal_detail)
		)
	if diamond_detail:
		_update_child_table(
			doc, "BOM Diamond Detail", "diamond_detail", json.loads(diamond_detail)
		)
	if gemstone_detail:
		_update_child_table(
			doc, "BOM Gemstone Detail", "gemstone_detail", json.loads(gemstone_detail)
		)
	if finding_detail:
		_update_child_table(
			doc, "BOM Finding Detail", "finding_detail", json.loads(finding_detail)
		)
	if other_detail:
		_update_child_table(
			doc, "BOM Other Detail", "other_detail", json.loads(other_detail)
		)

	doc.ignore_validate_update_after_submit = True
	doc.save()

	return "Tracking BOM Updated"


def _update_child_table(parent, child_doctype, table_field, data):
	"""Helper to update a child table on Tracking BOM."""
	for d in data:
		if not d.get("docname"):
			child_doc = parent.append(table_field, {})
		else:
			child_doc = frappe.get_doc(child_doctype, d.get("docname"))
		d.pop("docname", "")
		d.pop("name", "")
		child_doc.update(d)
		child_doc.flags.ignore_validate_update_after_submit = True
		child_doc.save()
