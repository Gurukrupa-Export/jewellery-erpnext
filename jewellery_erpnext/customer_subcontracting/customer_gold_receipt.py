# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Server-side eligibility rules for a Customer Gold receipt.

Everything here is gated twice: the ``enable_customer_gold_flow`` master switch, and the
Stock Entry Type configured on ``Subcontracting Settings``. With the switch off, or on any
other Stock Entry Type, these functions return immediately and existing behaviour is
untouched.

The rules are deliberately split across two lifecycle points, because ``batch_no`` does not
exist during validation:

``before_validate``  customer, inventory type, item and quantity -- everything the caller
                    supplies directly.
``before_submit``    batch ownership -- runs AFTER ``batch_rename.create_child_batches`` so
                    that batches minted by ``create_parent_batches`` are visible. Placing a
                    batch check any earlier would reject every legitimate receipt, since the
                    rows carry no batch until then.

``before_validate`` additionally freezes the Customer Gold rate snapshot -- see
``set_customer_gold_rate_snapshot`` -- so the receipt permanently records which rate was
resolved for its posting date.

This module does NOT set ``basic_rate``, ``valuation_rate`` or any stock/GL value. Nominal
valuation and the liability posting are separate, later work.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from jewellery_erpnext.customer_subcontracting.customer_gold_rate import (
	resolve_customer_gold_rate_for_date,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
	get_allowed_customer_gold_items,
	get_customer_gold_company_settings,
	get_customer_gold_settings,
	get_customer_gold_valuation_policy,
	is_customer_gold_enabled,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	DEFAULT_INVENTORY_TYPE,
)

CUSTOMER_GOODS = "Customer Goods"

#: The Item Attribute carrying metal purity. Hardcoded the same way ``metal_utils.py:21`` and
#: ``sub_utils/repack.py:377`` hardcode it -- there is no shared constant to import, and the
#: test fixtures' copy is test code.
METAL_PURITY_ATTRIBUTE = "Metal Purity"

#: Snapshot fields written by ``set_customer_gold_rate_snapshot``. Provisioned by
#: ``patches.add_customer_gold_rate_snapshot_fields``.
RATE_SNAPSHOT_FIELDS = (
	"custom_gold_rate_reference",
	"custom_gold_rate_date",
	"custom_gold_rate_source",
	"custom_gold_rate_field",
	"custom_gold_rate_raw",
	"custom_gold_rate_unit",
	"custom_gold_rate_per_gram",
)


def _receipt_settings(doc):
	"""Return the settings when ``doc`` is a Customer Gold receipt, else ``None``.

	Fetched once per document -- never per row.
	"""
	if doc.doctype != "Stock Entry":
		return None
	if not is_customer_gold_enabled():
		return None

	settings = get_customer_gold_settings()
	configured_type = settings.get("customer_goods_stock_entry_type")
	if not configured_type or doc.get("stock_entry_type") != configured_type:
		return None

	return settings


def validate_customer_gold_receipt(doc, method=None):
	"""Customer, inventory type, item and quantity rules for a Customer Gold receipt."""
	settings = _receipt_settings(doc)
	if not settings:
		return

	_validate_receipt_purpose(settings)
	customer = _validate_customer(doc)
	_validate_rows(doc, settings, customer)
	rate = set_customer_gold_rate_snapshot(doc, settings)
	apply_valuation_policy(doc, rate)


def _validate_receipt_purpose(settings):
	"""Re-check the configured type at runtime, in case the master was edited later."""
	purpose = frappe.db.get_value(
		"Stock Entry Type", settings.customer_goods_stock_entry_type, "purpose"
	)
	if purpose != "Material Receipt":
		frappe.throw(
			_(
				"Stock Entry Type {0} is configured for Customer Gold receipts but its purpose is {1}, not {2}."
			).format(
				frappe.bold(settings.customer_goods_stock_entry_type),
				frappe.bold(purpose or _("not set")),
				frappe.bold("Material Receipt"),
			),
			title=_("Customer Gold Configuration Invalid"),
		)


def _validate_customer(doc):
	"""``Stock Entry._customer`` is the authoritative header customer for this flow."""
	customer = doc.get("_customer")
	if not customer:
		frappe.throw(
			_("Customer is mandatory for a Customer Gold receipt."),
			title=_("Customer Missing"),
		)

	for row in doc.get("items") or []:
		if not row.get("customer"):
			# Only the browser fills this today, so an API-created receipt would arrive
			# blank. Backfill from the authoritative header rather than reject.
			row.customer = customer
		elif row.customer != customer:
			# The batch is named when there is one, because in practice this message is what
			# a user sees when they reuse ANOTHER customer's batch -- not when they type a
			# mismatched customer. ``CustomStockEntry.update_batches`` runs earlier in the
			# before_validate chain and overwrites ``row.customer`` from the batch, so by the
			# time this check runs the row already carries the batch owner's name and the
			# dedicated "Batch ... belongs to Customer ..." throw further down is unreachable.
			#
			# The receipt is correctly blocked either way -- this is not a hole -- but without
			# the batch in the message the error text says only that two customer names differ
			# and gives no clue which batch caused it. Fixing the ordering instead would mean
			# reordering that before_validate chain, which is load-bearing for unrelated flows.
			suffix = (
				_(" Batch {0} is owned by {1}.").format(
					frappe.bold(row.batch_no), frappe.bold(row.customer)
				)
				if row.get("batch_no")
				else ""
			)
			frappe.throw(
				_(
					"Row #{0}: Customer {1} does not match the receipt Customer {2}."
				).format(row.idx, frappe.bold(row.customer), frappe.bold(customer))
				+ suffix,
				title=_("Customer Mismatch"),
			)

	return customer


def _validate_rows(doc, settings, customer):
	allowed_items = get_allowed_customer_gold_items(settings)

	for row in doc.get("items") or []:
		# ``Regular Stock`` here is NOT a caller's choice -- it is the framework's own
		# blanket default. ``doc_events.stock_entry.before_validate`` runs FIRST in the
		# before_validate chain (hooks.py) and ends with an unconditional
		# ``if not row.inventory_type: row.inventory_type = "Regular Stock"``, so by the
		# time this validator runs (last in that chain) every row already carries it.
		# Treating it as "supplied" made every server-created Customer Gold receipt throw
		# -- the API path the server-side tagging below exists to protect. Only the browser
		# got through, because stock_entry.js sets Customer Goods before the save is sent.
		# So blank and the default are both overridable; a DELIBERATE other ownership class
		# (Customer Stock, Pure Metal) still hard-fails.
		supplied = row.get("inventory_type")
		if supplied and supplied not in (CUSTOMER_GOODS, DEFAULT_INVENTORY_TYPE):
			frappe.throw(
				_(
					"Row #{0}: Customer Gold receipt requires Inventory Type {1}, but {2} was supplied."
				).format(
					row.idx,
					frappe.bold(CUSTOMER_GOODS),
					frappe.bold(supplied),
				),
				title=_("Invalid Inventory Type"),
			)
		# Set server-side so an API-created receipt cannot bypass ownership tagging;
		# today only the client sets this.
		row.inventory_type = CUSTOMER_GOODS

		if row.item_code not in allowed_items:
			# Customers do not all hand over the same purity -- 99.5 arrives alongside 99.9 --
			# so this is a membership test over the configured list, not equality with one
			# item. A site that configures no additional purities gets a single-element list
			# and therefore the exact behaviour this check had before.
			frappe.throw(
				_(
					"Row #{0}: Item {1} is not configured for Customer Gold receipts. "
					"Accepted items: {2}."
				).format(
					row.idx,
					frappe.bold(row.item_code),
					frappe.bold(", ".join(allowed_items) or _("none")),
				),
				title=_("Invalid Item"),
			)

		if flt(row.qty) <= 0:
			frappe.throw(
				_(
					"Row #{0}: Quantity must be greater than zero for a Customer Gold receipt."
				).format(row.idx),
				title=_("Invalid Quantity"),
			)


#: Batch fields read by ``validate_customer_gold_batches``. ``custom_company`` is optional
#: because it is NOT shipped by ``custom_fields/batch.json`` -- it reaches a site by some
#: other route, so a freshly installed one does not have the column at all and naming it
#: unconditionally raises ``Unknown column 'custom_company' in 'SELECT'``.
_REQUIRED_BATCH_FIELDS = (
	"item",
	"custom_customer",
	"custom_inventory_type",
	"disabled",
	"expiry_date",
)
_OPTIONAL_BATCH_FIELDS = ("custom_company",)


def _batch_fields():
	"""The Batch fields to read, skipping optional ones this site does not have.

	Uses ``frappe.db.has_column`` rather than ``frappe.get_meta``. Both answer the
	question, but ``get_meta`` resolves through ``frappe.db.get_value`` -- and the suites
	covering this module patch that accessor wholesale, so a meta lookup here would be
	answered by a test stub instead of the database. That is the same contamination that
	produced the original CI failure in this area; ``has_column`` reads the table columns
	directly and cannot be intercepted by it.
	"""
	return list(_REQUIRED_BATCH_FIELDS) + [
		field
		for field in _OPTIONAL_BATCH_FIELDS
		if frappe.db.has_column("Batch", field)
	]


def _metal_purity(item_code):
	"""The item's Metal Purity, read from the attribute VALUE, or ``None``.

	WHY NOT ``metal_utils.get_purity_percentage``
	----------------------------------------------
	That helper joins to ``Attribute Value.purity_percentage``, and that column is wrong on this
	bench: the row named ``99.9`` carries **100.0**, and ``91.75`` carries **0.0** across 54
	items (measured; see D04 in docs-customer-gold/DECISIONS.md). Reading it here would price
	99.5 gold against a 100.0 reference instead of 99.9 -- a 0.1% error on every receipt, in the
	customer's favour, for ever.

	The attribute VALUE is the same fact without the mis-keyed column: ``Attribute Value``
	autonames ``field:attribute_value``, so the row named ``99.9`` IS the purity. ``repack.py``
	and ``batch_rename.py`` already read it this way.

	This does NOT repair the master, deliberately. That row is load-bearing elsewhere -- it
	currently masks a fine-versus-reference basis mix-up in ``sub_utils/repack.py:244-256`` that
	produces physical quantities on submitted Stock Entries, so correcting it in isolation would
	turn a rounding error into wrong weights. Sequencing that is a business decision. Reading the
	right field here is not.

	Returns ``None`` when the item has no Metal Purity attribute or it does not parse. The
	caller refuses rather than defaulting: ``repack.get_purity`` falls back to 99.9 and
	``batch_rename.get_purity`` to 100 -- two different guesses for the same unknown -- and a
	guess that sets a customer's booked value is exactly the wrong place to have one.
	"""
	rows = frappe.get_all(
		"Item Variant Attribute",
		filters={"parent": item_code, "attribute": METAL_PURITY_ATTRIBUTE},
		fields=["attribute_value"],
		limit=1,
	)
	if not rows:
		return None

	purity = flt(rows[0].attribute_value)
	return purity if purity > 0 else None


def _rate_for_item(per_gram, item_code, reference_item):
	"""``per_gram`` restated for this row's purity.

	The configured Gold Rate is quoted per gram of the Customer 24KT Item (decision D02). When a
	customer hands over a different purity, the same rupees-per-gram would misprice it: gold is
	bought and sold on fine content, so 99.5 metal is worth ``99.5 / 99.9`` of 99.9 metal. On a
	10 g receipt at Rs.7,164.83 that difference is Rs.28.69 -- small per receipt, systematic
	across every one of them, and always in the same direction.

	The reference item returns ``per_gram`` unchanged, so a site receiving only the configured
	item books exactly what it booked before this existed.
	"""
	per_gram = flt(per_gram)
	if not per_gram or not item_code or item_code == reference_item:
		return per_gram

	row_purity = _metal_purity(item_code)
	reference_purity = _metal_purity(reference_item)
	if not row_purity or not reference_purity:
		frappe.throw(
			_(
				"Cannot book {0} at the Customer Gold rate: the Metal Purity of {1} could not "
				"be resolved, so its rate cannot be restated from the {2} quote. Set the Metal "
				"Purity attribute on both items."
			).format(
				frappe.bold(item_code),
				frappe.bold(item_code if not row_purity else reference_item),
				frappe.bold(reference_item),
			),
			title=_("Customer Gold Purity Unknown"),
		)

	return per_gram * row_purity / reference_purity


def apply_valuation_policy(doc, rate):
	"""Stamp the row valuation fields required by the configured policy.

	Runs AFTER ``set_customer_gold_rate_snapshot`` because the nominal branch needs the
	resolved per-gram rate, and after ``_validate_rows`` because it needs the final
	ownership tagging.

	**Zero Value (the default, and every existing site).** Stamp
	``allow_zero_valuation_rate`` and leave ``basic_rate`` alone. This has to happen here,
	not in ``doc_events.stock_entry.allow_zero_valuation``: hooks.py runs that hook FIRST,
	and at that point the blanket default earlier in the same hook has stamped the row
	"Regular Stock", so allow_zero_valuation sees a non-customer row and leaves the flag at
	0. ``_validate_rows`` then flips ownership to Customer Goods -- leaving Customer Goods
	metal without the flag, which sends erpnext to the ``stock_entry.py:1661`` fallback and
	either throws or books COMPANY valuation onto customer-owned metal. The browser never
	showed this because ``stock_entry.js`` sets both fields client-side.

	**Nominal.** Stamp ``basic_rate`` from the frozen per-gram rate and
	``set_basic_rate_manually``, and deliberately do NOT stamp the allow-zero flag.
	``set_basic_rate_manually`` is what makes the entered rate survive: erpnext's loop at
	``stock_entry.py:1615-1619`` takes ``continue`` for such a row, computing ``basic_amount``
	and skipping everything after -- including the allow-zero wipe at ``:1629``, the ``:1661``
	valuation fallback, and ``get_args_for_incoming_rate``.

	So the two flags are not in fact in conflict (``set_basic_rate_manually`` short-circuits
	before the flag is ever read), but stamping both would be stamping one that can never be
	consulted. They are kept mutually exclusive so the row says what it means.

	This function decides NOTHING about accounting policy -- it applies whichever policy is
	configured. Whether nominal is approved at all is D01.
	"""
	policy = get_customer_gold_valuation_policy()

	if policy != VALUATION_NOMINAL:
		for row in doc.get("items") or []:
			row.allow_zero_valuation_rate = 1
			row.set_basic_rate_manually = 0
		return

	# Resolved once per document, and deliberately NOT inside the loop: it throws when the
	# company has no configured row, and that must fail the whole receipt rather than half of
	# its rows. This is the first production caller of this resolver.
	accounts = get_customer_gold_company_settings(doc.get("company"))
	per_gram = flt(rate.per_gram_rate) if rate else 0.0

	settings = get_customer_gold_settings()
	reference_item = settings.get("customer_24kt_item")

	for row in doc.get("items") or []:
		row.basic_rate = _rate_for_item(per_gram, row.item_code, reference_item)
		row.set_basic_rate_manually = 1
		row.allow_zero_valuation_rate = 0

		# THE CONTRA ACCOUNT. For a Stock Entry the credit leg is the row's
		# ``expense_account`` -- ``StockController.get_gl_entries`` reads it at
		# ``stock_controller.py:810``, and the ``target_warehouse`` branch above it can never
		# fire because Stock Entry Detail has no such field (it uses ``t_warehouse``).
		#
		# A Liability-root account is legitimate here; all three gates accept it:
		#   * ``check_expense_account`` exempts "Stock Entry" from its P&L requirement
		#     (``stock_controller.py:1080``);
		#   * ``validate_difference_account`` rejects only ``account_type == "Stock"``, and
		#     for an opening entry actually REQUIRES Asset/Liability -- core explicitly
		#     contemplates a liability account here (``stock_entry.py:917-932``);
		#   * ``GL Entry.validate`` has no root-type check at all.
		#
		# So the standard path credits the liability directly and NO reclassification JE is
		# needed -- the spec's S07 §7.1 "already credits the approved liability" branch.
		# Adding a JE on top would be the duplicate stock debit that section forbids.
		row.expense_account = accounts.liability_account


def validate_customer_gold_batches(doc, method=None):
	"""Batch ownership rules, run after the batch creators have minted batches."""
	settings = _receipt_settings(doc)
	if not settings:
		return

	customer = doc.get("_customer")

	for row in doc.get("items") or []:
		if not row.get("batch_no"):
			frappe.throw(
				_(
					"Row #{0}: Customer gold must be batch tracked, but no batch could be determined for item {1}."
				).format(row.idx, frappe.bold(row.item_code)),
				title=_("Batch Missing"),
			)

		batch = frappe.db.get_value(
			"Batch", row.batch_no, _batch_fields(), as_dict=True
		)
		if not batch:
			frappe.throw(
				_("Row #{0}: Batch {1} does not exist.").format(
					row.idx, frappe.bold(row.batch_no)
				)
			)

		if batch.item and batch.item != row.item_code:
			# erpnext does catch this eventually -- but in
			# ``serial_and_batch_bundle``, built during ``on_submit``, i.e. after this
			# hook. Throwing here names the row and the receipt while the operator still
			# has the document in front of them.
			frappe.throw(
				_("Row #{0}: Batch {1} belongs to Item {2}, not {3}.").format(
					row.idx,
					frappe.bold(row.batch_no),
					frappe.bold(batch.item),
					frappe.bold(row.item_code),
				),
				title=_("Batch Item Mismatch"),
			)

		# Compared only when set, NEVER required, and only where the field exists at all.
		# ``create_parent_batches`` does not stamp ``custom_company`` and neither does
		# ``update_inventory_dimentions``, so demanding it would reject this flow's own
		# freshly minted batches.
		if (
			batch.get("custom_company")
			and doc.get("company")
			and batch.custom_company != doc.company
		):
			frappe.throw(
				_("Row #{0}: Batch {1} belongs to Company {2}, not {3}.").format(
					row.idx,
					frappe.bold(row.batch_no),
					frappe.bold(batch.custom_company),
					frappe.bold(doc.company),
				),
				title=_("Batch Company Mismatch"),
			)

		# A genuine gap, not belt-and-braces. erpnext's own ``StockEntry.validate_batch``
		# guards disabled and expired batches only for purposes ``Material Transfer for
		# Manufacture``, ``Manufacture``, ``Repack`` and ``Send to Subcontractor``.
		# ``Material Receipt`` is NOT in that list -- and Settings force the configured
		# Customer Gold type to be exactly ``Material Receipt``. So without this, a
		# disabled or expired batch is accepted by erpnext and by this validator alike.
		if batch.disabled:
			frappe.throw(
				_(
					"Row #{0}: Batch {1} is disabled and cannot receive customer gold."
				).format(row.idx, frappe.bold(row.batch_no)),
				title=_("Batch Disabled"),
			)

		if batch.expiry_date and doc.get("posting_date"):
			if getdate(batch.expiry_date) < getdate(doc.posting_date):
				frappe.throw(
					_(
						"Row #{0}: Batch {1} expired on {2}, before the posting date {3}."
					).format(
						row.idx,
						frappe.bold(row.batch_no),
						frappe.bold(batch.expiry_date),
						frappe.bold(doc.posting_date),
					),
					title=_("Batch Expired"),
				)

		# UNREACHABLE IN PRACTICE, and left in place deliberately. See the note at the
		# Customer Mismatch throw above: ``CustomStockEntry.update_batches`` copies the batch
		# owner onto ``row.customer`` earlier in the before_validate chain, so a row carrying
		# another customer's batch is rejected there first. This remains as the correct check
		# for any path that reaches this validator without that copy having happened -- a
		# server-side caller, or a future reordering. It must not be deleted on the assumption
		# that the earlier check will always fire.
		if batch.custom_customer and batch.custom_customer != customer:
			frappe.throw(
				_(
					"Row #{0}: Batch {1} belongs to Customer {2} and cannot be used for a receipt from Customer {3}."
				).format(
					row.idx,
					frappe.bold(row.batch_no),
					frappe.bold(batch.custom_customer),
					frappe.bold(customer),
				),
				title=_("Batch Belongs To Another Customer"),
			)

		if (
			batch.custom_inventory_type
			and batch.custom_inventory_type != CUSTOMER_GOODS
		):
			frappe.throw(
				_(
					"Row #{0}: Batch {1} is {2}, so it cannot hold customer gold."
				).format(
					row.idx,
					frappe.bold(row.batch_no),
					frappe.bold(batch.custom_inventory_type),
				),
				title=_("Invalid Batch Inventory Type"),
			)

		# The two guards above are of the shape `if <field> and <field> != expected`, so
		# a BLANK value short-circuits past both. That is the C06 hole, and it is not
		# theoretical: row_ownership documents that production holds Customer Goods
		# batches with a NULL customer.
		#
		# A batch minted by this flow always carries both fields -- create_parent_batches
		# sets custom_customer and custom_inventory_type together (batch_rename.py:76-77)
		# -- so the only way to arrive here with a blank is a PRE-EXISTING batch that was
		# never ownership-stamped, or was stamped Customer Goods without an owner. Neither
		# may be adopted into a customer's receipt by silence.
		#
		# Deliberately scoped to this receipt. It does NOT change
		# row_ownership.normalize_ownership, whose downgrade-to-Regular-Stock exists so
		# that loss and repack builders can consume malformed historical stock without
		# hard-failing a submit. Refusing to *originate* a customer obligation against an
		# unowned batch is a different question from refusing to *consume* one.
		if not batch.custom_inventory_type:
			frappe.throw(
				_(
					"Row #{0}: Batch {1} has no Inventory Type, so it cannot be accepted "
					"as customer gold. Set its Inventory Type to {2} and its Customer "
					"before submitting."
				).format(
					row.idx,
					frappe.bold(row.batch_no),
					frappe.bold(CUSTOMER_GOODS),
				),
				title=_("Batch Ownership Unresolved"),
			)

		if not batch.custom_customer:
			frappe.throw(
				_(
					"Row #{0}: Batch {1} is {2} but has no Customer, so the gold it holds "
					"has no owner. Set its Customer to {3} before submitting."
				).format(
					row.idx,
					frappe.bold(row.batch_no),
					frappe.bold(CUSTOMER_GOODS),
					frappe.bold(customer),
				),
				title=_("Batch Ownership Unresolved"),
			)


def set_customer_gold_rate_snapshot(doc, settings):
	"""Freeze the Customer Gold rate evidence on the receipt.

	Runs on every validate of a DRAFT and overwrites unconditionally. That is deliberate
	and serves two purposes at once:

	* the snapshot re-resolves whenever ``posting_date`` or the configured source / field /
	  unit changes, so a corrected posting date cannot leave yesterday's rate behind; and
	* a client-supplied value can never become financial truth -- whatever arrives over the
	  API is replaced by the server's own resolution.

	CAREFUL -- ``before_validate`` DOES run on the submit transition. ``run_before_save_methods``
	calls it for ``_action in ("save", "submit")`` (``frappe/model/document.py:1396-1397``) and
	``check_docstatus_transition`` sets ``_action = "submit"`` for 0 -> 1 (``document.py:1126``).
	So the snapshot is re-resolved ONE FINAL TIME at submit and is frozen only afterwards,
	because ordinary saves are then blocked. The practical consequence: if the Gold Rates row
	for this posting date is corrected between drafting and submitting, the submitted document
	carries the corrected rate. That matches the agreed policy (resolve on draft, re-resolve on
	change, freeze at submit) -- but it is a final re-resolve AT submit, not a stop-running-at-submit.

	Cancellation does not clear it: a cancelled receipt must still show the rate it originally
	used. An amendment carries no snapshot (the fields are ``no_copy``) and resolves afresh
	against its own posting date.

	Sets snapshot fields ONLY -- never ``basic_rate``, ``valuation_rate`` or an expense
	account. Consuming the frozen rate for stock valuation is later work, and that work
	must read ``custom_gold_rate_per_gram`` from here rather than resolving a rate again.
	"""
	rate = resolve_customer_gold_rate_for_date(doc.get("posting_date"), settings)

	doc.custom_gold_rate_reference = rate.gold_rate_reference
	doc.custom_gold_rate_date = rate.gold_rate_date
	doc.custom_gold_rate_source = rate.rate_source
	doc.custom_gold_rate_field = rate.rate_field
	doc.custom_gold_rate_raw = rate.raw_rate
	doc.custom_gold_rate_unit = rate.rate_unit
	doc.custom_gold_rate_per_gram = rate.per_gram_rate

	return rate
