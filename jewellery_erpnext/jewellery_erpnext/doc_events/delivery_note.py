import frappe
from frappe import _


def validate(self, method):
	if self.is_return:
		self.set("custom_invoice_item", [])
		return
	self.set("custom_invoice_item", [])
	added_items = set()

	for row in self.items:
		# New records deliver against the Quotation (the Sales Order step was removed from the
		# manufacturing flow); legacy records still deliver against the Sales Order. Both carry
		# the same e-invoice child fields, so only the source doctype/parent differ.
		if row.get("custom_against_quotation"):
			e_invoice_doctype = "Quotation E Invoice Item"
			e_invoice_parent = row.custom_against_quotation
		elif row.against_sales_order:
			e_invoice_doctype = "Sales Order E Invoice Item"
			e_invoice_parent = row.against_sales_order
		else:
			e_invoice_doctype = e_invoice_parent = None

		if e_invoice_parent:
			invoice_items = frappe.get_all(
				e_invoice_doctype,
				filters={"parent": e_invoice_parent},
				fields=[
					"item_code",
					"item_name",
					"uom",
					"qty",
					"rate",
					"amount",
					"tax_amount",
					"amount_with_tax",
					"tax_rate",
				],
			)

			for invoice_item in invoice_items:
				item_key = (
					invoice_item.item_code,
					invoice_item.item_name,
					invoice_item.uom,
					invoice_item.qty,
					invoice_item.rate,
					invoice_item.amount,
				)

				if item_key not in added_items:
					added_items.add(item_key)
					self.append(
						"custom_invoice_item",
						{
							"item_code": invoice_item.item_code,
							"item_name": invoice_item.item_name,
							"uom": invoice_item.uom,
							"qty": invoice_item.qty,
							"rate": invoice_item.rate,
							"amount": invoice_item.amount,
							"tax_amount": invoice_item.tax_amount,
							"amount_with_tax": invoice_item.amount_with_tax,
							"tax_rate": invoice_item.tax_rate,
						},
					)

		if row.serial_no:
			source_warehouse = frappe.db.get_value(
				"Serial No", row.serial_no, "warehouse"
			)
			self.set_warehouse = source_warehouse
			row.warehouse = source_warehouse

	for r in self.items:
		if r.serial_no:
			source_warehouse = frappe.db.get_value(
				"Serial No", r.serial_no, "warehouse"
			)
			# frappe.throw(f"{row}")
			self.set_warehouse = source_warehouse
			r.warehouse = source_warehouse


# def validate(self, method):
#     self.set("custom_invoice_item", [])
#     added_items = set()
#     source_warehouse = None

#     for row in self.items:
#         if row.against_sales_order:
#             invoice_items = frappe.get_all(
#                 'Sales Order E Invoice Item',
#                 filters={'parent': row.against_sales_order},
#                 fields=['item_code', 'item_name', 'uom', 'qty', 'rate', 'amount',
#                         'tax_amount', 'amount_with_tax', 'tax_rate']
#             )

#             for invoice_item in invoice_items:
#                 item_key = (
#                     invoice_item.item_code,
#                     invoice_item.item_name,
#                     invoice_item.uom,
#                     invoice_item.qty,
#                     invoice_item.rate,
#                     invoice_item.amount
#                 )
#                 if item_key not in added_items:
#                     added_items.add(item_key)
#                     self.append('custom_invoice_item', invoice_item)  # ✅ Pass dict directly

#         if row.serial_no and source_warehouse is None:
#             source_warehouse = frappe.db.get_value('Serial No', row.serial_no, 'warehouse')

#         if row.serial_no:
#             row.warehouse = source_warehouse

#     if source_warehouse:
#         self.set_warehouse = source_warehouse
