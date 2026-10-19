"""Manufacturing Plan -> KGGK.

Thin delegation. The push itself lives in one module in ``gke_customization``, next to the
Data Migration in KGGK settings it reads.

The import is deliberately inside the function: ``gke_customization`` imports this app, so a
module-level import here would close the loop.

There is one KGGK target and one engine behind it - Item saves, BOM saves, plan submits and
the hourly reconciler all end up in the same place - gated behind the "Enable KGGK Sync"
switch, which is off until somebody turns it on.
"""


def add_item_bom_to_kggk(doc, method=None):
	"""on_submit entry point. Queues the subcontracting rows; never blocks the submit."""
	from gke_customization.gke_order_forms.doc_events import kggk_sync

	return kggk_sync.on_submit(doc, method)
