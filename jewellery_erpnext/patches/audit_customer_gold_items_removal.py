"""Report what retiring ``Subcontracting Settings.customer_gold_items`` means for this site.

The Item flag ``custom_inventory_type_can_be_customer_goods`` is now the only thing that makes
an item eligible for a Customer Gold receipt; the Settings table ("Additional Customer Gold
Items") and its implicit inclusion of ``customer_24kt_item`` are no longer consulted. This patch
compares the two so the difference is known before anyone meets it at a receipt.

It CHANGES NOTHING, deliberately. The business chose the Item master as the authority, so an old
whitelist entry must not silently switch a flag on, and an item missing from the old list must not
silently switch one off. Every mismatch is reported for a person to act on -- in particular the
configured Customer 24KT Item, which the old code always accepted whatever its flag said.

The rows themselves are left where they are. Removing the Table field only stops Frappe loading
them; ``tabCustomer Gold Item`` keeps them, and dropping that table is a separate, irreversible
decision. Every read here is a raw SELECT because the field has already left the Settings meta
by the time a post-model-sync patch runs, and the table may not exist at all (sites that never
migrated to the 2026-09-17 schema never had it).

Idempotent, and safe to run ad hoc before a deploy's migrate::

    bench --site <site> execute jewellery_erpnext.patches.audit_customer_gold_items_removal.execute
"""

import frappe

from jewellery_erpnext.customer_subcontracting.customer_goods_eligibility import (
	CUSTOMER_GOLD_TEMPLATES,
	CUSTOMER_GOODS_FLAG,
	CUSTOMER_GOODS_FLAG_LABEL,
)

SETTINGS_DOCTYPE = "Subcontracting Settings"
CHILD_DOCTYPE = "Customer Gold Item"
PARENTFIELD = "customer_gold_items"
ANCHOR_FIELD = "customer_24kt_item"

#: The stock UOM a gold/finding row must carry -- ``subcontracting_settings.RECEIPT_STOCK_UOM``,
#: restated so this module imports nothing that a later refactor could remove.
GOLD_STOCK_UOM = "Gram"

MATCH = "MATCH"
MISMATCH = "MISMATCH"
MISSING = "ITEM MISSING"


def execute():
	report = collect()
	for line in format_report(report):
		print(line)


def collect():
	"""The comparison as data. SELECT-only; nothing is written."""
	report = frappe._dict(
		anchor=None,
		table_exists=bool(frappe.db.table_exists(CHILD_DOCTYPE)),
		flag_exists=bool(frappe.db.has_column("Item", CUSTOMER_GOODS_FLAG)),
		rows=[],
		new_eligible=[],
	)

	anchor = frappe.db.sql(
		"SELECT `value` FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s",
		(SETTINGS_DOCTYPE, ANCHOR_FIELD),
	)
	report.anchor = anchor[0][0] if anchor and anchor[0][0] else None

	old = []
	if report.anchor:
		old.append((report.anchor, "Customer 24KT Item"))
	if report.table_exists:
		for (item,) in frappe.db.sql(
			f"SELECT `item` FROM `tab{CHILD_DOCTYPE}` "
			"WHERE `parent` = %s AND `parentfield` = %s ORDER BY `idx`",
			(SETTINGS_DOCTYPE, PARENTFIELD),
		):
			if item and item not in {code for code, _role in old}:
				old.append((item, "Additional Customer Gold Item"))

	if not report.flag_exists:
		# Nothing to compare against: the Item field is not on this site.
		report.rows = [
			frappe._dict(item=code, role=role, flag=None, result=MISMATCH)
			for code, role in old
		]
		return report

	flags = {}
	if old:
		flags = dict(
			frappe.db.sql(
				f"SELECT `name`, `{CUSTOMER_GOODS_FLAG}` FROM `tabItem` WHERE `name` IN %s",
				([code for code, _role in old],),
			)
		)
	for code, role in old:
		flag = flags.get(code)
		result = MISSING if code not in flags else (MATCH if flag else MISMATCH)
		report.rows.append(frappe._dict(item=code, role=role, flag=flag, result=result))

	report.new_eligible = _new_eligible([code for code, _role in old])
	return report


def _new_eligible(old_codes):
	"""Flagged items the old list never named, counted by group and by receipt readiness.

	"Passes" means the item would clear the receipt's own gates today: enabled, a stock item,
	batch controlled, and -- for gold/finding only -- stocked in grams. Stones take any UOM.
	"""
	gold_templates = ", ".join(frappe.db.escape(t) for t in CUSTOMER_GOLD_TEMPLATES)
	exclude = ""
	values = []
	if old_codes:
		exclude = "AND `name` NOT IN %s"
		values.append(old_codes)

	return frappe.db.sql(
		f"""
		SELECT
			`item_group`,
			IFNULL(`variant_of`, '') IN ({gold_templates}) AS `is_gold`,
			(
				`disabled` = 0 AND `is_stock_item` = 1 AND `has_batch_no` = 1
				AND (IFNULL(`variant_of`, '') NOT IN ({gold_templates}) OR `stock_uom` = %s)
			) AS `passes_gates`,
			COUNT(*) AS `items`
		FROM `tabItem`
		WHERE `{CUSTOMER_GOODS_FLAG}` = 1 {exclude}
		GROUP BY `item_group`, `is_gold`, `passes_gates`
		ORDER BY `passes_gates` DESC, `items` DESC
		""",
		tuple([GOLD_STOCK_UOM, *values]),
		as_dict=True,
	)


def format_report(report):
	lines = [
		"customer_gold_items retirement audit -- Item flag is now the only eligibility source",
	]
	if not report.table_exists:
		lines.append(
			f"  {CHILD_DOCTYPE} table: not present on this site (never configured)"
		)
	if not report.flag_exists:
		lines.append(
			f"  WARNING: Item.{CUSTOMER_GOODS_FLAG} does not exist on this site"
		)

	lines.append(f"  {'OLD SETTING':<34} {'ITEM FLAG':<10} RESULT")
	if not report.rows:
		lines.append(
			"  (no Customer 24KT Item and no Additional Customer Gold Items configured)"
		)
	for row in report.rows:
		flag = "-" if row.flag is None else str(int(row.flag))
		lines.append(f"  {row.item:<34} {flag:<10} {row.result}   [{row.role}]")

	if any(row.result == MISMATCH for row in report.rows):
		lines.append(
			"  ACTION: MISMATCH items were accepted by the old Settings list but are NOT enabled "
			"on the Item. New Customer Gold receipts of them are blocked until "
			f"'{CUSTOMER_GOODS_FLAG_LABEL}' is ticked on the Item."
		)
	if any(row.result == MISSING for row in report.rows):
		lines.append(
			"  NOTE: ITEM MISSING rows name an item that no longer exists; the old Settings "
			"entry pointed at nothing and there is nothing to enable."
		)

	passing = sum(r["items"] for r in report.new_eligible if r["passes_gates"])
	blocked = sum(r["items"] for r in report.new_eligible if not r["passes_gates"])
	lines.append(
		f"  NEW ITEM-MASTER ELIGIBLE (flagged, never on the old list): {passing} pass every "
		f"receipt gate, {blocked} are flagged but blocked by a gate"
	)
	for r in report.new_eligible:
		kind = "gold/finding" if r["is_gold"] else "other"
		state = "passes gates" if r["passes_gates"] else "blocked by a gate"
		lines.append(
			f"    {r['item_group'] or '-':<30} {kind:<13} {state:<18} {r['items']}"
		)
	return lines
