"""Move variants left in their template's "- T" item group to their "- V" group.

Variants landed in "- T" mostly because ERPNext copies the template's item_group onto
every variant on each template save (757 of the 879 on the prod copy were "- V" first),
and some because they were created from the template in code. Item-Form-118 corrected
them only when someone opened them; variant_item_group now does it on every variant save,
so a moved variant stays moved. This moves the design variants only, with the same rule:

* material variants whose "- V" group is a batch group (Metal/Diamond/Gemstone/Finding/
  Other - V) are listed, not moved: those groups get a batch series and stock flags at
  insert, which a group change cannot add afterwards;
* variants with an attribute left empty (and not allowed to be zero) are listed, not
  moved: a new "- V" variant like that would be refused;
* everything is a dry run unless ``dry_run=0``.

Run as System Manager, dry run first:
    bench --site <site> execute jewellery_erpnext.variant_group_repair.repair
    bench --site <site> execute jewellery_erpnext.variant_group_repair.repair --kwargs "{'dry_run': 0}"
"""

from collections import defaultdict

import frappe
from frappe.model.document import clear_document_cache
from frappe.utils import cint

from jewellery_erpnext.jewellery_erpnext.doc_events.item import variant_item_group

BATCH_GROUPS = ("Metal - V", "Diamond - V", "Gemstone - V", "Finding - V", "Other - V")


def plan_moves():
	"""Classify every variant still in a "- T" group."""
	rows = frappe.get_all(
		"Item",
		filters={"variant_of": ["is", "set"], "item_group": ["like", "% - T"]},
		fields=["name", "variant_of", "item_group", "item_subcategory"],
		order_by="name",
	)
	plan = {"move": [], "batch_group": [], "empty_attribute": [], "kept": []}
	if not rows:
		return plan
	empty = _empty_attributes([row.name for row in rows])
	for row in rows:
		group = variant_item_group(row)
		entry = {"item": row.name, "from": row.item_group, "to": group}
		if not group:
			plan["kept"].append(entry)
		elif group in BATCH_GROUPS:
			plan["batch_group"].append(entry)
		elif missing := [
			attribute
			for attribute in empty.get(row.name, ())
			if not _zero_allowed(row.item_subcategory, attribute)
		]:
			plan["empty_attribute"].append({**entry, "attributes": missing})
		else:
			plan["move"].append(entry)
	return plan


def _empty_attributes(items):
	empty = defaultdict(list)
	for start in range(0, len(items), 500):
		for row in frappe.get_all(
			"Item Variant Attribute",
			filters={
				"parent": ["in", items[start : start + 500]],
				"parenttype": "Item",
			},
			fields=["parent", "attribute", "attribute_value"],
		):
			if not row.attribute_value:
				empty[row.parent].append(row.attribute)
	return empty


def _zero_allowed(subcategory, attribute):
	return bool(
		frappe.db.get_value(
			"Attribute Value Item Attribute Detail",
			{"parent": subcategory, "item_attribute": attribute},
			"allow_zero_values",
			cache=True,
		)
	)


@frappe.whitelist(methods=["POST"])
def repair(dry_run=1):
	"""Report (dry run) or move the design variants; returns counts and the lists."""
	frappe.only_for("System Manager")
	plan = plan_moves()
	if not cint(dry_run):
		for entry in plan["move"]:
			# No hooks, Version or modified change: only the group is corrected.
			frappe.db.set_value(
				"Item", entry["item"], "item_group", entry["to"], update_modified=False
			)
			clear_document_cache("Item", entry["item"])
	summary = {key: len(value) for key, value in plan.items()}
	summary["dry_run"] = bool(cint(dry_run))
	print(summary)
	return {"summary": summary, **plan}
