"""Manufacturing Plan -> KGGK item and BOM sync.

This module used to hold a self-contained sync that was never wired to anything: it was
registered in no hooks file, carried two hardcoded site URLs, read a settings doctype
different from the one its own error messages named, and posted to an endpoint that does
not exist. All of that is gone.

The sync engine now lives in ``gke_customization`` alongside the Data Migration in KGGK
settings it reads. The public names here are kept so nothing that already references them
breaks, and each one delegates. The import is deliberately inside the functions:
``gke_customization`` imports this app, so a module-level import here would close the loop.
"""

import frappe


def _plan_sync():
	from gke_customization.gke_order_forms.doc_events import manufacturing_plan

	return manufacturing_plan


def is_kggk_item_bom_sync_enabled():
	"""The Is Migrate switch on Data Migration in KGGK."""
	from gke_customization.gke_order_forms.doc_events.kggk_sync.config import is_sync_enabled

	return is_sync_enabled()


def add_item_bom_to_kggk(doc, method=None):
	"""on_submit entry point. Queues the subcontracting rows; never blocks the submit."""
	return _plan_sync().on_submit(doc, method)


@frappe.whitelist()
def sync_plan(plan_name, only_unsynced=1):
	"""Manual re-push for one Manufacturing Plan."""
	return _plan_sync().sync_plan(plan_name, only_unsynced)


@frappe.whitelist()
def get_plan_sync_status(plan_name):
	"""Synced / total counts for the indicator on the Manufacturing Plan form."""
	return _plan_sync().get_plan_sync_status(plan_name)


def add_item_bom_to_kggk_by_schedule():
	"""Kept as a scheduler entry point; drains unsynced records in a bounded batch.

	The old version scanned Manufacturing Plans created in a three-day window and posted to
	a hardcoded dummy site. Draining by sync marker is both cheaper and correct - a record
	that failed a week ago is still unsynced today and still needs pushing.
	"""
	from gke_customization.gke_order_forms.doc_events.kggk_sync.config import get_sync_config
	from gke_customization.gke_order_forms.doc_events.kggk_sync.log import log_skip
	from gke_customization.gke_order_forms.doc_events.kggk_sync.push import sync_records

	config, reason = get_sync_config()
	if not config:
		log_skip(reason)
		return {"status": "skipped", "reason": reason}

	from gke_customization.gke_order_forms.doc_events.kggk_sync import selectors

	items = selectors.unsynced_items(limit=200)
	boms = selectors.unsynced_boms(limit=200)
	if not items and not boms:
		return {"status": "completed", "items_sent": 0, "boms_sent": 0}

	return sync_records(items=items, boms=boms, trigger="Scheduler", reference="nightly drain")
