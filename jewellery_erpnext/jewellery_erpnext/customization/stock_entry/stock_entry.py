import copy
import json
from functools import lru_cache

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.inventory_utils import (
	in_configured_timeslot,
	validate_customer_voucher,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils import (
	get_fifo_batches,
	set_employee,
	set_gross_wt,
	validate_inventory_dimention,
	validate_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	custom_get_bom_scrap_material,
	custom_get_scrap_items_from_job_card,
)


def before_validate(self, method):
	if not in_configured_timeslot(self):
		frappe.throw(_("Not Allowed to do entries, its freeze time"))
	validate_customer_voucher(self)
	set_employee(self)
	set_gross_wt(self)
	validate_warehouse(self)


def on_submit(self, method):
	validate_inventory_dimention(self)


def _get_diamond_weights(item_codes: set) -> dict:
	if not item_codes:
		return {}

	attributes = frappe.get_all(
		"Item Variant Attribute",
		filters={
			"parent": ["in", list(item_codes)],
			"attribute": ["in", ["Diamond Grade", "Diamond Sieve Size"]],
		},
		fields=["parent", "attribute", "attribute_value"],
	)

	item_attrs = {}
	for attr in attributes:
		item_attrs.setdefault(attr.parent, {})[attr.attribute] = attr.attribute_value

	grade_sieve_pairs = {
		(v["Diamond Grade"], v["Diamond Sieve Size"])
		for v in item_attrs.values()
		if "Diamond Grade" in v and "Diamond Sieve Size" in v
	}

	if not grade_sieve_pairs:
		return {}

	weight_rows = frappe.get_all(
		"Attribute Value Diamond Sieve Size",
		filters={
			"parent": ["in", [g for g, _ in grade_sieve_pairs]],
			"diamond_sieve_size": ["in", [s for _, s in grade_sieve_pairs]],
		},
		fields=["parent", "diamond_sieve_size", "per_pcs_average_weight"],
	)
	weight_map = {
		(r.parent, r.diamond_sieve_size): (r.per_pcs_average_weight or 0)
		for r in weight_rows
	}

	return {
		item_code: weight_map.get(
			(attrs.get("Diamond Grade"), attrs.get("Diamond Sieve Size")), 0
		)
		for item_code, attrs in item_attrs.items()
	}


class CustomStockEntry(StockEntry):
	@frappe.whitelist()
	def update_batches(self):
		if self.auto_created:
			return

		@lru_cache(maxsize=None)
		def get_item_variant_of(item_code):
			return frappe.db.get_value("Item", item_code, "variant_of")

		@lru_cache(maxsize=None)
		def item_has_batch(item_code):
			return frappe.db.get_value("Item", item_code, "has_batch_no")

		@lru_cache(maxsize=None)
		def dept_blocks_dg(department):
			return (
				frappe.db.get_value(
					"Department", department, "custom_can_not_make_dg_entry"
				)
				== 1
			)

		rows_to_append = []

		for row in self.items:
			if row.get("department") and dept_blocks_dg(row.department):
				if get_item_variant_of(row.item_code) in ("D", "G"):
					frappe.throw(
						_("{0} not allowed in Operation {1}").format(
							row.item_code, row.department
						)
					)

			if item_has_batch(row.item_code):
				if row.s_warehouse:
					has_valid_batch = (
						row.get("batch_no")
						and get_batch_qty(row.batch_no, row.s_warehouse) >= row.qty
					)
					rows_to_append += (
						[copy.deepcopy(row)]
						if has_valid_batch
						else get_fifo_batches(self, row)
					)
				elif row.t_warehouse:
					rows_to_append.append(row.__dict__)
			else:
				rows_to_append.append(row.__dict__)

		if not rows_to_append:
			return

		batch_nos = {
			(
				item["batch_no"]
				if isinstance(item, dict)
				else getattr(item, "batch_no", None)
			)
			for item in rows_to_append
		}
		batch_nos.discard(None)

		batch_meta = {}
		if batch_nos:
			batch_meta = {
				b.name: b
				for b in frappe.get_all(
					"Batch",
					filters={"name": ["in", list(batch_nos)]},
					fields=["name", "custom_inventory_type", "custom_customer"],
				)
			}

		d_item_codes = {
			(item["item_code"] if isinstance(item, dict) else item.item_code)
			for item in rows_to_append
			if get_item_variant_of(
				item["item_code"] if isinstance(item, dict) else item.item_code
			)
			== "D"
		}
		diamond_weights = _get_diamond_weights(d_item_codes) if d_item_codes else {}

		self.items = []
		for item in rows_to_append:
			item = frappe._dict(item) if isinstance(item, dict) else item

			if item.batch_no and item.batch_no in batch_meta:
				meta = batch_meta[item.batch_no]
				if not item.inventory_type:
					item.inventory_type = meta.custom_inventory_type
				item.customer = meta.custom_customer

			if get_item_variant_of(item.item_code) == "D":
				weight = diamond_weights.get(item.item_code) or 0
				if weight > 0 and item.qty and int(item.get("pcs") or 0) < 1:
					item.pcs = int(item.qty / weight)

			self.append("items", item)

		if frappe.db.exists("Stock Entry", self.name):
			self.db_update()

	def validate_with_material_request(self):
		transit_items = {}
		if self.purpose == "Material Transfer" and self.outgoing_stock_entry:
			ste_details = [
				item.ste_detail for item in self.get("items") if item.ste_detail
			]
			if ste_details:
				transit_items = {
					r.name: r
					for r in frappe.get_all(
						"Stock Entry Detail",
						filters={"name": ["in", ste_details]},
						fields=["name", "material_request", "material_request_item"],
					)
				}

		mr_item_keys = []
		item_mr_map = {}

		for item in self.get("items"):
			mr = item.material_request or None
			mr_item = item.material_request_item or None

			if item.ste_detail and item.ste_detail in transit_items:
				parent = transit_items[item.ste_detail]
				mr = parent.material_request
				mr_item = parent.material_request_item

			if mr and mr_item:
				item_mr_map[item.idx] = (mr, mr_item)
				mr_item_keys.append(mr_item)

		if not item_mr_map:
			return

		mreq_items = {
			r.name: r
			for r in frappe.get_all(
				"Material Request Item",
				filters={"name": ["in", mr_item_keys]},
				fields=[
					"name",
					"item_code",
					"custom_alternative_item",
					"warehouse",
					"idx",
				],
			)
		}

		for item in self.get("items"):
			if item.idx not in item_mr_map:
				continue

			mr, mr_item_name = item_mr_map[item.idx]
			mreq_item = mreq_items.get(mr_item_name)

			if not mreq_item:
				continue

			if item.item_code not in (
				mreq_item.item_code,
				mreq_item.custom_alternative_item,
			):
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


@frappe.whitelist()
def get_html_data(doc):
	"""Aggregate item quantities and pcs counts from stock entry items."""
	if isinstance(doc, str):
		doc = json.loads(doc)

	itemwise_data = {}
	for row in doc.get("items", []):
		row = frappe._dict(row)
		entry = itemwise_data.get(row.item_code)
		if entry:
			entry["qty"] += row.qty
			entry["pcs"] += int(row.pcs) if row.get("pcs") else 0
		else:
			itemwise_data[row.item_code] = {
				"qty": row.qty,
				"pcs": int(row.pcs) if row.get("pcs") else 0,
			}

	return [
		{
			"item_code": item_code,
			"qty": flt(data["qty"], 3),
			"pcs": data["pcs"],
		}
		for item_code, data in itemwise_data.items()
	]
