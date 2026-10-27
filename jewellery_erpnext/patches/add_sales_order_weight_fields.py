"""Create the Sales Order / Sales Order Item weight fields on sites that never got them.

``doc_events.sales_order`` fills these from the BOM (``_update_bom_totals``) and reads
``custom_other_weight`` back in ``validate_item_dharm`` for the "Other Weight" invoice row,
so a site without the field fails every Sales Order save with:
  AttributeError: 'SalesOrder' object has no attribute 'custom_other_weight'

The fields are declared in custom_fields/sales_order*.json and in patches/customize_fields.py,
but neither reaches an existing site: the JSON only applies at install, and customize_fields
was never registered in patches.txt.

Create-only: a field already present is left untouched, so sites that have the block keep
their labels and the layout from split_sales_order_finding_weight. Created fields are
chained after ``custom_other_finding_weight`` (made by split_sales_order_finding_weight) so
they land beside the existing weight fields; the 5-column layout is NOT re-applied here.
Once the form looks right, it can be tidied with::

    bench --site <site> execute jewellery_erpnext.patches.split_sales_order_finding_weight.execute

Idempotent. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_sales_order_weight_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

WEIGHT_FIELDS = (
	("custom_gross_weight", "Float", "Gross Weight"),
	("custom_metal_weight", "Float", "Metal Weight"),
	("custom_finding_weight", "Float", "Finding Weight"),
	("custom_diamond_weight", "Float", "Diamond Weight"),
	("custom_gemstone_weight", "Float", "Gemstone Weight"),
	("custom_other_weight", "Float", "Other Weight"),
	("custom_diamond_pcs", "Int", "Diamond Pcs"),
	("custom_gemstone_pcs", "Int", "Gemstone Pcs"),
)

# Sales Order labels as set by split_sales_order_finding_weight.
SALES_ORDER_LABELS = {
	"custom_gemstone_weight": "Gemstone Weight (gms)",
	"custom_other_weight": "Other Weight (gms)",
}

# Anchor for the first created field when custom_other_finding_weight is absent.
FALLBACK_ANCHOR = {
	"Sales Order": "items",
	"Sales Order Item": "finding_weight_for_chain",
}


def _missing_fields(doctype):
	meta = frappe.get_meta(doctype)
	anchor = (
		"custom_other_finding_weight"
		if meta.has_field("custom_other_finding_weight")
		else FALLBACK_ANCHOR[doctype]
	)

	fields = []
	for fieldname, fieldtype, label in WEIGHT_FIELDS:
		if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
			continue

		field = {
			"fieldname": fieldname,
			"fieldtype": fieldtype,
			"label": label,
			"insert_after": anchor,
			"module": "Jewellery Erpnext",
		}
		if doctype == "Sales Order":
			field["label"] = SALES_ORDER_LABELS.get(fieldname, label)
			# Kept and filled for Delivery Note / Sales Invoice, but hidden on the form.
			if fieldname == "custom_finding_weight":
				field["hidden"] = 1

		fields.append(field)
		anchor = fieldname

	return fields


def execute():
	custom_fields = {}
	for doctype in ("Sales Order", "Sales Order Item"):
		fields = _missing_fields(doctype)
		if fields:
			custom_fields[doctype] = fields

	if not custom_fields:
		return

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_sales_order_weight_fields: created "
		+ ", ".join(
			f"{dt}.{f['fieldname']}"
			for dt, fields in custom_fields.items()
			for f in fields
		)
	)
