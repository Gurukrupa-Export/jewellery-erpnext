# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from jewellery_erpnext.refining.constants import REFINING_TYPES


class ManufacturingSetting(Document):
	def validate(self):
		self.validate_refining_variant_restrictions()

	def validate_refining_variant_restrictions(self):
		"""Reject a restriction row naming a refining type that no longer exists.

		The child table's Select options are a copy of Refining Entry's, so a rename there
		would leave stored rows pointing at a dead type — silently un-enforced. Checked
		against the live tuple so bulk edit and the API cannot store a stale value either.
		A BLANK type is valid and means "every refining type".
		"""
		for row in self.refining_variant_restrictions:
			if row.refining_type and row.refining_type not in REFINING_TYPES:
				frappe.throw(
					_(
						"Row #{0}: {1} is not a valid Refining Type. Valid types are: {2}."
					).format(
						row.idx,
						frappe.bold(row.refining_type),
						", ".join(REFINING_TYPES),
					)
				)
