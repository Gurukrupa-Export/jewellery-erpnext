"""Provision the ``Order Form Detail.pre_order_form_details`` custom field.

gke_customization's ``Order Form`` submit path reads this field as a direct attribute::

    # gke .../doctype/order_form/order_form.py -> create_cad_orders()
    for row in self.order_details:
        docname = make_cad_order(row.name, parent_doc=self)
        if row.pre_order_form_details:  # <-- direct attribute access
            frappe.db.set_value(
                "Pre Order Form Details", row.pre_order_form_details, ...
            )

The field is declared ONLY in ``gke_customization/fixtures/custom_field.json``, and CI
deliberately disables those fixtures (``install.sh`` moves ``gke_customization/.../fixtures``
aside before ``install-app gke_customization``), so the docfield never reaches the test DB.
A field absent from the doctype meta is never set as an attribute when the child row loads, so
``row.pre_order_form_details`` raises ``AttributeError: 'OrderFormDetail' object has no
attribute 'pre_order_form_details'`` during Order Form submit. That breaks jewellery_erpnext's
``test_quotation``: ``create_order`` -> ``make_order_form`` submit throws, no ``Order`` is
created, and the downstream ``make_quotation_batch([None])`` cascades into ``order.company`` /
``order_qty + 1`` on ``None``.

Provisioning the docfield makes the attribute resolve to ``None`` (the test's order rows carry
no pre-order link), so gke's guarded block is simply skipped. This is the same class of
"fixture-only config is dead on CI" gap that ``fetch_from_guard`` / the precision guards close.

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``, so on real sites where the
gke fixture already created the field this is a no-op. Wired in two places (both idempotent):
this ``post_model_sync`` patch (existing-site migrate) and ``create_test_data.setup_data``
(fresh / CI sites). Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_order_form_detail_pre_order_field.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Order Form Detail": [
			{
				"fieldname": "pre_order_form_details",
				"fieldtype": "Link",
				"options": "Pre Order Form Details",
				"label": "Pre Order Form Details",
				"insert_after": "status",
				"module": "GKE Order Forms",
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_order_form_detail_pre_order_field: ensured Order Form Detail.pre_order_form_details"
	)
