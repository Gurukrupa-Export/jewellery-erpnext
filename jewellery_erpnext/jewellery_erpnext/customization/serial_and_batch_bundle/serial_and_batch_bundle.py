import frappe
from jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.doc_events.utils import (
	update_parent_batch_id,
)
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import get_available_serial_nos
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import SerialandBatchBundle
from erpnext.stock.serial_batch_bundle import SerialNoValuation, BatchNoValuation
from collections import defaultdict
from frappe.utils import add_days, cint, cstr, flt, get_link_to_form, now, nowtime, today
from frappe.query_builder.functions import CombineDatetime, Sum
from frappe import _, _dict, bold

def after_insert(self, method):
	update_parent_batch_id(self)

class SerialNoDuplicateError(frappe.ValidationError):
	pass

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


	def validate_serial_nos_duplicate(self):
		# Don't inward same serial number multiple times
		if self.voucher_type in ["POS Invoice", "Pick List"]:
			return

		if not self.warehouse:
			return

		if self.voucher_type in ["Stock Reconciliation", "Stock Entry"] and self.docstatus != 1:
			return

		if not (self.has_serial_no and self.type_of_transaction == "Inward"):
			return

		serial_nos = [d.serial_no for d in self.entries if d.serial_no]

		purchase_type = "Branch Purchase"
		if self.voucher_type == "Purchase Receipt" and self.voucher_no:

			pr_doc = frappe.get_doc("Purchase Receipt", self.voucher_no)

			if pr_doc.purchase_type == "FG Purchase":
				purchase_type = "FG Purchase"

		kwargs = frappe._dict(
			{
				"item_code": self.item_code,
				"posting_date": self.posting_date,
				"posting_time": self.posting_time,
				"serial_nos": serial_nos,
				"check_serial_nos": True,
				"purchase_type": purchase_type
			}
		)
		# frappe.throw(f"{kwargs}")
		if self.returned_against and self.docstatus == 1:
			kwargs["ignore_voucher_detail_no"] = self.voucher_detail_no

		if self.docstatus == 1:
			kwargs["voucher_no"] = self.voucher_no

		available_serial_nos = get_available_serial_nos(kwargs)
		# frappe.throw(f"{kwargs['purchase_type']}")
		if kwargs["purchase_type"] not in ["Branch Purchase", "FG Purchase"]:
			for data in available_serial_nos:
				if data.serial_no in serial_nos:
					self.throw_error_message(
						f"Serial No {bold(data.serial_no)} is already present in the warehouse {bold(data.warehouse)}.",
						SerialNoDuplicateError,
					)

	def throw_error_message(self, message, exception=frappe.ValidationError):
		frappe.throw(_(message), exception, title=_("Error"))

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
