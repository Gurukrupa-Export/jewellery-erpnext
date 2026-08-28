import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from jewellery_erpnext.refining.constants import (
	BATCH_TYPE_UNUSED,
	REFINING_TYPE_SCRAP,
	REFINING_TYPE_SERIAL,
	REFINING_TYPE_UNUSED,
	REFINING_TYPE_WORK_ORDER,
	SOURCE_TYPE_BOM_COMPONENT,
	SOURCE_TYPE_CONSUMABLE,
	SOURCE_TYPE_SCRAP,
	SOURCE_TYPE_SERIAL,
	SOURCE_TYPE_UNUSED,
)
from jewellery_erpnext.refining.doctype.refinery_price_list.refinery_price_list import (
	build_refinery_price_index,
	pick_price_slab,
	refining_line_terms,
	resolve_from_index,
)
from jewellery_erpnext.utils import get_variant_of_item, resolve_manufacturing_setting

#: The pure (24KT-equivalent) Metal Loss variant every refining type books its loss
#: against. get_dust_item() falls back to a resolution chain only when it is absent.
PURE_LOSS_ITEM = "ML-G-24KT-99.9-Y"


EXTERNAL_PRICING_CATEGORY = {
	REFINING_TYPE_SERIAL: "REF-FSJ-001",
	REFINING_TYPE_WORK_ORDER: "REF-FSJ-001",
	REFINING_TYPE_UNUSED: "REF-RMS-001",
	REFINING_TYPE_SCRAP: "REF-MD-001",
}


def secondary_item_row(value="Scrap"):
	"""``{fieldname: value}`` for the Stock Entry Detail secondary-item Select.

	ERPNext 16.33 renamed ``Stock Entry Detail.type`` to ``secondary_item_type``
	(``erpnext.patches.v16_0.rename_secondary_item_type_field``), so the name has to be
	resolved against the installed version rather than hard-coded — this app runs against
	both sides of that rename.

	It is not a cosmetic field. ``StockEntry.validate_warehouse`` keys the "this row is an
	OUTPUT" branch off ``is_finished_item or <this field> or is_legacy_scrap_item``; a
	Manufacture row that misses it falls through to the input branch, which clears
	``t_warehouse`` and then throws "Source warehouse is mandatory for row N". Writing the
	pre-rename name on a renamed site is silently dropped by ``get_valid_dict``, so the
	stone and loss outputs of a repack died at insert with no hint of the cause.

	Drop this helper and inline ``secondary_item_type`` once every site is on 16.33+.
	"""
	fieldname = (
		"secondary_item_type"
		if frappe.get_meta("Stock Entry Detail").has_field("secondary_item_type")
		else "type"
	)
	return {fieldname: value}


def _default_receiving_warehouse(company):
	"""Company's default warehouse for a purchase, for stock lines with nowhere else to go."""
	if not company:
		return None
	return frappe.db.get_value(
		"Company", company, "default_warehouse_for_sales_return"
	) or frappe.db.get_value(
		"Warehouse",
		{"company": company, "is_group": 0, "disabled": 0, "warehouse_type": "Stores"},
		"name",
		order_by="name asc",
	)


class RefiningEntry(Document):
	def before_insert(self):
		# Set the series before autoname runs (autoname happens before validate), so
		# programmatic/API creation gets the correct per-type series, not just the UI.
		self.set_naming_series()

	def validate(self):
		self.set_naming_series()
		self.validate_configuration()
		self.set_source_department_from_user()
		self.validate_warehouse()
		self.validate_source_department_match()
		self.autofetch_source_materials()
		# After the auto-fetch (which has already dropped restricted rows with a notice):
		# anything restricted still in the table was added by hand or by the API.
		self.validate_variant_restriction()
		self.set_dust_system_quantity()
		self.validate_quantities()
		self.calculate_totals()

		if self.status == "Recovery Entered":
			self.validate_recovery_distribution()
			self.validate_recovered_non_metal()

	def before_submit(self):
		# External refining (the is_external checkbox on any refining type) has its own
		# submit-only lifecycle and per-supplier warehouse — see before_submit_external.
		if cint(self.is_external):
			self.before_submit_external()
			return
		if not self.refining_warehouse:
			frappe.throw(
				_(
					"Refining Warehouse could not be determined for Refining Department {0}."
				).format(frappe.bold(self.refining_department))
			)
		# Build the consolidated Material Items at submit for the types that source from
		# other documents, so an API-created entry (which never scanned) still has rows.
		if self.refining_type in ("Scrap Refining", "Work Order Refining"):
			if not self.material_items:
				self.build_material_table()
		if self.refining_type == "Scrap Refining":
			self.validate_dust_opening_material()
		if not self.material_items:
			frappe.throw(
				_("No materials to refine. Add or fetch materials before submitting.")
			)

		# Reject blocked customers' batches early; the material transfer
		# enforces it authoritatively for FIFO-resolved (batch-less) rows.
		self.validate_customer_block()
		# Re-checked here because build_material_table above CLEARS and rebuilds the table
		# after validate() already ran.
		self.validate_variant_restriction()

		# Serial Number Refining: validate that serials exist
		# For original entries (not parent_refining_entry), serials must be in source warehouse.
		# For duplicate entries (processing duplicate), serials may have moved to refining warehouse.
		if self.refining_type == "Serial Number Refining":
			for sn_row in self.serial_no_details:
				if not sn_row.serial_number:
					continue
				current_wh = frappe.db.get_value(
					"Serial No", sn_row.serial_number, "warehouse"
				)
				if not current_wh:
					frappe.throw(
						_("Serial Number {0} not found in warehouse.").format(
							sn_row.serial_number
						)
					)
				# On original entry (not parent), verify serial is in source warehouse
				# (with new fix, it should always be there since we skip the transfer)
				if not self.parent_refining_entry and current_wh != self.warehouse:
					frappe.log_error(
						f"Serial {sn_row.serial_number} is in {current_wh} but source warehouse is {self.warehouse}. "
						f"This indicates the serial was transferred when it should not have been. "
						f"The material_transfer_se may have included the serial incorrectly.",
						"Serial Warehouse Mismatch - Check Transfer SE",
					)

	def on_submit(self):
		if cint(self.is_external):
			self.on_submit_external()
			return

		if self.parent_refining_entry:
			return

		self.create_material_transfer_se()

		if self.refining_type == "Scrap Refining":
			self.create_dust_opening_receipt_se()

	def on_cancel(self):
		# External shares this path: cancel_linked_stock_entries catches its repack_se
		# (tagged custom_refining_entry like every type's SEs) and _cancel_refining_po
		# finds every PO linked via refining_entry=self.name.
		self.cancel_linked_stock_entries()
		self._cancel_refining_po()

	# --- External Refining (the "Is External Refining" checkbox) ---
	#
	# A modifier on every refining type, with a submit-only lifecycle (no classify/
	# repack/verify/complete/transfer) on ONE document:
	#   - Submit: issues material to the supplier's warehouse and creates the service
	#     Purchase Order.
	#   - receive_from_supplier (a dialog, any time after submit): one Repack SE that
	#     issues the sent material out of the supplier warehouse and receives the
	#     recovered pure metal into the department RM warehouse — no Purchase Receipt,
	#     no second Refining Entry.

	def before_submit_external(self):
		if not self.supplier:
			frappe.throw(_("Refinery Supplier is mandatory for external refining."))

		# Pricing category (the Refinery Price List this consignment bills under),
		# resolved from the refining type unless the operator picked one (Dust has
		# several possible categories — the Dust Item is only the default).
		if not self.pricing_item:
			default_item = EXTERNAL_PRICING_CATEGORY.get(self.refining_type)
			if default_item and frappe.db.exists("Item", default_item):
				self.pricing_item = default_item

		# Same submit-time material build the internal path does for the types that
		# source from other documents (Dust fetches the dept scrap warehouse, Work
		# Order pulls the MWO's running balance); Scrap/Serial rows come from scans.
		if not self.material_items and self.refining_type in (
			"Scrap Refining",
			"Work Order Refining",
		):
			self.build_material_table()
		# External serial refining does not put the design code in the table, so a serial
		# whose BOM has no components leaves it legitimately empty. Let that reach the
		# qty_to_refine check below, which throws with the accurate reason.
		if not self.material_items and not (
			self.refining_type == "Serial Number Refining" and self.serial_no_details
		):
			frappe.throw(
				_("No materials to refine. Add or fetch materials before submitting.")
			)
		# Same physical-verification guard as the internal path: Material Items must total
		# the counted physical quantity (the operator adds the difference item), and the
		# excess is receipted into the supplier warehouse on submit.
		if self.refining_type == "Scrap Refining":
			self.validate_dust_opening_material()
		self.validate_customer_block()
		# Same reason as before_submit: the build above rebuilds the table.
		self.validate_variant_restriction()

		self.supplier_warehouse = self._get_supplier_warehouse()
		self.refined_metal_item = self._get_pure_gold_24kt_item()
		# Gold weight actually melted by the refiner: the gold ALLOY, INCLUDING gold
		# findings (melted and recovered as pure gold). Only diamonds and gemstones come
		# back intact (see _is_returned_intact), so only their weight is excluded.
		self.qty_to_refine = flt(
			sum(
				flt(row.qty)
				for row in self.material_items
				if row.item_code
				and self.is_gold_item(row.item_code)
				and not self._is_returned_intact(row.item_code)
			),
			3,
		)
		if self.qty_to_refine <= 0:
			frappe.throw(_("No gold weight to send for external refining."))

	def on_submit_external(self):
		self.create_material_transfer_se(target_warehouse=self.supplier_warehouse)
		# Scrap: the physical-over-system excess is receipted straight into the supplier
		# warehouse so it travels with the rest of the consignment.
		if self.refining_type == "Scrap Refining":
			self.create_dust_opening_receipt_se(
				target_warehouse=self.supplier_warehouse
			)
		self.create_external_refining_po()

	def _external_billable_rows(self):
		"""Material Items rows the consignment BILLS on. Four exclusions, each for its own
		reason:

		* qty <= 0 — a zero row would conjure a phantom Flat-charge line out of nothing.
		* diamonds / gemstones (_is_returned_intact) — they ride along to the refiner but are
		  NOT melted; the refiner hands them back and does not charge for them, which is why
		  qty_to_refine already excludes them. Billing them also silently added CARATS to a
		  GRAM total.
		* BOM Component rows — display-only, EXCEPT Serial Number refining, whose design code
		  is not in the table at all, so its melted metal exists only as BOM Component rows
		  and those are what it bills on (same melted-gold predicate as qty_to_refine, so the
		  PO weight and qty_to_refine agree by construction).
		* the Serial Number design-code row — a PIECE COUNT, not grams. build_material_table
		  already keeps it out; this catches entries built before that rule.

		Memoises the two item predicates, which do two DB reads each on a table that can
		hold thousands of rows.
		"""
		gold = {}
		intact = {}
		rows = []
		for row in self.material_items:
			if not row.item_code or flt(row.qty, 3) <= 0:
				continue

			code = row.item_code
			if code not in gold:
				gold[code] = self.is_gold_item(code)
				intact[code] = self._is_returned_intact(code)

			if intact[code]:
				continue
			if row.get("source_type") == SOURCE_TYPE_SERIAL:
				continue
			if row.get("source_type") == SOURCE_TYPE_BOM_COMPONENT and not (
				self.refining_type == "Serial Number Refining" and gold[code]
			):
				continue
			rows.append(row)
		return rows

	def _external_po_groups(self, index):
		"""One dict per Purchase Order line, in first-seen row order.

		Grouped on ``(price_list, uom, consumable_item)`` — one key that encodes all three
		billing rules at once:

		* ``price_list`` — one line per matched Refinery Price List (resolved from the row's
		  own item code, its template, or the list's category item; see resolve_from_index).
		  A row matching nothing falls back to the entry's ``pricing_item`` list.
		* ``uom`` — grams, litres and carats can never be summed onto one line. Taken from
		  ``row.uom`` first (the operator weighed it) and ``Item.stock_uom`` only as a
		  fallback, because the consumables actually sent are ``stock_uom = Nos`` items
		  recorded in Gram.
		* ``consumable_item`` — the row's own item code for a consumable, else None. This
		  makes a consumable STRUCTURALLY unable to merge into another line while two rows of
		  the SAME consumable still merge at a summed qty. Consumables also skip the
		  ``pricing_item`` fallback: an unmapped one surfaces as a rate-0 line rather than
		  silently adding its weight to the material line at the material's rate.

		Returns ``[{price_list, item_code, uom, qty, consumable, row_idx}, …]``.
		"""
		rows = self._external_billable_rows()
		if not rows:
			return []

		codes = {row.item_code for row in rows}
		if self.pricing_item:
			codes.add(self.pricing_item)
		item_meta = {
			d.name: d
			for d in frappe.get_all(
				"Item",
				filters={"name": ["in", list(codes)]},
				fields=["name", "variant_of", "stock_uom"],
			)
		}

		fallback_list = None
		if self.pricing_item:
			meta = item_meta.get(self.pricing_item) or frappe._dict()
			fallback_list = resolve_from_index(
				index, self.pricing_item, meta.get("variant_of")
			)

		groups = {}
		for row in rows:
			meta = item_meta.get(row.item_code) or frappe._dict()
			consumable = (
				row.item_code
				if (
					row.get("is_consumable")
					or row.get("source_type") == SOURCE_TYPE_CONSUMABLE
				)
				else None
			)
			price_list = resolve_from_index(
				index, row.item_code, meta.get("variant_of")
			)
			if not price_list and not consumable:
				price_list = fallback_list
			uom = row.uom or meta.get("stock_uom") or "Gram"

			key = (price_list, uom, consumable)
			group = groups.setdefault(
				key,
				{
					"price_list": price_list,
					"item_code": consumable
					or (index["parents"].get(price_list) or frappe._dict()).get("item")
					or self.pricing_item,
					"uom": uom,
					"qty": 0.0,
					"consumable": consumable,
					"row_idx": [],
				},
			)
			group["qty"] = flt(group["qty"] + flt(row.qty), 3)
			group["row_idx"].append(row.idx)
		return list(groups.values())

	def _external_service_po_line(self, group, qty, rate, uom, slab=None):
		# The item the matched Refinery Price List bills under (REF-RMS-001, REF-MD-001, …),
		# already resolved into the group by _external_po_groups. It used to be dropped into
		# the description only, and every line went out on the generic REF-SVC-001 charge
		# item, so a PO never said what was actually being refined.
		line_item = group.get("item_code") or self._default_refining_service_item()
		if not line_item:
			frappe.throw(
				_(
					"No item to bill this refining charge against — set an Item on the "
					"Refinery Price List, or seed the default service item {0}."
				).format(frappe.bold("REF-SVC-001"))
			)

		basis = (slab or {}).get("weight_basis") or "Gross Weight"
		line = {
			"item_code": line_item,
			"qty": qty,
			"uom": uom,
			"rate": rate,
			"schedule_date": self.posting_date,
			# ALWAYS the material weight, even when qty is 1 for a flat charge.
			"custom_gross_wt": group["qty"],
			"custom_refining_price_list": group["price_list"],
			# The basis is spelled out because every line is billed on the GROSS weight
			# regardless of it: a Fine / After-burning slab is a true-up the buyer has to
			# chase on the invoice, not something this entry can compute at submit.
			"description": _(
				"{0} — {1} {2} (billed on Gross Weight, slab basis: {3})"
			).format(
				group["item_code"] or _("Unpriced"),
				flt(group["qty"], 3),
				group["uom"],
				basis,
			),
		}

		# The category items (REF-RMS-001 and friends) are stock items, unlike the generic
		# REF-SVC-001 charge item, and ERPNext rejects a stock line with no warehouse.
		#
		# Resolved best-effort and never thrown from: this method feeds _external_po_lines,
		# which is pure and doubles as the dry-run harness behind
		# preview_external_refining_po. A throw here would break the preview and the
		# "every external entry ALWAYS gets a draft PO" invariant. validate() populates
		# supplier_warehouse on every saved entry (_get_supplier_warehouse throws first if
		# the supplier has none), so an unresolved warehouse only happens on an unsaved
		# doc -- and ERPNext raises its own "Warehouse is mandatory" at insert regardless.
		if frappe.db.get_value("Item", line_item, "is_stock_item"):
			warehouse = self.supplier_warehouse or _default_receiving_warehouse(
				self.company
			)
			if warehouse:
				line["warehouse"] = warehouse

		return line

	def _external_po_lines(self, index=None):
		"""``(lines, unpriced)`` for the service Purchase Order. Pure — builds no documents
		and writes nothing, so it doubles as the dry-run harness
		(preview_external_refining_po)."""
		if index is None:
			index = build_refinery_price_index(self.refining_type)

		groups = self._external_po_groups(index)
		if not groups and flt(self.qty_to_refine) > 0:
			# Purchase Order.items is mandatory, so an empty map would abort the submit with
			# "Data missing in table: Items". Fall back to the entry's default category on
			# the melted-gold weight so the physical flow never blocks on a pricing gap.
			groups = [
				{
					"price_list": resolve_from_index(index, self.pricing_item)
					if self.pricing_item
					else None,
					"item_code": self.pricing_item,
					"uom": "Gram",
					"qty": flt(self.qty_to_refine, 3),
					"consumable": None,
					"row_idx": [],
				}
			]

		lines = []
		unpriced = []
		for group in groups:
			slab = (
				pick_price_slab(index, group["price_list"], group["qty"])
				if group["price_list"]
				else None
			)
			qty, rate, uom = refining_line_terms(
				slab.charge_type if slab else None,
				slab.rate if slab else 0,
				group["qty"],
				group["uom"],
			)
			if not slab:
				unpriced.append(group)
			lines.append(self._external_service_po_line(group, qty, rate, uom, slab))
		return lines, unpriced

	@frappe.whitelist()
	def preview_external_refining_po(self):
		"""What create_external_refining_po WOULD bill, without creating anything. Read-only,
		so it is safe to run against a submitted entry on a production site."""
		lines, unpriced = self._external_po_lines()
		return {
			"lines": lines,
			"unpriced": [
				{"item_code": g["item_code"], "qty": g["qty"], "uom": g["uom"]}
				for g in unpriced
			],
		}

	def create_external_refining_po(self):
		"""After the material transfer: ALWAYS create the draft service Purchase Order —
		every external entry gets one, no exceptions.

		It carries ONE LINE PER (price list, UOM, consumable item) present in Material Items
		(see _external_po_groups), each priced NOW from that group's own Refinery Price List
		slab on the group's summed weight. qty is the WEIGHT and rate is PER UNIT, so
		``amount = qty x rate`` — except a Flat Charge, which is a per-consignment fee and
		stays qty 1 (see refining_line_terms). A group with no matching slab gets a rate-0
		line for the purchase team to price manually. Left as a draft for the buyer to
		review; the physical flow never blocks on pricing.
		"""
		po = frappe.new_doc("Purchase Order")
		po.refining_entry = self.name
		po.supplier = self.supplier
		po.company = self.company
		po.transaction_date = self.posting_date
		# ALWAYS assign the attribute (None when the master is missing): the app's PO
		# validate hook reads self.purchase_type unconditionally, and on a site where the
		# custom field is not in the meta, a never-assigned attribute raises AttributeError.
		po.purchase_type = (
			"Service" if frappe.db.exists("Purchase Type", "Service") else None
		)

		lines, unpriced = self._external_po_lines()
		for line in lines:
			po.append("items", line)

		po.insert(ignore_permissions=True)
		self.db_set("refining_entry_po", po.name)

		if unpriced:
			frappe.msgprint(
				_(
					"No Refinery Price List slab matched these groups — their Purchase "
					"Order lines were created at rate 0 for manual pricing: {0}"
				).format(
					", ".join(
						f"{frappe.bold(g['item_code'] or '-')} "
						f"({flt(g['qty'], 3)} {g['uom']})"
						for g in unpriced
					)
				),
				alert=True,
				indicator="orange",
			)

	@frappe.whitelist()
	def receive_from_supplier(self, recovery_weight):
		"""Record receipt of refined metal from the supplier directly on THIS entry — no
		second Refining Entry, no Purchase Receipt. Builds one Repack Stock Entry that
		issues the originally sent material out of the supplier warehouse and receives
		the recovered pure metal into the department's Raw Material warehouse; ERPNext
		values the output from the consumed input (standard Repack costing), so no
		explicit rate is needed.

		Does NOT bill anything: every service charge is priced at submit on the gross
		material weight, whatever the slab's weight_basis says. A Received-Fine /
		After-Burning true-up against the weight reported here is not implemented.
		"""
		if not cint(self.is_external):
			frappe.throw(
				_("This action is only available for external refining entries.")
			)
		if self.docstatus != 1:
			frappe.throw(_("Submit the entry before receiving material."))
		if self.repack_se:
			frappe.throw(
				_(
					"Refined material has already been received against this entry "
					"(Stock Entry {0})."
				).format(frappe.bold(self.repack_se))
			)
		recovery_weight = flt(recovery_weight, 3)
		if recovery_weight <= 0:
			frappe.throw(_("Recovery Weight must be greater than zero."))

		target_wh = self._get_source_dept_rm_warehouse(self.department)

		precision = 3
		min_qty = 0.001

		# Booked as a Manufacture, not a plain Repack: the receipt has ONE finished good
		# (the pure metal) plus by-products (the returned stones). ERPNext's Repack
		# validation force-marks EVERY received row as a finished good and then refuses to
		# auto-cost multiple finished goods; a work-order-less Manufacture keeps our
		# is_finished_item flags and auto-costs the single FG from the consumed inputs.
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Manufacture"
		se.purpose = "Manufacture"
		se.company = self.company
		se.custom_refining_entry = self.name
		se.auto_created = 1
		se.to_subcontractor = self.supplier
		# The Stock Entry before_validate pure-qty hook resolves the Manufacturing
		# Setting via the SE's manufacturer for Metal/Finding rows; carry the entry's
		# manufacturer so it doesn't depend on the submitting user's session default.
		se.manufacturer = self.manufacturer

		# Group batch-less rows item-wise and FIFO-allocate ONCE per item: several rows of
		# the same item would each allocate independently and double-claim the same
		# batches, over-consuming them into negative stock. Serial rows keep per-row
		# identity.
		expected_qty = 0.0
		issued_qty = 0.0
		# Actually-consumed qty per item, so a returned row can never output more than
		# physically came back out of the supplier warehouse.
		consumed_by_item = {}
		grouped = {}
		serial_rows = []
		# See create_material_transfer_se: external serial refining keeps the design code
		# out of material_items, so the scanned serials — transferred into the supplier
		# warehouse on submit from the same source — are consumed back out of it here.
		for item in list(self.material_items) + self._serial_movement_rows():
			if item.get("source_type") == "BOM Component":
				continue
			qty = flt(item.qty, precision)
			if qty < min_qty:
				continue
			expected_qty += qty
			if item.serial_no:
				serial_rows.append(item)
			else:
				g = grouped.setdefault(item.item_code, {"qty": 0.0, "uom": item.uom})
				g["qty"] += qty

		for item_code, g in grouped.items():
			qty = flt(g["qty"], precision)
			has_batch = frappe.db.get_value("Item", item_code, "has_batch_no")
			if has_batch:
				allocations = self.allocate_fifo_batches(
					item_code,
					self.supplier_warehouse,
					qty,
					throw_if_missing=False,
				)
				for alloc in allocations:
					if flt(alloc["qty"], precision) >= min_qty:
						issued_qty += flt(alloc["qty"], precision)
						consumed_by_item[item_code] = consumed_by_item.get(
							item_code, 0.0
						) + flt(alloc["qty"], precision)
						se.append(
							"items",
							{
								"item_code": item_code,
								"qty": alloc["qty"],
								"uom": g["uom"],
								"s_warehouse": self.supplier_warehouse,
								"batch_no": alloc["batch_no"],
								"use_serial_batch_fields": 1,
								# Scrap/dust routinely enters stock via zero-rate
								# Material Receipts; the repack must not block on a
								# missing valuation history.
								"allow_zero_valuation_rate": 1,
							},
						)
			else:
				issued_qty += qty
				consumed_by_item[item_code] = consumed_by_item.get(item_code, 0.0) + qty
				se.append(
					"items",
					{
						"item_code": item_code,
						"qty": qty,
						"uom": g["uom"],
						"s_warehouse": self.supplier_warehouse,
						"use_serial_batch_fields": 1,
						"allow_zero_valuation_rate": 1,
					},
				)

		for item in serial_rows:
			qty = flt(item.qty, precision)
			issued_qty += qty
			consumed_by_item[item.item_code] = (
				consumed_by_item.get(item.item_code, 0.0) + qty
			)
			se.append(
				"items",
				{
					"item_code": item.item_code,
					"qty": qty,
					"uom": item.uom,
					"s_warehouse": self.supplier_warehouse,
					"serial_no": item.serial_no,
					"use_serial_batch_fields": 1,
					"allow_zero_valuation_rate": 1,
				},
			)

		if not se.items:
			frappe.throw(
				_("No material available in the Supplier Warehouse to receive against.")
			)

		# Keep customer-owned metal tagged Customer Goods across the round trip: stamp the
		# consumed rows from their supplier-warehouse batches (auto_created SEs skip the
		# generic backfill), then mint the recovered metal back to that customer when the
		# whole consumed lot is theirs (same rule as the internal repack).
		self._stamp_batch_ownership(se)
		# Batches already consumed as a SOURCE row on this Stock Entry. A batch can hold
		# stock in both the supplier and the target warehouse, and reusing one for an
		# output would put the same batch on both sides of a single Manufacture.
		consumed_batches = {row.batch_no for row in se.items if row.get("batch_no")}
		output_customer = self._recovered_output_customer(se, self.refined_metal_item)
		output_owner_row = (
			{"inventory_type": "Customer Goods", "customer": output_customer}
			if output_customer
			else {}
		)

		metal_row = {
			"item_code": self.refined_metal_item,
			"qty": recovery_weight,
			"uom": "Gram",
			"t_warehouse": target_wh,
			"is_finished_item": 1,
			"use_serial_batch_fields": 1,
			"allow_zero_valuation_rate": 1,
			**output_owner_row,
		}
		# Reuse the 24KT item's EXISTING batch in the receiving warehouse when there is one
		# — the refined metal belongs in the batch already shown against that item (see
		# _receive_target_batch). Mints only when nothing suitable exists.
		metal_batch = self._receive_target_batch(
			self.refined_metal_item,
			target_wh,
			customer=output_customer,
			exclude=consumed_batches,
		)
		if metal_batch:
			metal_row["batch_no"] = metal_batch
		se.append("items", metal_row)

		# Return the diamonds and gemstones intact. The refiner melts the gold alloy —
		# INCLUDING gold findings, reborn above as the single pure-24KT row — and hands
		# only the stones back, for every external refining type:
		#   - Scrap / Unused-Loose-Material / Work Order: real material_items rows, already
		#     CONSUMED out of the supplier warehouse above, so re-outputting them here nets
		#     to a supplier -> department transfer.
		#   - Serial Number: the stones sit INSIDE the FG serial (consumed above) and are
		#     represented by its BOM Component rows; here they are exploded out as repack
		#     outputs, exactly like the internal serial repack.
		# Aggregated per item so one fresh return batch is created for each.
		returned = {}
		for item in self.material_items:
			if not self._is_returned_intact(item.item_code):
				continue
			qty = flt(item.qty, precision)
			if qty < min_qty:
				continue
			# Serial's returnables are BOM Component rows exploded from the consumed FG
			# (output-only); every other type's are real rows consumed from the supplier above
			# and are capped below at the amount actually consumed.
			from_bom = item.get("source_type") == "BOM Component"
			r = returned.setdefault(
				item.item_code, {"qty": 0.0, "uom": item.uom, "from_bom": from_bom}
			)
			r["qty"] += qty

		for ret_item, r in returned.items():
			out_qty = flt(r["qty"], precision)
			if not r["from_bom"]:
				# Never return more of a real (non-serial) item than physically came back
				# out of the supplier warehouse, so a transfer/receive shortfall cannot mint
				# phantom department stock.
				out_qty = min(
					out_qty, flt(consumed_by_item.get(ret_item, 0.0), precision)
				)
			if out_qty < min_qty:
				continue
			row = {
				"item_code": ret_item,
				"qty": out_qty,
				"uom": r["uom"] or frappe.db.get_value("Item", ret_item, "stock_uom"),
				"t_warehouse": target_wh,
				# Scrap-type by-product (NOT a finished good): the single finished good is
				# the pure metal above, so the Manufacture auto-costs it from the consumed
				# inputs and these land at zero valuation — mirrors create_repack_se.
				**secondary_item_row(),
				"is_finished_item": 0,
				"use_serial_batch_fields": 1,
				"allow_zero_valuation_rate": 1,
			}
			# Same existing-batch-first rule as the metal row, and the customer is carried
			# through — but ONLY when the Item permits it, exactly as _recovered_output_customer
			# gates the metal. _auto_create_batch stamps custom_inventory_type = "Customer
			# Goods" and Batch.validate -> update_inventory_dimentions THROWS without that
			# flag; it is sparsely maintained on stone variants, so an ungated customer here
			# would hard-fail the whole receive.
			ret_customer = (
				output_customer
				if output_customer and self._item_allows_customer_goods(ret_item)
				else None
			)
			ret_batch = self._receive_target_batch(
				ret_item, target_wh, customer=ret_customer, exclude=consumed_batches
			)
			# Stamped outside the batch check so an unbatched row (or one where
			# auto_create_batch is unticked) still carries its ownership.
			if ret_customer:
				row["inventory_type"] = "Customer Goods"
				row["customer"] = ret_customer
			if ret_batch:
				row["batch_no"] = ret_batch
			se.append("items", row)

		self._assert_no_loss_output(se)

		se.insert(ignore_permissions=True)
		se.submit()

		# Backfill purity on rows that don't have one (e.g. added via scan_scrap_qr_action):
		# generate_recovery_table groups by row.purity, so a blank would silently drop that
		# row out of the distribution.
		for item in self.material_items:
			if not item.purity:
				item.purity = self.get_item_purity(item.item_code)

		# Reuse the SAME proportional-by-pure-content distribution the internal types use,
		# rather than a separate ad-hoc computation: it populates Gold Recovery Details per
		# karat, one refined_gold row per karat, and the Recovery Summary totals.
		self.generate_recovery_table(total_recovered_weight=recovery_weight)

		self.received_weight = recovery_weight
		self.repack_se = se.name
		self.db_set(
			{
				"received_weight": self.received_weight,
				"repack_se": self.repack_se,
				# Terminal status — overrides the "Classified" status
				# generate_recovery_table set; external refining has no
				# classify/repack/verify/complete lifecycle of its own.
				"status": "Transferred",
			},
			notify=False,
		)

		# allocate_fifo_batches(throw_if_missing=False) caps at available stock and
		# returns silently on shortfall — surface it instead of leaving the supplier
		# warehouse with an un-reconciled, uninvestigated balance.
		shortfall = flt(expected_qty - issued_qty, precision)
		if shortfall >= min_qty:
			frappe.log_error(
				title="External refining: supplier warehouse reconciliation shortfall",
				message=(
					f"Refining Entry {self.name}: expected to issue {expected_qty} out "
					f"of supplier warehouse {self.supplier_warehouse}, only "
					f"{issued_qty} was available. Shortfall {shortfall} — investigate "
					f"supplier warehouse stock."
				),
			)

		return se.name

	def _get_source_dept_rm_warehouse(self, department):
		wh = frappe.db.get_value(
			"Warehouse",
			{"department": department, "warehouse_type": "Raw Material", "disabled": 0},
			"name",
		)
		if not wh:
			frappe.throw(
				_("No Raw Material warehouse configured for Department {0}.").format(
					frappe.bold(department)
				)
			)
		return wh

	# --- Validations ---

	def autofetch_source_materials(self):
		"""Populate Material Items on save so the consolidated materials (and the Raw
		Materials HTML that reads them) are visible before submit. Runs only on a Draft
		while the table is still empty, so it never clobbers manually edited rows.

		Scoped to Scrap Refining only: it sources from the department Scrap warehouse and
		never throws on a missing attribute. Work Order material is built by
		scan_mwo_action and at submit — building it on every save would raise "Metal
		Purity is mandatory" on a plain Save and resolve source warehouses from Stock
		Reservation Entries whose lifecycle is handled at submit.
		"""
		if self.docstatus != 0 or self.material_items:
			return
		if (
			self.refining_type == "Scrap Refining"
			and (self.warehouse or self.multiple_department)
		) or (self.refining_type == "Work Order Refining" and self.mwo_details):
			self.build_material_table()

	def set_dust_system_quantity(self):
		"""Keep the Scrap Refining System Quantity in lockstep with the material actually
		available in the source Scrap warehouse(s), computed the SAME way the Material
		Items table is built (SBB-aware net qty for batched items). Summing raw
		Bin.actual_qty diverged from the fetched material rows, which is what made the
		System Quantity look wrong against the material table."""
		if self.refining_type != "Scrap Refining" or self.docstatus != 0:
			return
		self.system_quantity = flt(self._compute_dust_system_quantity(), 3)

	def set_naming_series(self):
		# The letters follow the current names: SCP = Scrap, ULM = Unused/Loose Material.
		# They used to trail the Dust->Scrap / Scrap->Unused-Loose rename, which left the
		# actual scrap type minting RFN-DST- ("dust") while SCP- ("scrap") went to
		# unused/loose material -- the reported bug.
		#
		# Document names are immutable, so the two historical prefixes stay meaningful:
		# RFN-DST- is always old Dust Refining, and RFN-SCP- up to 26-00047 is old Scrap
		# (now unused/loose) while later RFN-SCP- documents are Scrap Refining.
		series_map = {
			REFINING_TYPE_SCRAP: "RFN-SCP-.YY.-.#####",
			REFINING_TYPE_WORK_ORDER: "RFN-MWO-.YY.-.#####",
			REFINING_TYPE_SERIAL: "RFN-SRN-.YY.-.#####",
			REFINING_TYPE_UNUSED: "RFN-ULM-.YY.-.#####",
		}
		if self.refining_type in series_map:
			self.naming_series = series_map[self.refining_type]

	def validate_configuration(self):
		if (
			self.refining_type == "Scrap Refining"
			and self.multiple_department
			and self.multiple_operation
		):
			frappe.throw(
				_("Choose either Multiple Operations OR Multiple Department, not both.")
			)

	def set_source_department_from_user(self):
		"""Default the Source Department from the logged-in user's Employee record, which
		also drives the source warehouse via validate_warehouse. A manually chosen
		department is preserved and a missing Employee record is not an error.

		Serial Number Refining is the exception: the serials are refined by refinery staff
		whose own department differs from where the serials sit, so the department is
		derived from the scanned serials instead and always matches. Only on the original
		entry, not the processing duplicate.

		IMPORTANT: only derive from serials when the department is not already set. After
		the Material Transfer SE moves a serial to the refining warehouse its warehouse
		department becomes the refining department, so overwriting would make the Source
		Department track the refinery instead of the original source.
		"""
		if (
			self.refining_type == "Serial Number Refining"
			and not self.parent_refining_entry
		):
			if not self.department:
				for row in self.serial_no_details:
					if not row.serial_number:
						continue
					wh = frappe.db.get_value(
						"Serial No", row.serial_number, "warehouse"
					)
					dept = (
						frappe.db.get_value("Warehouse", wh, "department")
						if wh
						else None
					)
					if dept:
						self.department = dept
						break
			return
		if self.department:
			return
		dept = frappe.db.get_value(
			"Employee", {"user_id": frappe.session.user}, "department"
		)
		if dept:
			self.department = dept

	def validate_source_department_match(self):
		"""Refining is only allowed for material belonging to the Source Department: every
		scanned MWO's department must match (Work Order), and each serial's current
		warehouse department must match (Serial Number). Enforced here so API-created
		entries, which bypass the scan handlers, are validated too.

		Skipped on the processing duplicate (parent_refining_entry set) and on an already
		submitted document: by then the parent's Material Transfer has moved the material
		into the refining warehouse, so the check would wrongly fail. Source validation
		already ran on the parent before submit.
		"""
		if not self.department or self.parent_refining_entry or self.docstatus == 1:
			return

		if self.refining_type == "Work Order Refining":
			for row in self.mwo_details:
				if not row.manufacturing_work_order:
					continue
				mwo_dept = frappe.db.get_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					"department",
				)
				if mwo_dept and mwo_dept != self.department:
					frappe.throw(
						_(
							"MWO {0} belongs to department {1}, which does not match "
							"the Source Department {2}. Refining is not allowed."
						).format(
							row.manufacturing_work_order, mwo_dept, self.department
						)
					)
		if self.refining_type == "Serial Number Refining":
			# Resolve the refining department once so serials already moved into the refining
			# warehouse are allowed: for a duplicate entry (parent_refining_entry set) the
			# parent's material transfer has already moved them.
			refining_dept = (
				frappe.db.get_value("Warehouse", self.refining_warehouse, "department")
				if self.refining_warehouse
				else self.refining_department
			)
			for row in self.serial_no_details:
				if not row.serial_number:
					continue
				wh = frappe.db.get_value("Serial No", row.serial_number, "warehouse")
				sn_dept = (
					frappe.db.get_value("Warehouse", wh, "department") if wh else None
				)
				# Allow the serial in the source warehouse OR the refining warehouse — a duplicate
				# entry's serials have already been moved by the parent's material transfer SE.
				if sn_dept and sn_dept != self.department and sn_dept != refining_dept:
					frappe.throw(
						_(
							"Serial No {0} belongs to department {1}, which does not "
							"match either the Source Department {2} or the Refining Department {3}. "
							"Refining is not allowed."
						).format(
							row.serial_number, sn_dept, self.department, refining_dept
						)
					)

	def _get_supplier_warehouse(self, create=False):
		"""Resolve the Warehouse belonging to ``self.supplier`` via the app-wide
		``Warehouse.subcontractor`` (Link -> Supplier) convention (see
		``product_certification._get_supplier_certification_warehouse`` and
		``manufacturing_operation.py``'s subcontracting warehouse resolution). External
		Refinery issues material against this warehouse instead of a shared pool, so each
		supplier's warehouse must be configured before it can be used."""
		wh = frappe.db.get_value(
			"Warehouse",
			{
				"company": self.company,
				"subcontractor": self.supplier,
				"warehouse_type": "Raw Material",
				"disabled": 0,
				"is_group": 0,
			},
			"name",
		)
		if not wh:
			wh = frappe.db.get_value(
				"Warehouse",
				{
					"company": self.company,
					"subcontractor": self.supplier,
					"disabled": 0,
					"is_group": 0,
				},
				"name",
			)
		if not wh:
			frappe.throw(
				_(
					"Please configure a warehouse for supplier {0} (set Subcontractor on "
					"the Warehouse) before sending material for external refining."
				).format(frappe.bold(self.supplier))
			)
		return wh

	def validate_warehouse(self):
		if self.refining_department and not self.refining_warehouse:
			self.refining_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"department": self.refining_department,
					"warehouse_type": "Raw Material",
				},
				"name",
			)

		# External refining sends loss/dust/semi-finished material picked by the
		# operator — never auto-derive or overwrite their manually chosen Source
		# Warehouse (the field is editable, not read-only, when is_external is set).
		if self.department and not cint(self.is_external):
			is_final_polish = "Final Polish" in self.department
			if not self.warehouse or is_final_polish:
				# Scrap refining sources from the department Scrap warehouse; Unused/Loose Material
				# is received into the department RM warehouse as a designated item (per the
				# Refinery Change SOP) so it sources from RM; MWO and Serial source from
				# Manufacturing.
				if self.refining_type == "Scrap Refining":
					wh_type = "Scrap"
				elif self.refining_type == "Unused/Loose Material Refining":
					wh_type = "Raw Material"
				elif self.refining_type == "Work Order Refining":
					wh_type = "Manufacturing"
				elif self.refining_type == "Serial Number Refining":
					wh_type = "Manufacturing"
				else:
					wh_type = "Manufacturing"

				if is_final_polish and self.refining_type not in (
					"Work Order Refining",
					"Unused/Loose Material Refining",
				):
					wh_type = "Scrap"

				self.warehouse = frappe.db.get_value(
					"Warehouse",
					{"department": self.department, "warehouse_type": wh_type},
					"name",
				)

		# Auto-resolve the Refining Scrap Warehouse where leftover dust is moved
		# after refining (Refining Scrap Warehouse per SOP). Prefer a Scrap-type
		# warehouse on the refining department, then any company Scrap warehouse.
		if not self.scrap_warehouse:
			self.scrap_warehouse = frappe.db.get_value(
				"Warehouse",
				{"department": self.refining_department, "warehouse_type": "Scrap"},
				"name",
			)
			if not self.scrap_warehouse and self.company:
				self.scrap_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"company": self.company,
						"warehouse_type": "Scrap",
						"is_group": 0,
						"name": ["like", "%Refin%"],
					},
					"name",
				) or frappe.db.get_value(
					"Warehouse",
					{"company": self.company, "warehouse_type": "Scrap", "is_group": 0},
					"name",
				)

	def validate_quantities(self):
		# Applies to external dust refining too: the physical count is what actually
		# goes to the supplier, and the physical-over-system excess is receipted and
		# sent along with the rest (see before_submit_external / on_submit_external).
		if self.refining_type == "Scrap Refining":
			sys_qty = flt(self.system_quantity)
			phys_qty = flt(self.physical_quantity)
			self.difference_quantity = phys_qty - sys_qty
			if phys_qty <= 0:
				frappe.throw(_("Physical Quantity must be greater than zero."))

	def _compute_input_pure_weight(self):
		"""Compute gross pure weight and expected recovery from material inputs.

		For Serial Number Refining, prefer BOM Component rows (which include both
		metal AND finding weights), falling back to serial_no_details.pure_weight
		if no BOM components exist. For all other types, iterate material_items.
		This keeps the Recovery Summary consistent with the Gold Recovery
		Distribution table."""
		gross_pure_weight = 0.0
		expected_recovery = 0.0

		if cint(self.is_external):
			# Same purity-weighted computation as the generic branch below. Before receipt,
			# rows added via scan_scrap_qr_action rarely carry a purity yet — fall back to
			# qty_to_refine (the gold weight sent, computed at submit) so the Recovery Summary
			# still shows something sensible.
			for item in self.material_items:
				# Only the gold ALLOY that is actually melted contributes to the pure input.
				# Diamonds/gemstones are returned intact and excluded; gold FINDINGS are melted, so
				# they stay counted. Restricting to is_gold_item also drops the FG serial row,
				# whose qty is a piece count, not grams — it would double-count against the BOM
				# metal-component rows that carry the real melted weight.
				if not (
					self.is_gold_item(item.item_code)
					and not self._is_returned_intact(item.item_code)
				):
					continue
				if item.purity:
					purity_pct = frappe.db.get_value(
						"Attribute Value", item.purity, "purity_percentage"
					)
					if purity_pct:
						gross_pure_weight += flt(item.qty) * (flt(purity_pct) / 100.0)
			if gross_pure_weight <= 0:
				gross_pure_weight = flt(self.qty_to_refine)
			expected_recovery = gross_pure_weight
		elif self.refining_type == "Serial Number Refining":
			bom_components = [
				item
				for item in self.material_items
				if item.get("source_type") == "BOM Component"
			]
			if bom_components:
				for item in bom_components:
					if item.purity:
						purity_pct = frappe.db.get_value(
							"Attribute Value", item.purity, "purity_percentage"
						)
						if purity_pct:
							pure_weight = flt(item.qty) * (flt(purity_pct) / 100.0)
							gross_pure_weight += pure_weight
							expected_recovery += pure_weight
			else:
				for sn in self.serial_no_details:
					gross_pure_weight += flt(sn.pure_weight)
					expected_recovery += flt(sn.pure_weight)
		else:
			for item in self.material_items:
				if item.purity:
					purity_pct = frappe.db.get_value(
						"Attribute Value", item.purity, "purity_percentage"
					)
					if purity_pct:
						pure_weight = flt(item.qty) * (flt(purity_pct) / 100.0)
						gross_pure_weight += pure_weight
						expected_recovery += pure_weight

		return gross_pure_weight, expected_recovery

	def calculate_totals(self):
		(
			self.gross_pure_weight,
			self.expected_recovery,
		) = self._compute_input_pure_weight()

		self.refined_fine_weight = 0.0
		self.actual_recovery = 0.0
		for gold in self.refined_gold:
			self.actual_recovery += flt(gold.refining_gold_weight)
			self.refined_fine_weight += flt(gold.pure_weight) or flt(
				gold.refining_gold_weight
			)

		# Round to display precision (3 dp) before computing derived values so the
		# percentage matches what the user sees: float imprecision otherwise shows the
		# recovery % as 99.98 instead of 100 when the values are equal.
		self.gross_pure_weight = flt(self.gross_pure_weight, 3)
		self.expected_recovery = flt(self.expected_recovery, 3)
		self.refined_fine_weight = flt(self.refined_fine_weight, 3)
		self.actual_recovery = flt(self.actual_recovery, 3)

		# When Gold Recovery Details is populated the Recovery Summary must agree with it
		# EXACTLY. Each row stores its pure content and loss already rounded to 3 dp, and
		# summing those (sum-of-rounded) differs from rounding the input sum (round-of-sum)
		# by ~0.001 per extra karat. Derive the summary from the table so the total and the
		# per-row column never diverge by a stray milligram.
		if self.gold_recovery_details:
			self.gross_pure_weight = flt(
				sum(flt(r.pure_gold_weight, 3) for r in self.gold_recovery_details), 3
			)
			self.expected_recovery = self.gross_pure_weight
			self.refining_loss = flt(
				sum(flt(r.loss_weight, 3) for r in self.gold_recovery_details), 3
			)
		else:
			# Clamp at 0: recovered 24KT gold can slightly exceed the computed pure input
			# (rounding / assay variance) and must never book a negative loss.
			self.refining_loss = flt(
				max(self.gross_pure_weight - self.refined_fine_weight, 0.0), 3
			)

		if self.expected_recovery > 0:
			pct = min(
				(self.refined_fine_weight / self.expected_recovery) * 100.0,
				100.0,
			)
			# The field displays at 2 dp, so 99.9966% would round up to "100.00" next to a
			# non-zero Refining Loss. Cap at 99.99 whenever any loss remains; a full 100.00
			# appears only when the loss is exactly zero.
			if self.refining_loss > 0 and pct > 99.99:
				pct = 99.99
			self.recovery_percentage = pct
		else:
			self.recovery_percentage = 0.0

	def validate_recovery_distribution(self):
		# Refining always yields pure 24KT, so recovery is compared against the PURE gold
		# content of the input (gross_pure_weight), not the gross weight. Diamonds and
		# gemstones carry 1:1 from the source and are not part of this metal ceiling.
		total_recovered_gold = sum(
			flt(gold.refining_gold_weight) for gold in self.refined_gold
		)

		# gross_pure_weight is the pure 24KT-equivalent of the input, set by
		# calculate_totals / _recalculate_and_persist_totals for both refining types.
		pure_gold_input = flt(self.gross_pure_weight)

		# Allow a 0.1 margin for precision/rounding/assay differences
		if total_recovered_gold > pure_gold_input + 0.1:
			frappe.throw(
				_(
					"Recovered 24KT gold weight ({0}) cannot exceed the pure gold "
					"input weight ({1})."
				).format(flt(total_recovered_gold, 3), flt(pure_gold_input, 3))
			)

	def validate_recovered_non_metal(self):
		"""At Recovery Entered, the recovered diamond/gemstone amounts entered by the
		operator cannot exceed the amounts actually present (seeded from the MWO/BOM).
		Applies to both Work Order and Serial Number refining."""
		for label, rows in (
			("Diamond", self.recovered_diamond),
			("Gemstone", self.recovered_gemstone),
		):
			for row in rows:
				# sub-carat weights use a 0.001 margin (precision 3)
				if flt(row.recovered_weight) > flt(row.weight) + 0.001:
					frappe.throw(
						_(
							"Recovered {0} weight ({1}) for {2} cannot exceed the "
							"available weight ({3})."
						).format(
							label,
							flt(row.recovered_weight, 3),
							row.item,
							flt(row.weight, 3),
						)
					)
				if cint(row.recovered_pcs) > cint(row.pcs):
					frappe.throw(
						_(
							"Recovered {0} pcs ({1}) for {2} cannot exceed the "
							"available pcs ({3})."
						).format(
							label, cint(row.recovered_pcs), row.item, cint(row.pcs)
						)
					)

	# --- Action Handlers (Whitelisted for Client Scripts) ---

	@frappe.whitelist()
	def fetch_dust_balance(self):
		"""Fetch total available quantity of all items from the department's Scrap
		warehouse(s). Computed SBB-aware (net of reservations/consumption for batched
		items) so it matches the sum of the fetched Material Items table exactly —
		summing raw Bin.actual_qty diverged from the material rows and looked wrong."""
		if not self.multiple_department and not self.warehouse:
			frappe.throw(_("Source Warehouse is required."))
		self.system_quantity = flt(self._compute_dust_system_quantity(), 3)

		# Return the computed value; the caller decides whether to persist it. This keeps
		# the method usable as a live lookup on physical-quantity entry without forcing a
		# save (which would conflict with unsaved edits).
		return flt(self.system_quantity, 3)

	def _compute_dust_system_quantity(self):
		"""Total available (SBB-aware) qty across the source Scrap warehouse(s)."""
		if self.multiple_department:
			total = 0.0
			for d in self.refining_department_detail:
				dept_wh = frappe.db.get_value(
					"Warehouse",
					{"department": d.department, "warehouse_type": "Scrap"},
					"name",
				)
				if dept_wh:
					total += self._dust_available_qty(dept_wh)
			return total
		if not self.warehouse:
			return 0.0
		return self._dust_available_qty(self.warehouse)

	def _dust_available_qty(self, warehouse):
		"""Available qty of ALL items in a warehouse.
		Mirrors _fetch_loss_items_from_warehouse so System Quantity equals the sum of
		the Material Items it fetches. Without an employee filter this reads raw
		Bin.actual_qty (matches Stock Balance). With an employee selected it sums only
		that employee's scrap/dust batches, using the exact same enumeration the material
		fetch uses (_dust_employee_batch_rows), so the two can never diverge.

		Variants restricted for this refining type are skipped, because
		_fetch_loss_items_from_warehouse skips them too: System Quantity has to stay equal
		to the sum of the fetched rows or difference_quantity is wrong and
		validate_dust_opening_material throws on a difference the operator cannot fill."""
		if self.employee:
			return sum(
				flt(r["qty"], 3) for r in self._dust_employee_batch_rows(warehouse)
			)
		total = 0.0
		bins = frappe.db.get_all(
			"Bin",
			filters={"warehouse": warehouse, "actual_qty": [">", 0]},
			fields=["item_code", "actual_qty"],
		)
		restricted = self._blocked_item_codes(b.item_code for b in bins)
		for b in bins:
			if b.item_code in restricted:
				continue
			aq = flt(b.actual_qty, 3)
			if aq > 0:
				total += aq
		return total

	def _dust_employee_batch_rows(self, warehouse):
		"""Per-batch scrap rows in ``warehouse`` restricted to Batch.custom_employee ==
		self.employee. Shared by _dust_available_qty (System Quantity) and
		_fetch_loss_items_from_warehouse (Material Items) so the total and the fetched
		rows come from the exact same batch enumeration and stay identical.

		Non-batch stock cannot carry the employee marker, so it is excluded entirely when
		an employee filter is active. Blocked-customer batches are NOT excluded here
		(mirroring the unfiltered path): they are enforced authoritatively at submit, and
		matching that keeps System Quantity consistent with the material rows. Variants
		restricted for this refining type ARE excluded, in lockstep with the material fetch.
		"""
		rows = []
		if not warehouse or not self.employee:
			return rows
		bins = frappe.db.get_all(
			"Bin",
			filters={"warehouse": warehouse, "actual_qty": [">", 0]},
			fields=["item_code"],
		)
		restricted = self._blocked_item_codes(b.item_code for b in bins)
		for b in bins:
			if b.item_code in restricted:
				continue
			if not frappe.db.get_value("Item", b.item_code, "has_batch_no"):
				continue
			for a in self.allocate_fifo_batches(
				b.item_code, warehouse, 9999999, throw_if_missing=False
			):
				aq = flt(a.get("qty"), 3)
				if aq <= 0:
					continue
				if (
					frappe.db.get_value("Batch", a.get("batch_no"), "custom_employee")
					!= self.employee
				):
					continue
				rows.append(
					{"item_code": b.item_code, "batch_no": a.get("batch_no"), "qty": aq}
				)
		return rows

	@frappe.whitelist()
	def fetch_dust_materials(self):
		"""Populate the Material Items table with all available scrap materials
		(grouped by item group) from the department Scrap warehouse for Scrap Refining.
		Consumable rows already added manually are preserved."""
		if self.refining_type != "Scrap Refining":
			frappe.throw(_("This action is only available for Scrap Refining."))

		# Preserve any manually added consumable rows
		consumables = [
			row.as_dict()
			for row in self.material_items
			if row.get("is_consumable") or row.get("source_type") == "Consumable"
		]

		self.build_material_table()

		for c in consumables:
			c.pop("name", None)
			self.append("material_items", c)

		self.save(ignore_permissions=True)
		return len(self.material_items)

	@frappe.whitelist()
	def scan_mwo_action(self, barcode):
		mwo = frappe.db.get_value(
			"Manufacturing Work Order",
			{"name": barcode},
			[
				"name",
				"manufacturing_order",
				"item_code",
				"qty",
				"metal_weight",
				"manufacturing_operation",
				"department",
			],
			as_dict=True,
		)
		if not mwo:
			frappe.throw(_("Manufacturing Work Order {0} not found.").format(barcode))

		# Refining is only allowed for MWOs of the user's own (Source) department.
		if self.department and mwo.department and mwo.department != self.department:
			frappe.throw(
				_(
					"MWO {0} belongs to department {1}, which does not match the "
					"Source Department {2}. Refining is not allowed."
				).format(mwo.name, mwo.department, self.department)
			)

		self._throw_if_variant_restricted(mwo.item_code)

		# Check if MWO already added
		for row in self.mwo_details:
			if row.manufacturing_work_order == mwo.name:
				frappe.throw(_("MWO {0} is already added.").format(mwo.name))

		self.append(
			"mwo_details",
			{
				"manufacturing_work_order": mwo.name,
				"parent_manufacturing_work_order": mwo.manufacturing_order,
				"item_code": mwo.item_code,
				"metal_weight": mwo.metal_weight,
				"pcs": mwo.qty,
				"manufacturing_operation": mwo.manufacturing_operation,
			},
		)
		self.scan_mwo = ""
		self.build_material_table()
		# self.save(ignore_permissions=True)
		return True

	@frappe.whitelist()
	def scan_serial_no_action(self, barcode):
		serial_no = frappe.db.get_value(
			"Serial No",
			{"name": barcode, "status": "Active"},
			["name", "item_code", "warehouse", "custom_bom_no"],
			as_dict=True,
		)
		if not serial_no:
			frappe.throw(_("Active Serial Number {0} not found.").format(barcode))

		if serial_no.warehouse != self.warehouse:
			frappe.throw(
				_("Serial Number {0} is not in Source Warehouse {1}.").format(
					barcode, self.warehouse
				)
			)

		# Refining is only allowed for serials of the user's own (Source) department,
		# OR serials already in the refining department's warehouse (previously
		# transferred for refining).
		if self.department and serial_no.warehouse:
			sn_dept = frappe.db.get_value(
				"Warehouse", serial_no.warehouse, "department"
			)
			refining_dept = (
				frappe.db.get_value("Warehouse", self.refining_warehouse, "department")
				if self.refining_warehouse
				else self.refining_department
			)
			if sn_dept and sn_dept != self.department and sn_dept != refining_dept:
				frappe.throw(
					_(
						"Serial Number {0} belongs to department {1}, which does not "
						"match the Source Department {2}. Refining is not allowed."
					).format(barcode, sn_dept, self.department)
				)

		self._throw_if_variant_restricted(serial_no.item_code)

		for row in self.serial_no_details:
			if row.serial_number == serial_no.name:
				frappe.throw(
					_("Serial Number {0} is already added.").format(serial_no.name)
				)

		# Fetch weights from the serial's OWN as-built BOM (custom_bom_no): each piece has
		# its own BOM, and any active BOM for the design item would pull a different
		# piece's weights. Fall back to the item's active BOM only when the serial has none.
		bom_no = serial_no.custom_bom_no or frappe.db.get_value(
			"BOM", {"item": serial_no.item_code, "is_active": 1}, "name"
		)
		bom_details = (
			frappe.db.get_value(
				"BOM",
				bom_no,
				[
					"name",
					"metal_weight",
					"metal_and_finding_weight",
					"gross_weight",
				],
				as_dict=True,
			)
			if bom_no
			else None
		)

		gross_weight = bom_details.gross_weight if bom_details else 0.0
		net_weight = bom_details.metal_and_finding_weight if bom_details else 0.0
		# Prefer the BOM's metal weight (gold alloy only), but fall back to net
		# (metal+finding) weight — many BOMs carry metal_and_finding_weight with a blank
		# metal_weight, and using 0 would silently drop the serial from recovery.
		metal_weight = (
			flt(bom_details.metal_weight) or flt(bom_details.metal_and_finding_weight)
			if bom_details
			else 0.0
		)
		purity = self.get_item_purity(serial_no.item_code)
		# Pure gold is computed from the METAL weight (gold alloy only), NOT the
		# metal-and-finding (net) weight: findings are not gold, and applying the gold
		# purity to them over-counts the recoverable metal.
		pure_weight = flt(metal_weight) * flt(purity) / 100.0 if purity else 0.0

		# Populate metal_weight/metal_purity/quantity too — leaving them blank made the
		# serial table show 0 metal weight and an empty purity/quantity next to a
		# populated net/gross weight, which reads as a quantity mismatch.
		self.append(
			"serial_no_details",
			{
				"serial_number": serial_no.name,
				"item_code": serial_no.item_code,
				"metal_weight": metal_weight,
				"metal_purity": purity,
				"pure_weight": pure_weight,
				"gross_weight": gross_weight,
				"net_weight": net_weight,
				"pcs": 1,
				"quantity": 1,
			},
		)
		self.scan_serial_no = ""
		self.build_material_table()
		# self.save(ignore_permissions=True)
		return True

	@frappe.whitelist()
	def scan_scrap_qr_action(self, barcode):
		batch = frappe.db.get_value(
			"Batch", {"name": barcode}, ["name", "item"], as_dict=True
		)

		if batch and batch.item:
			if self._is_blocked_customer_batch(batch.name):
				frappe.throw(
					_(
						"Batch {0} belongs to a customer blocked from Scrap and Unused/Loose "
						"Material refining."
					).format(frappe.bold(batch.name))
				)
			qty = 1.0
			if self.warehouse:
				allocs = self.allocate_fifo_batches(
					batch.item, self.warehouse, 9999999, throw_if_missing=False
				)
				for a in allocs:
					if a.get("batch_no") == batch.name:
						qty = flt(a.get("qty"))
						break
			self._throw_if_variant_restricted(batch.item)
			self.append(
				"material_items",
				{
					"item_code": batch.item,
					"warehouse": self.warehouse,
					"qty": qty if qty > 0 else 1.0,
					"batch_no": batch.name,
					"use_serial_batch_fields": 1,
					"source_type": SOURCE_TYPE_UNUSED,
				},
			)
			return

		item = frappe.db.get_value("Item", {"name": barcode}, "name")
		if item:
			self._throw_if_variant_restricted(item)
			qty = 1.0
			if self.warehouse:
				bin_qty = frappe.db.get_value(
					"Bin",
					{"item_code": item, "warehouse": self.warehouse},
					"actual_qty",
				)
				if bin_qty:
					qty = flt(bin_qty)
					if frappe.db.get_value("Item", item, "has_batch_no"):
						allocs = self.allocate_fifo_batches(
							item, self.warehouse, 9999999, throw_if_missing=False
						)
						if allocs:
							qty = sum([flt(a.get("qty")) for a in allocs])

			self.append(
				"material_items",
				{
					"item_code": item,
					"warehouse": self.warehouse,
					"qty": qty if qty > 0 else 1.0,
					"use_serial_batch_fields": 1,
					"source_type": SOURCE_TYPE_UNUSED,
				},
			)
			return

		frappe.throw(_("Batch or Item {0} not found.").format(barcode))

	def build_material_table(self):
		"""Consolidate materials from source documents (MWO, SN, etc.) into material_items."""
		self.set("material_items", [])

		if self.refining_type == "Work Order Refining":
			# Work Order material physically sits in the warehouse where it was reserved (the
			# Stock Reservation Entry warehouse), not necessarily the MOP department's
			# Manufacturing warehouse — sourcing from the MOP warehouse raised "stock is not
			# available". Each MWO's balance is collected FIRST because its (item_code,
			# batch_no) keys decide which reservations are in scope (see _source_mwo_sre_rows),
			# so the warehouse map cannot be built before them.
			mwo_balances = []
			wanted_keys = set()
			for mwo_row in self.mwo_details:
				# Read the material the MWO currently holds via get_current_mop_balance_rows() —
				# the running balance (qty_after_transaction_batch_based), the same source of truth
				# get_material_wt() uses for the operation's weight fields. A SUM(qty_change)
				# diverges from it when MOP Log rows carry baseline clones (qty_change=0 with
				# non-zero running balances) during inter-operation handoffs.
				#
				# Queried per the LAST NOT-STARTED Manufacturing Operation on the MWO — where the
				# material physically sits — falling back to the MWO's manufacturing_operation.
				last_mop = (
					frappe.db.get_value(
						"Manufacturing Operation",
						{
							"manufacturing_work_order": mwo_row.manufacturing_work_order,
							"status": "Not Started",
						},
						"name",
						order_by="creation desc",
					)
					or mwo_row.manufacturing_operation
				)

				if not last_mop:
					continue

				from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
					get_current_mop_balance_rows,
				)

				balance_rows = get_current_mop_balance_rows(
					last_mop,
					include_fields=[
						"item_code",
						"qty_after_transaction_batch_based as qty",
						"batch_no",
						"to_warehouse",
						"manufacturing_operation",
					],
				)

				# Derive source warehouse from the MOP's department
				mop_dept = frappe.db.get_value(
					"Manufacturing Operation", last_mop, "department"
				)
				mop_warehouse = (
					frappe.db.get_value(
						"Warehouse",
						{"department": mop_dept, "warehouse_type": "Manufacturing"},
						"name",
					)
					if mop_dept
					else None
				)

				mwo_balances.append(
					(
						mwo_row.manufacturing_work_order,
						last_mop,
						mop_warehouse,
						balance_rows,
					)
				)
				for row in balance_rows:
					if flt(row.get("qty"), 3) > 0:
						wanted_keys.add((row.get("item_code"), row.get("batch_no")))

			sre_wh_map = self._mwo_sre_warehouse_map(keys=wanted_keys)

			for mwo, last_mop, mop_warehouse, balance_rows in mwo_balances:
				for row in balance_rows:
					qty = flt(row.get("qty"), 3)
					if qty <= 0:
						continue
					item_code = row.get("item_code")
					purity = self.get_item_purity(item_code)
					if not purity:
						frappe.throw(
							_(
								"Metal Purity is mandatory for Item {0}. Please check Item Variant Attribute details."
							).format(frappe.bold(item_code))
						)
					uom = frappe.db.get_value("Item", item_code, "stock_uom")
					self.append(
						"material_items",
						{
							"item_code": item_code,
							"batch_no": row.get("batch_no"),
							"warehouse": self._resolve_mwo_source_warehouse(
								mwo,
								item_code,
								row.get("batch_no"),
								qty,
								sre_wh_map,
								row.get("to_warehouse"),
								mop_warehouse,
							),
							"qty": qty,
							"uom": uom,
							"source_type": "MWO",
							"purity": purity,
							"manufacturing_work_order": mwo,
							"manufacturing_operation": row.get(
								"manufacturing_operation"
							)
							or last_mop,
						},
					)

		if self.refining_type == "Serial Number Refining":
			# EXTERNAL serial refining lists only what is actually refined (the BOM metal,
			# findings and stones). The DESIGN CODE is kept out of the table: its qty is a PIECE
			# COUNT while every weight consumer reads material_items.qty as GRAMS, so each
			# scanned piece added 1 g to qty_to_refine, the pure weight and the billed weight.
			# The serial still travels to the refiner — its movement is driven off
			# serial_no_details (see _serial_movement_rows).
			drop_design_code = bool(cint(self.is_external))
			for sn_row in self.serial_no_details:
				purity = self.get_item_purity(sn_row.item_code)
				if not purity:
					frappe.throw(
						_(
							"Metal Purity is mandatory for Item {0}. Please check Item Variant Attribute details."
						).format(frappe.bold(sn_row.item_code))
					)
				if not drop_design_code:
					# Internal: the FG item row is what the Material Transfer SE moves
					# (the FG physically travels with its serial to the refinery).
					self.append(
						"material_items",
						{
							"item_code": sn_row.item_code,
							"warehouse": self.warehouse,
							"qty": sn_row.pcs,
							"uom": "Nos",
							"serial_no": sn_row.serial_number,
							"source_type": "Serial Number",
							"purity": purity,
						},
					)

				# Also add the BOM components for visibility (they will be skipped during transfer/repack)
				bom_no = frappe.db.get_value(
					"Serial No", sn_row.serial_number, "custom_bom_no"
				)
				if not bom_no:
					bom_no = frappe.db.get_value(
						"BOM", {"item": sn_row.item_code, "is_active": 1}, "name"
					)

				if bom_no:
					bom_items = frappe.db.get_all(
						"BOM Item",
						filters={"parent": bom_no},
						fields=["item_code", "qty", "stock_qty", "uom", "stock_uom"],
					)
					for b_item in bom_items:
						# ERPNext allows a BOM to list its own parent item once, and this app never strips
						# it. That row is the DESIGN CODE again — a piece count, not meltable grams — and
						# the consolidation key below includes serial_no (set on the FG row, blank here) so
						# the two never merge. Dropped on the external path only, leaving internal weights
						# and the internal recovery distribution untouched.
						if drop_design_code and b_item.item_code == sn_row.item_code:
							continue
						self.append(
							"material_items",
							{
								"item_code": b_item.item_code,
								"warehouse": self.warehouse,
								"qty": flt(b_item.stock_qty) or flt(b_item.qty),
								"uom": b_item.stock_uom or b_item.uom,
								"source_type": "BOM Component",
								"purity": self.get_item_purity(b_item.item_code),
							},
						)

		if self.refining_type == "Scrap Refining":
			# Fetch ALL loss items from the department's Scrap warehouse
			if self.multiple_department:
				for d_row in self.refining_department_detail:
					self._fetch_loss_items_from_dept(d_row.department)
			else:
				self._fetch_loss_items_from_dept(self.department)

			# Fallback: when the department lookup found nothing (no department, or no Scrap
			# warehouse on it) but self.warehouse IS set, fetch from self.warehouse so this and
			# fetch_dust_balance stay in sync.
			if not self.material_items and self.warehouse:
				self._fetch_loss_items_from_warehouse(self.warehouse)

		# One hook for all three source branches (MWO / Serial / Scrap), and it runs BEFORE
		# the consolidation below so dropped rows never reach the grouping.
		self._drop_restricted_material_rows()

		# Consolidate the source materials item-wise for display (per the Refining SOP:
		# "If multiple references are entered, quantities are merged item-wise"), keyed by
		# (item_code, serial_no, warehouse, row_batch). row_batch is None for Serial /
		# Unused-Loose-Material / plain-scrap rows, so one item spread across several FIFO
		# batches shows as ONE line; the physical allocation is not lost — submit-time
		# create_material_transfer_se / create_repack_se FIFO-allocate any batch-less row.
		# Employee-filtered scrap and Work Order rows keep their batch (see below).
		item_group_cache = {}
		grouped_items = {}
		for item in self.material_items:
			# Employee-filtered scrap rows are per-batch and MUST stay per-batch: keep batch_no
			# in the grouping key and payload so they neither merge across batches nor lose
			# their batch, which would make submit re-FIFO across ALL batches and diverge from
			# System Quantity. Every other row keeps the item-wise merge (row_batch is a
			# constant None there, so the key collapses to the old 3-tuple).
			dust_batch_row = bool(
				self.refining_type == "Scrap Refining"
				and self.employee
				and item.get("batch_no")
			)
			# Work Order material is sourced PER BATCH from the warehouse that physically holds
			# that batch (_resolve_mwo_source_warehouse). Merging the batches into one
			# batch-less line would sum quantities living in DIFFERENT warehouses and re-FIFO
			# the total against whichever one the line landed on. Keeping the batch also
			# subsumes the customer-owned case: customer material must be consumed from (and
			# tagged with) the customer's OWN batch (see _stamp_batch_ownership).
			mwo_batch_row = bool(
				self.refining_type == "Work Order Refining" and item.get("batch_no")
			)
			row_batch = item.batch_no if (dust_batch_row or mwo_batch_row) else None
			key = (item.item_code, item.serial_no, item.warehouse, row_batch)
			if item.item_code not in item_group_cache:
				item_group_cache[item.item_code] = item.get(
					"item_group"
				) or frappe.db.get_value("Item", item.item_code, "item_group")
			if key not in grouped_items:
				grouped_items[key] = {
					"item_code": item.item_code,
					"serial_no": item.serial_no,
					"warehouse": item.warehouse,
					"batch_no": row_batch,
					"qty": item.qty,
					"uom": item.uom,
					"source_type": item.source_type,
					"is_consumable": item.get("is_consumable"),
					"item_group": item_group_cache[item.item_code],
					"purity": item.purity,
					"manufacturing_work_order": item.manufacturing_work_order,
					"manufacturing_operation": item.manufacturing_operation,
				}
			else:
				grouped_items[key]["qty"] += item.qty

		self.set("material_items", [])
		for key, item_dict in grouped_items.items():
			self.append("material_items", item_dict)

	@frappe.whitelist()
	def receive_materials(self):
		if self.status != "Submitted":
			frappe.throw(_("Can only receive materials if status is Submitted."))

		self.db_set("status", "Received")

		# Create duplicate entry per SOP
		duplicate = frappe.copy_doc(self)
		duplicate.status = "Draft"
		duplicate.parent_refining_entry = self.name
		if hasattr(duplicate, "repack_se"):
			duplicate.repack_se = None
		if hasattr(duplicate, "receiving_se"):
			duplicate.receiving_se = None
		if hasattr(duplicate, "transfer_se"):
			duplicate.transfer_se = None
		duplicate.insert(ignore_permissions=True)
		frappe.msgprint(
			_("Duplicate Refining Entry {0} created for Refining Processing.").format(
				duplicate.name
			)
		)
		return duplicate.name

	@frappe.whitelist()
	def generate_recovery_table(self, total_recovered_weight=None):
		"""Distribute gold recovery in proportion to each karat's PURE gold content."""
		self.set("gold_recovery_details", [])
		self.auto_classify_recoverable_non_metal()

		input_purity_map = {}
		input_item_map = {}
		if self.refining_type == "Serial Number Refining":
			bom_components = [
				item
				for item in self.material_items
				if item.get("source_type") == "BOM Component"
			]
			if bom_components:
				for item in bom_components:
					# External refining melts the gold alloy INCLUDING findings: exclude only the
					# returned stones AND the self-referential FG serial BOM row (a piece count, not
					# meltable grams), the same predicate _compute_input_pure_weight uses, so the
					# distribution's input weight matches the Recovery Summary.
					if cint(self.is_external) and not (
						self.is_gold_item(item.item_code)
						and not self._is_returned_intact(item.item_code)
					):
						continue
					if item.purity:
						pct = frappe.db.get_value(
							"Attribute Value", item.purity, "purity_percentage"
						)
						if pct:
							pct_flt = flt(pct)
							input_purity_map.setdefault(pct_flt, 0.0)
							input_purity_map[pct_flt] += flt(item.qty)
							input_item_map[pct_flt] = item.item_code
			else:
				for sn in self.serial_no_details:
					purity = self.get_item_purity(sn.item_code)
					if purity:
						pct = frappe.db.get_value(
							"Attribute Value", purity, "purity_percentage"
						)
						if pct:
							pct_flt = flt(pct)
							input_purity_map.setdefault(pct_flt, 0.0)
							input_purity_map[pct_flt] += flt(sn.metal_weight)
							input_item_map[pct_flt] = sn.item_code
		else:
			for item in self.material_items:
				# External refining returns diamonds/gemstones intact; keep them out of the
				# gold-recovery distribution. Gold findings ARE melted (internal and external
				# alike), so they stay in and contribute their karat's pure gold.
				if cint(self.is_external) and self._is_returned_intact(item.item_code):
					continue
				if item.purity:
					pct = frappe.db.get_value(
						"Attribute Value", item.purity, "purity_percentage"
					)
					if pct:
						pct_flt = flt(pct)
						input_purity_map.setdefault(pct_flt, 0.0)
						input_purity_map[pct_flt] += flt(item.qty)
						input_item_map[pct_flt] = item.item_code

		# Recovered gold is pure 24KT, so it is split in proportion to each karat's PURE
		# gold content, not its gross input weight: a gross split can assign an 18KT row
		# more recovered pure gold than it contained (recovery > 100%) while the 22KT row
		# shows a matching artificial loss.
		total_pure_input_weight = sum(
			weight * (flt(pct) / 100.0) for pct, weight in input_purity_map.items()
		)
		total_recovered_weight = self.get_recovered_gold_total(total_recovered_weight)
		purity_maps = self.get_purity_distribution_maps(
			input_purity_map, input_item_map
		)

		for pmap in purity_maps:
			pmap_pct = flt(pmap.purity_percentage)
			input_weight = input_purity_map.get(pmap_pct, 0)
			if input_weight <= 0:
				continue

			pure_gold_weight = input_weight * (pmap_pct / 100.0)
			recovered_weight = self.get_proportional_recovery_weight(
				pure_gold_weight, total_pure_input_weight, total_recovered_weight
			)
			row_loss = flt(
				max(flt(pure_gold_weight, 3) - flt(recovered_weight, 3), 0), 3
			)
			row_pct = flt(
				(flt(recovered_weight, 3) / flt(pure_gold_weight, 3)) * 100.0
				if flt(pure_gold_weight, 3)
				else 0.0,
				2,
			)
			# Never let 2-decimal display rounding show "100.00" next to a non-zero
			# loss (same guard as calculate_totals).
			if row_loss > 0 and row_pct > 99.99:
				row_pct = 99.99
			self.append(
				"gold_recovery_details",
				{
					"karat": pmap.karat,
					"purity_percentage": pmap.purity_percentage,
					"item_code": pmap.item_template,
					"input_weight": flt(input_weight, 3),
					"pure_gold_weight": flt(pure_gold_weight, 3),
					"recovered_weight": flt(recovered_weight, 3),
					# Loss is only meaningful when recovered < pure gold content
					"loss_weight": row_loss,
					"recovery_pct": row_pct,
				},
			)

		if total_recovered_weight:
			self.populate_refined_gold_from_distribution()
			self.calculate_totals()

		self.db_set("status", "Classified")
		self.update_children()
		self.db_update()

	@frappe.whitelist()
	def start_refining(self):
		if self.status != "Classified":
			frappe.throw(_("Materials must be classified before starting refining."))
		self.db_set("status", "Refining In Progress")

	@frappe.whitelist()
	def distribute_recovered_gold(self, total_recovered_weight=None):
		"""Apply SOP proportional split after actual recovered gold is known."""
		if not self.gold_recovery_details:
			self.generate_recovery_table(total_recovered_weight=total_recovered_weight)

		total_input_weight = sum(
			flt(row.input_weight) for row in self.gold_recovery_details
		)
		# Refining yields pure 24KT gold, so the entered recovery is capped by the PURE
		# gold content of the input, not the gross input weight.
		total_pure_input_weight = sum(
			flt(row.pure_gold_weight) for row in self.gold_recovery_details
		)
		total_recovered_weight = self.get_recovered_gold_total(total_recovered_weight)
		if total_input_weight <= 0:
			frappe.throw(_("Gold input weight is required for proportional recovery."))
		if total_pure_input_weight <= 0:
			frappe.throw(
				_("Pure gold input weight is required for proportional recovery.")
			)
		if total_recovered_weight <= 0:
			frappe.throw(
				_("Recovered gold weight is required for proportional recovery.")
			)
		if total_recovered_weight > total_pure_input_weight + 0.1:
			frappe.throw(
				_(
					"Recovered 24KT gold weight ({0}) cannot exceed the pure gold "
					"input weight ({1})."
				).format(
					flt(total_recovered_weight, 3), flt(total_pure_input_weight, 3)
				)
			)

		# Split in proportion to each row's PURE gold content (see generate_recovery_table).
		# Pre-compute each row's split at FULL precision, then round: loss_weight and
		# recovery_pct are derived from the full-precision share vs the full-precision pure
		# content, so a sub-milligram rounding artifact reads as 0.000 loss / 100% instead
		# of a contradictory 0.001 loss on a fully recovered row. The rounding remainder is
		# pushed onto the largest row so the persisted weights still sum to the entered
		# total.
		full_share = {
			id(row): self.get_proportional_recovery_weight(
				flt(row.pure_gold_weight),
				total_pure_input_weight,
				total_recovered_weight,
			)
			for row in self.gold_recovery_details
		}
		row_weights = {rid: flt(v, 3) for rid, v in full_share.items()}
		remainder = flt(flt(total_recovered_weight, 3) - sum(row_weights.values()), 3)
		if remainder and row_weights:
			largest = max(row_weights, key=row_weights.get)
			row_weights[largest] = flt(row_weights[largest] + remainder, 3)

		for row in self.gold_recovery_details:
			recovered_weight = row_weights[id(row)]
			pure_full = flt(row.pure_gold_weight)
			pure_gold_weight = flt(pure_full, 3)
			# Compute loss and recovery % using the rounded display values so the
			# percentage exactly matches the weights the user sees on screen.
			loss_weight = flt(max(pure_gold_weight - recovered_weight, 0), 3)
			recovery_pct = flt(
				(recovered_weight / pure_gold_weight) * 100.0
				if pure_gold_weight
				else 0.0,
				2,
			)
			# Never let 2-decimal display rounding show "100.00" next to a non-zero
			# loss (same guard as calculate_totals).
			if loss_weight > 0 and recovery_pct > 99.99:
				recovery_pct = 99.99

			# Update in-memory for downstream use
			row.recovered_weight = recovered_weight
			row.loss_weight = loss_weight
			row.recovery_pct = recovery_pct
			row.pure_gold_weight = pure_gold_weight

			# Persist directly to DB, bypassing validate_update_after_submit
			row.db_set(
				{
					"recovered_weight": recovered_weight,
					"loss_weight": loss_weight,
					"recovery_pct": recovery_pct,
					"pure_gold_weight": pure_gold_weight,
				}
			)

		# Rebuild refined_gold child table via DB operations
		self._rebuild_refined_gold_via_db()

		# Recalculate and persist parent totals
		self._recalculate_and_persist_totals()

		self.db_set("status", "Recovery Entered")

	def _rebuild_refined_gold_via_db(self):
		"""Delete existing refined_gold rows and insert new ones based on recovery details."""
		# Delete existing refined_gold rows for this parent
		frappe.db.delete("Refined Gold", {"parent": self.name})

		# Refining always yields pure 24KT regardless of the input karat, and the repack SE
		# books the single 24KT item — so Refined Gold must show 24KT (the pure output),
		# not the input karat.
		pure_item = self._get_pure_gold_24kt_item()
		pure_karat = self._get_pure_gold_karat()

		self.set("refined_gold", [])
		for idx, row in enumerate(self.gold_recovery_details, start=1):
			if flt(row.recovered_weight) <= 0:
				continue

			# The operator-entered recovered gold is already pure 24KT, so the recovered
			# weight IS the pure fine weight (no input-karat reduction).
			pure_weight = flt(row.recovered_weight, 3)

			child = self.append(
				"refined_gold",
				{
					"item_code": pure_item,
					"refining_gold_weight": row.recovered_weight,
					"pure_weight": pure_weight,
					"metal_purity": pure_karat,
				},
			)
			child.db_insert()

	def _recalculate_and_persist_totals(self):
		"""Recalculate summary fields and persist via db_set."""
		gross_pure_weight, expected_recovery = self._compute_input_pure_weight()

		refined_fine_weight = 0.0
		actual_recovery = 0.0
		for gold in self.refined_gold:
			actual_recovery += flt(gold.refining_gold_weight)
			refined_fine_weight += flt(gold.pure_weight) or flt(
				gold.refining_gold_weight
			)

		# Round to display precision (3 decimals) before computing derived values
		# so the percentage matches what the user sees on screen.
		gross_pure_weight = flt(gross_pure_weight, 3)
		expected_recovery = flt(expected_recovery, 3)
		refined_fine_weight = flt(refined_fine_weight, 3)
		actual_recovery = flt(actual_recovery, 3)

		# Keep the Recovery Summary in lockstep with the per-karat Gold Recovery Details
		# table (sum-of-rounded, not round-of-sum). See calculate_totals for the rationale.
		if self.gold_recovery_details:
			gross_pure_weight = flt(
				sum(flt(r.pure_gold_weight, 3) for r in self.gold_recovery_details), 3
			)
			expected_recovery = gross_pure_weight
			refining_loss = flt(
				sum(flt(r.loss_weight, 3) for r in self.gold_recovery_details), 3
			)
		else:
			# Clamp at 0: recovered 24KT gold can slightly exceed the computed pure input
			# (rounding / assay variance) and must never book a negative loss.
			refining_loss = flt(max(gross_pure_weight - refined_fine_weight, 0.0), 3)
		recovery_percentage = min(
			(refined_fine_weight / expected_recovery) * 100.0
			if expected_recovery > 0
			else 0.0,
			100.0,
		)
		# Never let 2-decimal display rounding show "100.00" next to a non-zero loss
		# (same guard as calculate_totals).
		if refining_loss > 0 and recovery_percentage > 99.99:
			recovery_percentage = 99.99

		self.db_set(
			{
				"gross_pure_weight": flt(gross_pure_weight, 3),
				"expected_recovery": flt(expected_recovery, 3),
				"refined_fine_weight": flt(refined_fine_weight, 3),
				"actual_recovery": flt(actual_recovery, 3),
				"refining_loss": flt(refining_loss, 3),
				"recovery_percentage": flt(recovery_percentage, 2),
			}
		)

	@frappe.whitelist()
	def verify_recovery(self):
		self.validate_recovery_distribution()
		self.validate_recovered_non_metal()
		self.db_set("status", "Recovery Verified")

	@frappe.whitelist()
	def complete_refining(self):
		if self.status != "Recovery Verified":
			frappe.throw(_("Recovery must be verified before completing."))

		frappe.publish_progress(
			10,
			title="Completing Refining",
			description="Creating Repack Stock Entry...",
		)
		self.create_repack_se()

		# Verify repack SE was actually created before proceeding
		if not self.repack_se:
			frappe.throw(
				_("Repack Stock Entry could not be created. Cannot complete refining.")
			)

		frappe.publish_progress(
			50,
			title="Completing Refining",
			description="Updating source dependencies...",
		)
		if self.refining_type == "Work Order Refining":
			# SOP: Current and future operation quantities -> 0 for every refined MWO.
			from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
				get_current_mop_balance_rows,
				get_last_mop_index,
			)

			for mwo in self.mwo_details:
				frappe.db.set_value(
					"Manufacturing Work Order",
					mwo.manufacturing_work_order,
					"qty",
					0,
				)
				# Zero out all operations that are not already Finished
				# We zero all weight buckets so the UI (gross_wt) and ledger stay at 0.
				frappe.db.sql(
					"""
					UPDATE `tabManufacturing Operation`
					SET qty = 0, gross_wt = 0, net_wt = 0, finding_wt = 0,
					    diamond_wt = 0, diamond_wt_in_gram = 0, diamond_pcs = 0,
					    gemstone_wt = 0, gemstone_wt_in_gram = 0, gemstone_pcs = 0,
					    other_wt = 0, prev_gross_wt = 0
					WHERE manufacturing_work_order = %s
					AND status != 'Finished'
					""",
					(mwo.manufacturing_work_order,),
				)

				# Zero the LEDGER for every operation of this MWO that still holds a
				# balance -- not just MWO.manufacturing_operation. Metal strands on
				# earlier operations routinely (a rework loop, a short return), and a
				# balance left behind here survives refining: the next operation's
				# opening balance picks it up and inflates a freshly issued weight.
				# That is how a re-cast of 3.210g came out as 3.220g.
				ops = frappe.get_all(
					"MOP Log",
					filters={
						"manufacturing_work_order": mwo.manufacturing_work_order,
						"is_cancelled": 0,
						"manufacturing_operation": ["is", "set"],
					},
					pluck="manufacturing_operation",
					distinct=True,
				)
				current_op = frappe.db.get_value(
					"Manufacturing Work Order",
					mwo.manufacturing_work_order,
					"manufacturing_operation",
				)
				if current_op:
					ops.append(current_op)
				ops = sorted({o for o in ops if o})

				if not ops:
					# Never silently skip: a refined MWO with no resolvable operation
					# means the ledger cannot be zeroed, and the next issue against it
					# will open on whatever balance survives.
					frappe.log_error(
						title="Refining: no operation to zero",
						message=(
							f"Refining Entry {self.name}: Manufacturing Work Order "
							f"{mwo.manufacturing_work_order} has no Manufacturing "
							"Operation and no active MOP Log rows, so its ledger could "
							"not be zeroed. Any later issue against this MWO will not "
							"start from a verified zero balance."
						),
					)
					continue

				# Create a 0-balance MOP Log for every active item in each operation
				# so that future operations read a 0 balance from the ledger.
				for op in ops:
					balance_rows = get_current_mop_balance_rows(op)
					if not balance_rows:
						continue
					last_idx = get_last_mop_index(op) or 0
					for bal in balance_rows:
						qty = flt(bal.get("qty_after_transaction_batch_based"))
						pcs = cint(bal.get("pcs_after_transaction_batch_based"))
						if qty <= 0 and pcs <= 0:
							continue

						ml = frappe.new_doc("MOP Log")
						ml.item_code = bal.get("item_code")
						ml.batch_no = bal.get("batch_no")
						ml.manufacturing_work_order = mwo.manufacturing_work_order
						ml.manufacturing_operation = op
						ml.voucher_type = self.doctype
						ml.voucher_no = self.name

						ml.qty_change = -qty
						ml.pcs_change = -pcs
						ml.qty_after_transaction = 0
						ml.qty_after_transaction_item_based = 0
						ml.qty_after_transaction_batch_based = 0
						ml.pcs_after_transaction = 0
						ml.pcs_after_transaction_item_based = 0
						ml.pcs_after_transaction_batch_based = 0

						ml.flow_index = last_idx + 1
						ml.is_cancelled = 0

						# We use ignore_permissions to ensure background processing succeeds
						ml.flags.ignore_permissions = True
						ml.insert(ignore_permissions=True)

			# Work Order cancellation / recasting is advisory only: the chosen action is
			# recorded on the entry, but the actual Work Order cancel or transfer to the
			# Casting department is performed manually by Central / Manufacturing.
			if self.work_order_action:
				frappe.msgprint(
					_(
						"Refining complete. Work Order action '{0}' must be performed "
						"manually by the Central / Manufacturing department."
					).format(self.work_order_action),
					indicator="orange",
					alert=True,
				)

		elif self.refining_type == "Serial Number Refining":
			for sn in self.serial_no_details:
				frappe.db.set_value("Serial No", sn.serial_number, "status", "Inactive")
				# Deactivate the serial's OWN as-built BOM (custom_bom_no) — the physical piece has
				# been melted down. An item can have several active per-serial BOMs, so the design
				# item's generic active BOM would both leave this piece's BOM active and disable one
				# other pieces still depend on. Fall back to it only when the serial has no BOM.
				bom = frappe.db.get_value(
					"Serial No", sn.serial_number, "custom_bom_no"
				) or frappe.db.get_value(
					"BOM", {"item": sn.item_code, "is_active": 1}, "name"
				)
				if bom and frappe.db.get_value("BOM", bom, "is_active"):
					frappe.db.set_value("BOM", bom, "is_active", 0)

		frappe.publish_progress(
			80,
			title="Completing Refining",
			description="Handling Refining Loss...",
		)
		# If there is refining loss, convert to dust and move to scrap warehouse
		if self.refining_loss > 0:
			self.create_scrap_transfer_se()

		self.db_set("status", "Completed")
		frappe.publish_progress(
			100,
			title="Completing Refining",
			description="Refining Completed Successfully",
		)

	@frappe.whitelist()
	def transfer_recovered_materials(self):
		if self.status != "Completed":
			frappe.throw(_("Can only transfer if Completed."))

		frappe.publish_progress(
			10,
			title="Transferring Materials",
			description="Finding Central RM Warehouse...",
		)
		# Always transfer recovered materials to Central RM warehouse
		target_warehouse = self._get_central_rm_warehouse()
		if not target_warehouse:
			frappe.throw(
				_(
					"Cannot determine Central RM Warehouse. Please ensure a warehouse with "
					"warehouse_type 'Raw Material' exists for the Central department."
				)
			)

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.company = self.company
		se.custom_refining_entry = self.name
		se.auto_created = 1

		frappe.publish_progress(
			30,
			title="Transferring Materials",
			description="Processing pure gold...",
		)
		# Transfer all recovered gold as 24KT pure gold (matching repack output)
		pure_gold_item = self._get_pure_gold_24kt_item()
		total_gold_weight = sum(flt(g.refining_gold_weight) for g in self.refined_gold)
		if total_gold_weight > 0 and pure_gold_item:
			batch_no = None
			for b in self.batch_tracking:
				if b.output_item == pure_gold_item:
					batch_no = b.output_batch
					break

			# Fallback: look up available batch from refining warehouse
			if not batch_no and frappe.db.get_value(
				"Item", pure_gold_item, "has_batch_no"
			):
				batch_no = self._get_available_batch(
					pure_gold_item, self.refining_warehouse
				)

			se.append(
				"items",
				{
					"item_code": pure_gold_item,
					"qty": flt(total_gold_weight, 3),
					"uom": "Gram",
					"s_warehouse": self.refining_warehouse,
					"t_warehouse": target_warehouse,
					"batch_no": batch_no,
					"use_serial_batch_fields": 1,
				},
			)

		frappe.publish_progress(
			50,
			title="Transferring Materials",
			description="Processing diamonds...",
		)
		for dia in self.recovered_diamond:
			dia_qty = flt(dia.recovered_weight)
			if dia_qty <= 0:
				continue
			conv_item = self.convert_diamond_item_code(dia.item)
			batch_no = None
			for b in self.batch_tracking:
				if b.output_item == conv_item:
					batch_no = b.output_batch
					break

			if not batch_no and frappe.db.get_value("Item", conv_item, "has_batch_no"):
				batch_no = self._get_available_batch(conv_item, self.refining_warehouse)

			se.append(
				"items",
				{
					"item_code": conv_item,
					"qty": dia_qty,
					"uom": "Carat",
					"s_warehouse": self.refining_warehouse,
					"t_warehouse": target_warehouse,
					"batch_no": batch_no,
					"use_serial_batch_fields": 1,
				},
			)

		frappe.publish_progress(
			70,
			title="Transferring Materials",
			description="Processing gemstones...",
		)
		for gem in self.recovered_gemstone:
			gem_qty = flt(gem.recovered_weight)
			if gem_qty <= 0:
				continue
			batch_no = None
			for b in self.batch_tracking:
				if b.output_item == gem.item:
					batch_no = b.output_batch
					break

			if not batch_no and frappe.db.get_value("Item", gem.item, "has_batch_no"):
				batch_no = self._get_available_batch(gem.item, self.refining_warehouse)

			se.append(
				"items",
				{
					"item_code": gem.item,
					"qty": gem_qty,
					"uom": "Carat",
					"s_warehouse": self.refining_warehouse,
					"t_warehouse": target_warehouse,
					"batch_no": batch_no,
					"use_serial_batch_fields": 1,
				},
			)

		frappe.publish_progress(
			85,
			title="Transferring Materials",
			description="Submitting Stock Entry...",
		)
		# Keep customer-owned recovered gold tagged Customer Goods on the outbound move to
		# Central RM (the recovered batch was minted Customer Goods in create_repack_se).
		self._stamp_batch_ownership(se)
		se.insert(ignore_permissions=True)
		se.submit()
		self.db_set("transfer_se", se.name)
		self.db_set("status", "Transferred")
		frappe.publish_progress(
			100,
			title="Transferring Materials",
			description="Materials Transferred Successfully",
		)

	# --- Stock Entry Automation ---

	def create_material_transfer_se(self, target_warehouse=None):
		"""Build the Material Transfer moving material_items into the refining warehouse
		(internal flow) or, when ``target_warehouse`` is given (external refining
		sending), into the supplier's warehouse instead."""
		target = target_warehouse or self.refining_warehouse

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.company = self.company
		se.custom_refining_entry = self.name
		# If we queue submission, we don't need auto_created=1 since it'll bypass blockages, but we'll keep it for custom validation bypasses
		se.auto_created = 1
		if target_warehouse:
			# External refining: tag the material as issued to the subcontractor supplier,
			# and carry the entry's manufacturer for the SE pure-qty hook (see
			# receive_from_supplier for the full rationale).
			se.to_subcontractor = self.supplier
			se.manufacturer = self.manufacturer

		precision = 3
		min_qty = 0.001

		# External serial refining keeps the design code out of material_items, so the rows
		# that physically move the scanned serials are synthesised from serial_no_details.
		# Empty for every other case, where movement_rows IS material_items. Feeding them
		# through the loop below keeps the batch / FIFO / customer-block handling.
		movement_rows = list(self.material_items) + self._serial_movement_rows()

		# Cache item attributes to prevent repetitive DB calls and speed up insertion
		item_batch_map = {}
		for item in movement_rows:
			if item.item_code not in item_batch_map:
				item_batch_map[item.item_code] = frappe.db.get_value(
					"Item", item.item_code, "has_batch_no"
				)

		self._dust_shortfalls = []
		block_customer = self.refining_type in (
			"Scrap Refining",
			"Unused/Loose Material Refining",
		)
		# Work Order Refining resolves its rows against PHYSICAL batch stock, not the
		# reservation-netted figure: the batch a MWO row consumes is reserved by the very
		# Stock Reservation Entry this transfer is about to cancel, so netting it out
		# understates the warehouse and throws against stock that is demonstrably there.
		# Same rationale as EOD sync's _eod_physical_batch_qty. Every other type keeps the
		# reservation-aware figure, having no reservations of its own to release.
		ignore_reserved = self.refining_type == "Work Order Refining"
		for item in movement_rows:
			if item.get("source_type") == "BOM Component":
				continue

			# Authoritative guard: never move a blocked customer's explicit batch into Scrap or
			# Unused/Loose Material refining, internal OR external (belt-and-suspenders over
			# validate_customer_block; also covers programmatic callers).
			if (
				block_customer
				and item.get("batch_no")
				and self._is_blocked_customer_batch(item.batch_no)
			):
				frappe.throw(
					_(
						"Batch {0} belongs to a customer blocked from Scrap and Unused/Loose "
						"Material refining."
					).format(frappe.bold(item.batch_no))
				)

			# We no longer skip dust opening items here; we attempt to transfer everything
			# and record any shortfalls (missing stock) for the receipt step.

			# Serial Number Refining: the FG serial item IS transferred to the refining
			# warehouse at submit, like every other refined material, so it is NOT skipped here.
			# Repack then consumes it from the refining warehouse.

			s_wh = item.warehouse or self.warehouse
			has_batch = item_batch_map.get(item.item_code)

			if has_batch:
				use_fifo = True
				if item.batch_no:
					from erpnext.stock.doctype.batch.batch import get_batch_qty

					batch_qty = get_batch_qty(
						batch_no=item.batch_no,
						warehouse=s_wh,
						item_code=item.item_code,
						ignore_reserved_stock=ignore_reserved,
					)
					if batch_qty >= item.qty:
						use_fifo = False
						if flt(item.qty, precision) >= min_qty:
							se.append(
								"items",
								{
									"item_code": item.item_code,
									"qty": item.qty,
									"uom": item.uom,
									"s_warehouse": s_wh,
									"t_warehouse": target,
									"batch_no": item.batch_no,
									"serial_no": item.serial_no,
									"use_serial_batch_fields": 1,
								},
							)

				if use_fifo:
					allocations = self.allocate_fifo_batches(
						item.item_code,
						s_wh,
						item.qty,
						throw_if_missing=(self.refining_type != "Scrap Refining"),
						exclude_blocked_customer=block_customer,
						ignore_reserved_stock=ignore_reserved,
						diagnostics={
							"batch_no": item.batch_no,
							"manufacturing_work_order": item.manufacturing_work_order,
							"company": self.company,
						},
					)

					allocated_qty = sum(flt(a["qty"], precision) for a in allocations)
					if self.refining_type == "Scrap Refining" and allocated_qty < flt(
						item.qty, precision
					):
						self._dust_shortfalls.append(
							{
								"item_code": item.item_code,
								"qty": flt(item.qty, precision) - allocated_qty,
								"uom": item.uom,
								"purity": item.purity,
							}
						)

					for alloc in allocations:
						if flt(alloc["qty"], precision) >= min_qty:
							se.append(
								"items",
								{
									"item_code": item.item_code,
									"qty": alloc["qty"],
									"uom": item.uom,
									"s_warehouse": s_wh,
									"t_warehouse": target,
									"batch_no": alloc["batch_no"],
									"serial_no": item.serial_no,
									"use_serial_batch_fields": 1,
								},
							)
			else:
				# Item has no batch number tracking
				transfer_qty = flt(item.qty, precision)
				if self.refining_type == "Scrap Refining":
					bin_qty = flt(
						frappe.db.get_value(
							"Bin",
							{"item_code": item.item_code, "warehouse": s_wh},
							"actual_qty",
						)
						or 0.0,
						precision,
					)
					transfer_qty = min(transfer_qty, max(0.0, bin_qty))
					if transfer_qty < flt(item.qty, precision):
						self._dust_shortfalls.append(
							{
								"item_code": item.item_code,
								"qty": flt(item.qty, precision) - transfer_qty,
								"uom": item.uom,
								"purity": item.purity,
							}
						)

				if transfer_qty >= min_qty:
					se.append(
						"items",
						{
							"item_code": item.item_code,
							"qty": transfer_qty,
							"uom": item.uom,
							"s_warehouse": s_wh,
							"t_warehouse": target,
							"batch_no": item.batch_no,
							"serial_no": item.serial_no,
							"use_serial_batch_fields": 1,
						},
					)

		# Work Order Refining: release the source MWO Stock Reservation Entries so this
		# Stock Entry can consume the now unreserved stock (otherwise the submit fails with
		# "stock is not available").
		#
		# Runs AFTER every row is resolved: cancelling up front meant an "Insufficient
		# batch stock" throw mid-loop left the reservations released, restored only by the
		# rollback — and a partial commit through the Submission Queue would have stranded
		# the stock unreserved with no transfer to show for it. Row resolution reads
		# physical batch qty via ignore_reserved_stock, so it no longer needs them gone.
		#
		# Still at submit time, NOT on every draft save, so an abandoned draft never loses
		# its reservations. Unconditional (not gated on se.items) so the reservations and
		# the weight zeroing below can never disagree about whether the MWO holds material.
		if self.refining_type == "Work Order Refining":
			self._cancel_source_mwo_sres()

		if se.items:
			# Keep customer-owned metal tagged Customer Goods through the transfer into the
			# refining/supplier warehouse (auto_created SEs skip the generic backfill).
			self._stamp_batch_ownership(se)
			se.insert(ignore_permissions=True)
			se.submit()
			self.db_set("material_transfer_se", se.name)

		# Now that the MWO material is physically in the refinery, zero the source MWO /
		# pending-operation weights. SREs were cancelled just above, respecting the
		# canonical lock order SRE -> SLE -> MOP Log -> Manufacturing Operation.
		if self.refining_type == "Work Order Refining":
			self._zero_source_mwo_and_mop_weights()

	# Weight buckets zeroed on transfer to the refinery. MWO and Manufacturing Operation
	# share the same set (MOP additionally carries gemstone_wt_in_gram).
	_MWO_WEIGHT_FIELDS = (
		"gross_wt",
		"net_wt",
		"finding_wt",
		"diamond_wt",
		"gemstone_wt",
		"other_wt",
		"received_gross_wt",
		"received_net_wt",
		"loss_wt",
		"diamond_wt_in_gram",
		"diamond_pcs",
		"gemstone_pcs",
	)

	# Fields every SRE lookup below needs. `manufacturing_work_order` is the custom Data
	# field the MWO reservation carries (see doc_events/stock_entry.stock_reservation_entry_for_mwo).
	_SRE_FIELDS = (
		"name",
		"item_code",
		"warehouse",
		"has_batch_no",
		"reservation_based_on",
		"manufacturing_work_order",
	)

	def _mwo_sales_order_line(self, mwo):
		"""``(sales_order, sales_order_item)`` behind a MWO.

		MWO reservations are booked against the SALES ORDER LINE held by the MWO's
		Parent Manufacturing Order (`SRE.voucher_type="Sales Order"`,
		`voucher_no=PMO.sales_order`, `voucher_detail_no=PMO.sales_order_item` — see
		``stock_reservation_entry_for_mwo``). Manufacturing Work Order has no
		``sales_order`` field of its own, so it always resolves through the PMO."""
		pmo = frappe.db.get_value(
			"Manufacturing Work Order", mwo, "manufacturing_order"
		)
		if not pmo:
			return None, None
		row = frappe.db.get_value(
			"Parent Manufacturing Order", pmo, ["sales_order", "sales_order_item"]
		)
		return (row[0], row[1]) if row else (None, None)

	def _sre_batches(self, sre_name):
		return [
			sb.batch_no
			for sb in frappe.db.get_all(
				"Serial and Batch Entry",
				filters={"parent": sre_name},
				fields=["batch_no"],
			)
			if sb.batch_no
		]

	@staticmethod
	def _sre_matches_keys(sre, wanted_items, wanted_batches, batches):
		"""Whether a Sales-Order-matched SRE covers material this entry actually moves.

		``wanted_batches`` holds the ``(item_code, batch_no)`` pairs from the MOP
		balance. A batch-based reservation must name one of them; a Qty-based one (or
		an item whose balance rows carry no batch at all) only has to match the item."""
		if sre.item_code not in wanted_items:
			return False
		if not (cint(sre.has_batch_no) and sre.reservation_based_on != "Qty"):
			return True
		item_batches = {b for (i, b) in wanted_batches if i == sre.item_code and b}
		if not item_batches or not batches:
			return True
		return any(b in item_batches for b in batches)

	def _source_mwo_sre_rows(self, keys=None):
		"""Every active Stock Reservation Entry this Refining Entry legitimately owns, as
		``[(mwo, sre_dict), ...]`` — MWO-owned reservations first. Two sources:

		1. SREs carrying a scanned MWO in the custom ``manufacturing_work_order`` field.
		2. SREs on the SAME Sales Order line that no OTHER Manufacturing Work Order
		   claims. EOD sync moves material purely on the strength of its reservations, so
		   a reservation booked against the Sales Order line without the MWO stamp still
		   pins stock this entry must consume. A Sales Order is shared across many MWOs,
		   so these are narrowed twice: to the ``(item_code, batch_no)`` keys the entry
		   actually transfers, and to rows whose ``manufacturing_work_order`` is blank or
		   one of the scanned MWOs — releasing a sibling MWO's reservation would strand
		   its material.

		``keys`` is an iterable of ``(item_code, batch_no)``; ``None`` disables the
		item/batch narrowing.
		"""
		scanned = {
			row.manufacturing_work_order
			for row in self.mwo_details
			if row.manufacturing_work_order
		}
		if not scanned:
			return []

		base_filters = {
			"docstatus": 1,
			"status": ["not in", ("Cancelled", "Delivered")],
		}
		fields = list(self._SRE_FIELDS)
		rows = []
		seen = set()

		# 1. Reservations the scanned MWOs own outright.
		for mwo in sorted(scanned):
			for sre in frappe.db.get_all(
				"Stock Reservation Entry",
				filters={**base_filters, "manufacturing_work_order": mwo},
				fields=fields,
				order_by="creation asc",
			):
				if sre.name in seen:
					continue
				seen.add(sre.name)
				rows.append((mwo, sre))

		# 2. Reservations on the same Sales Order line, unclaimed by a sibling MWO.
		wanted_items = {k[0] for k in keys} if keys is not None else None
		wanted_batches = set(keys) if keys is not None else None
		for mwo in sorted(scanned):
			sales_order, sales_order_item = self._mwo_sales_order_line(mwo)
			if not sales_order:
				continue
			so_filters = {
				**base_filters,
				"voucher_type": "Sales Order",
				"voucher_no": sales_order,
			}
			if sales_order_item:
				so_filters["voucher_detail_no"] = sales_order_item
			for sre in frappe.db.get_all(
				"Stock Reservation Entry",
				filters=so_filters,
				fields=fields,
				order_by="creation asc",
			):
				if sre.name in seen:
					continue
				owner = (sre.manufacturing_work_order or "").strip()
				if owner and owner not in scanned:
					continue
				if keys is not None and not self._sre_matches_keys(
					sre, wanted_items, wanted_batches, self._sre_batches(sre.name)
				):
					continue
				seen.add(sre.name)
				rows.append((mwo, sre))
		return rows

	def _serial_movement_rows(self):
		"""Material-Items-shaped rows for the serialised pieces of an EXTERNAL serial
		refining entry, synthesised from ``serial_no_details``.

		The design code is deliberately absent from ``material_items`` externally — its
		qty is a PIECE COUNT, not refinable grams, so it belongs in no weight, price or
		recovery total (see build_material_table). The piece itself still has to reach the
		refiner and be melted, so both Stock Entry builders append these rows to the
		material rows they iterate.

		Returns ``[]`` for every other case. Rows are ``frappe._dict`` so the builders'
		mixed ``item.attr`` / ``item.get(...)`` access works as on a real child row.
		"""
		if not (
			cint(self.is_external) and self.refining_type == "Serial Number Refining"
		):
			return []

		# An entry submitted BEFORE this change still carries its FG row in
		# material_items. Synthesising a movement row for that serial too would transfer
		# or consume the piece TWICE, so skip any serial the table already represents.
		already_in_table = {
			row.serial_no for row in self.material_items if row.serial_no
		}

		rows = []
		for sn_row in self.serial_no_details:
			if not (sn_row.serial_number and sn_row.item_code):
				continue
			if sn_row.serial_number in already_in_table:
				continue
			rows.append(
				frappe._dict(
					{
						"item_code": sn_row.item_code,
						"warehouse": self.warehouse,
						# One serial is one physical piece; fall back to 1 rather than 0
						# so an API-created row with a blank pcs cannot silently produce
						# an empty Stock Entry.
						"qty": flt(sn_row.pcs) or 1.0,
						"uom": "Nos",
						"serial_no": sn_row.serial_number,
						"batch_no": None,
						"source_type": "Serial Number",
						"purity": sn_row.metal_purity,
						"is_consumable": 0,
						"manufacturing_work_order": None,
						"manufacturing_operation": None,
					}
				)
			)
		return rows

	def _mwo_sre_warehouse_map(self, keys=None):
		"""Map ``(mwo, item_code, batch_no)`` -> ORDERED candidate reservation warehouses,
		with an item-level (``batch_no=None``) fallback entry.

		Work Order material physically sits in the warehouse where it was reserved, so
		build_material_table sources the transfer from here rather than the MOP
		department's Manufacturing warehouse. A CANDIDATE LIST, not a single warehouse: an
		item reserved in several warehouses would otherwise collapse onto whichever SRE
		the unordered query returned last.
		"""
		wh_map = {}

		def _add(key, warehouse):
			if not warehouse:
				return
			bucket = wh_map.setdefault(key, [])
			if warehouse not in bucket:
				bucket.append(warehouse)

		for mwo, sre in self._source_mwo_sre_rows(keys=keys):
			_add((mwo, sre.item_code, None), sre.warehouse)
			if cint(sre.has_batch_no) and sre.reservation_based_on != "Qty":
				for batch_no in self._sre_batches(sre.name):
					_add((mwo, sre.item_code, batch_no), sre.warehouse)
		return wh_map

	def _resolve_mwo_source_warehouse(
		self,
		mwo,
		item_code,
		batch_no,
		qty,
		sre_wh_map,
		to_warehouse,
		mop_warehouse,
	):
		"""Warehouse a Work Order material row is physically transferred FROM.

		A reservation records where stock was *logically* placed; only the Serial and
		Batch Bundle knows where it actually IS, and a stale reservation leaves the SRE
		warehouse holding nothing. Reuses EOD sync's picker (``_pick_eod_source_
		warehouse``): stock already at the MOP's own warehouse wins, else the first
		reservation warehouse that physically covers the qty, else the first candidate —
		which keeps a genuine shortage reporting against the warehouse the reservation
		names.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
			_pick_eod_source_warehouse,
		)

		candidates = list(sre_wh_map.get((mwo, item_code, batch_no)) or [])
		for warehouse in sre_wh_map.get((mwo, item_code, None)) or []:
			if warehouse not in candidates:
				candidates.append(warehouse)

		fallback = to_warehouse or mop_warehouse or self.warehouse
		return (
			_pick_eod_source_warehouse(item_code, batch_no, qty, candidates, fallback)
			or fallback
		)

	def _cancel_source_mwo_sres(self):
		"""Cancel the active Stock Reservation Entries this entry's material sits under, so
		the transfer can consume the now unreserved stock.

		Scope is ``_source_mwo_sre_rows``: every reservation the scanned MWOs own, plus
		the Sales-Order-matched ones no sibling MWO claims, narrowed to the items/batches
		actually in Material Items. Sharing that resolver with ``_mwo_sre_warehouse_map``
		means the warehouse a row was sourced FROM can never be a reservation left
		standing.
		"""
		from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
			cancel_stock_reservation_entries,
		)

		keys = {
			(row.item_code, row.batch_no)
			for row in self.material_items
			if row.item_code
		}
		names = [sre.name for _mwo, sre in self._source_mwo_sre_rows(keys=keys)]
		if names:
			cancel_stock_reservation_entries(sre_list=names, notify=False)

	def _zero_source_mwo_and_mop_weights(self):
		"""Zero the weight buckets on each refined MWO and ALL of its not-started
		Manufacturing Operations, since the material has left for the refinery.

		Not just the single last operation: the current pending operation AND every
		further (downstream) operation still to run in the work order route are zeroed,
		so no remaining step keeps a phantom weight for material that is now in the
		refinery. Already-Finished operations are left untouched (historical record)."""
		zero_map = {field: 0 for field in self._MWO_WEIGHT_FIELDS}
		mop_zero_map = dict(zero_map)
		mop_zero_map["gemstone_wt_in_gram"] = 0
		for row in self.mwo_details:
			mwo = row.manufacturing_work_order
			if not mwo:
				continue
			frappe.db.set_value("Manufacturing Work Order", mwo, zero_map)

			# Every operation that has not started manufacturing yet — the current
			# pending operation plus any further (downstream) operations in the route.
			pending_mops = frappe.db.get_all(
				"Manufacturing Operation",
				filters={"manufacturing_work_order": mwo, "status": "Not Started"},
				pluck="name",
			)
			for mop in pending_mops:
				frappe.db.set_value("Manufacturing Operation", mop, mop_zero_map)

	def create_dust_opening_receipt_se(self, target_warehouse=None):
		# When the physical scrap exceeds the system stock, the extra must be brought into
		# the refining warehouse (or, external, the supplier warehouse it was handed to) via
		# a Material Receipt so the downstream repack/receive can consume it.
		target = target_warehouse or self.refining_warehouse

		# We now use the exact shortfalls calculated during create_material_transfer_se.
		shortfalls = getattr(self, "_dust_shortfalls", [])
		if not shortfalls:
			return

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Receipt"
		se.purpose = "Material Receipt"
		se.company = self.company
		se.custom_refining_entry = self.name
		se.auto_created = 1
		if target_warehouse:
			# External refining: carry the entry's manufacturer for the SE pure-qty
			# hook (see receive_from_supplier for the full rationale).
			se.manufacturer = self.manufacturer

		added = False
		for sf in shortfalls:
			if sf["qty"] <= 0:
				continue
			dust_batch = self.get_dust_opening_batch(sf["item_code"])
			se.append(
				"items",
				{
					"item_code": sf["item_code"],
					"qty": sf["qty"],
					"uom": sf["uom"],
					"t_warehouse": target,
					"purity": sf["purity"],
					"batch_no": dust_batch,
					"use_serial_batch_fields": 1,
				},
			)
			added = True

		if not added:
			return

		se.insert(ignore_permissions=True)
		se.submit()

		self.db_set("receiving_se", se.name)
		self.db_set("dust_received", 1)

	def create_repack_se(self):
		# Savepoint taken at entry so a failed attempt (see the submit except below) can be
		# undone atomically before retrying with a larger guard. A plain delete_doc cannot
		# clean up after a failed Manufacture submit whose on_submit cascade may already
		# have created and SUBMITTED linked Stock Entries and renamed output Batches.
		# Only work done inside create_repack_se is rolled back.
		repack_savepoint = "refining_repack_attempt"
		frappe.db.savepoint(repack_savepoint)

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Manufacture"
		se.purpose = "Manufacture"
		se.company = self.company
		se.custom_refining_entry = self.name
		se.auto_created = 1

		precision = 3
		min_qty = 0.001

		# Cache item attributes
		item_batch_map = {}
		for item in self.material_items:
			if item.item_code not in item_batch_map:
				item_batch_map[item.item_code] = frappe.db.get_value(
					"Item", item.item_code, "has_batch_no"
				)

		# Get brought in batches pool
		brought_in_batches = {}
		se_names = [self.material_transfer_se, getattr(self, "receiving_se", None)]
		for se_name in filter(None, se_names):
			for d in frappe.get_all(
				"Stock Entry Detail",
				filters={"parent": se_name, "t_warehouse": self.refining_warehouse},
				fields=["item_code", "batch_no", "qty", "serial_no"],
			):
				brought_in_batches.setdefault(d.item_code, []).append(d)

		# Running per-(item, batch) tally of what THIS repack SE has already booked.
		# get_batch_qty returns the DB balance, which does not drop as we append lines to
		# the in-memory SE, so without this tally two rows sharing a batch each cap against
		# the same stale balance and jointly over-draw it ("negative batch quantity").
		se_consumed = {}

		# Per-batch consumption guard. DEFAULT 0.0 -> consume the full available balance so
		# NO stock is stranded on the clean path. Raised only on retry (see
		# create_repack_se's except) to absorb ERPNext's intermittent Serial-and-Batch-
		# Bundle Manufacture overshoot, which can remove a few thousandths of a gram MORE
		# than the row qty when dozens of tiny batches are drawn at once. Reacting only when
		# that happens keeps normal repacks exact.
		batch_consume_guard = getattr(self, "_repack_guard", 0.0)

		def _batch_available(item_code, batch_no):
			from erpnext.stock.doctype.batch.batch import get_batch_qty

			# Cap at the smaller of the per-warehouse SBB balance and the GLOBAL Batch.batch_qty
			# cache (what the submit-time negative-batch validation checks) — they drift apart
			# on real data. Net of what this SE already booked (se_consumed) and of the guard.
			wh_bal = flt(
				get_batch_qty(
					batch_no=batch_no,
					warehouse=self.refining_warehouse,
					item_code=item_code,
				)
				or 0,
				precision,
			)
			cache_bal = flt(
				frappe.db.get_value("Batch", batch_no, "batch_qty") or 0, precision
			)
			ceiling = max(0.0, min(wh_bal, cache_bal) - batch_consume_guard)
			return flt(ceiling - se_consumed.get((item_code, batch_no), 0.0), precision)

		# Input items (consumed)
		for item in self.material_items:
			if item.get("source_type") == "BOM Component":
				continue

			has_batch = item_batch_map.get(item.item_code)

			# The serial FG item is transferred to the refining warehouse at submit
			# (create_material_transfer_se), so — like every other refined material — it is
			# consumed from the refining warehouse during repack.
			consume_warehouse = self.refining_warehouse

			if has_batch:
				qty_remaining = flt(item.qty, precision)

				# 1. Consume from brought in batches pool
				if item.item_code in brought_in_batches:
					for b in brought_in_batches[item.item_code]:
						if qty_remaining <= 0:
							break
						if b.qty <= 0:
							continue

						# Cap by the LIVE batch balance net of what this SE already booked (see
						# se_consumed): SBB rounding can leave the landed batch a hair short, and two rows
						# sharing a batch must not both cap against the same stale balance.
						available = _batch_available(item.item_code, b.batch_no)
						consume_qty = min(
							flt(b.qty, precision), qty_remaining, available
						)
						if consume_qty >= min_qty:
							se.append(
								"items",
								{
									"item_code": item.item_code,
									"qty": consume_qty,
									"uom": item.uom,
									"s_warehouse": consume_warehouse,
									"batch_no": b.batch_no,
									"serial_no": item.serial_no or b.serial_no,
									"use_serial_batch_fields": 1,
								},
							)
							qty_remaining -= consume_qty
							qty_remaining = flt(qty_remaining, precision)
							b.qty -= consume_qty
							se_consumed[(item.item_code, b.batch_no)] = (
								se_consumed.get((item.item_code, b.batch_no), 0.0)
								+ consume_qty
							)

				# 2. Consume from original row batch if available and has stock
				if qty_remaining >= min_qty and item.batch_no:
					available = _batch_available(item.item_code, item.batch_no)
					if available >= min_qty:
						consume_qty = min(available, qty_remaining)
						if consume_qty >= min_qty:
							se.append(
								"items",
								{
									"item_code": item.item_code,
									"qty": consume_qty,
									"uom": item.uom,
									"s_warehouse": consume_warehouse,
									"batch_no": item.batch_no,
									"serial_no": item.serial_no,
									"use_serial_batch_fields": 1,
								},
							)
							qty_remaining -= consume_qty
							qty_remaining = flt(qty_remaining, precision)
							se_consumed[(item.item_code, item.batch_no)] = (
								se_consumed.get((item.item_code, item.batch_no), 0.0)
								+ consume_qty
							)

				# 3. Fallback to FIFO. Non-throwing: after the brought-in and original batches, any
				# leftover is a sub-precision rounding remainder with no matching batch stock —
				# consume what is available and let the refining loss absorb the rest, rather than
				# aborting the completion with "Insufficient batch stock".
				if qty_remaining >= min_qty:
					allocations = self.allocate_fifo_batches(
						item.item_code,
						consume_warehouse,
						qty_remaining,
						throw_if_missing=False,
					)
					for alloc in allocations:
						if qty_remaining < min_qty:
							break
						# Net the FIFO qty against this SE's running tally and the row's
						# remaining need, so a batch already drawn by an earlier row is not
						# double-booked here.
						available = _batch_available(item.item_code, alloc["batch_no"])
						alloc_qty = min(
							flt(alloc["qty"], precision), available, qty_remaining
						)
						if alloc_qty >= min_qty:
							se.append(
								"items",
								{
									"item_code": item.item_code,
									"qty": alloc_qty,
									"uom": item.uom,
									"s_warehouse": consume_warehouse,
									"batch_no": alloc["batch_no"],
									"serial_no": item.serial_no,
									"use_serial_batch_fields": 1,
								},
							)
							qty_remaining -= alloc_qty
							qty_remaining = flt(qty_remaining, precision)
							se_consumed[(item.item_code, alloc["batch_no"])] = (
								se_consumed.get(
									(item.item_code, alloc["batch_no"]), 0.0
								)
								+ alloc_qty
							)
			else:
				if flt(item.qty, precision) >= min_qty:
					se.append(
						"items",
						{
							"item_code": item.item_code,
							"qty": item.qty,
							"uom": item.uom,
							"s_warehouse": consume_warehouse,
							"batch_no": item.batch_no,
							"serial_no": item.serial_no,
							"use_serial_batch_fields": 1,
						},
					)

		# Stamp customer ownership on every consumed (input) row from its batch. At this point
		# se.items holds only the input-consume rows (outputs are appended below).
		self._stamp_batch_ownership(se)

		# Output items (produced - Pure Gold 24KT only, Diamond, Gemstone)
		batch_tracking_rows = []
		# SOP: All gold is converted to a single Pure Gold 24KT item
		pure_gold_item = self._get_pure_gold_24kt_item()
		total_gold_weight = sum(flt(g.refining_gold_weight) for g in self.refined_gold)

		# Recovered gold inherits the input's ownership ONLY when the entire consumed lot is
		# one customer's — otherwise the single pure-gold output would misclassify it.
		# _recovered_output_customer enforces that and the Customer-Goods item-flag gate.
		output_customer = self._recovered_output_customer(se, pure_gold_item)
		output_owner_row = (
			{"inventory_type": "Customer Goods", "customer": output_customer}
			if output_customer
			else {}
		)
		if total_gold_weight > 0 and pure_gold_item:
			new_batch = self._auto_create_batch(
				pure_gold_item, customer=output_customer
			)
			se.append(
				"items",
				{
					"item_code": pure_gold_item,
					"qty": flt(total_gold_weight, 3),
					"uom": "Gram",
					"t_warehouse": self.refining_warehouse,
					"batch_no": new_batch,
					"is_finished_item": 1,
					"use_serial_batch_fields": 1,
					**output_owner_row,
				},
			)
			batch_tracking_rows.append(
				{
					"output_item": pure_gold_item,
					"output_batch": new_batch,
					"output_warehouse": self.refining_warehouse,
					"output_qty": flt(total_gold_weight, 3),
				}
			)

		for dia in self.recovered_diamond:
			# Output the amount actually recovered by the operator, not the present amount.
			dia_qty = flt(dia.recovered_weight)
			if dia_qty <= 0:
				continue
			conv_item = self.convert_diamond_item_code(dia.item)
			new_batch = self._auto_create_batch(conv_item)
			se.append(
				"items",
				{
					"item_code": conv_item,
					"qty": dia_qty,
					"uom": "Carat",
					"t_warehouse": self.refining_warehouse,
					"batch_no": new_batch,
					**secondary_item_row(),
					"is_finished_item": 0,
					"use_serial_batch_fields": 1,
				},
			)
			if new_batch:
				batch_tracking_rows.append(
					{
						"output_item": conv_item,
						"output_batch": new_batch,
						"output_warehouse": self.refining_warehouse,
						"output_qty": dia_qty,
					}
				)

		for gem in self.recovered_gemstone:
			# Output the amount actually recovered by the operator, not the present amount.
			gem_qty = flt(gem.recovered_weight)
			if gem_qty <= 0:
				continue
			new_batch = self._auto_create_batch(gem.item)
			se.append(
				"items",
				{
					"item_code": gem.item,
					"qty": gem_qty,
					"uom": "Carat",
					"t_warehouse": self.refining_warehouse,
					"batch_no": new_batch,
					**secondary_item_row(),
					"is_finished_item": 0,
					"use_serial_batch_fields": 1,
				},
			)
			if new_batch:
				batch_tracking_rows.append(
					{
						"output_item": gem.item,
						"output_batch": new_batch,
						"output_warehouse": self.refining_warehouse,
						"output_qty": gem_qty,
					}
				)

		if flt(self.refining_loss) > 0:
			dust_item = self.get_dust_item()
			if dust_item:
				dust_batch = self._auto_create_batch(dust_item)
				# Loss dust is a pure OUTPUT of the repack, like the recovered gold/stone rows:
				# t_warehouse only. Also setting s_warehouse made the Manufacture SE CONSUME the
				# loss from the freshly created (empty) batch, driving it negative on submit. The
				# loss is moved to the scrap warehouse afterwards by create_scrap_transfer_se.
				se.append(
					"items",
					{
						"item_code": dust_item,
						"qty": self.refining_loss,
						"uom": "Gram",
						"t_warehouse": self.refining_warehouse,
						"batch_no": dust_batch,
						**secondary_item_row(),
						"is_finished_item": 0,
						"use_serial_batch_fields": 1,
					},
				)

		se.insert(ignore_permissions=True)
		try:
			se.submit()
		except Exception as e:
			# Retry on ERPNext's intermittent SBB negative-batch overshoot by rebuilding with a
			# larger per-batch guard. Roll the whole attempt back to the entry savepoint first —
			# this undoes the draft, the partial submit and its cascade, and the empty output
			# Batches, without the fragility of deleting an already-submitted Stock Entry. Match
			# both wordings so localised messages still self-heal.
			msg = str(e).lower()
			cur_guard = getattr(self, "_repack_guard", 0.0)
			if (
				"negative batch" in msg or "negative stock" in msg
			) and cur_guard < 0.15:
				frappe.db.rollback(save_point=repack_savepoint)
				self._repack_guard = flt(cur_guard + 0.03, 3)
				return self.create_repack_se()
			raise
		self.db_set("repack_se", se.name)

		# Persist batch_tracking rows via direct DB insert
		for bt in batch_tracking_rows:
			child = self.append("batch_tracking", bt)
			child.db_insert()

	def cancel_linked_stock_entries(self):
		ses = frappe.get_all(
			"Stock Entry", filters={"custom_refining_entry": self.name, "docstatus": 1}
		)
		for se in ses:
			doc = frappe.get_doc("Stock Entry", se.name)
			doc.cancel()

	def _default_refining_service_item(self):
		return "REF-SVC-001" if frappe.db.exists("Item", "REF-SVC-001") else None

	def _cancel_refining_po(self):
		"""Cancel (submitted) or delete (still draft) every auto-created refining service
		PO linked to this entry when the entry is cancelled. A PO that has already been
		received/billed must not be silently voided — surface a clear error so the operator
		reverses it first. Draft POs are deleted rather than left dangling as "Closed"."""
		po_names = frappe.get_all(
			"Purchase Order",
			filters={"refining_entry": self.name, "docstatus": ["<", 2]},
			pluck="name",
		)
		if (
			self.refining_entry_po
			and self.refining_entry_po not in po_names
			and frappe.db.exists("Purchase Order", self.refining_entry_po)
		):
			po_names.append(self.refining_entry_po)

		for name in po_names:
			po = frappe.get_doc("Purchase Order", name)
			if po.docstatus == 1:
				if flt(po.per_received) > 0 or flt(po.per_billed) > 0:
					frappe.throw(
						_(
							"Cannot cancel this Refining Entry: its service PO {0} is already "
							"received/billed. Reverse the Purchase Order first."
						).format(frappe.bold(po.name))
					)
				po.flags.ignore_permissions = True
				po.cancel()
			elif po.docstatus == 0:
				# Frappe's delete_doc refuses to delete a document that's still linked
				# FROM another document — including this Refining Entry's own
				# refining_entry_po field, even mid-cancel. Clear that link first.
				if self.refining_entry_po == name:
					self.db_set("refining_entry_po", None)
				po.flags.ignore_permissions = True
				po.delete(ignore_permissions=True)

	def create_scrap_transfer_se(self):
		scrap_warehouse = self.scrap_warehouse
		dust_item = self.get_dust_item()
		if scrap_warehouse and dust_item:
			se = frappe.new_doc("Stock Entry")
			se.stock_entry_type = "Material Transfer"
			se.purpose = "Material Transfer"
			se.company = self.company
			se.custom_refining_entry = self.name
			se.auto_created = 1

			precision = 3
			min_qty = 0.001
			has_batch = frappe.db.get_value("Item", dust_item, "has_batch_no")

			if has_batch:
				# Non-throwing: the loss to move out is whatever the repack actually booked into the
				# refining warehouse, which can be a few thousandths short of self.refining_loss
				# after SBB rounding. Move what is available instead of aborting completion.
				allocations = self.allocate_fifo_batches(
					dust_item,
					self.refining_warehouse,
					self.refining_loss,
					throw_if_missing=False,
				)
				for alloc in allocations:
					if flt(alloc["qty"], precision) >= min_qty:
						se.append(
							"items",
							{
								"item_code": dust_item,
								"qty": alloc["qty"],
								"uom": "Gram",
								"s_warehouse": self.refining_warehouse,
								"t_warehouse": scrap_warehouse,
								"batch_no": alloc["batch_no"],
								"use_serial_batch_fields": 1,
							},
						)
			else:
				if flt(self.refining_loss, precision) >= min_qty:
					se.append(
						"items",
						{
							"item_code": dust_item,
							"qty": self.refining_loss,
							"uom": "Gram",
							"s_warehouse": self.refining_warehouse,
							"t_warehouse": scrap_warehouse,
							"use_serial_batch_fields": 1,
						},
					)

			if se.items:
				se.insert(ignore_permissions=True)
				se.submit()

	# --- Utils ---

	def _get_central_rm_warehouse(self):
		"""Get the Central RM Warehouse for the company."""
		company = self.company
		# Look for Central department's Raw Material warehouse
		central_dept = frappe.db.get_value(
			"Department",
			{"name": ["like", "Central%"], "company": company},
			"name",
		)
		if central_dept:
			wh = frappe.db.get_value(
				"Warehouse",
				{"department": central_dept, "warehouse_type": "Raw Material"},
				"name",
			)
			if wh:
				return wh

		# Fallback: search by name pattern
		wh = frappe.db.get_value(
			"Warehouse",
			{
				"name": ["like", "Central RM%"],
				"company": company,
				"warehouse_type": "Raw Material",
			},
			"name",
		)
		return wh

	def _get_pure_gold_24kt_item(self):
		"""Get the standard 24KT Pure Gold item code for output."""
		# Prefer 99.9 purity, fallback to 99.5
		item = frappe.db.get_value(
			"Item",
			{
				"variant_of": "M",
				"disabled": 0,
				"name": ["like", "M-G-24KT-99.9%"],
			},
			"name",
		)
		if not item:
			item = frappe.db.get_value(
				"Item",
				{
					"variant_of": "M",
					"disabled": 0,
					"name": ["like", "M-G-24KT%"],
				},
				"name",
			)
		if not item:
			frappe.throw(
				_(
					"No active 24KT Pure Gold item (variant of 'M' with 24KT) found. "
					"Please create one before completing refining."
				)
			)
		return item

	def _get_pure_gold_karat(self):
		"""Karat (Metal Touch) attribute value of the pure gold output item, e.g. '24KT',
		used to label the Refined Gold rows so they match the pure 24KT output booked by
		the repack Stock Entry. Returns None if the item has no valid karat attribute."""
		pure_item = self._get_pure_gold_24kt_item()
		karat = frappe.db.get_value(
			"Item Variant Attribute",
			{"parent": pure_item, "attribute": "Metal Touch"},
			"attribute_value",
		)
		if karat and frappe.db.exists("Attribute Value", karat):
			return karat
		return None

	def _auto_create_batch(self, item_code, customer=None):
		if not self.auto_create_batch:
			return None

		item = frappe.get_doc("Item", item_code)
		if not item.has_batch_no:
			return None

		batch = frappe.new_doc("Batch")
		batch.item = item_code
		# Recovered output of customer-owned metal stays the customer's property (Q4):
		# tag the fresh batch as Customer Goods so it is Customer Goods everywhere it is
		# read back (Stock Balance, later refining, the batch->row ownership backfill).
		if customer:
			batch.custom_customer = customer
			batch.custom_inventory_type = "Customer Goods"

		# Name the batch under THIS entry's company rather than the session default: the
		# Batch autoname derives its company prefix from custom_company and otherwise falls
		# back to the user/global default — wrong for a GEPL entry on a KGJPL-defaulted
		# site, and absent in a background job. Set unconditionally even though
		# custom_company is not a field on every site: autoname reads it with self.get(),
		# which resolves out of __dict__, and saving only persists meta fields.
		if self.company:
			batch.custom_company = self.company

		# If item has a batch naming series, ERPNext autoname handles it.
		# Otherwise, generate a batch_id from item code + timestamp.
		if not item.batch_number_series:
			from frappe.utils import now_datetime

			ts = now_datetime().strftime("%y%m%d%H%M%S")
			# Append a short random suffix to avoid collisions when two batches of the
			# same item are created within the same second.
			batch.batch_id = f"{item_code}-RFN-{ts}-{frappe.generate_hash(length=4)}"

		batch.insert()
		return batch.name

	def _get_available_batch(self, item_code, warehouse):
		"""Get an available batch for an item in a warehouse (SBB-aware, FIFO).

		In v16, batch stock lives in the Serial and Batch Bundle, not in
		Stock Ledger Entry.batch_no, so reuse allocate_fifo_batches which reads
		the SBB via get_batch_qty.
		"""
		allocs = self.allocate_fifo_batches(
			item_code, warehouse, 9999999, throw_if_missing=False
		)
		return allocs[0]["batch_no"] if allocs else None

	def _batch_ownership_matches(self, batch_no, customer):
		"""Whether ``batch_no`` may receive output owned by ``customer`` (None = company).

		Ownership never crosses: customer-owned recovery must land in THAT customer's
		batch, and company recovery must not land in any customer's batch. Uses the same
		vocabulary as ``row_ownership.resolve_batch_ownership`` — a batch carrying a
		customer inventory type without a customer is malformed and is treated as
		company stock (row_ownership downgrades it the same way)."""
		from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
			CUSTOMER_INVENTORY_TYPES,
		)

		inv_type, batch_customer = self._batch_customer_owner(batch_no)
		batch_is_customer_owned = inv_type in CUSTOMER_INVENTORY_TYPES and bool(
			batch_customer
		)
		if customer:
			# Must match the row's own type, not merely "some customer type": the output row is
			# stamped "Customer Goods" and reuse never re-saves the Batch, so a "Customer Stock"
			# batch would leave the ledger and the Batch master permanently disagreeing.
			return inv_type == "Customer Goods" and batch_customer == customer
		return not batch_is_customer_owned

	def _receive_target_batch(self, item_code, warehouse, customer=None, exclude=None):
		"""Batch to receive refining output into: an EXISTING one when there is one,
		otherwise a freshly minted one.

		Prefer the batch that physically holds stock of this item in the destination
		warehouse — the one the operator sees in the batch selector — and mint only when
		nothing suitable exists, so the same item does not accumulate a new batch per
		refining entry.

		Ownership is honoured in BOTH directions (see _batch_ownership_matches): company
		recovery never lands in a customer's batch, and a customer's recovery never lands
		in another owner's.
		"""
		if not frappe.db.get_value("Item", item_code, "has_batch_no"):
			return None

		exclude = exclude or set()
		# custom_batch_type is provisioned by a patch rather than by migrate, so a site
		# can genuinely lack the column. Probe once, not per batch.
		has_batch_type = frappe.db.has_column("Batch", "custom_batch_type")

		# FIFO over what is physically in the destination warehouse, ownership-filtered.
		# allocate_fifo_batches already drops disabled/expired and zero-stock batches and
		# returns them in the configured pick order — only what it cannot know about is
		# filtered below.
		for alloc in self.allocate_fifo_batches(
			item_code, warehouse, 9999999, throw_if_missing=False
		):
			batch_no = alloc.get("batch_no")
			if not batch_no or batch_no in exclude:
				continue
			# Never merge freshly recovered metal into a Scrap-tagged batch:
			# get_scrap_items_balance fetches exactly those, so Unused/Loose Material Refining would
			# sweep the recovered metal straight back up as scrap.
			if has_batch_type and frappe.db.get_value(
				"Batch", batch_no, "custom_batch_type"
			):
				continue
			if self._batch_ownership_matches(batch_no, customer):
				return batch_no

		return self._auto_create_batch(item_code, customer=customer)

	def _assert_no_loss_output(self, se):
		"""External refining must never RECEIVE a loss item into stock.

		The refiner is handed metal and hands back pure metal plus the stones intact; the
		shortfall between the two is a LOSS — a number on the Recovery Summary, never a
		stock-increasing row. Send 22KT x 12.4 g, get 24KT x 11.3 g: the Stock Entry shows
		exactly those two rows.

		Consumed loss rows are untouched: Scrap and Unused/Loose Material refining
		legitimately SEND ``ML-``/``FL-`` material to the refiner, and those rows carry
		only an ``s_warehouse``. Only rows receiving INTO a warehouse are checked. The row
		builders cannot currently produce one; this pins that down so a later edit cannot
		quietly start inflating loss-item stock.
		"""
		# Resolved WITHOUT calling get_dust_item(): on a site with no "ML-G-24KT-99.9-Y"
		# that falls through to _get_pure_loss_item, which can msgprint on every receive,
		# can CREATE an Item variant (a write, from inside a guard), and as a last resort
		# returns the recovered pure 24KT code itself — which would make this guard throw on
		# the legitimate metal row and block every external receive.
		loss_items = {self.loss_item, "ML-G-24KT-99.9-Y", "Metal Process Loss"}
		loss_items = {c for c in loss_items if c and frappe.db.exists("Item", c)}
		loss_items.discard(self.refined_metal_item)

		offenders = []
		for row in se.items:
			# Only rows that INCREASE stock. A row carrying both warehouses is a
			# transfer, not a receipt — external Dust legitimately moves ML-/FL-.
			if row.get("s_warehouse") or not row.get("t_warehouse"):
				continue
			item_code = row.get("item_code") or ""
			if not item_code or item_code == self.refined_metal_item:
				continue
			# DL-/GL- are deliberately NOT treated as loss here: they are physical stones
			# handed back intact by the refiner (_is_returned_intact), not refining loss.
			if (
				item_code in loss_items
				or item_code.upper().startswith(("ML-", "FL-"))
				or frappe.db.get_value("Item", item_code, "variant_of") in ("ML", "FL")
			):
				offenders.append(item_code)

		if offenders:
			frappe.throw(
				_(
					"External refining cannot receive loss item(s) {0} into stock. The "
					"refining loss is reported on the Recovery Summary, not booked as "
					"stock. This is a bug — please report it."
				).format(frappe.bold(", ".join(sorted(set(offenders)))))
			)

	def _fetch_loss_items_from_dept(self, department):
		"""Fetch ALL loss items (dust items) from a department's Scrap warehouse."""
		if not department:
			return

		scrap_wh = frappe.db.get_value(
			"Warehouse",
			{"department": department, "warehouse_type": "Scrap"},
			"name",
		)
		if not scrap_wh:
			return

		self._fetch_loss_items_from_warehouse(scrap_wh)

	def _fetch_loss_items_from_warehouse(self, warehouse):
		"""Fetch ALL loss items (dust items) from a specific warehouse.

		With no employee filter: batch-less rows from raw Bin (unchanged) — batches are
		FIFO-resolved at submit. With an employee selected: per-batch rows carrying
		batch_no, restricted to that employee's batches, drawn from the SAME enumeration
		that _dust_available_qty sums, so System Quantity and these rows always match."""
		if not warehouse:
			return

		if self.employee:
			for r in self._dust_employee_batch_rows(warehouse):
				purity = self.get_item_purity(r["item_code"])
				uom = frappe.db.get_value("Item", r["item_code"], "stock_uom") or "Gram"
				item_group = frappe.db.get_value("Item", r["item_code"], "item_group")
				self.append(
					"material_items",
					{
						"item_code": r["item_code"],
						"item_group": item_group,
						"warehouse": warehouse,
						"qty": r["qty"],
						"batch_no": r["batch_no"],
						"uom": uom,
						"source_type": SOURCE_TYPE_SCRAP,
						"purity": purity,
					},
				)
			return

		bins = frappe.db.get_all(
			"Bin",
			filters={"warehouse": warehouse, "actual_qty": [">", 0]},
			fields=["item_code", "actual_qty"],
		)
		# Skipped at source rather than dropped later, so the per-row purity/UOM lookups are
		# never paid for a row that cannot be refined. _dust_available_qty applies the same
		# skip, keeping System Quantity equal to the sum of these rows.
		restricted = self._blocked_item_codes(b.item_code for b in bins)

		for b in bins:
			if b.item_code in restricted:
				continue

			actual_qty = flt(b.actual_qty, 3)

			if actual_qty <= 0:
				continue

			purity = self.get_item_purity(b.item_code)
			uom = frappe.db.get_value("Item", b.item_code, "stock_uom") or "Gram"
			item_group = frappe.db.get_value("Item", b.item_code, "item_group")
			self.append(
				"material_items",
				{
					"item_code": b.item_code,
					"item_group": item_group,
					"warehouse": warehouse,
					"qty": actual_qty,
					"uom": uom,
					"source_type": SOURCE_TYPE_SCRAP,
					"purity": purity,
				},
			)

	@frappe.whitelist()
	def get_scrap_items_balance(self):
		"""Fetch available unused/loose material from the department warehouse(s).

		Material returned from production is repacked onto a DEDICATED unused/loose item
		(a variant of the Metal / Finding Unused/Loose Material template, or of the legacy
		``ML``/``FL`` template) on a batch tagged ``custom_batch_type =
		"Unused/Loose Material"`` by the Manufacturing Operation "Receive Unused/Loose
		Material" action. Rows with no such target — diamonds, gemstones, alloys — keep
		their own item code and are isolated by the batch tag alone.

		This fetch is item-agnostic: only the batch tag decides. Only those batches are
		fetched (optionally narrowed to the selected item), so ordinary department stock
		sharing a warehouse is never pulled in. Non-batch stock cannot carry the marker and
		is excluded.
		"""
		if self.refining_type != "Unused/Loose Material Refining":
			frappe.throw(
				_("This action is only available for Unused/Loose Material Refining.")
			)

		# Source from the selected warehouse if known, else all company Raw Material and
		# Scrap warehouses (the two department sinks scrap can land in).
		if self.warehouse:
			rm_warehouses = [frappe._dict(name=self.warehouse)]
		else:
			rm_warehouses = frappe.db.get_all(
				"Warehouse",
				filters={
					"company": self.company,
					"warehouse_type": ["in", ["Raw Material", "Scrap"]],
					"is_group": 0,
				},
				fields=["name"],
			)

		bin_filters = {"actual_qty": [">", 0]}
		if self.scrap_item:
			bin_filters["item_code"] = self.scrap_item

		scrap_items = []
		for wh in rm_warehouses:
			bin_filters["warehouse"] = wh.name
			bins = frappe.db.get_all(
				"Bin",
				filters=bin_filters,
				fields=["item_code", "actual_qty"],
			)
			# Filtered at source so restricted variants never reach the operator's picker
			# dialog and therefore cannot be ticked.
			restricted = self._blocked_item_codes(b.item_code for b in bins)

			for b in bins:
				if b.item_code in restricted:
					continue

				purity = self.get_item_purity(b.item_code)
				uom = frappe.db.get_value("Item", b.item_code, "stock_uom") or "Gram"
				item_group = frappe.db.get_value("Item", b.item_code, "item_group")

				# Always batch-tracked and marked custom_batch_type. Non-batch stock cannot
				# carry the marker, so it is never unused/loose material.
				if not frappe.db.get_value("Item", b.item_code, "has_batch_no"):
					continue

				allocs = self.allocate_fifo_batches(
					b.item_code,
					wh.name,
					9999999,
					throw_if_missing=False,
					exclude_blocked_customer=True,
				)
				for a in allocs:
					aq = flt(a.get("qty"), 3)
					if aq <= 0:
						continue
					# Only Unused/Loose-Material-tagged batches are eligible. When an Employee is
					# selected, additionally restrict to that employee's own batches
					# (Batch.custom_employee); both markers are fetched in one call. Batches minted
					# before custom_employee existed return None and are correctly excluded.
					btype, bemp = frappe.db.get_value(
						"Batch",
						a.get("batch_no"),
						["custom_batch_type", "custom_employee"],
					) or (None, None)
					if btype != BATCH_TYPE_UNUSED:
						continue
					if self.employee and bemp != self.employee:
						continue
					scrap_items.append(
						{
							"item_code": b.item_code,
							"item_group": item_group,
							"warehouse": wh.name,
							"batch_no": a.get("batch_no"),
							"actual_qty": aq,
							# Scrap system qty == physical qty: default the Physical Qty
							# to the full available balance so the operator can just
							# select the row and add it (still editable to refine less).
							"qty": aq,
							"uom": uom,
							"purity": purity,
						}
					)

		return scrap_items

	def _is_blocked_customer_batch(self, batch_no):
		"""True when ``batch_no`` belongs to a customer flagged ``custom_block_refining``.
		Keyed on the batch's customer only (any inventory type) — the requirement is to
		keep ALL of a blocked customer's batches out of Scrap / Unused-Loose-Material refining."""
		if not batch_no:
			return False
		cust = frappe.db.get_value("Batch", batch_no, "custom_customer")
		return bool(cust) and bool(
			frappe.db.get_value("Customer", cust, "custom_block_refining")
		)

	def _batch_customer_owner(self, batch_no):
		"""Return (inventory_type, customer) ownership of ``batch_no`` from the Batch
		master, or (None, None) for a blank/unknown batch. Refining Stock Entries are
		auto_created, so the generic CustomStockEntry.update_batches back-fill is skipped
		and doc_events/stock_entry.py forces blank rows to 'Regular Stock' — the reason
		customer-owned metal used to lose its ownership in refining. Callers stamp the SE
		row explicitly from this instead."""
		if not batch_no:
			return None, None
		return frappe.db.get_value(
			"Batch", batch_no, ["custom_inventory_type", "custom_customer"]
		) or (None, None)

	def _batch_is_customer_owned(self, batch_no):
		"""True when a batch is customer-owned (Customer Goods / Customer Stock with a
		customer set). Used to keep the customer's batch identity through the material-table
		consolidation and to tag recovered output as Customer Goods."""
		inv_type, customer = self._batch_customer_owner(batch_no)
		return bool(customer) and inv_type in ("Customer Goods", "Customer Stock")

	def _item_allows_customer_goods(self, item_code):
		"""True when the Item permits a Customer Goods inventory type. Minting a Customer
		Goods batch for an item without this flag hard-fails in
		Batch.update_inventory_dimentions, so recovered-output tagging is gated on it."""
		return bool(
			frappe.db.get_value(
				"Item", item_code, "custom_inventory_type_can_be_customer_goods"
			)
		)

	def _stamp_batch_ownership(self, se):
		"""Stamp inventory_type + customer on every batched row of ``se`` from its batch's
		ownership, and return the set of customers seen.

		Refining SEs are auto_created, so the generic batch->row backfill
		(CustomStockEntry.update_batches) is skipped and doc_events/stock_entry.py forces
		blank rows to "Regular Stock" — call this before insert. Ownership resolves via
		the app's single source of truth (row_ownership.resolve_batch_ownership), so the
		Rule 2/3 coherence checks are applied uniformly.
		"""
		from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
			resolve_batch_ownership,
		)

		customers = set()
		for row in se.items:
			if not row.get("batch_no"):
				continue
			inventory_type, customer = resolve_batch_ownership(row)
			row.inventory_type = inventory_type
			row.customer = customer
			if customer:
				customers.add(customer)
		return customers

	def _recovered_output_customer(self, se, output_item):
		"""Customer to mint the recovered pure metal (``output_item``) to, or None.

		Only when EVERY consumed batched row belongs to the SAME single customer — no
		Regular Stock metal and no second owner. The repack blends all inputs into one
		pure-metal output, so a mixed lot cannot be attributed to one customer without
		misclassifying someone else's gold; it conservatively downgrades to Regular Stock.
		Call AFTER ``_stamp_batch_ownership`` and BEFORE the output rows are appended.

		Also gated on ``_item_allows_customer_goods``: minting a Customer Goods batch for
		an item without the flag hard-fails in Batch.update_inventory_dimentions.
		"""
		customers = set()
		saw_company_metal = False
		for row in se.items:
			if not (row.get("s_warehouse") and row.get("batch_no")):
				continue
			if row.get("customer"):
				customers.add(row.get("customer"))
			else:
				saw_company_metal = True

		if len(customers) != 1 or saw_company_metal:
			# Only worth flagging when a customer's metal is present but the lot is mixed;
			# an all-company lot legitimately produces company stock, silently.
			if customers:
				frappe.msgprint(
					_(
						"Refining lot mixes customer-owned and company/other-owner metal, "
						"so the recovered gold is booked as Regular Stock. Refine a single "
						"customer's material on its own to retain customer ownership."
					),
					indicator="orange",
					alert=True,
				)
			return None

		customer = next(iter(customers))
		if output_item and not self._item_allows_customer_goods(output_item):
			frappe.msgprint(
				_(
					"Recovered gold item {0} is not enabled for Customer Goods, so the "
					"recovered metal is booked as Regular Stock. Tick 'Inventory Type Can "
					"be Customer Goods' on that item to retain customer ownership."
				).format(frappe.bold(output_item)),
				indicator="orange",
				alert=True,
			)
			return None
		return customer

	def validate_customer_block(self):
		"""Early, per-row guard: reject any Material Item whose batch belongs to a
		blocked customer for Scrap / Unused-Loose-Material refining (internal or external),
		sees it before the Stock Entry is built. Dust rows are batch-less until FIFO
		resolves at submit — those are enforced authoritatively in
		``create_material_transfer_se``."""
		if self.refining_type not in (
			"Scrap Refining",
			"Unused/Loose Material Refining",
		):
			return
		for row in self.material_items:
			if row.get("batch_no") and self._is_blocked_customer_batch(row.batch_no):
				cust = frappe.db.get_value("Batch", row.batch_no, "custom_customer")
				frappe.throw(
					_(
						"Batch {0} (item {1}) belongs to customer {2}, who is blocked from "
						"Scrap and Unused/Loose Material refining. Remove it before submitting."
					).format(
						frappe.bold(row.batch_no), row.item_code, frappe.bold(cust)
					)
				)

	# --- Variant restrictions (Manufacturing Setting -> Refining Variant Restrictions) ---
	#
	# A blacklist of (variant, refining_type) pairs: an item whose template is listed —
	# or the template itself — may not be refined under that type. A blank refining_type
	# on a row blocks the variant for EVERY type. Applies to internal and external alike.
	#
	# FAILS OPEN. No Manufacturing Setting resolvable, or no rows, means no restrictions
	# and refining behaves exactly as it did before the feature existed. Every hook below
	# short-circuits on the empty set, so an unconfigured site pays nothing.

	def _restricted_variants(self):
		"""Blocked template item codes for THIS entry's refining type. One resolve + one
		child-table read per document, memoised on the instance so the six consumers in a
		single save share them."""
		cache = self.__dict__.setdefault("_refining_restriction_cache", {})
		key = self.refining_type or ""
		if key in cache:
			return cache[key]

		# A site that has not migrated yet has no child table, and refining must keep
		# working rather than throw "1146 Table doesn't exist" on every save. Same
		# defensive shape the Batch.custom_batch_type reads use.
		setting = (
			resolve_manufacturing_setting(self.company, self.get("manufacturer"))
			if frappe.db.table_exists("Refining Variant Restriction")
			else None
		)
		if not setting:
			cache[key] = frozenset()
			return cache[key]

		rows = frappe.db.get_all(
			"Refining Variant Restriction",
			filters={
				"parent": setting,
				"parenttype": "Manufacturing Setting",
				"parentfield": "refining_variant_restrictions",
			},
			fields=["variant", "refining_type"],
		)
		cache[key] = frozenset(
			row.variant
			for row in rows
			if row.variant and (not row.refining_type or row.refining_type == key)
		)
		return cache[key]

	def _blocked_item_codes(self, item_codes):
		"""Subset of ``item_codes`` that is restricted, in ONE batched Item query.

		Matches both arms: the item is a variant OF a restricted template, or it IS the
		template. A per-row ``variant_of`` lookup would be an N+1 on a table that can hold
		hundreds of fetched rows.
		"""
		blocked = self._restricted_variants()
		if not blocked:
			return set()

		cache = self.__dict__.setdefault("_variant_block_cache", {})
		codes = {c for c in item_codes if c}
		unknown = codes - set(cache)
		if unknown:
			hits = set(unknown) & blocked
			rest = unknown - hits
			if rest:
				hits |= {
					d.name
					for d in frappe.get_all(
						"Item",
						filters={
							"name": ["in", list(rest)],
							"variant_of": ["in", list(blocked)],
						},
						fields=["name"],
					)
				}
			for code in unknown:
				cache[code] = code in hits
		return {c for c in codes if cache[c]}

	def _is_variant_restricted(self, item_code):
		"""Single-code check for the scan paths; shares the batched cache."""
		return bool(item_code) and item_code in self._blocked_item_codes([item_code])

	def _restriction_exempt_items(self):
		"""Items the check must never block, or the document becomes unsaveable.

		The pure loss code is the row the operator is REQUIRED to add as the
		physical-difference row (validate_dust_opening_material throws without it), so
		restricting its template (``ML``) would otherwise deadlock Scrap Refining.

		Resolved WITHOUT calling ``get_dust_item()``: that falls through to
		``_get_pure_loss_item``, which can msgprint and can CREATE an Item variant — neither
		belongs in a guard that runs on every save. The hardcoded pure code plus the
		operator's own ``loss_item`` cover the deadlock.
		"""
		return {PURE_LOSS_ITEM, self.loss_item} - {None, ""}

	def _drop_restricted_material_rows(self):
		"""Remove auto-fetched rows of a restricted variant, with ONE grouped notice.

		Informational, never a throw: these rows were fetched from the department warehouse,
		not chosen by the operator, so a warehouse that happens to hold a blocked variant
		must not make the entry unsubmittable. Operator-added and scanned rows still throw
		(validate_variant_restriction / the scan guards).
		"""
		blocked = self._blocked_item_codes(row.item_code for row in self.material_items)
		if not blocked:
			return
		blocked -= self._restriction_exempt_items()
		if not blocked:
			return

		dropped = {}
		keep = []
		for row in self.material_items:
			if row.item_code in blocked:
				entry = dropped.setdefault(row.item_code, 0.0)
				dropped[row.item_code] = entry + flt(row.qty)
			else:
				keep.append(row)
		if not dropped:
			return

		self.set("material_items", keep)
		# Report the dropped QTY as well as the codes: physical_quantity is keyed off the
		# scale, so the operator needs to know how much not to weigh in — otherwise
		# validate_dust_opening_material throws on the difference.
		frappe.msgprint(
			_(
				"Excluded from Material Items — not allowed for {0} per Manufacturing "
				"Setting: {1}. Do not include this weight in the Physical Quantity."
			).format(
				frappe.bold(self.refining_type),
				", ".join(
					f"{code} ({flt(qty, 3)})" for code, qty in sorted(dropped.items())
				),
			),
			indicator="orange",
			alert=True,
		)

	def validate_variant_restriction(self):
		"""Authoritative per-row guard. Catches every path that puts a row in the table,
		including manual grid entry and API creation."""
		blocked = (
			self._blocked_item_codes(row.item_code for row in self.material_items)
			- self._restriction_exempt_items()
		)
		if not blocked:
			return
		for row in self.material_items:
			if row.item_code in blocked:
				frappe.throw(
					_(
						"Row #{0}: Item {1} is a variant of {2}, which is not allowed for "
						"{3}. Remove it, or drop the restriction from the Manufacturing "
						"Setting."
					).format(
						row.idx,
						frappe.bold(row.item_code),
						frappe.bold(
							get_variant_of_item(row.item_code) or row.item_code
						),
						frappe.bold(self.refining_type),
					)
				)

	def _throw_if_variant_restricted(self, item_code):
		"""Scan-time twin of validate_variant_restriction: immediate feedback at the gun,
		before the row is appended."""
		if item_code and item_code not in self._restriction_exempt_items():
			if self._is_variant_restricted(item_code):
				frappe.throw(
					_(
						"Item {0} is a variant of {1}, which is not allowed for {2}."
					).format(
						frappe.bold(item_code),
						frappe.bold(get_variant_of_item(item_code) or item_code),
						frappe.bold(self.refining_type),
					)
				)

	@frappe.whitelist()
	def get_blocked_variants(self):
		"""Restricted templates for the client-side Material Items link filter."""
		return sorted(self._restricted_variants())

	def is_gold_item(self, item_code):
		variant_of = frappe.db.get_value("Item", item_code, "variant_of")
		item_group = frappe.db.get_value("Item", item_code, "item_group")
		return (
			(variant_of and variant_of.startswith(("M", "FL")))
			or (
				item_group
				and (
					"Metal" in item_group
					or "Gold" in item_group
					or "Finding" in item_group
				)
			)
			or (
				item_code and item_code.upper().startswith(("M-", "ML-", "GOLD", "FL-"))
			)
		)

	def is_diamond_item(self, item_code):
		"""Diamond (loose/melee/recovered). The classic D-/DL- SKUs are recognised in EVERY
		context. The broader signals — the "Diamond …" item group and the DM-/DBK-/DB-
		variants — apply ONLY to external refining, where every non-metal item is handed
		back intact and so must be recognised. Gating the broadening on is_external keeps
		INTERNAL recovery classification byte-for-byte unchanged.
		"""
		variant_of = frappe.db.get_value("Item", item_code, "variant_of")
		item_group = frappe.db.get_value("Item", item_code, "item_group")
		if (
			variant_of in ("D", "DL")
			or item_group == "Diamond"
			or (item_code and item_code.upper().startswith(("D-", "DL-")))
		):
			return True
		if not cint(self.is_external):
			return False
		return (
			(item_group and "Diamond" in item_group)
			or variant_of in ("DM", "DBK", "DB")
			or (item_code and item_code.upper().startswith(("DM-", "DBK-", "DB-")))
		)

	def is_gemstone_item(self, item_code):
		"""Gemstone (coloured stone). The classic G-/GL- SKUs are recognised in EVERY
		context; the broader "Gemstone …" item group and the GB- variant are recognised ONLY
		for external refining (where all non-metal is returned intact), keeping INTERNAL
		classification unchanged. See is_diamond_item for the full rationale. NOTE: the
		broad match leads with the item group, NOT a "starts with G" prefix — many non-gem
		variants also start with G (GCM, GLS, GA, …) and must NOT be misread as gemstones."""
		variant_of = frappe.db.get_value("Item", item_code, "variant_of")
		item_group = frappe.db.get_value("Item", item_code, "item_group")
		if (
			variant_of in ("G", "GL")
			or item_group in ("Gemstone", "Gem Stone")
			or (item_code and item_code.upper().startswith(("G-", "GL-")))
		):
			return True
		if not cint(self.is_external):
			return False
		return (
			(item_group and ("Gemstone" in item_group or "Gem Stone" in item_group))
			or variant_of == "GB"
			or (item_code and item_code.upper().startswith("GB-"))
		)

	def is_finding_item(self, item_code):
		"""Findings (clasps, jump rings, chain parts) — authoritative signal is a
		"Finding …" item group ("Finding - V/T", "Finding DNU"); variant_of ships as F/FL.
		NOTE: findings carry a gold purity, so is_gold_item() ALSO matches them. Findings
		are treated as meltable gold (melted and recovered as pure gold), NOT returned
		intact — see _is_returned_intact."""
		variant_of = frappe.db.get_value("Item", item_code, "variant_of")
		item_group = frappe.db.get_value("Item", item_code, "item_group")
		return (
			(item_group and "Finding" in item_group)
			or variant_of in ("F", "FL")
			or (item_code and item_code.upper().startswith(("F-", "FL-")))
		)

	def _is_returned_intact(self, item_code):
		"""External refining (ALL types): diamonds and gemstones travel to the refiner
		alongside the metal but are NOT melted — the refiner processes the gold alloy
		(including gold FINDINGS, which ARE melted) and hands the stones back physically.
		The returned stones are therefore excluded from the pure-metal weight / recovery
		computations and re-output into the department's Raw Material warehouse on receipt.

		Findings are gold alloy and are recovered as pure gold, so they are NOT returned
		intact: their weight flows into the melted-metal input and the recovery
		distribution, matching internal refining. Only diamonds and gemstones remain.
		"""
		return self.is_diamond_item(item_code) or self.is_gemstone_item(item_code)

	def auto_classify_recoverable_non_metal(self):
		diamond_items = {row.item for row in self.recovered_diamond}
		gemstone_items = {row.item for row in self.recovered_gemstone}

		if self.refining_type == "Serial Number Refining":
			for sn in self.serial_no_details:
				# Use the serial's OWN as-built BOM (custom_bom_no) for the diamond/gemstone
				# weights, mirroring scan_serial_no_action: the design item's generic active BOM
				# belongs to a different piece and yields mismatched (often empty) stone weights.
				# Fall back to the active BOM only when the serial has no BOM linked.
				bom_name = frappe.db.get_value(
					"Serial No", sn.serial_number, "custom_bom_no"
				) or frappe.db.get_value(
					"BOM", {"item": sn.item_code, "is_active": 1}, "name"
				)
				if bom_name:
					bom_items = frappe.get_all(
						"BOM Item",
						filters={"parent": bom_name},
						fields=["item_code", "qty"],
					)

					# Aggregate quantities by item_code to prevent dropping rows
					# if the BOM has multiple rows for the same diamond/gemstone.
					agg_bom_items = {}
					for bi in bom_items:
						agg_bom_items[bi.item_code] = agg_bom_items.get(
							bi.item_code, 0.0
						) + flt(bi.qty)

					for bi_item_code, bi_qty in agg_bom_items.items():
						if (
							self.is_diamond_item(bi_item_code)
							and bi_item_code not in diamond_items
						):
							self.append(
								"recovered_diamond",
								{
									"item": bi_item_code,
									"weight": bi_qty,
									"pcs": 1,
									"recovered_weight": bi_qty,
									"recovered_pcs": 1,
								},
							)
							diamond_items.add(bi_item_code)
						elif (
							self.is_gemstone_item(bi_item_code)
							and bi_item_code not in gemstone_items
						):
							self.append(
								"recovered_gemstone",
								{
									"item": bi_item_code,
									"weight": bi_qty,
									"pcs": 1,
									"recovered_weight": bi_qty,
									"recovered_pcs": 1,
								},
							)
							gemstone_items.add(bi_item_code)
		else:
			agg_material_items = {}
			for item in self.material_items:
				if item.item_code:
					agg_material_items[item.item_code] = agg_material_items.get(
						item.item_code, 0.0
					) + flt(item.qty)

			for item_code, qty in agg_material_items.items():
				if self.is_diamond_item(item_code) and item_code not in diamond_items:
					self.append(
						"recovered_diamond",
						{
							"item": item_code,
							"weight": qty,
							"pcs": 1,
							"recovered_weight": qty,
							"recovered_pcs": 1,
						},
					)
					diamond_items.add(item_code)
				elif (
					self.is_gemstone_item(item_code) and item_code not in gemstone_items
				):
					self.append(
						"recovered_gemstone",
						{
							"item": item_code,
							"weight": qty,
							"pcs": 1,
							"recovered_weight": qty,
							"recovered_pcs": 1,
						},
					)
					gemstone_items.add(item_code)

	def get_recovered_gold_total(self, total_recovered_weight=None):
		if total_recovered_weight is not None:
			return flt(total_recovered_weight)
		if flt(self.actual_recovery):
			return flt(self.actual_recovery)
		refined_total = sum(flt(row.refining_gold_weight) for row in self.refined_gold)
		return refined_total

	def get_proportional_recovery_weight(
		self, input_weight, total_input_weight, total_recovered_weight
	):
		if total_input_weight <= 0 or total_recovered_weight <= 0:
			return 0.0
		return flt(total_recovered_weight) * (
			flt(input_weight) / flt(total_input_weight)
		)

	def get_purity_distribution_maps(self, input_purity_map, input_item_map=None):
		if input_item_map is None:
			input_item_map = {}
		purity_maps = frappe.db.get_all(
			"Refining Purity Map",
			fields=[
				"karat",
				"purity_percentage",
				"item_template",
				"metal_purity",
			],
		)
		# Dedupe by purity percentage: duplicate map rows would emit duplicate
		# gold_recovery_details rows for the same karat and over-distribute the
		# recovered weight.
		seen_percentages = set()
		deduped = []
		for row in purity_maps:
			pct = flt(row.purity_percentage)
			if pct in seen_percentages:
				continue
			seen_percentages.add(pct)
			deduped.append(row)
		purity_maps = deduped
		mapped_percentages = {flt(row.purity_percentage) for row in purity_maps}
		for purity_percentage in input_purity_map:
			if purity_percentage in mapped_percentages:
				continue
			purity_maps.append(
				frappe._dict(
					{
						"karat": self.get_karat_from_percentage(purity_percentage),
						"purity_percentage": purity_percentage,
						"item_template": input_item_map.get(purity_percentage)
						or self.get_default_recovered_gold_item(),
						"metal_touch": None,
						"metal_purity": None,
					}
				)
			)
		return purity_maps

	def get_karat_from_percentage(self, purity_percentage):
		"""Map purity percentage to standard karat value (18KT, 22KT, 24KT, etc.)."""
		calculated_karat = flt(purity_percentage) * 24 / 100

		# Fetch standard karat values from Attribute Value (e.g., 9KT, 14KT, 18KT, 22KT, 24KT)
		standard_karats = frappe.db.get_all(
			"Attribute Value",
			filters={"name": ["like", "%KT"]},
			pluck="name",
		)

		# Find the nearest standard karat (within ±1.5 tolerance)
		best_match = None
		best_diff = 999
		for kt_name in standard_karats:
			try:
				kt_val = flt(kt_name.replace("KT", ""))
				diff = abs(kt_val - calculated_karat)
				if diff < best_diff and diff <= 1.5:
					best_diff = diff
					best_match = kt_name
			except (ValueError, TypeError):
				continue

		if best_match:
			return best_match
		return ("{0:g}KT").format(round(calculated_karat, 0))

	def get_default_recovered_gold_item(self):
		return frappe.db.get_value("Item", {"variant_of": "M", "disabled": 0}, "name")

	def populate_refined_gold_from_distribution(self):
		# Recovered gold is pure 24KT (see _rebuild_refined_gold_via_db): label the
		# Refined Gold rows with the 24KT pure item/karat, not the input karat.
		pure_item = self._get_pure_gold_24kt_item()
		pure_karat = self._get_pure_gold_karat()

		self.set("refined_gold", [])
		for row in self.gold_recovery_details:
			if flt(row.recovered_weight) <= 0:
				continue

			# The operator-entered recovered gold is already pure 24KT, so the recovered
			# weight IS the pure fine weight (no input-karat reduction).
			pure_weight = flt(row.recovered_weight, 3)

			self.append(
				"refined_gold",
				{
					"item_code": pure_item,
					"refining_gold_weight": row.recovered_weight,
					"pure_weight": pure_weight,
					"metal_purity": pure_karat,
				},
			)

	def get_dust_item(self):
		"""Pure 24KT loss item for the manufacture entry, for all four refining types.

		The refining loss is always a pure-equivalent quantity, so the loss row must always
		use the pure ML variant. Falls back to the resolution chain in
		``_get_pure_loss_item`` only when that item does not exist."""
		pure_loss = PURE_LOSS_ITEM
		if frappe.db.exists("Item", pure_loss):
			return pure_loss
		return self._get_pure_loss_item()

	def _get_pure_loss_item(self):
		"""Loss item for Unused/Loose Material, Work Order and Serial refining.

		The refining loss (``self.refining_loss``) is a PURE (24KT-equivalent) gram
		quantity — see ``_recalculate_and_persist_totals`` — so the loss row of the repack
		Manufacture entry must carry the PURE karat. Booking it against an input-karat
		item would put pure grams on a ~91.6%-purity item. Resolution order:

		  1. the operator-picked ``loss_item``, unless it carries a non-pure karat;
		  2. an existing Metal Loss variant of the pure karat (ML-G-24KT…);
		  3. derive/create the ML variant of the pure gold item (Variant Loss Table
		     mapping, the same helper Main Slip uses for process loss);
		  4. the dedicated karat-less "Metal Process Loss" item;
		  5. the recovered pure gold item itself (last resort — never an input item).
		"""
		pure_karat = self._get_pure_gold_karat()

		if self.loss_item:
			loss_karat = frappe.db.get_value(
				"Item Variant Attribute",
				{"parent": self.loss_item, "attribute": "Metal Touch"},
				"attribute_value",
			)
			if not loss_karat or not pure_karat or loss_karat == pure_karat:
				return self.loss_item
			if not getattr(self, "_pure_loss_override_warned", False):
				self._pure_loss_override_warned = True
				frappe.msgprint(
					_(
						"Loss Item {0} carries karat {1}, but the refining loss is a "
						"pure ({2}) quantity — booking the loss to the pure loss item "
						"instead."
					).format(self.loss_item, loss_karat, pure_karat),
					indicator="orange",
					alert=True,
				)

		if pure_karat:
			existing = frappe.db.get_value(
				"Item",
				{
					"variant_of": "ML",
					"disabled": 0,
					"name": ["like", f"ML-G-{pure_karat}%"],
				},
				"name",
			)
			if existing:
				return existing

		try:
			from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
				get_item_loss_item,
			)

			derived = get_item_loss_item(
				self.company, self._get_pure_gold_24kt_item(), variant_of="M"
			)
			# The Variant Loss Table fallback returns the source template when no
			# loss variant is mapped; only accept a real ML loss variant here.
			if (
				derived
				and self.is_gold_item(derived)
				and derived != self._get_pure_gold_24kt_item()
			):
				return derived
		except Exception:
			# Missing pure item / Variant Loss Table mapping / variant-creation
			# permission: fall through to the dedicated process-loss item. Drop the
			# error get_item_loss_item may have queued so the fallback stays silent.
			if frappe.message_log:
				frappe.message_log.pop()

		dust_item = frappe.db.get_value(
			"Item", {"item_code": "Metal Process Loss", "disabled": 0}, "name"
		)
		if dust_item:
			return dust_item

		# Serial refining consumes the FG item (not gold-classified), so fall back to
		# the recovered metal item code (pure 24KT) so the loss is still booked as dust.
		for gold in self.refined_gold:
			if gold.item_code:
				return gold.item_code
		return None

	def is_dust_opening_item(self, item):
		if self.refining_type != "Scrap Refining":
			return False
		if self.loss_item:
			return item.item_code == self.loss_item
		return item.get("source_type") != SOURCE_TYPE_SCRAP

	def get_dust_opening_qty(self, dust_item):
		dust_qty = 0.0
		for item in self.material_items:
			if self.is_dust_opening_item(item):
				dust_qty += flt(item.qty)
		if not dust_qty and flt(self.difference_quantity) > 0:
			dust_qty += flt(self.additional_dust_qty) or flt(self.difference_quantity)
		return dust_qty

	def _opening_dust_qty(self):
		"""Quantity of extra ('opening') scrap to receive when physical exceeds system.

		A POSITIVE additional_dust_qty is an operator override — they may receive LESS
		than the full physical-vs-system difference — and is honoured as-is.

		When it lands at 0 we fall back to the authoritative positive difference_quantity
		rather than reading 0 as 'add no scrap'. additional_dust_qty is only ever a
		client-side snapshot taken the instant physical_quantity is typed, whereas
		difference_quantity is recomputed server-side on every save; when the system qty
		shifts between those moments the snapshot goes stale at 0 while a real positive
		difference remains, which silently skipped the Material Receipt and stranded
		physical gold. Still the single source of truth, so the opening material row and
		the receipt SE always agree.
		"""
		override = flt(self.additional_dust_qty)
		if override > 0:
			return override
		return max(flt(self.difference_quantity), 0.0)

	def validate_dust_opening_material(self):
		"""Scrap Refining: the extra ('opening') dust for a physical-over-system difference
		is NO LONGER auto-added. The operator must manually add the difference-quantity
		item to the Material Items table so the table totals the physical quantity. If the
		rows still fall short of the physical quantity at submit, abort and tell them to add
		it. The Material Receipt that brings that physical excess into the refining
		warehouse is still created on submit (create_dust_opening_receipt_se)."""
		phys = flt(self.physical_quantity, 3)
		if phys <= 0:
			return
		added = flt(sum(flt(row.qty) for row in self.material_items), 3)
		if added + 0.001 < phys:
			frappe.throw(
				_(
					"Physical Quantity is {0} but the Material Items table only totals {1}. "
					"Add the difference quantity item ({2}) to the Material Items table "
					"before submitting."
				).format(phys, added, flt(phys - added, 3))
			)

	def ensure_dust_opening_material_row(self):
		dust_item = self.get_dust_item()
		if not dust_item:
			return
		if any(self.is_dust_opening_item(row) for row in self.material_items):
			return

		dust_qty = self._opening_dust_qty()
		if dust_qty <= 0:
			return

		self.append(
			"material_items",
			{
				"item_code": dust_item,
				"warehouse": self.refining_warehouse,
				"qty": dust_qty,
				"uom": frappe.db.get_value("Item", dust_item, "stock_uom") or "Gram",
				"source_type": SOURCE_TYPE_SCRAP,
				"purity": self.get_item_purity(dust_item),
				"batch_no": self.get_dust_opening_batch(dust_item),
			},
		)

	def get_dust_opening_batch(self, dust_item):
		if not dust_item:
			return None
		if not frappe.db.get_value("Item", dust_item, "has_batch_no"):
			return None
		for row in self.material_items:
			if row.item_code == dust_item and row.batch_no:
				return row.batch_no
		batch_no = self._auto_create_batch(dust_item)
		if not batch_no:
			batch = frappe.new_doc("Batch")
			batch.item = dust_item
			batch.insert()
			batch_no = batch.name
		for row in self.material_items:
			if row.item_code == dust_item and not row.batch_no:
				row.batch_no = batch_no
		return batch_no

	def convert_diamond_item_code(self, item_code):
		# Example: DL-NT-RO-4-+00-0 -> D-NT-RO-4-+00-0
		if item_code and item_code.startswith("DL-"):
			return "D-" + item_code[3:]
		return item_code

	def get_item_purity(self, item_code):
		# Memoize within the request: this is called per material row and does up to
		# three DB lookups, so caching avoids N+1 on large material tables.
		cache = self.__dict__.setdefault("_purity_cache", {})
		if item_code in cache:
			return cache[item_code]
		cache[item_code] = self._compute_item_purity(item_code)
		return cache[item_code]

	def _compute_item_purity(self, item_code):
		purity_records = frappe.db.get_all(
			"Item Variant Attribute",
			filters={
				"parent": item_code,
				"attribute": ["in", ["Metal Purity", "Purity"]],
			},
			fields=["attribute_value"],
			limit=1,
		)
		if purity_records:
			return purity_records[0].attribute_value

		# Fallback 1: Try BOM
		bom_purity = frappe.db.get_value(
			"BOM", {"item": item_code, "is_active": 1}, "metal_purity"
		)
		if bom_purity:
			return bom_purity

		# Fallback 2: parse from item code (e.g. ML-G-18KT-75.4-P -> 75.4)
		if item_code and "-" in item_code:
			parts = item_code.split("-")
			# Check second to last part (touch, e.g., 75.4)
			if len(parts) >= 2:
				val = parts[-2]
				if frappe.db.exists("Attribute Value", val):
					return val
			# Check third to last part (karat, e.g., 18KT)
			if len(parts) >= 3:
				val = parts[-3]
				if frappe.db.exists("Attribute Value", val):
					return val
		return None

	def allocate_fifo_batches(
		self,
		item_code,
		warehouse,
		required_qty,
		throw_if_missing=True,
		exclude_blocked_customer=False,
		ignore_reserved_stock=False,
		diagnostics=None,
	):
		"""FIFO-allocate ``required_qty`` of ``item_code`` across the batches in
		``warehouse``, as ``[{"batch_no": ..., "qty": ...}, ...]``.

		``ignore_reserved_stock`` reads PHYSICAL batch qty instead of the
		reservation-netted figure, for callers about to cancel the very reservations
		holding this stock (Work Order Refining). It also makes the batch list agree with
		the reservation-independent Bin ledger cap below.

		``diagnostics`` is an optional context dict used only to enrich the shortfall
		message.
		"""
		from erpnext.stock.doctype.batch.batch import get_batch_qty

		# In v16 per-batch stock lives in the Serial and Batch Bundle, NOT in
		# `tabStock Ledger Entry.batch_no` (which is NULL) nor in a plain
		# `Serial and Batch Entry` sum by warehouse — querying those returns zero candidate
		# batches for real SBB stock. get_batch_qty() is the SBB-aware source of truth and
		# returns available batches in the configured pick order, netting reserved/consumed
		# stock unless ignore_reserved_stock is set.
		batches = [
			b
			for b in (
				get_batch_qty(
					item_code=item_code,
					warehouse=warehouse,
					ignore_reserved_stock=ignore_reserved_stock,
				)
				or []
			)
			if flt(b.get("qty")) > 0
		]
		if exclude_blocked_customer:
			batches = [
				b
				for b in batches
				if not self._is_blocked_customer_batch(b.get("batch_no"))
			]

		allocations = []
		precision = 3
		min_qty = 0.001

		# Cap allocation by actual Bin ledger stock to prevent NegativeStockError
		# if batches are out of sync with overall warehouse ledger qty.
		bin_qty = (
			frappe.db.get_value(
				"Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
			)
			or 0.0
		)
		max_available_qty = max(0.0, flt(bin_qty, precision))

		# We can only allocate up to what we actually have in the ledger
		target_qty = min(flt(required_qty, precision), max_available_qty)
		allocated_qty = 0.0

		for batch in batches:
			if allocated_qty >= target_qty:
				break

			available_qty = flt(batch.get("qty"), precision)

			if available_qty < min_qty:
				continue

			alloc_qty = min(available_qty, target_qty - allocated_qty)
			if flt(alloc_qty, precision) >= min_qty:
				allocations.append(
					{
						"batch_no": batch.get("batch_no"),
						"qty": flt(alloc_qty, precision),
					}
				)
				allocated_qty += alloc_qty

		shortfall = flt(required_qty, precision) - allocated_qty
		if shortfall >= min_qty and throw_if_missing:
			# Even after all batches, some qty is left.
			frappe.throw(
				self._batch_shortfall_message(
					item_code,
					warehouse,
					required_qty,
					shortfall,
					allocated_qty,
					batches,
					flt(bin_qty, precision),
					diagnostics,
				)
			)

		return allocations

	def _batch_shortfall_message(
		self,
		item_code,
		warehouse,
		required_qty,
		shortfall,
		allocated_qty,
		batches,
		bin_qty,
		diagnostics=None,
	):
		"""Actionable "Insufficient batch stock" text.

		The bare message this replaces named only the item and warehouse, which made the
		failure opaque when it surfaced from a background Submission Queue job — the
		operator could not tell WHICH batch was short, which MWO it belonged to, or that
		the missing quantity was sitting in another warehouse all along. Everything here
		is already loaded or one cheap query away."""
		precision = 3
		lines = [
			_("Insufficient batch stock found for Item {0} in Warehouse {1}.").format(
				frappe.bold(item_code), frappe.bold(warehouse)
			),
			_("Required: {0}, Available: {1}, Missing: {2}.").format(
				flt(required_qty, precision),
				flt(allocated_qty, precision),
				flt(shortfall, precision),
			),
			_("Warehouse ledger (Bin) qty: {0}.").format(bin_qty),
		]

		if batches:
			lines.append(
				_("Batches in this warehouse: {0}").format(
					", ".join(
						f"{b.get('batch_no')} ({flt(b.get('qty'), precision)})"
						for b in batches
					)
				)
			)
		else:
			lines.append(_("No batch of this item has stock in this warehouse."))

		# Where the item actually is. This is the single most useful line when the
		# material table sourced a row from a stale reservation warehouse.
		elsewhere = frappe.db.get_all(
			"Bin",
			filters={
				"item_code": item_code,
				"warehouse": ["!=", warehouse],
				"actual_qty": [">", 0],
			},
			fields=["warehouse", "actual_qty"],
			order_by="actual_qty desc",
			limit=5,
		)
		if elsewhere:
			lines.append(
				_("Same item in other warehouse(s): {0}").format(
					"; ".join(
						f"{b.warehouse} ({flt(b.actual_qty, precision)})"
						for b in elsewhere
					)
				)
			)

		diagnostics = diagnostics or {}
		batch_no = diagnostics.get("batch_no")
		mwo = diagnostics.get("manufacturing_work_order")
		if batch_no or mwo:
			# EOD sync reports the same virtual-vs-physical divergence as `batch_short`;
			# reuse its diagnostics so both surfaces read the same way (open reservations
			# here, open reservations elsewhere, and the stale-SRE hint).
			from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
				_format_batch_short_diagnostics,
			)

			detail = _format_batch_short_diagnostics(
				item_code,
				warehouse,
				batch_no,
				flt(required_qty, precision),
				flt(allocated_qty, precision),
				mwo,
				None,
				diagnostics.get("company") or self.company,
			)
			if detail:
				lines.extend(detail.split("\n"))

		lines.append(_("Please ensure batch stock is available before submitting."))
		# frappe.throw renders the message as HTML, so newlines would collapse.
		return "<br>".join(lines)
