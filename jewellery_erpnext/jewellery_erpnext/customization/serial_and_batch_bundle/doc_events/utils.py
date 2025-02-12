import frappe
from erpnext.stock.serial_batch_bundle import SerialBatchBundle, SerialBatchCreation
from frappe.utils import flt


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
				"custom_voucher_detail_no": self.get("voucher_detail_no") or None,
			}
		)
	)
