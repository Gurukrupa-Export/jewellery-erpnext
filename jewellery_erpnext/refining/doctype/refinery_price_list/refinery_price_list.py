# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today


class RefineryPriceList(Document):
	def validate(self):
		self.validate_slab_bands()
		self.validate_unique_item_scope()

	def validate_slab_bands(self):
		for slab in self.slabs:
			# to_weight == 0 encodes "no upper bound" ("Above N"), so only compare when set.
			if flt(slab.to_weight) and flt(slab.from_weight) > flt(slab.to_weight):
				frappe.throw(
					_(
						"Row #{0}: From Weight ({1}) cannot be greater than To Weight ({2})."
					).format(slab.idx, slab.from_weight, slab.to_weight)
				)

	def validate_unique_item_scope(self):
		"""One price list document per (item, supplier, company) scope — duplicate
		documents for the same scope would make get_refinery_rate's pick order
		arbitrary between them."""
		existing = frappe.db.get_value(
			"Refinery Price List",
			{
				"item": self.item,
				"supplier": self.supplier or "",
				"company": self.company or "",
				"name": ["!=", self.name],
			},
			"name",
		)
		if existing:
			frappe.throw(
				_(
					"Refinery Price List {0} already covers Item {1} for this "
					"supplier/company scope. Add slabs there instead."
				).format(frappe.bold(existing), frappe.bold(self.item))
			)


def on_doctype_update():
	# Index the lookup key so get_refinery_rate is cheap.
	frappe.db.add_index("Refinery Price List", ["item"], index_name="refinery_item")


def get_refinery_rate(item_code, weight=0, company=None, supplier=None):
	"""Return the best-matching price slab for ``item_code`` at ``weight`` grams.

	The Refinery Price List holds ONE document per item (each item can be refined
	under multiple processes — the document's child slabs carry the per-process /
	per-weight-band rates). The matching slab is the first (by row order) whose band
	covers ``weight``, where ``to_weight = 0`` means "no upper bound" ("Above N").
	Supplier-specific price lists win over generic ones; ``company``/``supplier`` may
	be blank on a document to mean "any".

	Returns a dict ``{name, item, refining_process, rate, charge_type, weight_basis,
	service_item, currency, slab_name}`` (``name`` is the parent document, so existing
	``Purchase Order Item.custom_refining_price_list`` back-links keep working) or
	``None`` when nothing matches (callers treat billing as best-effort).
	"""
	if not item_code:
		return None

	weight = flt(weight)
	filters = {"item": item_code}
	parents = frappe.get_all(
		"Refinery Price List",
		filters=filters,
		fields=["name", "item", "supplier", "company", "currency", "effective_from"],
	)

	def qualifies(p):
		if p.effective_from and str(p.effective_from) > today():
			return False
		if company and p.company and p.company != company:
			return False
		if supplier and p.supplier and p.supplier != supplier:
			return False
		return True

	candidates = [p for p in parents if qualifies(p)]
	# Supplier-specific price lists first, then company-specific, then generic;
	# newest effective_from wins within a tier.
	candidates.sort(
		key=lambda p: (
			0 if (supplier and p.supplier == supplier) else 1,
			0 if (company and p.company == company) else 1,
			str(p.effective_from or ""),
		),
	)

	for parent in candidates:
		slabs = frappe.get_all(
			"Refinery Price Slab",
			filters={"parent": parent.name, "parenttype": "Refinery Price List"},
			fields=[
				"name",
				"refining_process",
				"from_weight",
				"to_weight",
				"charge_type",
				"rate",
				"weight_basis",
				"service_item",
			],
			order_by="idx asc",
		)
		for slab in slabs:
			upper = flt(slab.to_weight) or 1e12
			if flt(slab.from_weight) <= weight <= upper:
				return {
					"name": parent.name,
					"item": parent.item,
					"refining_process": slab.refining_process,
					"rate": slab.rate,
					"charge_type": slab.charge_type,
					"weight_basis": slab.weight_basis,
					"service_item": slab.service_item,
					"currency": parent.currency,
					"slab_name": slab.name,
				}
	return None


def compute_refining_amount(charge_type, rate, weight_g):
	"""Charge amount for a price slab given its charge type and the billable weight (grams).

	``Flat Charge`` → rate; ``Per Gram`` → rate × grams; ``Per Kg`` → rate × grams / 1000.
	"""
	rate = flt(rate)
	weight_g = flt(weight_g)
	if charge_type == "Per Gram":
		return flt(rate * weight_g, 2)
	if charge_type == "Per Kg":
		return flt(rate * weight_g / 1000.0, 2)
	# Flat Charge (default)
	return flt(rate, 2)
