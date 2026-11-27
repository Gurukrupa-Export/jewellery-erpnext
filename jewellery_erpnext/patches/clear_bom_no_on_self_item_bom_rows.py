import frappe


def execute():
	"""Clear BOM No on the finished item's own row of draft BOMs.

	BOM.check_recursion reads the saved rows, so a draft BOM that already holds a BOM No on its
	own item's row fails every re-save (the Sales Order row then keeps no weights or rate).
	New saves are handled in before_validate. Submitted BOMs are left untouched.
	"""
	frappe.db.sql(
		"""
		UPDATE `tabBOM Item` bom_item
		INNER JOIN `tabBOM` bom ON bom.name = bom_item.parent
		SET bom_item.bom_no = '', bom_item.do_not_explode = 1
		WHERE bom_item.parenttype = 'BOM'
			AND bom.docstatus = 0
			AND bom_item.item_code = bom.item
			AND IFNULL(bom_item.bom_no, '') != ''
			AND IFNULL(bom_item.is_phantom_item, 0) = 0
		"""
	)
