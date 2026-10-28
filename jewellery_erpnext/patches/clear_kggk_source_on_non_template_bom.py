import frappe


def execute():
	"""Blank the inherited Source DocType/Site/Name on non-Template BOMs.

	A Source Site on a non-Template BOM was copied from its Template (see
	``doc_events.bom.clear_inherited_kggk_source``). Product Return Order BOMs have no site
	and are untouched.
	"""
	if not frappe.db.has_column("BOM", "custom_kggk_source_site"):
		return

	frappe.db.sql(
		"""
		UPDATE `tabBOM`
		SET custom_kggk_source_doctype = NULL,
			custom_kggk_source_site = NULL,
			custom_kggk_source_name = NULL
		WHERE bom_type != 'Template'
			AND IFNULL(custom_kggk_source_site, '') != ''
		"""
	)
