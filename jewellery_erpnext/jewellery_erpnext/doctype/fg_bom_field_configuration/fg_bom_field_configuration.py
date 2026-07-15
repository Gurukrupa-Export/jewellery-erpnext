# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class FGBOMFieldConfiguration(Document):
	def validate(self):
		bom_meta = frappe.get_meta("BOM")
		seen = set()
		for row in self.field_config:
			# Auto-map: when an active row has no target but a BOM field shares its
			# field_name, link them (admins commonly name the config field after the
			# BOM field, e.g. width/product_size/two_in_one). Saves a manual pick and
			# closes the captured-but-unmapped gap without forcing selection.
			if (
				row.is_active
				and not row.fg_bom_field
				and row.field_name
				and bom_meta.has_field(row.field_name)
			):
				row.fg_bom_field = row.field_name

			# An active field with no mapping target captures a value that goes
			# nowhere (the copy step skips blank fg_bom_field) -- block it so admins
			# don't get a silent no-op. Inactive rows may stay unmapped.
			if row.is_active and not row.fg_bom_field:
				frappe.throw(
					_(
						"Row {0}: set an FG BOM Field for active field '{1}' "
						"(subcategory {2}), or mark it inactive."
					).format(
						row.idx, row.field_label or row.field_name, row.subcategory
					)
				)

			# The mapping target must be a real field on BOM (restrict to existing
			# fields -- we never create BOM columns from config).
			if row.fg_bom_field and not bom_meta.has_field(row.fg_bom_field):
				frappe.throw(
					_(
						"Row {0}: FG BOM Field '{1}' does not exist on the BOM doctype."
					).format(row.idx, row.fg_bom_field)
				)

			# Field Name must be unique within a subcategory.
			key = (row.subcategory, (row.field_name or "").strip())
			if not row.field_name:
				continue
			if key in seen:
				frappe.throw(
					_(
						"Row {0}: Field Name '{1}' is duplicated for subcategory {2}."
					).format(row.idx, row.field_name, row.subcategory)
				)
			seen.add(key)


@frappe.whitelist()
def get_active_fields_for_subcategory(subcategory):
	"""Active configured field rows for a subcategory (server source of truth)."""
	if not subcategory:
		return []
	config = frappe.get_single("FG BOM Field Configuration")
	return [
		{
			"subcategory": row.subcategory,
			"field_label": row.field_label,
			"field_name": row.field_name,
			"field_type": row.field_type,
			"options": row.options,
			"is_mandatory": row.is_mandatory,
			"fg_bom_field": row.fg_bom_field,
		}
		for row in config.field_config
		if row.is_active and row.subcategory == subcategory
	]
