import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doc_events.sales_invoice import set_gst_details


def validate(self, method):
	bom_cache = {}
	for row in self.items:
		if row.bom:
			# keyed by row identity (not row.bom) so two different item rows
			# that happen to reference the same BOM still get independent BOM
			# doc instances, matching the original per-row frappe.get_doc()
			# behavior.
			if row.idx not in bom_cache:
				bom_cache[row.idx] = frappe.get_doc("BOM", row.bom)
			if row.against_sales_order:
				bom_doc = bom_cache[row.idx]
				row.custom_diamond_pcs = bom_doc.total_diamond_pcs
				row.custom_gemstone_pcs = bom_doc.total_gemstone_pcs
				row.custom_other_weight = bom_doc.total_other_weight
				row.custom_metal_weight = bom_doc.total_metal_weight
				row.custom_finding_weight = bom_doc.finding_weight
				row.custom_diamond_weight = bom_doc.total_diamond_weight_in_gms
				row.custom_gemstone_weight = bom_doc.total_gemstone_weight_in_gms
				row.custom_gross_weight = bom_doc.gross_weight
	diamond_pcs = gemstone_pcs = other_weight = metal_weight = 0
	finding_weight = diamond_weight = gemstone_weight = gross_weight = 0
	for r in self.items:
		diamond_pcs += int(r.custom_diamond_pcs or 0)
		gemstone_pcs += float(r.custom_gemstone_pcs or 0)
		other_weight += float(r.custom_other_weight or 0)
		metal_weight += float(r.custom_metal_weight or 0)
		finding_weight += float(r.custom_finding_weight or 0)
		diamond_weight += float(r.custom_diamond_weight or 0)
		gemstone_weight += float(r.custom_gemstone_weight or 0)
		gross_weight += float(r.custom_gross_weight or 0)
	self.custom_diamond_pcs = diamond_pcs
	self.custom_gemstone_pcs = gemstone_pcs
	self.custom_other_weight = other_weight
	self.custom_metal_weight = metal_weight
	self.custom_finding_weight = finding_weight
	self.custom_diamond_weight = diamond_weight
	self.custom_gemstone_weight = gemstone_weight
	self.custom_gross_weight = gross_weight

	# The e-invoice item table and GST used to be copied straight from the
	# Sales Order (a fixed snapshot at mapping time), so removing a row here
	# left stale amounts behind. Rebuild both from whatever items are
	# currently on this Delivery Note instead.
	update_dn_einvoice_items(self, bom_cache)
	self.total = flt(sum(flt(row.amount) for row in self.items))
	set_gst_details(self)
	self.calculate_taxes_and_totals()
	apply_einvoice_item_tax(self)


def _matching_e_invoice_item_parents(sales_type):
	return frappe.get_all(
		"Sales Type Multiselect",
		filters={"parenttype": "E Invoice Item", "sales_type": sales_type},
		pluck="parent",
	)


def _match_einvoice_item(rows, filters):
	"""In-memory equivalent of frappe.db.get_value('E Invoice Item', filters, ['name', 'hsn_code', 'uom'])
	against a prefetched row list. Supports the same filter shapes used here: plain equality,
	('in', [...]) and ('is', 'not set')."""
	for row in rows:
		matched = True
		for field, value in filters.items():
			if isinstance(value, (list, tuple)):
				operator, operand = value
				if operator == "in":
					if row.get(field) not in operand:
						matched = False
						break
				elif operator == "is" and operand == "not set":
					if row.get(field):
						matched = False
						break
			elif row.get(field) != value:
				matched = False
				break
		if matched:
			return row.name, row.hsn_code, row.uom
	return None


def update_dn_einvoice_items(self, bom_cache=None):
	if bom_cache is None:
		bom_cache = {}
	is_branch_customer = frappe.db.get_value(
		"Sales Type Multiselect", {"parent": self.customer, "sales_type": "Branch"}
	)
	matching_parents = _matching_e_invoice_item_parents(self.sales_type)
	einvoice_items = frappe.get_all(
		"E Invoice Item",
		fields=[
			"name",
			"hsn_code",
			"uom",
			"is_for_metal",
			"is_for_labour",
			"is_for_making",
			"is_for_finding",
			"is_for_finding_making",
			"is_for_diamond",
			"is_for_gemstone",
			"is_for_hallmarking",
			"is_for_certification",
			"metal_type",
			"metal_purity",
			"finding_category",
			"diamond_type",
		],
		# match frappe.db.get_value()'s default tie-break (oldest `modified` first)
		# so _match_einvoice_item()'s first-match result agrees with the single-row
		# get_value() calls it replaces when a filter combo matches multiple rows.
		order_by="modified",
	)

	aggregated_metal_items = {}
	aggregated_metal_making_items = {}
	aggregated_finding_items = {}
	aggregated_finding_making_items = {}
	aggregated_diamond_items = {}
	aggregated_gemstone_items = {}
	aggregated_hallmarking_items = {}
	aggregated_certification_items = {}

	def get_einvoice_item(filters):
		return _match_einvoice_item(einvoice_items, filters) or (None, None, None)

	hallmarking_item, hallmarking_hsn, hallmarking_uom = get_einvoice_item(
		{"is_for_hallmarking": 1}
	)

	def add(bucket, item_code, hsn, uom, amount, qty):
		if not item_code:
			return
		uom = uom or "Nos"
		key = (item_code, uom)
		if key not in bucket:
			bucket[key] = {
				"item_code": item_code,
				"item_name": item_code,
				"uom": uom,
				"hsn_code": hsn,
				"qty": 0,
				"amount": 0,
			}
		bucket[key]["qty"] += qty
		bucket[key]["amount"] += amount

	for row in self.items:
		if not row.bom:
			continue
		bom_doc = bom_cache.get(row.idx)
		if bom_doc is None:
			bom_doc = frappe.get_doc("BOM", row.bom)
			bom_cache[row.idx] = bom_doc

		for i in bom_doc.metal_detail:
			if i.is_customer_item:
				continue

			metal_item, metal_hsn, metal_uom = get_einvoice_item(
				{
					"is_for_metal": 1,
					"metal_type": i.metal_type,
					"metal_purity": i.metal_touch,
					"name": ["in", matching_parents],
				}
			)
			add(
				aggregated_metal_items,
				metal_item,
				metal_hsn,
				metal_uom,
				flt(i.amount),
				flt(i.quantity),
			)

			if not is_branch_customer:
				making_item, making_hsn, making_uom = get_einvoice_item(
					{
						"is_for_making": 1,
						"metal_type": i.metal_type,
						"metal_purity": i.metal_touch,
					}
				)
				making_amount = flt(i.making_amount) + flt(i.wastage_amount)
				add(
					aggregated_metal_making_items,
					making_item,
					making_hsn,
					making_uom,
					making_amount,
					flt(i.quantity),
				)

		for i in bom_doc.finding_detail:
			if i.is_customer_item:
				continue

			finding_item, finding_hsn, finding_uom = get_einvoice_item(
				{
					"is_for_finding": 1,
					"metal_type": i.metal_type,
					"metal_purity": i.metal_touch,
					"finding_category": i.finding_category,
				}
			)
			if finding_item:
				add(
					aggregated_finding_items,
					finding_item,
					finding_hsn,
					finding_uom,
					flt(i.amount),
					flt(i.quantity),
				)
			else:
				metal_item, metal_hsn, metal_uom = get_einvoice_item(
					{
						"is_for_metal": 1,
						"metal_type": i.metal_type,
						"metal_purity": i.metal_touch,
						"finding_category": ["is", "not set"],
						"name": ["in", matching_parents],
					}
				)
				add(
					aggregated_metal_items,
					metal_item,
					metal_hsn,
					metal_uom,
					flt(i.amount),
					flt(i.quantity),
				)

			if not is_branch_customer:
				making_amount = flt(i.making_amount) + flt(i.wastage_amount)
				finding_making_item, fm_hsn, fm_uom = get_einvoice_item(
					{
						"is_for_finding_making": 1,
						"metal_type": i.metal_type,
						"metal_purity": i.metal_touch,
						"finding_category": i.finding_category,
					}
				)
				if finding_making_item:
					add(
						aggregated_finding_making_items,
						finding_making_item,
						fm_hsn,
						fm_uom,
						making_amount,
						flt(i.quantity),
					)
				else:
					metal_making_item, mm_hsn, mm_uom = get_einvoice_item(
						{
							"is_for_making": 1,
							"metal_type": i.metal_type,
							"metal_purity": i.metal_touch,
						}
					)
					add(
						aggregated_metal_making_items,
						metal_making_item,
						mm_hsn,
						mm_uom,
						making_amount,
						flt(i.quantity),
					)

		for i in bom_doc.diamond_detail:
			if i.is_customer_item:
				continue
			result = get_einvoice_item(
				{
					"is_for_diamond": 1,
					"diamond_type": i.diamond_type,
					"name": ["in", matching_parents],
				}
			)
			einvoice_item, hsn_code, uom = result
			if not einvoice_item:
				continue
			amount = flt(i.diamond_rate_for_specified_quantity)
			add(
				aggregated_diamond_items,
				einvoice_item,
				hsn_code,
				uom,
				amount,
				flt(i.quantity),
			)

		for i in bom_doc.gemstone_detail:
			if i.is_customer_item:
				continue
			einvoice_item, hsn_code, uom = get_einvoice_item(
				{"is_for_gemstone": 1, "name": ["in", matching_parents]}
			)
			if not einvoice_item:
				continue
			if is_branch_customer:
				amount = flt(i.se_rate) * flt(i.quantity)
			else:
				amount = flt(i.gemstone_rate_for_specified_quantity)
			add(
				aggregated_gemstone_items,
				einvoice_item,
				hsn_code,
				uom,
				amount,
				flt(i.quantity),
			)

		if bom_doc.hallmarking_amount:
			add(
				aggregated_hallmarking_items,
				hallmarking_item,
				hallmarking_hsn,
				hallmarking_uom,
				flt(bom_doc.hallmarking_amount),
				1,
			)

		if bom_doc.certification_amount:
			einvoice_item, hsn_code, uom = get_einvoice_item(
				{"is_for_certification": 1}
			)
			add(
				aggregated_certification_items,
				einvoice_item,
				hsn_code,
				uom,
				flt(bom_doc.certification_amount),
				1,
			)

	self.set("custom_invoice_item", [])
	for bucket in (
		aggregated_diamond_items,
		aggregated_metal_items,
		aggregated_finding_items,
		aggregated_gemstone_items,
		aggregated_metal_making_items,
		aggregated_finding_making_items,
		aggregated_hallmarking_items,
		aggregated_certification_items,
	):
		for data in bucket.values():
			data["rate"] = data["amount"] / data["qty"] if data["qty"] else 0
			self.append(
				"custom_invoice_item",
				{
					"item_code": data["item_code"],
					"item_name": data["item_name"],
					"uom": data["uom"],
					"qty": data["qty"],
					"rate": data["rate"],
					"amount": flt(data["amount"], 3),
				},
			)


def apply_einvoice_item_tax(self):
	"""set_gst_details() already stamped cgst_rate/sgst_rate/igst_rate on
	self.items (all rows share the same rate) - reuse that same rate on the
	custom_invoice_item rows, which don't go through calculate_taxes_and_totals."""
	if not self.get("custom_invoice_item"):
		return
	overall_rate = 0
	if self.items:
		first = self.items[0]
		overall_rate = flt(first.igst_rate) or (
			flt(first.cgst_rate) + flt(first.sgst_rate)
		)
	for row in self.custom_invoice_item:
		row.tax_rate = overall_rate
		row.tax_amount = flt(flt(row.amount) * overall_rate / 100, 2)
		row.amount_with_tax = flt(row.amount) + row.tax_amount
