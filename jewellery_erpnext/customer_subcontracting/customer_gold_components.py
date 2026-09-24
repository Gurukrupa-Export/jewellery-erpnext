# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""C09 -- component-level provenance inside one mixed batch.

THE QUESTION C09 ASKS
---------------------
When a customer's 24KT gold and the company's own alloy are melted into one batch, the batch
has one ``custom_customer`` and one ``custom_inventory_type``. Those say who owns *the batch*.
They cannot say how many grams inside it are the customer's and how many are the company's --
and that is the number every downstream question needs: how much does the customer get back,
how much alloy may the company reclaim, and which part of a purity increase released whose
metal.

WHAT ALREADY EXISTS, AND WHY IT IS NOT ENOUGH
---------------------------------------------
``Batch.custom_origin_entries`` is genuine provenance and the exact C09 shape is already in
production data on ``gk``: a Customer Goods target batch whose origins are the customer's 24KT
(20.000 g) and company alloy ``M-AL`` (1.763 g). So the *data* to answer C09 demonstrably
exists. What is missing is an owner column, apportionment, decrementing, transitivity and a
fine-gold column -- enumerated with evidence in ``Batch Component``'s own docstring.

THE OWNERSHIP RULE, STATED ONCE
-------------------------------
A component's owner is decided ONCE, when the component enters the batch, and is then
immutable history. It is never re-derived from the source batch's current tags, because those
are mutable: ``Batch.custom_customer`` can be edited after the fact, and re-deriving would
silently rewrite the provenance of every descendant batch.

TRANSITIVITY (CG-T083)
----------------------
``resolve_components`` recurses. A batch made from a mixed batch inherits that batch's
*components*, apportioned by how much of it was drawn, not a single row naming the mixed batch.
That is what makes a reverse conversion able to say "3.2 g of this is still the customer's"
rather than "all of this came from batch X, whoever owns X now".

Recursion is depth-limited and cycle-guarded. Batch graphs in this app are acyclic by
construction -- a batch cannot be its own ancestor, because the source must be consumed before
the target exists -- but "by construction" has been wrong here before, and an infinite loop
inside a Stock Entry submit is not an acceptable failure mode.

MONEY, and a limitation stated plainly
--------------------------------------
``rate``/``amount`` are written only under the Nominal valuation policy.

They are Currency fields, and Frappe renders every Currency column ``decimal(21,9) NOT NULL
DEFAULT 0`` -- verified against ``tabBatch Component`` rather than assumed. So under Zero Value
they read **0.0, not NULL**, and a row cannot say on its own whether 0.0 means "valued at
nothing" or "never valued". An earlier revision of this docstring claimed NULL; that was not
achievable and the integration suite is what proved it.

This is a real limitation and it is accepted rather than worked around, because C09 is a
question about ownership and quantity: the authoritative fields here are ``qty``, ``pure_qty``,
``inventory_type`` and ``customer``, none of which have this problem. Where a money question
needs an unambiguous "was this valued at all", the answer lives on
``Customer Gold Ledger Entry.cg_currency`` -- a varchar, genuinely nullable, and stamped only
under Nominal precisely so that discriminator exists somewhere.
"""

import frappe
from frappe.query_builder.functions import Sum
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
	get_customer_gold_valuation_policy,
	is_customer_gold_enabled,
)

COMPONENT_TABLE = "custom_batch_components"
COMPONENT_DOCTYPE = "Batch Component"
CUSTOMER_GOODS = "Customer Goods"
REGULAR_STOCK = "Regular Stock"

#: How deep ``resolve_components`` will follow a batch's ancestry before giving up and
#: treating the batch as atomic. Ten is far beyond any real conversion chain (the longest
#: observed on ``gk`` is three) and exists only so a malformed graph degrades to a coarser
#: answer instead of hanging a submit.
MAX_DEPTH = 10

QTY_PRECISION = 3

#: Stock Entry purposes that MAKE a batch's contents. A component table describes what one of
#: these put into the batch. Transfers, returns and reconciliations move a batch or restate it;
#: they never make it, so they must not move the denominator.
PRODUCING_PURPOSES = ("Manufacture", "Repack", "Material Receipt")


def is_component_schema_ready():
	"""True when this app's component schema is actually present on the site.

	Asked BEFORE writing, never rescued afterwards -- the same discipline as
	``subcontracting_settings.has_settings_capability``, and for the same reason: the alternative
	is a broad exception catch on a hot submit path.

	Both halves are needed. The ``Batch Component`` DocType ships as app source and arrives with
	``bench migrate``; ``Batch.custom_batch_components`` is a Custom Field and arrives ONLY through
	``patches/add_batch_component_field.py`` (``custom_fields/batch.json`` is inert, because the
	``after_migrate`` hook is commented out at ``hooks.py:12``). A site can genuinely have one and
	not the other, and that is not hypothetical -- it is the measured state of every site on this
	bench except the disposable one.

	``Meta.has_field`` sees Custom Fields, which ``frappe.db.field_exists`` (DocField-only) would
	not. Both lookups are cache-first, so a warm request pays no query.
	"""
	if not frappe.db.exists("DocType", COMPONENT_DOCTYPE):
		return False

	try:
		return bool(frappe.get_meta("Batch").has_field(COMPONENT_TABLE))
	except frappe.DoesNotExistError:
		return False


def _batch_identity(batch_no):
	"""Owner and purity of a batch as it stands right now.

	Used ONLY for a batch that has no recorded components -- i.e. the first time it is mixed
	into something. From then on the recorded component rows are the authority and this is
	never consulted again for that lineage.
	"""
	return (
		frappe.db.get_value(
			"Batch",
			batch_no,
			[
				"custom_inventory_type",
				"custom_customer",
				"custom_pure_metal_qty",
				"batch_qty",
				"item",
			],
			as_dict=True,
		)
		or frappe._dict()
	)


def _recorded_components(batch_no):
	"""Component rows already stored on a batch, newest state, as plain dicts.

	Fails CLOSED to "no components recorded" on a site whose schema has not caught up. This
	is read from ``make_metal_stock_entry``, on the path of every Metal Conversion submit --
	including conversions that have nothing to do with customer gold -- so an exception here
	would abort unrelated production work on any site that has this app's code but not yet its
	DocType row. That is the same failure direction, and the same reasoning, as
	``is_customer_gold_enabled``.

	Returning ``[]`` is also the correct answer rather than a fudge: a site with no component
	table genuinely has no recorded components, and every caller already treats that as
	"carve out nothing, behave exactly as before".
	"""
	if not batch_no:
		return []

	try:
		return frappe.get_all(
			COMPONENT_DOCTYPE,
			filters={
				"parent": batch_no,
				"parenttype": "Batch",
				"parentfield": COMPONENT_TABLE,
			},
			fields=[
				"item_code",
				"inventory_type",
				"customer",
				"qty",
				"pure_qty",
				"source_batch",
				"source_voucher_type",
				"source_voucher_detail_no",
				"rate",
				"amount",
			],
			order_by="idx",
		)
	except (
		frappe.DoesNotExistError,
		frappe.db.TableMissingError,
		frappe.db.InvalidColumnName,
	):
		return []


def resolve_components(batch_no, drawn_qty, depth=0, seen=None):
	"""What ``drawn_qty`` taken out of ``batch_no`` is actually made of.

	Returns a list of component dicts, each ``qty`` in its OWN item's units -- a customer's 24KT
	component in 24KT grams, however many conversions sit between it and ``batch_no``. This is the
	transitive answer CG-T083 needs: if the batch is itself mixed, its components are apportioned
	pro rata and returned instead of a single row naming the batch.

	``drawn_qty`` is apportioned, never passed through whole -- that is gap 3 in the
	``Batch Component`` docstring, where the existing origin-entry writer hands the FULL lane
	source list to every inward row and double-counts.

	The apportioning denominator is :func:`attribution_basis`, not the component total. The two
	agree whenever the batch is exactly what its components say, and then the result sums to
	``drawn_qty`` as it always did. They part company in two cases, and the total was wrong in
	both: a finished piece counted in Nos, whose components are grams and carats; and a conversion
	whose alloy was never recorded as a component, where 6.000 g of 99.9% became 7.950 g of 75.4%
	and dividing by 6.000 attributed all 7.950 g to the customer as 24KT.
	"""
	drawn_qty = flt(drawn_qty, QTY_PRECISION)
	if not batch_no or drawn_qty <= 0:
		return []

	seen = seen or set()
	if batch_no in seen or depth >= MAX_DEPTH:
		# Degrade to "this batch, as it is tagged today" rather than recursing further. A
		# coarser answer that terminates beats a precise one that does not.
		return [_atomic_component(batch_no, drawn_qty)]

	components = _recorded_components(batch_no)
	if not components:
		return [_atomic_component(batch_no, drawn_qty)]

	total = flt(sum(flt(c["qty"]) for c in components), QTY_PRECISION)
	if total <= 0:
		return [_atomic_component(batch_no, drawn_qty)]

	basis = attribution_basis(batch_no, components) or total

	resolved = []
	for component in components:
		share = flt(flt(component["qty"]) / basis * drawn_qty, QTY_PRECISION)
		if share <= 0:
			continue

		# A component that names a source batch which ITSELF has components must be followed;
		# otherwise provenance stops one level down and CG-T083 cannot be answered.
		#
		# The ``_recorded_components`` test is load-bearing, not an optimisation. Recursing
		# unconditionally means an ATOMIC source batch gets resolved by ``_atomic_component``,
		# which reads the batch's CURRENT ``custom_customer``/``custom_inventory_type`` -- and
		# that silently overwrites the ownership recorded on this component row with whatever
		# the source batch is tagged as today. It breaks the module's central rule (a
		# component's owner is decided once, at mixing, and is immutable history) in the exact
		# direction that matters: a customer's metal reverts to company stock as soon as the
		# source batch is re-tagged or consumed. The unit suite caught this.
		source_batch = component.get("source_batch")
		deeper = []
		if (
			source_batch
			and source_batch not in seen
			and depth + 1 < MAX_DEPTH
			and _recorded_components(source_batch)
		):
			deeper = resolve_components(
				source_batch, share, depth + 1, seen | {batch_no}
			)

		if deeper:
			resolved.extend(deeper)
			continue

		fraction = share / flt(component["qty"]) if flt(component["qty"]) else 0.0
		resolved.append(
			{
				"item_code": component.get("item_code"),
				"inventory_type": component.get("inventory_type"),
				"customer": component.get("customer"),
				"qty": share,
				"pure_qty": flt(
					flt(component.get("pure_qty")) * fraction, QTY_PRECISION
				),
				"source_batch": component.get("source_batch") or batch_no,
				"source_voucher_type": component.get("source_voucher_type"),
				"source_voucher_detail_no": component.get("source_voucher_detail_no"),
				"rate": component.get("rate"),
			}
		)

	return resolved or [_atomic_component(batch_no, drawn_qty)]


def produced_qty(batch_no):
	"""How much the producing Stock Entries put into ``batch_no``, or 0.0 when none is recorded.

	The fixed denominator for any draw on the batch. ``Batch.batch_qty`` is the wrong one: it is
	the LIVE balance and falls with every delivery, so the second of three instalments divided by
	two instead of three and the three together released 166.7% of the booked value (K45).

	Read from the batch's inward Serial and Batch Entries, because in v16 the batch lives in the
	bundle and ``Stock Ledger Entry.batch_no`` is normally empty.
	"""
	if not batch_no:
		return 0.0

	sbe = frappe.qb.DocType("Serial and Batch Entry")
	sbb = frappe.qb.DocType("Serial and Batch Bundle")
	se = frappe.qb.DocType("Stock Entry")
	rows = (
		frappe.qb.from_(sbe)
		.join(sbb)
		.on(sbe.parent == sbb.name)
		.join(se)
		.on(se.name == sbb.voucher_no)
		.select(Sum(sbe.qty))
		.where(
			(sbe.batch_no == batch_no)
			& (sbb.voucher_type == "Stock Entry")
			& (sbb.type_of_transaction == "Inward")
			& (sbb.is_cancelled == 0)
			& (sbb.docstatus == 1)
			& (se.docstatus == 1)
			& (se.purpose.isin(PRODUCING_PURPOSES))
		)
	).run()

	return flt(rows[0][0] if rows and rows[0][0] else 0.0, QTY_PRECISION)


def _shares_one_unit(batch_no, components):
	"""Whether the batch and every component are counted in the same stock UOM."""
	item = frappe.db.get_value("Batch", batch_no, "item")
	uom = frappe.get_cached_value("Item", item, "stock_uom") if item else None
	if not uom:
		return False

	return all(
		frappe.get_cached_value("Item", component.get("item_code"), "stock_uom") == uom
		for component in components
	)


def attribution_basis(batch_no, components):
	"""The quantity of ``batch_no`` that ``components`` describe, in the batch's own units.

	Every draw is apportioned as ``component qty x drawn / basis``, so the result stays in the
	component's own units -- source grams -- whatever the batch itself is counted in.

	* **Different units** -- a finished piece in Nos made of grams of gold and carats of diamond.
	  The component total adds grams to carats and means nothing; the pieces produced are the
	  only denominator, and delivering 1 of 1 settles every component whole.
	* **Same units** -- the larger of the component total and the quantity produced:

	  - more produced than recorded: something joined the batch that is not a component (alloy
	    added without a consumed row). Dividing by the total would count that unrecorded metal
	    as the customer's.
	  - more recorded than produced: grams were lost in making it. Dividing by the total keeps
	    the loss where it happened, so a delivery releases only the customer metal physically
	    in the piece and the lost part stays owed until someone approves writing it off (§5.5,
	    §8.2). Dividing by the produced quantity would write it off silently.

	Returns ``None`` when the units differ and no production is recorded: there is then no
	defensible denominator. With the same units and no production record it falls back to the
	component total, the behaviour before this existed.
	"""
	total = flt(sum(flt(c.get("qty")) for c in components), QTY_PRECISION)
	produced = produced_qty(batch_no)
	same_unit = _shares_one_unit(batch_no, components)

	if produced <= 0:
		return total if same_unit and total > 0 else None

	if not same_unit:
		return produced

	return max(total, produced)


def _atomic_component(batch_no, drawn_qty):
	"""A batch with no recorded components is one component: itself."""
	identity = _batch_identity(batch_no)

	# Fine grams are apportioned from the batch's own recorded fine quantity rather than
	# recomputed from a purity attribute. Recomputing would silently adopt whichever of the
	# two disagreeing purity helpers happened to be imported -- the live-master defect
	# recorded as D04, where the same configured item reads 100.0 one way and 99.9 the other.
	batch_qty = flt(identity.get("batch_qty"))
	fine = flt(identity.get("custom_pure_metal_qty"))
	pure_qty = (
		flt(fine * (drawn_qty / batch_qty), QTY_PRECISION)
		if batch_qty and fine
		else 0.0
	)

	return {
		"item_code": identity.get("item"),
		"inventory_type": identity.get("custom_inventory_type") or REGULAR_STOCK,
		"customer": identity.get("custom_customer"),
		"qty": drawn_qty,
		"pure_qty": pure_qty,
		"source_batch": batch_no,
		"source_voucher_type": None,
		"source_voucher_detail_no": None,
		"rate": None,
	}


def merge_components(components):
	"""Collapse components that are the same thing, keyed on the WHOLE identity.

	The key is ``(item, inventory_type, customer, source_batch)``. Keying on the batch alone
	is gap 2 in the ``Batch Component`` docstring -- it is what makes the existing origin-entry
	writer drop a second contribution from the same batch. Two components from the same batch
	but different owners are genuinely two different facts and must stay apart.
	"""
	merged = {}
	order = []

	for component in components:
		key = (
			component.get("item_code"),
			component.get("inventory_type"),
			component.get("customer"),
			component.get("source_batch"),
		)
		if key not in merged:
			merged[key] = dict(component)
			order.append(key)
			continue

		merged[key]["qty"] = flt(
			flt(merged[key]["qty"]) + flt(component["qty"]), QTY_PRECISION
		)
		merged[key]["pure_qty"] = flt(
			flt(merged[key]["pure_qty"]) + flt(component["pure_qty"]), QTY_PRECISION
		)

	return [merged[key] for key in order]


def _rollback_to_savepoint(save_point):
	"""Roll back to ``save_point``, tolerating its absence.

	A MariaDB deadlock (1213) rolls back the whole transaction and discards every savepoint. A
	later ``rollback(save_point=...)`` then raises 1305 "SAVEPOINT does not exist", and that
	SECOND error is what propagates -- masking the real failure and, here, aborting a stock
	movement that the savepoint existed to protect. Same lesson as
	``mop_settings/mop_eod_sync.py:1206-1232``.
	"""
	try:
		frappe.db.rollback(save_point=save_point)
	except Exception:
		frappe.log_error(
			title="Batch Component savepoint already gone",
			message=(
				f"Could not roll back to {save_point}; the enclosing transaction had already "
				f"discarded it (typically after a deadlock).\n\n"
				+ frappe.get_traceback()
			),
		)


def _validated_rows(components, voucher_type, nominal):
	"""Turn resolved components into insertable rows, or raise BEFORE anything is destroyed.

	Every check here is one that would otherwise surface as a half-written component table.
	``item_code`` is ``reqd`` on the child doctype, and ``inventory_type`` is a Link to
	``Inventory Type`` -- a real DocType whose rows are site data, so the module's
	``"Regular Stock"`` literal is a genuine foreign key that can be missing on a fresh site,
	not a magic string.
	"""
	rows = []
	for component in components:
		row = dict(component)
		row["source_voucher_type"] = row.get("source_voucher_type") or voucher_type

		if not row.get("item_code"):
			frappe.throw(
				frappe._(
					"Cannot record batch components: a resolved component has no Item, which "
					"is mandatory. Source batch: {0}."
				).format(row.get("source_batch") or "unknown"),
				title=frappe._("Incomplete Component Provenance"),
			)

		inventory_type = row.get("inventory_type")
		if inventory_type and not frappe.db.exists("Inventory Type", inventory_type):
			frappe.throw(
				frappe._(
					"Cannot record batch components: Inventory Type {0} does not exist on this "
					"site. Provision the Inventory Type records before enabling Customer Gold."
				).format(frappe.bold(inventory_type)),
				title=frappe._("Missing Inventory Type"),
			)

		if flt(row.get("qty")) < 0:
			frappe.throw(
				frappe._(
					"Cannot record batch components: component quantity for source batch {0} "
					"is negative ({1})."
				).format(row.get("source_batch") or "unknown", row.get("qty")),
				title=frappe._("Invalid Component Quantity"),
			)

		if nominal and row.get("rate"):
			row["amount"] = flt(flt(row["rate"]) * flt(row["qty"]))
		else:
			# Under Zero Value there is no value to record. Set explicitly rather than left out,
			# so a re-record cannot carry a stale rate forward. These land as 0.0, not NULL --
			# Frappe's Currency columns are NOT NULL DEFAULT 0; see the module docstring.
			row["rate"] = None
			row["amount"] = None

		rows.append(row)

	return rows


def record_batch_components(target_batch, sources, voucher_type=None):
	"""Write the component breakdown of ``target_batch``.

	``sources`` is a list of ``(batch_no, qty)`` pairs -- what was consumed to make it. Each is
	resolved transitively, the results merged, and the table REPLACED rather than appended to,
	so re-running on the same batch converges instead of accumulating.

	Returns the component rows written.
	"""
	if not target_batch:
		return []

	# THE WRITE GATE. Reads are deliberately NOT gated -- see below.
	#
	# This function is reached from Serial and Batch Bundle ``after_insert``
	# (``hooks.py``), which fires inside the submit of EVERY Manufacture and Repack Stock
	# Entry -- not only customer-gold ones. Until now it ran unconditionally: it did not
	# consult the feature flag at all, and its first act was a settings read. On a site
	# carrying this code without the schema, that meant work and error logs on every such
	# submit, for every company, whether or not customer gold was in use.
	#
	# The recovery specification (§4.1) is explicit that removing the schema patch is NOT the
	# containment: "Removing a patch does not repair already-installed schema, registered
	# hooks, existing events or unsafe installer behavior." So the writer is gated instead,
	# and the patch stays.
	#
	# Reads (``_recorded_components``, ``get_component_qty``, ``get_company_component_qty``,
	# ``resolve_components``) stay ungated ON PURPOSE. A site that recorded components and then
	# turned the feature off must still be able to read, reverse and protect that history --
	# the same rule §4.6 states for fulfilment events. Gating reads would strand existing data.
	if not is_customer_gold_enabled() or not is_component_schema_ready():
		return []

	components = []
	for batch_no, qty in sources:
		components.extend(resolve_components(batch_no, qty))

	components = merge_components(components)
	nominal = get_customer_gold_valuation_policy() == VALUATION_NOMINAL

	# Child rows are written DIRECTLY, never by re-saving the parent Batch.
	#
	# ``frappe.get_doc("Batch", ...).save()`` was the first implementation and it is wrong,
	# measured rather than theorised -- the integration suite failed on it. Two reasons, both
	# serious:
	#
	# 1. **It re-runs every Batch validator.** The customer-goods guard at
	#    ``customization/batch/doc_events/utils.py:63-89`` reads
	#    ``Item.custom_inventory_type_can_be_customer_goods`` and throws when it is unset.
	#    Batches created through the receipt path pass that guard at CREATION via one of its
	#    bypasses; a later plain re-save qualifies for none of them, so recording provenance
	#    would throw on a batch that is already perfectly valid. Since the caller swallows
	#    exceptions (it must not fail a stock movement), provenance would simply never be
	#    recorded -- and nothing would say so.
	# 2. **It re-runs ``Batch.on_update``**, which blends ``custom_metal_rate`` /
	#    ``custom_alloy_rate`` from the origin entries. Writing provenance must not silently
	#    re-blend a batch's valuation as a side effect.
	#
	# Deleting and re-inserting (rather than diffing) is what makes this idempotent: a repost
	# or a re-submit converges on the same table instead of appending to it.
	# VALIDATE THE WHOLE REPLACEMENT SET BEFORE DESTROYING ANYTHING.
	#
	# The previous implementation deleted first and inserted row by row, with the caller
	# swallowing exceptions. A failure mid-loop therefore left the batch with NO components at
	# all -- strictly worse than the "nothing recorded, so carve out nothing" degradation this
	# module's contract relies on, and silent, because the caller only logged.
	rows = _validated_rows(components, voucher_type, nominal)

	# ATOMIC REPLACEMENT.
	#
	# A savepoint, not a transaction: this runs inside the submitting Stock Entry's transaction
	# (Serial and Batch Bundle after_insert -> Stock Entry on_submit), and provenance must not be
	# able to roll back a legitimate stock movement. The savepoint confines a failure here to
	# this batch's component rows and leaves the prior set intact.
	#
	# ``_rollback_to_savepoint`` rather than a bare ``frappe.db.rollback(save_point=...)``:
	# a MariaDB deadlock (1213) rolls back the ENTIRE transaction and discards every savepoint,
	# so the later rollback raises 1305 "SAVEPOINT does not exist" and that secondary error
	# propagates into the submit -- defeating the very guarantee the savepoint was added for.
	# The pattern is ``mop_settings/mop_eod_sync.py:1206-1232``, which already learned this.
	save_point = "cg_components_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(save_point)
	try:
		frappe.db.delete(
			COMPONENT_DOCTYPE,
			{
				"parent": target_batch,
				"parenttype": "Batch",
				"parentfield": COMPONENT_TABLE,
			},
		)
		for idx, row in enumerate(rows, start=1):
			frappe.get_doc(
				{
					"doctype": COMPONENT_DOCTYPE,
					"parent": target_batch,
					"parenttype": "Batch",
					"parentfield": COMPONENT_TABLE,
					"idx": idx,
					**row,
				}
			).insert(ignore_permissions=True)
	except Exception:
		_rollback_to_savepoint(save_point)
		raise
	else:
		frappe.db.release_savepoint(save_point)

	return components


def get_component_qty(batch_no, inventory_type=None, customer=None, item_code=None):
	"""How many grams inside ``batch_no`` match the given ownership, from recorded components.

	Returns 0.0 when the batch has no recorded components. That default is deliberate and it
	is what makes the C09 carve-out safe to ship: a site with no component history behaves
	exactly as it does today, because "no recorded company component" carves out nothing.
	"""
	total = 0.0
	for component in _recorded_components(batch_no):
		if (
			inventory_type is not None
			and component.get("inventory_type") != inventory_type
		):
			continue
		if customer is not None and component.get("customer") != customer:
			continue
		if item_code is not None and component.get("item_code") != item_code:
			continue
		total += flt(component.get("qty"))

	return flt(total, QTY_PRECISION)


def get_company_component_qty(batch_nos, item_code=None):
	"""Total company-owned grams recorded across ``batch_nos``.

	This is the number the Metal Conversion carve-out uses to decide how much released alloy
	is the company's own metal coming back, rather than the customer's.
	"""
	if isinstance(batch_nos, str):
		batch_nos = [batch_nos]

	return flt(
		sum(
			get_component_qty(
				batch_no, inventory_type=REGULAR_STOCK, item_code=item_code
			)
			for batch_no in batch_nos or []
		),
		QTY_PRECISION,
	)
