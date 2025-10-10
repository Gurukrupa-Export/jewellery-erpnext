import frappe
from erpnext.stock.serial_batch_bundle import SerialBatchBundle, SerialBatchCreation
from frappe.utils import flt
from frappe import _, _dict, bold
from frappe.utils import add_days, cint, cstr, flt, get_link_to_form, now, nowtime, today

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
		if self.sle.actual_qty > 0 and self.is_material_transfer():
			self.make_serial_batch_no_bundle_for_material_transfer()
			return

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
				"make_bundle_from_sle": 1,
				"sle": self.sle,
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
