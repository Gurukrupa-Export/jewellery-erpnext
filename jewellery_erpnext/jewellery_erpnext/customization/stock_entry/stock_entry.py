import copy

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
	get_incoming_rate,
	clear_batch_ledger_cache
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
	# clear_batch_ledger_cache(self)


class CustomStockEntry(StockEntry):
	@frappe.whitelist()
	def update_batches(self):
		if not self.auto_created:
			rows_to_append = []
			for row in self.items:
				if (
					row.get("department")
					and frappe.db.get_value("Department", row.department, "custom_can_not_make_dg_entry") == 1
				):
					if frappe.db.get_value("Item", row.item_code, "variant_of") in ["D", "G"]:
						frappe.throw(_("{0} not allowed in Operation {1}").format(row.item_code, row.department))
				if frappe.db.get_value("Item", row.item_code, "has_batch_no"):
					if row.s_warehouse:
						if row.get("batch_no") and get_batch_qty(row.batch_no, row.s_warehouse) >= row.qty:
							temp_row = copy.deepcopy(row)
							rows_to_append += [temp_row]
						else:
							rows_to_append += get_fifo_batches(self, row)
					elif row.t_warehouse:
						rows_to_append += [row.__dict__]
				else:
					rows_to_append += [row.__dict__]

			if rows_to_append:
				self.items = []
				for item in rows_to_append:
					if isinstance(item, dict):
						item = frappe._dict(item)
					if item.batch_no:
						item.inventory_type = frappe.db.get_value("Batch", item.batch_no, "custom_inventory_type")
						item.customer = frappe.db.get_value("Batch", item.batch_no, "custom_customer")
					if frappe.db.get_value("Item", item.item_code, "variant_of") == "D":
						attribute = frappe.db.get_value(
							"Item Variant Attribute",
							{"parent": item.item_code, "attribute": "Diamond Grade"},
							"attribute_value",
						)
						diamond_sieve_size = frappe.db.get_value(
							"Item Variant Attribute",
							{"parent": item.item_code, "attribute": "Diamond Sieve Size"},
							"attribute_value",
						)
						weight = (
							frappe.db.get_value(
								"Attribute Value Diamond Sieve Size",
								{"parent": attribute, "diamond_sieve_size": diamond_sieve_size},
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
				if item.item_code not in [mreq_item.item_code, mreq_item.custom_alternative_item]:
					frappe.throw(
						_("Item for row {0} does not match Material Request").format(item.idx),
						frappe.MappingMismatchError,
					)
				elif self.purpose == "Material Transfer" and self.add_to_transit:
					continue

	@frappe.whitelist()
	def get_html_data(self):
		itemwise_data = {}
		for row in self.items:
			if itemwise_data.get(row.item_code):
				itemwise_data[row.item_code]["qty"] += row.qty
				itemwise_data[row.item_code]["pcs"] += int(row.get("pcs")) if row.get("pcs") else 0
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

	def get_scrap_items_from_job_card(self):
		custom_get_scrap_items_from_job_card(self)

	def get_bom_scrap_material(self, qty):
		custom_get_bom_scrap_material(self, qty)

	def set_rate_for_outgoing_items(self, reset_outgoing_rate=True, raise_error_if_no_rate=True):
		outgoing_items_cost = 0.0
		outgoing_items = [d for d in self.get("items") if d.s_warehouse]
		if not outgoing_items:
			return outgoing_items_cost

		args_with_key = []
		for d in outgoing_items:
			args = self.get_args_for_incoming_rate(d)
			key = (
				args.get("item_code"),
				args.get("warehouse"),
				args.get("batch_no", ""),
				args.get("voucher_detail_no") or args.get("voucher_no")
			)
			args_with_key.append((key, args, d))

		if reset_outgoing_rate:
			rates = get_incoming_rate([args for _, args, _ in args_with_key], raise_error_if_no_rate)

		for key, _, d in args_with_key:
			if reset_outgoing_rate:
				rate = rates.get(key, 0.0)
				if rate >= 0:
					d.basic_rate = rate

			d.basic_amount = flt(flt(d.transfer_qty) * flt(d.basic_rate), d.precision("basic_amount"))
			if not d.t_warehouse:
				outgoing_items_cost += flt(d.basic_amount)

		return outgoing_items_cost