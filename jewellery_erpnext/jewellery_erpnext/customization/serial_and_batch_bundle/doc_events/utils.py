import frappe
from erpnext.stock.serial_batch_bundle import SerialBatchBundle, SerialBatchCreation
from frappe import _, bold
from frappe.utils import (
	flt,
	get_link_to_form,
)


def _conversion_lane_map(bundle, batch_list):
	"""``{voucher_detail_no: lane tag}`` for a lane-tagged Stock Entry, else ``{}``.

	Only Metal Conversions stamps ``Stock Entry Detail.custom_conversion_lane``. An
	empty map means "this voucher is not lane-tagged", and the caller then keeps the
	original voucher-wide behaviour -- so every other flow is untouched.

	The lane cannot be inferred from the batches themselves: alloy consume rows are
	booked "Regular Stock" yet legitimately fund a customer lane, so the tag written
	by the builder is the only reliable attribution.
	"""
	if bundle.voucher_type != "Stock Entry":
		return {}

	if not frappe.db.has_column("Stock Entry Detail", "custom_conversion_lane"):
		return {}

	row_names = {b.voucher_detail_no for b in batch_list if b.voucher_detail_no}
	row_names.add(bundle.get("voucher_detail_no"))
	row_names.discard(None)
	if not row_names:
		return {}

	lane_map = {
		row.name: row.custom_conversion_lane
		for row in frappe.get_all(
			"Stock Entry Detail",
			filters={"name": ["in", list(row_names)]},
			fields=["name", "custom_conversion_lane"],
		)
		if row.custom_conversion_lane
	}

	# Scope only when the produced row itself is tagged; a partially tagged voucher
	# would otherwise silently drop origin entries.
	if not lane_map.get(bundle.get("voucher_detail_no")):
		return {}

	return lane_map


def update_parent_batch_id(self):
	if self.type_of_transaction == "Inward" and self.voucher_type in [
		"Purchase Receipt",
		"Stock Entry",
	]:
		stock_entry_type = None
		if self.voucher_type == "Stock Entry":
			purpose, stock_entry_type = frappe.db.get_value(
				"Stock Entry",
				self.voucher_no,
				["purpose", "stock_entry_type"],
			)
			if purpose not in ["Manufacture", "Repack"]:
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
			outward_entries = frappe.db.get_all(
				"Serial and Batch Entry",
				{"parent": ["in", outward_bundle]},
				["batch_no", "qty", "incoming_rate", "voucher_detail_no"],
			)
			batch_list = [
				frappe._dict(
					{
						"name": row.batch_no,
						"qty": abs(row.qty),
						"rate": row.incoming_rate,
						"voucher_detail_no": row.voucher_detail_no,
					}
				)
				for row in outward_entries
			]

			# A voucher normally has one ownership, so every consumed batch is a
			# legitimate origin of every produced batch. A Metal Conversion, though, can
			# carry several ownership lanes at once: without scoping, each target batch
			# would inherit the OTHER lanes' sources too, which both leaks provenance
			# across customers and makes batch.on_update blend one cross-lane average
			# rate for all of them. See doctype/metal_conversions.
			lane_of = _conversion_lane_map(self, batch_list)

			for row in self.entries:
				if row.batch_no:
					batch_doc = frappe.get_doc("Batch", row.batch_no)

					lane = (
						lane_of.get(self.get("voucher_detail_no")) if lane_of else None
					)
					sources = (
						[
							b
							for b in batch_list
							if lane_of.get(b.voucher_detail_no) == lane
						]
						if lane
						else batch_list
					)

					for batch in sources:
						# Recomputed per append: a batch appearing in two outward entries
						# would otherwise pass a stale snapshot twice and be
						# double-weighted in the rate blend.
						existing_entries = {
							entry.batch_no for entry in batch_doc.custom_origin_entries
						}
						if batch.name not in existing_entries:
							batch_doc.append(
								"custom_origin_entries",
								{
									"batch_no": batch.name,
									"qty": batch.qty,
									"rate": batch.rate,
								},
							)
					batch_doc.flags.is_update_origin_entries = True
					batch_doc.flags.current_stock_entry_type = stock_entry_type
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
				"type_of_transaction": "Inward"
				if self.sle.actual_qty > 0
				else "Outward",
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
			purchase_type = frappe.db.get_value(
				"Purchase Receipt", self.sle.voucher_no, "purchase_type"
			)
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
			purchase_type = frappe.db.get_value(
				"Purchase Receipt", self.sle.voucher_no, "purchase_type"
			)
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
		if self.sle.actual_qty and flt(sn_doc.total_qty, precision) != flt(
			self.sle.actual_qty, precision
		):
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
