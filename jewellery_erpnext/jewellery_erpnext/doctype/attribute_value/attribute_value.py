# Copyright (c) 2022, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class AttributeValue(Document):
	def validate(self):
		self.validate_not_allowed_attribute_values()

	def validate_not_allowed_attribute_values(self):
		"""Each Not Allowed row must be unique and belong to its Item Attribute."""
		seen = set()
		for row in self.get("not_allowed_attribute_values") or []:
			key = (row.item_attribute, row.attribute_value)
			if key in seen:
				frappe.throw(
					_("Row #{0}: {1} {2} is already listed as not allowed").format(
						row.idx, row.item_attribute, frappe.bold(row.attribute_value)
					)
				)
			seen.add(key)

			if not frappe.db.exists(
				"Item Attribute Value",
				{"parent": row.item_attribute, "attribute_value": row.attribute_value},
			):
				frappe.throw(
					_("Row #{0}: {1} is not a value of Item Attribute {2}").format(
						row.idx,
						frappe.bold(row.attribute_value),
						frappe.bold(row.item_attribute),
					)
				)
