"""Canonical lock-ordering helpers for jewellery_erpnext.

Deadlocks (MariaDB 1213) and lock-wait timeouts (1205) in this app come almost
entirely from *different code paths locking the same rows in different orders*.
The cure is a single canonical acquisition order, enforced through these helpers.

Canonical order (acquire in this sequence inside every multi-doctype write):

    Parent control row  ->  tabSeries  ->  tabBin  ->  Batch / SBB
        ->  Stock Reservation Entry  ->  Stock Ledger Entry
        ->  MOP Log  ->  Manufacturing Operation  ->  Manufacturing Work Order
        ->  stamping counter / Employee IR draft re-check (terminal)

Manufacturing Operation comes before Manufacturing Work Order because that is the order the
hottest existing path already uses: a Stock Entry's MOP Log bridge locks the operation
(``MOPLog.validate``) and ``stamp_snc_requirement`` then writes the work order.

RULE D -- the work-order lifecycle block (:func:`lock_manufacturing_operations` then
:func:`lock_work_orders`, see ``doc_events/current_operation_guard.py``) is taken by Employee IR
and Department IR saves, submits and cancels, before the document is named or its own rows are
locked. Every attempt starts with ``current_operation_guard.begin_attempt`` -- the first
statement of the controller's ``check_if_latest`` (an existing document) or ``before_insert`` (a
new one): a save or submit during the EOD sync or an open stock-reconciliation window is refused
there by plain reads, before any lock below. A NEW document takes the block in ``before_insert``,
i.e. before ``set_new_name`` locks the shared naming-series row, so no request ever waits on a
busy work order while holding that row. A save or submit of an EXISTING document takes it in its
controller's ``check_if_latest``, before Frappe loads the document's own rows FOR UPDATE. An
Employee IR Receive SUBMIT takes its Tree / Series / Bin pre-locks there first
(``employee_ir.take_receive_prelocks``) and the block right after them, so the Stock Entry order
Tree -> Series -> Bin -> MOP -> MWO holds; only when they were not taken early (warn mode) does
``on_submit_receive`` take both, in the same order. Cancels still lock their own rows
(``check_if_latest``) and their trees before the block.

RULE E -- the block is two-phase: every operation row first, then every work-order row, each
set sorted, taken once. A Department IR save adds its row operations' PREVIOUS operations to the
operation phase, because ``update_previous_mop_data`` writes them afterwards. An interactive
attempt waits for the whole block (both phases, and a re-entrant second pass) within ONE budget
(``current_operation_lock_wait``, 15 s): each statement waits for what is left of it, and once it
is spent the remaining rows are taken NOWAIT; background jobs keep the server's
``innodb_lock_wait_timeout`` per statement. While holding the block, never WAIT on another
document's rows: only non-waiting locking reads (``NOWAIT`` / ``SKIP LOCKED``: the terminal
Employee IR draft re-check and the confirmation of what the transaction's older snapshot reported)
are allowed.

Writes that follow the block must lock only the rows they change. A ``set_value`` / ``UPDATE`` with
a filter no index serves scans the whole table and, under REPEATABLE READ, keeps a next-key lock on
every row and gap it read until the transaction ends -- every other writer of that table waits, and
one that already holds a row there closes a deadlock cycle with the block. The cancels write
``tabMOP Log`` (by voucher), ``tabDepartment IR Operation`` / ``tabStock Entry Detail`` (by
operation) and ``tabEmployee IR`` / ``tabEmployee IR Operation`` (casting-tree stamps), none of
them indexed for those filters on production: they read the matching names with a plain read and
write by primary key (:func:`update_by_primary_key`).

Residual edges, by design (each resolves as an InnoDB 1213: one side rolls back and is retried):

* an Employee IR Receive's metal-injection / loss Stock Entries lock source Bins resolved only at
  that point (each Stock Entry's ``prelock_bins``), i.e. after the block; so does the
  Process Loss / injection cancel of an Employee Receive cancel (``prelock_bins_on_cancel``);
* Refining Entry and the work-order split write the work order before its operations;
* the terminal draft re-check is the last statement of ``on_update`` / ``on_submit``, but Frappe
  still runs ``save_version`` / ``on_change`` / the Server Scripts after it in the same
  transaction, holding the re-check's share / gap locks for those milliseconds;
* ``preallocate_series`` / ``lock_bins`` (the Receive pre-locks) are plain ``FOR UPDATE`` and wait
  the server default, outside the block's budget;
* a casting Employee Issue cancel cancels its tree's Stock Entries (``cancel_tree_stock_entries``,
  ``prelock_bins_on_cancel``) after the block;
* a REST ``PUT`` / ``PATCH`` of a saved Employee / Department IR loads the document
  ``for_update`` (its own parent and child rows) before ``check_if_latest`` runs the pre-locks and
  the block; a concurrent new document of the same work order can then deadlock with it.

Two rules every custom on_submit / hook must follow:

* **RULE A** — any loop that ends up locking ``tabBin`` rows must iterate a list
  sorted by ``(item_code, warehouse, batch_no)`` so two concurrent transactions
  touch shared Bins in the *same* sequence. Sort a *copied view*; never mutate
  ``self.items`` (row order is meaningful elsewhere).
* **RULE B** — acquire all the ``tabBin`` row locks a transaction needs *up front*
  with ``SELECT ... FOR UPDATE``, in sorted order, via :func:`lock_bins`. This
  removes the "shared read now, exclusive write later" lock-upgrade that turns a
  Series<->Bin interleaving into a deadlock cycle.

**THE STAMPING COUNTER IS TERMINAL.** ``Serial No.custom_stamping_no`` claims its number
from a ``tabSeries`` row (``doc_events.serial_no.reserve_stamping_sequence``). That is
nominally position 2, and the ONE path that mints a number -- Serial Number Creator
(``update_new_serial_no``) -- does so while already holding Bin locks, which looks like an
inversion. It is safe ONLY because it takes the counter LAST and then commits, so
concurrent submits queue on it but never form a cycle.

There used to be three such paths: ``set_stamping_no`` was a ``before_save`` hook on Serial
No, so Product Certification (``update_huid`` -> ``add_to_serial_no``), Job Card
(``create_serial_no``) and every desk edit minted numbers too. The hook is gone -- a
stamping number identifies an SNC-produced piece, so the SNC calls ``set_stamping_no``
explicitly and nothing else touches this counter. Fewer claimants only shortens the queue;
the ordering argument is unchanged.

RULE C -- mint the stamping number last. If you add a path that needs a Bin (or any
position 2-8 row) AFTER a Serial No save, pre-lock with :func:`prelock_stamping_series`
instead of relying on that ordering.

Deliberately NOT pre-locked by default: it is one site-wide row, so pinning it at the start
of a cascade would make every concurrent SNC submit queue on it for the cascade's whole
duration -- exactly the hot-row pathology ``patches/shard_stock_entry_naming_by_type.py``
was written to escape. Minting late holds it for milliseconds instead.

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


MANUFACTURING_OPERATION_LOCK_FIELDS = (
	"name",
	"manufacturing_work_order",
	"company",
	"department",
	"status",
	"department_ir_status",
	"operation",
	"employee",
	"subcontractor",
	"for_subcontracting",
	"department_issue_id",
	"department_receive_id",
	"employee_ir",
	"previous_mop",
	# compared with the transaction's snapshot after the wait: a row changed meanwhile means the
	# snapshot the side effects read from is older than the decision (see the guard's
	# _refuse_if_snapshot_older)
	"modified",
)

WORK_ORDER_LOCK_FIELDS = (
	"name",
	"docstatus",
	"company",
	"manufacturing_operation",
	"department",
)


def _lock_wait_clause(wait):
	"""SQL suffix for a locking read: ``None`` = server default, ``0`` = NOWAIT, ``n`` = WAIT n."""
	if wait is None:
		return ""
	wait = int(wait)
	return " NOWAIT" if wait <= 0 else f" WAIT {wait}"


def _lock_rows_by_name(table, fields, names, wait):
	"""Lock each named row of ``table`` with a primary-key ``SELECT ... FOR UPDATE``, one
	statement per row in sorted order, and return ``{name: row}`` from those locking reads.

	Primary-key equality on an existing row takes a record lock only (no gap lock). The rows
	returned ARE the locking reads, so they show the latest committed values even when the
	transaction's REPEATABLE READ snapshot is older -- callers must decide from these rows,
	never from a plain re-read. Missing names are simply absent from the result.

	``wait``: ``None`` (the server default), seconds (``0`` = NOWAIT), or a callable returning one
	of those for EACH statement -- how several statements share one deadline (the guard's lock
	budget, RULE E).

	A failed statement (lock wait timeout, NOWAIT conflict, deadlock) is re-raised with the name
	it was waiting for in ``exc.lock_row_name``, so the caller can say WHICH row is busy.
	"""
	columns = ", ".join(f"`{f}`" for f in fields)
	locked = {}
	for name in sorted({n for n in names if n}):
		suffix = _lock_wait_clause(wait() if callable(wait) else wait)
		try:
			rows = frappe.db.sql(
				f"SELECT {columns} FROM `{table}` WHERE name = %s FOR UPDATE{suffix}",
				(name,),
				as_dict=True,
			)
		except Exception as exc:
			try:
				exc.lock_row_name = name
			except Exception:
				pass  # no instance __dict__: the caller then names every candidate instead
			raise
		if rows:
			locked[name] = rows[0]
	return locked


def update_by_primary_key(
	doctype, filters, fieldname, value=None, *, update_modified=True
):
	"""``frappe.db.set_value(doctype, filters, fieldname, value)`` that locks only the rows it
	changes. Returns the names it updated (sorted).

	``set_value`` with a filter dict is ONE ``UPDATE ... WHERE <filters>``. When no index serves
	those filters, InnoDB scans the whole table and -- under REPEATABLE READ -- keeps a next-key
	lock on every row and gap it read until the transaction ends (RULE E: writes after the block).
	Here the matching names come from a plain (consistent, lock-free) read and the UPDATE goes by
	primary key, with the filters applied again so a row that stopped matching is left alone.

	A plain read does not see rows committed after this transaction's snapshot. Safe for a
	voucher's own rows (all written by its own submit, and the cancel holds the voucher). For the
	link clears before ``delete_doc`` (rows of cancelled or discarded documents naming the deleted
	operation) a row that became cancelled after the snapshot is missed here, and ``delete_doc``'s
	link check -- reading the same snapshot -- then refuses the delete with a LinkExistsError and
	the whole cancel rolls back: nothing is deleted while still linked; the user retries.
	"""
	names = sorted(
		set(
			frappe.db.get_values(
				doctype, filters, "name", pluck=True, order_by="name asc"
			)
		)
	)
	if names:
		frappe.db.set_value(
			doctype,
			{**filters, "name": ("in", names)},
			fieldname,
			value,
			update_modified=update_modified,
		)
	return names


def lock_manufacturing_operations(names, *, wait=None):
	"""Lock Manufacturing Operation rows (RULE D/E phase one) and return their fresh state."""
	return _lock_rows_by_name(
		"tabManufacturing Operation", MANUFACTURING_OPERATION_LOCK_FIELDS, names, wait
	)


def lock_work_orders(names, *, wait=None):
	"""Lock Manufacturing Work Order rows (RULE D/E phase two) and return their fresh state."""
	return _lock_rows_by_name(
		"tabManufacturing Work Order", WORK_ORDER_LOCK_FIELDS, names, wait
	)


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


def _mints_new_name(doc):
	"""True when ``doc`` has yet to be given a name, i.e. it will still ask a naming
	counter for one.

	``Document.insert()`` calls ``set_new_name()`` (and through it ``getseries()``, which
	takes the ``tabSeries`` row ``FOR UPDATE``) *before* ``run_before_save_methods()``, and
	the update path never names a doc at all. So by the time a before_save/before_submit
	hook such as ``stock_entry.prelock_bins`` sees a real doc, its name is already minted
	and its counter lock is either already held by this transaction (insert) or will never
	be taken by it (submit of an already-saved draft). Pre-locking there buys nothing and
	merely holds the shared counter row for the rest of the transaction — on a site whose
	Stock Entries all share one ``MAT-STE-`` ``tabSeries`` row, that turns a microsecond
	counter lock into a transaction-length one and makes every concurrent submit queue
	behind it (the dominant 1213 source in the Sep-2026 production Error Log).

	Only an unnamed doc still needs the pre-lock: the ``frappe.new_doc`` stubs
	:func:`series_stubs` builds for the Stock Entries a cascade mints *later*, while it
	already holds Bin locks — exactly the inversion this module exists to prevent.

	CALLERS MUST GATE THIS ON SHARDING
	----------------------------------
	While a doctype still names off ONE shared ``tabSeries`` row, the blanket pre-lock is
	*accidentally* covering its cascades: it pins the very row each nested doc would later
	ask for, making ``getseries()`` inside an ``on_submit`` cascade a free re-entrant no-op.
	Skipping it there would remove that cover and let the cascade take the shared row *while
	already holding Bin locks* — a new inversion.

	:func:`preallocate_series_for_docs` therefore consults this ONLY for docs governed by a
	Document Naming Rule, i.e. those on a per-type counter no unrelated transaction wants.
	A doc falling back to a shared ``tabSeries`` row keeps the unconditional pre-lock. That
	gate is per-doc and automatic, so deployment order does not matter: before
	``patches/shard_stock_entry_naming_by_type.py`` runs, the old behaviour holds unchanged;
	as rules appear, each type switches itself over.
	"""
	name = doc.get("name") if hasattr(doc, "get") else getattr(doc, "name", None)
	# frappe gives an unsaved client-side doc a "new-<doctype>-<hash>" placeholder name.
	return not name or str(name).startswith("new-")


def preallocate_series_for_docs(*docs):
	"""Pre-acquire the naming-counter lock (canonical position 2) for each given
	doc/new_doc, before any Bin lock is taken.

	A doc that already carries a name is skipped — but ONLY when a Document Naming Rule
	governs it, i.e. its counter is per-(company x type) and no unrelated transaction wants
	that row. A doc that falls back to a SHARED ``tabSeries`` row is always pre-locked, even
	when already named, because a nested doc minted later by an ``on_submit`` cascade will
	ask for that same shared row after Bin locks are held. This gate is what makes the
	optimisation safe to deploy before, during or after
	``patches/shard_stock_entry_naming_by_type.py``. See :func:`_mints_new_name`.

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
			# SHARDED: this doc names off its own per-(company x type) counter row, which
			# no unrelated transaction wants. An already-named doc will never increment it
			# again, so there is nothing to pin — skip it and stop paying for the lock.
			if _mints_new_name(d):
				dnr_names.add(dnr)
			continue
		# NOT SHARDED: this doc falls back to a `tabSeries` row shared with every other doc
		# of its doctype — including the nested ones an on_submit cascade mints while
		# already holding Bin locks. Keep the unconditional pre-lock so that row is still
		# taken BEFORE any Bin, exactly as before. See _mints_new_name's gating note.
		prefix = series_prefix_for_doc(d)
		if prefix:
			series_prefixes.append(prefix)

	for name in sorted(dnr_names):
		frappe.db.get_value("Document Naming Rule", name, "counter", for_update=True)
	preallocate_series(series_prefixes)


def prelock_stamping_series():
	"""EXPERIMENTAL (opt-in): pin the stamping counter at canonical position 2, before any
	Bin lock, for cascades that mint a ``Serial No.custom_stamping_no``.

	OFF by default, and the app is correct without it -- see RULE C in the module docstring.
	Uniqueness does NOT depend on this: ``reserve_stamping_sequence`` is atomic wherever it
	is called from. This only decides WHERE in the acquisition order the counter lock lands,
	i.e. it is purely a deadlock-ordering guard.

	TRADE-OFF (accepted by the operator who enables it): the stamping counter is a single
	site-wide row, so pinning it up front makes every concurrent Serial Number Creator /
	Product Certification submit queue on it for the whole cascade rather than for the few
	milliseconds around the mint. That is the same hot-row cost
	``shard_stock_entry_naming_by_type`` exists to avoid, so enable it only if the 1213 rate
	actually says to: ``site_config.json`` ``"prelock_stamping_series": 1``.

	Pins TODAY'S and TOMORROW'S prefix. The prefix is derived from the clock, so a cascade
	that starts at 23:59 on 31-Dec and mints after midnight resolves a DIFFERENT key than the
	one it pinned -- and would take that fresh row while holding Bins, the exact inversion
	this is meant to remove. The two keys collapse to one on 364 days a year.
	"""
	from frappe.utils import add_to_date, now_datetime

	from jewellery_erpnext.jewellery_erpnext.doc_events.serial_no import (
		_ensure_stamping_series_row,
		_has_stamping_no_field,
		stamping_prefix,
		stamping_series_key,
	)

	if not frappe.conf.get("prelock_stamping_series"):
		return
	if not _has_stamping_no_field():
		return

	now = now_datetime()
	keys = []
	for prefix in sorted(
		{stamping_prefix(now), stamping_prefix(add_to_date(now, days=1))}
	):
		key = stamping_series_key(prefix)
		# FOR UPDATE on a row that does not exist locks nothing, so the row has to be there
		# before preallocate_series can pin it.
		_ensure_stamping_series_row(key, prefix)
		keys.append(key)

	preallocate_series(keys)
