"""External refinery round trip between KGGK and the refiner (GK), which run as SEPARATE sites.

KGGK submits an external Refining Entry: the material goes to the refiner's supplier
warehouse and a service Purchase Order (``Purchase Order.refining_entry``) is raised on the
refiner. The refiner then creates its own Refining Entry FROM that Purchase Order: it pulls
the PO and the material that was sent, receives the material into its refining warehouse on
submit, and tells KGGK, which marks the PO lines received.

This module holds both halves, because the app is installed on both sites:

* SERVER half (runs on KGGK, called over HTTP with a KGGK user's API token):
  ``list_refining_pos``, ``get_refining_po``, ``apply_gk_receipt``, ``reverse_gk_receipt``.
* CLIENT half (runs on GK): ``kggk_config`` and the ``fetch_*`` / ``push_*`` helpers. The
  transport — retries, timeouts, the https and same-site guards, the ``*****`` Password
  handling — is gke_customization's KGGK sync (``kggk_sync``), reused rather than written
  again. Its target and token (``to_site`` / ``api_key`` / ``api_secret`` in "Data Migration
  in KGGK") already mean "the KGGK site and a KGGK user"; only the ``enable_sync`` switch is
  NOT required, because that switch is for the Item/BOM push, not for this flow.

The receipt does NOT go through a Purchase Receipt: the PO lines are REF-* stock items in the
refiner's supplier warehouse, and a Purchase Receipt would add that weight to KGGK stock a
second time. Received qty / per_received / status and the Bin's ordered qty are set directly,
the same values a Purchase Receipt would have written.
"""

import hashlib
import importlib
import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from jewellery_erpnext.refining.constants import (
	REFINING_TYPE_SERIAL,
	SOURCE_TYPE_BOM_COMPONENT,
	SOURCE_TYPE_SERIAL,
)

API = "jewellery_erpnext.refining.kggk_po"
TRANSPORT = "gke_customization.gke_order_forms.doc_events.kggk_sync"

GK_ENTRY_FIELD = "custom_gk_refining_entry"
GK_RECEIVED_ON_FIELD = "custom_gk_received_on"

#: A user is waiting on the picker and on the fetch.
READ_TIMEOUT = 15
#: apply / reverse run inside the refiner's submit / cancel.
WRITE_TIMEOUT = 30
LOCK_TIMEOUT = 30


class GkReceiptConflict(frappe.ValidationError):
	"""The PO is already received against a different refiner entry."""

	http_status_code = 409


class GkReceiptBusy(frappe.ValidationError):
	"""Another apply/reverse of this PO still holds the lock. Safe to try again."""

	http_status_code = 503


# ---------------------------------------------------------------------------------
# SERVER HALF — runs on KGGK
# ---------------------------------------------------------------------------------


@frappe.whitelist(methods=["GET"])
def list_refining_pos(supplier=None):
	"""Submitted refining service POs the refiner has not received yet, newest first."""
	frappe.has_permission("Purchase Order", "read", throw=True)
	_require_gk_fields()
	filters = {
		"docstatus": 1,
		"refining_entry": ["is", "set"],
		"status": ["not in", ["Closed", "Completed", "On Hold"]],
		"per_received": ["<", 100],
		GK_ENTRY_FIELD: ["is", "not set"],
	}
	if supplier:
		filters["supplier"] = supplier
	return frappe.get_list(
		"Purchase Order",
		filters=filters,
		fields=[
			"name",
			"transaction_date",
			"supplier",
			"supplier_name",
			"company",
			"refining_entry",
			"total_qty",
			"grand_total",
		],
		order_by="transaction_date desc, creation desc",
		limit_page_length=200,
	)


@frappe.whitelist(methods=["GET"])
def get_refining_po(purchase_order):
	"""The PO, its lines, and the material its Refining Entry sent to the refiner."""
	po = frappe.get_doc("Purchase Order", purchase_order)
	frappe.has_permission("Purchase Order", "read", doc=po, throw=True)
	_require_gk_fields()
	_validate_refining_po(po)

	entry = frappe.get_doc("Refining Entry", po.refining_entry)
	frappe.has_permission("Refining Entry", "read", doc=entry, throw=True)

	return {
		"purchase_order": po.name,
		"supplier": po.supplier,
		"supplier_name": po.supplier_name,
		"company": po.company,
		"transaction_date": str(po.transaction_date),
		"status": po.status,
		"per_received": flt(po.per_received),
		"gk_refining_entry": po.get(GK_ENTRY_FIELD),
		"items": [
			{
				"item_code": d.item_code,
				"item_name": d.item_name,
				"qty": flt(d.qty),
				"uom": d.uom,
				"rate": flt(d.rate),
				"amount": flt(d.amount),
				"received_qty": flt(d.received_qty),
				"gross_wt": flt(d.get("custom_gross_wt")),
			}
			for d in po.items
		],
		"refining_entry": entry.name,
		"refining_type": entry.refining_type,
		"qty_to_refine": flt(entry.qty_to_refine),
		"materials": sent_material_rows(entry),
	}


def sent_material_rows(entry):
	"""The rows of ``entry`` that physically travelled to the refiner, as plain dicts.

	* the serial design-code row is a PIECE COUNT, not a weight — the piece arrives as its
	  BOM breakdown instead;
	* BOM Component rows are display-only, EXCEPT for Serial Number refining, where they are
	  the only description of what is inside the piece;
	* stones are KEPT (unlike the PO's billable rows): they are not billed, but they travel.
	"""
	rows = []
	for row in entry.material_items:
		if not row.item_code or flt(row.qty, 3) <= 0:
			continue
		if row.get("source_type") == SOURCE_TYPE_SERIAL:
			continue
		if (
			row.get("source_type") == SOURCE_TYPE_BOM_COMPONENT
			and entry.refining_type != REFINING_TYPE_SERIAL
		):
			continue
		rows.append(
			{
				"item_code": row.item_code,
				"item_name": row.get("item_name"),
				"qty": flt(row.qty, 3),
				"uom": row.uom,
				"purity": row.get("purity"),
				"source_type": row.get("source_type"),
				"is_consumable": cint(row.get("is_consumable")),
			}
		)
	return rows


@frappe.whitelist(methods=["POST"])
def apply_gk_receipt(purchase_order, gk_entry, gk_site=None):
	"""Mark every line of ``purchase_order`` received by the refiner's ``gk_entry``.

	Idempotent: applying the same ``gk_entry`` again returns ``already_applied`` and
	changes nothing, so the refiner can retry after a lost answer. A different entry gets a
	409 (GkReceiptConflict).
	"""
	if not gk_entry:
		frappe.throw(_("The refiner's Refining Entry is required."))
	key = _lock_key(purchase_order)
	_lock(key)
	try:
		po = frappe.get_doc("Purchase Order", purchase_order)
		frappe.has_permission("Purchase Order", "write", doc=po, throw=True)
		_require_gk_fields()
		_validate_refining_po(po)

		current = po.get(GK_ENTRY_FIELD)
		if current == gk_entry:
			return _receipt_result(po, "already_applied")
		if current:
			raise GkReceiptConflict(
				_(
					"Purchase Order {0} is already received against the refiner's Refining "
					"Entry {1}."
				).format(frappe.bold(po.name), frappe.bold(current))
			)
		if flt(po.per_received) > 0:
			raise GkReceiptConflict(
				_(
					"Purchase Order {0} is already {1}% received by another document."
				).format(frappe.bold(po.name), flt(po.per_received, 2))
			)

		_set_received(po, received=True)
		po.db_set(
			{GK_ENTRY_FIELD: gk_entry, GK_RECEIVED_ON_FIELD: now_datetime()},
			update_modified=False,
		)
		po.add_comment(
			"Info",
			_("Received by the refiner: Refining Entry {0}{1}.").format(
				gk_entry, f" on {gk_site}" if gk_site else ""
			),
		)
		# Before the lock goes: the next call for this PO must see what this one did.
		frappe.db.commit()
		return _receipt_result(po, "applied")
	finally:
		_unlock(key)


@frappe.whitelist(methods=["POST"])
def reverse_gk_receipt(purchase_order, gk_entry, gk_site=None):
	"""Undo ``apply_gk_receipt`` when the refiner cancels ``gk_entry``.

	Idempotent: a PO no longer carrying any refiner entry returns ``not_applied``.
	"""
	if not gk_entry:
		frappe.throw(_("The refiner's Refining Entry is required."))
	key = _lock_key(purchase_order)
	_lock(key)
	try:
		po = frappe.get_doc("Purchase Order", purchase_order)
		frappe.has_permission("Purchase Order", "write", doc=po, throw=True)
		_require_gk_fields()

		current = po.get(GK_ENTRY_FIELD)
		if not current:
			return _receipt_result(po, "not_applied")
		if current != gk_entry:
			raise GkReceiptConflict(
				_(
					"Purchase Order {0} was received against the refiner's Refining Entry "
					"{1}, not {2}."
				).format(frappe.bold(po.name), frappe.bold(current), frappe.bold(gk_entry))
			)

		_set_received(po, received=False)
		po.db_set(
			{GK_ENTRY_FIELD: None, GK_RECEIVED_ON_FIELD: None}, update_modified=False
		)
		po.add_comment(
			"Info",
			_("Refiner's receipt reversed: Refining Entry {0}{1} was cancelled.").format(
				gk_entry, f" on {gk_site}" if gk_site else ""
			),
		)
		frappe.db.commit()
		return _receipt_result(po, "reversed")
	finally:
		_unlock(key)


def _validate_refining_po(po):
	if not po.get("refining_entry"):
		frappe.throw(
			_("Purchase Order {0} is not a refining service order.").format(
				frappe.bold(po.name)
			)
		)
	if po.docstatus != 1:
		frappe.throw(
			_(
				"Purchase Order {0} is not submitted yet. It has to be priced and submitted "
				"before the refiner can receive against it."
			).format(frappe.bold(po.name))
		)
	if po.status in ("Closed", "On Hold"):
		frappe.throw(
			_("Purchase Order {0} is {1}.").format(frappe.bold(po.name), po.status)
		)


def _set_received(po, received):
	"""Write what a Purchase Receipt for the full PO would have written, without the stock."""
	for item in po.items:
		item.db_set(
			"received_qty", flt(item.qty) if received else 0, update_modified=False
		)
		item.received_qty = flt(item.qty) if received else 0
	po.update_receiving_percentage()
	po.reload()
	po.set_status(update=True)
	_refresh_ordered_qty(po)


def _refresh_ordered_qty(po):
	from erpnext.stock.stock_balance import get_ordered_qty, update_bin_qty

	seen = set()
	for item in po.items:
		key = (item.item_code, item.warehouse)
		if not item.warehouse or key in seen:
			continue
		seen.add(key)
		if not frappe.get_cached_value("Item", item.item_code, "is_stock_item"):
			continue
		update_bin_qty(
			item.item_code,
			item.warehouse,
			{"ordered_qty": get_ordered_qty(item.item_code, item.warehouse)},
		)


def _receipt_result(po, action):
	return {
		"action": action,
		"purchase_order": po.name,
		"per_received": flt(po.per_received),
		"status": po.status,
		"gk_refining_entry": po.get(GK_ENTRY_FIELD),
	}


def _require_gk_fields():
	if not frappe.get_meta("Purchase Order").has_field(GK_ENTRY_FIELD):
		frappe.throw(
			_(
				"Purchase Order has no {0} field yet. Run bench migrate on this site."
			).format(frappe.bold(GK_ENTRY_FIELD))
		)


def _lock_key(purchase_order):
	digest = hashlib.sha1(f"gk-refining-receipt|{purchase_order}".encode()).hexdigest()
	return f"gkrfn:{digest}"


def _lock(key):
	if frappe.db.db_type == "postgres":
		frappe.db.sql("select pg_advisory_xact_lock(hashtext(%s))", (key,))
		return
	got = frappe.db.sql("select get_lock(%s, %s)", (key, LOCK_TIMEOUT))
	if not got or cint(got[0][0]) != 1:
		raise GkReceiptBusy(
			_("Another receipt of this Purchase Order is still being applied. Try again.")
		)


def _unlock(key):
	if frappe.db.db_type == "postgres":
		return
	try:
		frappe.db.sql("select release_lock(%s)", (key,))
	except Exception:
		# The lock dies with the connection anyway; never let tidying up mask the result.
		pass


# ---------------------------------------------------------------------------------
# CLIENT HALF — runs on the refiner (GK)
# ---------------------------------------------------------------------------------


def _transport():
	try:
		return importlib.import_module(TRANSPORT)
	except ImportError:
		frappe.throw(
			_(
				"The KGGK connection is not available on this site (gke_customization's "
				"KGGK sync is not installed)."
			)
		)


def kggk_config():
	"""``frappe._dict(to_site, headers)`` for calling KGGK, or throws with the reason.

	``kggk_sync.get_sync_config`` minus its ``enable_sync`` switch: the target, the
	credentials, the same-site guard and the https guard all still apply.
	"""
	k = _transport()
	settings = (
		frappe.db.get_value(
			k.SETTINGS, k.SETTINGS, ["from_site", "to_site", "api_key"], as_dict=True
		)
		or frappe._dict()
	)
	target = settings.get("to_site")
	if not target:
		frappe.throw(_(k.SKIP_NO_TARGET))
	api_key = settings.get("api_key")
	api_secret = k._api_secret()
	if not api_key or not api_secret:
		frappe.throw(_(k.SKIP_NO_CREDS))

	target_host = k.host_of(target)
	from_site = settings.get("from_site")
	if target_host in k.current_site_hosts() or (
		from_site and k.host_of(from_site) == target_host
	):
		frappe.throw(_(k.SKIP_SAME_SITE).format(target_host))
	if not k.is_secure(target):
		frappe.throw(_(k.SKIP_INSECURE).format(target_host))

	return frappe._dict(
		to_site=k.base_url(target),
		headers={
			"Authorization": f"token {api_key}:{api_secret}",
			"Accept": "application/json",
		},
	)


def _call(method, function, params=None, payload=None, timeout=READ_TIMEOUT, retry=True):
	"""Call ``function`` of this module on KGGK and return its ``message``, or throw."""
	k = _transport()
	config = kggk_config()
	response = k.api_request(
		config,
		method,
		f"/api/method/{API}.{function}",
		params=params,
		json=payload,
		timeout=timeout,
		attempts=None if retry else 1,
		retry_connection=retry,
	)
	if not response.ok:
		frappe.throw(
			_("KGGK refused the request: {0}").format(remote_error(response)),
			title=_("KGGK Purchase Order"),
		)
	return (response.data or {}).get("message")


def remote_error(response):
	"""The readable message out of a Frappe error response."""
	data = response.data or {}
	raw = data.get("_server_messages")
	if raw:
		try:
			messages = [json.loads(m).get("message", m) for m in json.loads(raw)]
			text = " ".join(str(m) for m in messages if m)
			if text:
				return frappe.utils.strip_html(text)
		except Exception:
			pass
	return response.message()


def fetch_refining_pos(supplier=None):
	return (
		_call("GET", "list_refining_pos", params={"supplier": supplier} if supplier else None)
		or []
	)


def fetch_refining_po(purchase_order):
	return _call("GET", "get_refining_po", params={"purchase_order": purchase_order})


def _site_name():
	return frappe.local.site


def push_receipt(entry):
	"""Tell KGGK that ``entry`` (a refiner Refining Entry) received its Purchase Order."""
	# No connection retry: a lost answer is safe to resend by hand, because KGGK keys the
	# receipt on (PO, gk_entry) and answers ``already_applied``.
	return _call(
		"POST",
		"apply_gk_receipt",
		payload={
			"purchase_order": entry.source_purchase_order,
			"gk_entry": entry.name,
			"gk_site": _site_name(),
		},
		timeout=WRITE_TIMEOUT,
		retry=False,
	)


def push_reversal(entry):
	"""Tell KGGK that ``entry`` was cancelled, so the PO is open to receive again."""
	return _call(
		"POST",
		"reverse_gk_receipt",
		payload={
			"purchase_order": entry.source_purchase_order,
			"gk_entry": entry.name,
			"gk_site": _site_name(),
		},
		timeout=WRITE_TIMEOUT,
		retry=False,
	)
