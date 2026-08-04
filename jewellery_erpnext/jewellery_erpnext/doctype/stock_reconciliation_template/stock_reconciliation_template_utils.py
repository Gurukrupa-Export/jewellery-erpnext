from datetime import datetime, timedelta

import frappe

# NOTE: `CustomStockReconciliation` used to live here and was registered as the FIRST
# element of a two-element `override_doctype_class["Stock Reconciliation"]` list.
# Frappe resolves `class_overrides[doctype][-1]`, so it was never loaded — dead code
# that shadowed nothing. Its one method was a stale fork of upstream's
# `remove_items_with_no_change`, strictly behind the ERPNext v16 original. The live
# controller is customization/stock_reconciliation/stock_reonciliation.py, which now
# delegates to `super()`. Removed rather than revived.


def stock_reconciliation():
	stock_template = frappe.db.get_all(
		"Stock Reconciliation template",
		{
			"docstatus": 0,
			"template_status": "Active",
			"automation_type": "Auto Generate",
		},
		["name", "day", "time", "date"],
	)
	current_time = datetime.now().time()
	current_time_timedelta = timedelta(
		hours=current_time.hour,
		minutes=current_time.minute,
		seconds=current_time.second,
	)
	current_date = datetime.now().date()
	if stock_template:
		for stock in stock_template:
			if (
				stock.day == "Every Day : Working"
				and current_time_timedelta == stock.time
			):
				create_stock_reconciliation(stock)
			elif (
				stock.day == "End of Month : Working"
				and current_date == stock.date
				and current_time_timedelta == stock.time
			):
				create_stock_reconciliation(stock)
				set_next_execution_date(
					stock, timedelta(days=30)
				)  # Set the next execution date to next month
			elif (
				stock.day == "End of the Year : Working"
				and current_date.month == stock.date
				and current_date.day == 1
				and current_time_timedelta == stock.time
			):
				create_stock_reconciliation(stock)
				set_next_execution_date(
					stock, timedelta(days=365)
				)  # Set the next execution date to next year


def create_stock_reconciliation(stock):
	items = frappe.get_doc("Stock Reconciliation template Item", {"parent": stock.name})
	stock_reconciliation_doc = frappe.get_doc(
		{
			"doctype": "Stock Reconciliation",
			"set_warehouse": items.warehouse,
			"purpose": items.purpose,
			"custom_auto_creation": 1,
		},
		ignore_mandatory=True,
	)
	stock_reconciliation_doc.insert(ignore_mandatory=True)
	stock_reconciliation_doc.db_set("custom_auto_creation", 0)


def set_next_execution_date(stock, interval):
	next_execution_date = stock.date + interval
	stock_reconciliation_doc = frappe.get_doc(
		"Stock Reconciliation template", stock.name
	)
	stock_reconciliation_doc.db_set("date", next_execution_date)
