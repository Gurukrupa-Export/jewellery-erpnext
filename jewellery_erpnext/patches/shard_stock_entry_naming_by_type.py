"""GATED shard of Stock Entry naming onto per-(company x stock_entry_type) counters.

Problem
-------
On ``kggk-prod`` **100%** of Stock Entries name off the single ``tabSeries`` row
``MAT-STE-``. 101 Stock Entry Document Naming Rules exist and are all *enabled*, but every
one of them binds to a company that either is not on this site (``Sadguru Diamond``,
``Sadguru Hallmarking Centre``, ``Gurukrupa Export Private Limited``) or to a
``stock_entry_type`` that is never used (the 5 ``KGJPL-SE-*`` Customer-Goods rules, all
still at ``counter = 0``). So no rule ever matches and every Stock Entry falls back to the
shared naming series.

That one row is the dominant source of the production ``1213`` deadlocks: 11 of the 20
deadlock Error Logs captured 3-8 Sep 2026 died on

    SELECT `current` FROM `tabSeries` WHERE name = 'MAT-STE-' FOR UPDATE

reached from ``doc_events.stock_entry.prelock_bins`` -> ``lock_order.preallocate_series``.

Why sharding fixes it
---------------------
``DocumentNamingRule.apply()`` takes its counter with
``frappe.db.get_value(..., "counter", for_update=True)`` on the **rule's own row**, and its
``prefix`` carries no ``#`` — so a rule-named doc touches ``tabSeries`` not at all. Creating
one rule per (company x type) therefore moves ~10.9k documents off a single hot row onto
one row per type, and unrelated flows stop contending with each other entirely.
``lock_order.preallocate_series_for_docs`` already pins Document Naming Rule counters in
sorted order (the F-003 fix), so the sharded counters stay pre-lockable in canonical order.

Why this is a GATED, MANUAL patch (NOT wired into patches.txt)
-------------------------------------------------------------
1. **Business sign-off**: new Stock Entry numbers change shape from ``MAT-STE-######`` to
   ``KGJPL-SE-<type>-<yy>-#####``. Existing documents are never renamed.
2. **Duplicate-name risk**: a rule counter is a single integer on the rule row. This patch
   seeds each counter to ``GREATEST(current, max existing suffix for that prefix stem)`` so
   it can only ever move forward and cannot collide with an existing primary key.

``execute()`` therefore intentionally no-ops. Run it explicitly, dry-run FIRST:

    bench --site <site> execute jewellery_erpnext.patches.shard_stock_entry_naming_by_type.shard
    # review the per-type report, then:
    bench --site <site> execute jewellery_erpnext.patches.shard_stock_entry_naming_by_type.shard \
        --kwargs "{'confirm': True}"

Every defined Stock Entry Type is covered by default (``all_types=True``); a type left
uncovered would silently fall back to the shared row the first time it is used. A type
created *later* is covered automatically by the ``Stock Entry Type`` ``after_insert`` hook
(:func:`ensure_rules_for_type`), so the shard cannot decay over time.

Reversible with ``rollback()`` below: it disables ONLY the rules recorded at creation time,
so pre-existing rules that happen to share the company prefix are never touched. Counters
are left as seeded (forward-only, safe).
"""

import json
import re

import frappe

_DOCTYPE = "Stock Entry"
#: ``tabDefaultValue`` key recording the EXACT rules this patch created, per company.
#: ``rollback()`` operates only on these — never on a prefix match, which would sweep up
#: pre-existing rules that merely share the company prefix.
_CREATED_KEY = "shard_se_naming_created"
#: ``tabDefaultValue`` key holding the explicit lifecycle state, per company.
_STATE_KEY = "shard_se_naming_state"
_NOT_APPLIED = "NOT_APPLIED"
_ACTIVE = "ACTIVE"
_ROLLED_BACK = "ROLLED_BACK"
_PREFIX_DIGITS = 5
_PRIORITY = 0
#: Abbreviations that cannot be harvested from an existing rule for the same type.
_ABBR_OVERRIDES = {
	"Repack-Gemstone Conversion": "RGC",
}


def _created_key(company):
	return f"{_CREATED_KEY}::{company}"


def _state_key(company):
	return f"{_STATE_KEY}::{company}"


def _get_state(company):
	"""Lifecycle state for ``company``: NOT_APPLIED / ACTIVE / ROLLED_BACK.

	Kept EXPLICIT rather than inferred from "a creation record exists", because the record
	survives a rollback by design (it is what makes rollback exact). Inferring from it left a
	rolled-back site still reporting as sharded, so the new-type hook kept creating active
	rules on it.
	"""
	return frappe.db.get_default(_state_key(company)) or _NOT_APPLIED


def _set_state(company, state):
	frappe.db.set_default(_state_key(company), state)


def _get_created(company):
	"""Rule names this patch previously created for ``company`` (``None`` if it never ran)."""
	raw = frappe.db.get_default(_created_key(company))
	if not raw:
		return None
	try:
		return list(json.loads(raw))
	except (ValueError, TypeError):
		return None


def _record_created(company, names):
	"""Append ``names`` to the recorded set for ``company`` (idempotent, order-stable)."""
	merged = list(dict.fromkeys((_get_created(company) or []) + list(names)))
	frappe.db.set_default(_created_key(company), json.dumps(merged))
	return merged


def _default_company():
	companies = frappe.get_all("Company", pluck="name")
	if len(companies) == 1:
		return companies[0]
	frappe.throw(
		f"{len(companies)} companies on this site — pass company explicitly: "
		f"{companies}"
	)


def _rule_rows(active_only=False):
	"""Every Stock Entry naming rule, with its company/type conditions and a total condition
	count so a rule carrying EXTRA restrictions can be told apart from a plain one.

	``active_only`` filters out disabled rules. Coverage decisions MUST pass it: a disabled
	rule names nothing, so treating it as coverage silently leaves that type on the shared
	``MAT-STE-`` row — and, after a rollback, makes the whole shard un-reappliable.
	"""
	return frappe.db.sql(
		f"""
		SELECT r.name, r.disabled,
		       COUNT(*)                                                    AS n_conditions,
		       MAX(CASE WHEN c.field = 'company'           THEN c.value END) AS company,
		       MAX(CASE WHEN c.field = 'stock_entry_type'  THEN c.value END) AS setype
		FROM `tabDocument Naming Rule` r
		JOIN `tabDocument Naming Rule Condition` c ON c.parent = r.name
		WHERE r.document_type = %s {"AND r.disabled = 0" if active_only else ""}
		GROUP BY r.name
		""",
		(_DOCTYPE,),
		as_dict=True,
	)


def _rule_map(active_only=False):
	"""``{(company, stock_entry_type): rule_name}``. See :func:`_rule_rows` for ``active_only``."""
	rows = _rule_rows(active_only=active_only)
	return {(r.company, r.setype): r.name for r in rows if r.company and r.setype}


def _coverage(company, reenablable=()):
	"""Classify each ``stock_entry_type`` for ``company`` as covered or conflicting.

	``reenablable`` names rules THIS patch created that a previous ``rollback()`` disabled.
	They are the expected rolled-back state, not a conflict a human must adjudicate — the
	caller re-enables them — so they are excluded from the conflict report. Without this,
	restoring a rolled-back shard reports every restored type as broken.

	Returns ``(covered, conflicts)``. ``covered`` maps type -> rule name; ``conflicts`` lists
	``(type, reason, rule)`` for everything the planner must NOT silently skip.

	COVERAGE IS DECIDED BY ASKING FRAPPE, NOT BY INSPECTING CONDITIONS
	-----------------------------------------------------------------
	A rule's conditions carry an OPERATOR (``=``, ``!=``, ``>``, ``<``, ``>=``, ``<=``) and
	rules are evaluated in ``priority desc`` order. Reconstructing that from
	``(field, value)`` pairs got it wrong: a rule conditioned ``company != X`` read as
	``company = X``, and a higher-priority generic rule that actually wins was invisible.

	So the decision is delegated to :func:`lock_order.document_naming_rule_for_doc`, which
	resolves the rule through frappe's own ``get_doctype_map(filters={"disabled": 0},
	order_by="priority desc")`` + ``evaluate_filters`` — the very code path ``set_new_name``
	takes. Whatever it returns for a stub of this ``(company, type)`` IS the rule that will
	name the document. The structural checks below survive only to explain WHY a type is not
	covered; they never grant coverage.
	"""
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		document_naming_rule_for_doc,
	)

	reenablable = set(reenablable or ())
	rows = [r for r in _rule_rows() if r.company == company and r.setype]

	# Structural facts per type, used for conflict REASONS and for duplicate detection.
	by_type = {}
	for r in rows:
		by_type.setdefault(r.setype, []).append(r)

	covered, conflicts = {}, []
	for setype, candidates in sorted(by_type.items()):
		active = [r for r in candidates if not r.disabled]
		ours_disabled = [r for r in candidates if r.disabled and r.name in reenablable]

		# A rule this patch created that a previous rollback() disabled is the expected
		# rolled-back state, not a conflict — the caller re-enables it. It must also count as
		# coverage, or _plan would mint a duplicate rule beside it.
		if not active and ours_disabled:
			covered[setype] = ours_disabled[0].name
			continue

		if len(active) > 1:
			# Which one wins depends on priority and creation order, so the prefix is
			# unpredictable and a later priority edit silently changes it. Never "covered".
			for r in active[1:]:
				conflicts.append(
					(setype, "multiple active rules claim this type", r.name)
				)
			continue

		if not active:
			for r in candidates:
				conflicts.append((setype, "rule exists but is DISABLED", r.name))
			continue

		rule = active[0]
		if rule.n_conditions != 2:
			conflicts.append(
				(
					setype,
					f"rule has {rule.n_conditions} conditions (expected 2)",
					rule.name,
				)
			)
			continue

		# The authoritative check: does frappe actually pick this rule for this pair?
		stub = frappe.new_doc(_DOCTYPE)
		stub.company = company
		stub.stock_entry_type = setype
		effective = document_naming_rule_for_doc(stub)
		if effective != rule.name:
			conflicts.append(
				(
					setype,
					f"frappe resolves {effective or 'no rule'}, not this one",
					rule.name,
				)
			)
			continue

		covered[setype] = rule.name
	return covered, conflicts


def _harvest_abbreviations():
	"""``{stock_entry_type: abbr}`` learned from existing rules of ANY company.

	Existing prefixes follow ``<COMPANY_ABBR>-SE-<TYPE_ABBR>-.YY.-`` (e.g.
	``SHC-SE-MTWO-.YY.-``), so the site's own convention is reused rather than invented.
	A type whose rules disagree on the abbreviation is left out and must be overridden.
	"""
	rows = frappe.db.sql(
		"""
		SELECT r.prefix,
		       MAX(CASE WHEN c.field = 'stock_entry_type' THEN c.value END) AS setype
		FROM `tabDocument Naming Rule` r
		JOIN `tabDocument Naming Rule Condition` c ON c.parent = r.name
		WHERE r.document_type = %s
		GROUP BY r.name
		""",
		(_DOCTYPE,),
		as_dict=True,
	)
	found = {}
	for r in rows:
		m = re.match(r"^(.+?)-SE-(.+?)-\.", r.prefix or "")
		if m and r.setype:
			found.setdefault(r.setype, set()).add(m.group(2))
	return {t: next(iter(a)) for t, a in found.items() if len(a) == 1}


def _derive_abbr(setype):
	"""Initials of the significant words, matching the existing convention
	(``Repack-Metal Conversion`` -> ``RMC``)."""
	words = re.findall(r"[A-Za-z0-9]+", setype or "")
	return "".join(w[0] for w in words).upper()[:8] or "SE"


def _max_existing_suffix(stem):
	"""Highest numeric suffix already present on a Stock Entry whose name starts with
	``stem``. Zero when the prefix has never been used."""
	if not stem:
		return 0
	row = frappe.db.sql(
		"""
		SELECT MAX(CAST(REGEXP_SUBSTR(`name`, '[0-9]+$') AS UNSIGNED))
		FROM `tabStock Entry`
		WHERE `name` LIKE %s AND `name` REGEXP '[0-9]+$'
		""",
		(stem + "%",),
	)
	return int(row[0][0] or 0)


def _historical_counts():
	"""``{stock_entry_type: document count}``, always computed.

	Fetched independently of the type list so an ``all_types`` run still reports the true
	blast radius. Reporting 0 for every type would tell the operator that nothing is on the
	shared row, which is the opposite of the truth and defeats the point of the dry run.
	"""
	rows = frappe.db.sql(
		"SELECT stock_entry_type t, COUNT(*) c FROM `tabStock Entry` GROUP BY 1 ORDER BY c DESC",
		as_dict=True,
	)
	return {r.t: r.c for r in rows if r.t}


def _plan(company, all_types=False, reenablable=()):
	"""Build the list of rules to create, plus the conflicts a human must resolve.

	Returns ``(plan, skipped, conflicts)``. Never touches the database.
	``reenablable`` — see :func:`_coverage`.
	"""
	covered, conflicts = _coverage(company, reenablable=reenablable)
	abbrs = _harvest_abbreviations()
	abbrs.update(_ABBR_OVERRIDES)

	counts = _historical_counts()
	if all_types:
		types = frappe.get_all("Stock Entry Type", pluck="name")
		# Keep the busiest types first so the dry-run report leads with what matters.
		types = sorted(types, key=lambda t: -counts.get(t, 0))
	else:
		types = [t for t, _c in sorted(counts.items(), key=lambda kv: -kv[1])]

	company_abbr = frappe.db.get_value("Company", company, "abbr") or "CO"
	# Prefixes already in use anywhere — a new rule must never reuse one.
	taken = set(
		frappe.db.sql_list(
			"SELECT prefix FROM `tabDocument Naming Rule` WHERE prefix IS NOT NULL"
		)
	)

	conflicted = {t for t, _reason, _rule in conflicts}
	plan, skipped = [], []
	for setype in types:
		if setype in covered:
			skipped.append((setype, "active rule already covers it"))
			continue
		if setype in conflicted:
			# Neither covered nor safe to create alongside — a human must resolve it.
			continue
		abbr = abbrs.get(setype) or _derive_abbr(setype)
		prefix = f"{company_abbr}-SE-{abbr}-.YY.-"
		if prefix in taken:
			# Disambiguate rather than silently shadowing another company's prefix.
			n = 2
			while f"{company_abbr}-SE-{abbr}{n}-.YY.-" in taken:
				n += 1
			prefix = f"{company_abbr}-SE-{abbr}{n}-.YY.-"
		taken.add(prefix)
		stem = prefix.split(".")[0]
		plan.append(
			{
				"setype": setype,
				"prefix": prefix,
				"seed_to": _max_existing_suffix(stem),
				"docs": counts.get(setype, 0),
			}
		)
	return plan, skipped, conflicts


def shard(company=None, confirm=False, all_types=True):
	"""Dry-run (default) or apply the per-type Stock Entry naming shard.

	confirm=False   -> print the plan, change nothing.
	confirm=True    -> create one Document Naming Rule per uncovered stock_entry_type.
	all_types=True  -> cover EVERY defined Stock Entry Type (the default). Leaving a defined
	                   type uncovered means the first document of that type silently falls
	                   back to the shared MAT-STE- row, reintroducing the contention this
	                   patch exists to remove. Every unused prefix seeds to 0, so covering
	                   them is free. Pass all_types=False only to inspect the subset that
	                   has historical documents.
	"""
	company = company or _default_company()
	state = _get_state(company)

	# Rules this patch created and a previous rollback() disabled. Re-enabling them is what
	# makes shard -> rollback -> shard restore the earlier state instead of dead-ending: they
	# already hold the right prefixes and counters, so recreating them is neither possible
	# (prefix taken) nor desirable (the counter would restart). Resolved BEFORE planning so
	# they are not also reported as conflicts.
	recorded = _get_created(company) or []
	to_reenable = [
		r
		for r in recorded
		if frappe.db.exists("Document Naming Rule", r)
		and frappe.db.get_value("Document Naming Rule", r, "disabled")
	]

	plan, skipped, conflicts = _plan(
		company, all_types=all_types, reenablable=to_reenable
	)

	print(f"[shard] company={company!r}  confirm={confirm}  all_types={all_types}")
	print(f"[shard] current state: {state}")
	if skipped:
		print(
			f"[shard] {len(skipped)} type(s) already covered by an active rule — untouched."
		)
	if to_reenable:
		print(
			f"[shard] {len(to_reenable)} previously created rule(s) are DISABLED and will be re-enabled."
		)
	if conflicts:
		print(
			f"\n[shard] {len(conflicts)} CONFLICT(S) — these types are NOT covered and NOT safe to\n"
			f"[shard] auto-create alongside. Resolve by hand, then re-run:"
		)
		for setype, reason, rule in conflicts:
			print(f"    {setype[:44]:<45} {reason:<42} ({rule})")

	if not plan and not to_reenable:
		if conflicts:
			# NOT "nothing to do": a conflicted type is excluded from the plan, so an empty
			# plan here means the only outstanding work is a conflict a human must resolve.
			# Saying "every type already has an active rule" would be a flat lie, and on
			# confirm=True this path must still fail closed (checked below).
			print(
				f"\n[shard] No rules to create, but {len(conflicts)} conflict(s) remain "
				f"UNRESOLVED — those types still fall back to MAT-STE-."
			)
		else:
			print(
				"\n[shard] Nothing to do: every stock_entry_type already has an active rule."
			)
			return

	if not plan:
		print("\n[shard] No new rules needed — only re-enabling.")

	if plan:
		print(f"\n[shard] {len(plan)} rule(s) to create:\n")
		print(f"{'stock_entry_type':<45} {'prefix':<28} {'docs':>7} {'seed':>6}")
		for p in plan:
			print(
				f"{p['setype'][:44]:<45} {p['prefix']:<28} {p['docs']:>7} {p['seed_to']:>6}"
			)

		moved = sum(p["docs"] for p in plan)
		print(
			f"\n[shard] {moved} historical Stock Entries of these types are on the shared "
			f"MAT-STE- row today;"
		)
		print(f"[shard] new ones would spread across {len(plan)} counters.")

	if not confirm:
		print(
			"\n[shard] DRY-RUN only — nothing changed. Re-run with confirm=True to apply."
		)
		return

	if conflicts:
		# FAIL CLOSED. Applying while a type is unresolved would mark the company ACTIVE
		# while that type still falls back to MAT-STE- — and ACTIVE is what
		# sharded_companies() and repair_missing_rules() trust. There is deliberately no
		# override: ACTIVE must always mean fully covered.
		frappe.throw(
			f"Resolve all {len(conflicts)} Document Naming Rule conflict(s) before applying "
			f"the Stock Entry naming shard for {company!r}. Re-run the dry run to list them; "
			f"nothing has been changed."
		)

	for rule in to_reenable:
		frappe.db.set_value(
			"Document Naming Rule", rule, "disabled", 0, update_modified=False
		)

	created_names = []
	for p in plan:
		doc = frappe.get_doc(
			{
				"doctype": "Document Naming Rule",
				"document_type": _DOCTYPE,
				"priority": _PRIORITY,
				"prefix": p["prefix"],
				"prefix_digits": _PREFIX_DIGITS,
				"counter": p["seed_to"],  # forward-only; 0 for an unused prefix
				"disabled": 0,
				"conditions": [
					{"field": "company", "condition": "=", "value": company},
					{
						"field": "stock_entry_type",
						"condition": "=",
						"value": p["setype"],
					},
				],
			}
		)
		doc.insert(ignore_permissions=True)
		created_names.append(doc.name)

	# Record the EXACT names before committing, so rollback() never has to guess.
	all_recorded = _record_created(company, created_names)
	_set_state(company, _ACTIVE)
	frappe.clear_cache(doctype=_DOCTYPE)
	frappe.cache_manager.clear_doctype_map("Document Naming Rule", _DOCTYPE)
	frappe.db.commit()
	print(
		f"\n[shard] APPLIED: created {len(created_names)} rule(s), re-enabled "
		f"{len(to_reenable)}. New Stock Entries will name off "
		f"{frappe.db.get_value('Company', company, 'abbr')}-SE-<type>-<yy>-#####."
	)
	print(f"[shard] Recorded {len(all_recorded)} rule name(s); state -> {_ACTIVE}.")
	# No conflict summary here: apply is unreachable while any conflict remains (see the
	# fail-closed throw above), so reaching this line means coverage is complete.


def rollback(company=None):
	"""Disable ONLY the rules this patch created, so naming falls back to MAT-STE-.

	Counters are left as seeded (forward-only, so re-enabling can never collide).

	Operates strictly on the names recorded by :func:`shard`. It deliberately does NOT fall
	back to matching on the company prefix: on kggk-prod that predicate also matches five
	pre-existing ``KGJPL-SE-*`` Customer-Goods rules created months earlier, which this
	patch must never touch. With no record, it refuses and prints what it would have hit.
	"""
	company = company or _default_company()
	recorded = _get_created(company)

	if recorded is None:
		company_abbr = frappe.db.get_value("Company", company, "abbr") or "CO"
		suspects = [
			(rule, frappe.db.get_value("Document Naming Rule", rule, "prefix") or "")
			for (co, _t), rule in _rule_map().items()
			if co == company
		]
		suspects = [s for s in suspects if s[1].startswith(f"{company_abbr}-SE-")]
		print(
			f"[shard] REFUSING to roll back: no record of rules created for {company!r}.\n"
			f"[shard] Either shard() never ran here, or it ran before this patch recorded\n"
			f"[shard] its output. {len(suspects)} rule(s) share the {company_abbr}-SE- prefix,\n"
			f"[shard] but SOME MAY PREDATE THIS PATCH — disable the right ones by hand:"
		)
		for rule, prefix in sorted(suspects, key=lambda s: s[1]):
			created = frappe.db.get_value("Document Naming Rule", rule, "creation")
			print(f"    {rule:<14} {prefix:<26} created {str(created)[:19]}")
		return

	disabled, missing = 0, 0
	for rule in recorded:
		if not frappe.db.exists("Document Naming Rule", rule):
			missing += 1
			continue
		frappe.db.set_value(
			"Document Naming Rule", rule, "disabled", 1, update_modified=False
		)
		disabled += 1

	# The recorded names are deliberately KEPT — they are what lets shard() re-enable exactly
	# these rules later. The lifecycle state, not the record's existence, is what marks the
	# company as no longer sharded.
	_set_state(company, _ROLLED_BACK)
	frappe.clear_cache(doctype=_DOCTYPE)
	frappe.cache_manager.clear_doctype_map("Document Naming Rule", _DOCTYPE)
	frappe.db.commit()
	print(f"[shard] Rolled back: disabled {disabled} rule(s) for {company!r}.")
	if missing:
		print(f"[shard] {missing} recorded rule(s) no longer exist — skipped.")
	untouched = sum(1 for (co, _t) in _rule_map() if co == company) - disabled
	print(f"[shard] Left {untouched} pre-existing rule(s) for this company untouched.")
	print(f"[shard] State -> {_ROLLED_BACK}. Re-run shard(confirm=True) to restore.")


def sharded_companies():
	"""Companies whose Stock Entry naming is CURRENTLY sharded (state ``ACTIVE``).

	Keyed off the explicit lifecycle state, not the creation record — the record outlives a
	rollback on purpose, so using it here left the new-type hook armed on a rolled-back site.
	"""
	return [
		company
		for company in frappe.get_all("Company", pluck="name")
		if _get_state(company) == _ACTIVE
	]


def verify_shard(company=None, verbose=True):
	"""Report every Stock Entry Type without an active, full-coverage naming rule.

	The new-type hook is best-effort by design (a naming gap must never block creating a
	type), so a failure there is silent. This turns that into something checkable — run it
	from the scheduler or by hand. Returns ``{"state", "uncovered", "conflicts"}``.
	"""
	company = company or _default_company()
	covered, conflicts = _coverage(company)
	counts = _historical_counts()
	uncovered = sorted(
		(
			t
			for t in frappe.get_all("Stock Entry Type", pluck="name")
			if t not in covered
		),
		key=lambda t: -counts.get(t, 0),
	)
	state = _get_state(company)
	if verbose:
		# Uncovered and conflicting mean different things and are reported separately:
		# "uncovered" is what falls back to MAT-STE- right now; "conflicts" is the subset a
		# human must resolve before shard() will apply at all.
		print(f"[verify] company={company!r}  state={state}")
		print(f"[verify] covered by an active rule : {len(covered)}")

		print(
			f"\n[verify] NOT COVERED ({len(uncovered)}) — these fall back to MAT-STE-:"
		)
		if not uncovered:
			print("    (none)")
		for t in uncovered:
			print(f"    {t[:52]:<53} docs={counts.get(t, 0)}")

		print(
			f"\n[verify] CONFLICTS ({len(conflicts)}) — resolve before shard() will apply:"
		)
		if not conflicts:
			print("    (none)")
		for setype, why, rule in conflicts:
			print(f"    {setype[:40]:<41} {why:<48} ({rule})")

		if state == _ACTIVE and uncovered:
			print(
				"\n[verify] State is ACTIVE but some types fall back to MAT-STE- — "
				"run repair_missing_rules()."
			)
	return {"state": state, "uncovered": uncovered, "conflicts": conflicts}


def repair_missing_rules(company=None, confirm=False):
	"""Create the naming rules ``verify_shard`` reports as missing. Dry-run by default."""
	company = company or _default_company()
	if _get_state(company) != _ACTIVE:
		print(
			f"[repair] state is {_get_state(company)}, not {_ACTIVE} — refusing. "
			f"Run shard(confirm=True) first."
		)
		return
	return shard(company=company, confirm=confirm, all_types=True)


def ensure_rules_for_type(setype, company=None):
	"""Create the missing naming rule for ``setype`` in every already-sharded company.

	Without this, a Stock Entry Type added after the shard would have no rule and its first
	document would fall back to the shared ``MAT-STE-`` row — quietly reintroducing the
	contention. Only touches companies that were sharded, so a site that never ran this
	patch is unaffected. Does NOT commit: it runs inside the caller's transaction.

	Returns the created rule names.
	"""
	companies = [company] if company else sharded_companies()
	created = []
	for co in companies:
		plan, _skipped, _conflicts = _plan(co, all_types=True)
		for p in plan:
			if p["setype"] != setype:
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Document Naming Rule",
					"document_type": _DOCTYPE,
					"priority": _PRIORITY,
					"prefix": p["prefix"],
					"prefix_digits": _PREFIX_DIGITS,
					"counter": p["seed_to"],
					"disabled": 0,
					"conditions": [
						{"field": "company", "condition": "=", "value": co},
						{
							"field": "stock_entry_type",
							"condition": "=",
							"value": setype,
						},
					],
				}
			)
			doc.insert(ignore_permissions=True)
			_record_created(co, [doc.name])
			created.append(doc.name)
	if created:
		frappe.cache_manager.clear_doctype_map("Document Naming Rule", _DOCTYPE)
	return created


def on_stock_entry_type_insert(doc, method=None):
	"""``Stock Entry Type`` ``after_insert`` hook — keep the naming shard complete.

	Best-effort: a naming-rule gap must never block creating a Stock Entry Type.
	"""
	try:
		ensure_rules_for_type(doc.name)
	except Exception:
		frappe.log_error(
			title="shard_stock_entry_naming: could not create rule for new type"
		)


def execute():
	"""Migrate entrypoint intentionally no-ops — this is a gated manual migration.

	(Kept importable/safe so an accidental patches.txt wiring cannot flip production
	naming. Run ``shard`` explicitly after sign-off; see the module docstring.)"""
	frappe.logger("jewellery_erpnext").info(
		"shard_stock_entry_naming_by_type: gated manual patch; execute() no-ops. "
		"Run the `shard` function explicitly after sign-off."
	)
