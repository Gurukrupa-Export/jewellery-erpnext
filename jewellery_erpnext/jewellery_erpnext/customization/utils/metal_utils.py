import frappe


def _fetch_purity_percentages(items):
	"""Run the Metal Purity join for ``items`` and yield ``(item, purity_percentage)``.

	Single source of truth for the join, shared by :func:`get_purity_percentage` and
	:func:`prefetch_purity_percentages` so the two can never drift apart.
	"""
	IVA = frappe.qb.DocType("Item Variant Attribute")
	ITEM = frappe.qb.DocType("Item")
	AV = frappe.qb.DocType("Attribute Value")

	return (
		frappe.qb.from_(IVA)
		.join(ITEM)
		.on(ITEM.name == IVA.parent)
		.join(AV)
		.on(IVA.attribute_value == AV.name)
		.select(ITEM.name, AV.purity_percentage)
		.where((IVA.attribute == "Metal Purity") & (ITEM.name.isin(list(items))))
	).run()


@frappe.request_cache
def get_purity_percentage(item):
	"""Metal purity % for an item variant.

	Request/job-scoped cache: an item's Metal Purity attribute cannot change mid-request, and
	the Stock Entry ``before_validate`` hook calls this **once per metal/finding row**. A
	consolidated EOD transfer carries ~9,954 such rows against only ~496 distinct item codes,
	so the uncached version issued that three-way join ~20x more often than it needed to.
	``frappe.request_cache`` is cleared per request and per background job, so a purity edit
	is picked up by the next one.

	Callers that already know the whole item set up front should call
	:func:`prefetch_purity_percentages` once first -- it collapses the per-item misses into a
	single query and leaves this function's cache warm for every later caller in the request.
	"""
	if not item:
		return

	purity_percentage = _fetch_purity_percentages([item])

	if not purity_percentage:
		return

	return purity_percentage[0][1]


def prefetch_purity_percentages(items):
	"""Warm :func:`get_purity_percentage`'s request cache for many items in one query.

	Behaviour-neutral by construction: this writes nothing but the values
	``get_purity_percentage`` would have computed itself, so every caller keeps calling that
	function and keeps getting the same answer -- it just stops paying one round trip per
	distinct item. Nothing here changes what is returned, only how many queries it took.

	Three details keep it exact:

	- The cache is keyed by the *undecorated* function and by the positional-args tuple (see
	  ``frappe/utils/caching.py``), hence ``__wrapped__`` and ``(item,)``. If that internal
	  ever changes shape, the primed entries simply never match and callers fall back to the
	  real per-item query -- slower, never wrong.
	- Items with no Metal Purity row are primed as ``None``, because that is what
	  ``get_purity_percentage`` returns for them. Leaving them out would send each one back to
	  the database for the same answer.
	- First row wins, mirroring ``get_purity_percentage``'s ``[0]``, so a malformed duplicate
	  attribute row resolves the same way through either entry point.

	Outside a request/job there is no cache to warm, so this is a no-op.
	"""
	items = {item for item in items if item}
	if not items:
		return

	cache = getattr(frappe.local, "request_cache", None)
	if cache is None:
		return

	found = {}
	for item, purity_percentage in _fetch_purity_percentages(sorted(items)):
		found.setdefault(item, purity_percentage)

	store = cache[getattr(get_purity_percentage, "__wrapped__", get_purity_percentage)]
	for item in items:
		# setdefault: never clobber a value this request already resolved.
		store.setdefault((item,), found.get(item))
