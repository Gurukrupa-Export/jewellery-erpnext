import frappe
from jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.doc_events.utils import (
	update_parent_batch_id,
)

from erpnext.stock.serial_batch_bundle import BatchNoValuation
from collections import defaultdict
from frappe.utils import add_days, cint, cstr, flt, get_link_to_form, now, nowtime, today

def after_insert(self, method):
	update_parent_batch_id(self)

class CustomBatchNoValuation(BatchNoValuation):
	def __init__(self, **kwargs):
		for key, value in kwargs.items():
			setattr(self, key, value)

		self.batch_nos = self.get_batch_nos()
		self.prepare_batches()
		self.calculate_avg_rate()
		self.calculate_valuation_rate()

	def calculate_avg_rate(self):
		if flt(self.sle.actual_qty) > 0:
			self.stock_value_change = frappe.get_cached_value(
				"Serial and Batch Bundle", self.sle.serial_and_batch_bundle, "total_amount"
			)
		else:
			entries = self.get_batch_no_ledgers()
			self.stock_value_change = 0.0
			self.batch_avg_rate = defaultdict(float)
			self.available_qty = defaultdict(float)
			self.stock_value_differece = defaultdict(float)

			for ledger in entries:
				self.stock_value_differece[ledger.batch_no] += flt(ledger.incoming_rate)
				self.available_qty[ledger.batch_no] += flt(ledger.qty)

			self.calculate_avg_rate_from_deprecarated_ledgers()
			self.calculate_avg_rate_for_non_batchwise_valuation()
			self.set_stock_value_difference()

	def get_batch_no_ledgers(self) -> list[dict]:
		"""Use ledger_map if available, else fall back to core query."""
		if not self.batchwise_valuation_batches:
			return []

		if self.ledger_map:
			key = (self.item_code, self.warehouse, self.sle.batch_no)
			entry = self.ledger_map.get(key, {})
			if entry:
				return [frappe._dict(entry)]

		# Core query logic (unchanged)
		parent = frappe.qb.DocType("Serial and Batch Bundle")
		child = frappe.qb.DocType("Serial and Batch Entry")

		timestamp_condition = ""
		if self.sle.posting_date:
			if self.sle.posting_time is None:
				self.sle.posting_time = frappe.utils.nowtime()
			timestamp_condition = frappe.query_builder.functions.CombineDatetime(
				parent.posting_date, parent.posting_time
			) < frappe.query_builder.functions.CombineDatetime(
				self.sle.posting_date, self.sle.posting_time
			)
			if self.sle.creation:
				timestamp_condition |= (
					frappe.query_builder.functions.CombineDatetime(parent.posting_date, parent.posting_time)
					== frappe.query_builder.functions.CombineDatetime(self.sle.posting_date, self.sle.posting_time)
				) & (parent.creation < self.sle.creation)

		query = (
			frappe.qb.from_(parent)
			.inner_join(child)
			.on(parent.name == child.parent)
			.select(
				child.batch_no,
				frappe.query_builder.functions.Sum(child.stock_value_difference).as_("incoming_rate"),
				frappe.query_builder.functions.Sum(child.qty).as_("qty"),
			)
			.where(
				(child.batch_no.isin(self.batchwise_valuation_batches))
				& (parent.warehouse == self.sle.warehouse)
				& (parent.item_code == self.sle.item_code)
				& (parent.docstatus == 1)
				& (parent.is_cancelled == 0)
				& (parent.type_of_transaction.isin(["Inward", "Outward"]))
			)
			.groupby(child.batch_no)
		)

		if self.sle.voucher_detail_no:
			query = query.where(parent.voucher_detail_no != self.sle.voucher_detail_no)
		elif self.sle.voucher_no:
			query = query.where(parent.voucher_no != self.sle.voucher_no)

		query = query.where(parent.voucher_type != "Pick List")
		if timestamp_condition:
			query = query.where(timestamp_condition)

		return query.run(as_dict=True)
