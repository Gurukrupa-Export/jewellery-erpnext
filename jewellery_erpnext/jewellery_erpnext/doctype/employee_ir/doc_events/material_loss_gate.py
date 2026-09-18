"""Per-operation, blanket per-material gate on Employee IR loss booking.

Department Operation carries four Check fields — "Don't Allow Loss Metal",
"Don't Allow Loss Diamond", "Don't Allow Loss Finding" and "Don't Allow Loss
Gemstone". Ticking one says: on this operation nothing whose ``Item.variant_of``
is that template may carry process loss at all, whether the row was created
automatically or typed into the manual table.

This is the blanket sibling of ``finding_loss_gate``. That module gates one
finding category at a time off the ``finding_loss_booking`` table; this one gates
a whole material class off a single checkbox. The two are independent and purely
additive — a row blocked by either gate is blocked, and neither reads the other's
configuration.

**Fail-open contract**, identical to ``finding_loss_gate``. An operation with no
box ticked books loss exactly as before: ``get_blocked_loss_variants`` returns an
empty set, which makes every ``is_variant_loss_blocked`` call short-circuit to
False and skips the ``variant_of`` prefetch entirely. Shipping this module
changes nothing on any existing site until someone ticks a box.

**Exact variant_of, not item-code prefix.** ``variant_of`` is compared by
equality against the template names "M"/"D"/"F"/"G", so the loss and broken
variants this app also mints ("ML", "FL", "DL", "GL", "DM", "DBK", "DB", "GB")
are NOT caught — an ``ML-`` item still books loss under "Don't Allow Loss Metal".
That is the specification as written. Widening it later means turning the
template values in ``LOSS_BLOCK_FLAGS`` into tuples and making the membership
test in ``is_variant_loss_blocked`` a set intersection; nothing else has to move.

**Automatic and manual loss are treated differently on purpose.**

  * **Automatic** (``employee_loss_details``) — ``EmployeeIR.book_metal_loss``
    drops blocked items from the proportional pool, immediately after the
    finding-category skip and BEFORE ``total_qty`` is summed, so the survivors
    absorb the blocked row's share and the booked total still equals
    ``gross_wt - received_gross_wt`` — which is why the balance validators in
    ``validation_utils`` need no changes. This path **never throws**, not even
    when it empties the pool. That pool is already restricted to ``M``/``F`` item
    codes upstream, so in practice only the Metal and Finding flags change what it
    produces; Diamond and Gemstone can only ever bite on the manual table.
  * **Manual** (``manually_book_loss_details``) —
    ``validate_loss_rows_against_material_gate`` throws, and does so **only from
    ``on_submit``**. Saving a draft must always succeed.

**Do not add a validate-time call.** An earlier revision checked both tables from
``EmployeeIR.validate`` and refused the save. On a metal-only operation with
"Don't Allow Loss Metal" ticked that made the document unsaveable, because an
emptied automatic pool is the normal case there rather than an edge case.

Known, accepted gap: a draft saved before an admin ticked the box keeps its
automatic rows through submit, because Frappe sets ``docstatus = 1`` before
``validate`` runs on a submit (``frappe/model/document.py`` ``_submit``), so
``validate_process_loss`` early-returns and never rebuilds the table. Re-saving
the draft fixes it and always succeeds. Blocking it instead would mean throwing at
an operator for an admin's change they had no part in.

``validate_material_gate_left_nothing_to_book`` covers the remaining case: the
flags emptied the automatic table and nothing was booked by hand, so the operator
gets a message naming the flag instead of the generic "no loss details found".

Blocked manual rows are thrown on rather than silently dropped at Stock Entry
creation, for the same reason ``finding_loss_gate`` gives: ``get_employee_ir_loss_map``
reads the child tables directly, so suppressing only the Stock Entry line would
book a MOP Log loss with no matching stock movement.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

# (Department Operation fieldname, Item.variant_of template, label for messages),
# in the order the four checkboxes read on the form. This tuple is the single
# source of truth: the fields queried, the templates matched and the wording of
# every message all derive from it.
LOSS_BLOCK_FLAGS = (
	("dont_allow_loss_metal", "M", "Don't Allow Loss Metal"),
	("dont_allow_loss_diamond", "D", "Don't Allow Loss Diamond"),
	("dont_allow_loss_finding", "F", "Don't Allow Loss Finding"),
	("dont_allow_loss_gemstone", "G", "Don't Allow Loss Gemstone"),
)
LOSS_BLOCK_FIELDS = [field for field, _variant, _label in LOSS_BLOCK_FLAGS]
VARIANT_FLAG_LABELS = {variant: label for _field, variant, label in LOSS_BLOCK_FLAGS}

# The operator-entered table, named as it reads ON THE FORM.
MANUAL_TABLE_LABEL = "Manually Book Loss Details"


def get_blocked_loss_variants(operation):
	"""``{variant_of}`` templates this Department Operation refuses loss on.

	An empty set means "book loss exactly as before" — see the fail-open contract
	above. Returns an empty set when the operation is unset or no box is ticked,
	which makes every ``is_variant_loss_blocked`` call short-circuit to False.
	"""
	if not operation:
		return set()

	flags = frappe.db.get_value(
		"Department Operation", operation, LOSS_BLOCK_FIELDS, as_dict=True
	)
	if not flags:
		return set()

	return {v for field, v, _label in LOSS_BLOCK_FLAGS if cint(flags.get(field))}


def get_variant_of_map(item_codes):
	"""``{item_code: variant_of}`` in one round-trip.

	One query for the whole batch of rows rather than a lookup per row, following
	the app's prefetch-map convention. ``EmployeeIR._bulk_variant_of`` delegates
	here so a single document resolves ``variant_of`` through one code path.
	"""
	item_codes = sorted({i for i in (item_codes or []) if i})
	if not item_codes:
		return {}
	return {
		r["name"]: r["variant_of"]
		for r in frappe.db.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "variant_of"],
		)
	}


def is_variant_loss_blocked(item_code, blocked_variants, variant_map):
	"""True only when the item's template is one this operation refuses."""
	if not blocked_variants:
		return False
	if not item_code:
		return False

	# Fail open on an unknown or blank variant_of: a non-variant item (the literal
	# "METAL LOSS" / "FINDING LOSS" codes, say) belongs to no template and books
	# loss exactly as before.
	return (variant_map or {}).get(item_code) in blocked_variants


def validate_loss_rows_against_material_gate(doc):
	"""Throw if an OPERATOR-ENTERED loss row sits on a blocked material.

	``manually_book_loss_details`` only. ``employee_loss_details`` is deliberately
	not inspected: it is machine-written and ``book_metal_loss`` already excludes
	blocked items while building it, so the only way a blocked row reaches it is a
	draft saved before the flag was ticked. Refusing that would punish the operator
	for an admin's change they had no part in; re-saving the draft rebuilds the
	table correctly and always succeeds.

	Caller is ``EmployeeIR.on_submit`` and nothing else — this must not run on
	save. Not gated on ``docstatus``: by the time ``on_submit`` runs it is already
	1, matching ``validate_loss_tables_required``.
	"""
	if getattr(doc, "type", None) != "Receive":
		return

	rows = getattr(doc, "manually_book_loss_details", None) or []
	if not rows:
		return

	blocked_variants = get_blocked_loss_variants(getattr(doc, "operation", None))
	if not blocked_variants:
		return

	variant_map = get_variant_of_map([row.item_code for row in rows])

	for row in rows:
		if not is_variant_loss_blocked(row.item_code, blocked_variants, variant_map):
			continue
		flag_label = VARIANT_FLAG_LABELS.get(variant_map.get(row.item_code))
		frappe.throw(
			_(
				"{0} row #{1}: <b>{2}</b> is ticked on operation <b>{3}</b>, so no "
				"loss can be booked against <b>{4}</b>. Delete this row, or untick "
				"<b>{2}</b> on the Department Operation."
			).format(
				MANUAL_TABLE_LABEL,
				row.idx,
				flag_label,
				doc.operation,
				row.item_code,
			)
		)


def validate_material_gate_left_nothing_to_book(doc):
	"""Explain an automatic loss table these flags emptied.

	When every eligible item in the operation balance is blocked, the automatic
	table comes out empty and ``validate_loss_tables_required`` would raise its
	generic "no loss details found" — which reads as a data problem and tells the
	operator to book loss without saying why none was booked. This runs first and
	names the flag instead.

	Deliberately conservative: it throws ONLY when a re-read of the balance shows
	every eligible row was blocked. If some eligible row was not blocked, the empty
	table has a different cause and blaming the flags would mislead, so this stays
	silent and lets the generic validator speak.

	Caller is ``EmployeeIR.on_submit``. The guard clauses are ordered cheapest
	first, so on the normal path (a populated loss table) it costs nothing.
	"""
	if getattr(doc, "type", None) != "Receive":
		return
	if (getattr(doc, "employee_loss_details", None) or []) or (
		getattr(doc, "manually_book_loss_details", None) or []
	):
		return

	# Same baseline validate_loss_tables_required computes, and the same reason to
	# ignore a receive that gained weight: only a shortfall needs attributing.
	baseline = 0.0
	pairs = []
	for op in getattr(doc, "employee_ir_operations", None) or []:
		if not op.received_gross_wt:
			continue
		if flt(op.gross_wt, 3) > flt(op.received_gross_wt, 3):
			baseline += flt(op.gross_wt, 3) - flt(op.received_gross_wt, 3)
			pairs.append((op.manufacturing_work_order, op.manufacturing_operation))
	baseline = flt(baseline, 3)
	if baseline <= 0 or not pairs:
		return

	blocked_variants = get_blocked_loss_variants(getattr(doc, "operation", None))
	if not blocked_variants:
		return

	# One re-read of the balance, only ever on the failure path.
	balance = frappe.get_all(
		"MOP Log",
		filters={
			"manufacturing_work_order": ["in", sorted({p[0] for p in pairs})],
			"manufacturing_operation": ["in", sorted({p[1] for p in pairs})],
			"is_cancelled": 0,
		},
		fields=["item_code"],
	)
	# Mirror book_metal_loss's own eligibility filter: only M/F item codes ever
	# enter the automatic pool, so only those can have been blocked out of it.
	eligible = {
		r["item_code"]
		for r in balance
		if r["item_code"] and r["item_code"][0] in ("M", "F")
	}
	if not eligible:
		return

	variant_map = get_variant_of_map(sorted(eligible))
	hits = set()
	for item_code in eligible:
		variant = variant_map.get(item_code)
		if variant not in blocked_variants:
			# Something eligible survived the gate, so the flags are not the reason
			# the table is empty.
			return
		hits.add(variant)

	frappe.throw(
		_(
			"Manufacturing Work Order {0}: {1} g of loss is unbooked. With {2} ticked "
			"on operation <b>{3}</b>, nothing in the operation balance could take it "
			"automatically. Book the shortfall in {4} against an item this operation "
			"still allows, or receive the full issued weight."
		).format(
			", ".join(sorted({p[0] for p in pairs if p[0]})),
			baseline,
			", ".join(sorted(VARIANT_FLAG_LABELS.get(v, v) for v in hits if v)),
			doc.operation,
			MANUAL_TABLE_LABEL,
		)
	)
