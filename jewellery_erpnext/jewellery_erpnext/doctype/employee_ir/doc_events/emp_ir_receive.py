import frappe
from frappe import _
from frappe.query_builder import DocType


def get_warehouses(doc, warehouse_data):
	if not warehouse_data.get(doc.department):
		warehouse_data[doc.department] = frappe.get_value(
			"Warehouse", {"disabled": 0, "department": doc.department, "warehouse_type": "Manufacturing"}
		)
	department_wh = warehouse_data.get(doc.department)
	if not department_wh:
		frappe.throw(_("Please set warhouse for department {0}").format(doc.department))

	if doc.subcontracting == "Yes":
		if not warehouse_data.get(doc.subcontractor):
			warehouse_data[doc.subcontractor] = frappe.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": doc.company,
					"subcontractor": doc.subcontractor,
					"warehouse_type": "Manufacturing",
				},
			)
		employee_wh = warehouse_data.get(doc.subcontractor)
	else:
		if not warehouse_data.get(doc.employee):
			warehouse_data[doc.employee] = frappe.get_value(
				"Warehouse", {"disabled": 0, "employee": doc.employee, "warehouse_type": "Manufacturing"}
			)
		employee_wh = warehouse_data.get(doc.employee)

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
			& (StockEntryDetail.manufacturing_operation.like(f"%{manufacturing_operation}%"))
			& (StockEntryDetail.t_warehouse == employee_wh)
			& (StockEntryDetail.to_department == department)
		)
		.orderby(StockEntry.creation)
	)
	return query.run(as_dict=True, pluck=True)


def get_stock_data_new(manufacturing_operation, employee_wh, department):
	StockEntry = DocType("Stock Entry").as_("se")
	StockEntryDetail = DocType("Stock Entry Detail").as_("sed")
	query = (
		frappe.qb.from_(StockEntry)
		.inner_join(StockEntryDetail)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select((StockEntry.name).as_("se_name"), (StockEntryDetail.item_code).as_("item_code"),(StockEntryDetail.manufacturing_operation).as_("manufacturing_operation"))
		.where(
			(StockEntry.docstatus == 1)
			& (StockEntryDetail.manufacturing_operation == manufacturing_operation)
			& (StockEntryDetail.t_warehouse == employee_wh)
			& (StockEntryDetail.to_department == department)
		)
		.orderby(StockEntry.creation)
	)
	return query.run(as_dict=True)