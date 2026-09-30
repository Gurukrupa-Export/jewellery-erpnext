# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Per-batch gate on Employee IR loss booking for CUSTOMER-SUPPLIED FINDINGS.

A finding (clasp, jump ring, chain part) the customer supplied is expected back at exactly
its issued weight, so no process loss may be booked against it. This gate refuses such a row
on Employee IR -- automatic or manual -- when all three of these hold:

  * the item's ``Item.variant_of`` is ``"F"`` (a finding);
  * the row's Batch carries ``custom_inventory_type = "Customer Goods"``; and
  * that Batch was minted by a ``Customer Goods Received`` Stock Entry, or by a Purchase
    Receipt.

This is the third gate in this directory and the only one keyed on the BATCH rather than on
Department Operation configuration. ``finding_loss_gate`` gates one finding category off a
table; ``material_loss_gate`` gates a whole material class off a checkbox; this one gates
individual customer-owned stock wherever it turns up. All three are independent and purely
additive -- a row blocked by any of them is blocked, and none reads another's configuration.

**Fail-open contract**, identical to its two siblings. An operation holding no customer-goods
finding batch books loss exactly as before: ``get_blocked_finding_batches`` returns an empty
dict after at most one narrowing query, which makes every
``is_customer_goods_finding_blocked`` call short-circuit to False.

WHY THE BATCH, NEVER THE ROW'S ``inventory_type``
-------------------------------------------------
Both loss child tables carry an ``inventory_type`` column, which looks like the obvious thing
to test. It is EMPTY when this gate runs. ``loss_stock_entry._resolve_batch_inventory``
records why: ``validate_process_loss`` appends ``employee_loss_details`` with
``inventory_type`` commented out, and the only later writer is the fill-if-empty default in
Stock Entry ``before_validate``, which stamps every blank row ``"Regular Stock"``. A gate
keyed on the row field would match nothing here, then match the wrong thing later. The Batch
is the authoritative runtime marker -- the same conclusion ``sample_goods`` reaches for the
sample-goods block, and rule 1 of ``row_ownership`` ("the batch wins over the row").

WHY ORIGIN RESOLUTION HAS TWO PATHS
-----------------------------------
``custom_create_batch`` stamps ``reference_doctype`` / ``reference_name`` AND
``custom_voucher_detail_no`` on every batch it mints. ERPNext then NULLs the first two when
the minting voucher is cancelled (``serial_and_batch_bundle``) but leaves the detail row
alone -- 940 batches on the live bench have already lost ``reference_name`` while keeping it.
So the detail row is the PRIMARY path and ``reference_name`` the fallback, exactly as
``customization/utils/diamond_conversion_batches.py`` resolves the same question.
``custom_voucher_detail_no`` may name a ``Stock Entry Detail`` OR a ``Purchase Receipt Item``
(``Batch.update_inventory_dimentions`` walks every child table of the reference doctype for
the same reason), so both are resolved; it is a bare ``Data`` field, so a name found in
neither table simply drops out.

``frappe.db.get_all`` is used throughout rather than ``frappe.get_all``: it is a thin
delegating staticmethod frappe internals never call, so a test may patch it without hijacking
DocType meta loading or poisoning the translation cache.

**Automatic and manual loss are treated differently, on purpose** -- the same split
``material_loss_gate`` documents at length:

  * **Automatic** (``employee_loss_details``) -- ``EmployeeIR.book_metal_loss`` drops blocked
    batches from the proportional pool BEFORE ``total_qty`` is summed, so the survivors absorb
    the blocked row's share and the booked total still equals ``gross_wt - received_gross_wt``
    -- which is why the balance validators in ``validation_utils`` need no changes. This path
    NEVER throws, not even when it empties the pool. Company metal absorbs what the customer's
    finding may not, which is both the commercial answer and what ``tiered_allocate``'s
    "overflow anchors on the FIRST funded tier" guarantee already delivers.
  * **Manual** (``manually_book_loss_details``) --
    ``validate_customer_goods_finding_loss_rows`` throws, and does so ONLY from ``on_submit``.
    Saving a draft must always succeed.

**Do not add a validate-time call.** ``material_loss_gate`` records an earlier revision that
checked both tables from ``EmployeeIR.validate`` and made documents unsaveable. The same
hazard applies here: on an operation whose only material is the customer's finding, an
emptied automatic pool is the normal case rather than an edge case.

Not to be confused with ``Customer.custom_no_wastage``
(``employee_ir._bulk_no_wastage_batches``), which refuses loss for a whole CUSTOMER across all
their material and throws from the automatic path. That is a per-customer contract; this is a
per-item-type, per-origin rule that applies whatever the customer flag says.

FAIL OPEN ON UNRESOLVABLE ORIGIN
--------------------------------
A Customer Goods finding batch whose voucher cannot be resolved at all -- both the detail row
and ``reference_name`` gone -- is NOT blocked. ``MOP Log.batch_no`` is a ``Data`` field with
no referential integrity and the 940 reference-stripped batches are real, so an unresolvable
batch is routine rather than exceptional; every other default in this app
(``row_ownership.DEFAULT_INVENTORY_TYPE``, ``ownership_priority.loss_rank``) treats an
unresolvable owner as the company's. Failing closed would make documents silently
unsubmittable for a provenance gap nobody can repair from the shop floor.

KNOWN LIMITS
------------
* **A re-minted batch escapes.** Process Loss and Repack mint a NEW batch carrying ownership
  forward, whose ``reference_doctype`` points at the repack rather than at the original
  receipt. Such a batch is still Customer Goods but is not blocked. That follows the
  specification as written (two named origins). Widening it means following lineage through
  ``Batch.custom_origin_entries`` / ``Batch Component`` -- ``_source_batch_voucher_type`` in
  ``customization/batch/doc_events/utils.py`` is the in-repo precedent -- and nothing else
  here has to move.
* **``Customer Stock`` is not blocked**, only ``Customer Goods``. ``row_ownership`` defines
  two customer types; this rule names one.
* **``FL-`` items are not caught**, because matching is on exact ``variant_of == "F"`` -- the
  same deliberate choice ``material_loss_gate`` makes. An ``FL`` item is minted loss, never
  customer-received.
"""

import frappe
from frappe import _

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.material_loss_gate import (
	get_variant_of_map,
)

#: The one inventory type this gate refuses. ``Customer Stock`` is deliberately excluded.
CUSTOMER_GOODS = "Customer Goods"

#: Exact ``Item.variant_of`` for a finding, matching ``material_loss_gate.LOSS_BLOCK_FLAGS``.
FINDING_TEMPLATE = "F"

STOCK_ENTRY = "Stock Entry"
PURCHASE_RECEIPT = "Purchase Receipt"

#: The seeded Stock Entry Type for a customer goods receipt (``patches/seed_stock_entry_types``).
#: Note the past tense -- "Customer Goods Receive" is an ACCOUNT name on this site, not a type.
DEFAULT_RECEIPT_SE_TYPE = "Customer Goods Received"

#: The operator-entered table, named as it reads ON THE FORM.
MANUAL_TABLE_LABEL = "Manually Book Loss Details"

#: Batch provenance fields. ``custom_voucher_detail_no`` is read only when the column exists:
#: ``install.py`` asserts ``custom_inventory_type`` and ``custom_customer`` as fields this app
#: cannot function without, but NOT this one, and the app's ``custom_fields/*.json`` are not
#: applied by migrate. An unguarded select would raise MariaDB 1054 from inside the loss engine
#: and abort the submit -- the same failure ``_row_value`` in
#: ``customization/batch/doc_events/utils.py`` guards with the same probe.
_BATCH_FIELDS = ("name", "reference_doctype", "reference_name")
_DETAIL_FIELD = "custom_voucher_detail_no"

#: ``(child doctype, parent voucher doctype)`` for the two tables that can mint a batch here.
_DETAIL_PARENTS = (
	("Stock Entry Detail", STOCK_ENTRY),
	("Purchase Receipt Item", PURCHASE_RECEIPT),
)


def _get(row, fieldname):
	"""Read ``fieldname`` off a row that may be a dict or a Document/namespace.

	The automatic path passes MOP balance dicts and the manual path passes child Documents,
	so both shapes reach this module. Mirrors ``row_ownership._get``.
	"""
	if isinstance(row, dict):
		return row.get(fieldname)
	return getattr(row, fieldname, None)


def get_customer_goods_receipt_se_types():
	"""Stock Entry Types that count as a customer goods receipt.

	The seeded literal, plus whatever ``Subcontracting Settings`` configures while the
	Customer Gold flow is on (``get_customer_gold_receipt_type`` already returns ``None``
	when it is off, and reads the Single through ``frappe.get_cached_doc`` once per request).

	Wrapped because a site without that module, that field, or that Single must still get the
	literal rather than an exception raised from inside the loss engine.
	"""
	types = {DEFAULT_RECEIPT_SE_TYPE}

	try:
		from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
			get_customer_gold_receipt_type,
		)

		configured = get_customer_gold_receipt_type()
	except Exception:
		configured = None

	if configured:
		types.add(configured)
	return types


def _resolve_detail_parents(detail_names):
	"""``{detail row name: (voucher doctype, voucher name)}`` across both minting tables.

	Queried table by table, each time only for the names still unresolved, so a document whose
	batches all came from Stock Entries never touches ``Purchase Receipt Item``. Both queries
	are driven from a bounded, caller-supplied name list and key on PRIMARY.
	"""
	resolved = {}
	for child_doctype, parent_doctype in _DETAIL_PARENTS:
		remaining = [name for name in detail_names if name not in resolved]
		if not remaining:
			break
		for row in frappe.db.get_all(
			child_doctype,
			filters={"name": ["in", remaining]},
			fields=["name", "parent"],
		):
			if row.get("parent"):
				resolved[row["name"]] = (parent_doctype, row["parent"])
	return resolved


def get_blocked_finding_batches(rows):
	"""``{batch_no: voucher}`` for the batches this gate refuses loss on.

	``rows`` is any iterable of objects carrying ``item_code`` and ``batch_no`` -- MOP balance
	dicts and loss child rows both qualify. The value is the minting voucher's name, so a
	refusal can say where the material came from.

	Only blocked batches appear, so a caller tests membership and still has the voucher for the
	message. Returns ``{}`` -- without querying at all where possible -- whenever nothing can
	match, so the default path pays nothing.
	"""
	pairs = [(_get(row, "item_code"), _get(row, "batch_no")) for row in rows or []]
	# Narrow on the item-code prefix BEFORE any query. Free, and it keeps the promise the
	# sibling gates make: an operation carrying no findings at all -- the common case on a
	# metal-only operation -- issues zero extra queries. The prefix also catches "FL-", so
	# variant_of still has to be resolved below to exclude it; this only decides whether
	# that lookup happens, never what it answers. Same prefilter
	# ``finding_loss_gate.get_finding_category_map`` applies, for the same reason.
	pairs = [
		(item, batch)
		for item, batch in pairs
		if item and batch and item[0] == FINDING_TEMPLATE
	]
	if not pairs:
		return {}

	# 1. Findings only, on EXACT variant_of, resolved through the shared map so this gate
	#    and material_loss_gate read variant_of through one code path.
	variant_map = get_variant_of_map({item for item, _batch in pairs})
	finding_batches = sorted(
		{batch for item, batch in pairs if variant_map.get(item) == FINDING_TEMPLATE}
	)
	if not finding_batches:
		return {}

	# 2. ...of those, the Customer Goods ones. The inventory-type filter is pushed into the
	#    query so a document holding only company stock reads no provenance at all.
	fields = list(_BATCH_FIELDS)
	if frappe.db.has_column("Batch", _DETAIL_FIELD):
		fields.append(_DETAIL_FIELD)

	batches = frappe.db.get_all(
		"Batch",
		filters={
			"name": ["in", finding_batches],
			"custom_inventory_type": CUSTOMER_GOODS,
		},
		fields=fields,
	)
	if not batches:
		return {}

	# 3. Resolve each batch to its minting voucher: detail row first, reference_name second.
	detail_names = sorted(
		{b.get(_DETAIL_FIELD) for b in batches if b.get(_DETAIL_FIELD)}
	)
	detail_parent = _resolve_detail_parents(detail_names) if detail_names else {}

	voucher_of = {}
	for batch in batches:
		voucher = detail_parent.get(batch.get(_DETAIL_FIELD))
		if not voucher and batch.get("reference_doctype") in (
			STOCK_ENTRY,
			PURCHASE_RECEIPT,
		):
			voucher = (batch["reference_doctype"], batch.get("reference_name"))
		# Fail open on an unresolvable origin -- see the module docstring.
		if voucher and voucher[1]:
			voucher_of[batch["name"]] = voucher
	if not voucher_of:
		return {}

	# 4. A Purchase Receipt origin always qualifies; a Stock Entry only on a receipt type.
	se_names = sorted({name for dt, name in voucher_of.values() if dt == STOCK_ENTRY})
	receipt_ses = set()
	if se_names:
		receipt_ses = set(
			frappe.db.get_all(
				STOCK_ENTRY,
				filters={
					"name": ["in", se_names],
					"stock_entry_type": [
						"in",
						sorted(get_customer_goods_receipt_se_types()),
					],
				},
				pluck="name",
			)
		)

	return {
		batch_no: voucher
		for batch_no, (doctype, voucher) in voucher_of.items()
		if doctype == PURCHASE_RECEIPT or voucher in receipt_ses
	}


def is_customer_goods_finding_blocked(batch_no, blocked_map):
	"""True only when ``batch_no`` is one ``get_blocked_finding_batches`` refused.

	A pure predicate with no DB access, so the auto pool can test it per row for free and the
	suite can pin it without patching anything -- the same separation
	``is_loss_booking_blocked`` and ``is_variant_loss_blocked`` keep from their resolvers.
	"""
	if not blocked_map:
		return False
	if not batch_no:
		return False
	return batch_no in blocked_map


def validate_customer_goods_finding_loss_rows(doc):
	"""Throw if an OPERATOR-ENTERED loss row sits on a customer-supplied finding batch.

	``manually_book_loss_details`` only. ``employee_loss_details`` is deliberately not
	inspected: it is machine-written and ``book_metal_loss`` already excludes blocked batches
	while building it, so the only way a blocked row reaches it is a draft saved before the
	batch became resolvable. Refusing that would punish the operator for a state they had no
	part in; re-saving the draft rebuilds the table correctly and always succeeds.

	Caller is ``EmployeeIR.on_submit`` and nothing else -- this must not run on save. Not
	gated on ``docstatus``: by the time ``on_submit`` runs it is already 1, matching
	``validate_loss_tables_required``.
	"""
	if getattr(doc, "type", None) != "Receive":
		return

	rows = getattr(doc, "manually_book_loss_details", None) or []
	if not rows:
		return

	blocked = get_blocked_finding_batches(rows)
	if not blocked:
		return

	for row in rows:
		batch_no = _get(row, "batch_no")
		if not is_customer_goods_finding_blocked(batch_no, blocked):
			continue
		frappe.throw(
			_(
				"{0} row #{1}: batch <b>{2}</b> is customer-supplied Finding stock "
				"(Customer Goods, received on <b>{3}</b>), so no loss can be booked "
				"against <b>{4}</b>. Delete this row and receive the full issued weight "
				"for this finding, or book the loss against company-owned material."
			).format(
				MANUAL_TABLE_LABEL,
				_get(row, "idx"),
				batch_no,
				blocked.get(batch_no),
				_get(row, "item_code"),
			)
		)
