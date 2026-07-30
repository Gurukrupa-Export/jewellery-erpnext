"""Provision the ``Purchase Order.purchase_type`` link.

``purchase_type`` is read UNCONDITIONALLY on every Purchase Order validate --
``doc_events/purchase_order.py::set_gst_details`` opens with
``if self.purchase_type not in (...)`` -- and it is what
``make_subcontracting_order`` stamps on the subcontracting PO it builds from a
Manufacturing Plan (and ``Employee IR`` / ``Product Certification`` / the external
refining flow stamp on theirs). On a site where the field is missing from the
Purchase Order meta, that read raises ``AttributeError`` on any PO whose creator did
not happen to assign the attribute first, and every value assigned to it is dropped
instead of persisted.

The field is declared only in ``custom_fields/purchase_order.json``, which is applied
solely by ``migrate.after_migrate()`` -- a hook that is intentionally disabled
(hooks.py). It therefore exists on long-lived sites (gk.site) purely by history, and
is absent from the ``git_action_v16`` fixture set CI restores, which is why the
Manufacturing Plan subcontracting test could not read ``po.purchase_type`` back off a
reloaded PO. Per the app convention this is wired in two idempotent places: this
``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run
ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_po_purchase_type_field.execute

``update=False`` so a site that already has the field keeps its own layout -- the
declared ``insert_after`` anchor (``get_items_from_open_material_requests``) no longer
exists on the v16 Purchase Order, so re-asserting it would only shuffle the form.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Purchase Order": [
			{
				"fieldname": "purchase_type",
				"fieldtype": "Link",
				"label": "Purchase Type",
				"options": "Purchase Type",
				"insert_after": "company",
				"module": "Jewellery Erpnext",
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True, update=False)

	if not frappe.db.has_column("Purchase Order", "purchase_type"):
		# Custom Field row present but column absent (a half-applied site): the
		# create above is a no-op there, so force the schema sync.
		frappe.db.updatedb("Purchase Order")

	frappe.logger().info(
		"add_po_purchase_type_field: ensured Purchase Order.purchase_type"
	)
