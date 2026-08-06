"""Track, on every Serial No, WHICH SALES DOCUMENT currently holds the piece.

The pointer is the pair ``Serial No.custom_reference_doctype`` /
``custom_reference_docname`` (provisioned by
``patches/add_serial_no_sales_reference_fields.py``). It advances as the piece moves

    Sales Order  ->  Delivery Note  ->  Sales Invoice

and is written on ``validate`` — on SAVE, including drafts — not on submit, so the Serial
No answers "where is this piece right now?" the moment somebody starts a document for it.

This is NOT the core ``reference_doctype`` / ``reference_name`` pair. Those are core
ERPNext fields meaning "the voucher that CREATED this serial"; ERPNext writes them in
``SerialandBatchBundle.set_source_document_no`` and clears them in
``remove_source_document_no``, and two readers in this app depend on that meaning
(``_get_company_context`` in doc_events/sales_order.py and
``SerialNumberCreator.get_serial_summary``). They are left alone.

WRITING ON VALIDATE MEANS DRAFTS CAN GO STALE. Three rules contain that:

1. FORWARD-ONLY. A stage never claims a serial away from a LATER stage. A brand-new draft
   Sales Order cannot steal a piece that is already on a live Sales Invoice.
2. RECLAIM A DEAD INCUMBENT. The forward-only rule is lifted when the document currently
   holding the pointer is no longer a live forward claim — it was deleted, cancelled, or
   is a return / credit note. That is what lets a piece be re-sold after a sales return.
3. RELEASE ON REMOVAL. Every save diffs against ``get_doc_before_save()`` and clears the
   pointer on serials this save DROPPED — but only where the pointer still points at this
   document, so a serial another document has already claimed is never disturbed.

Same-stage collisions (serial moved from one Sales Order to another) resolve as "latest
document wins", per the agreed design.

Cancel and delete are handled by ``clear_serial_reference`` because Frappe does NOT run
``validate`` on cancel (``Document.run_before_save_methods`` dispatches only
``before_cancel``), so a cancelled document would otherwise keep the pointer forever.

PERFORMANCE. One indexed SELECT over the document's serials, then at most ONE batched
UPDATE for the claims and ONE for the releases — independent of how many item rows the
document has. Re-saving an unchanged document writes NOTHING (see the "already ours" guard
in ``_decide_claims``). The writes go through ``frappe.qb`` rather than
``frappe.db.set_value`` for two reasons: ``set_value`` with a dict filter calls
``frappe.clear_document_cache("Serial No")`` with NO name, which is a Redis SCAN over every
serial key on a ~20k-serial site; and ``set_value`` bumps ``modified``, which on a save-time
hook would churn Serial No timestamps (and risk TimestampMismatchError for anyone holding a
Serial No form open) on every Sales Order save. Core ERPNext writes this exact table with
the same ``frappe.qb.update(...).where(name.isin(...))`` idiom.
"""

import frappe

# Sales-lifecycle stage order. Higher rank = later in the lifecycle. A serial's pointer
# only moves FORWARD through these unless the incumbent is dead (see _incumbent_is_live).
_STAGE_RANK = {
	"Sales Order": 1,
	"Delivery Note": 2,
	"Sales Invoice": 3,
}

# Of the stages above, the ones that actually have an ``is_return`` column. Sales Order
# does not, so it must not be included in the get_value field list or the query throws.
_HAS_IS_RETURN = frozenset({"Delivery Note", "Sales Invoice"})


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def _split_serials(value):
	"""Normalize a serial_no field value to a list.

	``Sales Order Item.serial_no`` is a Link (exactly one serial per row — the jewellery
	flow is one piece per row, ``_update_bom_totals`` sets ``row.qty = 1``), but
	``Delivery Note Item.serial_no`` and ``Sales Invoice Item.serial_no`` are core Text
	fields that ERPNext may fill with a newline-separated list. Handle both, and tolerate
	comma separation, which operators do use.
	"""
	if not value:
		return []
	return [s.strip() for s in str(value).replace(",", "\n").split("\n") if s.strip()]


def _serials_of(doc):
	"""Ordered, de-duplicated list of every serial on ``doc``'s item rows.

	``custom_serial_no`` is the gke_customization Data mirror that the product-return
	client flow writes (public/js/doctype_js/sales_invoice.js); it is only consulted when
	``serial_no`` is empty, so the two can never double-count.
	"""
	serials = []
	for row in doc.get("items") or []:
		serials.extend(
			_split_serials(row.get("serial_no") or row.get("custom_serial_no"))
		)
	return list(dict.fromkeys(serials))


def _decide_claims(doctype, docname, rows):
	"""Which serials must be (re)pointed at ``doctype`` / ``docname``.

	``rows`` are the current pointer values, as returned by ``frappe.get_all("Serial No",
	fields=["name", "custom_reference_doctype", "custom_reference_docname"])``.

	Kept free of any query of its own (bar the rare ``_incumbent_is_live`` probe) so the
	decision table is unit-testable without a database.
	"""
	rank = _STAGE_RANK[doctype]
	to_claim = []

	for row in rows:
		ref_dt = row.get("custom_reference_doctype")
		ref_dn = row.get("custom_reference_docname")

		if ref_dt == doctype and ref_dn == docname:
			# Already ours — this is the idempotency guard that makes a no-op re-save
			# cost zero writes.
			continue

		if not ref_dt or not ref_dn:
			to_claim.append(row.get("name"))  # unclaimed
		elif rank >= _STAGE_RANK.get(ref_dt, 0):
			to_claim.append(row.get("name"))  # forward, or same stage -> latest wins
		elif not _incumbent_is_live(ref_dt, ref_dn):
			to_claim.append(row.get("name"))  # backward, but incumbent is dead

	return to_claim


# ---------------------------------------------------------------------------
# database helpers
# ---------------------------------------------------------------------------


def _has_pointer_fields():
	"""``True`` when the Serial No pointer columns exist on this site.

	``frappe.get_meta`` is request-cached, so this is effectively free. It exists because
	this module runs on EVERY sales-document save: on a site where the patch has not run
	(this app's recurring "custom_fields/*.json are dead config" problem) a missing column
	would otherwise take down all sales entry with ``1054 Unknown column``.
	"""
	meta = frappe.get_meta("Serial No")
	return meta.has_field("custom_reference_doctype") and meta.has_field(
		"custom_reference_docname"
	)


def _incumbent_is_live(doctype, docname):
	"""``True`` when the document holding the pointer is still a live forward claim.

	Dead means: not a stage this module manages, deleted, cancelled, or a return / credit
	note. A return frees the piece for re-sale, which is why it must not block a later
	Sales Order from reclaiming the pointer.
	"""
	if doctype not in _STAGE_RANK:
		return False

	fields = ["docstatus", "is_return"] if doctype in _HAS_IS_RETURN else ["docstatus"]
	values = frappe.db.get_value(doctype, docname, fields, as_dict=True)
	if not values:
		return False  # deleted -> reclaimable
	if values.get("docstatus") == 2:
		return False  # cancelled -> reclaimable
	return not values.get("is_return")


def _write_pointer(serials, doctype, docname):
	"""Point ``serials`` at ``doctype`` / ``docname`` in one batched UPDATE."""
	if not serials:
		return

	sn = frappe.qb.DocType("Serial No")
	(
		frappe.qb.update(sn)
		.set(sn.custom_reference_doctype, doctype)
		.set(sn.custom_reference_docname, docname)
		.where(sn.name.isin(serials))
	).run()

	for serial in serials:
		frappe.clear_document_cache("Serial No", serial)


def _release(serials, doctype, docname):
	"""Clear the pointer on ``serials`` — but ONLY where it still points at this document.

	Scoping the UPDATE by both pointer columns is what makes this safe to run
	unconditionally: a serial that a later document has already claimed is left alone, so
	cancelling an old Delivery Note can never blank the Sales Invoice that superseded it.
	Same shape as core's ``remove_source_document_no``.
	"""
	if not serials:
		return

	sn = frappe.qb.DocType("Serial No")
	(
		frappe.qb.update(sn)
		.set(sn.custom_reference_doctype, None)
		.set(sn.custom_reference_docname, None)
		.where(
			sn.name.isin(serials)
			& (sn.custom_reference_doctype == doctype)
			& (sn.custom_reference_docname == docname)
		)
	).run()

	for serial in serials:
		frappe.clear_document_cache("Serial No", serial)


# ---------------------------------------------------------------------------
# doc_events entry points
# ---------------------------------------------------------------------------


def set_serial_reference(doc, method=None):
	"""``validate`` hook for Sales Order / Delivery Note / Sales Invoice.

	Points every serial on the document at the document, and releases the serials this
	save dropped. Both ``doc.name`` and ``get_doc_before_save()`` are already populated by
	the time ``validate`` runs, on insert as well as update (``Document.insert`` calls
	``set_new_name`` before ``run_before_save_methods``; ``Document._save`` calls
	``check_if_latest`` -> ``load_doc_before_save`` before it).
	"""
	if doc.doctype not in _STAGE_RANK or not doc.get("name"):
		return
	if not _has_pointer_fields():
		return

	current = _serials_of(doc)

	# Stale-draft containment: a serial the user just took OFF this document must not keep
	# pointing at it. Diffing the pre-save copy is exact and costs no extra query.
	before = doc.get_doc_before_save()
	if before:
		still_here = set(current)
		dropped = [s for s in _serials_of(before) if s not in still_here]
		_release(dropped, doc.doctype, doc.name)

	if not current:
		return

	rows = frappe.get_all(
		"Serial No",
		filters={"name": ("in", current)},
		fields=["name", "custom_reference_doctype", "custom_reference_docname"],
	)

	_write_pointer(_decide_claims(doc.doctype, doc.name, rows), doc.doctype, doc.name)


def clear_serial_reference(doc, method=None):
	"""``on_cancel`` / ``on_trash`` hook for Sales Order / Delivery Note / Sales Invoice.

	Required because Frappe does not run ``validate`` on cancel, so ``set_serial_reference``
	never fires there and a cancelled or deleted document would hold the pointer forever.
	Deleting matters more than cancelling: a pointer at a DELETED document is a broken
	Dynamic Link, and ``Document._validate_links`` would then throw on the next
	``Serial No.save()`` — which this app triggers via its own Serial No ``validate`` hook.

	The pointer is cleared, not rewound to the predecessor stage: the predecessor's next
	save reclaims it anyway (forward-or-equal rank), so "no live sales document holds this"
	is the honest interim state.
	"""
	if doc.doctype not in _STAGE_RANK:
		return
	if not _has_pointer_fields():
		return

	_release(_serials_of(doc), doc.doctype, doc.name)
