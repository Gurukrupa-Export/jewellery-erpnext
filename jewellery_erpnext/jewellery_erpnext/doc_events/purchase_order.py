import json

import frappe
from erpnext.setup.utils import get_exchange_rate
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import refetch_fg_purchase_rate

# from jewellery_erpnext.utils import update_existing

BOM_AMOUNT_FIELDS = [
	"making_fg_purchase",
	"finding_bom_amount",
	"diamond_fg_purchase",
	"gemstone_fg_purchase",
	"certification_amount",
	"freight_amount",
	"hallmarking_amount",
	"custom_duty_amount",
]

# gold_bom_amount is the metal value; gold_rate_with_gst is the rate it was computed at and is only
# ever a divisor -- never add it to an amount. bom_type/docstatus decide whether an unpriced BOM
# can be repriced at all.
BOM_STATE_FIELDS = BOM_AMOUNT_FIELDS + [
	"gold_bom_amount",
	"gold_rate_with_gst",
	"bom_type",
	"docstatus",
]

# The amounts the FG Purchase rate is actually built from. If every one is zero the BOM has never
# been priced, as opposed to being legitimately worth nothing.
FG_AMOUNT_FIELDS = (
	"making_fg_purchase",
	"finding_bom_amount",
	"diamond_fg_purchase",
	"gemstone_fg_purchase",
)

# bom.py only runs set_bom_rate for these types -- saving any other type (notably "Template")
# is a no-op, so never spend a BOM save attempting it.
PRICEABLE_BOM_TYPES = ("Quotation", "Sales Order", "Manufacturing Process")

# One Purchase Order can carry 150 rows. Repricing every unpriced BOM inline would run 150 full
# BOM validates in a single request, so heal this many per save and report the remainder.
MAX_INLINE_BOM_REFETCH = 25


def validate(self, method):
	update_rate(self)


def update_rate(self):
	if self.purchase_type != "FG Purchase":
		return

	bom_data = frappe._dict()
	heal = frappe._dict(attempted=set(), repriced=set(), unpriced=set(), deferred=set())
	updated = False

	for row in self.items:
		if not row.manufacturing_bom:
			continue

		# confirm with Rajnibhai on 8 Jan 2025
		# bom_doc = frappe.get_doc("BOM", row.manufacturing_bom)
		# bom_doc.gold_rate_with_gst = self.gold_rate_with_gst
		# bom_doc.validate()
		# bom_doc.save()

		if row.manufacturing_bom not in bom_data:
			bom_data[row.manufacturing_bom] = _get_bom_state(row.manufacturing_bom)

		bom_doc = bom_data.get(row.manufacturing_bom)
		if not bom_doc:
			frappe.throw(
				_("Row #{0}: Manufacturing BOM {1} does not exist.").format(
					row.idx, row.manufacturing_bom
				)
			)

		# A BOM with no FG amounts at all was never priced -- Manufacturing Plan adopts the Sales
		# Order BOM with a raw db.set_value, so BOM.validate (and set_bom_rate with it) never ran.
		# Fetch the rates once, here, at the point of use.
		if _needs_fg_rate(bom_doc):
			bom_doc = _fetch_fg_rate(row.manufacturing_bom, bom_data, heal)

		# Metal. gold_bom_amount is the BOM's metal value at the BOM's own gold rate; rescale only
		# when the buyer entered a gold rate on this order, matching set_bom_rate_in_quotation
		# (bom_utils) and sales_invoice. With no rate on the order the row keeps the BOM's own
		# valuation, which is what the Quotation was priced at.
		gold_factor = 1.0
		if flt(self.gold_rate_with_gst) > 0 and flt(bom_doc.gold_rate_with_gst) > 0:
			gold_factor = flt(self.gold_rate_with_gst) / flt(bom_doc.gold_rate_with_gst)

		# gold_bom_amount covers metal_detail + finding_detail (bom_utils.get_gold_rate sums both)
		# and finding_bom_amount is the finding slice of it -- subtract so findings are paid once.
		# Quotation and Sales Invoice add both and so count finding metal twice; the PO does not.
		gold = flt(bom_doc.gold_bom_amount)
		finding = flt(bom_doc.finding_bom_amount)

		# A BOM saved before bom_utils always-persisted finding_bom_amount can still carry a stale
		# value after its findings were removed. It is a slice of gold_bom_amount, so it can never
		# legitimately exceed it -- drop it rather than drive metal negative. Zeroing the impossible
		# component (instead of clamping metal) keeps metal + finding == gold, so the rate is right.
		if finding > gold:
			finding = 0.0

		row.metal_amount = (gold - finding) * gold_factor
		row.finding_amount = finding * gold_factor

		row.making_amount = flt(bom_doc.making_fg_purchase)
		row.diamond_amount = flt(bom_doc.diamond_fg_purchase)
		row.gemstone_amount = flt(bom_doc.gemstone_fg_purchase)
		row.custom_certification_amount = flt(bom_doc.certification_amount)
		row.custom_freight_amount = flt(bom_doc.freight_amount)
		row.custom_hallmarking_amount = flt(bom_doc.hallmarking_amount)
		row.custom_custom_duty_amount = flt(bom_doc.custom_duty_amount)

		row.rate = flt(
			row.metal_amount
			+ row.making_amount
			+ row.finding_amount
			+ row.diamond_amount
			+ row.gemstone_amount
			+ row.custom_certification_amount
			+ row.custom_freight_amount
			+ row.custom_hallmarking_amount
			+ row.custom_custom_duty_amount,
			row.precision("rate"),
		)
		updated = True

	_report_unpriced(heal)

	if updated:
		# doc_events `validate` handlers run AFTER the controller's own validate (frappe's
		# Document.hook compose()), so calculate_taxes_and_totals() has already run against the
		# pre-update rates -- amount, net_amount and the document totals would otherwise stay one
		# save behind. Called directly rather than via run_method so the hooks do not re-enter.
		self.calculate_taxes_and_totals()
		# set_total_in_words() is a separate call in AccountsController.validate (line ~309), so
		# it also ran against the old total and would still print "INR Zero only".
		self.set_total_in_words()


def _get_bom_state(bom_name):
	return frappe.db.get_value("BOM", bom_name, BOM_STATE_FIELDS, as_dict=1)


def _needs_fg_rate(bom_doc):
	"""True when nothing the Purchase Order reads from this BOM has ever been priced."""
	return not any(flt(bom_doc.get(field)) for field in FG_AMOUNT_FIELDS)


def _fetch_fg_rate(bom_name, bom_data, heal):
	"""Reprice one unpriced BOM in place, at most once per Purchase Order save.

	Returns the BOM state to use for this row -- refreshed when the fetch ran, otherwise the
	original. Never raises: `refetch_fg_purchase_rate` contains each BOM in a savepoint, so a BOM
	that throws during validate rolls back on its own and the Purchase Order still saves.
	"""
	if bom_name in heal.attempted:
		return bom_data[bom_name]

	heal.attempted.add(bom_name)
	bom_doc = bom_data[bom_name]

	# Saving a type bom.py excludes, or a submitted BOM, fetches nothing -- do not spend the save.
	if bom_doc.docstatus != 0 or bom_doc.bom_type not in PRICEABLE_BOM_TYPES:
		heal.unpriced.add(bom_name)
		return bom_doc

	# Budget the BOM *saves*, not the rows looked at -- an order full of Template BOMs must not
	# exhaust the cap and defer the handful of BOMs that could actually have been repriced.
	if len(heal.repriced) >= MAX_INLINE_BOM_REFETCH:
		heal.deferred.add(bom_name)
		return bom_doc

	heal.repriced.add(bom_name)

	# Each BOM warns about its own unpriced rows; this function reports one consolidated message.
	frappe.flags.ignore_fg_rate_warning = True
	try:
		refetch_fg_purchase_rate([bom_name], quiet=True)
	finally:
		frappe.flags.ignore_fg_rate_warning = False

	bom_data[bom_name] = _get_bom_state(bom_name) or bom_doc
	if _needs_fg_rate(bom_data[bom_name]):
		heal.unpriced.add(bom_name)

	return bom_data[bom_name]


def _report_unpriced(heal):
	"""One message for the whole order rather than one per BOM."""
	if heal.unpriced:
		frappe.msgprint(
			_("No FG Purchase Rate found for BOM: {0}<br>Check Making Charge Price and Diamond "
			  "Price List for this customer.").format(", ".join(sorted(heal.unpriced))),
			title=_("FG Purchase Rate"),
			indicator="orange",
		)

	if heal.deferred:
		frappe.msgprint(
			_("{0} more BOM(s) still need pricing. Save again, or use 'Fetch FG Purchase "
			  "Rate'.").format(len(heal.deferred)),
			title=_("FG Purchase Rate"),
			indicator="orange",
		)


@frappe.whitelist()
def fetch_fg_purchase_rate(purchase_order):
	"""Re-fetch supplier FG purchase rates into the BOMs this Purchase Order reads.

	Manufacturing Plan does this at submit time (`ManufacturingPlan.fetch_fg_purchase_rate`), but
	BOMs adopted before that existed -- or priced before the customer price masters were filled in
	-- still hold stale rates. Saving the BOM runs `BOM.validate` -> `set_bom_rate`, which fetches
	`fg_purchase_rate` for the metal, finding, diamond and gemstone rows and rolls the amounts up
	into the BOM Amount tab.
	"""
	doc = frappe.get_doc("Purchase Order", purchase_order)
	doc.check_permission("write")

	return refetch_fg_purchase_rate(
		row.manufacturing_bom for row in doc.items if row.manufacturing_bom
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
			supplier_wise_items[row.supplier]["schedule_date"] = row.estimated_delivery_date
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

	gold_rate = _source_gold_rate(doc)

	for row in supplier_wise_items:
		po_doc = frappe.new_doc("Purchase Order")
		po_doc.supplier = row
		po_doc.company = doc.company
		po_doc.schedule_date = supplier_wise_items[row].get("schedule_date") or po_doc.transaction_date
		po_doc.purchase_type = supplier_wise_items[row].get("purchase_type")
		po_doc.ref_customer = supplier_wise_items[row].get("ref_customer")
		po_doc.manufacturing_plan = doc.name
		po_doc.custom_customer_po = supplier_wise_items[row].get("custom_customer_po")
		po_doc.is_subcontracted = supplier_wise_items[row].get("is_subcontracted")
		if gold_rate:
			po_doc.gold_rate_with_gst = gold_rate
		for item in supplier_wise_items[row]["items"]:
			po_doc.append("items", item)
		po_doc.save()


def _source_gold_rate(doc):
	"""Gold rate to stamp on the Purchase Order, taken from the source Sales Order(s).

	Returns a rate only when every source order agrees. A single header rate rescales the metal on
	every row (`update_rate`), so stamping one rate onto an order drawn from Sales Orders quoted at
	different gold rates would silently reprice some of them. Leaving it unset is always safe --
	each row then keeps its own BOM's valuation.
	"""
	sales_orders = {row.sales_order for row in doc.manufacturing_plan_table if row.get("sales_order")}
	if not sales_orders:
		return None

	# One query however many orders feed the plan -- a 133-row plan can span dozens of them.
	rates = {
		flt(row.gold_rate_with_gst)
		for row in frappe.get_all(
			"Sales Order",
			filters={"name": ["in", list(sales_orders)]},
			fields=["gold_rate_with_gst"],
		)
	}
	rates.discard(0)

	return rates.pop() if len(rates) == 1 else None


def on_cancel(doc, method=None):
	pass
	# update_existing("Manufacturing Plan Table", doc.rowname, "manufacturing_order_qty", f"manufacturing_order_qty - {doc.qty}")
	# update_existing("Sales Order Item", doc.sales_order_item, "manufacturing_order_qty", f"manufacturing_order_qty - {doc.qty}")


@frappe.whitelist()
def make_quotation(source_name, target_doc=None):
	def set_missing_values(source, target):
		from erpnext.controllers.accounts_controller import get_default_taxes_and_charges

		quotation = frappe.get_doc(target)
		company_currency = frappe.get_cached_value("Company", quotation.company, "default_currency")
		customer = frappe.db.get_value("Company", source.company, "customer_code")

		target.party_name = customer

		if company_currency == quotation.currency:
			exchange_rate = 1
		else:
			exchange_rate = get_exchange_rate(
				quotation.currency, company_currency, quotation.transaction_date, args="for_selling"
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
		field_map = {
			"transaction_date": "transaction_date",
			"ref_customer": "ref_customer",
		}
		for target_field, source_field in field_map.items():
			quotation.set(target_field, source.get(source_field))

	if isinstance(target_doc, str):
		target_doc = json.loads(target_doc)
	if not target_doc:
		target_doc = frappe.new_doc("Quotation")
	else:
		target_doc = frappe.get_doc(target_doc)

	po_doc = frappe.get_doc("Purchase Order", source_name)

	target_doc.po_no = po_doc.custom_customer_po

	for row in po_doc.items:
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
				"custom_po_details": row.name,
			},
		)
	set_missing_values(po_doc, target_doc)

	return target_doc
