"""Revert "Nova Glow" → "Close" and "Nova Glow Setting" → "Close Setting" in-place.

This is the exact inverse of `v16_0_rename_close_setting_to_nova_glow` (v2), which is
being reverted in code. That patch renamed the Attribute Value masters, rewrote the
Item Attribute option lists, and rewrote every doctype column that stores the setting
string; this one puts all three back.

Hierarchy after this patch:
  Attribute Value "Close" (is_setting_type=1, abbreviation=NULL)
    └── Attribute Value "Close Setting" (is_sub_setting_type=1,
        parent_attribute_value="Close", abbreviation=NULL)

Abbreviations restored:
  * Item Attribute Value, parent "Setting Type":                "NG"  → "CL"
  * Item Attribute Value, parent "Sub Setting Type{,1,2}":      "NGS" → "CLS"
  * Attribute Value.abbreviation on the sub-setting master:     "NGS" → NULL

  The last one is deliberately NULL, not "CLS". The forward patch *introduced*
  `abbreviation="NGS"` on the master (its step 1c); it was not carrying a value before.
  Every other setting/sub-setting Attribute Value on this site has abbreviation NULL,
  so NULL is the pre-rename state. `CLS` lives on the Item Attribute Value rows, which
  this patch does restore. Flip RESTORE_SUB_ABBREVIATION below if the business wants
  "CLS" stamped on the master instead.

Column contract — every setting column gets BOTH exact replacements:
  * "Nova Glow"         → "Close"         (parent setting columns)
  * "Nova Glow Setting" → "Close Setting" (sub-setting columns)
Exact equality, never LIKE/REPLACE: that is what keeps the parent replacement from
mangling "Nova Glow Setting" into "Close Setting" a second time, and what keeps the
sibling "Close-Open Setting" (under "Open") untouched. Both replacements therefore run
on every column regardless of which family it belongs to, and the order is irrelevant.

DOCTYPE_COLUMNS is copied verbatim from the forward patch, so the revert covers exactly
the surface the rename touched — no more, no less.

Idempotency: guarded by a `tabDefaultValue` sentinel (`revert_nova_glow_to_close_v1`).
Re-running is a no-op. On a site that never ran the forward patch there is nothing named
"Nova Glow", so every statement matches zero rows and the patch is harmless.

Cleanup: this patch also clears the forward sentinel (`close_setting_to_nova_glow_v2`)
and the forward patch's `tabPatch Log` row, so the rename can be re-applied cleanly if
the business reverses the decision again.
"""

import frappe
from frappe.utils import cint

SENTINEL = "revert_nova_glow_to_close_v1"
FORWARD_SENTINEL = "close_setting_to_nova_glow_v2"
FORWARD_PATCH = "jewellery_erpnext.patches.v16_0_rename_close_setting_to_nova_glow"

OLD_SETTING_TYPE = "Nova Glow"
NEW_SETTING_TYPE = "Close"
OLD_SUB_SETTING = "Nova Glow Setting"
NEW_SUB_SETTING = "Close Setting"

SETTING_ABBR = "CL"
SUB_SETTING_ABBR = "CLS"

# Attribute Value.abbreviation on the sub-setting master. The forward patch set this to
# "NGS"; pre-rename it was unset, like every sibling sub-setting. See module docstring.
RESTORE_SUB_ABBREVIATION = None

# Verbatim from v16_0_rename_close_setting_to_nova_glow.DOCTYPE_COLUMNS.
DOCTYPE_COLUMNS = {
	# ---- Parent + sub setting tables ----
	"Item": ["setting_type", "sub_setting_type", "custom_old_sub_setting_type"],
	"BOM": ["setting_type", "sub_setting_type1", "sub_setting_type2"],
	"Order": ["setting_type", "sub_setting_type1", "sub_setting_type2"],
	"Sketch Order": ["setting_type", "sub_setting_type1", "sub_setting_type2"],
	"Repair Order": ["setting_type", "sub_setting_type1", "sub_setting_type2"],
	"Order Form Detail": ["setting_type", "sub_setting_type1", "sub_setting_type2"],
	"CAD Order Form Detail": ["setting_type", "sub_setting_type1", "sub_setting_type2"],
	"Sketch Order Form Detail": [
		"setting_type",
		"sub_setting_type1",
		"sub_setting_type2",
	],
	"Repair Order Form Detail": [
		"setting_type",
		"sub_setting_type1",
		"sub_setting_type2",
	],
	"Parent Manufacturing Order": ["setting_type", "sub_setting_type"],
	"Manufacturing Work Order": ["setting_type", "sub_setting_type"],
	"Customer Order Form": ["setting_type", "setting_type_2"],
	"Sketch Order Form Category": [
		"setting_type",
		"sub_setting_type",
		"sub_setting_type2",
	],
	"Pre Order Form Details": ["setting_type", "bom_setting_type", "item_setting_type"],
	# ---- Parent-only setting tables ----
	"Making Charge Price": ["setting_type"],
	"Manufacturing Plan": ["setting_type"],
	"Product Return Order": ["setting_type"],
	"Serial No and Design Code Order": ["setting_type"],
	"Titan Design Information Sheet": ["setting_type"],
	"Revise Making Charge Price": ["setting_type"],
	"Serial No and Design Code Order Form Detail": ["setting_type"],
	"Metal Ratio": ["setting_type"],
	"Product Return Form Item": ["setting_type"],
	"Tracking Bom": ["setting_type"],
	"Exploded Product Details": ["setting_type"],
	"Final Sketch Approval CMO": ["setting_type"],
	"Final Sketch Approval - Hold": ["setting_type"],
	"Final Sketch Approval CMO - Rejected": ["setting_type"],
	"Old Style Bio Data": ["setting_type"],
	"Titan Order Form Details": ["setting_type"],
	"Quotation Item": ["setting_type"],
	"Sales Order Item": ["setting_type"],
	"Customer Order Form Detail": ["setting_type"],
	"Metal Labour Price": ["setting_type"],
	"Order Target Detail": ["setting_type"],
	"Reliance Cost Sheet": ["setting_type"],
	"Sketch Order Form Setting Type": ["setting_type"],
	# ---- Sub-only setting tables ----
	"BOM Diamond Detail": ["sub_setting_type"],
	"BOM Gemstone Detail": ["sub_setting_type"],
	"Order BOM Diamond Detail": ["sub_setting_type"],
	"Order BOM Gemstone Detail": ["sub_setting_type"],
	"Customer Setting Detail": ["gk_setting_type", "gk_sub_setting_type"],
	"MWO MOP Balance Table": ["sub_setting_type"],
	"Stock Entry MOP Item": ["custom_sub_setting_type"],
	"SM Source Table": ["sub_setting_type"],
	"Manually Book Loss Details": ["sub_setting_type"],
	"SM Remain Balance Table": ["sub_setting_type"],
	"MOP Balance Table": ["sub_setting_type"],
	"SM Target Table": ["sub_setting_type"],
	"Employee Target Table": ["sub_setting_type"],
	"Employee Source Table": ["sub_setting_type"],
	"Department Target Table": ["sub_setting_type"],
	"Department Source Table": ["sub_setting_type"],
	"PMO Gemstone Table": ["sub_setting_type"],
	"SNC Source Table": ["sub_setting_type"],
	"SNC SFG Details": ["sub_setting_type"],
	"SNC FG Details": ["sub_setting_type"],
	"Employee Loss Details": ["sub_setting_type"],
	"Material Request Item": ["custom_sub_setting_type"],
	"Stock Entry Detail": ["custom_sub_setting_type"],
	"Product Return Order Diamond Detail": ["sub_setting_type"],
	"Product Return Order Gemstone Detail": ["sub_setting_type"],
}


def _sentinel_is_set() -> bool:
	row = frappe.db.sql(
		"select defvalue from tabDefaultValue where defkey = %s and parent = %s",
		(SENTINEL, "__default"),
	)
	return bool(row) and cint(row[0][0])


def _set_sentinel():
	frappe.db.sql(
		"""
        insert into tabDefaultValue (name, parent, parenttype, defkey, defvalue)
        values (%(name)s, '__default', '__default', %(key)s, '1')
        on duplicate key update defvalue = '1'
        """,
		{"name": f"__default-{SENTINEL}", "key": SENTINEL},
	)
	frappe.cache.delete_value("__default")


def _clear_forward_sentinel():
	"""Drop the rename patch's sentinel and Patch Log row so it can re-run if ever re-added."""
	frappe.db.sql(
		"delete from tabDefaultValue where defkey = %s and parent = %s",
		(FORWARD_SENTINEL, "__default"),
	)
	frappe.db.sql(
		"delete from `tabPatch Log` where patch like %s", (f"%{FORWARD_PATCH}%",)
	)
	frappe.cache.delete_value("__default")


def _preflight_check() -> bool:
	"""Return True if safe to proceed.

	Only blocks on a genuine collision: the renamed value still exists AND the original
	one is already back — renaming would create a duplicate Attribute Value (the doctype
	autonames on `field:attribute_value`). No sentinel is set on failure, so a real
	conflict keeps surfacing on every migrate until someone resolves it by hand.
	"""
	if frappe.db.exists(
		"Attribute Value", {"attribute_value": OLD_SETTING_TYPE}
	) and frappe.db.exists("Attribute Value", {"attribute_value": NEW_SETTING_TYPE}):
		frappe.logger().error(
			f"{SENTINEL}: both '{OLD_SETTING_TYPE}' and '{NEW_SETTING_TYPE}' exist"
		)
		return False
	if frappe.db.exists(
		"Attribute Value", {"attribute_value": OLD_SUB_SETTING}
	) and frappe.db.exists("Attribute Value", {"attribute_value": NEW_SUB_SETTING}):
		frappe.logger().error(
			f"{SENTINEL}: both '{OLD_SUB_SETTING}' and '{NEW_SUB_SETTING}' exist"
		)
		return False
	return True


def execute():
	if _sentinel_is_set():
		frappe.logger().info(f"{SENTINEL}: sentinel set, already applied — skipping")
		return

	if not _preflight_check():
		return

	_apply_revert()

	_clear_forward_sentinel()
	_set_sentinel()
	frappe.clear_cache(doctype="Attribute Value")
	for dt in DOCTYPE_COLUMNS:
		frappe.clear_cache(doctype=dt)

	frappe.logger().info(
		f"{SENTINEL}: reverted '{OLD_SETTING_TYPE}'→'{NEW_SETTING_TYPE}' and "
		f"'{OLD_SUB_SETTING}'→'{NEW_SUB_SETTING}' + migrated doctype records "
		f"across {len(DOCTYPE_COLUMNS)} doctypes"
	)


def _apply_revert():
	# ========== STEP 1: Rename Attribute Values (master data) ==========
	# Attribute Value autonames on field:attribute_value, so `name` moves with the value.

	# 1a. Parent setting_type "Nova Glow" → "Close"; clear the abbreviation the rename added.
	frappe.db.sql(
		"UPDATE `tabAttribute Value` SET `name` = %s, `attribute_value` = %s, "
		"`abbreviation` = NULL WHERE `attribute_value` = %s AND `is_setting_type` = 1",
		(NEW_SETTING_TYPE, NEW_SETTING_TYPE, OLD_SETTING_TYPE),
	)

	# 1b. Point the child back at the restored parent name.
	frappe.db.sql(
		"UPDATE `tabAttribute Value` SET `parent_attribute_value` = %s "
		"WHERE `attribute_value` = %s AND `is_sub_setting_type` = 1",
		(NEW_SETTING_TYPE, OLD_SUB_SETTING),
	)

	# 1c. Sub setting "Nova Glow Setting" → "Close Setting", abbreviation back to its
	#     pre-rename state (NULL by default — see RESTORE_SUB_ABBREVIATION).
	frappe.db.sql(
		"UPDATE `tabAttribute Value` SET `name` = %s, `attribute_value` = %s, "
		"`abbreviation` = %s WHERE `attribute_value` = %s AND `is_sub_setting_type` = 1",
		(NEW_SUB_SETTING, NEW_SUB_SETTING, RESTORE_SUB_ABBREVIATION, OLD_SUB_SETTING),
	)

	# ========== STEP 1b: Revert Item Attribute option lists ==========
	_revert_item_attribute_values()

	# ========== STEP 2: Revert doctype records (data) ==========
	for doctype, columns in DOCTYPE_COLUMNS.items():
		for col in columns:
			_apply_column_replacements(doctype, col)

	# Single commit at the end — no mid-patch commit, to avoid a partial-failure deadlock.
	frappe.db.commit()


def _apply_column_replacements(doctype, col):
	"""Run both exact replacements on a single column, if it exists on this site."""
	if not frappe.db.has_column(doctype, col):
		frappe.logger().debug(f"{SENTINEL}: skipping {doctype}.{col} (no such column)")
		return

	# Parent setting: "Nova Glow" → "Close"
	n = frappe.db.sql(
		f"UPDATE `tab{doctype}` SET `{col}` = %s WHERE `{col}` = %s",
		(NEW_SETTING_TYPE, OLD_SETTING_TYPE),
	)
	if n:
		frappe.logger().debug(
			f"{SENTINEL}: {doctype}.{col} 'Nova Glow'→'Close' ({n} rows)"
		)

	# Sub setting: "Nova Glow Setting" → "Close Setting"
	m = frappe.db.sql(
		f"UPDATE `tab{doctype}` SET `{col}` = %s WHERE `{col}` = %s",
		(NEW_SUB_SETTING, OLD_SUB_SETTING),
	)
	if m:
		frappe.logger().debug(
			f"{SENTINEL}: {doctype}.{col} 'Nova Glow Setting'→'Close Setting' ({m} rows)"
		)


def _revert_item_attribute_values():
	"""Revert Item Attribute option lists for all setting-related attributes."""
	attr_names = frappe.get_all(
		"Item Attribute",
		filters={
			"attribute_name": [
				"in",
				[
					"Setting Type",
					"Sub Setting Type",
					"Sub Setting Type1",
					"Sub Setting Type2",
				],
			]
		},
		pluck="name",
	)

	for attr_name in attr_names:
		if attr_name == "Setting Type":
			frappe.db.sql(
				"""
                UPDATE `tabItem Attribute Value`
                SET `attribute_value` = %s, `abbr` = %s
                WHERE `parent` = %s AND `attribute_value` = %s
                """,
				(NEW_SETTING_TYPE, SETTING_ABBR, attr_name, OLD_SETTING_TYPE),
			)
		else:
			frappe.db.sql(
				"""
                UPDATE `tabItem Attribute Value`
                SET `attribute_value` = %s, `abbr` = %s
                WHERE `parent` = %s AND `attribute_value` = %s
                """,
				(NEW_SUB_SETTING, SUB_SETTING_ABBR, attr_name, OLD_SUB_SETTING),
			)
