import frappe
from frappe import _


@frappe.whitelist()
def get_child_reconciliation(doc, method=None):
	child_stock = frappe.db.get_all(
		"Child Stock Reconcilation", {"stock_reconcillation": doc}, ["name"]
	)
	items = []
	for stock in child_stock:
		child_items = frappe.get_all(
			"Child Stock Reconcilation Item", filters={"parent": stock.name}, fields=["*"]
		)
		for item in child_items:
			if item.item_code is not None:
				items.append(
					{
						"item_code": item.item_code,
						"warehouse": item.warehouse,
						"qty": item.qty,
						"valuation_rate": item.valuation_rate,
					}
				)

	return items


def validate_department(self, method=None):
	if not self.set_warehouse:
		return

	if self.workflow_state not in ["In Progress", "Send for Approval"]:
		return

	department = frappe.db.get_value("Warehoouse", self.set_warehouse, "department")

	if not department:
		frappe.msgprint(_("Department not mentioned in warehouse"))
		return

	if frappe.db.get_all(
		"Manufacturig Operation", {"department": department, "department_ir_status": "In-Transit"}
	):
		frappe.throw(
			_(
				"Some Manufacturing Operations are in Transit mode, Complete Transit First then perform the action"
			)
		)



 
import frappe
from frappe import _
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import StockReconciliation


class CustomStockReconciliation(StockReconciliation):
    def has_custom_mwo(self):
        """Return True if any row has custom_manufacturing_work_order checked"""
        for row in self.items:
            if row.custom_manufacturing_work_order:
                return True
        return False

    def validate(self):

        # If condition is not met, run standard ERPNext validate
        if not self.has_custom_mwo():
            return super().validate()

        # If condition is met, allow saving draft without item_code
        if self.docstatus == 0:
            return

        # If not draft, run standard validations
        return super().validate()


    def on_submit(self):

        # If condition is not met, run standard ERPNext submit
        if not self.has_custom_mwo():
            return super().on_submit()

        # If condition is met, block submit if item_code missing
        for row in self.items:
            if not row.item_code:
                frappe.throw(_("You cannot submit Stock Reconciliation without Item Code."))

        return super().on_submit()
    
    def remove_items_with_no_change(self):
        # disable validation
        return
