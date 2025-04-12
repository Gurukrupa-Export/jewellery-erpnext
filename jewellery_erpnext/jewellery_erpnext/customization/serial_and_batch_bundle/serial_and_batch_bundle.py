import frappe
from jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.doc_events.utils import (
	update_parent_batch_id,
)

from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import SerialandBatchBundle
from erpnext.stock.serial_batch_bundle import SerialNoValuation, BatchNoValuation
from collections import defaultdict
from frappe.utils import add_days, cint, cstr, flt, get_link_to_form, now, nowtime, today
from frappe.query_builder.functions import CombineDatetime, Sum

def after_insert(self, method):
	update_parent_batch_id(self)


class CustomSerialandBatchBundle(SerialandBatchBundle):
	def set_incoming_rate_for_outward_transaction(self, row=None, save=False, allow_negative_stock=False):
		sle = self.get_sle_for_outward_transaction()

		if self.has_serial_no:
			sn_obj = SerialNoValuation(
				sle=sle,
				item_code=self.item_code,
				warehouse=self.warehouse,
			)

		else:
			sn_obj = CustomBatchNoValuation(
				sle=sle,
				item_code=self.item_code,
				warehouse=self.warehouse,
			)

		for d in self.entries:
			available_qty = 0

			if self.has_serial_no:
				d.incoming_rate = abs(sn_obj.serial_no_incoming_rate.get(d.serial_no, 0.0))
			else:
				d.incoming_rate = abs(flt(sn_obj.batch_avg_rate.get(d.batch_no)))

				available_qty = flt(sn_obj.available_qty.get(d.batch_no), d.precision("qty"))
				if self.docstatus == 1:
					available_qty += flt(d.qty, d.precision("qty"))

				if not allow_negative_stock:
					self.validate_negative_batch(d.batch_no, available_qty)

			d.stock_value_difference = flt(d.qty) * flt(d.incoming_rate)

			if save:
				d.db_set(
					{"incoming_rate": d.incoming_rate, "stock_value_difference": d.stock_value_difference}
				)


class CustomBatchNoValuation(BatchNoValuation):
	def __init__(self, **kwargs):
		for key, value in kwargs.items():
			setattr(self, key, value)

		self.batch_nos = self.get_batch_nos()
		self.batch_valuation_ledger = getattr(frappe.local, "batch_valuation_ledger", None)
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

	def get_batch_no_ledgers(self):
		if not self.batchwise_valuation_batches:
			return []

		# Use prefetched Batch Valuation Ledger if available
		if self.batch_valuation_ledger:
			result = []
			for batch_no in self.batchwise_valuation_batches:
				ledger_data = self.batch_valuation_ledger.get_batch_data(self.warehouse, self.item_code, batch_no)
				if ledger_data:
					result.append(frappe._dict({
						"batch_no": batch_no,
						"incoming_rate": ledger_data["incoming_rate"],
						"qty": ledger_data["qty"]
					}))
				else:
					return self.old_get_batch_no_ledgers()

			return result

	def old_get_batch_no_ledgers(self):
		# Fallback QB query
		parent = frappe.qb.DocType("Serial and Batch Bundle")
		child = frappe.qb.DocType("Serial and Batch Entry")

		timestamp_condition = None  # Use None for consistency
		if self.sle.posting_date:
			if self.sle.posting_time is None:
				self.sle.posting_time = nowtime()
			timestamp_condition = CombineDatetime(parent.posting_date, parent.posting_time) < CombineDatetime(
				self.sle.posting_date, self.sle.posting_time
			)
			if self.sle.creation:
				timestamp_condition |= (
					(CombineDatetime(parent.posting_date, parent.posting_time) == CombineDatetime(self.sle.posting_date, self.sle.posting_time))
					& (parent.creation < self.sle.creation)
				)

		query = (
			frappe.qb.from_(parent)
			.inner_join(child)
			.on(parent.name == child.parent)
			.select(
				child.batch_no,
				Sum(child.stock_value_difference).as_("incoming_rate"),
				Sum(child.qty).as_("qty"),
			)
			.where(
				(child.batch_no.isin(self.batchwise_valuation_batches))
				& (parent.warehouse == self.sle.warehouse)
				& (parent.item_code == self.sle.item_code)
				& (parent.docstatus == 1)
				& (parent.is_cancelled == 0)
				& (parent.type_of_transaction.isin(["Inward", "Outward"]))
				& (parent.voucher_type != "Pick List")
				& (parent.voucher_no != self.sle.voucher_no)  # Always exclude current voucher
			)
			.groupby(child.batch_no)
		)

		if self.sle.voucher_detail_no:
			query = query.where(parent.voucher_detail_no != self.sle.voucher_detail_no)
		if timestamp_condition:
			query = query.where(timestamp_condition)

		return query.run(as_dict=True)
