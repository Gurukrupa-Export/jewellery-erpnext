import frappe
from erpnext.stock.serial_batch_bundle import SerialBatchBundle, SerialBatchCreation
from frappe.utils import flt
from frappe import _, _dict, bold
from frappe.utils import flt, get_link_to_form, getdate

from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	get_available_batches,
	get_stock_ledgers_batches,
	get_reserved_batches_for_pos,
	get_reserved_batches_for_sre,
	get_picked_batches,
	update_available_batches,
	filter_zero_near_batches,
	get_qty_based_available_batches,
)

def update_parent_batch_id(self):
	if self.type_of_transaction == "Inward" and self.voucher_type in [
		"Purchase Receipt",
		"Stock Entry",
	]:
		if self.voucher_type == "Stock Entry" and frappe.db.get_value(
			"Stock Entry", self.voucher_no, "purpose"
		) not in ["Manufacture", "Repack"]:
			return
		outward_bundle = frappe.db.get_all(
			"Serial and Batch Bundle",
			{
				"type_of_transaction": "Outward",
				"voucher_type": self.voucher_type,
				"voucher_no": self.voucher_no,
			},
			pluck="name",
		)

		if outward_bundle:
			batch_list = [
				frappe._dict({"name": row.batch_no, "qty": abs(row.qty), "rate": row.incoming_rate})
				for row in frappe.db.get_all(
					"Serial and Batch Entry",
					{"parent": ["in", outward_bundle]},
					["batch_no", "qty", "incoming_rate"],
				)
			]

			for row in self.entries:
				if row.batch_no:
					batch_doc = frappe.get_doc("Batch", row.batch_no)

					existing_entries = [row.batch_no for row in batch_doc.custom_origin_entries]

					for batch in batch_list:
						if batch.name not in existing_entries:
							batch_doc.append(
								"custom_origin_entries", {"batch_no": batch.name, "qty": batch.qty, "rate": batch.rate}
							)
					batch_doc.flags.is_update_origin_entries = True
					batch_doc.save()


class CustomSerialBatchBundle(SerialBatchBundle):
	def make_serial_batch_no_bundle(self):
		self.validate_item()

		sn_doc = CustomSerialBatchCreation(
			{
				"item_code": self.item_code,
				"warehouse": self.warehouse,
				"posting_date": self.sle.posting_date,
				"posting_time": self.sle.posting_time,
				"voucher_type": self.sle.voucher_type,
				"voucher_no": self.sle.voucher_no,
				"voucher_detail_no": self.sle.voucher_detail_no,
				"qty": self.sle.actual_qty,
				"avg_rate": self.sle.incoming_rate,
				"total_amount": flt(self.sle.actual_qty) * flt(self.sle.incoming_rate),
				"type_of_transaction": "Inward" if self.sle.actual_qty > 0 else "Outward",
				"company": self.company,
				"is_rejected": self.is_rejected_entry(),
			}
		).make_serial_and_batch_bundle()

		self.set_serial_and_batch_bundle(sn_doc)

	def validate_item_and_warehouse(self):
		# Skip validation if Purchase Receipt has purchase_type = "Branch Purchase"
		if self.sle.voucher_type == "Purchase Receipt":
			purchase_type = frappe.db.get_value("Purchase Receipt", self.sle.voucher_no, "purchase_type")
			if purchase_type == "Branch Purchase" or purchase_type == "FG Purchase":
				return  # Skip validation

		if self.sle.serial_and_batch_bundle and not frappe.db.exists(
			"Serial and Batch Bundle",
			{
				"name": self.sle.serial_and_batch_bundle,
				"item_code": self.item_code,
				"warehouse": self.warehouse,
				"voucher_no": self.sle.voucher_no,
			},
		):
			msg = f"""
				The Serial and Batch Bundle
				{bold(self.sle.serial_and_batch_bundle)}
				does not belong to Item {bold(self.item_code)}
				or Warehouse {bold(self.warehouse)}
				or {self.sle.voucher_type} no {bold(self.sle.voucher_no)}
			"""
			frappe.throw(_(msg))

	def validate_actual_qty(self, sn_doc):
		link = get_link_to_form("Serial and Batch Bundle", sn_doc.name)
		if self.sle.voucher_type == "Purchase Receipt":
			purchase_type = frappe.db.get_value("Purchase Receipt", self.sle.voucher_no, "purchase_type")
			if purchase_type == "Branch Purchase" or purchase_type == "FG Purchase":
				return  # Skip validation
		condition = {
			"Inward": self.sle.actual_qty > 0,
			"Outward": self.sle.actual_qty < 0,
		}.get(sn_doc.type_of_transaction)

		if not condition and self.sle.actual_qty:
			correct_type = "Inward"
			if sn_doc.type_of_transaction == "Inward":
				correct_type = "Outward"

			msg = f"The type of transaction of Serial and Batch Bundle {link} is {bold(sn_doc.type_of_transaction)} but as per the Actual Qty {self.sle.actual_qty} for the item {bold(self.sle.item_code)} in the {self.sle.voucher_type} {self.sle.voucher_no} the type of transaction should be {bold(correct_type)}"
			frappe.throw(_(msg), title=_("Incorrect Type of Transaction"))

		precision = sn_doc.precision("total_qty")
		if self.sle.actual_qty and flt(sn_doc.total_qty, precision) != flt(self.sle.actual_qty, precision):
			msg = f"Total qty {flt(sn_doc.total_qty, precision)} of Serial and Batch Bundle {link} is not equal to Actual Qty {flt(self.sle.actual_qty, precision)} in the {self.sle.voucher_type} {self.sle.voucher_no}"
			frappe.throw(_(msg))



class CustomSerialBatchCreation(SerialBatchCreation):
	def create_batch(self):
		return custom_create_batch(self)


def custom_create_batch(self):
	from erpnext.stock.doctype.batch.batch import make_batch

	return make_batch(
		frappe._dict(
			{
				"item": self.get("item_code"),
				"reference_doctype": self.get("voucher_type"),
				"reference_name": self.get("voucher_no"),
				"custom_voucher_detail_no": self.get("voucher_detail_no"),
			}
		)
	)


def get_auto_batch_nos(kwargs):
	available_batches = get_available_batches(kwargs)
	qty = flt(kwargs.qty)

	stock_ledgers_batches = get_stock_ledgers_batches(kwargs)
	pos_invoice_batches = get_reserved_batches_for_pos(kwargs)
	sre_reserved_batches = get_reserved_batches_for_sre(kwargs)
	picked_batches = frappe._dict()
	if kwargs.get("is_pick_list"):
		picked_batches = get_picked_batches(kwargs)

	if stock_ledgers_batches or pos_invoice_batches or sre_reserved_batches or picked_batches:
		update_available_batches(
			available_batches,
			stock_ledgers_batches,
			pos_invoice_batches,
			sre_reserved_batches,
			picked_batches,
		)

	if kwargs.based_on == "Expiry":
		available_batches = sorted(available_batches, key=lambda x: (x.expiry_date or getdate("9999-12-31")))

	if not kwargs.get("do_not_check_future_batches") and available_batches and kwargs.get("posting_date"):
		filter_zero_near_batches(available_batches, kwargs)

	if not kwargs.consider_negative_batches:
		precision = frappe.get_precision("Stock Ledger Entry", "actual_qty")
		available_batches = [d for d in available_batches if flt(d.qty, precision) > 0]

	if not qty:
		return available_batches

	is_customer_goods = kwargs.get("is_customer_goods")
	filter_inventory_based_batches(available_batches, is_customer_goods)

	return get_qty_based_available_batches(available_batches, qty)


def filter_inventory_based_batches(available_batches, is_customer_goods):
	"""
	Filter out batches that are only for customer goods.
	"""
	if not available_batches:
		return []

	customer_inventories = ["Customer Goods", "Customer Stock"]

	for batch in available_batches:
		inventory_type = frappe.db.get_value("Batch", batch.batch_no, "custom_inventory_type")

		if is_customer_goods and inventory_type not in customer_inventories:
			available_batches.remove(batch)
		elif not is_customer_goods and inventory_type in customer_inventories:
			available_batches.remove(batch)
