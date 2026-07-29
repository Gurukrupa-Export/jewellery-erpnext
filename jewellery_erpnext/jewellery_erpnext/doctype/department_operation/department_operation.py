# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# The Item Attribute whose values are stored on finding items as Item Variant
# Attribute rows. This is the identifier space the loss gate matches against, so
# a Finding Category Loss Booking row that names anything else can never fire.
FINDING_CATEGORY_ATTRIBUTE = "Finding Category"


class DepartmentOperation(Document):
	def validate(self):
		self.validate_finding_loss_booking()

	def validate_finding_loss_booking(self):
		"""Keep the Finding Category Loss Booking table resolvable.

		Two failure modes this catches, both of which would otherwise turn the
		gate into a silent no-op rather than an error:

		  1. Two rows for the same category — the gate reads the table into a
		     ``{category: books_loss}`` map, so a duplicate would be resolved by
		     row order rather than by intent.
		  2. A category that is not a value of the ``Finding Category`` Item
		     Attribute. Finding items store their category as an Item Variant
		     Attribute value, so a row naming anything outside that set never
		     matches an item and the operator would see no effect at all.
		"""
		rows = self.get("finding_loss_booking") or []
		if not rows:
			return

		seen = {}
		for row in rows:
			if not row.finding_category:
				continue
			if row.finding_category in seen:
				frappe.throw(
					_(
						"Row #{0}: Finding Category <b>{1}</b> is already listed in row #{2}. "
						"Each finding category may appear only once."
					).format(row.idx, row.finding_category, seen[row.finding_category])
				)
			seen[row.finding_category] = row.idx

		if not seen:
			return

		valid = set(
			frappe.get_all(
				"Item Attribute Value",
				filters={
					"parent": FINDING_CATEGORY_ATTRIBUTE,
					"attribute_value": ["in", list(seen)],
				},
				pluck="attribute_value",
			)
		)
		unknown = [(cat, idx) for cat, idx in seen.items() if cat not in valid]
		if unknown:
			frappe.throw(
				_(
					"Row #{0}: <b>{1}</b> is not a value of the <b>{2}</b> Item Attribute, so no "
					"finding item can ever match it. Pick a finding category that findings are "
					"actually built from."
				).format(unknown[0][1], unknown[0][0], FINDING_CATEGORY_ATTRIBUTE)
			)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_finding_categories(doctype, txt, searchfield, start, page_len, filters):
	# Link-field query for Finding Category Loss Booking.finding_category.
	#
	# Sourced from the `Finding Category` Item Attribute rather than filtering
	# `Attribute Value` by is_finding_category: that check field exists but is
	# never written by any code path, so filtering on it returns an empty list.
	# The Item Attribute values are the same strings finding items carry on their
	# Item Variant Attribute rows, which is exactly what the gate compares.
	IAV = frappe.qb.DocType("Item Attribute Value")

	return (
		frappe.qb.from_(IAV)
		.select(IAV.attribute_value)
		.distinct()
		.where(
			(IAV.parent == FINDING_CATEGORY_ATTRIBUTE)
			& (IAV.attribute_value.like(f"%{txt}%"))
		)
		.orderby(IAV.attribute_value)
		.limit(page_len)
		.offset(start)
	).run()
