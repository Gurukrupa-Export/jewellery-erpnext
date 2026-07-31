"""Seed the refining dust/scrap master data from ``Process Dust With Rate.xlsx``.

Creates (idempotently):
  * Item Groups ``Refining Scrap`` and ``Refining Chemical``
  * the 10 dust/scrap master Items (Sheet 2 "ItemList")
  * the ``REF-SVC-001`` refining-charge service Item
  * ``Buying Settings.allow_multiple_items`` (external refining POs repeat the service
    item across per-category lines; ERPNext blocks that unless this is on)

The Refinery Price List rows (Sheet 1) are seeded separately by
``seed_refinery_price_list`` (which links each price row to these items).

Wired both here (``post_model_sync``) and in ``create_test_data.setup_data`` because
``after_migrate`` is disabled and ``install-app`` marks patches complete WITHOUT
running them on fresh / CI sites. Idempotent: every create is guarded by
``frappe.db.exists``. Can be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.seed_refining_masters.execute
"""

import frappe

ITEM_GROUPS = ("Refining Scrap", "Refining Chemical")

# The distinct refining processes from the price sheet (Sheet 1 col A). Seeded as a
# master so Refinery Price List / Refining Entry can Link to them (controlled dropdown),
# instead of free-text that lets typos through.
REFINING_PROCESSES = (
	"Filing/Setting/Grinding",
	"Polishing/Vacuum",
	"Setting Tank",
	"Refining Metal",
	"Refining Studded Jewellery",
	"Floor/Vacuum Cleaning",
	"Burnout Residue",
	"Chemical Process",
	"Tools Scrap",
)

# item_code, item_name, item_group, stock_uom, description
DUST_ITEMS = [
	(
		"REF-MD-001",
		"Dust Item",
		"Refining Scrap",
		"Gram",
		"Dust collected from Filing, Setting, Grinding and Chemical processes",
	),
	(
		"REF-VB-001",
		"Vacuum Bag Dust",
		"Refining Scrap",
		"Gram",
		"Dust collected from Vacuum Bag during polishing and floor cleaning",
	),
	(
		"REF-ST-001",
		"Sedimentation Tank Sludge",
		"Refining Scrap",
		"Gram",
		"Sludge collected from Setting Tank",
	),
	(
		"REF-UL-001",
		"Ultra Liquid",
		"Refining Chemical",
		"Litre",
		"Chemical solution collected from ultrasonic cleaning tank",
	),
	(
		"REF-RMS-001",
		"Metal Refining Scrap",
		"Refining Scrap",
		"Gram",
		"Scrap generated during refining operations",
	),
	(
		"REF-FSJ-001",
		"Finish & Semi Finish Scrap",
		"Refining Scrap",
		"Gram",
		"Rejected or damaged finished/semi-finished studded jewellery",
	),
	(
		"REF-BR-001",
		"Burnout Residue",
		"Refining Scrap",
		"Gram",
		"Residue generated after burnout process",
	),
	(
		"REF-CF-001",
		"Carpet / Thread / Paper Filter Dust",
		"Refining Scrap",
		"Gram",
		"Precious metal recovered from carpet, thread and paper filters",
	),
	(
		"REF-NB-001",
		"Napkin / Thread / Buff Waste",
		"Refining Scrap",
		"Gram",
		"Buffs, polishing cloths and napkins sent for refining",
	),
	(
		"REF-TD-001",
		"Tools Dust",
		"Refining Scrap",
		"Gram",
		"Dust generated during tool maintenance",
	),
]

# Precious-metal waste and scrap (a valid existing GST HSN Code on the site).
_HSN = "7112"


def _ensure_refining_processes():
	if not frappe.db.exists("DocType", "Refining Process"):
		return
	for process in REFINING_PROCESSES:
		if not frappe.db.exists("Refining Process", process):
			frappe.get_doc(
				{"doctype": "Refining Process", "process_name": process}
			).insert(ignore_permissions=True)


def _ensure_item_groups():
	for group in ITEM_GROUPS:
		if not frappe.db.exists("Item Group", group):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": group,
					"parent_item_group": "All Item Groups",
					"is_group": 0,
				}
			).insert(ignore_permissions=True)


def _ensure_items():
	hsn = _HSN if frappe.db.exists("GST HSN Code", _HSN) else None
	for item_code, item_name, item_group, uom, description in DUST_ITEMS:
		if frappe.db.exists("Item", item_code):
			continue
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_name,
				"item_group": item_group,
				"stock_uom": uom,
				"description": description,
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
				"is_purchase_item": 1,
				"is_sales_item": 0,
				"include_item_in_manufacturing": 0,
				"country_of_origin": "India",
				"gst_hsn_code": hsn,
				"uoms": [{"uom": uom, "conversion_factor": 1}],
			}
		).insert(ignore_permissions=True)


# The non-stock service item every refining-service PO line is billed as.
SERVICE_ITEM = "REF-SVC-001"


def _ensure_service_item():
	if frappe.db.exists("Item", SERVICE_ITEM):
		return
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": SERVICE_ITEM,
			"item_name": "Refining Charges",
			"item_group": "Refining Scrap",
			"stock_uom": "Nos",
			"description": "Service charge for external refining (billed on the Purchase Order).",
			"is_stock_item": 0,
			"is_purchase_item": 1,
			"is_sales_item": 0,
			"include_item_in_manufacturing": 0,
			"country_of_origin": "India",
			"gst_hsn_code": "9988"
			if frappe.db.exists("GST HSN Code", "9988")
			else None,
		}
	).insert(ignore_permissions=True)


def _ensure_allow_multiple_items():
	"""External refining bills ONE Purchase Order line per pricing category (see
	RefiningEntry.create_external_refining_po). When several categories share the default
	service item REF-SVC-001 — always, now that slabs no longer carry a service_item of
	their own — the PO carries that same item on multiple rows. ERPNext's buying
	validation (erpnext.buying.utils.validate_for_items) rejects that with "Same item
	cannot be entered multiple times." unless this Buying Setting is on, so a multi-category
	consignment fails to create its PO without it. Production already runs with it enabled;
	enable it on fresh sites (and CI) so they match."""
	if not frappe.db.get_single_value("Buying Settings", "allow_multiple_items"):
		frappe.db.set_single_value("Buying Settings", "allow_multiple_items", 1)


def _ensure_service_item_uoms():
	"""Give REF-SVC-001 the UOM Conversion Detail rows the external refining PO needs.

	The service line's qty is no longer a piece count (1 Nos) but the summed material
	weight, so its uom is Gram / Litre / Carat depending on what was sent — and
	``Purchase Order Item.uom`` is mandatory. Nothing THROWS without these rows
	(get_conversion_factor falls through to 1.0 when no UOM Conversion Factor exists), but
	that is an accident of a fallback: an explicit factor-1 row pins it, and it is what
	Stock Settings.allow_uom_with_conversion_rate_defined_in_item restricts the UI dropdown
	to if that setting is ever switched on.

	Carat is included because a consignment can legitimately bill a carat line if the
	returned-intact exclusion in _external_billable_rows is ever relaxed.
	"""
	if not frappe.db.exists("Item", SERVICE_ITEM):
		return
	item = frappe.get_doc("Item", SERVICE_ITEM)
	changed = False
	for uom in ("Nos", "Gram", "Litre", "Carat"):
		if not frappe.db.exists("UOM", uom):
			continue
		if any(row.uom == uom for row in item.uoms):
			continue
		item.append("uoms", {"uom": uom, "conversion_factor": 1.0})
		changed = True
	if changed:
		item.flags.ignore_permissions = True
		item.save(ignore_permissions=True)


def execute():
	_ensure_refining_processes()
	_ensure_item_groups()
	_ensure_items()
	_ensure_service_item()
	_ensure_service_item_uoms()
	_ensure_allow_multiple_items()
	frappe.logger().info(
		"seed_refining_masters: ensured Refining processes + item groups + 10 dust items "
		"+ service item (with weight UOMs) + allow_multiple_items"
	)
