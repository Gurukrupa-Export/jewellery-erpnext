import frappe
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report import (
	get_linked_batches,
)


def create_repack_for_customer_gold(doc, method=None):
	print("Doc:", doc.name, doc.stock_entry_type)

	if doc.doctype != "Stock Entry":
		return

	if doc.docstatus != 1:
		return

	if doc.stock_entry_type not in [
		"Customer Goods Received",
		"Customer Goods Transfer",
	]:
		return

	has_target_wh = False

	for d in doc.items:
		if d.t_warehouse == "Central RM - GEPL":
			has_target_wh = True
			break

	if not has_target_wh:
		return

	source_customer = doc._customer

	print("Source Customer:", source_customer)

	if not source_customer:
		return

	source_batches = []

	for d in doc.items:
		if not d.batch_no:
			continue

		source_batches.append(
			{
				"batch_no": d.batch_no,
				"qty": flt(d.qty),
				"remaining_qty": flt(d.qty),
			}
		)

	print(f"Source batches: {source_batches}")

	if not source_batches:
		return

	mtwo_data = frappe.db.sql(
		"""
		SELECT
			se.name AS stock_entry,
			sed.name AS stock_entry_detail,
			sed.batch_no AS target_batch,
			sed.qty,
			sed.custom_pure_qty,
			sed.item_code,
			se.manufacturing_work_order,
			se.manufacturing_order AS pmo,
			pmo.customer,
			pmo.is_customer_gold

		FROM `tabStock Entry` se

		INNER JOIN `tabStock Entry Detail` sed
			ON sed.parent = se.name

		LEFT JOIN `tabParent Manufacturing Order` pmo
			ON pmo.name = se.manufacturing_order

		WHERE
			se.docstatus = 1

			AND se.stock_entry_type IN (
				'Material Transfer (WORK ORDER)',
				'Material Transfer to Department'
			)

			AND pmo.customer = %(customer)s
			AND sed.batch_no IS NOT NULL

		ORDER BY se.creation ASC
		""",
		{"customer": source_customer},
		as_dict=1,
	)

	print(f"MTWO Data: {mtwo_data}")

	if not mtwo_data:
		return

	for row in mtwo_data:
		if not row.is_customer_gold:
			print("Skipping Non Customer Gold")
			continue

		already_repacked = frappe.db.exists(
			"Stock Entry",
			{
				"stock_entry_type": "Subcontracting Repack",
				"docstatus": 1,
				"custom_reference_stock_entry": row.stock_entry,
				"custom_reference_detail": row.stock_entry_detail,
			},
		)

		if already_repacked:
			print(f"Already Repacked : " f"{row.stock_entry_detail}")

			continue

		target_batch = row.target_batch

		if not target_batch:
			continue

		target_purity = get_purity(row.item_code)

		if not target_purity:
			frappe.log_error(
				f"Purity Not Found For Item {row.item_code}", "Customer Gold Repack"
			)

			continue

		required_qty = flt(row.qty * (target_purity / 100), 6)

		print(f"Required Qty: {required_qty}")

		if required_qty <= 0:
			continue

		linked_batches = get_linked_batches(target_batch)

		parent_batch = None

		for b in linked_batches:
			item = frappe.get_value("Batch", b, "item")

			if item and "24KT" in item:
				parent_batch = b
				break

		print(f"Parent Batch: {parent_batch}")

		if not parent_batch:
			frappe.log_error(
				f"24KT Parent Batch Not Found : {target_batch}", "Customer Gold Repack"
			)

			continue

		for source_row in source_batches:
			if required_qty <= 0:
				break

			if source_row["remaining_qty"] <= 0:
				continue

			consume_qty = min(source_row["remaining_qty"], required_qty)

			if consume_qty <= 0:
				continue

			print(f"Creating Repack : " f"{consume_qty}")

			create_gold_repack_entry(
				source_batch=source_row["batch_no"],
				target_batch=parent_batch,
				qty=consume_qty,
				source_customer=source_customer,
				reference_stock_entry=row.stock_entry,
				reference_detail=row.stock_entry_detail,
			)

			source_row["remaining_qty"] -= consume_qty

			required_qty -= consume_qty

			print(f"Remaining Source Qty : " f"{source_row['remaining_qty']}")

			print(f"Pending Required Qty : " f"{required_qty}")

		if required_qty > 0:
			frappe.msgprint(
				f"""
				Insufficient Customer Gold.

				Pending Qty:
				{required_qty}
				"""
			)


def create_gold_repack_entry(
	source_batch,
	target_batch,
	qty,
	source_customer,
	reference_stock_entry,
	reference_detail,
):
	print("Initiated Repack Creation")

	source_batch_doc = frappe.get_doc("Batch", source_batch)

	target_batch_doc = frappe.get_doc("Batch", target_batch)

	se = frappe.new_doc("Stock Entry")

	se.stock_entry_type = "Subcontracting Repack"
	se.purpose = "Repack"

	se.company = frappe.defaults.get_user_default("Company")

	se.custom_reference_stock_entry = reference_stock_entry

	se.custom_reference_detail = reference_detail

	se.append(
		"items",
		{
			"item_code": source_batch_doc.item,
			"batch_no": source_batch,
			"s_warehouse": "Central RM - GEPL",
			"qty": qty,
			"customer": source_customer,
			"inventory_type": (source_batch_doc.custom_inventory_type),
			"is_finished_item": 0,
			"use_serial_batch_fields": 1,
		},
	)

	se.append(
		"items",
		{
			"item_code": target_batch_doc.item,
			"batch_no": target_batch,
			"t_warehouse": "Central RM - GEPL",
			"qty": qty,
			"customer": (
				target_batch_doc.custom_customer
				if target_batch_doc.custom_customer
				else None
			),
			"inventory_type": (target_batch_doc.custom_inventory_type),
			"is_finished_item": 1,
			"use_serial_batch_fields": 1,
		},
	)

	se.insert(ignore_permissions=True)
	se.submit()

	print(f"Repack Created : {se.name}")


def get_purity(item_code):
	item = frappe.get_doc("Item", item_code)
	purity = 100
	for attr in item.attributes:
		if attr.attribute == "Metal Purity":
			purity = flt(attr.attribute_value)
	return purity
