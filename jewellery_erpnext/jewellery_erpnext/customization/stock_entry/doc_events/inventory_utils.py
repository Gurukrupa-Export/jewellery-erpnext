import frappe
from frappe import _
from frappe.utils import (
	add_days,
	get_first_day,
	get_weekday,
	is_last_day_of_the_month,
	nowtime,
	today,
)

from jewellery_erpnext.jewellery_erpnext.customization.utils.sample_goods import (
	SAMPLE_ALLOWED_SE_TYPES,
	get_sample_batches,
)


def validate_customer_voucher(self):
	if not self.customer_voucher_type:
		return

	item_data = frappe._dict()

	if self.customer_voucher_type == "Customer Subcontracting":
		for row in self.items:
			if not item_data.get(row.item_code):
				item_data[row.item_code] = frappe.db.get_value(
					"Item", row.item_code, "has_serial_no"
				)
			if item_data.get(row.item_code):
				frappe.throw(
					_("Serialized items not allowd in this Customer Voucher Type")
				)
	elif self.customer_voucher_type == "Customer Repair":
		for row in self.items:
			if not item_data.get(row.item_code):
				item_data[row.item_code] = frappe.db.get_value(
					"Item", row.item_code, "has_batch_no"
				)
			if item_data.get(row.item_code):
				frappe.throw(_("Batch items not allowd in this Customer Voucher Type"))

	elif self.customer_voucher_type == "Customer Sample Goods":
		for row in self.items:
			if not item_data.get(row.t_warehouse):
				item_data[row.t_warehouse] = frappe.db.get_value(
					"Warehouse", row.t_warehouse, "warehouse_type"
				)
			if (
				row.manufacturing_operation
				and item_data.get(row.t_warehouse) == "Manufacturing"
			):
				frappe.throw(
					_(
						"Manufacturing Type warehouse not allowed in this Customer Voucher Type"
					)
				)


def validate_sample_goods_not_consumed(self):
	"""Block Customer Sample Goods stock from Work-Order use / issue-to-floor / manufacturing.

	The sample marker lives on the resolved Batch (``custom_customer_voucher_type ==
	"Customer Sample Goods"``), not on this Stock Entry, so this guard is independent of
	``self.customer_voucher_type`` (which is only set on the receive SE). By the time this
	runs in ``before_validate`` the first hook (``doc_events.stock_entry.before_validate`` ->
	``update_batches``) has already resolved ``row.batch_no`` on every source row, and
	``before_validate`` fires for auto-created EOD Stock Entries too -- so this is the
	authoritative backstop for every consumption path.

	A sample batch may only ever appear as a source row on the three Customer Goods
	movements (Received / Issue / Transfer); every other stock_entry_type that draws it is
	blocked.
	"""
	if self.stock_entry_type in SAMPLE_ALLOWED_SE_TYPES:
		return

	sample_batches = get_sample_batches(
		{row.batch_no for row in self.items if row.s_warehouse and row.batch_no}
	)
	if not sample_batches:
		return

	for row in self.items:
		if row.s_warehouse and row.batch_no in sample_batches:
			customer = frappe.get_cached_value("Batch", row.batch_no, "custom_customer")
			frappe.throw(
				_(
					"Batch {0} (item {1}) is Customer Sample Goods received for customer {2}. "
					"Sample stock cannot be used in a Work Order, issued to Production, or "
					"consumed in manufacturing. Use a Customer Goods Issue/Transfer instead."
				).format(
					frappe.bold(row.batch_no), row.item_code, frappe.bold(customer)
				)
			)


def in_configured_timeslot(self):
	"""Check if current time is in configured timeslot for reposting."""

	company = frappe.get_cached_doc("Company", self.company)

	if not company.custom_freeze_entries:
		return True

	if (
		company.custom_ignore_freeze_for_role
		and company.custom_ignore_freeze_for_role
		in frappe.get_roles(frappe.session.user)
	):
		return True

	# if get_weekday() == company.limits_dont_apply_on:
	# 	return True

	if company.custom_freeze_type == "Monthly":
		if company.custom_end_of_month and not is_last_day_of_the_month(today()):
			return True

		elif not company.custom_end_of_month and company.custom_date_of_month:
			if today() != add_days(
				get_first_day(today()), days=company.custom_date_of_month
			):
				return True

	start_time = company.custom_start_time
	end_time = company.custom_end_time

	from datetime import datetime, timedelta

	t = datetime.strptime(nowtime(), "%H:%M:%S.%f").time()

	now_time = timedelta(hours=t.hour, minutes=t.minute, seconds=t.second)

	if start_time < end_time:
		return end_time < now_time or now_time < start_time

	return now_time < start_time and now_time > end_time
