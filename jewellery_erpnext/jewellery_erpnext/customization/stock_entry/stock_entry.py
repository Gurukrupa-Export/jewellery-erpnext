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
	set_gross_wt,
	# validate_inventory_dimention,
	validate_warehouse,
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


def before_validate(self, method):
	if not in_configured_timeslot(self):
		frappe.throw(_("Not Allowed to do entries, its freeze time"))
	validate_customer_voucher(self)
	validate_sample_goods_not_consumed(self)
	set_employee(self)
	set_gross_wt(self)
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
