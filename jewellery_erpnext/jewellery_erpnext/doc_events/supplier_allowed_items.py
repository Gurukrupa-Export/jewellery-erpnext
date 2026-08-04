"""Per-supplier Metal whitelist — ``Supplier.custom_allowed_item_group``.

Suppliers are not interchangeable for metal: a bullion supplier or refiner deals in a
specific set of alloys, and buying the wrong alloy from the wrong party is expensive to
unwind once it has reached batches and the metal ledger. So each Supplier carries a table
of the Metal items it is allowed to supply.

**The restriction is scoped to Metal and nothing else.** An item is in scope only when its
Item Group is ``Metal - T`` / ``Metal - V`` or a descendant of one of those (resolved
through the Item Group nested set, so new sub-groups are covered without a code change).
Diamond, Gemstone, Finding, Design, Consumable — and the deprecated ``Metal DNU`` — are
never touched: they continue to display and transact exactly as before.

Semantics: strict whitelist. An empty table means the supplier may not be used with **any**
Metal item; there is no implicit allow.

Two layers, the house idiom for restrictions here (cf. ``tree_number.tree_metal_item_query``
vs ``tree_material_balance.validate_item_matches_tree_metal``):

- ``supplier_item_query`` — convenience only. Keeps disallowed metal items out of the item
  dropdown on the buying forms. Not load-bearing.
- ``validate`` — the real gate, wired on Purchase Order / Purchase Receipt /
  Purchase Invoice / Supplier Quotation. Catches what the dropdown structurally cannot:
  API pushes, data import, and the supplier being changed *after* items were picked.

Enforcement is unconditional — there is no config switch. A supplier whose table is empty
cannot be used with any metal item, so the supplier masters must be populated before this
reaches a live site.
"""

import frappe
from frappe import _
from frappe.utils.nestedset import get_descendants_of

#: Roots of the Metal taxonomy. Descendants are resolved from the nested set at runtime.
#: ``Metal DNU`` is deliberately absent — it is the deprecated group and out of scope.
METAL_ROOT_ITEM_GROUPS = ("Metal - T", "Metal - V")

SUPPLIER_TABLE_FIELD = "custom_allowed_item_group"


def get_metal_item_groups() -> frozenset:
	"""Every Item Group in Metal scope: the roots plus all their descendants.

	Roots missing from the site are skipped rather than raised on — a site that has not
	been seeded with the metal taxonomy simply has nothing to restrict.
	"""
	groups = set()
	for root in METAL_ROOT_ITEM_GROUPS:
		if not frappe.db.exists("Item Group", root):
			continue
		groups.add(root)
		groups.update(
			get_descendants_of("Item Group", root, ignore_permissions=True) or []
		)
	return frozenset(groups)


def get_allowed_metal_items(supplier) -> set:
	"""Item codes whitelisted on this supplier. Empty set means: no metal allowed."""
	if not supplier:
		return set()

	return set(
		frappe.get_all(
			"Supplier Allowed Item Group",
			filters={"parent": supplier, "parenttype": "Supplier"},
			pluck="item_code",
		)
	)


def get_blocked_metal_items(supplier) -> list:
	"""Metal items this supplier may NOT supply — the complement of the whitelist.

	Used to build the link-field filter. The metal universe is small (~40 items), so an
	explicit ``not in`` list is cheaper and clearer than a correlated subquery.
	"""
	metal_groups = get_metal_item_groups()
	if not metal_groups:
		return []

	metal_items = frappe.get_all(
		"Item", filters={"item_group": ["in", list(metal_groups)]}, pluck="name"
	)
	return sorted(set(metal_items) - get_allowed_metal_items(supplier))


def validate(doc, method=None):
	"""Block Metal items that are not whitelisted on the document's supplier.

	Wired on Purchase Order / Purchase Receipt / Purchase Invoice / Supplier Quotation.
	Non-metal rows are ignored entirely.
	"""
	supplier = doc.get("supplier")
	items = doc.get("items")
	if not supplier or not items:
		return

	metal_groups = get_metal_item_groups()
	if not metal_groups:
		return

	allowed = get_allowed_metal_items(supplier)

	offending = []
	for row in items:
		item_code = row.get("item_code")
		if not item_code:
			continue

		item_group = frappe.get_cached_value("Item", item_code, "item_group")
		if item_group not in metal_groups:
			# Not metal -- unrestricted, this feature does not apply.
			continue

		if item_code in allowed:
			continue

		offending.append((row.idx, item_code, item_group))

	if offending:
		_reject(supplier, offending)


def _reject(supplier, offending):
	"""Raise on a whitelist breach, naming every offending row in one message."""
	rows = "<br>".join(
		_("Row #{0}: Item {1} ({2})").format(idx, frappe.bold(item_code), item_group)
		for idx, item_code, item_group in offending
	)

	frappe.throw(
		_(
			"{0}<br><br>The item(s) above are not in the Allowed Item Group list for supplier {1}."
			"<br>Add them under Supplier &rarr; Allowed Item Group, or pick an allowed metal item."
			"<br><br>This restriction applies to Metal item groups only; all other item groups are unrestricted."
		).format(rows, frappe.bold(supplier)),
		title=_("Metal Item Not Allowed for Supplier"),
	)


def validate_supplier_rows(doc, method=None):
	"""Keep the Supplier's own table coherent: metal groups only, matching items, no dupes."""
	rows = doc.get(SUPPLIER_TABLE_FIELD)
	if not rows:
		return

	metal_groups = get_metal_item_groups()
	seen = {}

	for row in rows:
		if metal_groups and row.item_group not in metal_groups:
			frappe.throw(
				_(
					"Row #{0}: Item Group {1} is not a Metal item group. This table restricts "
					"Metal only — allowed groups are {2} and their sub-groups."
				).format(
					row.idx,
					frappe.bold(row.item_group),
					", ".join(frappe.bold(g) for g in METAL_ROOT_ITEM_GROUPS),
				),
				title=_("Metal Item Groups Only"),
			)

		actual_group = frappe.get_cached_value("Item", row.item_code, "item_group")
		if actual_group and actual_group != row.item_group:
			frappe.throw(
				_("Row #{0}: Item {1} belongs to Item Group {2}, not {3}.").format(
					row.idx,
					frappe.bold(row.item_code),
					frappe.bold(actual_group),
					frappe.bold(row.item_group),
				),
				title=_("Item Group Mismatch"),
			)

		if row.item_code in seen:
			frappe.throw(
				_("Row #{0}: Item {1} is already listed in row #{2}.").format(
					row.idx, frappe.bold(row.item_code), seen[row.item_code]
				),
				title=_("Duplicate Item"),
			)
		seen[row.item_code] = row.idx


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def supplier_item_query(doctype, txt, searchfield, start, page_len, filters):
	"""Item link query that hides metal items the supplier is not allowed to supply.

	Convenience only -- the load-bearing check is ``validate`` above. Delegates to ERPNext's
	``item_query`` so upstream behaviour (search fields, Party Specific Item, description
	matching) is preserved; we only add a ``name not in [...]`` exclusion.
	"""
	from erpnext.controllers.queries import item_query

	filters = dict(filters or {})
	supplier = filters.get("supplier")

	if supplier:
		blocked = get_blocked_metal_items(supplier)
		if blocked:
			filters["name"] = ["not in", blocked]

	return item_query(doctype, txt, searchfield, start, page_len, filters)


@frappe.whitelist()
def get_metal_item_group_names() -> list:
	"""Metal item groups, for the Supplier form's ``item_group`` link filter."""
	return sorted(get_metal_item_groups())
