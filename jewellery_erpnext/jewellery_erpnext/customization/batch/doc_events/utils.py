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
			# Stamp the responsible employee so scrap/dust can be fetched employee-wise
			# in refining (Batch.custom_employee). Prefer the source row's own employee
			# (Employee Loss Entry sets it per produce row); fall back to the Stock Entry
			# header employee (Employee IR Process Loss sets it only on the header).
			# Copied the same way custom_customer/custom_inventory_type are.
			emp = frappe.db.get_value(
				row.options, self.custom_voucher_detail_no, "employee"
			)
			if not emp and self.reference_doctype == "Stock Entry":
				emp = frappe.db.get_value(
					"Stock Entry", self.reference_name, "employee"
				)
			if emp:
				self.custom_employee = emp
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
		and not is_repair_unpack(self)
	):
		frappe.throw(
			_(
				"Item {0} is not allowed as {1} (customer: {2}, batch: {3}). Tick "
				"'Inventory Type Can be Customer Goods' on the Item, or book it as "
				"Regular Stock."
			).format(
				frappe.bold(self.item),
				self.custom_inventory_type,
				self.custom_customer or "-",
				frappe.bold(self.name or self.get("batch_id") or _("new")),
			)
		)

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


def is_repair_unpack(batch):
	"""Exempt Repair-Unpack Customer Goods batches from the item-flag guard.

	``create_unpack_serial_no_stock_entry`` (manufacturing_work_order.py) disassembles a
	customer's repair article into its FULL design-BOM composition and books every component
	as the customer's stock. The article was physically brought in by the customer, so each
	component IS theirs -- ownership comes from the repair, not from a
	``custom_inventory_type_can_be_customer_goods`` flag on each component's Item master. That
	flag is sparsely maintained (no finding variant carries it), so without this exemption an
	unpack throws "Item ... is not allowed as Customer Goods" on the first unflagged diamond or
	finding and the whole submit fails. Same rationale as the loss variant in
	``is_process_loss_repack``.

	Unlike its two sibling helpers, this one CANNOT key on ``reference_doctype`` alone: the
	unpack mints each component's Batch *standalone, before the Stock Entry exists*
	(``batch_doc.save()`` runs before the SE is built), so at the only moment the guard fires
	``reference_doctype`` is still None. It is instead recognised by the ``Customer Repair``
	voucher type stamped on the batch just before that save -- a marker no other flow writes.
	The reference-based leg (mirroring the two siblings) is kept as well, so the exemption also
	holds if the batch is re-validated after the SE links it. Both legs require a customer:
	a Customer Goods batch with no customer is malformed and must not be silently exempted (see
	``normalize_ownership`` rule 3 in customization/utils/row_ownership.py).
	"""
	if not getattr(batch, "custom_customer", None):
		return False

	if getattr(batch, "custom_customer_voucher_type", None) == "Customer Repair":
		return True

	if getattr(batch, "reference_doctype", None) != "Stock Entry":
		return False

	return (
		frappe.db.get_value(
			"Stock Entry", getattr(batch, "reference_name", None), "stock_entry_type"
		)
		== "Repair Unpack"
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
