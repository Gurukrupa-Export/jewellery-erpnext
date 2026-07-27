import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from jewellery_erpnext.refining.doctype.refining_entry.recovery_distribution import (
	build_row_targets,
)

# Roles allowed to drive the refining department processing actions
REFINING_ROLES = ["Refining User", "Refining Manager", "System Manager"]

# Manager-level roles for the approval/finalisation steps (verify, complete,
# transfer). Per the Refining approval matrix these are restricted to managers;
# `require_refining_role` still auto-passes for Administrator.
REFINING_MANAGER_ROLES = ["Refining Manager", "System Manager"]

# The DEFAULT pricing CATEGORY per refining type (per the "Process Dust With Rate" sheet):
# Serial/MWO consignments default to Finish & Semi Finish Scrap, Scrap refining to Metal
# Refining Scrap, Dust to Main Dust. This is only the FALLBACK — the external PO now emits
# one line per category found in the Material Items (see _external_price_categories): a row
# stocked as a priced category (Ultra Liquid, Napkin/Thread/Buff, Tools Dust, Vacuum Bag,
# ...) bills under ITSELF, while ML/FL loss and plain scrap rows fall back to this default.
# The category items are seeded by seed_refining_masters.
EXTERNAL_PRICING_CATEGORY = {
	"Serial Number Refining": "REF-FSJ-001",
	"Work Order Refining": "REF-FSJ-001",
	"Scrap Refining": "REF-RMS-001",
	"Dust Refining": "REF-MD-001",
}


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
		self.set_dust_system_quantity()
		self.validate_quantities()
		self.calculate_totals()

		if self.status == "Recovery Entered":
			self.validate_recovery_distribution()
			self.validate_recovered_non_metal()

	def before_submit(self):
		# External refining (the "Is External Refining" checkbox on any refining type)
		# has its own submit-only lifecycle (no classify/repack/verify/complete/transfer)
		# and its own warehouse model (per-supplier, not the shared department
		# refining_warehouse) — handled entirely by before_submit_external.
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
		if self.refining_type in ("Dust Refining", "Work Order Refining"):
			if not self.material_items:
				self.build_material_table()
		if self.refining_type == "Dust Refining":
			self.validate_dust_opening_material()
		if not self.material_items:
			frappe.throw(
				_("No materials to refine. Add or fetch materials before submitting.")
			)

		# Reject blocked customers' batches early (Dust/Scrap); the material transfer
		# enforces it authoritatively for FIFO-resolved (batch-less) rows.
		self.validate_customer_block()

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

		if self.refining_type == "Dust Refining":
			self.create_dust_opening_receipt_se()

	def on_cancel(self):
		# External refining shares this path — cancel_linked_stock_entries also catches
		# its repack_se (tagged with custom_refining_entry like every other type's SEs),
		# and _cancel_refining_po already finds every PO linked via refining_entry=self.name
		# (the service PO plus any true-up), so no special-case dispatch is needed.
		self.cancel_linked_stock_entries()
		self._cancel_refining_po()

	# --- External Refining (the "Is External Refining" checkbox) ---
	#
	# A modifier available on every refining type, with its own submit-only lifecycle —
	# no classify/repack/verify/complete/transfer, and everything happens on ONE
	# document (no separate "receiving entry"):
	#   - Submit: issues material to the supplier's warehouse (Material Transfer) and
	#     creates an optional service Purchase Order billing the Refinery Price List
	#     Gross-Weight charge per sent item, if price entries are configured.
	#   - "Receive Material from Supplier" (receive_from_supplier, called from a dialog,
	#     any time after submit): books a single Repack Stock Entry that issues the
	#     originally sent material out of the supplier warehouse and receives the
	#     recovered pure metal into the source department's Raw Material warehouse in
	#     one atomic movement — no Purchase Receipt, no second Refining Entry.

	def before_submit_external(self):
		if not self.supplier:
			frappe.throw(_("Refinery Supplier is mandatory for external refining."))

		# Pricing category (the Refinery Price List this consignment bills under),
		# resolved from the refining type unless the operator picked one (Dust has
		# several possible categories — Main Dust is only the default).
		if not self.pricing_item:
			default_item = EXTERNAL_PRICING_CATEGORY.get(self.refining_type)
			if default_item and frappe.db.exists("Item", default_item):
				self.pricing_item = default_item

		# Same submit-time material build the internal path does for the types that
		# source from other documents (Dust fetches the dept scrap warehouse, Work
		# Order pulls the MWO's running balance); Scrap/Serial rows come from scans.
		if not self.material_items and self.refining_type in (
			"Dust Refining",
			"Work Order Refining",
		):
			self.build_material_table()
		if not self.material_items:
			frappe.throw(
				_("No materials to refine. Add or fetch materials before submitting.")
			)
		# Same physical-verification guard the internal Dust path enforces: the
		# Material Items table must total the counted physical quantity (the operator
		# adds the difference item manually) — the excess is then receipted into the
		# supplier warehouse on submit, exactly like the internal dust-opening receipt.
		if self.refining_type == "Dust Refining":
			self.validate_dust_opening_material()
		self.validate_customer_block()

		self.supplier_warehouse = self._get_supplier_warehouse()
		self.refined_metal_item = self._get_pure_gold_24kt_item()
		# Gold weight actually melted by the refiner: the gold ALLOY, INCLUDING gold
		# findings (they are melted and recovered as pure gold). Across every external
		# refining type the refiner hands back only the diamonds and gemstones intact
		# (see _is_returned_intact), so only their weight is excluded from the metal.
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
		# Dust: the physical-over-system excess (recorded as shortfalls during the
		# transfer) is receipted straight into the supplier warehouse so it travels
		# with the rest of the dust — mirrors the internal dust-opening receipt, which
		# receipts it into the refining warehouse.
		if self.refining_type == "Dust Refining":
			self.create_dust_opening_receipt_se(
				target_warehouse=self.supplier_warehouse
			)
		self.create_external_refining_po()

	def _external_price_categories(self):
		"""Ordered ``{pricing_category_item -> summed_weight_g}`` across the sent Material
		Items — one entry per PO line to bill.

		A row's pricing CATEGORY is its OWN ``item_code`` when a Refinery Price List exists
		for that item (the seeded REF-* categories: Ultra Liquid, Napkin/Thread/Buff, Tools
		Dust, Vacuum Bag, Sedimentation, Main Dust, …); otherwise it collapses to the
		entry's DEFAULT category ``self.pricing_item`` (Main Dust for Dust, Metal Refining
		Scrap for Scrap, Finish & Semi Finish for MWO/Serial). So ML-/FL- loss and plain
		scrap rows merge into the default line, while a row physically stocked as a priced
		category bills under itself. Same category → one combined line; different → separate.

		Display-only BOM Component rows are excluded (same rule as the former gross-weight
		sum); consumable rows ARE counted. Zero/negative-qty rows are skipped so a phantom
		Flat-charge line can't appear. dict insertion order gives deterministic PO line order.
		The key may be ``None`` (empty ``pricing_item`` and an unpriced item) — that still
		yields exactly one rate-0 manual line."""
		categories = {}
		has_price_list = {}
		for row in self.material_items:
			if not row.item_code or row.get("source_type") == "BOM Component":
				continue
			qty = flt(row.qty, 3)
			if qty <= 0:
				continue
			code = row.item_code
			if code not in has_price_list:
				has_price_list[code] = bool(
					frappe.db.exists("Refinery Price List", {"item": code})
				)
			category = code if has_price_list[code] else (self.pricing_item or None)
			categories[category] = flt(categories.get(category, 0.0) + qty, 3)
		return categories

	def _external_service_po_line(self, price_row, weight_g, rate):
		service_item = (price_row or {}).get(
			"service_item"
		) or self._default_refining_service_item()
		# The PO line bills a refining SERVICE charge, so its item MUST be a non-stock
		# service item. A price slab misconfigured with a stock item (e.g. a metal/loss
		# code like ML-G-18KT-75.4-Y) would otherwise put a stock item on the Purchase
		# Order, which fails validation with "Warehouse is mandatory for stock Item ...".
		# Fall back to the default service item and flag the bad slab rather than blocking
		# the whole submit.
		if service_item and frappe.db.get_value("Item", service_item, "is_stock_item"):
			fallback = self._default_refining_service_item()
			frappe.msgprint(
				_(
					"Refinery Price List slab {0} has the stock item {1} set as its "
					"Service Item; a refining-charge line must be a non-stock service "
					"item. Billing to {2} instead — please fix the slab's Service Item."
				).format(
					frappe.bold((price_row or {}).get("name") or "-"),
					frappe.bold(service_item),
					frappe.bold(fallback or "-"),
				),
				alert=True,
				indicator="orange",
			)
			service_item = (
				fallback
				if fallback
				and not frappe.db.get_value("Item", fallback, "is_stock_item")
				else None
			)
		if not service_item:
			frappe.throw(
				_(
					"No service item configured for external refining — set a non-stock "
					"Service Item on the price slab or seed the default service item {0}."
				).format(frappe.bold("REF-SVC-001"))
			)
		return {
			"item_code": service_item,
			"qty": 1,
			"rate": rate,
			"schedule_date": self.posting_date,
			"custom_gross_wt": weight_g,
			"custom_refining_price_list": (price_row or {}).get("name"),
		}

	def create_external_refining_po(self):
		"""After the material transfer: ALWAYS create the draft service Purchase Order
		— every external entry gets one, no exceptions. It carries ONE LINE PER PRICING
		CATEGORY present in the Material Items table (see _external_price_categories): a
		dust consignment yields e.g. a Main Dust line (all ML/FL loss merged), an Ultra
		Liquid line and a Napkin/Thread/Buff line; Scrap/MWO/Serial collapse to a single
		category → one line.

		Each line is priced NOW from that category's own Refinery Price List slab on the
		category's summed material-items weight (get_refinery_rate + compute_refining_amount,
		for ANY weight basis — the weights are already in the table at submit). A category
		with no matching slab (weight-band gap, unpriced category, or no price list at all
		e.g. REF-BR-001) gets a rate-0 line for the purchase team to price manually.
		Left as a draft for the buyer to review/submit; the physical flow never blocks
		on pricing."""
		from jewellery_erpnext.refining.doctype.refinery_price_list.refinery_price_list import (
			compute_refining_amount,
			get_refinery_rate,
		)

		po = frappe.new_doc("Purchase Order")
		po.refining_entry = self.name
		po.supplier = self.supplier
		po.company = self.company
		po.transaction_date = self.posting_date
		# ALWAYS assign the attribute (None when the master is missing): the app's own
		# PO validate hook (doc_events/purchase_order.py update_rate) reads
		# self.purchase_type unconditionally, and on sites where the custom field isn't
		# in the Purchase Order meta (e.g. the CI fixture set), reading a never-assigned
		# attribute raises AttributeError instead of returning None.
		po.purchase_type = (
			"Service" if frappe.db.exists("Purchase Type", "Service") else None
		)

		unpriced = []
		for category, weight_g in self._external_price_categories().items():
			row = (
				get_refinery_rate(
					category,
					weight_g,
					company=self.company,
					supplier=self.supplier,
				)
				if category
				else None
			)
			if row:
				rate = compute_refining_amount(
					row.get("charge_type"), row.get("rate"), weight_g
				)
			else:
				rate = 0
				unpriced.append((category, weight_g))
			po.append("items", self._external_service_po_line(row, weight_g, rate))

		po.insert(ignore_permissions=True)
		self.db_set("refining_entry_po", po.name)

		if unpriced:
			frappe.msgprint(
				_(
					"No Refinery Price List slab matched these categories — their Purchase "
					"Order lines were created at rate 0 for manual pricing: {0}"
				).format(
					", ".join(f"{frappe.bold(c or '-')} ({w} g)" for c, w in unpriced)
				),
				alert=True,
				indicator="orange",
			)

	@frappe.whitelist()
	def receive_from_supplier(self, recovery_weight, received_qty=None):
		"""Record receipt of refined metal from the supplier directly on THIS entry —
		no second Refining Entry, no Purchase Receipt. Builds a single Repack Stock
		Entry that issues the originally sent material out of the supplier warehouse
		and receives the recovered pure metal into the source department's Raw
		Material warehouse in one atomic movement; ERPNext values the output from the
		consumed input automatically (standard Repack costing), so no explicit rate is
		needed. Also bills any Fine/After-Burning-basis service charge now that the
		actual received weight is known."""
		self.require_refining_role(REFINING_ROLES, _("receive material from supplier"))
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

		# Booked as a Manufacture (not a plain Repack): the refiner melts the gold alloy
		# into pure 24KT and hands back the findings/diamonds/gemstones, so the receipt has
		# ONE finished good (the pure metal) plus several by-products (the returned stones/
		# findings) — exactly the shape create_repack_se uses internally. ERPNext's Repack
		# validation instead force-marks EVERY received row as a finished good and then
		# refuses to auto-cost multiple finished goods ("set the basic rate manually"); a
		# work-order-less Manufacture keeps our is_finished_item flags and auto-costs the
		# single FG from the consumed inputs, letting the scrap by-products land at 0.
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

		# Group batch-less rows item-wise and FIFO-allocate ONCE per item: multiple
		# rows of the same item (e.g. the fetched dust rows plus the operator-added
		# physical-difference row) would otherwise each FIFO-allocate independently and
		# double-claim the same batches, over-consuming them into negative stock.
		# Rows carrying a serial number keep their per-row identity.
		expected_qty = 0.0
		issued_qty = 0.0
		# Actually-consumed qty per item, so a returned (non-serial) row can never output
		# more than physically came back out of the supplier warehouse (guards against
		# creating phantom department stock when a transfer/receive shortfall means less was
		# available than the Material Items row claims).
		consumed_by_item = {}
		grouped = {}
		serial_rows = []
		for item in self.material_items:
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

		# Keep customer-owned metal tagged Customer Goods across the external round trip:
		# stamp the consumed rows from their supplier-warehouse batches (auto_created SEs skip
		# the generic backfill), then mint the recovered pure metal back to that customer when
		# the whole consumed lot is theirs (same rule/guard as the internal repack).
		self._stamp_batch_ownership(se)
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
		if frappe.db.get_value("Item", self.refined_metal_item, "has_batch_no"):
			metal_row["batch_no"] = self._auto_create_batch(
				self.refined_metal_item, customer=output_customer
			)
		se.append("items", metal_row)

		# Return the diamonds and gemstones intact. The refiner melts the gold alloy —
		# INCLUDING gold findings — reborn above as the single pure-24KT metal row, and
		# hands only the stones back, for EVERY external refining type (findings are NOT
		# re-output; their gold is in the recovered metal — see _is_returned_intact):
		#   - Dust / Scrap / Work Order: these are real material_items rows, already CONSUMED
		#     out of the supplier warehouse in the input rows above; re-outputting them here
		#     into the department RM warehouse nets to a supplier -> department transfer.
		#   - Serial Number: the stones sit INSIDE the FG serial (consumed above) and are
		#     represented by its "BOM Component" rows; here they are "exploded" out as repack
		#     outputs (output-only — there is no separate supplier-warehouse stock to consume
		#     for them), exactly like the internal serial repack books its recovered
		#     diamond/gemstone rows.
		# Aggregated per item so one fresh return batch is created for each. Without this the
		# findings/stones were issued out (or melted away) and never received anywhere.
		returned = {}
		for item in self.material_items:
			if not self._is_returned_intact(item.item_code):
				continue
			qty = flt(item.qty, precision)
			if qty < min_qty:
				continue
			# Serial's returnables are "BOM Component" rows exploded from the consumed FG
			# (output-only, no separate supplier stock); the other types' returnables are
			# real rows consumed from the supplier above and are capped below at the amount
			# actually consumed.
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
				"type": "Scrap",
				"is_finished_item": 0,
				"use_serial_batch_fields": 1,
				"allow_zero_valuation_rate": 1,
			}
			if frappe.db.get_value("Item", ret_item, "has_batch_no"):
				row["batch_no"] = self._auto_create_batch(ret_item)
			se.append("items", row)

		se.insert(ignore_permissions=True)
		se.submit()

		# Backfill purity on rows that don't have one set (e.g. added via
		# scan_scrap_qr_action, which doesn't capture it) — generate_recovery_table
		# groups by row.purity directly, so a blank value would silently drop that
		# row out of the distribution.
		for item in self.material_items:
			if not item.purity:
				item.purity = self.get_item_purity(item.item_code)

		# Reuse the SAME proportional-by-pure-content distribution the other 4
		# refining types use: populates Gold Recovery Details (per karat) and
		# Recovered Metal (one refined_gold row per karat, not a single blended row),
		# and computes the Recovery Summary totals — all via the same already-audited
		# logic, rather than a separate ad-hoc computation.
		self.generate_recovery_table(total_recovered_weight=recovery_weight)

		self.received_weight = recovery_weight
		self.repack_se = se.name
		if received_qty:
			self.received_qty = flt(received_qty)
		self.db_set(
			{
				"received_weight": self.received_weight,
				"repack_se": self.repack_se,
				"received_qty": flt(self.received_qty),
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
		"""Populate the Material Items table on save so the consolidated materials (and
		the Raw Materials HTML that reads them) are visible before submit — previously
		the table was only built at submit time. Runs only on a Draft while the table
		is still empty, so it never clobbers manually added/edited rows.

		Scoped to Dust Refining only: it sources from the department Scrap warehouse and
		never throws on a missing attribute. Work Order material is built by
		scan_mwo_action (and at submit via before_submit), NOT here — building it on every
		save would (a) raise "Metal Purity is mandatory" and block a plain Save, and
		(b) resolve source warehouses from Stock Reservation Entries whose lifecycle is
		now handled at submit."""
		if self.docstatus != 0 or self.material_items:
			return
		if (
			self.refining_type == "Dust Refining"
			and (self.warehouse or self.multiple_department)
		) or (self.refining_type == "Work Order Refining" and self.mwo_details):
			self.build_material_table()

	def set_dust_system_quantity(self):
		"""Keep the Dust Refining System Quantity in lockstep with the material actually
		available in the source Scrap warehouse(s), computed the SAME way the Material
		Items table is built (SBB-aware net qty for batched items). Summing raw
		Bin.actual_qty diverged from the fetched material rows, which is what made the
		System Quantity look wrong against the material table."""
		if self.refining_type != "Dust Refining" or self.docstatus != 0:
			return
		self.system_quantity = flt(self._compute_dust_system_quantity(), 3)

	def set_naming_series(self):
		series_map = {
			"Dust Refining": "RFN-DST-.YY.-.#####",
			"Work Order Refining": "RFN-MWO-.YY.-.#####",
			"Serial Number Refining": "RFN-SRN-.YY.-.#####",
			"Scrap Refining": "RFN-SCP-.YY.-.#####",
		}
		if self.refining_type in series_map:
			self.naming_series = series_map[self.refining_type]

	def validate_configuration(self):
		if (
			self.refining_type == "Dust Refining"
			and self.multiple_department
			and self.multiple_operation
		):
			frappe.throw(
				_("Choose either Multiple Operations OR Multiple Department, not both.")
			)

	def set_source_department_from_user(self):
		"""Default the Source Department from the logged-in user's Employee record.
		This scopes refining to the user's own department (it also drives the source
		warehouse via validate_warehouse). A manually chosen department is preserved,
		and a missing Employee record is not an error.

		Serial Number Refining is the exception: the serials are refined by refinery
		staff whose own department (e.g. Refinery) differs from where the serials
		actually sit (e.g. Tagging). Defaulting to the operator's department left the
		Source Department mismatching the scanned serials, so validate_source_department_match
		blocked the first submit ("Serial No ... belongs to department ..."). Derive it
		from the scanned serials instead so it always matches. Only on the original
		entry (not the processing duplicate, whose serials have already moved to the
		refining warehouse).

		IMPORTANT: Only derive the department from serials when it is not already set.
		Once set (by the first scan/save), the Source Department must NOT be
		overwritten — after the Material Transfer SE moves the serial to the refining
		warehouse the serial's warehouse department changes to the refining department,
		and overwriting self.department from that would make the Source Department
		track the refining department instead of the original source."""
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
		"""Refining is only allowed for material belonging to the Source Department.
		- Work Order Refining: every scanned MWO's department must match.
		- Serial Number Refining: each serial's current warehouse department must match.
		Enforced defensively here so API-created entries (which bypass the scan
		handlers) are also validated.

		Skipped on the processing duplicate (parent_refining_entry set): by the time
		the duplicate is created the parent's Material Transfer has already moved the
		material into the refining warehouse, so the source-department check would
		wrongly fail (e.g. a serial now sits in the Refinery department, not its
		original source). Source validation already ran on the parent before submit.
		Also skipped if the document is already submitted (docstatus == 1), for the
		same reason (saving a submitted document shouldn't re-validate past state)."""
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
			# Resolve the refining department once so we can allow serials
			# that have already been moved into the refining warehouse (e.g.
			# by a prior material-transfer SE). For newly scanned serials or
			# the original refining entry, serials should be in the source warehouse.
			# For duplicate entries (parent_refining_entry set), serials may have
			# already moved to the refining warehouse during the first submit.
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
				# Allow serial if it's in source warehouse OR refining warehouse.
				# When processing duplicates (parent_refining_entry set), the serial
				# may have already been moved to refining warehouse by the parent's
				# material transfer SE, so we permit both locations.
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
				# Dust refining: source is from the department Scrap warehouse.
				# Scrap refining: scrap is now received into the department RM warehouse
				#   as a designated Scrap Item (per Refinery Change SOP), so source is RM.
				# MWO and Serial Number refining: source is from Manufacturing warehouse.
				if self.refining_type == "Dust Refining":
					wh_type = "Scrap"
				elif self.refining_type == "Scrap Refining":
					wh_type = "Raw Material"
				elif self.refining_type == "Work Order Refining":
					wh_type = "Manufacturing"
				elif self.refining_type == "Serial Number Refining":
					wh_type = "Manufacturing"
				else:
					wh_type = "Manufacturing"

				if is_final_polish and self.refining_type not in (
					"Work Order Refining",
					"Scrap Refining",
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
		if self.refining_type == "Dust Refining":
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
			# Same purity-weighted computation as the generic branch below (kept
			# consistent with Gold Recovery Details, which receive_from_supplier
			# populates via generate_recovery_table once purity is backfilled onto
			# every row). Before receipt, material_items rows added via
			# scan_scrap_qr_action rarely carry a purity value yet — fall back to
			# qty_to_refine (the gold-item weight sent, computed at submit) so the
			# Recovery Summary still shows something sensible in the meantime.
			for item in self.material_items:
				# Only the gold ALLOY that is actually melted contributes to the pure-metal
				# input. Diamonds/gemstones are returned intact (see _is_returned_intact)
				# and excluded; gold FINDINGS are melted, so they stay counted here (their
				# recovered gold reconciles against the pure input). Restricting to
				# is_gold_item also drops the non-metal FG serial row (source_type
				# "Serial Number"), whose qty is a piece count, not grams — it would
				# otherwise double-count against the BOM metal-component rows that carry
				# the real melted weight.
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

		# Round to display precision (3 decimals) before computing derived values
		# so the percentage matches what the user sees on screen. Without this,
		# internal float imprecision (e.g. 0.4380456 displayed as 0.438) causes
		# the recovery % to show 99.98 instead of 100 when values are equal.
		self.gross_pure_weight = flt(self.gross_pure_weight, 3)
		self.expected_recovery = flt(self.expected_recovery, 3)
		self.refined_fine_weight = flt(self.refined_fine_weight, 3)
		self.actual_recovery = flt(self.actual_recovery, 3)

		# The Recovery Summary is derived from the PARENT inputs (material_items x purity,
		# refined_gold), never from the Gold Recovery Details table.
		#
		# It briefly worked the other way round, to make the summary loss agree with the
		# table's loss column to the milligram. That inverted the dependency: the table is
		# written once per lifecycle by two button actions that become unreachable once the
		# entry leaves "Refining In Progress", while this runs on EVERY save. Rows written by
		# a superseded formula (loss measured against the gross alloy weight, or a gross-
		# proportional split scored on the pure basis) were then adopted as the truth — and
		# `refining_loss` is minted as pure-24KT dust by create_repack_se, so a stale column
		# became phantom gold. One real entry moved 0.033 -> 0.860 g on a no-op save.
		#
		# The milligram divergence is real, but it is fixed at the WRITER instead: the table
		# is apportioned FROM these totals by _sync_recovery_table, so each column sums back
		# exactly. Clamp at 0: recovered 24KT gold can slightly exceed the computed pure input
		# (rounding / assay variance) and must never book a negative loss.
		self.refining_loss = flt(
			max(self.gross_pure_weight - self.refined_fine_weight, 0.0), 3
		)

		self._preserve_settled_summary()

		if self.expected_recovery > 0:
			pct = min(
				(self.refined_fine_weight / self.expected_recovery) * 100.0,
				100.0,
			)
			# The field displays at 2 decimals, so e.g. 99.9966% would ROUND UP to
			# "100.00" while a non-zero Refining Loss shows right next to it — a
			# contradiction to the reader. Cap the display at 99.99 whenever any loss
			# remains; a full 100.00 appears only when the loss is exactly zero.
			if self.refining_loss > 0 and pct > 99.99:
				pct = 99.99
			self.recovery_percentage = pct
		else:
			self.recovery_percentage = 0.0

		self._sync_recovery_table()

	# Weights are grams; anything at or below this is rounding noise, not a real move.
	SUMMARY_DRIFT_TOLERANCE = 0.0005

	def _preserve_settled_summary(self):
		"""Never silently restate the gold figures of an entry whose stock has already moved.

		``calculate_totals`` runs on every save, and a Refining Entry stays ``docstatus = 0``
		for its whole lifecycle — the workflow lives in ``status``. So an entry that already
		posted its repack / transfer Stock Entries is still freely savable, and any later drift
		in the inputs (an edited material row, a purity master that now resolves differently, a
		changed formula) would rewrite the very numbers those Stock Entries were posted with.

		When the stock has moved, the stored figures win and the divergence is surfaced via
		``recovery_table_out_of_sync`` instead of being applied. Fixing it is then a deliberate
		act, not a side effect of opening the form.
		"""
		if not (self.repack_se or self.transfer_se):
			return

		previous = self.get_doc_before_save()
		if not previous:
			return

		drifted = False
		for field in (
			"gross_pure_weight",
			"expected_recovery",
			"refined_fine_weight",
			"actual_recovery",
			"refining_loss",
		):
			stored = flt(previous.get(field))
			if abs(flt(self.get(field)) - stored) > self.SUMMARY_DRIFT_TOLERANCE:
				self.set(field, stored)
				drifted = True

		if drifted:
			# Remembered for the rest of this save: _sync_recovery_table must not clear a
			# flag raised here when it goes on to heal the table successfully.
			self._summary_held = True
			self.recovery_table_out_of_sync = 1

	def _sync_recovery_table(self):
		"""Re-derive the per-karat columns FROM the authoritative parent totals.

		The Gold Recovery Details rows are written once per lifecycle by two button actions
		that disappear from the form once the entry leaves "Refining In Progress", so a row can
		outlive the formula that produced it. Three formula generations exist in the wild —
		loss measured against the gross alloy weight, a gross-proportional split scored on the
		pure basis, and the current pure-proportional form — and nothing ever recomputed the
		older two. Re-deriving here keeps the displayed split honest without waiting for a
		button the operator can no longer reach.

		Deliberately narrow:
		  * only the four derived columns, NEVER a parent total — the figures that drive the
		    repack and scrap Stock Entries are computed from parent inputs and are not touched
		    here;
		  * only while ``docstatus == 0``;
		  * a no-op when there is no table yet, or when no purity basis can be rebuilt (the
		    external pre-receipt case, where rows carry no purity) — leaving the rows alone
		    beats blanking them;
		  * pure math, no role gate — this runs inside ``validate``.
		"""
		if self.docstatus != 0 or not self.gold_recovery_details:
			return

		# The summary was held back because this entry's stock has already moved, so the table
		# cannot be reconciled to it — leave the flag raised and the rows untouched.
		if getattr(self, "_summary_held", False):
			return

		purity_map = self._collect_input_purity_map()[0]
		if not purity_map or sum(flt(v) for v in purity_map.values()) <= 0:
			# No basis to rebuild from: flag it rather than silently leaving a stale table
			# that looks authoritative.
			if self._recovery_table_diverges():
				self.recovery_table_out_of_sync = 1
			return

		pure_weights = [
			flt(purity_map.get(flt(row.purity_percentage), 0.0))
			* (flt(row.purity_percentage) / 100.0)
			for row in self.gold_recovery_details
		]
		if sum(pure_weights) <= 0:
			if self._recovery_table_diverges():
				self.recovery_table_out_of_sync = 1
			return

		targets = build_row_targets(
			pure_weights, self.refined_fine_weight, self.refining_loss
		)
		for row, target in zip(self.gold_recovery_details, targets):
			row.update(target)

		self.recovery_table_out_of_sync = 0

	def _recovery_table_diverges(self):
		"""True when the table's loss column no longer sums to the summary loss."""
		if not self.gold_recovery_details:
			return False
		column = flt(sum(flt(r.loss_weight) for r in self.gold_recovery_details), 3)
		return abs(column - flt(self.refining_loss, 3)) > self.SUMMARY_DRIFT_TOLERANCE

	def _validate_mint_identity(self):
		"""The repack entry mints TWO pure outputs; together they cannot exceed the input.

		``create_repack_se`` produces ``sum(refined_gold.refining_gold_weight)`` (== the parent
		``actual_recovery``) of pure 24KT gold AND ``refining_loss`` of pure-24KT dust. So the
		only identity that actually constrains the ledger is

		    actual_recovery + refining_loss <= gross_pure_weight

		Note this is NOT the same as checking ``refining_loss`` against
		``gross_pure_weight - refined_fine_weight``: that is how ``refining_loss`` is defined,
		so it can never fail. The gap is real because ``actual_recovery`` sums
		``refining_gold_weight`` while ``refined_fine_weight`` prefers ``pure_weight``, and the
		two diverge whenever the operator's entered figures disagree.
		"""
		minted = flt(self.actual_recovery) + flt(self.refining_loss)
		available = flt(self.gross_pure_weight)
		# Same 0.1 g assay/rounding margin validate_recovery_distribution already allows.
		if minted > available + 0.1:
			frappe.throw(
				_(
					"Refining would create more pure gold than it consumed: recovered "
					"{0} g plus loss {1} g exceeds the pure gold input of {2} g. Correct "
					"the Recovered Gold rows or the Recovery Summary before proceeding."
				).format(
					flt(self.actual_recovery, 3),
					flt(self.refining_loss, 3),
					flt(available, 3),
				),
				title=_("Refining Mass Balance"),
			)

	def validate_recovery_distribution(self):
		# Refining always yields pure 24KT gold, so the recovered gold is compared against
		# the PURE gold content of the input (gross_pure_weight), not the gross input weight.
		# Diamonds/gemstones are carried 1:1 from the source/BOM, so they are not part
		# of this metal ceiling (they are in different units and cannot be "over-recovered").
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
		fetch uses (_dust_employee_batch_rows), so the two can never diverge."""
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
		for b in bins:
			aq = flt(b.actual_qty, 3)
			if aq > 0:
				total += aq
		return total

	def _dust_employee_batch_rows(self, warehouse):
		"""Per-batch dust rows in ``warehouse`` restricted to Batch.custom_employee ==
		self.employee. Shared by _dust_available_qty (System Quantity) and
		_fetch_loss_items_from_warehouse (Material Items) so the total and the fetched
		rows are computed from the exact same batch enumeration and stay identical.

		Non-batch stock cannot carry the employee marker, so it is excluded entirely
		when an employee filter is active (acceptable per the feature requirement).
		Blocked-customer batches are NOT excluded here (mirroring the unfiltered dust
		path): blocked customers are enforced authoritatively at submit
		(create_material_transfer_se / validate_customer_block), and matching that keeps
		System Quantity consistent with the material rows."""
		rows = []
		if not warehouse or not self.employee:
			return rows
		bins = frappe.db.get_all(
			"Bin",
			filters={"warehouse": warehouse, "actual_qty": [">", 0]},
			fields=["item_code"],
		)
		for b in bins:
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
		(grouped by item group) from the department Scrap warehouse for Dust Refining.
		Consumable rows already added manually are preserved."""
		if self.refining_type != "Dust Refining":
			frappe.throw(_("This action is only available for Dust Refining."))

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

		for row in self.serial_no_details:
			if row.serial_number == serial_no.name:
				frappe.throw(
					_("Serial Number {0} is already added.").format(serial_no.name)
				)

		# Fetch BOM details for pure/gross/net weight from the serial's OWN as-built
		# BOM (custom_bom_no). Each serialized piece has its own BOM capturing its
		# actual weights; falling back to any active BOM for the design item would
		# pull a different piece's weights and mismatch the serial. Only fall back to
		# the item's active BOM when the serial has no BOM linked.
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
		# Prefer the BOM's metal weight (gold alloy only, findings excluded), but fall
		# back to the net (metal+finding) weight when metal_weight is unpopulated — many
		# BOMs (hundreds on real data) carry metal_and_finding_weight but a 0/blank
		# metal_weight, and using 0 would silently drop the serial from recovery.
		metal_weight = (
			flt(bom_details.metal_weight) or flt(bom_details.metal_and_finding_weight)
			if bom_details
			else 0.0
		)
		purity = self.get_item_purity(serial_no.item_code)
		# Pure gold is computed from the METAL weight (gold alloy only), NOT the
		# metal-and-finding (net) weight — findings (F-) are not gold and applying the
		# gold purity to them over-counted the recoverable metal. This is the serial
		# "metal quantity mismatch": the metal figure and the pure/recovery weights
		# disagreed because pure_weight was based on net (metal+finding) weight.
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
						"Batch {0} belongs to a customer blocked from Dust/Scrap refining."
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
			self.append(
				"material_items",
				{
					"item_code": batch.item,
					"warehouse": self.warehouse,
					"qty": qty if qty > 0 else 1.0,
					"batch_no": batch.name,
					"use_serial_batch_fields": 1,
					"source_type": "Scrap",
				},
			)
			return

		item = frappe.db.get_value("Item", {"name": barcode}, "name")
		if item:
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
					"source_type": "Scrap",
				},
			)
			return

		frappe.throw(_("Batch or Item {0} not found.").format(barcode))

	def build_material_table(self):
		"""Consolidate materials from source documents (MWO, SN, etc.) into material_items."""
		self.set("material_items", [])

		if self.refining_type == "Work Order Refining":
			# Work Order material physically sits in the warehouse where it was reserved
			# (the Stock Reservation Entry warehouse), which is not necessarily the MOP
			# department's Manufacturing warehouse. Sourcing the transfer from the MOP
			# warehouse raised "stock is not available"; resolve from the SRE instead.
			sre_wh_map = self._mwo_sre_warehouse_map()
			for mwo_row in self.mwo_details:
				# Use the canonical get_current_mop_balance_rows() to fetch the
				# material the MWO currently holds. This reads the running balance
				# (qty_after_transaction_batch_based) — the same source of truth
				# that get_material_wt() uses to compute the Manufacturing
				# Operation's weight fields (gross_wt, net_wt, etc.).
				#
				# The previous SUM(qty_change) approach diverged from the running
				# balance when MOP Log entries carried baseline clones (qty_change=0
				# but non-zero running balances) during inter-operation handoffs,
				# causing the refining material table to mismatch the MOP details.
				#
				# We query per the LAST NOT-STARTED Manufacturing Operation on the
				# MWO — this is where the material physically sits. If none is
				# found, fall back to the MWO's designated manufacturing_operation.
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
							"warehouse": sre_wh_map.get(
								(
									mwo_row.manufacturing_work_order,
									item_code,
									row.get("batch_no"),
								)
							)
							or sre_wh_map.get(
								(mwo_row.manufacturing_work_order, item_code, None)
							)
							or row.get("to_warehouse")
							or mop_warehouse
							or self.warehouse,
							"qty": qty,
							"uom": uom,
							"source_type": "MWO",
							"purity": purity,
							"manufacturing_work_order": mwo_row.manufacturing_work_order,
							"manufacturing_operation": row.get(
								"manufacturing_operation"
							)
							or last_mop,
						},
					)

		if self.refining_type == "Serial Number Refining":
			for sn_row in self.serial_no_details:
				purity = self.get_item_purity(sn_row.item_code)
				if not purity:
					frappe.throw(
						_(
							"Metal Purity is mandatory for Item {0}. Please check Item Variant Attribute details."
						).format(frappe.bold(sn_row.item_code))
					)
				# Add the FG item row (needed for the serial-number Material
				# Transfer SE — the FG physically moves with its serial).
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

		if self.refining_type == "Dust Refining":
			# Fetch ALL loss items from the department's Scrap warehouse
			if self.multiple_department:
				for d_row in self.refining_department_detail:
					self._fetch_loss_items_from_dept(d_row.department)
			else:
				self._fetch_loss_items_from_dept(self.department)

			# Fallback: if department-based lookup fetched nothing (department
			# not set, or no Scrap warehouse on the department), but
			# self.warehouse IS set (which fetch_dust_balance uses), fetch
			# directly from self.warehouse so the two stay in sync.
			if not self.material_items and self.warehouse:
				self._fetch_loss_items_from_warehouse(self.warehouse)

		# Consolidate the source materials item-wise for display (per the Refining SOP:
		# "If multiple references are entered, quantities are merged item-wise"). The rows
		# are keyed by (item_code, serial_no, warehouse) — deliberately NOT by batch_no, so
		# a single item spread across several FIFO batches shows as ONE consolidated line
		# instead of one line per batch. Physical batch allocation is not lost: it is
		# resolved at submit time by create_material_transfer_se / create_repack_se (which
		# FIFO-allocate any batch-less row), so the visible table stays item-group-wise
		# while the stock movements remain batch-accurate.
		item_group_cache = {}
		grouped_items = {}
		for item in self.material_items:
			# Employee-filtered dust rows are per-batch and MUST stay per-batch: keep
			# batch_no in the grouping key and payload so they neither merge across
			# batches nor lose their batch (which would make submit re-FIFO across ALL
			# batches, breaking the employee filter and diverging from System Quantity).
			# Every other row keeps the historical item-wise merge — row_batch is a
			# constant None there, so the key collapses to the old 3-tuple and MWO/SN/
			# plain-dust grouping is byte-for-byte unchanged.
			dust_batch_row = bool(
				self.refining_type == "Dust Refining"
				and self.employee
				and item.get("batch_no")
			)
			# Customer-owned Work Order material must be consumed from (and tagged with) the
			# customer's OWN batch in the transfer and repack SEs (see _stamp_batch_ownership).
			# Preserve its batch through consolidation — like the employee-dust case — so the
			# exact customer batch flows to the SEs instead of being re-FIFO'd across the
			# warehouse (which could pick a different customer's / regular batch and also
			# raised the "Insufficient batch stock" the reporter hit on RFN-MWO-26-00099).
			customer_batch_row = bool(
				self.refining_type == "Work Order Refining"
				and item.get("batch_no")
				and self._batch_is_customer_owned(item.batch_no)
			)
			row_batch = (
				item.batch_no if (dust_batch_row or customer_batch_row) else None
			)
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
		self.require_refining_role(REFINING_ROLES, _("receive materials"))
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

	def _collect_input_purity_map(self):
		"""Gross input weight per purity percentage, plus a representative item per purity.

		Lifted verbatim out of ``generate_recovery_table`` so the distribution and the
		save-time table heal share ONE definition of "melted gold input" — including the
		external ``_is_returned_intact`` filter and the Serial-Number BOM-components-vs-
		``serial_no_details`` branching. No role gate and no writes: it is safe to call from
		``validate``.

		Returns ``(input_purity_map, input_item_map)``.
		"""
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
					# External refining melts the gold alloy INCLUDING findings: exclude only
					# the returned stones AND the self-referential FG serial BOM row (a piece
					# count, not meltable grams) from the melted-gold distribution — the same
					# "melted metal" predicate _compute_input_pure_weight uses, so the
					# distribution's input weight matches the Recovery Summary. Findings are
					# melted for both internal and external refining (see _is_returned_intact).
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
				# External refining returns diamonds/gemstones intact (see
				# _is_returned_intact); keep them out of the gold-recovery distribution.
				# Gold findings ARE melted (internal and external alike), so they stay in
				# the distribution and contribute their karat's pure gold.
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

		return input_purity_map, input_item_map

	@frappe.whitelist()
	def generate_recovery_table(self, total_recovered_weight=None):
		"""Distribute gold recovery in proportion to each karat's PURE gold content."""
		self.require_refining_role(REFINING_ROLES, _("classify materials"))
		self.set("gold_recovery_details", [])
		self.auto_classify_recoverable_non_metal()

		input_purity_map, input_item_map = self._collect_input_purity_map()

		# Recovered gold is pure 24KT, so it is split in proportion to each karat's
		# PURE gold content, not its gross input weight. A gross-weight split
		# over-allocates recovery to low-purity rows — an 18KT (75.4%) row could be
		# assigned more recovered pure gold than it even contained (recovery > 100%)
		# while the 22KT row showed a matching artificial loss.
		total_pure_input_weight = sum(
			weight * (flt(pct) / 100.0) for pct, weight in input_purity_map.items()
		)
		total_recovered_weight = self.get_recovered_gold_total(total_recovered_weight)
		purity_maps = self.get_purity_distribution_maps(
			input_purity_map, input_item_map
		)

		# Build the row basis first, then apportion every column off the totals in one pass so
		# the columns sum EXACTLY to the summary regardless of how many karats are present
		# (see recovery_distribution.build_row_targets — this is the real fix for the "1 mg
		# variation with more than one touch of gold").
		row_basis = []
		for pmap in purity_maps:
			pmap_pct = flt(pmap.purity_percentage)
			input_weight = input_purity_map.get(pmap_pct, 0)
			if input_weight <= 0:
				continue
			row_basis.append((pmap, input_weight, input_weight * (pmap_pct / 100.0)))

		total_loss = max(
			flt(total_pure_input_weight, 3) - flt(total_recovered_weight, 3), 0.0
		)
		targets = build_row_targets(
			[pure for _pmap, _input, pure in row_basis],
			total_recovered_weight,
			total_loss,
		)

		for (pmap, input_weight, _pure), target in zip(row_basis, targets):
			self.append(
				"gold_recovery_details",
				{
					"karat": pmap.karat,
					"purity_percentage": pmap.purity_percentage,
					"item_code": pmap.item_template,
					"input_weight": flt(input_weight, 3),
					**target,
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
		self.require_refining_role(REFINING_ROLES, _("enter recovered gold"))
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

		# Calculate and persist recovery details row by row via db_set.
		# Split in proportion to each row's PURE gold content (see
		# generate_recovery_table): a gross-weight split let a low-purity row
		# "recover" more pure gold than it contained (recovery % above 100).
		#
		# Apportioned with the largest-remainder method rather than rounded row by row, so
		# every column sums back to its total EXACTLY for any number of karats. That is the
		# real fix for the "1 mg variation with more than one touch of gold": the drift was
		# sum-of-rounded vs round-of-sum, and it is removed here at the WRITER instead of by
		# making the Recovery Summary adopt whatever the table happens to hold.
		#
		# Loss is apportioned too, NOT computed per row as max(pure - recovered, 0): that
		# clamp discards one row's negative against another's positive, so the column could
		# sum to more gold than actually went missing (one real entry read 0.860 g against a
		# 0.033 g mass balance).
		pure_weights = [flt(row.pure_gold_weight) for row in self.gold_recovery_details]
		total_loss = max(
			flt(total_pure_input_weight, 3) - flt(total_recovered_weight, 3), 0.0
		)
		targets = build_row_targets(pure_weights, total_recovered_weight, total_loss)

		for row, target in zip(self.gold_recovery_details, targets):
			# Update in-memory for downstream use
			row.update(target)

			# Persist directly to DB, bypassing validate_update_after_submit
			row.db_set(dict(target))

		# Rebuild refined_gold child table via DB operations
		self._rebuild_refined_gold_via_db()

		# Recalculate and persist parent totals
		self._recalculate_and_persist_totals()

		self.db_set("status", "Recovery Entered")

	def _rebuild_refined_gold_via_db(self):
		"""Delete existing refined_gold rows and insert new ones based on recovery details."""
		# Delete existing refined_gold rows for this parent
		frappe.db.delete("Refined Gold", {"parent": self.name})

		# Refining always yields pure 24KT gold regardless of the input karat, and the
		# repack Stock Entry books the single 24KT pure-gold item. The Refined Gold table
		# must therefore show 24KT (the pure output), not the input karat (18/22KT) — it
		# previously carried the input karat/item, which is what showed "22KT".
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

		# Derived from the PARENT inputs, never from the Gold Recovery Details table — the
		# table is apportioned FROM these totals instead (see calculate_totals for why the
		# other direction was wrong). Clamp at 0: recovered 24KT gold can slightly exceed the
		# computed pure input (rounding / assay variance) and must never book a negative loss.
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

		# This path bypasses validate (db_set), so heal the table explicitly and persist the
		# rows the same way distribute_recovered_gold does. db_set is a no-op on an unsaved
		# row (name is None), so only persist rows that already exist.
		self._sync_recovery_table()
		for row in self.gold_recovery_details:
			if not row.name:
				continue
			row.db_set(
				{
					"pure_gold_weight": flt(row.pure_gold_weight, 3),
					"recovered_weight": flt(row.recovered_weight, 3),
					"loss_weight": flt(row.loss_weight, 3),
					"recovery_pct": flt(row.recovery_pct, 2),
				}
			)
		self.db_set("recovery_table_out_of_sync", cint(self.recovery_table_out_of_sync))

	@frappe.whitelist()
	def verify_recovery(self):
		self.require_refining_role(REFINING_MANAGER_ROLES, _("verify recovery"))
		self.validate_recovery_distribution()
		self.validate_recovered_non_metal()
		self.db_set("status", "Recovery Verified")

	@frappe.whitelist()
	def complete_refining(self):
		self.require_refining_role(REFINING_MANAGER_ROLES, _("complete refining"))
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

				op = frappe.db.get_value(
					"Manufacturing Work Order",
					mwo.manufacturing_work_order,
					"manufacturing_operation",
				)
				if not op:
					continue

				# Create a 0-balance MOP Log for every active item in the current operation
				# so that future operations read a 0 balance from the ledger.
				balance_rows = get_current_mop_balance_rows(op)
				if balance_rows:
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
				# Deactivate the serial's OWN as-built BOM (custom_bom_no) — the physical
				# piece has been melted down. Falling back to the design item's generic
				# active BOM (as before) deactivated the WRONG BOM: an item can have
				# several active per-serial BOMs and multiple serials, so the generic
				# lookup both left this piece's BOM active and could disable a BOM other
				# pieces still depend on. Fall back to the active BOM only when the serial
				# has no BOM linked.
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
		self.require_refining_role(
			REFINING_MANAGER_ROLES, _("transfer recovered materials")
		)
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

		# Work Order Refining: release the source MWO Stock Reservation Entries BEFORE
		# building/submitting the transfer, so this Stock Entry can consume the now
		# unreserved stock (otherwise the submit fails with "stock is not available").
		# This runs at submit time — NOT on every draft save — so an abandoned/never
		# submitted draft never permanently loses its reservations, and the SRE-based
		# source-warehouse resolution in build_material_table stays valid across re-scans
		# while the entry is still in Draft.
		if self.refining_type == "Work Order Refining":
			self._cancel_source_mwo_sres()

		precision = 3
		min_qty = 0.001

		# Cache item attributes to prevent repetitive DB calls and speed up insertion
		item_batch_map = {}
		for item in self.material_items:
			if item.item_code not in item_batch_map:
				item_batch_map[item.item_code] = frappe.db.get_value(
					"Item", item.item_code, "has_batch_no"
				)

		self._dust_shortfalls = []
		block_customer = self.refining_type in ("Dust Refining", "Scrap Refining")
		for item in self.material_items:
			if item.get("source_type") == "BOM Component":
				continue

			# Authoritative guard: never move a blocked customer's explicit batch into
			# Dust/Scrap refining — internal OR external (belt-and-suspenders over
			# validate_customer_block and the fetch-time exclusion; also covers
			# programmatic callers).
			if (
				block_customer
				and item.get("batch_no")
				and self._is_blocked_customer_batch(item.batch_no)
			):
				frappe.throw(
					_(
						"Batch {0} belongs to a customer blocked from Dust/Scrap refining."
					).format(frappe.bold(item.batch_no))
				)

			# We no longer skip dust opening items here; we attempt to transfer everything
			# and record any shortfalls (missing stock) for the receipt step.

			# Serial Number Refining: the FG serial item IS transferred to the refining
			# warehouse at submit (like every other refined material), so it is NOT skipped
			# here. Its warehouse/department legitimately moves to the refinery; repack then
			# consumes it from the refining warehouse.

			s_wh = item.warehouse or self.warehouse
			has_batch = item_batch_map.get(item.item_code)

			if has_batch:
				use_fifo = True
				if item.batch_no:
					from erpnext.stock.doctype.batch.batch import get_batch_qty

					batch_qty = get_batch_qty(
						batch_no=item.batch_no, warehouse=s_wh, item_code=item.item_code
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
						throw_if_missing=(self.refining_type != "Dust Refining"),
						exclude_blocked_customer=block_customer,
					)

					allocated_qty = sum(flt(a["qty"], precision) for a in allocations)
					if self.refining_type == "Dust Refining" and allocated_qty < flt(
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
				if self.refining_type == "Dust Refining":
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

		if se.items:
			# Keep customer-owned metal tagged Customer Goods through the transfer into the
			# refining/supplier warehouse (auto_created SEs skip the generic backfill).
			self._stamp_batch_ownership(se)
			se.insert(ignore_permissions=True)
			se.submit()
			self.db_set("material_transfer_se", se.name)

		# Now that the MWO material is physically transferred into the refinery, zero the
		# source MWO / pending-operation weights. (SREs were already cancelled above,
		# before the transfer, respecting the canonical lock order SRE -> SLE -> MOP Log
		# -> Manufacturing Operation.)
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

	def _mwo_sre_warehouse_map(self):
		"""Map (mwo, item_code, batch_no) -> the SRE reservation warehouse for every
		active Stock Reservation Entry under the scanned MWOs, with an item-level
		(batch_no=None) fallback. Work Order material physically sits in the reserved
		warehouse, so build_material_table sources the transfer from here rather than
		the MOP department's Manufacturing warehouse."""
		wh_map = {}
		for row in self.mwo_details:
			mwo = row.manufacturing_work_order
			if not mwo:
				continue
			sres = frappe.db.get_all(
				"Stock Reservation Entry",
				filters={
					"manufacturing_work_order": mwo,
					"docstatus": 1,
					"status": ["not in", ("Cancelled", "Delivered")],
				},
				fields=[
					"name",
					"item_code",
					"warehouse",
					"has_batch_no",
					"reservation_based_on",
				],
			)
			for sre in sres:
				wh_map[(mwo, sre.item_code, None)] = sre.warehouse
				if cint(sre.has_batch_no) and sre.reservation_based_on != "Qty":
					for sb in frappe.db.get_all(
						"Serial and Batch Entry",
						filters={"parent": sre.name},
						fields=["batch_no"],
					):
						if sb.batch_no:
							wh_map[(mwo, sre.item_code, sb.batch_no)] = sre.warehouse
		return wh_map

	def _cancel_source_mwo_sres(self):
		"""Cancel the active Stock Reservation Entries linked to each refined MWO.
		SREs are linked to the MWO via the custom `manufacturing_work_order` Data field
		(SRE.voucher_no is the shared Sales Order, so we must filter by MWO and pass
		sre_list explicitly)."""
		from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
			cancel_stock_reservation_entries,
		)

		names = []
		for row in self.mwo_details:
			if not row.manufacturing_work_order:
				continue
			names += frappe.db.get_all(
				"Stock Reservation Entry",
				filters={
					"manufacturing_work_order": row.manufacturing_work_order,
					"docstatus": 1,
					"status": ["not in", ("Cancelled", "Delivered")],
				},
				pluck="name",
			)
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
		# When the physical dust exceeds the system stock, the extra dust must be brought
		# into the refining warehouse (or, for external refining, the supplier warehouse
		# it was physically handed over to) via a Material Receipt so the downstream
		# repack/receive can consume it.
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
		# clean up after a failed Manufacture submit whose on_submit cascade may have already
		# created & SUBMITTED linked Stock Entries and renamed output Batches; rolling back to
		# this savepoint discards the auto-created batches, the inserted draft, the submit, and
		# every cascade side effect in one shot. Only work done inside create_repack_se is
		# rolled back — complete_refining's earlier writes precede the savepoint and survive.
		# This entry mints pure gold AND pure dust; refuse before either reaches the ledger.
		self._validate_mint_identity()

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

		# Running per-(item, batch) tally of what THIS repack SE has already booked for
		# consumption. get_batch_qty returns the DB balance, which does not drop as we
		# append lines to the in-memory SE, so without this tally two material rows
		# sharing a batch (e.g. a transferred dust row + the dust-opening row) each cap
		# against the same stale balance and jointly over-draw it — driving the batch
		# negative on submit ("negative batch quantity").
		se_consumed = {}

		# Per-batch consumption guard. DEFAULT 0.0 → consume the full available balance so
		# NO stock is stranded on the common (clean) path. It is only raised on retry
		# (see create_repack_se's except) to absorb ERPNext's intermittent Serial-and-
		# Batch-Bundle Manufacture overshoot, which can remove a few thousandths of a gram
		# MORE than the row qty when dozens of tiny batches are drawn at once and trip its
		# global-Batch.batch_qty negative-batch validation. Reacting only when that
		# actually happens keeps normal repacks exact and confines any tiny stranding to
		# the rare warehouse that needs it.
		batch_consume_guard = getattr(self, "_repack_guard", 0.0)

		def _batch_available(item_code, batch_no):
			from erpnext.stock.doctype.batch.batch import get_batch_qty

			# Cap at the smaller of the per-warehouse SBB balance and the GLOBAL
			# Batch.batch_qty cache (the value the submit-time negative-batch validation
			# checks). They can drift apart on real data. Net of what this SE has already
			# booked for the batch (se_consumed) so rows sharing a batch never double-draw
			# it, and of the guard above.
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

						# Cap by the LIVE batch balance net of what this SE already booked
						# for the batch (see se_consumed): SBB rounding can leave the landed
						# batch a hair short, and two rows sharing the batch must not both
						# cap against the same stale balance, or the batch goes negative.
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

				# 3. Fallback to FIFO. Non-throwing: after consuming the brought-in and
				# original batches, any leftover is a sub-precision rounding remainder
				# (a few thousandths of a gram) with no matching batch stock — consume
				# what is actually available and let the refining loss absorb the rest,
				# rather than aborting the whole completion with "Insufficient batch stock".
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
		# one customer's (no company/other-owner metal blended in) — otherwise the single
		# pure-gold output would misclassify that gold. _recovered_output_customer enforces
		# that and the Customer-Goods item-flag gate (see it for the rationale).
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
					"type": "Scrap",
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
					"type": "Scrap",
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
				# Loss dust is a pure OUTPUT of the repack (like the recovered gold /
				# diamond / gemstone rows above): t_warehouse only. It previously also set
				# s_warehouse=refining_warehouse, which made the Manufacture SE CONSUME the
				# loss from the freshly-created (empty) batch, driving it negative on submit
				# ("negative batch quantity"). The loss is moved to the scrap warehouse
				# afterwards by create_scrap_transfer_se.
				se.append(
					"items",
					{
						"item_code": dust_item,
						"qty": self.refining_loss,
						"uom": "Gram",
						"t_warehouse": self.refining_warehouse,
						"batch_no": dust_batch,
						"type": "Scrap",
						"is_finished_item": 0,
						"use_serial_batch_fields": 1,
					},
				)

		se.insert(ignore_permissions=True)
		try:
			se.submit()
		except Exception as e:
			# Retry on ERPNext's intermittent SBB negative-batch overshoot by rebuilding
			# with a larger per-batch guard. Roll the whole attempt back to the entry
			# savepoint first — this undoes the inserted draft, the (partial) submit and its
			# cascade, and the empty output Batches this attempt auto-created, without the
			# fragility of deleting an already-submitted/linked Stock Entry. Match both the
			# "negative batch" and generic "Negative Stock" wordings so localised/reworded
			# messages still self-heal.
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
		self._validate_mint_identity()
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
				# Non-throwing: the loss dust to move out is whatever the repack actually
				# booked into the refining warehouse, which can be a few thousandths short
				# of self.refining_loss after SBB rounding. Move what is available instead
				# of aborting completion with "Insufficient batch stock".
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
						"source_type": "Dust",
						"purity": purity,
					},
				)
			return

		bins = frappe.db.get_all(
			"Bin",
			filters={"warehouse": warehouse, "actual_qty": [">", 0]},
			fields=["item_code", "actual_qty"],
		)

		for b in bins:
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
					"source_type": "Dust",
					"purity": purity,
				},
			)

	@frappe.whitelist()
	def get_scrap_items_balance(self):
		"""Fetch available scrap stock from the department warehouse(s).

		Manufacturing scrap is received back into the department under the SAME item
		code (there is no dedicated scrap item) but a batch tagged
		``custom_batch_type = "Scrap"`` by the Manufacturing Operation "Receive Scrap
		Item" action. This fetches ONLY Scrap-typed batches (optionally narrowed to the
		selected Scrap Item), so ordinary department stock sharing a warehouse is never
		pulled in. Non-batch stock cannot carry the Scrap marker and is excluded.
		"""
		if self.refining_type != "Scrap Refining":
			frappe.throw(_("This action is only available for Scrap Refining."))

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
			for b in bins:
				purity = self.get_item_purity(b.item_code)
				uom = frappe.db.get_value("Item", b.item_code, "stock_uom") or "Gram"
				item_group = frappe.db.get_value("Item", b.item_code, "item_group")

				# Scrap is always batch-tracked and marked custom_batch_type="Scrap".
				# Non-batch stock cannot carry the marker, so it is never scrap.
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
					# Only Scrap-tagged batches are eligible for Scrap Refining.
					# When an Employee is selected, additionally restrict to that
					# employee's own scrap batches (Batch.custom_employee). Fetch both
					# markers in one call. Batches minted before custom_employee existed
					# return None and are correctly excluded when an employee is set
					# (forward-only, per requirement).
					btype, bemp = frappe.db.get_value(
						"Batch",
						a.get("batch_no"),
						["custom_batch_type", "custom_employee"],
					) or (None, None)
					if btype != "Scrap":
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
		keep ALL of a blocked customer's batches out of Dust/Scrap refining."""
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
		ownership, and return the set of customers seen. Refining SEs are auto_created, so
		the generic batch->row backfill (CustomStockEntry.update_batches) is skipped and
		doc_events/stock_entry.py forces blank rows to 'Regular Stock' — call this before
		insert so customer-owned batches keep their ownership across the transfer/repack/
		outbound moves. Ownership is resolved via the app's single source of truth
		(row_ownership.resolve_batch_ownership), so the Rule 2/3 coherence checks (a customer
		type with no customer downgrades to Regular Stock) are applied uniformly."""
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

		Only when EVERY consumed (input) batched row belongs to the SAME single customer —
		no company (Regular Stock) metal and no second owner. The repack blends all inputs
		into one pure-metal output, so a mixed lot cannot be attributed to one customer
		without misclassifying the company's / another owner's gold; such a lot conservatively
		downgrades to Regular Stock (company holds it for manual reconciliation) rather than
		over-claim. Call AFTER ``_stamp_batch_ownership`` (so consumed rows carry their
		resolved customer) and BEFORE the output rows are appended (so only inputs are seen).

		Also gated on ``_item_allows_customer_goods``: minting a Customer Goods batch for an
		item without the flag hard-fails in Batch.update_inventory_dimentions, so downgrade
		with a note instead."""
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
		blocked customer for Dust/Scrap refining (internal or external), so the operator
		sees it before the Stock Entry is built. Dust rows are batch-less until FIFO
		resolves at submit — those are enforced authoritatively in
		``create_material_transfer_se``."""
		if self.refining_type not in ("Dust Refining", "Scrap Refining"):
			return
		for row in self.material_items:
			if row.get("batch_no") and self._is_blocked_customer_batch(row.batch_no):
				cust = frappe.db.get_value("Batch", row.batch_no, "custom_customer")
				frappe.throw(
					_(
						"Batch {0} (item {1}) belongs to customer {2}, who is blocked from "
						"Dust/Scrap refining. Remove it before submitting."
					).format(
						frappe.bold(row.batch_no), row.item_code, frappe.bold(cust)
					)
				)

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
		variants (which the old exact "== 'Diamond'" / D-,DL- checks silently dropped) — are
		applied ONLY for external refining, where every non-metal item is handed back intact
		and so must be recognised. Gating the broadening on is_external keeps INTERNAL
		recovery classification (auto_classify_recoverable_non_metal) byte-for-byte
		unchanged; the identical DM-/DBK-/DB- loss in internal refining is a separate,
		pre-existing bug deliberately left untouched here."""
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

	# is_finding_item() was removed here deliberately, not lost. Findings classification had
	# exactly one consumer — _is_returned_intact — and that stopped calling it when findings
	# became meltable gold (they are melted and recovered as pure 24KT, so they are NOT handed
	# back intact). It then had zero callers bench-wide. Refining draws no distinction between
	# a finding and any other gold alloy row; only diamonds and gemstones are returned. If a
	# finding-specific rule is ever needed again, restore it from git history rather than
	# assuming the classification was still live.

	def _is_returned_intact(self, item_code):
		"""External refining (ALL types): diamonds and gemstones travel to the refiner
		alongside the metal but are NOT melted — the refiner processes the gold alloy
		(including gold FINDINGS, which are melted) and hands the stones back physically.
		The returned stones are therefore (a) excluded from the pure-metal weight /
		recovery computations and (b) re-output into the source department's Raw Material
		warehouse on receipt (receive_from_supplier). This applies to Dust, Scrap, Work
		Order and Serial Number external refining alike.

		NOTE (2026-07): findings are gold alloy and ARE melted/recovered as pure gold, so
		they are NOT returned intact — they are excluded here so their weight flows into
		the melted-metal input and the recovery distribution (matching internal refining,
		which already melts findings). Only diamonds and gemstones remain returned."""
		return self.is_diamond_item(item_code) or self.is_gemstone_item(item_code)

	def auto_classify_recoverable_non_metal(self):
		diamond_items = {row.item for row in self.recovered_diamond}
		gemstone_items = {row.item for row in self.recovered_gemstone}

		if self.refining_type == "Serial Number Refining":
			for sn in self.serial_no_details:
				# Use the serial's OWN as-built BOM (custom_bom_no) for the diamond/
				# gemstone weights, mirroring scan_serial_no_action. Each serialized
				# piece has its own BOM capturing its actual stone weights; the design
				# item's generic active BOM belongs to a different piece and yields
				# mismatched (often empty) stone weights — the reported "diamond weight
				# is not match" bug. Fall back to the active BOM only when the serial
				# has no BOM linked.
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
		"""Return the pure 24KT loss item for the manufacture entry across all
		four refining types (Dust, Scrap, Work Order, Serial Number).

		The refining loss is always a pure-equivalent quantity, so the loss row
		must always use ML-G-24KT-99.9-Y.  Falls back to the resolution chain
		in ``_get_pure_loss_item`` only when that item does not exist."""
		pure_loss = "ML-G-24KT-99.9-Y"
		if frappe.db.exists("Item", pure_loss):
			return pure_loss
		return self._get_pure_loss_item()

	def _get_pure_loss_item(self):
		"""Loss item for non-Dust (Scrap / Work Order / Serial) refining.

		The refining loss (``self.refining_loss``) is a PURE (24KT-equivalent) gram
		quantity — see ``_recalculate_and_persist_totals`` — so the loss row of the
		repack Manufacture entry must carry the PURE karat. Booking it against an
		input-karat item (the first ML-G-22KT input row, or a manually picked 22KT
		loss item) put pure grams on a ~91.6%-purity item: the reported "loss shows
		the 22KT metal code" bug. Resolution order:

		  1. the operator-picked ``loss_item``, unless it carries a non-pure karat;
		  2. an existing Metal Loss variant of the pure karat (ML-G-24KT…);
		  3. derive/create the ML variant of the pure gold item (Variant Loss Table
		     mapping, same helper Main Slip uses for process loss);
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
		if self.refining_type != "Dust Refining":
			return False
		if self.loss_item:
			return item.item_code == self.loss_item
		return item.get("source_type") != "Dust"

	def get_dust_opening_qty(self, dust_item):
		dust_qty = 0.0
		for item in self.material_items:
			if self.is_dust_opening_item(item):
				dust_qty += flt(item.qty)
		if not dust_qty and flt(self.difference_quantity) > 0:
			dust_qty += flt(self.additional_dust_qty) or flt(self.difference_quantity)
		return dust_qty

	def _opening_dust_qty(self):
		"""Quantity of extra ('opening') dust to receive when physical exceeds system.

		A POSITIVE additional_dust_qty is an operator override (they can receive LESS
		than the full physical-vs-system difference — e.g. RFN-DST-26-00067 kept 0.5 of a
		1.0 difference) and is honoured as-is.

		When additional_dust_qty lands at 0 we fall back to the authoritative positive
		difference_quantity instead of treating 0 as 'add no dust'. additional_dust_qty is
		only ever populated by a client-side snapshot taken the instant physical_quantity
		is typed, whereas difference_quantity is recomputed server-side on every save
		(set_dust_system_quantity + validate_quantities). When the system qty shifts
		between those two moments the snapshot goes stale at 0 while a real positive
		difference remains — which silently skipped the Material Receipt and stranded
		physical gold (observed on RFN-DST-26-00074/81/85/88). Since physical > system
		means there genuinely IS excess dust to bring in, defaulting to the difference is
		the safe behaviour. Still the single source of truth, so the opening material row
		and the receipt SE always agree."""
		override = flt(self.additional_dust_qty)
		if override > 0:
			return override
		return max(flt(self.difference_quantity), 0.0)

	def validate_dust_opening_material(self):
		"""Dust Refining: the extra ('opening') dust for a physical-over-system difference
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
				"source_type": "Dust",
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

	def require_refining_role(self, allowed_roles, action):
		if frappe.session.user == "Administrator":
			return
		user_roles = set(frappe.get_roles(frappe.session.user))
		if user_roles.intersection(set(allowed_roles)):
			return
		frappe.throw(
			_("Only users with {0} can {1}.").format(", ".join(allowed_roles), action),
			frappe.PermissionError,
		)

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
	):
		from erpnext.stock.doctype.batch.batch import get_batch_qty

		# In v16, per-batch stock lives in the Serial and Batch Bundle, NOT in
		# `tabStock Ledger Entry.batch_no` (which is NULL) nor in a plain
		# `Serial and Batch Entry` sum by warehouse. Querying those raw tables
		# returned zero candidate batches for real SBB stock, which dropped batched
		# items from the material table (Dust/Scrap fetch) and raised false
		# "Insufficient batch stock" errors on submit. get_batch_qty() is the
		# SBB-aware source of truth and already returns available batches in the
		# configured pick order (FIFO), netting reserved/consumed stock.
		batches = [
			b
			for b in (get_batch_qty(item_code=item_code, warehouse=warehouse) or [])
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
				_(
					"Insufficient batch stock found for Item {0} in Warehouse {1}. "
					"Required: {2}, Missing: {3}. Please ensure batch stock is available before submitting."
				).format(
					frappe.bold(item_code),
					frappe.bold(warehouse),
					required_qty,
					shortfall,
				)
			)

		return allocations
