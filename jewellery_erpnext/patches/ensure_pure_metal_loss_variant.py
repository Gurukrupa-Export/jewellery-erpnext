import frappe


def execute():
	"""Pre-create the Metal Loss (ML) variant of the pure 24KT gold item.

	Non-Dust refining books its loss in PURE (24KT-equivalent) grams, so the repack
	loss row uses the pure ML variant (see ``RefiningEntry._get_pure_loss_item``).
	Creating it here (as Administrator, idempotent) means the runtime resolution
	hits the existing-item lookup instead of creating an Item mid-``complete_refining``
	under an operator's permissions. Skips silently when the site has no pure gold
	item or no Variant Loss Table mapping — the runtime chain then falls back to the
	dedicated "Metal Process Loss" item.
	"""
	pure_item = frappe.db.get_value(
		"Item",
		{"variant_of": "M", "disabled": 0, "name": ["like", "M-G-24KT-99.9%"]},
		"name",
	) or frappe.db.get_value(
		"Item",
		{"variant_of": "M", "disabled": 0, "name": ["like", "M-G-24KT%"]},
		"name",
	)
	if not pure_item:
		print("ensure_pure_metal_loss_variant: no pure 24KT gold item — skipped")
		return
	if not frappe.db.get_value("Variant Loss Table", {"variant": "M"}, "loss_variant"):
		print(
			"ensure_pure_metal_loss_variant: no Variant Loss Table mapping for M — skipped"
		)
		return

	existing = frappe.db.get_value(
		"Item",
		{"variant_of": "ML", "disabled": 0, "name": ["like", "ML-G-24KT%"]},
		"name",
	)
	if existing:
		print(f"ensure_pure_metal_loss_variant: {existing} already exists")
		return

	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
		get_item_loss_item,
	)

	company = frappe.defaults.get_global_default("company") or frappe.db.get_value(
		"Company", {}, "name"
	)
	try:
		created = get_item_loss_item(company, pure_item, variant_of="M")
	except Exception as e:
		print(
			f"ensure_pure_metal_loss_variant: could not create variant ({e}) — skipped"
		)
		return
	print(
		f"ensure_pure_metal_loss_variant: created/resolved {created} from {pure_item}"
	)
