"""Manufacturing Plan -> KGGK.

Thin delegation. The push itself lives in one module in ``gke_customization``, next to the
Data Migration in KGGK settings it reads.

The module is imported at call time: ``gke_customization`` imports this app, so a
module-level import here would close the loop.

There is one KGGK target and one engine behind it - Item saves, BOM saves, plan submits and
the hourly reconciler all end up in the same place - gated behind the "Enable KGGK Sync"
switch, which is off until somebody turns it on.
"""

import importlib
import importlib.util

KGGK_SYNC = "gke_customization.gke_order_forms.doc_events.kggk_sync"


def add_item_bom_to_kggk(doc, method=None):
	"""on_submit entry point. Queues the subcontracting rows; never blocks the submit."""
	kggk_sync = _kggk_sync()
	if not kggk_sync:
		return None

	return kggk_sync.on_submit(doc, method)


def _kggk_sync():
	"""The KGGK sync module, or ``None`` when this site's gke_customization does not have it.

	Older gke_customization branches - KGGK's own sites and the CI bench run ``kggk_prod`` -
	have no ``kggk_sync``. A plan submitted there has nothing to queue and must still submit.
	Only a missing module is treated that way: an error inside a present module still raises.
	"""
	try:
		if not importlib.util.find_spec(KGGK_SYNC):
			return None
	except ModuleNotFoundError:
		# gke_customization itself is not installed on this bench.
		return None

	return importlib.import_module(KGGK_SYNC)
