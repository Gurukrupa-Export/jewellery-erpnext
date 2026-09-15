"""Provision ``Batch.custom_batch_components`` -- the C09 component-provenance table.

WHY A PATCH AND NOT JUST ``custom_fields/batch.json``
-----------------------------------------------------
The JSON file is inert on an existing site. It is read only by ``after_migrate``, which is
commented out at ``hooks.py:12``, and by ``install.py``'s ``after_install``, which by
definition does not run on a site that is already installed. Adding the field to
``custom_fields/batch.json`` alone therefore provisions it on FRESH installs and on no
existing site -- which was measured: after adding it there and running
``bench --site cg-integration.test migrate``, the ``Batch Component`` DocType arrived (it is
app source) and the Custom Field did not.

So it is wired in both idempotent places, per the app convention that C04 established: the
JSON for fresh installs, and this ``post_model_sync`` patch for everything else.

``insert_after`` anchors on ``custom_origin_entries``, which this app owns and which
``custom_fields/batch.json`` has carried for a long time; if it is somehow absent the field
still lands (``create_custom_fields`` tolerates an unresolvable anchor and appends), it simply
sits elsewhere on the form.

WHAT IT DOES NOT DO
-------------------
Nothing backfills existing batches, deliberately. A component breakdown reconstructed after
the fact from mutable ``Batch.custom_customer`` tags would be a guess presented as history --
the precise failure mode C09 exists to fix. Batches created before this patch have no
recorded components, and every consumer treats that as "nothing recorded", which carves out
nothing and changes no behaviour. Provenance starts accruing from the next conversion.

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``. Can be re-run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_batch_component_field.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	if not frappe.db.exists("DocType", "Batch Component"):
		# The child DocType ships as app source, so migrate creates it before patches run.
		# If it is missing, the app is mid-install or partially synced -- provisioning a
		# Table field pointing at an absent DocType poisons the Batch meta for every reader
		# (the failure mode measured on gke's own fixtures: a Table field whose options
		# DocType no app ships makes every frappe.get_doc on the parent raise ImportError).
		# Skip and let the next migrate do it.
		return

	create_custom_fields(
		{
			"Batch": [
				{
					"fieldname": "custom_batch_components",
					"fieldtype": "Table",
					"label": "Batch Components",
					"options": "Batch Component",
					"insert_after": "custom_origin_entries",
					"module": "Customer Subcontracting",
					"description": (
						"C09. What this batch is actually MADE OF, with each component's owner "
						"recorded as at the moment of mixing. Distinct from Batch MultiSelect "
						"above, which records which batches were consumed."
					),
				}
			]
		},
		ignore_validate=True,
	)
