import frappe
from frappe import _
from frappe.query_builder import DocType


def get_warehouses(doc):
	department_wh = frappe.get_value(
		"Warehouse", {"disabled": 0, "department": doc.department, "warehouse_type": "Manufacturing"}
	)
	if not department_wh:
		frappe.throw(_("Please set warhouse for department {0}").format(doc.department))

	if doc.subcontracting == "Yes":
		employee_wh = frappe.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": doc.company,
				"subcontractor": doc.subcontractor,
				"warehouse_type": "Manufacturing",
			},
		)
	else:
		employee_wh = frappe.get_value(
			"Warehouse", {"disabled": 0, "employee": doc.employee, "warehouse_type": "Manufacturing"}
		)
	if not employee_wh:
		frappe.throw(
			_("Please set warhouse for {0} {1}").format(
				"subcontractor" if doc.subcontracting == "Yes" else "employee",
				doc.subcontractor if doc.subcontracting == "Yes" else doc.employee,
			)
		)

	return department_wh, employee_wh


def get_stock_data(manufacturing_operation, employee_wh, department):
	StockEntry = DocType("Stock Entry").as_("se")
	StockEntryDetail = DocType("Stock Entry Detail").as_("sed")
	query = (
		frappe.qb.from_(StockEntry)
		.inner_join(StockEntryDetail)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(StockEntry.name)
		.distinct()
		.where(
			(StockEntry.docstatus == 1)
			& (StockEntryDetail.manufacturing_operation == manufacturing_operation)
			& (StockEntryDetail.t_warehouse == employee_wh)
			& (StockEntryDetail.to_department == department)
		)
		.orderby(StockEntry.creation)
	)
	return query.run(as_dict=True, pluck=True)
