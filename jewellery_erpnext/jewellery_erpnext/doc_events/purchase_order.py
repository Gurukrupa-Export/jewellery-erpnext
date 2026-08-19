import json

import frappe
from erpnext.setup.utils import get_exchange_rate
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events.remote_po import (
	assert_local_customer,
	fetch_remote_po,
	fetch_remote_ref_customer,
	remote_lookup_configured,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice import (
	_get_rcm_base_account_head,
)

# from jewellery_erpnext.utils import update_existing


def validate(self, method):
	update_rate(self)
	set_gst_details(self)
	self.calculate_taxes_and_totals()


def set_gst_details(self):
	if self.purchase_type not in (
		"Finished Goods",
		"FG Purchase",
		"Subcontracting",
		"Branch Purchase",
	):
		return

	customer_state = frappe.db.get_value(
		"Address", self.supplier_address, "gst_state_number"
	)
	company_state = frappe.db.get_value(
		"Address", self.billing_address, "gst_state_number"
	)

	if not customer_state or not company_state:
		return

	self.tax_category = "In-State" if customer_state == company_state else "Out-State"

	item_template_map = {
		"Finished Goods": {
			"Gurukrupa Export Private Limited": "GST 3% - GEPL",
			"KG GK Jewellers Private Limited": "GST 3% - KGJPL",
		},
		"FG Purchase": {
			"Gurukrupa Export Private Limited": "GST 3% - GEPL",
			"KG GK Jewellers Private Limited": "GST 3% - KGJPL",
		},
		"Subcontracting": {
			"Gurukrupa Export Private Limited": "GST 5% - GEPL",
			"KG GK Jewellers Private Limited": "GST 5% - KGJPL",
		},
		"Branch Purchase": {
			"Gurukrupa Export Private Limited": "GST 3% - GEPL",
			"KG GK Jewellers Private Limited": "GST 3% - KGJPL",
		},
	}
	item_tax_template = item_template_map.get(self.purchase_type, {}).get(self.company)

	if not item_tax_template:
		return

	taxes_and_charges = frappe.db.get_value(
		"Purchase Taxes and Charges Template",
		{
			"company": self.company,
			"tax_category": self.tax_category,
			"disabled": 0,
		},
		"name",
	)
	self.taxes_and_charges = taxes_and_charges
	if not taxes_and_charges:
		frappe.log_error(
			f"No Sales Taxes and Charges Template found for "
			f"Company: {self.company}, Tax Category: {self.tax_category}",
			"set_gst_details",
		)
		return
	# frappe.throw(f"{taxes_and_charges}")
	self.taxes_and_charges = taxes_and_charges

	template_rates = frappe.get_all(
		"Item Tax Template Detail",
		filters={"parent": item_tax_template},
		fields=["tax_type", "tax_rate"],
	)

	cgst_rate = sgst_rate = igst_rate = 0.0
	for r in template_rates:
		tax_type = r.tax_type or ""
		if "Output" not in tax_type or "RCM" in tax_type:
			continue
		if "CGST" in tax_type:
			cgst_rate = float(r.tax_rate)
		elif "SGST" in tax_type:
			sgst_rate = float(r.tax_rate)
		elif "IGST" in tax_type:
			igst_rate = float(r.tax_rate)

	# Header row rate: read the item's real Item Tax Template directly
	# (Input Tax heads) rather than self.items[0].item_tax_rate -- that
	# field is computed by core ERPNext's update_item_tax_map(), which
	# silently backfills any account head the template doesn't define
	# with whatever rate the tax row already had (the Purchase Taxes and
	# Charges Template default), so an RCM head can look "resolved" while
	# actually still carrying the stale default. Same fix already applied
	# and verified for Purchase Invoice/Purchase Receipt in
	# doc_events/purchase_invoice.py::sync_tax_row_rate_with_item().
	input_tax_rates = {
		r.tax_type: flt(r.tax_rate)
		for r in template_rates
		if (r.tax_type or "").startswith("Input")
	}

	self.taxes = []

	tax_rows = frappe.get_all(
		"Purchase Taxes and Charges",
		filters={"parent": self.taxes_and_charges},
		fields=[
			"charge_type",
			"account_head",
			"description",
			"rate",
			"cost_center",
			"add_deduct_tax",
		],
		order_by="idx asc",
	)

	resolved_rates = {}
	for t in tax_rows:
		if t.account_head in input_tax_rates:
			t.rate = input_tax_rates[t.account_head]
			resolved_rates[t.account_head] = t.rate

	# RCM (Reverse Charge) account heads (e.g. "Input Tax IGST RCM - KGJPL")
	# have no entry of their own in the item's Item Tax Template -- only
	# the plain "Input Tax IGST - KGJPL" head does. By GST rules the RCM
	# rate is always the same as the corresponding normal rate, so mirror
	# it instead of leaving the row on the Purchase Taxes and Charges
	# Template default.
	for t in tax_rows:
		if t.account_head in input_tax_rates:
			continue
		base_account_head = _get_rcm_base_account_head(t.account_head)
		if base_account_head and base_account_head in resolved_rates:
			t.rate = resolved_rates[base_account_head]

	for t in tax_rows:
		self.append(
			"taxes",
			{
				"charge_type": t.charge_type,
				"account_head": t.account_head,
				"description": t.description,
				"rate": t.rate,
				"cost_center": t.cost_center,
				"tax_amount": round(self.total * t.rate / 100, 2),
				"total": round((self.total * t.rate / 100) + self.total, 2),
				"category": "Total",
				# preserve the template row's own Add/Deduct -- this was
				# previously hardcoded to "Add" for every row, which would
				# have applied an RCM row as an extra charge instead of a
				# deduction.
				"add_deduct_tax": t.add_deduct_tax,
			},
		)
		self.total_taxes_and_charges = round((self.total * t.rate / 100) + self.total, 2)

		self.grand_total = self.total_taxes_and_charges
	for item in self.items:
		if not item.item_code:
			continue

		item.item_tax_template = item_tax_template
		item.gst_treatment = "Taxable"
		item.cgst_rate = 0.0
		item.sgst_rate = 0.0
		item.igst_rate = 0.0
		item.cgst_amount = 0.0
		item.sgst_amount = 0.0
		item.igst_amount = 0.0

		taxable_value = float(item.taxable_value)

		if self.tax_category == "In-State":
			item.cgst_rate = cgst_rate
			item.sgst_rate = sgst_rate
			item.cgst_amount = flt(taxable_value * cgst_rate / 100, 2)
			item.sgst_amount = flt(taxable_value * sgst_rate / 100, 2)
		else:
			item.igst_rate = igst_rate
			item.igst_amount = flt(taxable_value * igst_rate / 100, 2)


def update_rate(self):
	if self.purchase_type == "FG Purchase" and not self.is_new():
		bom_data = frappe._dict()
		for row in self.items:
			if row.manufacturing_bom:
				# confirm with Rajnibhai on 8 Jan 2025
				# bom_doc = frappe.get_doc("BOM", row.manufacturing_bom)
				# bom_doc.gold_rate_with_gst = self.gold_rate_with_gst
				# bom_doc.validate()
				# bom_doc.save()

				field = [
					"making_fg_purchase",
					"finding_bom_amount",
					"diamond_fg_purchase",
					"gemstone_fg_purchase",
					"certification_amount",
					"freight_amount",
					"hallmarking_amount",
					"custom_duty_amount",
				]

				if not bom_data.get(row.manufacturing_bom):
					bom_data[row.manufacturing_bom] = frappe.db.get_value(
						"BOM", row.manufacturing_bom, field, as_dict=1
					)
				bom_doc = bom_data.get(row.manufacturing_bom)
				row.making_amount = bom_doc.making_fg_purchase
				row.finding_amount = bom_doc.finding_bom_amount
				row.diamond_amount = bom_doc.diamond_fg_purchase
				row.gemstone_amount = bom_doc.gemstone_fg_purchase
				row.custom_certification_amount = bom_doc.certification_amount
				row.custom_freight_amount = bom_doc.freight_amount
				row.custom_hallmarking_amount = bom_doc.hallmarking_amount
				row.custom_custom_duty_amount = bom_doc.custom_duty_amount

				row.rate = (
					row.metal_amount
					+ row.making_amount
					+ row.finding_amount
					+ row.diamond_amount
					+ row.gemstone_amount
					+ row.custom_certification_amount
					+ row.custom_freight_amount
					+ row.custom_hallmarking_amount
					+ row.custom_custom_duty_amount
				)


def make_subcontracting_order(doc):
	default_item = frappe.db.get_single_value("Jewellery Settings", "service_item")
	supplier_wise_items = frappe._dict()
	for row in doc.manufacturing_plan_table:
		if not supplier_wise_items.get(row.supplier):
			supplier_wise_items.setdefault(row.supplier, {"items": []})

		supplier_wise_items[row.supplier]["ref_customer"] = row.get("customer", None)
		supplier_wise_items[row.supplier]["purchase_type"] = row.purchase_type
		supplier_wise_items[row.supplier]["custom_customer_po"] = row.customer_po

		if row.purchase_type == "FG Purchase":
			supplier_wise_items[row.supplier]["items"].append(
				{
					"item_code": (row.item_code),
					"qty": row.subcontracting_qty,
					"manufacturing_bom": row.manufacturing_bom,
					"diamond_quality": row.diamond_quality,
					"custom_manufacturing_plan": doc.name,
					"custom_m_plan_details": row.name,
					"custom_child_po_no": row.child_po,
				}
			)
		else:
			supplier_wise_items[row.supplier]["is_subcontracted"] = 1
			supplier_wise_items[row.supplier][
				"schedule_date"
			] = row.estimated_delivery_date
			supplier_wise_items[row.supplier]["items"].append(
				{
					"item_code": default_item,
					"qty": 1,
					"fg_item": row.item_code,
					"fg_item_qty": row.subcontracting_qty,
					"schedule_date": row.estimated_delivery_date,
					"custom_child_po_no": row.child_po,
					"custom_m_plan_details": row.name,
				}
			)

	for row in supplier_wise_items:
		po_doc = frappe.new_doc("Purchase Order")
		po_doc.supplier = row
		po_doc.company = doc.company
		po_doc.schedule_date = (
			supplier_wise_items[row].get("schedule_date") or po_doc.transaction_date
		)
		po_doc.purchase_type = supplier_wise_items[row].get("purchase_type")
		po_doc.ref_customer = supplier_wise_items[row].get("ref_customer")
		po_doc.manufacturing_plan = doc.name
		po_doc.custom_customer_po = supplier_wise_items[row].get("custom_customer_po")
		po_doc.is_subcontracted = supplier_wise_items[row].get("is_subcontracted")
		for item in supplier_wise_items[row]["items"]:
			po_doc.append("items", item)
		po_doc.save()


def on_cancel(doc, method=None):
	pass
	# update_existing("Manufacturing Plan Table", doc.rowname, "manufacturing_order_qty", f"manufacturing_order_qty - {doc.qty}")
	# update_existing("Sales Order Item", doc.sales_order_item, "manufacturing_order_qty", f"manufacturing_order_qty - {doc.qty}")


@frappe.whitelist()
def get_po_ref_customer(po_name):
	"""Return the Ref Customer recorded on a Purchase Order.

	Read-only lookup for the KGGK site, whose mirrored copy of a Gurukrupa Export Purchase Order can
	reach it without ref_customer. Returns None rather than throwing for an unknown name, because
	the caller runs inside a Quotation save and swallows everything it gets back.

	Superseded by get_po_for_quotation, which returns this value alongside everything else the
	mapper needs. Kept because the owning site serves KGGK deploys at more than one version, and a
	caller that only knows this endpoint must keep working.
	"""
	if not po_name or not frappe.db.exists("Purchase Order", po_name):
		return None

	frappe.has_permission("Purchase Order", "read", doc=po_name, throw=True)

	return frappe.db.get_value("Purchase Order", po_name, "ref_customer")


# Read as a filtered list rather than a fixed one: a caller site can be a version ahead of this
# one, and a column this site does not carry would otherwise raise 1054 and 500 the whole endpoint
# instead of returning the fields it does have.
_PO_HEADER_FIELDS = (
	"name",
	"company",
	"supplier",
	"purchase_type",
	"docstatus",
	"transaction_date",
	"ref_customer",
	"custom_customer_po",
)

_PO_ITEM_FIELDS = (
	"name",
	"item_code",
	"qty",
	"rate",
	"branch",
	"project",
	"diamond_quality",
)


@frappe.whitelist()
def get_po_for_quotation(po_name):
	"""Return everything make_quotation needs to build a Quotation from this Purchase Order.

	The KGGK site works from mirrored copies of Gurukrupa Export Purchase Orders. A mirror can land
	without ref_customer, and KGGK has no Company row carrying the buying company's customer_code
	either -- so neither the Quotation's Customer nor its Ref Customer can be resolved there. Both
	are resolved here, on the site that owns the Purchase Order and has the masters to do it.

	Returns None rather than throwing for an unknown name: the caller runs inside a Quotation save
	and falls back to its own local copy of the Purchase Order for anything it cannot use.
	"""
	if not po_name or not frappe.db.exists("Purchase Order", po_name):
		return None

	frappe.has_permission("Purchase Order", "read", doc=po_name, throw=True)

	po = frappe.db.get_value(
		"Purchase Order",
		po_name,
		_present_columns("Purchase Order", _PO_HEADER_FIELDS),
		as_dict=True,
	)
	if not po:
		return None

	# The Company -> Customer mapping the selling side needs for party_name. This is the field the
	# caller cannot resolve for itself, and the reason this endpoint exists at all.
	po["customer_code"] = frappe.db.get_value(
		"Company", po.get("company"), "customer_code"
	)

	# get_all, not get_list: the parent permission check above is the gate, and the child table
	# carries no meaningful permissions of its own.
	po["items"] = frappe.get_all(
		"Purchase Order Item",
		filters={"parent": po_name},
		fields=_present_columns("Purchase Order Item", _PO_ITEM_FIELDS),
		order_by="idx asc",
	)

	return po


def _present_columns(doctype, fieldnames):
	"""Return the subset of fieldnames that exist as columns on doctype."""
	return [f for f in fieldnames if frappe.db.has_column(doctype, f)]


def _resolve_purchase_order(source_name):
	"""Return ``(po, is_remote)`` -- the Purchase Order to map from, owning site preferred.

	A site holding only a mirror cannot answer two of the questions the Quotation needs: the mirror
	can arrive without ref_customer, and that site has no Company row for the buying company, so its
	customer_code is out of reach too. Both come back resolved when the fetch succeeds.

	Falls back to the local copy, so a site that owns its Purchase Orders -- and any site whose peer
	is briefly unreachable -- keeps working. The fallback is announced only when a fetch was
	actually expected; where no From Site is configured the local read is simply the normal path.
	"""
	remote = fetch_remote_po(source_name)
	if remote:
		po = frappe._dict(remote)
		# Subscript, never attribute access: _dict subclasses dict, so po.items resolves to the
		# built-in dict.items method and silently shadows the child table.
		po["items"] = [frappe._dict(row) for row in (po.get("items") or [])]
		return po, True

	if remote_lookup_configured():
		frappe.msgprint(
			_(
				"Could not read Purchase Order {0} from the site that owns it. Used this site's copy,"
				" which may be missing Customer and Ref Customer."
			).format(source_name),
			indicator="orange",
			alert=True,
		)

	local = frappe.get_doc("Purchase Order", source_name)

	return (
		frappe._dict(
			name=local.name,
			company=local.company,
			transaction_date=local.transaction_date,
			ref_customer=local.get("ref_customer"),
			custom_customer_po=local.get("custom_customer_po"),
			customer_code=frappe.db.get_value(
				"Company", local.company, "customer_code"
			),
			items=[
				frappe._dict(
					name=row.name,
					item_code=row.get("item_code"),
					qty=row.get("qty"),
					rate=row.get("rate"),
					branch=row.get("branch"),
					project=row.get("project"),
					diamond_quality=row.get("diamond_quality"),
				)
				for row in local.items
			],
		),
		False,
	)


@frappe.whitelist()
def make_quotation(source_name, target_doc=None):
	def set_missing_values(source, target, is_remote):
		from erpnext.controllers.accounts_controller import (
			get_default_taxes_and_charges,
		)

		quotation = frappe.get_doc(target)
		company_currency = frappe.get_cached_value(
			"Company", quotation.company, "default_currency"
		)

		# Resolved by whichever site supplied the Purchase Order -- the owning one when the fetch
		# succeeded, this one otherwise. Guarded because party_name is a Link: a Customer name this
		# site does not carry would turn a blank field into a hard throw on save.
		target.party_name = assert_local_customer(
			source.get("customer_code"), source.name, "Customer"
		)

		if company_currency == quotation.currency:
			exchange_rate = 1
		else:
			exchange_rate = get_exchange_rate(
				quotation.currency,
				company_currency,
				quotation.transaction_date,
				args="for_selling",
			)
		quotation.conversion_rate = exchange_rate
		# get default taxes
		taxes = get_default_taxes_and_charges(
			"Sales Taxes and Charges Template", company=quotation.company
		)
		if taxes.get("taxes"):
			quotation.update(taxes)
		quotation.run_method("set_missing_values")
		quotation.run_method("calculate_taxes_and_totals")

		quotation.quotation_to = "Customer"
		quotation.transaction_date = source.get("transaction_date")

		ref_customer = source.get("ref_customer")
		if not ref_customer and not is_remote:
			# Only worth asking when the full fetch did not already answer it: this is the local
			# mirror, arrived without ref_customer, and an owning site a version behind still
			# serves the narrow endpoint even though it has no get_po_for_quotation.
			ref_customer = fetch_remote_ref_customer(source.name)

		ref_customer = assert_local_customer(ref_customer, source.name, "Ref Customer")
		if ref_customer:
			quotation.set("ref_customer", ref_customer)

	if isinstance(target_doc, str):
		target_doc = json.loads(target_doc)
	if not target_doc:
		target_doc = frappe.new_doc("Quotation")
	else:
		target_doc = frappe.get_doc(target_doc)

	po_doc, is_remote = _resolve_purchase_order(source_name)

	target_doc.po_no = po_doc.get("custom_customer_po")

	for row in po_doc.get("items") or []:
		target_doc.append(
			"items",
			{
				"branch": row.get("branch"),
				"project": row.get("project"),
				"item_code": row.get("item_code"),
				"qty": row.get("qty"),
				"diamond_quality": row.get("diamond_quality"),
				"custom_customer_gold": "No",
				"rate": row.get("rate"),
				"custom_customer_diamond": "No",
				"custom_customer_stone": "No",
				"custom_customer_good": "No",
				"po_no": po_doc.get("name"),
				"custom_po_details": row.get("name"),
			},
		)
	set_missing_values(po_doc, target_doc, is_remote)

	return target_doc
