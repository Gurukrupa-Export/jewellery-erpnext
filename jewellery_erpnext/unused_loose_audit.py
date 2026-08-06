"""Which source items have no usable Unused/Loose Material target — read-only.

The "Receive Unused/Loose Material" action on Manufacturing Operation books returned
material onto a dedicated item carrying the same Metal Purity and Metal Colour as the
item it came from, and BLOCKS the whole receive when no such item exists. This report
lists the gaps up front so the item master can be filled in before an operator hits them.

It shares ``_probe_unused_loose_item`` with the resolver, so what this lists and what the
button blocks on can never drift apart.

NEVER WRITES. There is deliberately no repair mode: creating an item master entry is a
decision about how material is classified, not a mechanical fix.
"""


import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	UNUSED_OK,
	UNUSED_OUT_OF_SCOPE,
	UNUSED_SOURCE_FAMILY,
	UNUSED_TARGET_TEMPLATES,
	_format_item_list,
	_item_attributes,
	_probe_unused_loose_item,
	_unused_target_template,
)

#: Reported alongside a resolved target rather than as a failure: the source and its
#: target disagree on karat. The match key is purity + colour by design, so this does not
#: block anything — but a 18KT source landing on a 22KT-labelled item is worth a look.
TOUCH_MISMATCH = "TOUCH_MISMATCH"

_TOUCH_ATTRIBUTE = "Metal Touch"


def _source_templates(family=None):
	"""Source item templates in scope, optionally narrowed to one family."""
	return sorted(
		template
		for template, fam in UNUSED_SOURCE_FAMILY.items()
		if (not family or fam == family) and frappe.db.exists("Item", template)
	)


def _source_items(templates, scope):
	"""Active variants of ``templates``, narrowed by ``scope``."""
	items = set(
		frappe.db.get_all(
			"Item",
			filters={"variant_of": ["in", templates], "disabled": 0},
			pluck="name",
		)
	)
	if not items or scope == "all":
		return sorted(items)

	if scope == "sre":
		reserved = frappe.db.get_all(
			"Stock Reservation Entry",
			filters={"docstatus": 1, "item_code": ["in", list(items)]},
			pluck="item_code",
			distinct=True,
		)
		return sorted(set(reserved))

	if scope == "stock":
		warehouses = frappe.db.get_all(
			"Warehouse",
			filters={
				"warehouse_type": ["in", ["Raw Material", "Scrap"]],
				"is_group": 0,
			},
			pluck="name",
		)
		if not warehouses:
			return []
		in_stock = frappe.db.get_all(
			"Bin",
			filters={
				"item_code": ["in", list(items)],
				"warehouse": ["in", warehouses],
				"actual_qty": [">", 0],
			},
			pluck="item_code",
			distinct=True,
		)
		return sorted(set(in_stock))

	frappe.throw(f"Unknown scope {scope!r}. Use 'sre', 'stock' or 'all'.")


def run_unused_loose_audit(family=None, scope="sre", limit=None, as_dict=False):
	"""Every source variant whose Unused/Loose Material target is missing or unusable.

	``family``  "metal" | "finding" | None (both).
	``scope``   "sre"   (default) only items reserved by a submitted Stock Reservation
	                    Entry — i.e. exactly what can actually be received today.
	            "stock" items with stock in a Raw Material / Scrap warehouse.
	            "all"   every non-disabled variant of every mapped source template.
	``limit``   cap the number of reported rows (the summary still counts everything).
	"""
	if family and family not in UNUSED_TARGET_TEMPLATES:
		frappe.throw(
			f"Unknown family {family!r}. Use {' or '.join(sorted(UNUSED_TARGET_TEMPLATES))}."
		)

	print(f"Scope: {scope}" + (f"   Family: {family}" if family else ""))
	for fam in sorted(UNUSED_TARGET_TEMPLATES):
		if family and fam != family:
			continue
		resolved = _unused_target_template(fam)
		print(
			f"  {fam:8s} target template: {resolved or '(none usable)'}"
			f"   [candidates: {', '.join(UNUSED_TARGET_TEMPLATES[fam])}]"
		)

	templates = _source_templates(family)
	if not templates:
		print("No source item templates on this site — nothing to audit.")
		return [] if as_dict else None

	rows = []
	counts = {}
	for item_code in _source_items(templates, scope):
		probe = _probe_unused_loose_item(item_code)
		status = probe.status
		if status == UNUSED_OUT_OF_SCOPE:
			continue
		if status == UNUSED_OK:
			source_touch = _item_attributes(item_code).get(_TOUCH_ATTRIBUTE)
			target_touch = _item_attributes(probe.target).get(_TOUCH_ATTRIBUTE)
			if not (source_touch and target_touch and source_touch != target_touch):
				continue
			status = TOUCH_MISMATCH

		counts[status] = counts.get(status, 0) + 1
		rows.append(
			frappe._dict(
				status=status,
				item_code=item_code,
				family=probe.family,
				purity=probe.purity,
				colour=probe.colour,
				template=probe.template,
				target=probe.target,
				candidates=probe.candidates,
			)
		)

	rows.sort(key=lambda r: (r.status, r.item_code))
	shown = rows[: int(limit)] if limit else rows

	print()
	if not rows:
		print("No gaps: every source item in scope resolves to a usable target.")
	else:
		width = max(len(r.item_code) for r in shown) if shown else 12
		for row in shown:
			detail = row.target or (
				_format_item_list(row.candidates, limit=3) if row.candidates else "-"
			)
			print(
				f"  {row.status:18s} {row.item_code:{width}s} "
				f"purity={str(row.purity):8s} colour={str(row.colour):14s} -> {detail}"
			)
		if len(rows) > len(shown):
			print(
				f"  ... {len(rows) - len(shown)} more row(s) not shown (limit={limit})"
			)

	print()
	print(f"Summary ({len(rows)} row(s)):")
	for status, count in sorted(counts.items()):
		print(f"  {status:18s} {count}")

	if as_dict:
		return rows
