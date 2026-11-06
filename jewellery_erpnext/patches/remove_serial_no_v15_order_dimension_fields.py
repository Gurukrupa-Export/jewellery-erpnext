"""Retire the v15 ``Serial No.sales_type`` / ``order_type`` / ``flow_type`` Custom Fields.

The v15 chain patch ``add_chain_sales_order_fields`` created these three on Serial No. The v16
chain then added ``custom_sales_type`` / ``custom_order_type`` / ``custom_flow_type``
(``add_order_sales_flow_type_fields``), and ``create_manufacturing_entry`` stamps only those. Sites
that ran both show every label twice on the Serial No form, with one copy always empty. gepl-prod on
2026-11-06 had 916 serials stamped in the ``custom_*`` trio and 210 in the old pair, all of them
"Repair" from gke_customization's ``ProductReturnOrder.on_submit``. That writer now stamps the
``custom_*`` fields too.

For each (old, kept) pair, and only when both columns exist:

1. the old value is copied into the kept field where the kept field is blank. This is a single
   set-based UPDATE, so no Serial No is saved (the stock-managed-field guard never fires) and
   ``modified`` is left alone;
2. Custom Fields anchored on the old field are re-pointed to the old field's own anchor, and the
   old name is pruned from the ``field_order`` Property Setter (``on_trash`` only clears setters
   whose ``field_name`` is this field);
3. the Custom Field is deleted, as in ``remove_serial_no_ownership_tag_field``. The rows are
   Administrator-owned and ``on_trash`` refuses anyone else, which is why the desk cannot remove
   them; migrate and ``bench execute`` run as Administrator.

The columns stay on ``tabSerial No`` with their values. Dropping them is a separate, irreversible
decision, the same position the sibling patch takes.

Idempotent: once the rows are gone there is nothing to backfill and nothing to delete. Can also be
run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.remove_serial_no_v15_order_dimension_fields.execute
"""

import frappe

from jewellery_erpnext.patches.field_order_utils import strip_field_order_entries

DOCTYPE = "Serial No"

# old v15 fieldname -> the v16 field that replaces it
RETIRED_FIELDS = {
	"sales_type": "custom_sales_type",
	"order_type": "custom_order_type",
	"flow_type": "custom_flow_type",
}

LOG_PREFIX = "remove_serial_no_v15_order_dimension_fields"


def backfill(old, kept):
	"""Copy ``old`` into ``kept`` wherever ``kept`` is blank. Returns the number of rows copied."""
	where = f"IFNULL(`{kept}`, '') = '' AND IFNULL(`{old}`, '') != ''"
	count = frappe.db.sql(f"SELECT COUNT(*) FROM `tabSerial No` WHERE {where}")[0][0]
	if count:
		frappe.db.sql(f"UPDATE `tabSerial No` SET `{kept}` = `{old}` WHERE {where}")
	return count


def _reanchor_dependants(fieldname, anchor, retiring):
	"""Re-point Custom Fields anchored on ``fieldname`` to ``anchor``, skipping ``retiring``."""
	dependants = [
		name
		for name in frappe.get_all(
			"Custom Field",
			filters={"dt": DOCTYPE, "insert_after": fieldname},
			pluck="name",
		)
		if name not in retiring
	]
	for name in dependants:
		frappe.db.set_value("Custom Field", name, "insert_after", anchor)
	return dependants


def _surviving_anchor(fieldname, fields):
	"""The first anchor up the ``insert_after`` chain that is not itself being retired.

	The three were created anchored on each other (flow_type -> order_type -> sales_type ->
	custom_bom_no), so a dependant of flow_type must land on custom_bom_no, not on order_type.
	"""
	anchor = fields[fieldname].insert_after
	while anchor in fields:
		anchor = fields[anchor].insert_after
	return anchor


def execute():
	logger = frappe.logger("jewellery_erpnext")

	fields = {}
	for old, kept in RETIRED_FIELDS.items():
		field = frappe.db.get_value(
			"Custom Field",
			{"dt": DOCTYPE, "fieldname": old},
			["name", "insert_after"],
			as_dict=True,
		)
		if not field:
			continue
		if not (
			frappe.db.has_column(DOCTYPE, old) and frappe.db.has_column(DOCTYPE, kept)
		):
			logger.warning(
				f"{LOG_PREFIX}: kept {field.name}, {DOCTYPE}.{kept} has no column"
			)
			continue
		fields[old] = field

	retiring = {field.name for field in fields.values()}
	retired = []
	for old, field in fields.items():
		kept = RETIRED_FIELDS[old]
		copied = backfill(old, kept)
		dependants = _reanchor_dependants(old, _surviving_anchor(old, fields), retiring)
		frappe.delete_doc(
			"Custom Field", field.name, force=True, ignore_permissions=True
		)
		retired.append(old)

		logger.info(
			f"{LOG_PREFIX}: deleted {field.name}; copied {copied} value(s) into {kept}"
			+ (f"; re-anchored {', '.join(dependants)}" if dependants else "")
		)

	if retired:
		strip_field_order_entries(DOCTYPE, retired)
		frappe.clear_cache(doctype=DOCTYPE)
		print(f"{LOG_PREFIX}: removed {DOCTYPE} {', '.join(retired)}")
