from contextlib import suppress

import frappe
import requests

SETTINGS = "Data Migration in KGGK"
REMOTE_METHOD = (
	"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.get_po_ref_customer"
)
TIMEOUT = 2

# Data Migration in KGGK carries two Data fields both labelled "From Site", indistinguishable in
# the UI. from_site_1 (the "Get Pricing Details" section) is the one the other pull paths use as a
# base URL -- doc_events/sales_order.py reads it for all four rate lookups. from_site sits next to
# to_site and is only a truthiness guard for the Item/BOM push in gke_customization's item.py, which
# posts to to_site. This is a pull, so from_site_1 wins; from_site is tried after it so neither way
# of configuring the Single leaves the lookup permanently dead.
SOURCE_SITE_FIELDS = ("from_site_1", "from_site")

# Request-scoped memo, so N Quotation rows sharing one Purchase Order cost one call at most.
CACHE_KEY = "_jewellery_remote_po_ref_customer"

# Cross-request breaker: one failure silences the lookup for BREAKER_TTL seconds, so an upstream
# outage costs one TIMEOUT wait per window instead of one per save. The TTL is the reset.
BREAKER_KEY = "jewellery:remote_po_ref_customer:unavailable"
BREAKER_TTL = 300


def _cache():
	cache = getattr(frappe.local, CACHE_KEY, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, CACHE_KEY, cache)
	return cache


def _source_site():
	for field in SOURCE_SITE_FIELDS:
		site = frappe.db.get_single_value(SETTINGS, field)
		if site:
			return site
	return None


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

	cache = _cache()
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

	try:
		from_site = _source_site()
		if not from_site:
			return None

		api_key = frappe.db.get_single_value(SETTINGS, "api_key")
		api_secret = frappe.db.get_single_value(SETTINGS, "api_secret")

		response = requests.post(
			f"{from_site}/api/method/{REMOTE_METHOD}",
			headers={"Authorization": f"token {api_key}:{api_secret}"},
			json={"po_name": po_name},
			timeout=TIMEOUT,
		)
		response.raise_for_status()
		ref_customer = (response.json() or {}).get("message")
	except Exception:
		# Neither of these may raise on the way out: the breaker is a cache write, and log_error
		# inserts an Error Log from inside the Quotation's own transaction.
		with suppress(Exception):
			frappe.cache().set_value(BREAKER_KEY, 1, expires_in_sec=BREAKER_TTL)
		with suppress(Exception):
			frappe.log_error(
				title="Quotation: remote Ref Customer lookup failed",
				message=f"Purchase Order: {po_name}\n\n{frappe.get_traceback()}",
			)
		return None

	cache[po_name] = ref_customer or None
	return cache[po_name]
