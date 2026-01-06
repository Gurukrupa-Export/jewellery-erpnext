from datetime import datetime, timedelta

import frappe
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import (
	EmptyStockReconciliationItemsError,
	StockReconciliation,
	get_inventory_dimensions,
	get_stock_balance_for,
)
from frappe import _


class CustomStockReconciliation(StockReconciliation):
	def remove_items_with_no_change(self):

		"""Remove items if qty or rate is not changed"""
		self.difference_amount = 0.0

		def _changed(item):
			if item.current_serial_and_batch_bundle:
				bundle_data = frappe.get_all(
					"Serial and Batch Bundle",
					filters={"name": item.current_serial_and_batch_bundle},
					fields=["total_qty as qty", "avg_rate as rate"],
				)[0]

				self.calculate_difference_amount(item, bundle_data)
				return True

			inventory_dimensions_dict = {}
			if not item.batch_no and not item.serial_no:
				for dimension in get_inventory_dimensions():
					if item.get(dimension.get("fieldname")):
						inventory_dimensions_dict[dimension.get("fieldname")] = item.get(dimension.get("fieldname"))

			item_dict = get_stock_balance_for(
				item.item_code,
				item.warehouse,
				self.posting_date,
				self.posting_time,
				batch_no=item.batch_no,
				row = item,
				inventory_dimensions_dict=inventory_dimensions_dict,
			)

			if (
				(item.qty is None or item.qty == item_dict.get("qty"))
				and (item.valuation_rate is None or item.valuation_rate == item_dict.get("rate"))
				and (not item.serial_no or (item.serial_no == item_dict.get("serial_nos")))
			):
				return False
			else:
				# set default as current rates
				if item.qty is None:
					item.qty = item_dict.get("qty")

				if item.valuation_rate is None:
					item.valuation_rate = item_dict.get("rate")

				if item_dict.get("serial_nos"):
					item.current_serial_no = item_dict.get("serial_nos")
					if self.purpose == "Stock Reconciliation" and not item.serial_no and item.qty:
						item.serial_no = item.current_serial_no

				item.current_qty = item_dict.get("qty")
				item.current_valuation_rate = item_dict.get("rate")
				self.calculate_difference_amount(item, item_dict)
				return True

		items = [item for item in self.items if _changed(item)]

		if not items and not self.custom_auto_creation:
			frappe.throw(
				_("None of the items have any change in quantity or value."),
				EmptyStockReconciliationItemsError,
			)

		elif len(items) != len(self.items):
			self.items = items
			for i, item in enumerate(self.items):
				item.idx = i + 1
			frappe.msgprint(_("Removed items with no change in quantity or value."))


def stock_reconciliation():
	stock_template = frappe.db.get_all(
		"Stock Reconciliation template",
		{"docstatus": 0, "template_status": "Active", "automation_type": "Auto Generate"},
		["name", "day", "time", "date"],
	)
	current_time = datetime.now().time()
	current_time_timedelta = timedelta(
		hours=current_time.hour, minutes=current_time.minute, seconds=current_time.second
	)
	current_date = datetime.now().date()
	if stock_template:
		for stock in stock_template:
			if stock.day == "Every Day : Working" and current_time_timedelta == stock.time:
				create_stock_reconciliation(stock)
			elif (
				stock.day == "End of Month : Working"
				and current_date == stock.date
				and current_time_timedelta == stock.time
			):
				create_stock_reconciliation(stock)
				set_next_execution_date(stock, timedelta(days=30))  # Set the next execution date to next month
			elif (
				stock.day == "End of the Year : Working"
				and current_date.month == stock.date
				and current_date.day == 1
				and current_time_timedelta == stock.time
			):
				create_stock_reconciliation(stock)
				set_next_execution_date(stock, timedelta(days=365))  # Set the next execution date to next year


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
	stock_reconciliation_doc = frappe.get_doc("Stock Reconciliation template", stock.name)
	stock_reconciliation_doc.db_set("date", next_execution_date)
