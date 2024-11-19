import frappe
from frappe import _


def create_so_for_subcontracting(po_doc):
	so_doc = frappe.new_doc("Sales Order")
	company = frappe.db.get_value("Company", {"supplier_code": po_doc.supplier}, "name")
	customer = frappe.db.get_value("Company", po_doc.company, "customer_code")
	if not company:
		frappe.throw(_("Mention Supplier {0} in the company").format(po_doc.supplier))

	if not customer:
		frappe.throw(_("Mention customer in the company {0}").format(po_doc.company))

	so_doc.customer = customer
	so_doc.company = company
	so_doc.delivery_date = frappe.utils.today()
	for row in po_doc.items:
		so_doc.append(
			"items", {"item_code": row.item_code, "qty": row.qty, "uom": row.uom, "rate": row.rate}
		)
	so_doc.save()
	# so_doc.submit()
