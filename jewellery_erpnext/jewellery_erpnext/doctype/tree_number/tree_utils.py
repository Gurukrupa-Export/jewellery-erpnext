# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

"""Shared wax-tree and flask weight calculations for the Tree Number doctype.

These centralise the KT -> Manufacturing Setting field mapping and the flask
powder/water/boric/special formulas that were previously duplicated inline in
Main Slip (main_slip.py:68-84 and main_slip.js:86-168). Keeping them here lets
Tree Number stay authoritative on the server while the client script mirrors the
same arithmetic for instant feedback.
"""

import frappe
from frappe.utils import flt

# Metal Touch (KT) -> Manufacturing Setting wax-to-gold conversion factor field.
WAX_TO_GOLD_FIELD_MAP = {
	"10KT": "wax_to_gold_10",
	"14KT": "wax_to_gold_14",
	"18KT": "wax_to_gold_18",
	"22KT": "wax_to_gold_22",
	"24KT": "wax_to_gold_24",
}


def get_wax_to_gold_ratio(manufacturer, metal_touch):
	"""Conversion factor used to turn wax tree weight into computed gold weight."""
	field = WAX_TO_GOLD_FIELD_MAP.get(metal_touch)
	if not field or not manufacturer:
		return 0.0
	return flt(
		frappe.db.get_value(
			"Manufacturing Setting", {"manufacturer": manufacturer}, field
		)
	)


def get_computed_gold_wt(manufacturer, metal_touch, tree_wax_wt):
	return flt(tree_wax_wt) * get_wax_to_gold_ratio(manufacturer, metal_touch)


def get_flask_weights(manufacturer, powder_wt, is_wax_setting=False):
	"""Return {water_weight, boric_powder_weight, special_powder_weight}.

	Mirrors main_slip.js:86-145 exactly:
	  water_weight = powder_wt * water_value / powder_value
	    (with is_wax_setting -> water_value_individual / power_value_individual)
	  boric/special only when is_wax_setting, both divided by the ORIGINAL
	  powder_value (not the individual one).
	"""
	result = {
		"water_weight": 0.0,
		"boric_powder_weight": 0.0,
		"special_powder_weight": 0.0,
	}
	if not powder_wt or not manufacturer:
		return result

	v = (
		frappe.db.get_value(
			"Manufacturing Setting",
			{"manufacturer": manufacturer},
			[
				"powder_value",
				"water_value",
				"boric_value",
				"special_powder_boric_value",
				"power_value_individual",
				"water_value_individual",
			],
			as_dict=True,
		)
		or {}
	)

	orig_powder_value = flt(v.get("powder_value"))
	water_value = flt(v.get("water_value"))
	powder_value = orig_powder_value

	if is_wax_setting:
		water_value = flt(v.get("water_value_individual"))
		powder_value = flt(v.get("power_value_individual"))
		if orig_powder_value:
			result["boric_powder_weight"] = (
				flt(powder_wt) * flt(v.get("boric_value")) / orig_powder_value
			)
			result["special_powder_weight"] = (
				flt(powder_wt)
				* flt(v.get("special_powder_boric_value"))
				/ orig_powder_value
			)

	if powder_value:
		result["water_weight"] = flt(powder_wt) * water_value / powder_value

	return result
