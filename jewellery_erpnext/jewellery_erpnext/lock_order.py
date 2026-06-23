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
		get = row.get if isinstance(row, dict) else (lambda f, d=None: getattr(row, f, d))
		return stock_lock_key(
			get("item_code"), get(warehouse_attr), get(batch_attr)
		)

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
		get = row.get if isinstance(row, dict) else (lambda f, d=None: getattr(row, f, d))
		item_code = get("item_code")
		for attr in warehouse_attrs:
			pairs.append((item_code, get(attr)))
	return lock_bins(pairs)


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
