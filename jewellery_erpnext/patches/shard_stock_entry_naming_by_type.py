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


def _get_created_raw(company):
	"""Raw recorded entries — a mix of plain names (legacy) and identity dicts."""
	raw = frappe.db.get_default(_created_key(company))
	if not raw:
		return None
	try:
		return list(json.loads(raw))
	except (ValueError, TypeError):
		return None


def _get_created(company):
	"""Rule NAMES this patch previously created for ``company`` (``None`` if it never ran).

	Accepts both record shapes: the original plain-string list and the richer identity dicts
	written since — so an existing record is never stranded by the format change.
	"""
	entries = _get_created_raw(company)
	if entries is None:
		return None
	return [e["name"] if isinstance(e, dict) else e for e in entries]


def _created_identity(company, rule):
	"""The recorded identity for ``rule``, or ``None`` for a legacy string-only record."""
	for e in _get_created_raw(company) or []:
		if isinstance(e, dict) and e.get("name") == rule:
			return e
	return None


def _record_created(company, entries):
	"""Append ``entries`` to the record for ``company`` (idempotent, order-stable).

	Each entry may be a plain rule name or an identity dict
	``{name, company, stock_entry_type, prefix}``. The identity form is what lets
	:func:`_validate_recorded_rule` PROVE a rolled-back rule still represents the pair it was
	created for, instead of inferring it from whatever the rule now says.
	"""
	existing = _get_created_raw(company) or []
	seen = {e["name"] if isinstance(e, dict) else e for e in existing}
	merged = list(existing)
	for e in entries:
		name = e["name"] if isinstance(e, dict) else e
		if name not in seen:
			seen.add(name)
			merged.append(e)
	frappe.db.set_default(_created_key(company), json.dumps(merged))
	return [e["name"] if isinstance(e, dict) else e for e in merged]


def _validate_recorded_rule(rule, company, setype):
	"""Return a problem description if ``rule`` is not safe to re-enable, else ``None``.

	A rollback leaves the rule in place but disabled, and a disabled rule can be edited.
	Re-enabling on the strength of its NAME alone would trust whatever it now points at —
	possibly a different company, type or prefix. Everything that defines the rule's identity
	is therefore re-checked, and against the recorded identity when one was stored.
	"""
	if not frappe.db.exists("Document Naming Rule", rule):
		return "recorded rule no longer exists"

	doc = frappe.get_doc("Document Naming Rule", rule)
	if doc.document_type != _DOCTYPE:
		return f"document_type drifted to {doc.document_type!r}"
	if not (doc.prefix or "").strip():
		return "prefix is empty"

	conds = {(c.field, c.condition): c.value for c in doc.conditions}
	if len(doc.conditions) != 2:
		return f"has {len(doc.conditions)} conditions (expected 2)"
	if conds.get(("company", "=")) != company:
		return f"company condition drifted (expected {company!r})"
	if conds.get(("stock_entry_type", "=")) != setype:
		return f"stock_entry_type condition drifted (expected {setype!r})"

	identity = _created_identity(company, rule)
	if identity:
		for field, actual in (
			("stock_entry_type", setype),
			("prefix", doc.prefix),
		):
			if identity.get(field) and identity[field] != actual:
				return (
					f"{field} drifted from recorded {identity[field]!r} to {actual!r}"
				)
		return None

	# LEGACY record (a bare rule name, written before identities were stored): there is no
	# recorded type to compare against, so type drift cannot be PROVEN here. Deliberately do
	# NOT guess the type back from the prefix abbreviation: `_harvest_abbreviations` reads the
	# live rules, so a drifted rule poisons the expected abbreviation for OTHER types and the
	# check reports healthy rules as drifted — a false positive that would block a legitimate
	# restore, which is worse than the gap it closes. Such drift is instead caught by the
	# post-write verification in `_verify_or_rollback`, which re-resolves every type for real.
	# `_upgrade_record_identities` closes the gap permanently after one healthy apply.
	return None


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

	Only ``=`` conditions populate ``company``/``setype``. The operator is load-bearing: a rule
	conditioned ``company != X`` carries the same field and value as ``company = X``, so
	projecting on value alone made a NON-matching exclusion rule look like another exact
	candidate for X. Combined with the duplicate check below — which runs before frappe's
	resolver is consulted — that raised a false "multiple active rules claim this type", and
	since apply is fail-closed a false conflict BLOCKS the whole migration. Rules that do not
	bind by equality are not exact candidates; they remain visible to the authoritative paths
	(the resolver and :func:`ambiguous_at_same_priority`) and to :func:`_namespace_rows`.
	"""
	return frappe.db.sql(
		f"""
		SELECT r.name, r.disabled, r.prefix, r.prefix_digits, r.counter,
		       COUNT(*)                                                     AS n_conditions,
		       MAX(CASE WHEN c.field = 'company'          AND c.condition = '='
		                THEN c.value END)                                   AS company,
		       MAX(CASE WHEN c.field = 'stock_entry_type' AND c.condition = '='
		                THEN c.value END)                                   AS setype
		FROM `tabDocument Naming Rule` r
		JOIN `tabDocument Naming Rule Condition` c ON c.parent = r.name
		WHERE r.document_type = %s {"AND r.disabled = 0" if active_only else ""}
		GROUP BY r.name
		""",
		(_DOCTYPE,),
		as_dict=True,
	)


def _namespace_rows():
	"""EVERY active Stock Entry naming rule — no company filter, no operator filter.

	``tabStock Entry.name`` is ONE global namespace while each Document Naming Rule owns an
	INDEPENDENT counter, so "who else can mint names under this prefix?" is a global question.
	Answering it from the company-filtered candidate rows missed a same-prefix rule belonging to
	another company entirely: both counters would march through the same names and collide on
	the primary key.

	Deliberately unfiltered by operator too — a rule conditioned ``company != X`` is not an
	exact candidate, but it still OWNS its prefix and still mints names under it, so it must be
	counted as a namespace owner even though :func:`_rule_rows` excludes it from candidates.
	"""
	return frappe.get_all(
		"Document Naming Rule",
		filters={"document_type": _DOCTYPE, "disabled": 0},
		fields=["name", "prefix", "prefix_digits", "counter", "disabled"],
	)


def rule_priority(rule_name):
	"""Priority of a Document Naming Rule, 0 when unreadable."""
	return frappe.utils.cint(
		frappe.db.get_value("Document Naming Rule", rule_name, "priority")
	)


def ambiguous_at_same_priority(stub, rule, company):
	"""Active Stock Entry rules that ALSO match ``stub`` at the same priority as ``rule``.

	``document_naming_rule_for_doc`` returns the first candidate frappe would pick, but the
	underlying ordering is ``priority desc`` with no tie-breaker, so among equal-priority
	matches the winner is whatever the database happens to return first. Enumerating every
	matching rule at that priority is the only way to tell "this rule wins" from "this rule
	won the coin flip this time".
	"""
	try:
		from frappe.utils import evaluate_filters

		target = rule_priority(rule.name)
		out = []
		for r in frappe.get_all(
			"Document Naming Rule",
			filters={"document_type": _DOCTYPE, "disabled": 0, "priority": target},
			fields=["name"],
		):
			doc = frappe.get_cached_doc("Document Naming Rule", r.name)
			if doc.conditions and not evaluate_filters(
				stub,
				[
					(doc.document_type, c.field, c.condition, c.value)
					for c in doc.conditions
				],
			):
				continue
			out.append(r)
		return out
	except Exception:
		# Ambiguity detection must never break a dry run; a failure degrades to "no rivals".
		return []


def _namespace_problem(rule, all_rows):
	"""Return why ``rule``'s OUTPUT NAMESPACE is unsafe to accept as coverage, else ``None``.

	Rules this patch CREATES are protected twice: :func:`_plan` avoids every prefix already in
	use, and seeds ``counter`` to the historical maximum suffix. A PRE-EXISTING rule accepted
	as coverage got neither check — ``_rule_rows`` did not even select ``prefix``/``counter``,
	so it could not have.

	That matters because each Document Naming Rule owns an INDEPENDENT counter. Two rules
	sharing a prefix therefore march through the same names, and a rule whose counter sits
	below the highest name already issued under its prefix re-issues names that exist. Either
	way ``DocumentNamingRule.apply()`` mints the name with no existence check and ``db_insert``
	raises ``DuplicateEntryError``. Worse, the counter bump is rolled back with the failed
	request, so every retry re-mints the SAME colliding name — the pair stays wedged until a
	human intervenes. Report it; never auto-repair someone else's rule.
	"""
	prefix = (rule.get("prefix") or "").strip()
	if not prefix:
		return "rule has an empty prefix"

	digits = frappe.utils.cint(rule.get("prefix_digits"))
	if digits < 1:
		return f"rule has prefix_digits={rule.get('prefix_digits')!r} (expected >= 1)"

	sharers = [
		r.name
		for r in all_rows
		if r.name != rule.name and not r.disabled and (r.prefix or "").strip() == prefix
	]
	if sharers:
		return (
			f"prefix {prefix!r} is shared with {sharers[0]}"
			f"{f' (+{len(sharers) - 1} more)' if len(sharers) > 1 else ''} — "
			f"independent counters can mint the same name"
		)

	floor = _max_existing_suffix(prefix.split(".")[0])
	if frappe.utils.cint(rule.get("counter")) < floor:
		return (
			f"counter={rule.get('counter')} is behind the highest existing name "
			f"({floor}) for prefix {prefix!r} — would re-issue used names"
		)
	return None


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
	# Candidate rows are company- and equality-scoped; namespace ownership is neither.
	namespace_rows = _namespace_rows()

	# Structural facts per type, used for conflict REASONS and for duplicate detection.
	by_type = {}
	for r in rows:
		by_type.setdefault(r.setype, []).append(r)

	# EVERY defined type is resolved, not just those that already have an exact candidate.
	# A rule conditioned on company ALONE has no stock_entry_type, so it never appears in
	# `by_type` — yet it matches every Stock Entry of that company and, at a higher priority,
	# wins over an exact rule. Iterating only `by_type` made such a rule invisible: the
	# planner saw the type as missing, created a priority-0 exact rule, frappe kept resolving
	# the generic one, and the shard was marked ACTIVE anyway. Asking the resolver for every
	# type surfaces that in the DRY RUN, before anything is written.
	covered, conflicts = {}, []
	for setype in sorted(frappe.get_all("Stock Entry Type", pluck="name")):
		candidates = by_type.get(setype, [])
		active = [r for r in candidates if not r.disabled]
		ours_disabled = [r for r in candidates if r.disabled and r.name in reenablable]

		# A rule this patch created that a previous rollback() disabled is the expected
		# rolled-back state, not a conflict — the caller re-enables it. It must also count as
		# coverage, or _plan would mint a duplicate rule beside it. Its identity is validated
		# first: a disabled rule can be edited, and re-enabling it blindly would trust
		# whatever it now points at.
		if not active and ours_disabled:
			rule = ours_disabled[0]
			problem = _validate_recorded_rule(rule.name, company, setype)
			if problem:
				conflicts.append((setype, problem, rule.name))
			else:
				covered[setype] = rule.name
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
			if candidates:
				for r in candidates:
					conflicts.append((setype, "rule exists but is DISABLED", r.name))
				continue
			# No candidate at all. Genuinely missing -> plannable, UNLESS some other rule
			# (generic, or bound to a different type) already resolves for this pair.
			stub = frappe.new_doc(_DOCTYPE)
			stub.company = company
			stub.stock_entry_type = setype
			shadow = document_naming_rule_for_doc(stub)
			if shadow:
				conflicts.append(
					(
						setype,
						f"no exact rule, but frappe resolves {shadow} for this type",
						shadow,
					)
				)
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

		# Resolving to this rule proves WHICH rule wins, not that its output namespace is
		# safe. A pre-existing rule reaches here without ever having been through _plan's
		# prefix-avoidance and counter seeding, so validate that separately.
		problem = _namespace_problem(rule, namespace_rows)
		if problem:
			conflicts.append((setype, problem, rule.name))
			continue

		# Frappe orders candidates by `priority desc` with NO secondary key, so a tie between
		# two matching rules is resolved by whatever order the DB returns — stable within a
		# cached process, not stable across cache rebuilds. Accepting a coin-flip winner would
		# let the effective prefix change under us later, so require an unambiguous match.
		rivals = [
			r.name
			for r in ambiguous_at_same_priority(stub, rule, company)
			if r.name != rule.name
		]
		if rivals:
			conflicts.append(
				(
					setype,
					f"ambiguous: {rivals[0]} matches at the same priority ({rule_priority(rule.name)})",
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


def _upgrade_record_identities(company, covered):
	"""Rewrite legacy bare-name record entries as full identity dicts.

	Runs only once coverage has been VERIFIED, so what is recorded is known-good. After this,
	a later rollback→edit→reapply can be refused at the pre-write drift gate instead of
	relying on post-write verification, because each rule's intended (company, type, prefix)
	is finally written down. Entries that already carry an identity are left untouched.
	"""
	entries = _get_created_raw(company) or []
	rule_to_type = {rule: setype for setype, rule in covered.items()}
	upgraded, changed = [], 0
	for e in entries:
		if isinstance(e, dict):
			upgraded.append(e)
			continue
		setype = rule_to_type.get(e)
		if not setype or not frappe.db.exists("Document Naming Rule", e):
			upgraded.append(e)  # not ours to describe, or gone — leave as-is
			continue
		upgraded.append(
			{
				"name": e,
				"company": company,
				"stock_entry_type": setype,
				"prefix": frappe.db.get_value("Document Naming Rule", e, "prefix"),
			}
		)
		changed += 1
	if changed:
		frappe.db.set_default(_created_key(company), json.dumps(upgraded))
	return changed


def _verify_or_rollback(company):
	"""Re-resolve every Stock Entry Type after writing; roll back and throw unless all covered.

	Called while the rule inserts/re-enables are still UNCOMMITTED, so a failure leaves the
	site exactly as it was. The naming-rule map is cleared first: the resolver reads it, and a
	stale map would happily confirm the plan we just wrote instead of the current reality.
	"""
	frappe.cache_manager.clear_doctype_map("Document Naming Rule", _DOCTYPE)
	covered, conflicts = _coverage(company)
	missing = sorted(
		set(frappe.get_all("Stock Entry Type", pluck="name")) - set(covered)
	)
	if not missing and not conflicts:
		# Coverage is proven, so anything recorded without an identity can now be described
		# safely. Doing it here (and only here) means the record only ever gains identities
		# that were verified true at the time of writing.
		if _upgrade_record_identities(company, covered):
			print("[shard] Upgraded legacy rule record entries to full identities.")
		return

	frappe.db.rollback()
	frappe.cache_manager.clear_doctype_map("Document Naming Rule", _DOCTYPE)
	detail = []
	if missing:
		detail.append(f"{len(missing)} type(s) still uncovered: {missing[:8]}")
	if conflicts:
		detail.append(
			f"{len(conflicts)} conflict(s): {[(c[0], c[1]) for c in conflicts[:5]]}"
		)
	frappe.throw(
		"Stock Entry naming shard verification FAILED after writing — everything has been "
		"rolled back and the site is unchanged. " + "; ".join(detail)
	)


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
	to_reenable, drifted = [], []
	for r in recorded:
		if not frappe.db.exists("Document Naming Rule", r):
			continue
		if not frappe.db.get_value("Document Naming Rule", r, "disabled"):
			continue
		# Validate against the type the rule CURRENTLY claims; _coverage independently
		# validates against the type it is meant to cover, and reports drift as a conflict
		# (which fails the apply closed). This second check is defence in depth: a rule that
		# cannot prove its identity is never re-enabled, whatever else happens.
		setype = frappe.db.get_value(
			"Document Naming Rule Condition",
			{"parent": r, "field": "stock_entry_type"},
			"value",
		)
		problem = _validate_recorded_rule(r, company, setype)
		if problem:
			drifted.append((r, problem))
		else:
			to_reenable.append(r)

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
	if drifted:
		print(
			f"\n[shard] {len(drifted)} recorded rule(s) were EDITED since rollback and will NOT be\n"
			f"[shard] re-enabled — they no longer prove the pair they were created for:"
		)
		for rule, problem in drifted:
			print(f"    {rule:<16} {problem}")
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
			if confirm:
				# Nothing to write, but a confirmed run still VERIFIES. That makes ACTIVE
				# reflect reality when the company was already fully covered (by rules someone
				# else created, or an earlier run whose state was never set), and it is where
				# legacy record entries get upgraded to full identities.
				_verify_or_rollback(company)
				if state != _ACTIVE:
					_set_state(company, _ACTIVE)
					print(f"[shard] Coverage verified; state {state} -> {_ACTIVE}.")
				else:
					print("[shard] Coverage verified; state unchanged.")
				frappe.clear_cache(doctype=_DOCTYPE)
				frappe.db.commit()
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

	if drifted:
		# Reapply flow: "any edited/drifted? -> STOP / human review". A recorded rule that
		# was edited while disabled cannot be silently skipped and replaced either — the
		# operator must see what changed and decide, because the edit may have been deliberate.
		frappe.throw(
			f"{len(drifted)} recorded Document Naming Rule(s) were edited since rollback and "
			f"no longer match what this patch created: "
			f"{[(r, p) for r, p in drifted[:5]]}. Review them by hand (re-point or delete the "
			f"record) before re-applying; nothing has been changed."
		)

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

	created_entries = []
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
		# Record the full identity, not just the name, so a later re-enable can PROVE the
		# rule still represents this (company, type, prefix) rather than infer it.
		created_entries.append(
			{
				"name": doc.name,
				"company": company,
				"stock_entry_type": p["setype"],
				"prefix": p["prefix"],
			}
		)

	# Record the EXACT identities before committing, so rollback() never has to guess and
	# a later re-enable can PROVE what each rule was created for.
	all_recorded = _record_created(company, created_entries)

	# POST-WRITE VERIFICATION. Everything above is still uncommitted. Re-resolve every type
	# through frappe now that the rules actually exist, because a plan that looked safe is not
	# proof: a pre-existing higher-priority rule can still win over a rule we just created.
	# ACTIVE must mean "frappe really resolves every type to the intended rule", not "the
	# writes succeeded". Cache first — the resolver reads the doctype map.
	_verify_or_rollback(company)

	_set_state(company, _ACTIVE)
	frappe.clear_cache(doctype=_DOCTYPE)
	frappe.db.commit()
	print(
		f"\n[shard] APPLIED: created {len(created_entries)} rule(s), re-enabled "
		f"{len(to_reenable)}. New Stock Entries will name off "
		f"{frappe.db.get_value('Company', company, 'abbr')}-SE-<type>-<yy>-#####."
	)
	print(f"[shard] Recorded {len(all_recorded)} rule name(s); state -> {_ACTIVE}.")
	# No conflict summary here: apply is unreachable while any conflict remains (see the
	# fail-closed throw above), so reaching this line means coverage is complete.


def rollback(company=None, force=False):
	"""Disable ONLY the rules this patch created, so naming falls back to MAT-STE-.

	Counters are left as seeded (forward-only, so re-enabling can never collide).

	Refuses BEFORE touching anything if any recorded rule has drifted from what this patch
	created — someone repurposing a managed rule means disabling it would switch off
	configuration that is no longer ours. Pass ``force=True`` to proceed anyway: rollback is
	the emergency lever for undoing the shard, so it must stay reachable during an incident,
	but taking down a repurposed rule has to be a deliberate act rather than a side effect.

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

	# PREFLIGHT — read-only. Every recorded rule is checked before ANY write, so a drifted
	# rule stops the run with the site untouched rather than being disabled and reported
	# afterwards (by which point the change is already committed).
	targets, missing, repurposed = [], 0, []
	for rule in recorded:
		if not frappe.db.exists("Document Naming Rule", rule):
			missing += 1
			continue
		setype = frappe.db.get_value(
			"Document Naming Rule Condition",
			{"parent": rule, "field": "stock_entry_type"},
			"value",
		)
		problem = _validate_recorded_rule(rule, company, setype)
		if problem:
			repurposed.append((rule, problem))
		targets.append(rule)

	if repurposed and not force:
		print(
			f"[shard] {len(repurposed)} recorded rule(s) no longer match what this patch "
			f"created:"
		)
		for rule, problem in repurposed:
			print(f"    {rule:<16} {problem}")
		frappe.throw(
			f"REFUSING to roll back {company!r}: {len(repurposed)} recorded Document Naming "
			f"Rule(s) have been repurposed since this patch created them, so disabling them "
			f"would switch off configuration that is no longer ours. Nothing has been "
			f"changed. Review them, then re-run with force=True to disable them anyway."
		)

	disabled = 0
	for rule in targets:
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
	if repurposed:
		# Only reachable with force=True — the preflight above throws otherwise.
		print(
			f"[shard] WARNING: force=True disabled {len(repurposed)} rule(s) that no longer "
			f"match what this patch created — review before re-applying:"
		)
		for rule, problem in repurposed:
			print(f"    {rule:<16} {problem}")
	# Clamped: _rule_map() only sees rules carrying BOTH company and stock_entry_type
	# conditions, so a recorded rule shaped otherwise can make the subtraction go negative
	# and print a nonsense count.
	untouched = max(0, sum(1 for (co, _t) in _rule_map() if co == company) - disabled)
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

	Returns ``(created_rule_names, unresolved)`` where ``unresolved`` lists
	``(company, reason)`` for a company where this type could NOT be covered — either a
	conflict blocks it or the planner produced no row. Callers MUST surface that: silently
	returning an empty list left the type on the shared ``MAT-STE-`` row while the company
	still reported ``ACTIVE``.
	"""
	companies = [company] if company else sharded_companies()
	created, unresolved = [], []
	for co in companies:
		plan, _skipped, conflicts = _plan(co, all_types=True)

		# A conflicted type is deliberately excluded from `plan`, so an empty plan row is
		# NOT "nothing to do" — it means a human must resolve something first.
		blocking = [c for c in conflicts if c[0] == setype]
		if blocking:
			unresolved.append((co, f"{blocking[0][1]} ({blocking[0][2]})"))
			continue
		if not any(p["setype"] == setype for p in plan):
			unresolved.append((co, "planner produced no rule for this type"))
			continue

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
			# Record the FULL identity, exactly as shard() does. Recording a bare name here
			# gave hook-created rules strictly weaker drift protection than shard-created
			# ones: _validate_recorded_rule falls into its legacy branch and cannot prove the
			# rule still represents this pair. `p` already carries both fields.
			_record_created(
				co,
				[
					{
						"name": doc.name,
						"company": co,
						"stock_entry_type": setype,
						"prefix": p["prefix"],
					}
				],
			)
			created.append(doc.name)
	if created:
		frappe.cache_manager.clear_doctype_map("Document Naming Rule", _DOCTYPE)
	return created, unresolved


def on_stock_entry_type_insert(doc, method=None):
	"""``Stock Entry Type`` ``after_insert`` hook — keep the naming shard complete.

	Best-effort by design: a naming-rule gap must never block creating a Stock Entry Type.
	But "best-effort" must not mean "silent" — an unresolved type falls back to the shared
	``MAT-STE-`` row while the company still reports ACTIVE, so anything short of full
	success is written to the Error Log where ``verify_shard()`` findings can be matched
	against it.
	"""
	try:
		_created, unresolved = ensure_rules_for_type(doc.name)
		if unresolved:
			detail = "; ".join(f"{co}: {why}" for co, why in unresolved)
			frappe.log_error(
				message=(
					f"Stock Entry Type {doc.name!r} was created but could NOT be given a "
					f"naming rule: {detail}. It will fall back to the shared MAT-STE- "
					f"counter. Resolve the conflict, then run "
					f"shard_stock_entry_naming_by_type.repair_missing_rules()."
				),
				title="shard_stock_entry_naming: new type left uncovered",
			)
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
