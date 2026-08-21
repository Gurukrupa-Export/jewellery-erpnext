"""Reposition custom fields on a form that carries a ``field_order`` Property Setter.

WHY THIS EXISTS
---------------
``Meta.sort_fields`` (frappe/model/meta.py) resolves field order by priority:

    1. the ``field_order`` Property Setter
    2. computed ``insert_after`` for standard fields
    3. the ``insert_after`` property of custom fields

Customize Form writes a DocType-level ``field_order`` Property Setter listing EVERY field
in its arranged order (``CustomizeForm.set_property_setter_for_field_order``). Once that
exists, any field named in it keeps its position there and rewriting
``Custom Field.insert_after`` alone changes NOTHING on screen — only custom fields ABSENT
from the list are placed by ``insert_after``.

Several doctypes in this app have been through Customize Form (Serial No, Sales Order,
Delivery Note, Sales Invoice all carry one), so a patch that moves fields must update BOTH:
the Property Setter (what a customized site renders by) and the ``insert_after`` chain
(what a fresh install or CI site renders by).
"""

import json

import frappe


def custom_field_name(doctype, fieldname):
	"""Name of the Custom Field row for ``doctype``.``fieldname``, or None if standard."""
	return frappe.db.get_value(
		"Custom Field", {"dt": doctype, "fieldname": fieldname}, "name"
	)


def rewrite_field_order(doctype, block):
	"""Splice ``block`` (an ordered list of fieldnames) into the ``field_order`` Property
	Setter, keeping every other field where it is.

	The block is placed at the position of its own earliest current member, so the
	surrounding form is untouched. Returns True when a Property Setter existed and was
	updated, False when there is none (in which case ``insert_after`` is already the
	authority).
	"""
	ps = frappe.db.get_value(
		"Property Setter",
		{"doc_type": doctype, "property": "field_order", "doctype_or_field": "DocType"},
		["name", "value"],
		as_dict=True,
	)
	if not ps:
		return False

	order = json.loads(ps.value)
	members = set(block)
	positions = [i for i, name in enumerate(order) if name in members]
	if not positions:
		return False

	# Where the block starts once its own members are pulled out of the list.
	insert_at = sum(1 for name in order[: min(positions)] if name not in members)
	remaining = [name for name in order if name not in members]
	new_order = remaining[:insert_at] + block + remaining[insert_at:]

	if new_order != order:
		frappe.db.set_value(
			"Property Setter",
			ps.name,
			"value",
			json.dumps(new_order),
			update_modified=False,
		)
	return True


def rewrite_insert_after_chain(doctype, block, anchor):
	"""Point each fieldname in ``block`` at the one before it, starting from ``anchor``.

	``db.set_value`` rather than ``Custom Field.save()``: this only re-anchors the chain and
	must not re-run Custom Field validation on fields the caller is not otherwise changing.
	Standard fields in ``block`` are skipped — they have no Custom Field row.
	"""
	for name in block:
		field = custom_field_name(doctype, name)
		if field:
			frappe.db.set_value("Custom Field", field, "insert_after", anchor)
		anchor = name


def drop_layout_fields(doctype, fieldnames):
	"""Delete layout-only Custom Fields (Section / Column Break) that are no longer used.

	Layout fieldtypes hold no data and get no database column, so removing them is purely
	a metadata change. Anything that is not a Section Break or Column Break is refused
	rather than silently deleted — this helper must never be able to drop a data field.

	Returns the list of fieldnames actually deleted.
	"""
	dropped = []
	for fieldname in fieldnames:
		field = frappe.db.get_value(
			"Custom Field",
			{"dt": doctype, "fieldname": fieldname},
			["name", "fieldtype"],
			as_dict=True,
		)
		if not field:
			continue
		if field.fieldtype not in ("Section Break", "Column Break"):
			frappe.logger().warning(
				f"drop_layout_fields: refusing to delete {doctype}.{fieldname} "
				f"(fieldtype {field.fieldtype} is not layout-only)"
			)
			continue
		frappe.delete_doc(
			"Custom Field", field.name, ignore_permissions=True, force=True
		)
		dropped.append(fieldname)
	return dropped


def strip_field_order_entries(doctype, fieldnames):
	"""Remove ``fieldnames`` from the ``field_order`` Property Setter, if one exists."""
	ps = frappe.db.get_value(
		"Property Setter",
		{"doc_type": doctype, "property": "field_order", "doctype_or_field": "DocType"},
		["name", "value"],
		as_dict=True,
	)
	if not ps:
		return

	order = json.loads(ps.value)
	pruned = [name for name in order if name not in set(fieldnames)]
	if pruned != order:
		frappe.db.set_value(
			"Property Setter",
			ps.name,
			"value",
			json.dumps(pruned),
			update_modified=False,
		)
