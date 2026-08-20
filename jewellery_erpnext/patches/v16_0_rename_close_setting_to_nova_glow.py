"""Rename "Close" → "Nova Glow" and "Close Setting" → "Nova Glow Setting" in-place.

This renames existing Attribute Values AND updates all doctype records that reference
the old string values. The doctype columns store strings directly, not FKs.

Current hierarchy:
  Attribute Value "Close" (is_setting_type=1)
    └── Attribute Value "Close Setting" (is_sub_setting_type=1, parent_attribute_value="Close")

After rename:
  Attribute Value "Nova Glow" (is_setting_type=1)
    └── Attribute Value "Nova Glow Setting" (is_sub_setting_type=1, parent_attribute_value="Nova Glow", abbreviation="NGS")

Plus: All doctype records with setting_type="Close" → "Nova Glow"
      All doctype records with sub_setting_type*="Close Setting" → "Nova Glow Setting"

Idempotency: guarded by a `tabDefaultValue` sentinel (`close_setting_to_nova_glow_v1`).
Re-running is a no-op. Pre-flight checks that new values don't already exist.

ROLLBACK (code revert plus):
    UPDATE `tabAttribute Value` SET `name` = 'Close', `attribute_value` = 'Close', `abbreviation` = NULL
    WHERE `attribute_value` = 'Nova Glow';
    UPDATE `tabAttribute Value` SET `name` = 'Close Setting', `attribute_value` = 'Close Setting', `parent_attribute_value` = 'Close'
    WHERE `attribute_value` = 'Nova Glow Setting';
    UPDATE `tabItem` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabItem` SET `sub_setting_type` = 'Close Setting' WHERE `sub_setting_type` = 'Nova Glow Setting';
    UPDATE `tabBOM` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabBOM` SET `sub_setting_type1` = 'Close Setting' WHERE `sub_setting_type1` = 'Nova Glow Setting';
    UPDATE `tabBOM` SET `sub_setting_type2` = 'Close Setting' WHERE `sub_setting_type2` = 'Nova Glow Setting';
    UPDATE `tabOrder` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabOrder` SET `sub_setting_type1` = 'Close Setting' WHERE `sub_setting_type1` = 'Nova Glow Setting';
    UPDATE `tabOrder` SET `sub_setting_type2` = 'Close Setting' WHERE `sub_setting_type2` = 'Nova Glow Setting';
    UPDATE `tabSketch Order` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabSketch Order` SET `sub_setting_type1` = 'Close Setting' WHERE `sub_setting_type1` = 'Nova Glow Setting';
    UPDATE `tabSketch Order` SET `sub_setting_type2` = 'Close Setting' WHERE `sub_setting_type2` = 'Nova Glow Setting';
    UPDATE `tabRepair Order` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabRepair Order` SET `sub_setting_type1` = 'Close Setting' WHERE `sub_setting_type1` = 'Nova Glow Setting';
    UPDATE `tabRepair Order` SET `sub_setting_type2` = 'Close Setting' WHERE `sub_setting_type2` = 'Nova Glow Setting';
    UPDATE `tabOrder Form Detail` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabOrder Form Detail` SET `sub_setting_type1` = 'Close Setting' WHERE `sub_setting_type1` = 'Nova Glow Setting';
    UPDATE `tabOrder Form Detail` SET `sub_setting_type2` = 'Close Setting' WHERE `sub_setting_type2` = 'Nova Glow Setting';
    UPDATE `tabSketch Order Form Detail` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabSketch Order Form Detail` SET `sub_setting_type1` = 'Close Setting' WHERE `sub_setting_type1` = 'Nova Glow Setting';
    UPDATE `tabSketch Order Form Detail` SET `sub_setting_type2` = 'Close Setting' WHERE `sub_setting_type2` = 'Nova Glow Setting';
    UPDATE `tabRepair Order Form Detail` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabRepair Order Form Detail` SET `sub_setting_type1` = 'Close Setting' WHERE `sub_setting_type1` = 'Nova Glow Setting';
    UPDATE `tabRepair Order Form Detail` SET `sub_setting_type2` = 'Close Setting' WHERE `sub_setting_type2` = 'Nova Glow Setting';
    UPDATE `tabParent Manufacturing Order` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabParent Manufacturing Order` SET `sub_setting_type` = 'Close Setting' WHERE `sub_setting_type` = 'Nova Glow Setting';
    UPDATE `tabManufacturing Work Order` SET `setting_type` = 'Close' WHERE `setting_type` = 'Nova Glow';
    UPDATE `tabManufacturing Work Order` SET `sub_setting_type` = 'Close Setting' WHERE `sub_setting_type` = 'Nova Glow Setting';
    (and all other doctypes in DOCTYPES_WITH_BOTH and DOCTYPES_SUB_ONLY — all setting_type and sub_setting_type* columns independently)
    then `delete from tabDefaultValue where defkey = 'close_setting_to_nova_glow_v1'`
    and `frappe.clear_cache()`
"""

import frappe
from frappe.utils import cint

SENTINEL = "close_setting_to_nova_glow_v1"

OLD_SETTING_TYPE = "Close"
NEW_SETTING_TYPE = "Nova Glow"
OLD_SUB_SETTING = "Close Setting"
NEW_SUB_SETTING = "Nova Glow Setting"
NEW_SUB_ABBR = "NGS"

# Doctypes that have BOTH setting_type AND sub_setting_type fields
DOCTYPES_WITH_BOTH = [
	("Item", ["sub_setting_type"]),
	("BOM", ["sub_setting_type1", "sub_setting_type2"]),
	("Order", ["sub_setting_type1", "sub_setting_type2"]),
	("Sketch Order", ["sub_setting_type1", "sub_setting_type2"]),
	("Repair Order", ["sub_setting_type1", "sub_setting_type2"]),
	("Order Form Detail", ["sub_setting_type1", "sub_setting_type2"]),
	("CAD Order Form Detail", ["sub_setting_type1", "sub_setting_type2"]),
	("Sketch Order Form Detail", ["sub_setting_type1", "sub_setting_type2"]),
	("Repair Order Form Detail", ["sub_setting_type1", "sub_setting_type2"]),
	("Parent Manufacturing Order", ["sub_setting_type"]),
	("Manufacturing Work Order", ["sub_setting_type"]),
	# Additional doctypes with setting_type (Link to Attribute Value)
	("Making Charge Price", ["sub_setting_type"]),
	("Manufacturing Plan", ["sub_setting_type"]),
	("Customer Order Form", ["sub_setting_type"]),
	("Product Return Order", ["sub_setting_type"]),
	("Serial No and Design Code Order", ["sub_setting_type"]),
	("Titan Design Information Sheet", ["sub_setting_type"]),
	("Revise Making Charge Price", ["sub_setting_type"]),
	("Serial No and Design Code Order Form Detail", ["sub_setting_type"]),
	("Metal Ratio", ["sub_setting_type"]),
	("Product Return Form Item", ["sub_setting_type"]),
	("Tracking Bom", ["sub_setting_type"]),
	("Exploded Product Details", ["sub_setting_type"]),
	("Final Sketch Approval CMO", ["sub_setting_type"]),
	("Final Sketch Approval - Hold", ["sub_setting_type"]),
	("Final Sketch Approval CMO - Rejected", ["sub_setting_type"]),
	("Old Style Bio Data", ["sub_setting_type"]),
	("Titan Order Form Details", ["sub_setting_type"]),
]

# Doctypes that ONLY have sub_setting_type fields (no setting_type)
DOCTYPES_SUB_ONLY = [
	("BOM Diamond Detail", ["sub_setting_type"]),
	("BOM Gemstone Detail", ["sub_setting_type"]),
	("Order BOM Diamond Detail", ["sub_setting_type"]),
	("Order BOM Gemstone Detail", ["sub_setting_type"]),
	("Customer Setting Detail", ["gk_sub_setting_type"]),
	("MWO MOP Balance Table", ["sub_setting_type"]),
	("Stock Entry MOP Item", ["custom_sub_setting_type"]),
	("SM Source Table", ["sub_setting_type"]),
	("Manually Book Loss Details", ["sub_setting_type"]),
	("SM Remain Balance Table", ["sub_setting_type"]),
	("MOP Balance Table", ["sub_setting_type"]),
	("SM Target Table", ["sub_setting_type"]),
	("Employee Target Table", ["sub_setting_type"]),
	("Employee Source Table", ["sub_setting_type"]),
	("Department Target Table", ["sub_setting_type"]),
	("Department Source Table", ["sub_setting_type"]),
	("PMO Gemstone Table", ["sub_setting_type"]),
	("SNC Source Table", ["sub_setting_type"]),
	("SNC SFG Details", ["sub_setting_type"]),
	("SNC FG Details", ["sub_setting_type"]),
	("Employee Loss Details", ["sub_setting_type"]),
	("Material Request Item", ["custom_sub_setting_type"]),
	("Stock Entry Detail", ["custom_sub_setting_type"]),
	("Sketch Order Form Category", ["sub_setting_type", "sub_setting_type2"]),
	("Product Return Order Diamond Detail", ["sub_setting_type"]),
	("Product Return Order Gemstone Detail", ["sub_setting_type"]),
]


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


def _preflight_check() -> bool:
	"""Return True if safe to proceed."""
	# Check new values don't already exist
	if frappe.db.exists("Attribute Value", {"attribute_value": NEW_SETTING_TYPE}):
		frappe.logger().error(f"{SENTINEL}: '{NEW_SETTING_TYPE}' already exists")
		_set_sentinel()
		return False
	if frappe.db.exists("Attribute Value", {"attribute_value": NEW_SUB_SETTING}):
		frappe.logger().error(f"{SENTINEL}: '{NEW_SUB_SETTING}' already exists")
		_set_sentinel()
		return False
	return True


def execute():
	if _sentinel_is_set():
		frappe.logger().info(f"{SENTINEL}: sentinel set, already applied — skipping")
		return

	if not _preflight_check():
		return

	_apply_rename_and_migrate()

	_set_sentinel()
	frappe.clear_cache(doctype="Attribute Value")
	# Clear cache for all affected doctypes
	for dt, _ in DOCTYPES_WITH_BOTH:
		frappe.clear_cache(doctype=dt)
	for dt, _ in DOCTYPES_SUB_ONLY:
		frappe.clear_cache(doctype=dt)

	frappe.logger().info(
		f"{SENTINEL}: renamed '{OLD_SETTING_TYPE}'→'{NEW_SETTING_TYPE}' and '{OLD_SUB_SETTING}'→'{NEW_SUB_SETTING}' + migrated doctype records"
	)


def _apply_rename_and_migrate():
	# ========== STEP 1: Rename Attribute Values (master data) ==========
	# NOTE: Attribute Value uses autoname: field:attribute_value, so we must update BOTH name and attribute_value

	# 1a. Rename parent setting_type "Close" → "Nova Glow" (update both name and attribute_value)
	frappe.db.sql(
		"UPDATE `tabAttribute Value` SET `name` = %s, `attribute_value` = %s WHERE `attribute_value` = %s AND `is_setting_type` = 1",
		(NEW_SETTING_TYPE, NEW_SETTING_TYPE, OLD_SETTING_TYPE),
	)

	# 1b. Update child's parent_attribute_value to point to new parent name
	frappe.db.sql(
		"UPDATE `tabAttribute Value` SET `parent_attribute_value` = %s WHERE `attribute_value` = %s AND `is_sub_setting_type` = 1",
		(NEW_SETTING_TYPE, OLD_SUB_SETTING),
	)

	# 1c. Rename sub_setting_type "Close Setting" → "Nova Glow Setting" and set abbreviation (update both name and attribute_value)
	frappe.db.sql(
		"UPDATE `tabAttribute Value` SET `name` = %s, `attribute_value` = %s, `abbreviation` = %s WHERE `attribute_value` = %s AND `is_sub_setting_type` = 1",
		(NEW_SUB_SETTING, NEW_SUB_SETTING, NEW_SUB_ABBR, OLD_SUB_SETTING),
	)

	# ========== STEP 1b: Migrate Item Attribute option lists (Sub Setting Type1/2) ==========
	# Item Attribute is a separate master from Attribute Value doctype
	# Update tabItem Attribute Value for "Sub Setting Type1" and "Sub Setting Type2" attributes
	_migrate_item_attribute_values()

	# ========== STEP 2: Migrate doctype records (data) ==========
	# 2a. Migrate records with BOTH fields: setting_type="Close" (regardless of sub_setting_type)
	#     AND sub_setting_type*="Close Setting" (regardless of setting_type)
	for doctype, sub_fields in DOCTYPES_WITH_BOTH:
		meta = frappe.get_meta(doctype)
		if not any(f.fieldname == "setting_type" for f in meta.fields):
			continue

		# ---- First: Update ALL rows where setting_type = "Close" -> "Nova Glow" ----
		updated_st = frappe.db.sql(
			f"UPDATE `tab{doctype}` SET `setting_type` = %s WHERE `setting_type` = %s",
			(NEW_SETTING_TYPE, OLD_SETTING_TYPE),
		)
		if updated_st:
			frappe.logger().debug(
				f"{SENTINEL}: {doctype}.setting_type updated {updated_st} rows"
			)

		# ---- Also fix: setting_type incorrectly containing "Close Setting" (sub-setting value in setting_type field) ----
		updated_st2 = frappe.db.sql(
			f"UPDATE `tab{doctype}` SET `setting_type` = %s WHERE `setting_type` = %s",
			(NEW_SUB_SETTING, OLD_SUB_SETTING),
		)
		if updated_st2:
			frappe.logger().debug(
				f"{SENTINEL}: {doctype}.setting_type (was Close Setting) updated {updated_st2} rows"
			)

		# ---- Second: Update sub_setting_type* = "Close Setting" -> "Nova Glow Setting" ----
		for sf in sub_fields:
			if not frappe.db.has_column(doctype, sf):
				continue
			updated = frappe.db.sql(
				f"UPDATE `tab{doctype}` SET `{sf}` = %s WHERE `{sf}` = %s",
				(NEW_SUB_SETTING, OLD_SUB_SETTING),
			)
			if updated:
				frappe.logger().debug(
					f"{SENTINEL}: {doctype}.{sf} updated {updated} rows"
				)

	# 2b. Migrate records with ONLY sub_setting_type fields (where sub_setting_type="Close Setting")
	for doctype, sub_fields in DOCTYPES_SUB_ONLY:
		for sf in sub_fields:
			if not frappe.db.has_column(doctype, sf):
				continue
			updated = frappe.db.sql(
				f"UPDATE `tab{doctype}` SET `{sf}` = %s WHERE `{sf}` = %s",
				(NEW_SUB_SETTING, OLD_SUB_SETTING),
			)
			if updated:
				frappe.logger().debug(
					f"{SENTINEL}: {doctype}.{sf} updated {updated} rows"
				)

	# Single commit at the end — no mid-patch commit to avoid partial-failure deadlock
	frappe.db.commit()


def _migrate_item_attribute_values():
	"""Migrate Item Attribute option lists for all setting-related attributes."""
	# Find all Item Attributes related to setting types
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
			# Migrate "Close" → "Nova Glow" for Setting Type (also set abbreviation CL → NG)
			frappe.db.sql(
				"""
                UPDATE `tabItem Attribute Value`
                SET `attribute_value` = %s, `abbr` = %s
                WHERE `parent` = %s AND `attribute_value` = %s
                """,
				(NEW_SETTING_TYPE, "NG", attr_name, OLD_SETTING_TYPE),
			)
			frappe.logger().debug(
				f"{SENTINEL}: Item Attribute '{attr_name}' option '{OLD_SETTING_TYPE}' → '{NEW_SETTING_TYPE}' (abbr: NG)"
			)
		else:
			# Migrate "Close Setting" → "Nova Glow Setting" for Sub Setting Type*
			# Also update abbreviation from CLS → NGS
			frappe.db.sql(
				"""
                UPDATE `tabItem Attribute Value`
                SET `attribute_value` = %s, `abbr` = %s
                WHERE `parent` = %s AND `attribute_value` = %s
                """,
				(NEW_SUB_SETTING, NEW_SUB_ABBR, attr_name, OLD_SUB_SETTING),
			)
			frappe.logger().debug(
				f"{SENTINEL}: Item Attribute '{attr_name}' option '{OLD_SUB_SETTING}' → '{NEW_SUB_SETTING}' (abbr: {NEW_SUB_ABBR})"
			)
