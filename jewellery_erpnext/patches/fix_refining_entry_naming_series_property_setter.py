"""Delete the stale ``Refining Entry-naming_series-options`` Property Setter.

It was created via Customize Form at some point and pins ``naming_series.options`` to
the original 4-series list, silently SHADOWING the DocField definition in
``refining_entry.json`` (Property Setters take precedence over the base DocField in
meta resolution). Left in place, this blocks saving any External Refinery entry:
Frappe's Select-field validation rejects a ``naming_series`` value
(``RFN-EXT-.YY.-.#####``) that is not in the *effective* (property-setter-overridden)
options list, even though it IS in the DocType JSON's list.

Deleting it (rather than updating its value to match) makes the DocField the sole
source of truth going forward, so future series additions never need a matching
property-setter patch. Idempotent: a no-op once already absent.
"""

import frappe


def execute():
	frappe.db.delete(
		"Property Setter",
		{
			"doc_type": "Refining Entry",
			"field_name": "naming_series",
			"property": "options",
		},
	)
	frappe.clear_cache(doctype="Refining Entry")
	frappe.logger().info(
		"fix_refining_entry_naming_series_property_setter: removed stale naming_series "
		"options Property Setter on Refining Entry"
	)
