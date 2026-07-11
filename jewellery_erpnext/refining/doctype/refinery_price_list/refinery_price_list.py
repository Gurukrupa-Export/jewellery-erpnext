# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today


class RefineryPriceList(Document):
	def validate(self):
		# to_weight == 0 encodes "no upper bound" ("Above N"), so only compare when set.
		if flt(self.to_weight) and flt(self.from_weight) > flt(self.to_weight):
			frappe.throw(
				_("From Weight ({0}) cannot be greater than To Weight ({1}).").format(
					self.from_weight, self.to_weight
				)
			)


def on_doctype_update():
	# Index the lookup keys so get_refinery_rate is cheap (mirrors Diamond Price List).
	frappe.db.add_index(
		"Refinery Price List",
		["dust_item", "refining_process", "from_weight", "to_weight"],
		index_name="refinery_band",
	)


def get_refinery_rate(
	dust_item=None, weight=0, refining_process=None, company=None, supplier=None
):
	"""Return the best-matching Refinery Price List row for a dust item / process + weight band.

	``weight`` is in grams. A row matches when ``from_weight <= weight <= to_weight``, where
	``to_weight = 0`` means "no upper bound" (the "Above N" bands). ``dust_item`` and
	``refining_process`` are each optional filters — pass whichever the entry can determine;
	``refining_process`` disambiguates rows where the same dust item is priced differently by
	process (e.g. Vacuum Bag: Polishing vs Floor Cleaning). ``company`` may be blank on a row
	to mean "any". ``supplier`` narrows to supplier-specific rates; rows with no supplier
	always qualify.

	Returns a dict ``{name, dust_item, refining_process, rate, charge_type, weight_basis,
	service_item, currency}`` or ``None`` when no band matches (callers should raise a clear
	"configure the price list" message).
	"""
	conditions = [
		"%(weight)s BETWEEN from_weight AND (CASE WHEN to_weight = 0 THEN 1e12 ELSE to_weight END)",
	]
	params = {"weight": flt(weight), "today": today()}

	if dust_item:
		conditions.append("dust_item = %(dust_item)s")
		params["dust_item"] = dust_item
	if refining_process:
		conditions.append("refining_process = %(refining_process)s")
		params["refining_process"] = refining_process
	if company:
		conditions.append("(company = %(company)s OR company IS NULL OR company = '')")
		params["company"] = company
	if supplier:
		conditions.append(
			"(supplier = %(supplier)s OR supplier IS NULL OR supplier = '')"
		)
		params["supplier"] = supplier

	# Respect effective dating when set; undated rows always apply.
	conditions.append("(effective_from IS NULL OR effective_from <= %(today)s)")

	rows = frappe.db.sql(
		"""
		SELECT name, dust_item, refining_process, rate, charge_type,
			weight_basis, service_item, currency
		FROM `tabRefinery Price List`
		WHERE {conditions}
		ORDER BY effective_from DESC, creation DESC
		LIMIT 1
		""".format(conditions=" AND ".join(conditions)),
		params,
		as_dict=True,
	)
	return rows[0] if rows else None


def compute_refining_amount(charge_type, rate, weight_g):
	"""Charge amount for a price row given its charge type and the billable weight (grams).

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
