import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.bom_weights import (
	get_weights_for_serials,
	row_serials,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
	prefetch_purity_percentages,
)


def _is_pure_qty_row(self, row):
	"""Rows whose pure quantity is derived from metal purity.

	Written once and used by both the prefetch and the loop below so the two can never come
	to select different rows.
	"""
	return (
		row.custom_variant_of in ["M", "F"]
		and self.custom_transfer_type != "Transfer To Branch"
	)


def update_pure_qty(self):
	self.custom_total_quantity = 0
	pure_item_purity = None

	# One query for every purity this document needs, rather than one per distinct item.
	# ``get_purity_percentage`` is request-cached, so priming it here also serves the Stock
	# Entry that ``create_stock_entry`` saves later in this same request -- its
	# ``before_validate`` looks up the very same item codes once per row.
	prefetch_purity_percentages(
		row.custom_alternative_item or row.item_code
		for row in self.items
		if _is_pure_qty_row(self, row)
	)

	for row in self.items:
		if _is_pure_qty_row(self, row):
			if not pure_item_purity:
				# pure_item = frappe.db.get_value("Manufacturing Setting", self.company, "pure_gold_item")

				pure_item = frappe.db.get_value(
					"Manufacturing Setting",
					{"manufacturer": self.custom_manufacturer},
					"pure_gold_item",
				)

				if not pure_item:
					# frappe.throw(_("Pure Item not mentioned in Manufacturing Setting"))
					frappe.throw(
						_("Select Manufacturer in session defaults or in Filed")
					)

				pure_item_purity = get_purity_percentage(pure_item)

			item_purity = get_purity_percentage(
				row.custom_alternative_item or row.item_code
			)

			if not item_purity:
				continue

			if pure_item_purity == item_purity:
				row.custom_pure_qty = row.qty

			else:
				row.custom_pure_qty = flt((item_purity * row.qty) / pure_item_purity, 3)

		self.custom_total_quantity += row.qty


def get_variant_warehouse_map(manufacturer):
	"""``{variant: target_warehouse}`` from ``Manufacturer.custom_reservation_table``.

	Filtered on the full parent/parenttype/parentfield tuple: ``Manufacturer`` carries three
	sibling child tables and nothing on the child doctype prevents a stray row.
	"""
	if not manufacturer:
		return {}

	rows = frappe.db.get_all(
		"Variant based Warehouse",
		{
			"parent": manufacturer,
			"parenttype": "Manufacturer",
			"parentfield": "custom_reservation_table",
		},
		["variant", "target_warehouse"],
		order_by="idx",
	)

	return {
		row.variant: row.target_warehouse
		for row in rows
		if row.variant and row.target_warehouse
	}


def set_reservation_warehouse(self):
	"""Default ``set_warehouse`` from the Manufacturer's reservation table.

	Only for ``Manufacture`` requests, and only when ``set_warehouse`` is still blank -- an MR
	routed by hand (RM Procurement, Waxing RM, ...) is never overwritten. The desk form
	re-derives on item/manufacturer/type change; this is the server-side net that also covers
	programmatic creation.
	"""
	if self.material_request_type != "Manufacture" or self.set_warehouse:
		return

	manufacturer = self.custom_manufacturer or frappe.defaults.get_user_default(
		"manufacturer"
	)
	warehouse_map = get_variant_warehouse_map(manufacturer)
	if not warehouse_map:
		return

	# Every row must resolve to the same warehouse. A row whose variant is unmapped (or blank,
	# as on the dummy gemstone item) contributes None, which fails this check -- deliberately,
	# so a partially-mappable request is left alone rather than half-routed.
	targets = {warehouse_map.get(row.custom_variant_of) for row in self.items}
	if len(targets) != 1:
		return

	target = targets.pop()
	if not target:
		return

	# Manufacturer.company is informational -- nothing filters or validates it, and the live
	# data already disagrees in places -- so a mismatched custom_manufacturer would otherwise
	# write another company's warehouse onto the rows.
	if frappe.get_cached_value("Warehouse", target, "company") != self.company:
		return

	# Only claim the header when the rows agree with it, otherwise ERPNext's
	# reset_default_field_value would clear it straight back out.
	routed = {row.warehouse for row in self.items if row.warehouse}
	if routed - {target}:
		return

	self.set_warehouse = target
	for row in self.items:
		if not row.warehouse:
			row.warehouse = target


def validate_warehouse(self):
	if self.material_request_type == "Material Transfer":
		if self.set_from_warehouse and self.set_warehouse:
			if self.set_from_warehouse == self.set_warehouse:
				frappe.throw(
					_(
						"The source warehouse and the target warehouse cannot be the same."
					)
				)

			for row in self.items:
				if row.from_warehouse == row.warehouse:
					frappe.throw(
						_(
							"The source warehouse and the target warehouse cannot be the same."
						)
					)


def validate_fg_serial_rows(self):
	"""One FG serial per row, qty 1, with that piece's BOM weights stamped.

	Each finished-goods piece is a distinct physical item carrying its own as-built BOM,
	so a row holding several FG serials cannot describe them -- its weights would belong
	to whichever piece happened to be scanned first. The desk form already splits scans
	one row per serial; this is the backstop for the paths that bypass it (direct API
	save, data import, an amended older request).

	Scope is deliberately narrow. Only rows whose serials resolve an as-built BOM are
	touched, which is exactly what "an FG serial" means here. A serialised row that is
	not FG -- and every row with no serial at all -- is left completely alone, so this
	cannot disturb the non-FG serialised flows or the Settle path.

	Throws rather than silently coercing ``qty``: ``qty`` is in
	``GUARDED_MR_ITEM_FIELDS``, so a quiet correction on a post-Draft save would read as
	a user edit to ``guard_non_system_manager_field_edits`` and be rejected there with a
	far more confusing message.

	``getattr`` throughout, mirroring that guard: the tests drive this path with
	``SimpleNamespace``-style mocks that carry no ``.get()``.
	"""
	rows = [
		(row, row_serials(getattr(row, "serial_no", None)))
		for row in (getattr(self, "items", None) or [])
	]
	rows = [(row, serials) for row, serials in rows if serials]
	if not rows:
		# The overwhelming majority of Material Requests carry no serials at all, and
		# this runs on every save -- cost nothing on them.
		return

	# One bulk resolve for every serial in the document, not just the single-serial
	# rows: a merged row has to be classified as FG before it can be rejected as one.
	weights = get_weights_for_serials(
		[serial for _, serials in rows for serial in serials]
	)

	for row, serials in rows:
		fg_serials = [serial for serial in serials if serial in weights]
		if not fg_serials:
			continue

		idx = getattr(row, "idx", "?")
		if len(serials) > 1:
			frappe.throw(
				_(
					"Row #{0}: {1} serial numbers on one row. Each finished-goods serial"
					" must be on its own row with quantity 1."
				).format(idx, len(serials))
			)

		if flt(getattr(row, "qty", 0)) != 1:
			frappe.throw(
				_(
					"Row #{0}: quantity must be 1 for finished-goods serial {1}, not {2}."
				).format(idx, serials[0], flt(getattr(row, "qty", 0)))
			)

		for fieldname, value in weights[serials[0]].items():
			setattr(row, fieldname, value)
