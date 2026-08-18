from contextlib import suppress

import frappe
import requests
from frappe import _

SETTINGS = "Data Migration in KGGK"

REF_CUSTOMER_METHOD = (
	"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.get_po_ref_customer"
)
PO_METHOD = (
	"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.get_po_for_quotation"
)

# Two timeouts because the two calls carry different weight. The ref-customer lookup is a hint --
# the Quotation is complete without it -- so it stays cheap. The document fetch is what the
# Quotation is built from, and it returns item rows, so a 2s ceiling would fail it routinely over
# a cloud link. Tunable per site via site_config, read at call time so no restart is needed.
TIMEOUT = 2
PO_TIMEOUT = 10
PO_TIMEOUT_CONF_KEY = "remote_po_fetch_timeout"

# Data Migration in KGGK carries two Data fields both labelled "From Site", indistinguishable in
# the UI. from_site_1 (the "Get Pricing Details" section) is the one the other pull paths use as a
# base URL -- doc_events/sales_order.py reads it for all four rate lookups. from_site sits next to
# to_site and is only a truthiness guard for the Item/BOM push in gke_customization's item.py, which
# posts to to_site. This is a pull, so from_site_1 wins; from_site is tried after it so neither way
# of configuring the Single leaves the lookup permanently dead.
SOURCE_SITE_FIELDS = ("from_site_1", "from_site")

# Request-scoped memos, so N Quotation rows sharing one Purchase Order cost one call at most.
CACHE_KEY = "_jewellery_remote_po_ref_customer"
PO_CACHE_KEY = "_jewellery_remote_po_document"

# Cross-request breaker: one failure silences the lookup for BREAKER_TTL seconds, so an upstream
# outage costs one TIMEOUT wait per window instead of one per save. The TTL is the reset.
#
# It gates the ref-customer hint only. fetch_remote_po is deliberately exempt: silencing it would
# degrade every Quotation built in the window to local-mirror data without the operator asking for
# it, and the request-scoped memo plus PO_TIMEOUT already bound the cost of a single save.
BREAKER_KEY = "jewellery:remote_po_ref_customer:unavailable"
BREAKER_TTL = 300

LOG_TITLE = "Quotation: remote Ref Customer lookup failed"


def _cache(attr):
	cache = getattr(frappe.local, attr, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, attr, cache)
	return cache


def _source_site():
	for field in SOURCE_SITE_FIELDS:
		site = frappe.db.get_single_value(SETTINGS, field)
		if site:
			return site
	return None


def _credentials():
	api_key = frappe.db.get_single_value(SETTINGS, "api_key")
	api_secret = frappe.db.get_single_value(SETTINGS, "api_secret")
	return api_key, api_secret


def remote_lookup_configured():
	"""True when this site pulls Purchase Orders from another site.

	Lets a caller tell "the fetch failed" apart from "this site owns its Purchase Orders and was
	never going to fetch", so only the first is worth telling the operator about.
	"""
	try:
		return bool(_source_site())
	except Exception:
		return False


def _classify(exc):
	"""Return a short, greppable reason for a failed remote call.

	Every failure returns None to the caller, so the reason is the only thing that tells a
	misconfigured site apart from a missing endpoint, a permission denial and a slow link.
	"""
	if isinstance(exc, requests.exceptions.Timeout):
		return "timeout"

	status = getattr(getattr(exc, "response", None), "status_code", None)
	if status:
		return f"http-{status}"

	if isinstance(exc, requests.exceptions.RequestException):
		return "transport"

	return "unexpected"


def _log_failure(exc, remote_method, from_site, po_name):
	"""Record why a remote call failed, without letting the recording itself raise.

	defer_insert because log_error otherwise inserts inside the caller's own transaction: a
	failure logged from Quotation.before_validate is rolled back with the Quotation if the save
	later aborts, which makes an empty Error Log look like proof the lookup never ran.

	The title is deliberately stable -- operators filter on it, and it lands in Error Log.method,
	not a title column -- so the discriminator goes on the first line of the message instead.
	"""
	reason = _classify(exc)
	body = getattr(getattr(exc, "response", None), "text", "") or ""

	with suppress(Exception):
		frappe.log_error(
			title=LOG_TITLE,
			message=(
				f"reason: {reason}\n"
				f"method: {remote_method}\n"
				f"from_site: {from_site}\n"
				f"Purchase Order: {po_name}\n"
				f"response: {body[:500]}\n\n"
				f"{frappe.get_traceback()}"
			),
			defer_insert=True,
		)

	return reason


def _call(remote_method, po_name, timeout):
	"""POST to remote_method on the owning site and return its ``message``.

	Raises nothing: returns ``(value, reason)`` where reason is None on success and a short
	failure code otherwise. ``from_site`` unset is not a failure -- it is the off switch -- and
	returns ``(None, None)`` without logging.
	"""
	from_site = _source_site()
	if not from_site:
		return None, None

	api_key, api_secret = _credentials()

	try:
		response = requests.post(
			f"{from_site}/api/method/{remote_method}",
			headers={"Authorization": f"token {api_key}:{api_secret}"},
			json={"po_name": po_name},
			timeout=timeout,
		)
		response.raise_for_status()
		return (response.json() or {}).get("message"), None
	except Exception as exc:
		return None, _log_failure(exc, remote_method, from_site, po_name)


def fetch_remote_ref_customer(po_name):
	"""Return the Ref Customer recorded against ``po_name`` on the site that owns it.

	The KGGK site works from mirrored copies of Gurukrupa Export Purchase Orders, and a mirror can
	land without ``ref_customer`` -- leaving the Quotation with nothing to copy. This reaches back
	for it.

	Best-effort by convention: this is a hint, not a correctness guard, so every failure path returns
	None -- it can never raise into a Quotation save. It does block that save while it waits, so the
	wait is bounded on both axes: one TIMEOUT-second attempt, then silence for BREAKER_TTL seconds.

	Returns None on the owning site itself, where no From Site is configured -- that empty setting is
	the off switch, so no site_config flag is needed.
	"""
	if not po_name:
		return None

	cache = _cache(CACHE_KEY)
	if po_name in cache:
		return cache[po_name]

	# Seed the miss first: a failure must not be retried once per remaining row.
	cache[po_name] = None

	# Ahead of the settings read, so an open breaker costs nothing at all. A cache that cannot be
	# read counts as closed and the lookup goes ahead: TIMEOUT is the primary bound, so losing the
	# breaker costs latency, never the feature. Failing the other way would let a Redis hiccup
	# silently disable the whole lookup.
	breaker_open = False
	with suppress(Exception):
		breaker_open = bool(frappe.cache().get_value(BREAKER_KEY, expires=True))
	if breaker_open:
		return None

	ref_customer, reason = _call(REF_CUSTOMER_METHOD, po_name, TIMEOUT)

	if reason:
		# The breaker value is the reason rather than 1, so an operator reading the cache learns
		# why the lookup is currently silent. bool() above still treats any non-empty string as open.
		with suppress(Exception):
			frappe.cache().set_value(BREAKER_KEY, reason, expires_in_sec=BREAKER_TTL)
		return None

	cache[po_name] = ref_customer or None
	return cache[po_name]


def fetch_remote_po(po_name):
	"""Return the full Purchase Order payload from the site that owns it, or None.

	This is what the Quotation is built from on a site holding only a mirror: the mirror can be
	missing ``ref_customer``, and the mirror's site has no Company row carrying the buying
	company's ``customer_code``, so neither of the Quotation's two Customer fields can be resolved
	locally. Both arrive here, resolved by the owning site against its own masters.

	Still never raises -- the caller falls back to its local copy of the Purchase Order -- but
	unlike the ref-customer hint the failure is visible: the caller says which source it used.

	Returns None where no From Site is configured, which is every site that owns its own Purchase
	Orders. That empty setting keeps the local path in place with no request made at all.
	"""
	if not po_name:
		return None

	cache = _cache(PO_CACHE_KEY)
	if po_name in cache:
		return cache[po_name]

	cache[po_name] = None

	timeout = frappe.conf.get(PO_TIMEOUT_CONF_KEY) or PO_TIMEOUT
	po, reason = _call(PO_METHOD, po_name, timeout)
	if reason or not po:
		return None

	cache[po_name] = po
	return po


def assert_local_customer(customer, po_name, fieldname):
	"""Return ``customer`` when it resolves locally, else warn and return None.

	Both Quotation Customer fields are Link -> Customer, so assigning a name this site does not
	have turns a blank field into a hard throw on save. Dropping it is therefore correct -- but
	dropping it *silently* is what made this whole class of failure invisible, so say so.
	"""
	if not customer:
		return None

	if frappe.db.exists("Customer", customer):
		return customer

	frappe.msgprint(
		_(
			"{0} {1} on Purchase Order {2} is not a Customer on this site, so it was not copied."
		).format(fieldname, frappe.bold(customer), po_name),
		indicator="orange",
		alert=True,
	)
	frappe.logger("jewellery.quotation").warning(
		f"dropped {fieldname}={customer} for Purchase Order {po_name}: no local Customer"
	)
	return None


def resolve_ref_customer(po_name, local_value=None):
	"""Return the Ref Customer for ``po_name``, preferring what this site already holds.

	Falls back to the owning site, and to the narrow endpoint when that site is a version behind
	and does not serve get_po_for_quotation yet.

	The local-Customer guard applies to the local value too: a mirrored Purchase Order can name a
	Customer this site has never had, and that is the case most likely to throw on save.
	"""
	if local_value:
		return assert_local_customer(local_value, po_name, "Ref Customer")

	if not po_name:
		return None

	po = fetch_remote_po(po_name)
	if po is not None:
		remote = po.get("ref_customer")
	else:
		remote = fetch_remote_ref_customer(po_name)

	return assert_local_customer(remote, po_name, "Ref Customer")
