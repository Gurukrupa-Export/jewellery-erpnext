"""Bulk-prefetch helpers shared by the Material Request Stock Entry builders.

Follows the ``{key: value}`` map convention used elsewhere in the app
(``mop_eod_sync._preload_sre_warehouse_map``, ``manufacturing_plan.fetch_doc_map``). The
residue query is written out here rather than delegating to ``fetch_doc_map`` so that these
two hot paths do not have to import the Manufacturing Plan controller, which pulls in the
Purchase Order and Parent Manufacturing Order modules behind it.
"""

import frappe


def mri_warehouse_map(se_items, mr=None):
	"""Map ``Material Request Item.name -> warehouse`` for the rows of a Stock Entry.

	``mr``, when given, is a Material Request document that is already loaded. Its child rows
	carry both fields, so any name found there is resolved for free; only names it does not
	cover fall through to a single query. Passing it is what makes the common case cost zero
	round trips -- the Stock Entries built here are copies of an SE whose rows were stamped
	from that very document.

	The fallback query is kept for correctness rather than as an optimisation: it means a row
	pointing at some other Material Request still resolves exactly as it did when every row
	was read from the database.
	"""
	names = {row.material_request_item for row in se_items if row.material_request_item}
	if not names:
		return {}

	warehouse_map = {}
	if mr is not None:
		for item_row in mr.get("items") or []:
			if item_row.name in names:
				warehouse_map[item_row.name] = item_row.warehouse

	missing = names - set(warehouse_map)
	if missing:
		warehouse_map.update(
			{
				d.name: d.warehouse
				for d in frappe.get_all(
					"Material Request Item",
					filters={"name": ("in", sorted(missing))},
					fields=["name", "warehouse"],
				)
			}
		)

	return warehouse_map
