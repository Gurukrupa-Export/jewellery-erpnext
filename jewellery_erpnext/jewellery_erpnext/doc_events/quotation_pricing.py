"""
Pricing helper functions for applying customer-specific pricing
to Tracking BOM child tables during Quotation BOM creation.

These functions were extracted from the original create_quotation_bom logic.
They apply diamond, metal, finding, gemstone pricing based on customer price lists.
"""
import frappe


def _apply_kg_gk_pricing(
	self,
	row,
	doc,
	ref_customer,
	diamond_price_list_ref_customer,
	gemstone_price_list_ref_customer,
	diamond_price_list_customer,
	gemstone_price_list_customer,
	diamond_price_list,
	gemstone_price_list,
):
	"""Apply KG GK Jewellers Private Limited specific pricing logic."""
	doc.company = self.company

	# Diamond pricing for KG GK
	for diamond in doc.diamond_detail:
		diamond.rate = doc.gold_rate_with_gst
		if self.custom_customer_diamond == "Yes":
			diamond.is_customer_item = 1
		if diamond_price_list and any(
			dpl["price_list_type"] == diamond_price_list_ref_customer
			for dpl in diamond_price_list
		):
			if diamond_price_list_ref_customer == "Size (in mm)":
				_apply_kg_gk_diamond_size_mm(
					doc, diamond, diamond_price_list_customer, ref_customer
				)
			if diamond_price_list_ref_customer == "Sieve Size Range":
				_apply_kg_gk_diamond_sieve(
					doc, diamond, diamond_price_list_ref_customer, ref_customer
				)
			if diamond_price_list_ref_customer == "Weight (in cts)":
				_apply_kg_gk_diamond_weight(
					doc, diamond, diamond_price_list_ref_customer, ref_customer
				)

	# Finding pricing for KG GK
	for find in doc.finding_detail:
		_apply_making_charge_finding(self, doc, find, ref_customer)

	# Gemstone pricing for KG GK
	for gem in doc.gemstone_detail:
		gem.rate = doc.gold_rate_with_gst
		if gemstone_price_list and any(
			dpl["price_list_type"] == gemstone_price_list_ref_customer
			for dpl in gemstone_price_list
		):
			if gemstone_price_list_ref_customer == "Multiplier":
				_apply_gemstone_multiplier(
					doc, gem, row, ref_customer, gemstone_price_list_ref_customer
				)

	# Metal pricing for KG GK
	for metal in doc.metal_detail:
		_apply_making_charge_metal(self, doc, metal, ref_customer)

	# Other detail
	for other in doc.other_detail:
		if row.custom_customer_good == "Yes":
			other.is_customer_item = 1


def _apply_standard_pricing(
	self,
	row,
	doc,
	attribute_data,
	metal_criteria,
	diamond_price_list_customer,
	gemstone_price_list_customer,
	diamond_price_list,
	gemstone_price_list,
):
	"""Apply standard company pricing logic (non-KG GK)."""
	# Set customer item flags
	for item in doc.metal_detail + doc.finding_detail:
		if (
			row.custom_customer_finding == "Yes"
			and item.parentfield == "finding_detail"
			and item.finding_category in attribute_data
		):
			item.is_customer_item = 1
		if row.custom_customer_gold == "Yes":
			if (
				item.parentfield == "finding_detail"
				and item.finding_category not in attribute_data
			):
				item.is_customer_item = 1
			elif item.parentfield != "finding_detail":
				item.is_customer_item = 1
		if item.metal_touch:
			item.metal_purity = metal_criteria.get(item.metal_touch)

	# Metal pricing
	for metal in doc.metal_detail:
		_apply_making_charge_metal(self, doc, metal, doc.customer)

	# Finding pricing
	for find in doc.finding_detail:
		_apply_making_charge_finding(self, doc, find, doc.customer)

	# Diamond pricing
	for diamond in doc.diamond_detail:
		diamond.rate = doc.gold_rate_with_gst
		if self.custom_customer_diamond == "Yes":
			diamond.is_customer_item = 1
		if diamond_price_list and any(
			dpl["price_list_type"] == diamond_price_list_customer
			for dpl in diamond_price_list
		):
			_apply_standard_diamond_pricing(doc, diamond, diamond_price_list_customer)

		if diamond_price_list_customer == "Sieve Size Range":
			_apply_standard_diamond_sieve(doc, diamond, diamond_price_list_customer)
		if diamond_price_list_customer == "Size (in mm)":
			_apply_standard_diamond_size_mm(doc, diamond, diamond_price_list_customer)

		if row.diamond_quality:
			diamond.quality = row.diamond_quality

	# Gemstone pricing
	for gem in doc.gemstone_detail:
		gem.rate = doc.gold_rate_with_gst
		if gemstone_price_list and any(
			dpl["price_list_type"] == gemstone_price_list_customer
			for dpl in gemstone_price_list
		):
			if gemstone_price_list_customer == "Multiplier":
				_apply_gemstone_multiplier(
					doc, gem, row, doc.customer, gemstone_price_list_customer
				)

		if row.custom_customer_stone == "Yes":
			gem.is_customer_item = 1

	# Other detail
	for other in doc.other_detail:
		if row.custom_customer_good == "Yes":
			other.is_customer_item = 1


# --- Diamond pricing helpers ---


def _apply_kg_gk_diamond_size_mm(doc, diamond, diamond_price_list_customer, customer):
	entries = frappe.db.sql(
		"""
		SELECT name, supplier_fg_purchase_rate, rate,
			custom_outright_handling_charges_rate, custom_outright_handling_charges_in_percentage,
			custom_outwork_handling_charges_rate, custom_outwork_handling_charges_in_percentage
		FROM `tabDiamond Price List`
		WHERE customer = %s AND price_list_type = %s AND size_in_mm = %s
		ORDER BY creation DESC LIMIT 1
	""",
		(doc.customer, diamond_price_list_customer, diamond.size_in_mm),
		as_dict=True,
	)
	if entries:
		entry = entries[0]
		diamond.fg_purchase_rate = entry.get("supplier_fg_purchase_rate", 0)
		diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity
		_apply_handling_charges(diamond, entry, diamond.size_in_mm)


def _apply_kg_gk_diamond_sieve(doc, diamond, price_list_type, customer):
	entries = frappe.db.sql(
		"""
		SELECT name, supplier_fg_purchase_rate, rate,
			custom_outright_handling_charges_rate, custom_outright_handling_charges_in_percentage,
			custom_outwork_handling_charges_rate, custom_outwork_handling_charges_in_percentage
		FROM `tabDiamond Price List`
		WHERE customer = %s AND price_list_type = %s AND sieve_size_range = %s
		ORDER BY creation DESC LIMIT 1
	""",
		(customer, price_list_type, diamond.sieve_size_range),
		as_dict=True,
	)
	if entries:
		entry = entries[0]
		diamond.total_diamond_rate = entry.get("rate", 0)
		diamond.fg_purchase_rate = entry.get("supplier_fg_purchase_rate", 0)
		diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity


def _apply_kg_gk_diamond_weight(doc, diamond, price_list_type, customer):
	entries = frappe.db.sql(
		"""
		SELECT name, from_weight, to_weight, supplier_fg_purchase_rate, rate,
			custom_outright_handling_charges_rate, custom_outright_handling_charges_in_percentage,
			custom_outwork_handling_charges_rate, custom_outwork_handling_charges_in_percentage
		FROM `tabDiamond Price List`
		WHERE customer = %s AND price_list_type = %s AND %s BETWEEN from_weight AND to_weight
		ORDER BY creation DESC LIMIT 1
	""",
		(customer, price_list_type, diamond.weight_per_pcs),
		as_dict=True,
	)
	if entries:
		entry = entries[0]
		diamond.fg_purchase_rate = entry.get("supplier_fg_purchase_rate", 0)
		diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity
		_apply_handling_charges(diamond, entry, diamond.weight_per_pcs)


def _apply_handling_charges(diamond, entry, multiplier):
	"""Apply outwork/outright handling charges to diamond based on customer item status."""
	if diamond.is_customer_item:
		diamond.total_diamond_rate = entry.get(
			"custom_outwork_handling_charges_rate", 0
		)
		diamond.diamond_rate_for_specified_quantity = (
			diamond.total_diamond_rate * multiplier
		)
		if entry.get("custom_outwork_handling_charges_rate") == 0:
			percentage = entry.get("custom_outwork_handling_charges_in_percentage", 0)
			amount = entry.get("rate", 0) * (percentage / 100)
			diamond.total_diamond_rate = amount
			diamond.diamond_rate_for_specified_quantity = (
				diamond.total_diamond_rate * multiplier
			)
	else:
		diamond.total_diamond_rate = entry.get("rate", 0) + entry.get(
			"custom_outright_handling_charges_rate", 0
		)
		diamond.diamond_rate_for_specified_quantity = (
			diamond.total_diamond_rate * multiplier
		)
		if entry.get("custom_outright_handling_charges_rate") == 0:
			percentage = entry.get("custom_outright_handling_charges_in_percentage", 0)
			rate = entry.get("rate", 0) * (percentage / 100)
			diamond.total_diamond_rate = rate + entry.get("rate", 0)
			diamond.diamond_rate_for_specified_quantity = (
				diamond.total_diamond_rate * multiplier
			)


def _apply_standard_diamond_pricing(doc, diamond, price_list_type):
	"""Weight-based diamond pricing for standard companies."""
	entries = frappe.db.sql(
		"""
		SELECT name, from_weight, to_weight, supplier_fg_purchase_rate, rate,
			custom_outright_handling_charges_rate, custom_outright_handling_charges_in_percentage,
			custom_outwork_handling_charges_rate, custom_outwork_handling_charges_in_percentage
		FROM `tabDiamond Price List`
		WHERE customer = %s AND price_list_type = %s AND %s BETWEEN from_weight AND to_weight
		ORDER BY creation DESC LIMIT 1
	""",
		(doc.customer, price_list_type, diamond.weight_per_pcs),
		as_dict=True,
	)
	if entries:
		entry = entries[0]
		diamond.fg_purchase_rate = entry.get("supplier_fg_purchase_rate", 0)
		diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity
		_apply_handling_charges(diamond, entry, diamond.weight_per_pcs)


def _apply_standard_diamond_sieve(doc, diamond, price_list_type):
	entries = frappe.db.sql(
		"""
		SELECT name, supplier_fg_purchase_rate, rate,
			custom_outright_handling_charges_rate, custom_outright_handling_charges_in_percentage,
			custom_outwork_handling_charges_rate, custom_outwork_handling_charges_in_percentage
		FROM `tabDiamond Price List`
		WHERE customer = %s AND price_list_type = %s AND sieve_size_range = %s
		ORDER BY creation DESC LIMIT 1
	""",
		(doc.customer, price_list_type, diamond.sieve_size_range),
		as_dict=True,
	)
	if entries:
		entry = entries[0]
		diamond.total_diamond_rate = entry.get("rate", 0)
		diamond.fg_purchase_rate = entry.get("supplier_fg_purchase_rate", 0)
		diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity


def _apply_standard_diamond_size_mm(doc, diamond, price_list_type):
	entries = frappe.db.sql(
		"""
		SELECT name, supplier_fg_purchase_rate, rate,
			custom_outright_handling_charges_rate, custom_outright_handling_charges_in_percentage,
			custom_outwork_handling_charges_rate, custom_outwork_handling_charges_in_percentage
		FROM `tabDiamond Price List`
		WHERE customer = %s AND price_list_type = %s AND size_in_mm = %s
		ORDER BY creation DESC LIMIT 1
	""",
		(doc.customer, price_list_type, diamond.size_in_mm),
		as_dict=True,
	)
	if entries:
		entry = entries[0]
		diamond.total_diamond_rate = entry.get("rate", 0)
		diamond.fg_purchase_rate = entry.get("supplier_fg_purchase_rate", 0)
		diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity
		_apply_handling_charges(diamond, entry, diamond.size_in_mm)


# --- Making charge helpers ---


def _apply_making_charge_metal(self, doc, metal, customer):
	"""Apply making charge pricing for metal detail rows."""
	rate_per_gm = 0
	fg_purchase_rate = 0
	fg_purchase_amount = 0
	wastage_rate = 0

	if self.custom_customer_gold == "Yes":
		metal.is_customer_item = 1

	making_charge_price_list = frappe.get_all(
		"Making Charge Price",
		filters={"customer": customer, "setting_type": doc.setting_type},
		fields=["name"],
	)
	making_charge_price_list_with_gold_rate = frappe.get_all(
		"Making Charge Price",
		filters={
			"customer": customer,
			"setting_type": doc.setting_type,
			"from_gold_rate": ["<=", doc.gold_rate_with_gst],
			"to_gold_rate": [">=", doc.gold_rate_with_gst],
		},
		fields=["name"],
	)

	if making_charge_price_list:
		subcategories = frappe.get_all(
			"Making Charge Price Item Subcategory",
			filters={"parent": making_charge_price_list[0]["name"]},
			fields=[
				"subcategory",
				"rate_per_gm",
				"supplier_fg_purchase_rate",
				"wastage",
				"custom_subcontracting_rate",
				"custom_subcontracting_wastage",
			],
		)
		if subcategories:
			match = next(
				(
					r
					for r in subcategories
					if r.get("subcategory") == doc.item_subcategory
				),
				None,
			)
			if match:
				rate_per_gm = match.get("rate_per_gm", 0)
				fg_purchase_rate = match.get("supplier_fg_purchase_rate", 0)
				fg_purchase_amount = fg_purchase_rate * metal.quantity
				if metal.is_customer_item:
					metal.rate = match.get("custom_subcontracting_rate", 0)
					wastage_rate = match.get("custom_subcontracting_wastage", 0)
					fg_purchase_rate = 0
					fg_purchase_amount = 0
					rate_per_gm = 0
				else:
					metal.rate = doc.gold_rate_with_gst
					wastage_rate = match.get("wastage", 0) / 100

	metal.wastage_rate = wastage_rate
	metal.amount = metal.rate * metal.quantity
	metal.wastage_amount = metal.wastage_rate * metal.amount
	metal.fg_purchase_rate = fg_purchase_rate
	metal.fg_purchase_amount = fg_purchase_amount
	metal.making_rate = rate_per_gm
	metal.making_amount = metal.making_rate * metal.quantity


def _apply_making_charge_finding(self, doc, find, customer):
	"""Apply making charge pricing for finding detail rows."""
	rate_per_gm = 0
	fg_purchase_rate = 0
	fg_purchase_amount = 0
	wastage_rate = 0

	if self.custom_customer_gold == "Yes":
		find.is_customer_item = 1

	making_charge_price_list = frappe.get_all(
		"Making Charge Price",
		filters={"customer": customer, "setting_type": doc.setting_type},
		fields=["name"],
	)
	making_charge_price_list_with_gold_rate = frappe.get_all(
		"Making Charge Price",
		filters={
			"customer": customer,
			"setting_type": doc.setting_type,
			"from_gold_rate": ["<=", doc.gold_rate_with_gst],
			"to_gold_rate": [">=", doc.gold_rate_with_gst],
		},
		fields=["name"],
	)

	matching_subcategory = None
	if making_charge_price_list:
		price_list_name = making_charge_price_list[0]["name"]
		subcategories = frappe.get_all(
			"Making Charge Price Item Subcategory",
			filters={"parent": price_list_name},
			fields=[
				"subcategory",
				"rate_per_gm",
				"supplier_fg_purchase_rate",
				"wastage",
				"custom_subcontracting_rate",
				"custom_subcontracting_wastage",
			],
		)
		if subcategories:
			matching_subcategory = next(
				(
					r
					for r in subcategories
					if r.get("subcategory") == doc.item_subcategory
				),
				None,
			)
		if matching_subcategory:
			rate_per_gm = matching_subcategory.get("rate_per_gm", 0)
			fg_purchase_rate = matching_subcategory.get("supplier_fg_purchase_rate", 0)
			fg_purchase_amount = fg_purchase_rate * find.quantity
			if find.is_customer_item:
				find.rate = matching_subcategory.get("custom_subcontracting_rate", 0)
				wastage_rate = matching_subcategory.get(
					"custom_subcontracting_wastage", 0
				)
				fg_purchase_rate = 0
				fg_purchase_amount = 0
				rate_per_gm = 0
			else:
				find.rate = doc.gold_rate_with_gst
				wastage_rate = matching_subcategory.get("wastage", 0) / 100

	find.wastage_rate = wastage_rate
	find.amount = find.rate * find.quantity
	find.making_rate = rate_per_gm
	if making_charge_price_list_with_gold_rate:
		find.making_amount = find.making_rate * find.quantity
	find.fg_purchase_rate = fg_purchase_rate
	find.fg_purchase_amount = fg_purchase_amount
	find.wastage_amount = find.wastage_rate * find.amount


# --- Gemstone pricing helper ---


def _apply_gemstone_multiplier(doc, gem, row, customer, price_list_type):
	"""Apply gemstone multiplier pricing."""
	query = frappe.db.sql(
		"""
		SELECT gpl.name, gpl.cut_or_cab, gpl.gemstone_grade,
			gm.item_category, gm.precious, gm.semi_precious, gm.synthetic,
			sfm.precious AS supplier_precious, sfm.semi_precious AS supplier_semi_precious,
			sfm.synthetic AS supplier_synthetic
		FROM `tabGemstone Price List` gpl
		INNER JOIN `tabGemstone Multiplier` gm
			ON gm.parent = gpl.name AND gm.item_category = %s AND gm.parentfield = 'gemstone_multiplier'
		LEFT JOIN `tabGemstone Multiplier` sfm
			ON sfm.parent = gpl.name AND sfm.item_category = %s AND sfm.parentfield = 'supplier_fg_multiplier'
		WHERE gpl.customer = %s AND gpl.price_list_type = %s
		AND gpl.cut_or_cab = %s AND gpl.gemstone_grade = %s
		ORDER BY gpl.creation DESC LIMIT 1
	""",
		(
			doc.item_category,
			doc.item_category,
			customer,
			price_list_type,
			gem.cut_or_cab,
			gem.gemstone_grade,
		),
		as_dict=True,
	)

	if query:
		entry = query[0]
		gemstone_quality = gem.gemstone_quality or row.get("gemstone_quality")
		gemstone_pr = gem.gemstone_pr

		multiplier_value = (
			entry.get("precious")
			if gemstone_quality == "Precious"
			else entry.get("semi_precious")
			if gemstone_quality == "Semi Precious"
			else entry.get("synthetic")
		)
		supplier_value = (
			entry.get("supplier_precious")
			if gemstone_quality == "Precious"
			else entry.get("supplier_semi_precious")
			if gemstone_quality == "Semi Precious"
			else entry.get("supplier_synthetic")
		)

		if multiplier_value is not None:
			gem.total_gemstone_rate = multiplier_value
			gem.gemstone_rate_for_specified_quantity = (
				gem.total_gemstone_rate * gemstone_pr
			)
		if supplier_value is not None:
			gem.fg_purchase_rate = supplier_value
			gem.fg_purchase_amount = gem.fg_purchase_rate * gemstone_pr
