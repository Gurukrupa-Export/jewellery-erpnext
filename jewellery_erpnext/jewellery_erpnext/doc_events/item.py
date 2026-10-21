import datetime
import json

import frappe
from frappe import _
from frappe.utils import flt


def before_validate(self, method):
	add_item_attributes(self)


def validate(self, method):
	system_item_restriction(self)
	update_item_uom_conversion(self)
	set_attribute_and_value_in_description(self)
	validate_not_allowed_attribute_values(self)


def before_save(self, method):
	pass
	# update_item_uom_conversion(self)


def on_trash(self, method):
	if frappe.session.user != "Administrator" and self.is_system_item:
		frappe.throw(_("Can not delete the system item."))


def system_item_restriction(self):
	items = frappe.get_all(
		"Jewellery System Item", {"parent": "Jewellery Settings"}, "item_code"
	)
	item_list = [row.get("item_code") for row in items]
	if (
		not self.is_new()
		and frappe.session.user != "Administrator"
		and self.is_system_item
		and (self.item_code in item_list or self.variant_of in item_list)
	):
		frappe.throw(
			_(
				"You can not edit system item. Please contact administrator to edit the item."
			)
		)
	if self.item_code in item_list and not self.is_system_item:
		self.is_system_item = 1


def add_item_attributes(self):
	if self.has_variants and self.subcategory and not self.attributes:
		# change in this quary
		item_attributes = frappe.get_all(
			"Attribute Value Item Attribute Detail",
			{"parent": self.subcategory, "in_item_variant": 1},
			"item_attribute",
			order_by="idx asc",
		)

		if item_attributes:
			self.attributes = []
			for row in item_attributes:
				self.append(
					"attributes",
					{
						"attribute": row.item_attribute,
						"numeric_values": frappe.db.get_value(
							"Item Attribute", row.item_attribute, "numeric_values"
						),
						"from_range": frappe.db.get_value(
							"Item Attribute", row.item_attribute, "from_range"
						),
						"to_range": frappe.db.get_value(
							"Item Attribute", row.item_attribute, "to_range"
						),
						"increment": frappe.db.get_value(
							"Item Attribute", row.item_attribute, "increment"
						),
					},
				)


def update_item_uom_conversion(self):
	if self.attributes:
		attribute_list = [row.attribute for row in self.attributes]
		weight = set_diamond_attribute_weight(self, attribute_list)
		if not weight:
			weight = set_gemstone_attribute_weight(self, attribute_list)
		if weight:
			to_remove = [d for d in self.uoms if d.uom == "Pcs"]
			for d in to_remove:
				self.remove(d)
			self.append("uoms", {"uom": "Pcs", "conversion_factor": weight})


def set_diamond_attribute_weight(self, attribute_list):
	diamond_attribute_list = ["Diamond Type", "Stone Shape", "Diamond Sieve Size"]
	weight = 0
	if set(diamond_attribute_list).issubset(set(attribute_list)):
		attribute_filters = {}
		for row in self.attributes:
			if row.attribute == "Diamond Type":
				attribute_filters.update(
					{row.attribute.replace(" ", "_").lower(): row.attribute_value}
				)
			if row.attribute == "Stone Shape":
				attribute_filters.update(
					{row.attribute.replace(" ", "_").lower(): row.attribute_value}
				)
			if row.attribute == "Diamond Sieve Size":
				attribute_filters.update(
					{row.attribute.replace(" ", "_").lower(): row.attribute_value}
				)
		if frappe.db.exists("Diamond Weight", attribute_filters):
			weight = frappe.db.get_value("Diamond Weight", attribute_filters, "weight")
	return weight or 0


def set_gemstone_attribute_weight(self, attribute_list):
	gemstone_attribute_list = [
		"Gemstone Type",
		"Stone Shape",
		"Gemstone Grade",
		"Gemstone Size",
	]
	weight = 0
	if set(gemstone_attribute_list).issubset(set(attribute_list)):
		attribute_filters = {}
		for row in self.attributes:
			if row.attribute == "Gemstone Type":
				attribute_filters.update(
					{row.attribute.replace(" ", "_").lower(): row.attribute_value}
				)
			if row.attribute == "Stone Shape":
				attribute_filters.update(
					{row.attribute.replace(" ", "_").lower(): row.attribute_value}
				)
			if row.attribute == "Gemstone Grade":
				attribute_filters.update(
					{row.attribute.replace(" ", "_").lower(): row.attribute_value}
				)
			if row.attribute == "Gemstone Size":
				attribute_filters.update(
					{row.attribute.replace(" ", "_").lower(): row.attribute_value}
				)
		if frappe.db.exists("Gemstone Weight", attribute_filters):
			weight = frappe.db.get_value("Gemstone Weight", attribute_filters, "weight")
	return weight


# Not Allowed rules live on Gemstone Type Attribute Values, so only the value
# selected under this attribute can block others. The same text may exist under
# other Item Attributes (e.g. a Collection named "Coral") and must not trigger
# the Gemstone Type's rules.
NOT_ALLOWED_SOURCE_ATTRIBUTE = "Gemstone Type"


def get_not_allowed_attribute_values(selected):
	"""Values blocked by the Not Allowed table of the selected Gemstone Type.

	selected is {item_attribute: attribute_value}. Returns
	{item_attribute: {blocked_value: gemstone_type}}, e.g.
	{"Gemstone Type": "Coral"} -> {"Cut or Cab": {"Faceted": "Coral"}}.
	"""
	gemstone_type = selected.get(NOT_ALLOWED_SOURCE_ATTRIBUTE)
	if not gemstone_type:
		return {}

	rows = frappe.get_all(
		"Attribute Value Not Allowed Detail",
		filters={
			"parenttype": "Attribute Value",
			"parentfield": "not_allowed_attribute_values",
			"parent": gemstone_type,
		},
		fields=["parent", "item_attribute", "attribute_value"],
	)
	blocked = {}
	for row in rows:
		blocked.setdefault(row.item_attribute, {})[row.attribute_value] = row.parent
	return blocked


@frappe.whitelist()
def get_allowed_attribute_values(item_attribute, txt="", selected=None):
	"""Autocomplete source for an attribute field of the Create Variant dialog.

	Same list as ERPNext's get_item_attribute, minus the values blocked by
	the Gemstone Type already selected in the dialog.
	"""
	if not frappe.has_permission("Item"):
		frappe.throw(_("No Permission"))

	selected = frappe.parse_json(selected) or {}
	values = frappe.get_all(
		"Item Attribute Value",
		filters={
			"parent": item_attribute,
			"attribute_value": ("like", f"%{txt or ''}%"),
		},
		pluck="attribute_value",
		order_by="idx",
	)
	other_selected = {
		attribute: value
		for attribute, value in selected.items()
		if attribute != item_attribute
	}
	blocked = get_not_allowed_attribute_values(other_selected).get(item_attribute, {})
	return [value for value in values if value not in blocked]


@frappe.whitelist()
def get_blocked_attributes(selected=None):
	"""Attributes whose selected value is blocked by the selected Gemstone Type."""
	selected = frappe.parse_json(selected) or {}
	blocked = get_not_allowed_attribute_values(selected)
	return [
		attribute
		for attribute, value in selected.items()
		if value in blocked.get(attribute, {})
	]


def validate_not_allowed_attribute_values(self):
	"""Block a variant whose attribute values break its Gemstone Type's Not Allowed table.

	Only checked when the variant is new or its attributes changed, so items
	created before the master was configured still save.
	"""
	if not self.get("variant_of"):
		return

	attributes = {
		row.attribute: row.attribute_value
		for row in self.get("attributes") or []
		if row.attribute_value
	}
	if not attributes:
		return

	before = self.get_doc_before_save()
	if before:
		old = {
			row.attribute: row.attribute_value
			for row in before.get("attributes") or []
			if row.attribute_value
		}
		if old == attributes:
			return

	throw_if_not_allowed(attributes)


@frappe.whitelist()
def get_variant(
	template, args=None, variant=None, manufacturer=None, manufacturer_part_no=None
):
	"""Override of ERPNext's whitelisted get_variant, called first by Create Variant.

	An existing variant is always returned, even one created before its
	combination was marked Not Allowed. Only when no variant exists -- i.e.
	Create would make a new one -- are blocked values rejected, so the message
	shows while the dialog is still open.
	"""
	from erpnext.controllers.item_variant import get_variant as erpnext_get_variant

	existing = erpnext_get_variant(
		template, args, variant, manufacturer, manufacturer_part_no
	)
	if existing:
		return existing

	attributes = {
		attribute: value
		for attribute, value in (frappe.parse_json(args) or {}).items()
		if value and isinstance(value, str)
	}
	throw_if_not_allowed(attributes)
	return existing


def throw_if_not_allowed(attributes):
	"""Throw if any {attribute: value} is blocked by the selected Gemstone Type."""
	if not attributes:
		return

	blocked = get_not_allowed_attribute_values(attributes)
	for attribute, value in attributes.items():
		blocked_by = blocked.get(attribute, {}).get(value)
		if not blocked_by:
			continue
		frappe.throw(not_allowed_message(attribute, value, blocked_by))


def not_allowed_message(attribute, value, blocked_by):
	"""e.g. "Cut or Cab Faceted is not allowed for Gemstone Type Coral"."""
	return _("{0} {1} is not allowed for {2} {3}").format(
		attribute,
		frappe.bold(value),
		NOT_ALLOWED_SOURCE_ATTRIBUTE,
		frappe.bold(blocked_by),
	)


def set_attribute_and_value_in_description(self):
	if self.variant_of:
		description_value = "<b><u>" + self.variant_of + "</u></b><br/>"
		for d in self.get("attributes"):
			description_value += (
				str(d.attribute) + " : " + str(d.attribute_value) + "<br/>"
			)
		self.description = description_value


@frappe.whitelist()
def calculate_item_wt_details(doc, bom=None, item=None):
	if isinstance(doc, str):
		doc = json.loads(doc)
	settings = frappe.get_doc("Jewellery Settings")
	doc["cad_to_rpt_ratio"] = settings.cad_to_rpt
	doc["estimated_rpt_wt"] = flt(doc["cad_weight"]) / flt(settings.cad_to_rpt)
	doc["rpt_to_wax_ratio"] = settings.rpt_to_wax
	doc["estimated_wax_wt"] = flt(doc["estimated_rpt_wt"]) / flt(settings.rpt_to_wax)
	doc["wax_to_10kt_gold_ratio"] = settings.wax_to_gold_10
	doc["wax_to_14kt_gold_ratio"] = settings.wax_to_gold_14
	doc["wax_to_18kt_gold_ratio"] = settings.wax_to_gold_18
	doc["wax_to_22kt_gold_ratio"] = settings.wax_to_gold_22
	doc["wax_to_silver_ratio"] = settings.wax_to_silver
	doc["estimated_10kt_gold_wt"] = flt(doc["estimated_wax_wt"]) * flt(
		doc["wax_to_10kt_gold_ratio"]
	)
	doc["estimated_14kt_gold_wt"] = flt(doc["estimated_wax_wt"]) * flt(
		doc["wax_to_14kt_gold_ratio"]
	)
	doc["estimated_18kt_gold_wt"] = flt(doc["estimated_wax_wt"]) * flt(
		doc["wax_to_18kt_gold_ratio"]
	)
	doc["estimated_22kt_gold_wt"] = flt(doc["estimated_wax_wt"]) * flt(
		doc["wax_to_22kt_gold_ratio"]
	)
	doc["estimated_silver_wt"] = flt(doc["estimated_wax_wt"]) * flt(
		doc["wax_to_silver_ratio"]
	)
	if bom:
		doc["estimated_finding_gold_wt_bom"] = frappe.db.get_value(
			"BOM", bom, "finding_weight"
		)
	else:
		BOM = frappe.qb.DocType("BOM")
		query = (
			frappe.qb.from_(BOM)
			.select(BOM.finding_weight)
			.where(BOM.item == item)
			.limit(1)
		)
		finding_weight = query.run(as_dict=True)

		if finding_weight:
			doc["estimated_finding_gold_wt_bom"] = finding_weight[0].get(
				"finding_weight"
			)
	return doc


def before_insert(self, method):
	consumables_list = []
	iav = frappe.qb.DocType("Item Attribute Value")

	for i in (
		frappe.qb.from_(iav)
		.select(iav.attribute_value)
		.where(iav.parent == "Consumables")
		.run()
	):
		consumables_list.append(i[0])
	year_code = get_year_code()
	month_code = get_month_code()
	week_code = get_week_code()
	if self.item_group in [
		"Metal - V",
		"Diamond - V",
		"Gemstone - V",
		"Finding - V",
		"Other - V",
	]:
		year_code = get_year_code()
		month_code = get_month_code()
		week_code = get_week_code()
		if self.item_group == "Diamond - V":
			batch_number = "GE{year_code}{month_code}{week_code}-D".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif self.item_group == "Metal - V":
			batch_number = "GE{year_code}{month_code}{week_code}-M".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif self.item_group == "Gemstone - V":
			batch_number = "GE{year_code}{month_code}{week_code}-G".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif self.item_group == "Finding - V":
			batch_number = "GE{year_code}{month_code}{week_code}-F".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif self.item_group == "Other - V":
			batch_number = "GE{year_code}{month_code}{week_code}-O".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		batch_abbr_code_list = []
		for i in self.attributes:
			if i.attribute == "Finding Category":
				continue
			batch_abbreviation = frappe.db.get_value(
				"Attribute Value", i.attribute_value, "custom_batch_abbreviation"
			)
			if i.attribute_value:
				if batch_abbreviation:
					batch_abbr_code_list.append(batch_abbreviation)
				else:
					frappe.throw(
						_("Abbrivation is missing for {0}").format(i.attribute_value)
					)
		batch_code = batch_number + "".join(batch_abbr_code_list) + "-.##."
		self.batch_number_series = batch_code
		self.has_batch_no = 1
		self.create_new_batch = 1
		self.is_stock_item = 1
		self.include_item_in_manufacturing = 1
	elif " - V" in self.item_group and self.variant_of:
		validate_attribute_value(self)
	elif self.item_group in consumables_list and self.variant_of:
		for i in self.attributes:
			if not frappe.db.get_value(
				"Attribute Value", i.attribute_value, "custom_batch_or_serial_no"
			):
				frappe.throw(
					_(
						"Select one options for <b>{attribute_value}</b> in Attribute Value"
					).format(attribute_value=i.attribute_value)
				)
			if (
				frappe.db.get_value(
					"Attribute Value", i.attribute_value, "custom_batch_or_serial_no"
				)
				== "Batch"
			):
				batch_number = "GE{year_code}{month_code}{week_code}-CO".format(
					year_code=year_code, month_code=month_code, week_code=week_code
				)
				# group_abbr = frappe.db.sql("""select abbr  from `tabItem Attribute Value` where attribute_value = {item_group}""".format(item_group=self.item_group),as_dict=1)
				group_abbr = (
					frappe.qb.from_(iav)
					.select(iav.abbr)
					.where(iav.attribute_value == self.item_group)
					.run()
				)
				if not group_abbr:
					frappe.throw(
						_(
							"Abbr is not available for <b>{item_group}</b> in Item Attribute Consumnables"
						).format(item_group=self.item_group)
					)
				# batch_abbr_code = frappe.db.get_value(
				# 	"Attribute Value", i.attribute_value, "custom_batch_abbreviation"
				# )
				# if not batch_abbr_code:
				# 	frappe.throw(("Abbrivation is missing for {0}".format(i.attribute_value)))
				# batch_code = batch_number + group_abbr[0][0] + batch_abbr_code + "-.##."

				total_variant = len(
					frappe.db.get_list(
						"Item", {"item_group": self.item_group, "has_batch_no": 1}
					)
				)

				if total_variant == 0:
					sequence = 1
					sequence = group_abbr[0][0] + f"{sequence:02}"
				else:
					sequence = total_variant + 1
					sequence = group_abbr[0][0] + f"{sequence:02}"

				batch_code = batch_number + group_abbr[0][0] + sequence + "-.##."

				self.batch_number_series = batch_code
				self.has_batch_no = 1
				self.create_new_batch = 1
				self.is_stock_item = 1
				self.include_item_in_manufacturing = 1
			elif (
				frappe.db.get_value(
					"Attribute Value", i.attribute_value, "custom_batch_or_serial_no"
				)
				== "Serial No"
			):
				self.has_serial_no = 1
				self.is_stock_item = 1
				self.include_item_in_manufacturing = 1
				group_abbr = (
					frappe.qb.from_(iav)
					.select(iav.abbr)
					.where(iav.attribute_value == self.item_group)
					.run()
				)
				if not group_abbr:
					frappe.throw(
						_(
							"Abbr is not available for <b>{item_group}</b> in Item Attribute Consumnables"
						).format(item_group=self.item_group)
					)
				total_variant = len(
					frappe.db.get_list(
						"Item", {"item_group": self.item_group, "has_serial_no": 1}
					)
				)
				if total_variant == 0:
					sequence = 1
					self.serial_no_series = group_abbr[0][0] + f"{sequence:05}"
				else:
					sequence = total_variant + 1
					self.serial_no_series = group_abbr[0][0] + f"{sequence:05}"


def get_year_code():
	year_dict = {
		"1": "A",
		"2": "B",
		"3": "C",
		"4": "D",
		"5": "E",
		"6": "F",
		"7": "G",
		"8": "H",
		"9": "I",
		"0": "J",
	}
	current_year = datetime.datetime.now().year
	last_two_digits = current_year % 100
	return str(last_two_digits)[0] + year_dict[str(last_two_digits)[1]]


def get_week_code():
	current_date = datetime.date.today()
	week_number = (current_date.day - 1) // 7 + 1
	return str(week_number)


def get_month_code():
	current_date = datetime.datetime.now()
	month_two_digit = current_date.strftime("%m")
	return str(month_two_digit)


def validate_attribute_value(self):
	for i in self.attributes:
		# if i.attribute == 'Chain Type' and i.attribute_value == 'No':
		# 	chain_type = 1

		# valid_with_zero_value = ['Chain Thickness','Chain Length','Distance Between Kadi To Mugappu','Space between Mugappu','Back Side Size','Number of Ant','Count of Spiral Turns']

		allow_with_zero = frappe.db.get_value(
			"Attribute Value Item Attribute Detail",
			{"parent": self.item_subcategory, "item_attribute": i.attribute},
			"allow_zero_values",
		)

		# if chain_type == 1 and allow_with_zero == 1:
		if allow_with_zero == 1:
			continue

		if not i.attribute_value:
			frappe.throw(
				f"Value is not available for attribute value: <b>{i.attribute}</b>"
			)
