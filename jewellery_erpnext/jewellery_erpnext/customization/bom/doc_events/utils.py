import frappe


def product_ratio(self):
	gold_wt = 0.0
	for gold in self.metal_detail:
		if gold.metal_type == "Gold":
			gold_wt += gold.quantity
	self.metal_to_diamond_ratio_excl_of_finding = gold_wt


# Specification fields that mirror the first metal row: header field, then the row field.
HEADER_FROM_FIRST_METAL_ROW = (
	("metal_touch", "metal_touch"),
	("metal_purity", "metal_purity"),
)


def follow_first_metal_row(self):
	"""Keep the Specification tab's Metal Touch and Metal Purity in step with the first metal row.

	`update_specifications` fills them only while blank, so a later change to the row never
	reached the header - and KGGK sync sends the header as it is. The header always takes the
	row's value: keeping a "deliberately different" header cannot work while the BOM Client
	Scripts reset it from the header Metal Touch on every refresh, which made a hand-set value
	and a script-set one look the same. A row cleared to nothing never clears the header.
	"""
	row = _first_metal_row(self)
	if not row:
		return

	for header, field in HEADER_FROM_FIRST_METAL_ROW:
		value = row.get(field)
		if value and self.get(header) != value:
			self.set(header, value)


def _first_metal_row(doc):
	"""The metal row the header copies from: first by idx, as `update_specifications` reads it."""
	rows = doc.get("metal_detail") or []
	return min(rows, key=lambda row: row.get("idx") or 0) if rows else None


def update_specifications(self):
	if not self.metal_type_:
		self.metal_type_ = (
			self.metal_detail[0].metal_type if self.metal_detail else None
		)

	if not self.metal_touch:
		self.metal_touch = (
			self.metal_detail[0].metal_touch if self.metal_detail else None
		)

	if not self.metal_colour:
		self.metal_colour = (
			self.metal_detail[0].metal_colour if self.metal_detail else None
		)

	if not self.metal_purity:
		self.metal_purity = (
			self.metal_detail[0].metal_purity if self.metal_detail else None
		)

	if not self.sub_setting_type1:
		self.sub_setting_type1 = (
			self.diamond_detail[0].sub_setting_type if self.diamond_detail else None
		)

	if not self.diamond_quality:
		self.diamond_quality = (
			self.diamond_detail[0].quality if self.diamond_detail else None
		)

	# for idx, row in enumerate(self.gemstone_detail):
	# 	if idx == 0:
	# 		if not self.gemstone_type:
	# 			self.gemstone_type = row.gemstone_type
	# 		if not self.gemstone_quality:
	# 			self.gemstone_quality = row.gemstone_quality
	# 	elif not self.get(f"gemstone_type{idx}") and idx >= 1:
	# 		variable = f"gemstone_type{idx}"
	# 		self.db_set(variable, row.gemstone_type)

	for idx, row in enumerate(self.other_detail):
		if idx >= 2:
			continue
		if not self.get(f"custom_other_item_{idx + 1}"):
			self.db_set(f"custom_other_item_{idx + 1}", row.item_code)
		if not self.get(f"custom_other_wt_{idx + 1}"):
			self.db_set(f"custom_other_wt_{idx + 1}", row.quantity)
