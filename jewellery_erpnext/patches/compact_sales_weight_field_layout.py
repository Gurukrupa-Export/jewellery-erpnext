"""Lay the eight sales weight fields out as an even 4-column grid on Sales Order /
Delivery Note / Sales Invoice, with Gross Weight leading.

THE PROBLEM
-----------
``custom_metal_weight``, ``custom_finding_weight``, ``custom_diamond_weight``,
``custom_gemstone_weight``, ``custom_other_weight``, ``custom_gross_weight``,
``custom_diamond_pcs`` and ``custom_gemstone_pcs`` were arranged through Customize Form,
which left them in a ragged five-column grid — two field rows with empty cells, plus a
stray EMPTY leading column break on Sales Order (``custom_column_break_j8wfx``) — and a lot
of vertical whitespace::

    Row 1:  Metal | Finding | Diamond Wt | Gemstone Wt | Other Wt
    Row 2:  Gross | Dia Pcs | Gem Pcs    |             |

THE FIX
-------
Four columns of two fields each: no empty cells, every field a quarter of the width (wide
enough that labels like "Gemstone Weight" stay on one line), and Gross Weight first::

    Row 1:  Gross Weight    | Metal Weight | Finding Weight | Diamond Weight
    Row 2:  Gemstone Weight | Other Weight | Diamond Pcs    | Gemstone Pcs

Eight fields across a single row was tried first and came out congested — at an eighth of
the width every label wrapped to two lines — so the grid is deliberately 4 x 2.

WHY THIS TOUCHES THE ``field_order`` PROPERTY SETTER
----------------------------------------------------
All three parents carry a Customize Form ``field_order`` Property Setter, which OUTRANKS
``insert_after``. Rewriting ``Custom Field.insert_after`` alone changes nothing on screen.
See ``field_order_utils`` for the full explanation; this patch writes both, so a customized
site and a fresh install land on the same layout.

COLUMN BREAKS
-------------
Three separators are needed for four columns. Existing ones in the block are reused in a
stable fieldname-sorted order, the shortfall is created as ``custom_weight_col_1`` ..
``custom_weight_col_3``, and any surplus is DELETED — surplus column breaks would otherwise
render as trailing empty columns, which is the very problem this patch exists to remove.
Only Section Break / Column Break fields are ever deleted (``drop_layout_fields`` refuses
anything else); layout fieldtypes hold no data and get no database column, so this is a
metadata-only change.

Discovery is dynamic — the section break and the existing column breaks are located by
walking the resolved meta rather than by hardcoding the random Customize Form suffixes.

Idempotent: re-running recomputes the same block and rewrites the same values.

Can be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.compact_sales_weight_field_layout.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import (
	custom_field_name,
	drop_layout_fields,
	rewrite_field_order,
	rewrite_insert_after_chain,
	strip_field_order_entries,
)

PARENT_DOCTYPES = ("Sales Order", "Delivery Note", "Sales Invoice")

# Reading order, left to right then down: Gross leads, then the remaining weights, then the
# two piece counts. Laid into COLUMN_COUNT columns below.
WEIGHT_FIELD_ORDER = (
	"custom_gross_weight",
	"custom_metal_weight",
	"custom_finding_weight",
	"custom_diamond_weight",
	"custom_gemstone_weight",
	"custom_other_weight",
	"custom_diamond_pcs",
	"custom_gemstone_pcs",
)

# Four columns of two fields: wide enough that no label wraps.
COLUMN_COUNT = 4

# Four columns need three separators.
COLUMN_BREAKS = tuple(f"custom_weight_col_{i}" for i in range(1, COLUMN_COUNT))


def _locate_block(doctype):
	"""Return ``(section_fieldname, block_members)`` for the weight block.

	``section_fieldname`` is the Section Break the block lives under — found by walking
	back from the first weight field, not hardcoded, since Customize Form generated its
	name. ``block_members`` is every fieldname between that section and the last weight
	field inclusive, i.e. the slice this patch rearranges.

	Returns ``(None, [])`` when the parent has no weight fields at all.
	"""
	fields = frappe.get_meta(doctype).fields

	positions = [i for i, df in enumerate(fields) if df.fieldname in WEIGHT_FIELD_ORDER]
	if not positions:
		return None, []

	first_weight, last_weight = min(positions), max(positions)

	section_index = None
	for i in range(first_weight - 1, -1, -1):
		if fields[i].fieldtype == "Section Break":
			section_index = i
			break
	if section_index is None:
		return None, []

	members = [df.fieldname for df in fields[section_index : last_weight + 1]]
	return fields[section_index].fieldname, members


def _existing_separators(doctype, members):
	"""Column Break Custom Fields inside ``members``, sorted for a stable reuse order."""
	meta = frappe.get_meta(doctype)
	return sorted(
		name
		for name in members
		if meta.get_field(name)
		and meta.get_field(name).fieldtype == "Column Break"
		and custom_field_name(doctype, name)
	)


def _build_columns(present):
	"""Split ``present`` into COLUMN_COUNT columns, filled so the form READS row-major.

	With eight fields and four columns, column *i* holds fields *i* and *i + 4*, which
	renders as two rows in the declared order.
	"""
	return [list(present[i::COLUMN_COUNT]) for i in range(COLUMN_COUNT)]


def _compact_one(doctype):
	"""Rearrange one parent's weight block into the 4-column grid. True when applied."""
	section_fieldname, members = _locate_block(doctype)
	if not section_fieldname:
		frappe.logger().info(
			f"compact_sales_weight_field_layout: no weight block on {doctype}, skipped"
		)
		return False

	present = [f for f in WEIGHT_FIELD_ORDER if custom_field_name(doctype, f)]
	if not present:
		return False

	columns = [column for column in _build_columns(present) if column]
	breaks_needed = len(columns) - 1

	existing = _existing_separators(doctype, members)
	separators = list(existing[:breaks_needed])
	shortfall = [name for name in COLUMN_BREAKS if name not in separators][
		: breaks_needed - len(separators)
	]

	if shortfall:
		# Anchor the new breaks on an existing weight field so create_custom_fields'
		# insert_after validation passes; the real chain is written below.
		create_custom_fields(
			{
				doctype: [
					{
						"fieldname": name,
						"fieldtype": "Column Break",
						"insert_after": present[0],
						"module": "Jewellery Erpnext",
					}
					for name in shortfall
				]
			},
			ignore_validate=True,
		)
	separators.extend(shortfall)

	# section, col1 fields, break, col2 fields, break, ...
	block = [section_fieldname]
	for i, column in enumerate(columns):
		if i:
			block.append(separators[i - 1])
		block.extend(column)

	# Surplus column breaks would render as trailing empty columns — exactly the ragged
	# look this patch removes — so they go. drop_layout_fields refuses anything that is
	# not a Section/Column Break, so no data field can be caught by this.
	surplus = [name for name in members if name not in block]
	if surplus:
		dropped = drop_layout_fields(doctype, surplus)
		strip_field_order_entries(doctype, dropped)
		kept = [name for name in surplus if name not in dropped]
		if kept:
			# Not layout-only: leave it in place at the end of the block rather than
			# silently losing it from the form.
			block.extend(kept)
			frappe.logger().warning(
				f"compact_sales_weight_field_layout: {doctype} had non-layout field(s) "
				f"{kept} inside the weight block — parked at the end of the block."
			)

	had_property_setter = rewrite_field_order(doctype, block)
	rewrite_insert_after_chain(doctype, block[1:], section_fieldname)

	frappe.clear_cache(doctype=doctype)
	frappe.logger().info(
		f"compact_sales_weight_field_layout: {doctype} -> {len(columns)} columns x "
		f"{max(len(c) for c in columns)} rows ({len(present)} fields, "
		f"{len(shortfall)} breaks created, {len(surplus)} surplus removed, "
		f"field_order property setter={'updated' if had_property_setter else 'absent'})"
	)
	return True


def execute():
	for doctype in PARENT_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		_compact_one(doctype)

	frappe.db.commit()
