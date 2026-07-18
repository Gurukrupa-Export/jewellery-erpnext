import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)


def update_inventory_dimentions(self):
	item_groups = frappe.db.get_all(
		"Item Group", {"custom_is_alloy_group": 1}, pluck="name"
	)
	alloy_item_list = frappe.db.get_all(
		"Item",
		{"item_group": ["in", item_groups], "variant_of": ["in", ["M", "F"]]},
		pluck="name",
	)
	for row in frappe.db.get_all(
		"DocField",
		{"parent": self.reference_doctype, "fieldtype": "Table"},
		["options"],
	):
		if frappe.db.exists(row.options, self.custom_voucher_detail_no):
			self.custom_inventory_type = frappe.db.get_value(
				row.options, self.custom_voucher_detail_no, "inventory_type"
			)
			self.custom_customer = frappe.db.get_value(
				row.options, self.custom_voucher_detail_no, "customer"
			)
			attribute_value = frappe.db.get_value(
				"Item Variant Attribute",
				{"parent": self.item, "attribute": "Metal Type"},
				"attribute_value",
			)
			if self.reference_doctype != "Stock Entry":
				if self.item in alloy_item_list:
					self.custom_alloy_rate = frappe.db.get_value(
						row.options, self.custom_voucher_detail_no, "rate"
					)
				elif self.item not in alloy_item_list and frappe.db.get_value(
					"Attribute Value", attribute_value, "is_metal_type"
				):
					self.custom_metal_rate = frappe.db.get_value(
						row.options, self.custom_voucher_detail_no, "rate"
					)
			else:
				if self.item in alloy_item_list:
					self.custom_alloy_rate = frappe.db.get_value(
						row.options, self.custom_voucher_detail_no, "custom_alloy_rate"
					)
					if not self.custom_alloy_rate:
						self.custom_alloy_rate = frappe.db.get_value(
							row.options, self.custom_voucher_detail_no, "basic_rate"
						)
				elif self.item not in alloy_item_list and frappe.db.get_value(
					"Attribute Value", attribute_value, "is_metal_type"
				):
					self.custom_metal_rate = frappe.db.get_value(
						row.options, self.custom_voucher_detail_no, "custom_metal_rate"
					)
					if not self.custom_metal_rate:
						self.custom_metal_rate = frappe.db.get_value(
							row.options, self.custom_voucher_detail_no, "basic_rate"
						)
			break

	item_allows_customer_goods = frappe.db.get_value(
		"Item", self.item, "custom_inventory_type_can_be_customer_goods"
	)
	is_customer_inventory = self.custom_inventory_type in [
		"Customer Goods",
		"Customer Stock",
	]

	if (
		not item_allows_customer_goods
		and is_customer_inventory
		and not is_subcontracting_gold_repack(self)
		and not is_process_loss_repack(self)
	):
		frappe.throw(_("This item is not allowed as Customer Goods"))

	if self.reference_doctype == "Stock Entry" and self.custom_customer:
		self.custom_customer_voucher_type = frappe.db.get_value(
			"Stock Entry", self.reference_name, "customer_voucher_type"
		) or _source_batch_voucher_type(self)


def _source_batch_voucher_type(batch):
	"""The voucher type of the customer's batch this one was made from.

	``customer_voucher_type`` only lives on the SE that first *received* the goods; a derived entry
	(a Process Loss repack, a conversion) leaves the header blank, so reading it alone stamps the
	new batch with an empty voucher type and the material stops looking like the Customer
	Subcontracting / Sample / Repair stock it still is. Fall back to the batch this one was
	physically made from -- the consumed row of the same Stock Entry, matched on the customer we
	already resolved.

	The SE header is deliberately not written instead (as the Employee IR loss engine does at
	loss_stock_entry.py): setting it trips ``validate_customer_voucher``, which throws for batch
	items under "Customer Repair" -- the same trap documented at manufacturing_work_order.py's
	unpack flow.
	"""
	source_batches = frappe.db.get_all(
		"Stock Entry Detail",
		filters={
			"parent": batch.reference_name,
			"customer": batch.custom_customer,
			"s_warehouse": ["is", "set"],
			"batch_no": ["is", "set"],
		},
		pluck="batch_no",
	)
	for batch_no in source_batches:
		if not batch_no or batch_no == batch.name:
			continue
		voucher_type = frappe.db.get_value(
			"Batch", batch_no, "custom_customer_voucher_type"
		)
		if voucher_type:
			return voucher_type
	return None


def is_subcontracting_gold_repack(batch):
	if getattr(batch, "reference_doctype", None) != "Stock Entry":
		return False

	if not getattr(batch, "custom_customer", None):
		return False

	item_code = getattr(batch, "item", None)
	if not isinstance(item_code, str) or not item_code.startswith("M-G-"):
		return False

	return (
		frappe.db.get_value(
			"Stock Entry", getattr(batch, "reference_name", None), "stock_entry_type"
		)
		== "Subcontracting Repack"
	)


def is_process_loss_repack(batch):
	"""Exempt Employee IR Process Loss scrap/loss batches from the Customer Goods
	guard.

	The Process Loss Stock Entry (Employee IR receive) moves a customer's metal
	that has been booked as loss into a scrap/loss *variant* item, and that
	material stays the customer's -- so the produce row is stamped
	inventory_type = "Customer Goods" (see
	employee_ir/doc_events/loss_stock_entry.py::_resolve_batch_inventory). The
	loss variant item is not necessarily flagged
	``custom_inventory_type_can_be_customer_goods``, so without this exemption the
	batch-creation guard above would throw "This item is not allowed as Customer
	Goods" and block the whole EIR receive submit. Mirror
	``is_subcontracting_gold_repack``: only exempt batches minted by a
	Process Loss Stock Entry that actually carries a customer.
	"""
	if getattr(batch, "reference_doctype", None) != "Stock Entry":
		return False

	if not getattr(batch, "custom_customer", None):
		return False

	return (
		frappe.db.get_value(
			"Stock Entry", getattr(batch, "reference_name", None), "stock_entry_type"
		)
		== "Process Loss"
	)


def update_pure_qty(self):
	if not self.batch_qty:
		return

	variant_of = frappe.db.get_value("Item", self.item, "variant_of")

	if variant_of not in ["M", "F"]:
		return

	if not self.reference_doctype:
		return

	# company = frappe.db.get_value(self.reference_doctype, self.reference_name, "company")

	# pure_item = frappe.db.get_value("Manufacturing Setting", company, "pure_gold_item")

	manufacturer = frappe.db.get_value(
		self.reference_doctype, self.reference_name, "manufacturer"
	)

	pure_item = frappe.db.get_value(
		"Manufacturing Setting", {"manufacturer": manufacturer}, "pure_gold_item"
	)

	if not pure_item:
		return

	batch_item_purity = get_purity_percentage(self.item)
	pure_item_purity = get_purity_percentage(pure_item)

	if not batch_item_purity:
		return

	self.custom_pure_metal_qty = flt(
		(batch_item_purity * self.batch_qty) / pure_item_purity, 3
	)
