import frappe
from jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.doc_events.utils import (
	update_parent_batch_id,
)
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import get_available_serial_nos
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import SerialandBatchBundle
from collections import defaultdict
from frappe.utils import add_days, cint, cstr, flt, get_link_to_form, now, nowtime, today
from frappe.query_builder.functions import CombineDatetime, Sum
from frappe import _, _dict, bold

def after_insert(self, method):
	update_parent_batch_id(self)

class SerialNoDuplicateError(frappe.ValidationError):
	pass

class CustomSerialandBatchBundle(SerialandBatchBundle):
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

