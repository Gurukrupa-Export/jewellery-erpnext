"""Canonical lock-ordering helpers for jewellery_erpnext.

Deadlocks (MariaDB 1213) and lock-wait timeouts (1205) in this app come almost
entirely from *different code paths locking the same rows in different orders*.
The cure is a single canonical acquisition order, enforced through these helpers.

Canonical order (acquire in this sequence inside every multi-doctype write):

    Parent control row  ->  tabSeries  ->  tabBin  ->  Batch / SBB
        ->  Stock Reservation Entry  ->  Stock Ledger Entry
        ->  MOP Log  ->  Manufacturing Operation

Two rules every custom on_submit / hook must follow:

* **RULE A** — any loop that ends up locking ``tabBin`` rows must iterate a list
  sorted by ``(item_code, warehouse, batch_no)`` so two concurrent transactions
  touch shared Bins in the *same* sequence. Sort a *copied view*; never mutate
  ``self.items`` (row order is meaningful elsewhere).
* **RULE B** — acquire all the ``tabBin`` row locks a transaction needs *up front*
  with ``SELECT ... FOR UPDATE``, in sorted order, via :func:`lock_bins`. This
  removes the "shared read now, exclusive write later" lock-upgrade that turns a
  Series<->Bin interleaving into a deadlock cycle.

These helpers are deliberately tiny and side-effect-free except for the row locks
they take (which release on the enclosing transaction's COMMIT/ROLLBACK, exactly
like every other Frappe row lock).
"""

import frappe

# Sentinel used so a missing/None warehouse or batch sorts deterministically
# (before any real value) instead of raising on ``None < str``.
_SORT_NULL = ""


def stock_lock_key(item_code, warehouse, batch_no=None):
	"""Return the canonical, None-safe sort/lock key for a stock row."""
	return (
		item_code or _SORT_NULL,
		warehouse or _SORT_NULL,
		batch_no or _SORT_NULL,
	)


def sorted_stock_rows(rows, warehouse_attr="warehouse", batch_attr="batch_no"):
	"""Return ``rows`` ordered by the canonical ``(item_code, warehouse, batch_no)``
	key, WITHOUT mutating the input list.

	``warehouse_attr`` selects which warehouse field drives the order for this loop
	(e.g. ``"t_warehouse"`` for inbound reservation, ``"s_warehouse"`` for issue).
	Rows may be Documents/child rows or plain dicts.
	"""

	def _key(row):
		get = (
			row.get if isinstance(row, dict) else (lambda f, d=None: getattr(row, f, d))
		)
		return stock_lock_key(get("item_code"), get(warehouse_attr), get(batch_attr))

	return sorted(rows, key=_key)


def lock_bins(pairs):
	"""Acquire ``tabBin`` row locks for every ``(item_code, warehouse)`` in ``pairs``,
	in canonical sorted order, with ``SELECT ... FOR UPDATE`` (RULE B).

	* De-duplicates pairs so each Bin is locked once.
	* Locks one row per statement, in sorted order, so InnoDB acquires the locks in
	  a deterministic sequence across all transactions (breaks reverse-order cycles).
	* Skips ``(item, warehouse)`` combinations that have no Bin row yet — there is
	  nothing to lock, and the row will be created (and locked) by the stock posting
	  that follows.

	Returns the list of locked Bin names (mostly useful for tests/logging).
	"""
	seen = set()
	ordered = []
	for item_code, warehouse in pairs:
		if not item_code or not warehouse:
			continue
		key = (item_code, warehouse)
		if key in seen:
			continue
		seen.add(key)
		ordered.append(key)

	ordered.sort()

	locked = []
	for item_code, warehouse in ordered:
		name = frappe.db.sql(
			"""
			SELECT name FROM `tabBin`
			WHERE item_code = %s AND warehouse = %s
			FOR UPDATE
			""",
			(item_code, warehouse),
		)
		if name:
			locked.append(name[0][0])
	return locked


def lock_bins_for_rows(rows, *warehouse_attrs):
	"""Convenience wrapper: lock the Bins for every ``(item_code, <warehouse>)`` found
	on ``rows`` across the given warehouse attributes (default both s/t warehouse).

	Example::

	    lock_bins_for_rows(self.items, "s_warehouse", "t_warehouse")
	"""
	if not warehouse_attrs:
		warehouse_attrs = ("s_warehouse", "t_warehouse")

	pairs = []
	for row in rows:
		get = (
			row.get if isinstance(row, dict) else (lambda f, d=None: getattr(row, f, d))
		)
		item_code = get("item_code")
		for attr in warehouse_attrs:
			pairs.append((item_code, get(attr)))
	return lock_bins(pairs)


def lock_items(item_codes):
	"""EXPERIMENTAL (F-004, opt-in): serialize concurrent Stock Entry submits that touch
	the same item by acquiring a ``FOR UPDATE`` lock on each distinct ``tabItem`` row, in
	sorted order, BEFORE stock posting (canonical position 0 -- broadest scope, so it can
	never invert against Series/Bin).

	This is the only app-level way to stop ERPNext core's own cross-voucher repost locks
	(``stock_ledger.py:1680`` future-SLE index-range gap lock, and ``:1374``
	``get_lazy_doc`` FOR UPDATE on OTHER vouchers' Stock Entry rows -- neither reachable by
	Bin-level ordering) from deadlocking against a concurrent submit's child-row inserts.
	Cross-host safe (a real DB row lock, unlike the filesystem ``conflict_lock``) and
	transaction-scoped (releases on COMMIT/ROLLBACK).

	TRADE-OFF (accepted by the operator who enables it): two submits touching the same
	item now serialize across ALL warehouses -- a real throughput cost under load, and it
	holds the Item master row for the submit's duration. **OFF by default**; enable per
	site with ``site_config.json`` ``"serialize_stock_submit_by_item": 1`` and A/B measure
	the deadlock rate before keeping it on. The F-004 collision is INFERRED (no holder-side
	deadlock capture exists on this system), so this MUST be validated by measurement, not
	assumed to help -- and it may worsen the naming-contention latency it stacks on top of.
	"""
	for item_code in sorted({c for c in item_codes if c}):
		frappe.db.sql(
			"SELECT name FROM `tabItem` WHERE name = %s FOR UPDATE", (item_code,)
		)


def preallocate_series(prefixes):
	"""Pre-acquire the ``tabSeries`` counter-row lock for each given prefix, in sorted
	order, *before* any Bin lock is taken (canonical position 2).

	This pins the otherwise-lazy ``getseries() FOR UPDATE`` to a fixed point in the
	acquisition order, so a transaction can't be caught holding a Bin while waiting
	on a series row that another transaction holds while waiting on that Bin.

	Only needed for doctypes still named from a shared sequential ``tabSeries`` row
	(e.g. Stock Entry, Stock Reservation Entry). Doctypes moved to ``hash`` or a
	sharded prefix take no series lock and need not be listed here. Existing rows are
	locked (not incremented); the real ``getseries()`` during insert re-locks the same
	row re-entrantly within the same transaction.
	"""
	for prefix in sorted({p for p in prefixes if p}):
		frappe.db.sql(
			"SELECT `current` FROM `tabSeries` WHERE name = %s FOR UPDATE",
			(prefix,),
		)


def series_prefix_for_doc(doc):
	"""Resolve the ``tabSeries`` row key that ``getseries()`` will lock for ``doc``'s
	naming series, WITHOUT incrementing or locking it — or ``None`` when the doctype
	is named by ``hash`` / a field / Prompt (no shared counter row to lock).

	Only ``naming_series:``-style autonames are resolved here. ``hash`` takes no series
	lock at all, and ``format:`` doctypes parse each ``{...}`` brace in isolation (a
	different key derivation handled by their own seeded per-prefix rows), so they are
	deliberately skipped rather than resolved incorrectly.

	The prefix is derived by replaying frappe's own ``parse_naming_series`` with a
	capturing number-generator, so ``.YYYY.`` / ``.MM.`` / fieldname / custom-parser
	parts resolve to exactly the key ``getseries`` would use during insert.
	"""
	from frappe.model.naming import get_default_naming_series, parse_naming_series

	meta = frappe.get_meta(doc.doctype)
	autoname = (meta.autoname or "").strip()
	if (
		autoname.startswith("naming_series:")
		or meta.get("naming_rule") == 'By "Naming Series" field'
	):
		key = (
			doc.get("naming_series") if hasattr(doc, "get") else None
		) or get_default_naming_series(doc.doctype)
		if not key:
			return None
		key = key + ".#####"
	else:
		# hash / field / Prompt / format: — no single shared counter to pre-lock here.
		return None

	captured = {}

	def _capture(prefix, digits):
		captured.setdefault("prefix", prefix)
		return "0" * digits

	try:
		parse_naming_series(key, doc.doctype, doc, number_generator=_capture)
	except Exception:
		# Naming resolution must never break a submit; pre-locking is best-effort.
		return None
	return captured.get("prefix")


def document_naming_rule_for_doc(doc):
	"""Resolve the *active* Document Naming Rule whose counter ``set_new_name`` will lock
	for ``doc`` (matched by document_type + conditions, i.e. company / stock_entry_type),
	WITHOUT incrementing it — or ``None`` when no rule governs this doc and it falls back
	to its naming series.

	A Document Naming Rule counter lives in the ``tabDocument Naming Rule`` row itself
	(not ``tabSeries``); when a rule matches, frappe's naming precedence
	(``set_naming_from_document_naming_rule`` runs *before* the naming-series path) uses
	it INSTEAD of the naming series. Pre-locking must therefore pin whichever of the two
	a doc actually uses. Resolution reuses frappe's own rule map + ``evaluate_filters`` so
	it picks exactly the rule frappe will pick at insert. Best-effort: any failure returns
	``None`` — pre-locking must never break a submit.
	"""
	try:
		from frappe.utils import evaluate_filters

		rules = frappe.cache_manager.get_doctype_map(
			"Document Naming Rule",
			doc.doctype,
			filters={"document_type": doc.doctype, "disabled": 0},
			order_by="priority desc",
		)
		for d in rules:
			rule = frappe.get_cached_doc("Document Naming Rule", d.name)
			if rule.conditions and not evaluate_filters(
				doc,
				[
					(rule.document_type, c.field, c.condition, c.value)
					for c in rule.conditions
				],
			):
				continue
			return rule.name
	except Exception:
		return None
	return None


def series_stubs(company, *stock_entry_types):
	"""Build one Stock Entry naming stub per distinct ``stock_entry_type`` (order-
	preserving dedupe), each carrying ``company`` + the type, for
	:func:`preallocate_series_for_docs`.

	Cascades that pre-lock BEFORE minting their nested Stock Entries must pass stubs
	that resolve the same naming counter the real nested SEs will lock. Since every
	active Stock Entry Document Naming Rule matches on (company, stock_entry_type), a
	blank ``frappe.new_doc("Stock Entry")`` matches NO rule and falls back to pinning
	the shared ``MAT-STE-`` tabSeries row — the wrong counter post-reshard. One stub
	per nested SE type pins each real per-(company x type) DNR counter (or, for a
	company with no rules, the naming-series fallback, which the blank new_doc's
	default ``naming_series`` keeps resolvable).

	Usage::

	    preallocate_series_for_docs(*series_stubs(self.company, "Repack", "Manufacture"))
	"""
	stubs = []
	for setype in dict.fromkeys(stock_entry_types):
		stub = frappe.new_doc("Stock Entry")
		stub.company = company
		stub.stock_entry_type = setype
		stubs.append(stub)
	return stubs


def preallocate_series_for_docs(*docs):
	"""Pre-acquire the naming-counter lock (canonical position 2) for each given
	doc/new_doc, before any Bin lock is taken.

	Respects frappe's naming precedence: when an active Document Naming Rule governs a
	doc, its ``tabDocument Naming Rule.counter`` row is the lock to pin — the
	naming-series ``tabSeries`` row is NOT used for that doc, so it is deliberately not
	locked (locking it would add needless contention on a row this doc never increments,
	the exact F-001 hot row we are relieving). Otherwise the naming-series ``tabSeries``
	row is pinned via :func:`series_prefix_for_doc` + :func:`preallocate_series`. This is
	the F-003 fix: the previously un-pinnable Document Naming Rule counter is now acquired
	in canonical order.

	Both kinds of naming-counter lock are acquired in one deterministic order (DNR rows
	sorted by name, then ``tabSeries`` rows sorted by prefix) so concurrent transactions
	take shared naming rows in the same sequence. Docs named by ``hash`` contribute no
	lock and are silently skipped. Re-entrant: the real ``set_new_name`` at insert
	re-locks the same row within the same transaction.

	Multi-type cascades pass one stub per distinct nested Stock Entry type via
	:func:`series_stubs` so EVERY nested type's naming counter is pinned up front
	(DNR names are deduped in a set and acquired in sorted order below).
	"""
	series_prefixes = []
	dnr_names = set()
	for d in docs:
		if d is None:
			continue
		dnr = document_naming_rule_for_doc(d)
		if dnr:
			dnr_names.add(dnr)
			continue
		prefix = series_prefix_for_doc(d)
		if prefix:
			series_prefixes.append(prefix)

	for name in sorted(dnr_names):
		frappe.db.get_value("Document Naming Rule", name, "counter", for_update=True)
	preallocate_series(series_prefixes)
