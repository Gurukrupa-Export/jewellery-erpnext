import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import flt

from jewellery_erpnext.utils import get_item_from_attribute


def valid_reparing_or_next_operation(self):
	if self.docstatus != 0:
		return
	if self.type == "Issue":
		mwo_list = [row.manufacturing_work_order for row in self.employee_ir_operations]
		operation = self.operation

		EmployeeIR = DocType("Employee IR")
		EmployeeIROperation = DocType("Employee IR Operation")

		query = (
			frappe.qb.from_(EmployeeIR)
			.join(EmployeeIROperation)
			.on(EmployeeIROperation.parent == EmployeeIR.name)
			.select(EmployeeIR.name)
			.where((EmployeeIR.name != self.name) & (EmployeeIR.operation == operation))
		)

		if mwo_list:
			query = query.where(
				EmployeeIROperation.manufacturing_work_order.isin(mwo_list)
			)

		test = query.run(as_dict=True)
		if test:
			self.transfer_type = "Repairing"


def get_po_rates(supplier, operation, purchase_type, row):
	item_details = frappe.db.get_value(
		"Manufacturing Operation",
		row.manufacturing_operation,
		["metal_type", "item_code"],
		as_dict=1,
	)
	sub_category = frappe.db.get_value(
		"Item", item_details.item_code, "item_subcategory"
	)

	sup_ser_pri_item_sub = frappe.qb.DocType("Supplier Services Price Item Subcategory")
	supplier_serivice_price = frappe.qb.DocType("Supplier Services Price")
	return (
		frappe.qb.from_(sup_ser_pri_item_sub)
		.join(supplier_serivice_price)
		.on(supplier_serivice_price.name == sup_ser_pri_item_sub.parent)
		.select(sup_ser_pri_item_sub.rate_per_gm)
		.where(
			(sup_ser_pri_item_sub.supplier == supplier)
			and (sup_ser_pri_item_sub.metal_type == item_details.get("metal_type"))
			and (supplier_serivice_price.type_of_subcontracting == operation)
			and (supplier_serivice_price.purchase_type == purchase_type)
			and (sup_ser_pri_item_sub.sub_category == sub_category)
		)
	).run(as_dict=True)
