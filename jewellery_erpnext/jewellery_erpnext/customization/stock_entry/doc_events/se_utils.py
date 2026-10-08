import copy

# import erpnext
import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	get_auto_batch_nos,
)

# from erpnext.stock.utils import (
# 	_get_fifo_lifo_rate,
# 	get_serial_nos_data,
# 	get_valuation_method,
# )
from frappe import _

# from frappe.query_builder import Case
# from frappe.query_builder.functions import CombineDatetime, Locate, Sum
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.hybrid_findings import (
	batch_is_eligible,
	get_batch_ownership_map,
	hybrid_requirement,
	shortfall_hint,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.subcontracting_utils import (
	create_subcontracting_doc,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.bom_weights import (
	apply_bom_weights,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.diamond_conversion_batches import (
	get_diamond_conversion_target_batches,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_INVENTORY_TYPES,
	STRICT_CUSTOMER_GOODS_VARIANTS,
	normalize_ownership,
	pmo_expects_customer_goods,
	pmo_requires_company_stock,
	pmo_requires_customer_goods,
	resolve_batch_ownership,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.sample_goods import (
	SAMPLE_ALLOWED_SE_TYPES,
	get_sample_batches,
)
from jewellery_erpnext.utils import bulk_map

# from jewellery_erpnext.utils import get_item_from_attribute


#: While True an ownership mismatch is only logged instead of thrown.
#:
#: STAGED ROLLOUT, NOT A PREFERENCE. ``validate_inventory_dimention`` was commented out by
#: 0040324c ("fix: sync v15 updates with v16", 8 May 2026) -- a 17-file bulk sync that gave no
#: reason -- and stayed dead for months, so production holds submitted Stock Entries that this
#: guard would now refuse, including legitimately finished work. Run the
#: ``audit_pmo_row_ownership`` report first; flip this to False once it comes back clean.
WARN_ONLY_PMO_ROW_OWNERSHIP = True


def _report_ownership_error(message, soft=False, hard=False):
	"""Throw, or -- while the guard is staged or the manufacturer opted out -- only log it.

	``hard`` throws regardless of both: a diamond or gemstone row draws exactly the owner its
	order names, never a substitute in either direction.

	A case that is let through is NOT shown to the user. The allocator already picks the right
	owner's batch, and a pop-up that changes nothing -- twice, once on save and again on submit --
	only taught people to click it away. It goes to the ``inventory_ownership`` log instead, which
	``audit_pmo_row_ownership`` complements for history.
	"""
	if hard or not (soft or WARN_ONLY_PMO_ROW_OWNERSHIP):
		frappe.throw(message, title=_("Inventory Ownership"))
	_log_ownership_warning(message)


def _log_ownership_warning(message):
	frappe.logger("inventory_ownership").warning(frappe.utils.strip_html(message))


def validate_inventory_dimention(self, method=None):
	"""A row must draw the owner's material its order was placed on (F5 enforcement).

	WHAT THIS REFUSES
	-----------------
	1. Material owned by somebody other than this order's customer.
	2. Company stock on a row the order says the customer supplied.
	3. Customer-owned stock on a row the order says the company supplies.

	WHY IT READS THE BATCH AND NOT THE ROW
	--------------------------------------
	The original compared ``row.customer`` against the order's. That is the row's own claim about
	itself, and the claim is exactly what goes wrong: KLHGX62F1119's company diamond travelled as
	"Customer Goods" with a NULL customer through MAT-STE-18637/38/39 while the batch it drew was
	plain Regular Stock. ``resolve_batch_ownership`` applies the rule the rest of the app already
	shares -- the batch is the physical truth and wins over the row -- so a stale or hand-typed
	value cannot pass a check the stock itself would fail.

	SCOPE
	-----
	Only rows that CONSUME (``s_warehouse``) against a Parent Manufacturing Order. A pure inward row
	has nothing to have drawn wrongly, and Customer Gold receipts have no batch at all until
	``batch_rename.create_parent_batches`` mints one at ``before_submit`` -- judging them here would
	refuse every receipt.

	Runs on ``validate``, not the ``on_submit`` it was originally wired to: by ``validate`` the
	batches are allocated (``update_batches`` runs in ``before_validate``) and the user is told on
	save, before anything moves.

	DIAMONDS AND GEMSTONES ARE BLOCKED OUTRIGHT
	-------------------------------------------
	A diamond or gemstone row draws exactly the owner its order names: the customer's own goods
	when customer diamond / gemstone is ticked, company Regular Stock when it is not. All three
	refusals throw for them whatever the staged rollout or the manufacturer's allowance says --
	but only on an entry somebody built by hand (``auto_created`` unset), which is where batches
	are chosen. An auto-created follow-on
	move (Material Transfer From Reserve, the IR cascades) only carries what an earlier entry
	already drew, and refusing it would strand work reserved before this rule existed.
	"""
	pmo_cache = {}
	manufacturer_cache = {}
	chosen_here = not self.get("auto_created")

	# Narrowed BEFORE any query: most Stock Entries name no order at all, and this runs on
	# every single save. A row that consumes nothing, or answers to no order, costs nothing.
	header_pmo = self.get("manufacturing_order")
	rows = [
		row
		for row in self.items
		if row.get("s_warehouse")
		and (row.get("custom_parent_manufacturing_order") or header_pmo)
	]
	if not rows:
		return

	# One query for the variant letters, so the per-row resolution below costs nothing. The
	# fetched ``custom_variant_of`` cannot be relied on -- see ``row_variant_of``.
	item_map = bulk_map("Item", [row.get("item_code") for row in rows], ["variant_of"])
	# Likewise the batches: one round-trip for the document, not one get_value per row.
	batch_map = bulk_map(
		"Batch",
		[row.get("batch_no") for row in rows],
		["custom_inventory_type", "custom_customer"],
	)

	for row in rows:
		pmo_list = row.get("custom_parent_manufacturing_order") or header_pmo
		inventory_type, customer = resolve_batch_ownership(
			row, batch=batch_map.get(row.get("batch_no")) or {}
		)
		is_customer_owned = inventory_type in CUSTOMER_INVENTORY_TYPES

		for pmo in str(pmo_list).split(","):
			pmo = pmo.strip()
			if not pmo:
				continue

			if pmo not in pmo_cache:
				pmo_cache[pmo] = frappe.db.get_value(
					"Parent Manufacturing Order",
					pmo,
					[
						"is_customer_gold",
						"is_customer_diamond",
						"is_customer_gemstone",
						"is_customer_material",
						"customer",
						"manufacturer",
					],
					as_dict=1,
				)

			pmo_data = pmo_cache[pmo]
			# A deleted or mistyped order is not this guard's business to report, and the
			# original raised TypeError here by subscripting the None straight away.
			if not pmo_data:
				continue

			manufacturer = pmo_data.get("manufacturer")
			if manufacturer not in manufacturer_cache:
				manufacturer_cache[manufacturer] = frappe.db.get_value(
					"Manufacturer",
					manufacturer,
					"custom_allow_regular_goods_instead_of_customer_goods",
				)
			# The manufacturer's standing permission to put company stock in the customer's
			# place. It downgrades the two "wrong lane" refusals to a warning; it is NOT
			# permission to consume a DIFFERENT customer's goods, so (1) ignores it.
			allow_substitution = bool(manufacturer_cache[manufacturer])

			variant_of = row_variant_of(row, item_map)
			expects_customer_goods = pmo_expects_customer_goods(pmo_data, variant_of)
			strict = chosen_here and variant_of in STRICT_CUSTOMER_GOODS_VARIANTS

			if is_customer_owned and customer != pmo_data.get("customer"):
				_report_ownership_error(
					_(
						"Row #{0} ({1}): batch {2} belongs to {3}, but {4} was placed for {5}. "
						"A customer's material may only be consumed on that customer's order."
					).format(
						row.get("idx"),
						row.get("item_code"),
						frappe.bold(row.get("batch_no") or _("(no batch)")),
						frappe.bold(customer or _("nobody")),
						frappe.bold(pmo),
						frappe.bold(pmo_data.get("customer") or _("no customer")),
					),
					hard=strict,
				)
				continue

			if expects_customer_goods and not is_customer_owned:
				_report_ownership_error(
					_(
						"Row #{0} ({1}): {2} is a customer-supplied order, but batch {3} is {4}. "
						"Can not use regular stock inventory for Customer provided Item."
					).format(
						row.get("idx"),
						row.get("item_code"),
						frappe.bold(pmo),
						frappe.bold(row.get("batch_no") or _("(no batch)")),
						frappe.bold(inventory_type),
					),
					soft=allow_substitution,
					hard=strict,
				)
				continue

			if is_customer_owned and not expects_customer_goods:
				_report_ownership_error(
					_(
						"Row #{0} ({1}): batch {2} is {3} owned by {4}, but {5} does not say the "
						"customer supplied this material. Can not use Customer Goods inventory "
						"for non provided customer Item."
					).format(
						row.get("idx"),
						row.get("item_code"),
						frappe.bold(row.get("batch_no") or _("(no batch)")),
						frappe.bold(inventory_type),
						frappe.bold(customer or _("nobody")),
						frappe.bold(pmo),
					),
					soft=allow_substitution,
					hard=strict,
				)

				if row.custom_variant_of in variant_mapping:
					customer_key = variant_mapping[row.custom_variant_of]
					if pmo_data.get(customer_key) and row.inventory_type not in [
						"Customer Goods",
						"Customer Stock",
					]:
						if allow_customer_goods:
							frappe.msgprint(
								_(
									"Can not use regular stock inventory for Customer provided Item"
								)
							)
						else:
							frappe.throw(
								_(
									"Can not use regular stock inventory for Customer provided Item"
								)
							)
					elif not pmo_data.get(customer_key) and row.inventory_type in [
						"Customer Goods",
						"Customer Stock",
					]:
						if allow_customer_goods:
							frappe.msgprint(
								_(
									"Can not use Customer Goods inventory for non provided customer Item"
								)
							)
						else:
							frappe.throw(
								_(
									"Can not use Customer Goods inventory for non provided customer Item"
								)
							)


def get_fifo_batches(self, row, consumed=None, batch_cache=None):

def row_variant_of(row, item_map=None):
	"""``Item.variant_of`` for a row, WITHOUT trusting the fetched field to have landed yet.

	WHY NOT JUST READ ``row.custom_variant_of``
	-------------------------------------------
	That field is ``fetch_from: item_code.variant_of``, which Frappe resolves inside ``_validate()``
	-- i.e. during ``validate``, AFTER ``before_validate``. ``update_batches`` (and so this module's
	allocator) runs in ``before_validate``, so on the FIRST save of a server-built Stock Entry whose
	rows were appended as plain dicts the letter is still empty.

	The Material Request reserve entry (``doc_events/material_request.create_stock_entry``) is built
	exactly that way. Its rows carried ``custom_parent_manufacturing_order`` but no variant letter, so
	the Customer Goods lane below never applied and FIFO happily took the oldest batch in the
	warehouse -- a company one -- on every customer-diamond and customer-gemstone order. Reading the
	Item master removes the ordering dependency for every caller instead of patching one builder.

	``item_map`` is the ``bulk_map("Item", ..., ["variant_of", "has_batch_no"])`` the caller may
	already hold (``CustomStockEntry.update_batches`` does), so the common path costs no extra query.
	"""
	item_code = row.get("item_code")
	if not item_code:
		return None

	if row.get("custom_variant_of"):
		return row.get("custom_variant_of")

	if item_map is not None:
		return (item_map.get(item_code) or {}).get("variant_of")

	return frappe.db.get_value("Item", item_code, "variant_of")


def get_fifo_batches(self, row, consumed=None, item_map=None):
	rows_to_append = []
	# `consumed` tracks how much has already been allocated per (warehouse, batch)
	# across all rows of the same document. Callers that process multiple rows for
	# the same item must pass a shared dict so a batch is not double-booked (which
	# would otherwise push it negative on submit). Defaults to a fresh dict so
	# single-row callers behave exactly as before.
	if consumed is None:
		consumed = {}
	# `batch_cache` memoizes the raw candidate-batch fetch (get_auto_batch_nos /
	# get_batch_data_from_msl) per (item_code, warehouse[, msl]) within one
	# update_batches() run. That fetch is the single most expensive part of this
	# function -- profiled at ~100-140ms per row, largely core ERPNext SQL -- and a
	# Department transfer commonly has several rows drawing the SAME item from the
	# SAME source warehouse to different target departments, which previously
	# re-ran the identical fetch once per row. Each caller gets its own fresh copy
	# via deepcopy below, since the loop mutates `batch.qty` in place per row.
	if batch_cache is None:
		batch_cache = {}
	row.batch_no = None
	total_qty = row.qty
	row_qty = row.qty
	existing_updated = False

	msl = self.get("main_slip") or self.get("to_main_slip")
	warehouse = row.get("s_warehouse") or self.get("source_warehouse")
	use_msl = (
		msl
		and frappe.db.get_value("Main Slip", msl, "raw_material_warehouse")
		== row.s_warehouse
	)
	if use_msl:
		main_slip = self.main_slip or self.to_main_slip
		cache_key = ("msl", row.item_code, main_slip, row.s_warehouse)
	else:
		main_slip = None
		cache_key = (
			"auto",
			self.get("posting_time"),
			self.get("posting_date"),
			row.item_code,
			warehouse,
		)

	if cache_key not in batch_cache:
		if use_msl:
			batch_cache[cache_key] = get_batch_data_from_msl(
				row.item_code, main_slip, row.s_warehouse
			)
		else:
			# NOTE: do not pass "qty" here. get_auto_batch_nos truncates the result to
			# just enough FIFO batches to cover the requested qty, which can drop the
			# Customer Goods / customer-specific batches before the inventory-type and
			# customer filtering below runs. Fetch all available batches and let the
			# loop pick the matching ones up to total_qty.
			batch_cache[cache_key] = get_auto_batch_nos(
				frappe._dict(
					{
						"posting_time": self.get("posting_time"),
						"posting_date": self.get("posting_date"),
						"item_code": row.item_code,
						"warehouse": warehouse,
					}
				)
			)
	batch_data = copy.deepcopy(batch_cache[cache_key])

	customer_item_data = frappe._dict({})
	manufacturer_data = frappe._dict({})
	# The row's own order, else the entry's. A Material Transfer (WORK ORDER) built by hand names
	# the order only in the header -- MAT-STE-57381 did -- and reading the row alone left this
	# allocator blind to a customer-diamond order, so FIFO took the company's stones. Conversion
	# documents have no ``manufacturing_order``; ``self.get`` answers None for them. A row naming
	# several orders is judged by the first; the guard checks every one of them at validate.
	pmo = (
		str(
			row.get("custom_parent_manufacturing_order")
			or self.get("manufacturing_order")
			or ""
		)
		.split(",")[0]
		.strip()
	)
	if pmo:
		# ``or frappe._dict()``: a row pointing at a deleted order returns None here, and every
		# ``.get`` below would then raise AttributeError instead of simply finding no flags.
		customer_item_data = frappe.db.get_value(
			"Parent Manufacturing Order",
			pmo,
			[
				"is_customer_gold",
				"is_customer_diamond",
				"is_customer_gemstone",
				"is_customer_material",
				"customer",
				"manufacturer",
			],
			as_dict=1,
		) or frappe._dict()
	if not manufacturer_data.get(customer_item_data.get("manufacturer")):
		manufacturer_data[customer_item_data.get("manufacturer")] = frappe.db.get_value(
			"Manufacturer",
			customer_item_data.get("manufacturer"),
			"custom_allow_regular_goods_instead_of_customer_goods",
		)

	allow_customer_goods = manufacturer_data.get(customer_item_data.get("manufacturer"))

	variant_of = row_variant_of(row, item_map)
	# Diamonds and gemstones on an order placed on the customer's own stones take ONLY that
	# customer's batches: the manufacturer's allowance never lets company stock stand in for
	# them, and running short is refused below rather than quietly topped up.
	strict = pmo_requires_customer_goods(customer_item_data, variant_of)
	if strict:
		allow_customer_goods = 0
	# ...and the same two on an order that does NOT say the customer supplied them take ONLY
	# company stock. KGJPL-MR-MF-26-34479's reserve drew GJCU0009's diamonds for
	# PMO-KGJPL-EA10929-001-0006, whose order says "No", because FIFO took the first batch it saw.
	company_only = pmo_requires_company_stock(customer_item_data, variant_of)

	if pmo_expects_customer_goods(customer_item_data, variant_of):
		row.inventory_type = "Customer Goods"
		row.customer = customer_item_data.customer
	elif company_only:
		# A lane the row arrived with (a Material Request row stamped Customer Goods) is not the
		# order's answer; the order's is.
		row.inventory_type = "Regular Stock"
		row.customer = None

	# Hybrid orders: a finding listed in Subcontracting Settings may come only from the order's
	# own customer batch -- Customer Goods received for the PMO's Ref Customer (hybrid_findings).
	# Set before the expected lane is captured below, so neither the "regular instead of
	# customer goods" fallback nor the Regular Stock branch can ever take such a row.
	hybrid = hybrid_requirement(self, row)
	if hybrid:
		row.inventory_type = "Customer Goods"
		row.customer = hybrid.customer

	if not row.inventory_type:
		row.inventory_type = "Regular Stock"
	# Prefetch the Batch fields read per batch in the loop below (custom_inventory_type
	# is read up to 3x per batch across the branches, custom_customer once) in a single
	# query instead of 2-3 get_value calls per batch. Same None/missing semantics as
	# frappe.db.get_value via ``(batch_info.get(name) or {}).get(field)``.
	# Only the customer-goods branches and the only_regular_stock_allowed gate read it,
	# so the ordinary Regular Stock path keeps issuing zero Batch queries; every reader
	# is behind an ``and`` that short-circuits before touching an empty map.
	batch_info = {}
	if hybrid:
		# The same map plus the Ref Customer that batch_is_eligible reads.
		batch_info = get_batch_ownership_map([batch.batch_no for batch in batch_data])
	elif (
		row.inventory_type in ["Customer Goods", "Customer Stock"]
		or self.flags.only_regular_stock_allowed
		or company_only
	):
		batch_info = bulk_map(
			"Batch",
			[batch.batch_no for batch in batch_data],
			["custom_inventory_type", "custom_customer"],
		)
	# Same reason as batch_info above: is_customer_sample_batch used frappe.get_cached_value
	# per candidate batch, which loads the whole Batch doc on any cache miss -- e.g. batches
	# minted in this same request, or a cold Redis. Profiled at ~20-80ms per call across the
	# FIFO scan of a single row. get_sample_batches does the equivalent check for every
	# candidate batch in one query, gated behind the same allowed-type condition so the
	# ordinary Customer Goods flows (where sample stock is legitimately used) still skip it.
	sample_batches = (
		get_sample_batches([batch.batch_no for batch in batch_data])
		if self.get("stock_entry_type")
		and self.get("stock_entry_type") not in SAMPLE_ALLOWED_SE_TYPES
		else set()
	)
	# Opt-in, and set only by DiamondConversion.before_validate for the "Sieve Size to Sieve
	# Size" conversion type, which may not re-consume its own output. Every other caller leaves
	# the flag unset (self.flags is a _dict, so it reads None) and issues zero extra queries.
	# update_batch_details calls this function once per source row, so the resolved map is
	# memoised on the doc for the duration of the save rather than re-queried per row.
	barred_batches = {}
	if self.flags.exclude_diamond_conversion_target_batches:
		if self.flags.diamond_conversion_batch_cache is None:
			self.flags.diamond_conversion_batch_cache = {}
		cache = self.flags.diamond_conversion_batch_cache
		unresolved = [
			batch.batch_no for batch in batch_data if batch.batch_no not in cache
		]
		if unresolved:
			resolved = get_diamond_conversion_target_batches(unresolved)
			cache.update({batch_no: resolved.get(batch_no) for batch_no in unresolved})
		barred_batches = {
			batch.batch_no for batch in batch_data if cache.get(batch.batch_no)
		}
	# F5: the lane a row is booked in comes from the batch it actually draws, not from what the
	# PMO expected. KLHGX62F1119's company diamond (a Regular Stock batch, no customer) was
	# allowed in place of the customer's diamond and travelled as Customer Goods with a NULL
	# customer through MAT-STE-18637/38/39. ``expected`` is what the PMO asked for; each
	# allocation below stamps the lane of the batch it took.
	expected_lane = (row.inventory_type, row.get("customer"))
	# The branch tests below read these, never the row: an allocation relabels the row, and
	# the next batch must still be matched against what the PMO asked for.
	expected_type, expected_customer = expected_lane

	for batch in batch_data:
		# reduce this batch's availability by what earlier rows already took
		batch_key = (warehouse, batch.batch_no)
		batch.qty = flt(batch.qty - consumed.get(batch_key, 0), 4)
		if batch.qty <= 0:
			continue
		# Never auto-allocate Customer Sample Goods stock into a Work-Order use /
		# issue-to-floor / manufacturing consumption row. Samples stay FIFO-eligible only
		# for the Customer Goods movements (Received / Issue / Transfer). The
		# stock_entry_type truthiness gate leaves non-Stock-Entry callers (e.g. Metal /
		# Diamond Conversion, whose doc has no stock_entry_type) untouched. The loud block
		# for a hand-typed sample batch is validate_sample_goods_not_consumed.
		if batch.batch_no in sample_batches:
			continue
		# Diamond Conversion output is not eligible input for another Diamond Conversion.
		if batch.batch_no in barred_batches:
			continue
		# A listed Hybrid finding draws only its customer's batch (see ``hybrid`` above).
		if hybrid and not batch_is_eligible(batch_info.get(batch.batch_no), hybrid):
			continue
		if (
			expected_type in ["Customer Goods", "Customer Stock"]
			and (batch_info.get(batch.batch_no) or {}).get("custom_inventory_type")
			== expected_type
			and (batch_info.get(batch.batch_no) or {}).get("custom_customer")
			== expected_customer
		):
			if total_qty > 0 and batch.qty > 0:
				lane = expected_lane
				if not existing_updated:
					# Single UPDATE instead of 2-3 sequential ones: db_set accepts a
					# {field: value} dict and writes it in one query. transfer_qty must
					# still equal the qty just computed (previously read back via
					# row.qty after the first db_set landed it) -- capture it in
					# new_qty rather than relying on that in-memory round trip.
					new_qty = min(total_qty, batch.qty)
					update_values = {"qty": new_qty}
					_stamp_lane(row, lane)
					row.db_set("qty", min(total_qty, batch.qty))
					if self.get("date"):
						update_values["batch"] = batch.batch_no
					else:
						update_values["transfer_qty"] = new_qty
						update_values["batch_no"] = batch.batch_no
					row.db_set(update_values)
					consumed[batch_key] = consumed.get(batch_key, 0) + min(
						total_qty, batch.qty
					)
					total_qty -= batch.qty
					existing_updated = True
					rows_to_append.append(row.as_dict())
				else:
					temp_row = copy.deepcopy(row.as_dict())
					temp_row["name"] = None
					temp_row["idx"] = None
					temp_row["batch_no"] = batch.batch_no
					temp_row["transfer_qty"] = 0
					temp_row["qty"] = flt(min(total_qty, batch.qty), 4)
					temp_row["inventory_type"], temp_row["customer"] = lane
					rows_to_append.append(temp_row)
					consumed[batch_key] = consumed.get(batch_key, 0) + min(
						total_qty, batch.qty
					)
					total_qty -= batch.qty

		elif (
			expected_type in ["Customer Goods", "Customer Stock"]
			and (batch_info.get(batch.batch_no) or {}).get("custom_inventory_type")
			!= expected_type
			and allow_customer_goods == 1
		):
			if total_qty > 0 and batch.qty > 0:
				info = batch_info.get(batch.batch_no) or {}
				lane = normalize_ownership(
					info.get("custom_inventory_type"),
					info.get("custom_customer"),
					batch_no=batch.batch_no,
					item_code=row.item_code,
				)
				if not existing_updated:
					# Single UPDATE instead of 2-3 sequential ones: db_set accepts a
					# {field: value} dict and writes it in one query. transfer_qty must
					# still equal the qty just computed (previously read back via
					# row.qty after the first db_set landed it) -- capture it in
					# new_qty rather than relying on that in-memory round trip.
					new_qty = min(total_qty, batch.qty)
					update_values = {"qty": new_qty}
					_stamp_lane(row, lane)
					row.db_set("qty", min(total_qty, batch.qty))
					if self.get("date"):
						update_values["batch"] = batch.batch_no
					else:
						update_values["transfer_qty"] = new_qty
						update_values["batch_no"] = batch.batch_no
					row.db_set(update_values)
					consumed[batch_key] = consumed.get(batch_key, 0) + min(
						total_qty, batch.qty
					)
					total_qty -= batch.qty
					existing_updated = True
					rows_to_append.append(row.as_dict())
				else:
					temp_row = copy.deepcopy(row.as_dict())
					temp_row["name"] = None
					temp_row["idx"] = None
					temp_row["batch_no"] = batch.batch_no
					temp_row["transfer_qty"] = 0
					temp_row["qty"] = flt(min(total_qty, batch.qty), 4)
					temp_row["inventory_type"], temp_row["customer"] = lane
					rows_to_append.append(temp_row)
					consumed[batch_key] = consumed.get(batch_key, 0) + min(
						total_qty, batch.qty
					)
					total_qty -= batch.qty

		elif expected_type not in ["Customer Goods", "Customer Stock"]:
			if (self.flags.only_regular_stock_allowed or company_only) and (
				batch_info.get(batch.batch_no) or {}
			).get("custom_inventory_type") in ["Customer Goods", "Customer Stock"]:
				continue

			if total_qty > 0 and batch.qty > 0:
				if not existing_updated:
					# Single UPDATE instead of 2-3 sequential ones: db_set accepts a
					# {field: value} dict and writes it in one query. transfer_qty must
					# still equal the qty just computed (previously read back via
					# row.qty after the first db_set landed it) -- capture it in
					# new_qty rather than relying on that in-memory round trip.
					new_qty = min(total_qty, batch.qty)
					update_values = {"qty": new_qty}
					if self.get("date"):
						update_values["batch"] = batch.batch_no
					else:
						update_values["transfer_qty"] = new_qty
						update_values["batch_no"] = batch.batch_no
					row.db_set(update_values)
					consumed[batch_key] = consumed.get(batch_key, 0) + min(
						total_qty, batch.qty
					)
					total_qty -= batch.qty
					existing_updated = True
					rows_to_append.append(row.as_dict())
				else:
					temp_row = copy.deepcopy(row.as_dict())
					temp_row["name"] = None
					temp_row["idx"] = None
					temp_row["batch_no"] = batch.batch_no
					temp_row["transfer_qty"] = 0
					temp_row["qty"] = flt(min(total_qty, batch.qty), 4)
					rows_to_append.append(temp_row)
					consumed[batch_key] = consumed.get(batch_key, 0) + min(
						total_qty, batch.qty
					)
					total_qty -= batch.qty

	if strict and round(total_qty, 3) > 0:
		frappe.throw(
			_(
				"Row #{0} ({1}): {2} is a customer-supplied order, so only {3}'s Customer Goods "
				"may be used. Only {4} of {5} is available in {6}. Receive the customer's "
				"material first."
			).format(
				row.get("idx"),
				row.item_code,
				frappe.bold(pmo),
				frappe.bold(expected_customer or _("the customer")),
				flt(flt(row_qty) - total_qty, 3),
				flt(row_qty, 3),
				frappe.bold(warehouse),
			),
			title=_("Customer Goods Not Available"),
		)

	if company_only and round(total_qty, 3) > 0:
		frappe.throw(
			_(
				"Row #{0} ({1}): {2} does not say the customer supplied this material, so only "
				"company Regular Stock may be used -- not a customer's goods. Only {3} of {4} is "
				"available in {5}. If the customer did supply it, tick it on the order; otherwise "
				"receive company stock first."
			).format(
				row.get("idx"),
				row.item_code,
				frappe.bold(pmo),
				flt(flt(row_qty) - total_qty, 3),
				flt(row_qty, 3),
				frappe.bold(warehouse),
			),
			title=_("Company Stock Not Available"),
		)

	if round(total_qty, 3) > 0:
		message = _("For <b>{0}</b> {1} is missing in <b>{2}</b>").format(
			row.item_code, flt(total_qty, 2), warehouse
		)
		if row.get("manufacturing_operation"):
			message += _("<br><b>Ref : {0}</b>").format(row.manufacturing_operation)
		if hybrid:
			message += "<br>" + shortfall_hint(hybrid)
		if self.flags.throw_batch_error:
			frappe.throw(message)
			self.flags.throw_batch_error = False
		else:
			frappe.msgprint(message)

	return rows_to_append


def _stamp_lane(row, lane):
	"""Book ``row`` in ``lane`` -- ``(inventory_type, customer)`` -- if it is not already."""
	inventory_type, customer = lane
	if row.inventory_type != inventory_type:
		row.db_set("inventory_type", inventory_type)
	if row.get("customer") != customer:
		row.db_set("customer", customer)


def get_batch_data_from_msl(item_code, main_slip, warehouse):
	batch_data = []
	msl_doc = frappe.get_doc("Main Slip", main_slip)

	avl_batch = get_batch_qty(warehouse=warehouse, item_code=item_code)
	avl_batch = [system_batch.get("batch_no") for system_batch in avl_batch]
	if warehouse != msl_doc.raw_material_warehouse:
		frappe.msgprint(
			_("Please select batch manually for receving goods in Main Slip")
		)
		return batch_data

	for row in msl_doc.batch_details:
		if avl_batch and row.batch_no in avl_batch:
			batch_row = frappe._dict()
			batch_row.update(
				{"batch_no": row.batch_no, "qty": row.qty - row.consume_qty}
			)
			batch_data.append(batch_row)

	return batch_data


def create_repack_for_subcontracting(self, subcontractor, main_slip=None):
	if not subcontractor and main_slip:
		subcontractor = frappe.db.get_value("Main Slip", main_slip, "subcontractor")

	raw_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"company": self.company,
			"subcontractor": subcontractor,
			"warehouse_type": "Raw Material",
		},
	)
	mfg_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"company": self.company,
			"subcontractor": subcontractor,
			"warehouse_type": "Manufacturing",
		},
	)
	repack_raws = []
	receive = False
	for row in self.items:
		temp_raw = copy.deepcopy(row.as_dict())
		if row.t_warehouse == raw_warehouse:
			receive = True
			temp_raw["name"] = None
			temp_raw["idx"] = None
			repack_raws.append(temp_raw)
		elif row.s_warehouse == raw_warehouse and row.t_warehouse == mfg_warehouse:
			temp_raw["name"] = None
			temp_raw["idx"] = None
			repack_raws.append(temp_raw)

	if repack_raws:
		create_subcontracting_doc(
			self, subcontractor, self.department, repack_raws, main_slip, receive
		)


def validate_gross_weight_for_unpack(self):
	if self.stock_entry_type == "Repair Unpack":
		source_gr_wt = 0
		receive_gr_wt = 0
		for row in self.items:
			if row.s_warehouse:
				source_gr_wt += row.get("gross_weight") or 0
			elif row.t_warehouse:
				receive_gr_wt += row.get("gross_weight") or 0

		if flt(receive_gr_wt, 3) != flt(source_gr_wt, 3):
			frappe.throw(_("Gross weight does not match for source and target items"))


def validation_for_stock_entry_submission(self):
	for item in self.items:
		stock_reco = frappe.get_doc(
			"Stock Reconciliation", {"set_warehouse": item.s_warehouse}
		)
		if stock_reco.docstatus != 1:
			frappe.throw(
				_(
					"Please complete the Stock Reconciliation {0}  to Submit the Stock Entry".format_(
						stock_reco.name
					)
				)
			)


def set_employee(self):
	if self.stock_entry_type != "Material Transfer (WORK ORDER)":
		return

	if mop_details := frappe.db.get_value(
		"Manufacturing Operation",
		self.manufacturing_operation,
		["status", "employee"],
		as_dict=1,
	):
		if mop_details.status == "WIP":
			self.to_employee = mop_details.employee


def set_gross_wt(self):
	for row in self.items:
		if row.serial_no:
			gross_weight = frappe.db.get_value(
				"Serial No", row.serial_no, "custom_gross_wt"
			)
			# F22: a finished piece's serial is weighed after this entry is built, so its
			# serial reads nothing yet. Blanking the row then erased the weight the builder
			# had set; keep it unless the serial actually carries one.
			if gross_weight:
				row.gross_weight = gross_weight


def set_fg_bom_weights(self):
	"""Mirror each FG serial's own as-built BOM weights onto its row.

	The Material Request -> Stock Entry mapper already carries these across for free --
	the fieldnames are identical on Material Request Item and Stock Entry Detail, so
	``frappe.model.mapper`` copies them with no mapping code. This re-stamps them on
	every save anyway, for two reasons: ``update_batches`` rebuilds ``self.items``
	wholesale on each draft save, and a Stock Entry can reach a serial by routes that
	never touched a Material Request at all.

	Distinct from ``set_gross_wt`` above, which fills the separate, older
	``Stock Entry Detail.gross_weight`` from ``Serial No.custom_gross_wt``. That field
	and this block are independent; neither writes the other's column.

	Rows this cannot resolve are left untouched rather than blanked -- see
	``apply_bom_weights``.
	"""
	apply_bom_weights(self.items)


def _row_serials(serial_no):
	"""Serials of a Stock Entry Detail row, in row order.

	``splitlines()`` rather than ``split("\\n")`` so a pasted CRLF list and a
	trailing newline both behave. Interior blanks are kept: position is meaningful.
	"""
	return [s.strip() for s in (serial_no or "").splitlines()]


def _join_tags(serials, tag_map):
	"""Newline-joined Jwelex tags, aligned line-for-line with ``serials``.

	A serial with no tag contributes a blank line, so line N of the result is the
	tag of serial N and the two fields can be read side by side. Returns None when
	no serial on the row has a tag, so the field reads empty rather than a run of
	blank lines.
	"""
	tags = [((tag_map.get(s) or {}).get("custom_jwelex_tag_no") or "") for s in serials]
	return "\n".join(tags) if any(tags) else None


def set_jwelex_tag_no(self):
	"""Mirror ``Serial No.custom_jwelex_tag_no`` onto each row, one tag per serial.

	``Stock Entry Detail.serial_no`` is a newline-separated Text field, not a Link,
	so Frappe's ``fetch_from`` cannot resolve it -- the value has to be stamped here.
	A row carries as many serials as its qty, so the tag field mirrors that list.

	One ``bulk_map`` for the whole document: a 42-row entry with 4 serials a row
	would otherwise cost 168 round trips.
	"""
	rows = [(row, _row_serials(row.serial_no)) for row in self.items if row.serial_no]
	tag_map = bulk_map(
		"Serial No",
		[s for _, serials in rows for s in serials],
		["custom_jwelex_tag_no"],
	)
	for row, serials in rows:
		row.custom_jwelex_tag_no = _join_tags(serials, tag_map)


@frappe.whitelist()
def get_jwelex_tag_no(serial_no):
	"""Resolve a row's Jwelex tags for the Stock Entry Detail client handler.

	Shares ``_row_serials`` / ``_join_tags`` with ``set_jwelex_tag_no`` so the form
	shows exactly what the server will store.
	"""
	serials = _row_serials(serial_no)
	return _join_tags(serials, bulk_map("Serial No", serials, ["custom_jwelex_tag_no"]))


def validate_warehouse(self):
	if self.stock_entry_type != "Material Transfer (WORK ORDER)":
		return
	if self.from_warehouse and self.to_warehouse:
		if self.from_warehouse == self.to_warehouse:
			frappe.throw(
				_("The source warehouse and the target warehouse cannot be the same.")
			)

	for row in self.items:
		if row.s_warehouse == row.t_warehouse:
			frappe.throw(
				_("The source warehouse and the target warehouse cannot be the same.")
			)
