import frappe


def execute():
	"""Tick Do Not Explode on existing Template BOM rows; new ones get it in before_validate.

	Other BOM types are copied from the Template and keep its flag, so only Template rows are
	touched. Phantom rows are skipped because ERPNext keeps the flag off for them.
	"""
	frappe.db.sql(
		"""
		UPDATE `tabBOM Item` bom_item
		INNER JOIN `tabBOM` bom ON bom.name = bom_item.parent
		SET bom_item.do_not_explode = 1
		WHERE bom_item.parenttype = 'BOM'
			AND bom.bom_type = 'Template'
			AND IFNULL(bom_item.do_not_explode, 0) = 0
			AND IFNULL(bom_item.is_phantom_item, 0) = 0
		"""
	)
