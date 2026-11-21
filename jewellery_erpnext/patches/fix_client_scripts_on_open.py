"""Stop three DB Client Scripts from changing Items and BOMs as they open.

The scripts live only in the database, so this patch edits them in place. Each edit
replaces one exact block of text; a script whose text differs (edited since this
patch was written) or that does not exist is left alone and reported.
"""

import frappe

BOM_PURITY_ON_REFRESH = '\t    if(frm.doc.bom_type == \'Template\'){\n    \t  if(frm.doc.metal_touch == \'18KT\'){\n              frm.set_value("metal_purity", "75.4");\n    \t  }  \n    \t  if(frm.doc.metal_touch == \'22KT\'){\n    \t      frm.set_value("metal_purity", "91.75");\n    \t  }  \n    \t  if(frm.doc.metal_touch == \'14KT\'){\n    \t      frm.set_value("metal_purity", "58.6");\n    \t  }  \n    \t  if(frm.doc.metal_touch == \'10KT\'){\n    \t      frm.set_value("metal_purity", "41.7");\n    \t  }  \n\t    }\n'

# kggk_uat already commented out the 22KT branch of the same block.
BOM_PURITY_ON_REFRESH_22KT_COMMENTED = '\t    if(frm.doc.bom_type == \'Template\'){\n    \t  if(frm.doc.metal_touch == \'18KT\'){\n              frm.set_value("metal_purity", "75.4");\n    \t  }  \n    // \t  if(frm.doc.metal_touch == \'22KT\'){\n    // \t      frm.set_value("metal_purity", "91.75");\n    // \t  }  \n    \t  if(frm.doc.metal_touch == \'14KT\'){\n    \t      frm.set_value("metal_purity", "58.6");\n    \t  }  \n    \t  if(frm.doc.metal_touch == \'10KT\'){\n    \t      frm.set_value("metal_purity", "41.7");\n    \t  }  \n\t    }\n'

BOM_PURITY_NOTE = '\t    // Purity is set when metal_touch changes (BOM-Form-116). Setting it here on refresh marked\n\t    // most Template BOMs "Not Saved" as soon as they opened and overwrote purity 91.9.\n'

EDITS = {
	# Template BOMs: purity is set on metal_touch change (BOM-Form-116), not on every refresh.
	"BOM-Form-94": [
		(BOM_PURITY_ON_REFRESH, BOM_PURITY_NOTE),
		(BOM_PURITY_ON_REFRESH_22KT_COMMENTED, BOM_PURITY_NOTE),
	],
	# Existing variants: derive item_group only for a new Item.
	"Item-Form-118": [
		(
			"\t    if(frm.doc.variant_of && frm.doc.item_code){\n",
			"\t    // Only for a new variant: on an existing one this rewrote item_group on every open.\n\t    if(frm.is_new() && frm.doc.variant_of && frm.doc.item_code){\n",
		),
	],
	# Reuse signed image URLs instead of calling the signing Lambda on every refresh.
	"Item Image": [
		(
			"// Get a signed URL straight from the Lambda\nasync function get_signed_url(value) {\n",
			"// Signed URLs are reused for a few minutes, so a refresh (e.g. after save) does not sign\n// every image again. Keep SIGNED_URL_TTL_MS below the Lambda's URL lifetime.\nconst SIGNED_URL_TTL_MS = 4 * 60 * 1000;\nconst signed_url_cache = new Map();\n\nasync function get_signed_url(value) {\n    const cached = signed_url_cache.get(value);\n    if (cached && cached.expires > Date.now()) return cached.url;\n    const url = await fetch_signed_url(value);\n    signed_url_cache.set(value, { url, expires: Date.now() + SIGNED_URL_TTL_MS });\n    return url;\n}\n\n// Get a signed URL straight from the Lambda\nasync function fetch_signed_url(value) {\n",
		),
	],
}


def apply_edits(script, edits):
	"""Return the script with every edit whose old text occurs exactly once replaced."""
	applied = 0
	for old, new in edits:
		if script.count(old) == 1:
			script = script.replace(old, new)
			applied += 1
	return script, applied


def execute():
	for name, edits in EDITS.items():
		if not frappe.db.exists("Client Script", name):
			print(f"Client Script {name}: not on this site, skipped")
			continue
		doc = frappe.get_doc("Client Script", name)
		script, applied = apply_edits(doc.script or "", edits)
		if not applied:
			if any(new in script for _old, new in edits):
				print(f"Client Script {name}: already updated")
			else:
				print(
					f"Client Script {name}: differs from the reviewed version, left unchanged"
				)
			continue
		doc.script = script
		doc.save(ignore_permissions=True)
		print(f"Client Script {name}: updated")
