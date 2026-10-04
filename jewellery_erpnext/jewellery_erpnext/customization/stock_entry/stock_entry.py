import copy
import json

import frappe
from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry
from frappe import _
from frappe.utils import flt

# from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
# 	BatchValuationLedger,
# )
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.inventory_utils import (
	in_configured_timeslot,
	validate_customer_voucher,
	validate_sample_goods_not_consumed,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils import (
	get_fifo_batches,
	set_employee,
	set_fg_bom_weights,
	set_gross_wt,
	set_jwelex_tag_no,
	# validate_inventory_dimention,
	validate_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.transit import (
	has_non_transit_target,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.entered_metal_rate import (
	capture_entered_metal_rates,
	restore_entered_metal_rates,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.loss_valuation import (
	set_process_loss_produce_rates,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	custom_get_bom_scrap_material,
	custom_get_scrap_items_from_job_card,
)
from jewellery_erpnext.utils import bulk_map


def set_manufacturing_refs(self):
	"""Derive the PMO / operation from the MWO whenever the browser fill did not land.

	The client fill (public/js/doctype_js/stock_entry.js, manufacturing_work_order
	handler) is a single un-awaited fetch with no error handler: if its response
	carries no message the ``.then`` raises and *neither* field is written, and
	nothing retries. That leaves a mandatory field blank with no way to re-pick it
	by hand -- an FG MWO's operation is created "Finished", which the link query on
	``manufacturing_operation`` hides.

	Fill-if-empty only. ``Manufacturing Work Order.manufacturing_operation`` is a
	moving pointer (Employee IR / Department IR re-point it as work advances), so
	overwriting would silently rewrite a deliberately older operation.

	All three are Custom Fields, so read them through getattr -- a doc built without
	them must be a no-op here, never an AttributeError that aborts the whole save.
	"""
	mwo_name = getattr(self, "manufacturing_work_order", None)
	if not mwo_name:
		return

	pmo = getattr(self, "manufacturing_order", None)
	mop = getattr(self, "manufacturing_operation", None)
	if pmo and mop:
		return

	mwo = frappe.db.get_value(
		"Manufacturing Work Order",
		mwo_name,
		["manufacturing_order", "manufacturing_operation"],
		as_dict=True,
	)
	if not mwo:
		return

	self.manufacturing_order = pmo or mwo.manufacturing_order
	self.manufacturing_operation = mop or mwo.manufacturing_operation


def normalize_add_to_transit(self):
	"""Hold ``add_to_transit`` at 0 on entries that are not themselves in transit.

	ERPNext v16.36.0 (``StockEntry.validate_transit_warehouses``, frappe/erpnext#59192)
	rejects an entry with Add to Transit on whose target is not a Transit warehouse. The
	flag cannot be cleared where such an entry is built: ``Stock Entry.add_to_transit`` is
	``fetch_from`` the Stock Entry Type with ``fetch_if_empty``, and frappe's
	``_validate_links`` -- which runs before any hook -- treats 0 as empty and fetches the
	type's 1 back in for "Material Transfer (DEPARTMENT)" and "Customer Goods Transfer".
	This hook runs after that fetch and before ``StockEntry.validate``, so it is the first
	place a 0 sticks; on submit frappe no longer refetches, so the 0 reaches the database.

	Three kinds of entry are never in transit:

	* a receipt leg (``outgoing_stock_entry`` set): it is what ends the transit. ERPNext
	  hides the field on one and its own End Transit maps to a type with 0, but this app's
	  End Transit override keeps the source's transit type;
	* an entry whose maker set ``flags.no_transit`` because it moves the stock in one shot;
	* an amendment of an entry stored with 0: amending keeps the 0, and the fetch would
	  turn it back into 1.
	"""
	if self.get("outgoing_stock_entry"):
		# The Customer Goods Received > Issue mapper (doc_events.stock_entry
		# make_stock_in_entry) links its Material Issue / Receipt the same way without
		# either being a transit receipt; only a Material Transfer can end a transit.
		if self.get("purpose") == "Material Transfer":
			validate_transit_receipt_source(self)
		self.add_to_transit = 0
		return

	flags = getattr(self, "flags", None) or {}
	if flags.get("no_transit"):
		self.add_to_transit = 0
		return

	amended_from = self.get("amended_from")
	if not amended_from or not self.get("add_to_transit"):
		return

	original = frappe.db.get_value(
		"Stock Entry",
		amended_from,
		["add_to_transit", "stock_entry_type"],
		as_dict=True,
	)
	# Only the same kind of entry: an amendment that changes the type is a new decision.
	if (
		original
		and not original.add_to_transit
		and original.stock_entry_type == self.get("stock_entry_type")
	):
		self.add_to_transit = 0


def validate_transit_receipt_source(self):
	"""A receipt leg must receive a real transit entry: one that sent stock into Transit.

	Once ``normalize_add_to_transit`` clears the flag on receipt legs, the new ERPNext
	check no longer stops End Transit being run against a receipt leg (or picking one
	under Get Items From > Transit Entry). Every such "second hop" in the data was a no-op
	move back into the warehouse the stock already sat in. The flag alone is not proof
	either: thousands of older reserve entries carry add_to_transit = 1 but moved stock
	into Reserve/RM warehouses, and still show End Transit.
	"""
	source_name = self.get("outgoing_stock_entry")
	source = frappe.db.get_value(
		"Stock Entry",
		source_name,
		["add_to_transit", "outgoing_stock_entry"],
		as_dict=True,
	)
	if (
		source
		and source.add_to_transit
		and not source.outgoing_stock_entry
		and not has_non_transit_target(source_name)
	):
		return

	frappe.throw(
		_(
			"Stock Entry {0} is not an in-transit entry, so it cannot be received. "
			"Only a transfer that was sent to a Transit warehouse can be ended."
		).format(frappe.bold(self.get("outgoing_stock_entry")))
	)


def before_validate(self, method):
	if not in_configured_timeslot(self):
		frappe.throw(_("Not Allowed to do entries, its freeze time"))
	# Must precede StockEntry.validate, which rejects Add to Transit into a non-Transit
	# warehouse; doc_event before_validate handlers all run before it.
	normalize_add_to_transit(self)
	# Must precede set_employee: it reads self.manufacturing_operation to resolve
	# to_employee on Material Transfer (WORK ORDER).
	set_manufacturing_refs(self)
	validate_customer_voucher(self)
	validate_sample_goods_not_consumed(self)
	set_employee(self)
	set_gross_wt(self)
	set_fg_bom_weights(self)
	set_jwelex_tag_no(self)
	validate_warehouse(self)


def on_submit(self, method):
	pass
	# validate_inventory_dimention(self)


class CustomStockEntry(StockEntry):
	# def autoname(self):
	# 	"""
	# 	Temporarily name doc for fast insertion
	# 	name will be changed using autoname options (in a scheduled job)
	# 	"""
	# 	self.name = frappe.generate_hash(txt="", length=10)
	# 	if self.meta.autoname == "hash":
	# 		self.to_rename = 0

	@frappe.whitelist()
	def update_batches(self):
		if not self.auto_created:
			rows_to_append = []
			# shared across rows so the same batch is not double-allocated when
			# multiple rows draw from the same item/warehouse. Both FIFO-allocated
			# rows (via get_fifo_batches) and already-filled rows kept below record
			# their consumption here.
			consumed = {}
			# Prefetch the Item / Department fields read per row below (and again in
			# the rebuild loop) so the loops issue one query each instead of O(rows).
			# get_fifo_batches only splits a source row into more rows of the SAME
			# item_code, so an Item map built from self.items also covers the rebuild.
			item_map = bulk_map(
				"Item",
				[row.item_code for row in self.items],
				["variant_of", "has_batch_no"],
			)
			dept_map = bulk_map(
				"Department",
				[row.get("department") for row in self.items],
				["custom_can_not_make_dg_entry"],
			)
			for row in self.items:
				if (
					row.get("department")
					and (dept_map.get(row.department) or {}).get(
						"custom_can_not_make_dg_entry"
					)
					== 1
				):
					if (item_map.get(row.item_code) or {}).get("variant_of") in [
						"D",
						"G",
					]:
						frappe.throw(
							_("{0} not allowed in Operation {1}").format(
								row.item_code, row.department
							)
						)
				if (item_map.get(row.item_code) or {}).get("has_batch_no"):
					if row.s_warehouse:
						if row.get("batch_no"):
							# Batch already filled — keep it as-is and do NOT refetch /
							# re-split, even if the row qty now exceeds the batch's
							# available qty (an over-issue is caught at submit). Only
							# rows with an empty batch trigger FIFO allocation.
							# Record this row's consumption so a later empty row drawing
							# from the same item/warehouse can't double-book the batch.
							batch_key = (
								row.s_warehouse or self.get("source_warehouse"),
								row.batch_no,
							)
							consumed[batch_key] = consumed.get(batch_key, 0) + flt(
								row.qty
							)
							temp_row = copy.deepcopy(row)
							rows_to_append += [temp_row]
						else:
							rows_to_append += get_fifo_batches(self, row, consumed)
					elif row.t_warehouse:
						rows_to_append += [row.__dict__]
				else:
					rows_to_append += [row.__dict__]

			# The item table is always rebuilt so inventory_type / customer / diamond
			# pcs are backfilled from the batch every save. The expensive FIFO refetch
			# (get_fifo_batches) only runs for rows with an empty batch, so a save where
			# every batch-tracked source row is already filled does zero refetches.
			if rows_to_append:
				# Prefetch the two Batch fields (one query instead of two per row) and
				# the diamond-attribute lookups. FIFO introduces new batch_no values,
				# so these maps are built from rows_to_append (post-split), not
				# self.items. Every row shape here (dict, frappe._dict, and the
				# deepcopied Document) supports .get(field) -> None for a missing key.
				batch_map = bulk_map(
					"Batch",
					[it.get("batch_no") for it in rows_to_append],
					["custom_inventory_type", "custom_customer"],
				)
				d_codes = [
					it.get("item_code")
					for it in rows_to_append
					if (item_map.get(it.get("item_code")) or {}).get("variant_of")
					== "D"
				]
				grade_map, sieve_map = {}, {}
				if d_codes:
					for r in frappe.get_all(
						"Item Variant Attribute",
						filters={
							"parent": ["in", list(set(d_codes))],
							"attribute": [
								"in",
								["Diamond Grade", "Diamond Sieve Size"],
							],
						},
						fields=["parent", "attribute", "attribute_value"],
					):
						if r.attribute == "Diamond Grade":
							grade_map.setdefault(r.parent, r.attribute_value)
						else:
							sieve_map.setdefault(r.parent, r.attribute_value)

				self.items = []
				for item in rows_to_append:
					if isinstance(item, dict):
						item = frappe._dict(item)
					if item.batch_no:
						binfo = batch_map.get(item.batch_no) or {}
						if not item.inventory_type:
							item.inventory_type = binfo.get("custom_inventory_type")
						item.customer = binfo.get("custom_customer")
					if (item_map.get(item.item_code) or {}).get("variant_of") == "D":
						attribute = grade_map.get(item.item_code)
						diamond_sieve_size = sieve_map.get(item.item_code)
						weight = (
							frappe.db.get_value(
								"Attribute Value Diamond Sieve Size",
								{
									"parent": attribute,
									"diamond_sieve_size": diamond_sieve_size,
								},
								"per_pcs_average_weight",
							)
							or 0
						)

						if weight > 0 and item.qty and int(item.pcs) < 1:
							item.pcs = int(item.qty / weight)
					self.append("items", item)

			if frappe.db.exists("Stock Entry", self.name):
				self.db_update()

	def validate_with_material_request(self):
		for item in self.get("items"):
			material_request = item.material_request or None
			material_request_item = item.material_request_item or None
			if self.purpose == "Material Transfer" and self.outgoing_stock_entry:
				parent_se = frappe.get_value(
					"Stock Entry Detail",
					item.ste_detail,
					["material_request", "material_request_item"],
					as_dict=True,
				)
				if parent_se:
					material_request = parent_se.material_request
					material_request_item = parent_se.material_request_item

			if material_request:
				mreq_item = frappe.db.get_value(
					"Material Request Item",
					{"name": material_request_item, "parent": material_request},
					["item_code", "custom_alternative_item", "warehouse", "idx"],
					as_dict=True,
				)
				if item.item_code not in [
					mreq_item.item_code,
					mreq_item.custom_alternative_item,
				]:
					frappe.throw(
						_("Item for row {0} does not match Material Request").format(
							item.idx
						),
						frappe.MappingMismatchError,
					)
				elif self.purpose == "Material Transfer" and self.add_to_transit:
					continue

	def get_scrap_items_from_job_card(self):
		custom_get_scrap_items_from_job_card(self)

	def get_bom_scrap_material(self, qty):
		custom_get_bom_scrap_material(self, qty)

	def set_basic_rate(self, reset_outgoing_rate=True, raise_error_if_no_rate=True):
		"""Value a Process Loss SE's produce rows from the rows they consumed.

		ERPNext skips every ``set_basic_rate_manually`` row -- which is every loss/scrap
		produce row -- leaving basic_rate and basic_amount at 0, so the metal's value left
		the ledger and nothing replaced it. See ``utils/loss_valuation`` for why the flag
		cannot simply be dropped, and why this belongs on the controller rather than in
		each of the six builders (ERPNext re-derives Repack rates on every repost).

		Runs after super() so the consume rows already carry their basic_amount, and
		before ``update_valuation_rate`` / ``set_total_incoming_outgoing_value`` in
		``calculate_rate_and_amount``, which then pick the new values up for free.

		The capture/restore pair around super() keeps a rate the user typed on a
		Customer Goods row reachable by the Batch that row mints -- super() clears
		``basic_rate`` on every allow-zero-valuation row, which is why those batches
		were created rate-less. See ``utils/entered_metal_rate``; the ledger's
		valuation is deliberately left at 0.
		"""
		entered_rates = capture_entered_metal_rates(self)
		super().set_basic_rate(reset_outgoing_rate, raise_error_if_no_rate)
		restore_entered_metal_rates(entered_rates)
		set_process_loss_produce_rates(self)


@frappe.whitelist()
def get_html_data(doc):
	if isinstance(doc, str):
		doc = json.loads(doc)
	itemwise_data = {}
	for row in doc.get("items"):
		row = frappe._dict(row)
		if itemwise_data.get(row.item_code):
			itemwise_data[row.item_code]["qty"] += row.qty
			itemwise_data[row.item_code]["pcs"] += (
				int(row.get("pcs")) if row.get("pcs") else 0
			)
		else:
			itemwise_data[row.item_code] = {
				"qty": row.qty,
				"pcs": int(row.get("pcs")) if row.get("pcs") else 0,
			}

	data = []
	for row in itemwise_data:
		data.append(
			{
				"item_code": row,
				"qty": flt(itemwise_data[row].get("qty"), 3),
				"pcs": itemwise_data[row].get("pcs"),
			}
		)

	return data
