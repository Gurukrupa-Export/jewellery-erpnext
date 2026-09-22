# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

import re

import frappe
from erpnext.controllers.queries import get_batch_no
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.diamond_conversion_batches import (
	get_diamond_conversion_target_batches,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.utils import (
	update_batch_details,
)

# The conversion type both new validations are gated on. Renamed from "Sieve Size to Sieve Size
# Range" by patches/rename_diamond_conversion_sieve_type.py.
SIEVE_TO_SIEVE = "Sieve Size to Sieve Size"

# Source AND target are read through the SAME item attribute for SIEVE_TO_SIEVE. The rename was
# to the Diamond Conversion option label only, not to any item attribute.
SIEVE_RANGE_ATTRIBUTE = "Diamond Sieve Size Range"

# "+13-17.5" -> ("13", "17.5"). Anchored and fully specified so a malformed value (no "+", a
# third segment, a stray letter) fails to match instead of being silently mangled the way
# revise_diamond_price_list.py's ``value[1:].split("-")`` is.
SIEVE_BOUNDS_PATTERN = re.compile(r"^\+(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$")


class DiamondConversion(Document):
	def before_validate(self):
		# Both flags are opt-in and read only by get_fifo_batches. exclude_... makes FIFO skip
		# Diamond-Conversion output; throw_batch_error turns the resulting shortfall from a
		# msgprint into a throw, which is what stops update_batch_details either leaving a
		# batch-less row behind (it only rebuilds the table ``if rows_to_append``) or silently
		# dropping a starved row when some other row did allocate. Either case would otherwise
		# reach make_diamond_stock_entry with batch_no=None and let the Stock Entry auto-select
		# the very batch this conversion type bans.
		sieve_to_sieve = self.conversion_type == SIEVE_TO_SIEVE
		self.flags.exclude_diamond_conversion_target_batches = sieve_to_sieve
		self.flags.throw_batch_error = sieve_to_sieve
		update_batch_details(self)

	def on_submit(self):
		make_diamond_stock_entry(self)

	def validate(self):
		to_check_valid_qty_in_table(self)
		validate_target_item(self)
		validate_sieve_size_band(self)
		validate_source_batches(self)
		validate_purity(self)

	@frappe.whitelist()
	def get_detail_tab_value(self):
		errors = []
		dpt, branch = frappe.get_value(
			"Employee", self.employee, ["department", "branch"]
		)
		if not dpt:
			errors.append(
				f"Department Messing against <b>{self.employee} Employee Master</b>"
			)
		if not branch:
			errors.append(
				f"Branch Messing against <b>{self.employee} Employee Master</b>"
			)
		mnf = frappe.get_value("Department", dpt, "manufacturer")
		if not mnf:
			errors.append("Manufacturer Messing against <b>Department Master</b>")
		s_wh = frappe.get_value("Warehouse", {"disabled": 0, "department": dpt}, "name")
		if not mnf:
			errors.append("Warehouse Missing Warehouse Master Department Not Set")
		if errors:
			frappe.throw("<br>".join(errors))
		if dpt and mnf and s_wh:
			self.department = dpt
			self.branch = branch
			self.manufacturer = mnf
			self.source_warehouse = s_wh
			self.target_warehouse = s_wh

	@frappe.whitelist()
	def get_batch_detail(self):
		bal_qty = ""
		supplier = ""
		customer = ""
		inventory_type = ""

		error = []
		for row in self.sc_source_table:
			bal_qty = get_batch_qty(batch_no=row.batch, warehouse=self.source_warehouse)
			reference_doctype = None
			if row.batch:
				reference_doctype, reference_name = frappe.get_value(
					"Batch", row.batch, ["reference_doctype", "reference_name"]
				)
			if not bal_qty:
				error.append("Batch Qty zero")
			if reference_doctype:
				if reference_doctype == "Purchase Receipt":
					supplier = frappe.get_value(
						reference_doctype, reference_name, "supplier"
					)
					inventory_type = "Regular Stock"
				if reference_doctype == "Stock Entry":
					inventory_type = frappe.get_value(
						reference_doctype, reference_name, "inventory_type"
					)
					if inventory_type == "Customer Goods":
						customer = frappe.get_value(
							reference_doctype, reference_name, "_customer"
						)
			if error:
				frappe.throw(", ".join(error))
		return (
			bal_qty or None,
			supplier or None,
			customer or None,
			inventory_type or None,
		)


def to_check_valid_qty_in_table(self):
	for row in self.sc_source_table:
		if row.qty <= 0:
			frappe.throw(_("Source Table Qty not allowed Nigative or Zero Value"))
	for row in self.sc_target_table:
		if row.qty <= 0:
			frappe.throw(_("Target Table Qty not allowed Nigative or Zero Value"))
	if not self.sc_source_table:
		frappe.throw(_("Source table is empty. Please add rows."))
	if not self.sc_target_table:
		frappe.throw(_("Target table is empty. Please add rows."))


def validate_target_item(self):
	"""Sieve membership check for "Sieve Size Range to Sieve Size" only.

	The sibling "Sieve Size to Sieve Size Range" branch that used to live here was replaced by
	``validate_sieve_size_band`` when that option was renamed to "Sieve Size to Sieve Size":
	the new rule reads ``Diamond Sieve Size Range`` on BOTH sides and compares numeric UP/DOWN
	bands rather than following the ``Attribute Value.sieve_size_range`` link.
	"""
	if self.conversion_type == "Sieve Size Range to Sieve Size":
		attribute_data = frappe._dict()
		sieve_size_range_value = []
		for row in self.sc_source_table:
			attr_value = frappe.db.get_value(
				"Item Variant Attribute",
				{"attribute": "Diamond Sieve Size Range", "parent": row.item_code},
				"attribute_value",
			)
			if attr_value:
				if not attribute_data.get(attr_value):
					attribute_data[attr_value] = frappe.db.get_all(
						"Attribute Value",
						{"sieve_size_range": attr_value},
						pluck="name",
					)
				sieve_size_range_value += attribute_data.get(attr_value)
		# sieve_size_range_value = []
		# for row in attr_value_list:

		for row in self.sc_target_table:
			attr_value = frappe.db.get_value(
				"Item Variant Attribute",
				{"attribute": "Diamond Sieve Size", "parent": row.item_code},
				"attribute_value",
			)
			if attr_value not in sieve_size_range_value:
				frappe.throw(
					_(
						"{0} attribute value not available in the sieve size range value"
					).format(row.item_code)
				)


def _sieve_key(attribute_value):
	"""Normalise an attribute value the way MariaDB's collation compares it.

	``utf8mb4_unicode_ci`` is case-insensitive and PAD SPACE, so ``"+6.5-8 "`` and ``"+6.5-8"``
	are the same string to the database. Same shim as ``doc_events/material_request.py``:
	``Item Variant Attribute.attribute_value`` is free-text Data with no FK to
	``Attribute Value.name``, so a bare dict lookup would miss where SQL matched -- and a miss
	here would skip the check entirely, failing open on a validation.
	"""
	return (attribute_value or "").strip().casefold()


def _get_dimension(dimension_map, attribute_value):
	"""Look up an Attribute Value row, exact match first, collation-equivalent second."""
	return dimension_map.get(attribute_value) or dimension_map.get(
		_sieve_key(attribute_value)
	)


def _parse_sieve_bounds(attribute_value):
	"""``"+13-17.5"`` -> ``(13.0, 17.5)``. ``None`` for anything that is not exactly ``+A-B``."""
	match = SIEVE_BOUNDS_PATTERN.match((attribute_value or "").strip())
	if not match:
		return None
	return flt(match.group(1), 3), flt(match.group(2), 3)


def _get_sieve_range_values(item_codes):
	"""``{item_code: attribute_value}`` for the ``Diamond Sieve Size Range`` attribute.

	One query for the whole document. ``setdefault`` is first-win, which matches the row a
	``frappe.db.get_value`` would have returned had a malformed duplicate attribute row existed.
	"""
	if not item_codes:
		return {}

	sieve_values = {}
	for row in frappe.db.get_all(
		"Item Variant Attribute",
		filters={
			"attribute": SIEVE_RANGE_ATTRIBUTE,
			"parent": ["in", sorted(item_codes)],
		},
		fields=["parent", "attribute_value"],
	):
		sieve_values.setdefault(row.parent, row.attribute_value)
	return sieve_values


def _get_sieve_dimensions(attribute_values):
	"""``Attribute Value`` rows keyed by both their exact name and the normalised one."""
	if not attribute_values:
		return {}

	dimension_map = {}
	for row in frappe.db.get_all(
		"Attribute Value",
		filters={"name": ["in", sorted(attribute_values)]},
		fields=["name", "height", "weight", "is_diamond_sieve_size_range"],
	):
		dimension_map[row.name] = row
		dimension_map.setdefault(_sieve_key(row.name), row)
	return dimension_map


def _resolve_sieve_band(row, label, sieve_values, dimension_map):
	"""``(attribute_value, down, up)`` for one row's item, throwing on anything unusable.

	``height`` is UP and ``weight`` is DOWN -- ``attribute_value.js`` relabels them that way for
	sieve values. Never skips: every unusable state is a throw that names what to fix.
	"""
	attr_value = sieve_values.get(row.item_code)
	if not attr_value:
		frappe.throw(
			_("Row #{0}: {1} item {2} has no {3} attribute.").format(
				row.idx,
				label,
				frappe.bold(row.item_code),
				frappe.bold(SIEVE_RANGE_ATTRIBUTE),
			)
		)

	dimension = _get_dimension(dimension_map, attr_value)
	if not dimension:
		# Distinct from the "fill UP/DOWN" message below: there is no record to open, so
		# telling the user to edit it would be an instruction they cannot follow.
		frappe.throw(
			_(
				"Row #{0}: {1} item {2} references Attribute Value {3}, which does not exist."
			).format(
				row.idx, label, frappe.bold(row.item_code), frappe.bold(attr_value)
			)
		)

	if not dimension.is_diamond_sieve_size_range:
		frappe.throw(
			_(
				"Row #{0}: Attribute Value {1} on {2} item {3} is not marked as a {4}."
			).format(
				row.idx,
				frappe.bold(attr_value),
				label,
				frappe.bold(row.item_code),
				frappe.bold(SIEVE_RANGE_ATTRIBUTE),
			)
		)

	down, up = flt(dimension.weight, 3), flt(dimension.height, 3)
	# "Unset" is BOTH endpoints at zero, not a falsy test on either. The columns are Float NOT
	# NULL DEFAULT 0, so None never occurs and a falsy test cannot tell "never filled in" from
	# "legitimately zero" -- which would make "+0-2", whose natural DOWN is 0, permanently
	# unusable.
	if not down and not up:
		frappe.throw(
			_(
				"Row #{0}: Attribute Value {1} has no UP/DOWN set, so the allowed sieve band "
				"cannot be determined. Set UP and DOWN on that Attribute Value first."
			).format(row.idx, frappe.bold(attr_value))
		)

	if up <= down:
		frappe.throw(
			_(
				"Row #{0}: Attribute Value {1} has UP {2} not greater than DOWN {3}."
			).format(
				row.idx, frappe.bold(attr_value), frappe.bold(up), frappe.bold(down)
			)
		)

	return attr_value, down, up


def validate_sieve_size_band(self):
	"""Every target's sieve must sit inside every source's UP/DOWN band.

	For source ``D-NT-RO-4-+14-16`` the ``Diamond Sieve Size Range`` attribute is ``+14-16``,
	whose Attribute Value carries DOWN 13 and UP 17.5 -- so a target must lie inside 13..17.5,
	both by its own ``+A-B`` bounds and by its own UP/DOWN.

	Intersection, not union, across distinct source items: ``make_diamond_stock_entry`` melts
	every source row into every target row in one Repack, so a target that fits only the wider
	band is still genuinely being produced from the narrow-band source. Sources are deduped by
	item first, because ``update_batch_details`` splits one item across many rows by batch.
	"""
	if self.conversion_type != SIEVE_TO_SIEVE:
		return

	source_rows = {}
	for row in self.sc_source_table:
		source_rows.setdefault(row.item_code, row)

	if not source_rows or not self.sc_target_table:
		return

	item_codes = set(source_rows)
	item_codes.update(row.item_code for row in self.sc_target_table if row.item_code)

	sieve_values = _get_sieve_range_values(item_codes)
	dimension_map = _get_sieve_dimensions(
		{value for value in sieve_values.values() if value}
	)

	source_bands = [
		_resolve_sieve_band(row, _("Source"), sieve_values, dimension_map)
		for row in source_rows.values()
	]

	for target_row in self.sc_target_table:
		target_value, target_down, target_up = _resolve_sieve_band(
			target_row, _("Target"), sieve_values, dimension_map
		)

		bounds = _parse_sieve_bounds(target_value)
		if not bounds:
			frappe.throw(
				_(
					"Row #{0}: Target item {1} has sieve value {2}, which is not in the "
					"expected +From-To form."
				).format(
					target_row.idx,
					frappe.bold(target_row.item_code),
					frappe.bold(target_value),
				)
			)
		low, high = bounds

		for source_value, source_down, source_up in source_bands:
			if low < source_down or high > source_up:
				frappe.throw(
					_(
						"Row #{0}: Target item {1} has sieve range {2} to {3}, which is outside "
						"the allowed band {4} to {5} of source sieve size {6}."
					).format(
						target_row.idx,
						frappe.bold(target_row.item_code),
						frappe.bold(low),
						frappe.bold(high),
						frappe.bold(source_down),
						frappe.bold(source_up),
						frappe.bold(source_value),
					)
				)

			if target_down < source_down or target_up > source_up:
				frappe.throw(
					_(
						"Row #{0}: Target item {1} has UP/DOWN {2} to {3}, which is outside the "
						"allowed band {4} to {5} of source sieve size {6}."
					).format(
						target_row.idx,
						frappe.bold(target_row.item_code),
						frappe.bold(target_down),
						frappe.bold(target_up),
						frappe.bold(source_down),
						frappe.bold(source_up),
						frappe.bold(source_value),
					)
				)


def validate_source_batches(self):
	"""A batch a Diamond Conversion produced may not be re-consumed by another one.

	The picker exclusion in ``get_source_batches`` is cosmetic -- Frappe's ``_validate_links``
	only checks that the Batch exists and never re-runs a field's custom ``get_query`` -- so a
	pasted, imported or API-set batch reaches here untouched. This is the real enforcement.
	"""
	if self.conversion_type != SIEVE_TO_SIEVE:
		return

	for row in self.sc_source_table:
		if not row.batch:
			# update_batch_details only rebuilds the table ``if rows_to_append``, so a row whose
			# every candidate batch was barred survives with no batch at all. Left alone it
			# would reach make_diamond_stock_entry as batch_no=None and let the Stock Entry
			# auto-select the banned batch at submit.
			frappe.throw(
				_(
					"Row #{0}: No usable batch could be allocated for {1}. The available stock "
					"was produced by a Diamond Conversion and cannot be converted again."
				).format(row.idx, frappe.bold(row.item_code)),
				title=_("No Usable Batch"),
			)

	conversion_by_batch = get_diamond_conversion_target_batches(
		[row.batch for row in self.sc_source_table]
	)
	if not conversion_by_batch:
		return

	for row in self.sc_source_table:
		producing_conversion = conversion_by_batch.get(row.batch)
		if producing_conversion:
			frappe.throw(
				_(
					"Row #{0}: Batch {1} was created by Diamond Conversion {2}, so it cannot be "
					"used as a source batch here. Clear the batch and pick another."
				).format(
					row.idx, frappe.bold(row.batch), frappe.bold(producing_conversion)
				),
				title=_("Batch Already Converted"),
			)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_source_batches(doctype, txt, searchfield, start, page_len, filters):
	"""Source-batch picker for Diamond Conversion, minus this conversion's own output.

	A separate query rather than a change to ``metal_conversions.get_filtered_batches``, which
	Metal Conversions and Gemstone Conversion also use.

	The six positional parameters are load-bearing: ``validate_and_sanitize_search_inputs``
	rebuilds kwargs with ``dict(zip(fn.__code__.co_varnames, args))`` and ``co_varnames`` is
	positional and includes locals, so any extra input has to ride inside ``filters``.
	"""
	conversion_type = None
	if isinstance(filters, dict):
		# search_widget also hands through str / None / list, none of which support pop.
		filters = dict(filters)
		conversion_type = filters.pop("conversion_type", None)

	data = get_batch_no(doctype, txt, searchfield, start, page_len, filters)
	if conversion_type != SIEVE_TO_SIEVE or not data:
		return data

	conversion_by_batch = get_diamond_conversion_target_batches(
		[row[0] for row in data]
	)
	if not conversion_by_batch:
		return data

	return [row for row in data if row[0] not in conversion_by_batch]


def validate_purity(self):
	allowed_purities = frappe.db.get_all(
		"Diamond Conversion Purity",
		filters={
			"parent": self.manufacturer,
			"parenttype": "Manufacturing Setting",
			"parentfield": "diamond_conversion_purity",
		},
		fields=["from_purity", "to_purity"],
	)

	allowed_tuples = {(d.from_purity, d.to_purity) for d in allowed_purities}

	item_codes = list(
		{row.item_code for row in self.sc_source_table + self.sc_target_table}
	)

	attributes = frappe.db.get_all(
		"Item Variant Attribute",
		filters={"attribute": "Diamond Grade", "parent": ["in", item_codes]},
		fields=["parent", "attribute_value"],
	)
	grade_map = {d.parent: d.attribute_value for d in attributes}

	source_purities = set()
	for row in self.sc_source_table:
		purity = grade_map.get(row.item_code)
		if purity:
			source_purities.add(purity)
		else:
			frappe.throw(
				_(
					"Row #{0}: Source item {1} is missing the Diamond Grade attribute."
				).format(row.idx, frappe.bold(row.item_code))
			)

	target_purities = set()
	for row in self.sc_target_table:
		purity = grade_map.get(row.item_code)
		if purity:
			target_purities.add(purity)
		else:
			frappe.throw(
				_(
					"Row #{0}: Target item {1} is missing the Diamond Grade attribute."
				).format(row.idx, frappe.bold(row.item_code))
			)

	for s_purity in source_purities:
		valid_target_found = False
		for t_purity in target_purities:
			if s_purity == t_purity or (s_purity, t_purity) in allowed_tuples:
				valid_target_found = True
				break

		if not valid_target_found:
			frappe.throw(
				_(
					"No valid target Diamond Grade found for source Diamond Grade {0}. Allowed conversions as per Manufacturing Settings of {1} must exist."
				).format(frappe.bold(s_purity), frappe.bold(self.manufacturer))
			)

	for t_purity in target_purities:
		valid_source_found = False
		for s_purity in source_purities:
			if s_purity == t_purity or (s_purity, t_purity) in allowed_tuples:
				valid_source_found = True
				break

		if not valid_source_found:
			frappe.throw(
				_(
					"No valid source Diamond Grade found to convert to target Diamond Grade {0}. Allowed conversions as per Manufacturing Settings of {1} must exist."
				).format(frappe.bold(t_purity), frappe.bold(self.manufacturer))
			)


def make_diamond_stock_entry(self):
	target_wh = self.target_warehouse
	source_wh = self.source_warehouse

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"company": self.company,
			"stock_entry_type": "Repack-Diamond Conversion",
			"purpose": "Repack",
			"custom_diamond_conversion": self.name,
			"auto_created": 1,
			"branch": self.branch,
		}
	)
	inventory_wise_data = {}
	for row in self.sc_source_table:
		if inventory_wise_data.get(row.inventory_type):
			inventory_wise_data[row.inventory_type]["qty"] += row.qty
		else:
			inventory_wise_data[row.inventory_type] = {
				"customer": row.get("customer"),
				"qty": row.qty,
			}
		se.append(
			"items",
			{
				"item_code": row.item_code,
				"qty": row.qty,
				"inventory_type": row.inventory_type,
				"batch_no": row.batch,
				"department": self.department,
				"employee": self.employee,
				"manufacturer": self.manufacturer,
				"s_warehouse": source_wh,
				"use_serial_batch_fields": True,
				"customer": row.get("customer"),
			},
		)
	for row in self.sc_target_table:
		for inventory in inventory_wise_data:
			se.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": (
						(row.qty * inventory_wise_data[inventory]["qty"])
						/ self.sum_source_table
					),
					# "inventory_type": "Regular Stock",  # row.inventory_type,
					# "batch_no":row.batch,
					"department": self.department,
					"employee": self.employee,
					"manufacturer": self.manufacturer,
					"t_warehouse": target_wh,
					"inventory_type": inventory,
					"set_basic_rate_manually": 1,
					"customer": inventory_wise_data[inventory].get("customer"),
				},
			)
	se.save()
	amount = 0
	for row in se.items:
		if row.s_warehouse:
			amount += row.amount

	avg_amount = amount / self.sum_source_table
	for row in se.items:
		if row.t_warehouse:
			row.basic_rate = flt(avg_amount, 3)
			row.amount = row.qty * avg_amount
			row.basic_amount = row.qty * avg_amount

	se.save()
	se.submit()
	self.stock_entry = se.name
