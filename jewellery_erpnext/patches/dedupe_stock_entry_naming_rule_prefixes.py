"""GATED dedupe of duplicate-prefix Stock Entry Document Naming Rules.

Problem
-------
Two enabled Document Naming Rules that share one ``prefix`` each keep their OWN
``counter`` — so both can mint the identical document name (``<prefix><counter>``)
and collide on the ``tabStock Entry.name`` PRIMARY KEY (DuplicateEntryError at
submit time). On gk this exists twice, caused by a whitespace typo in the
``stock_entry_type`` condition value:

* ``GE-SE-MIC-.YY.-``  — ``6k1h2o8k4m`` binds to "Material Issue - Consumables"
  (single space, a Stock Entry Type that does NOT exist) vs ``m3c1dhgise`` which
  binds to the real double-space type.
* ``KGJPL-SE-MIC-.YY.-`` — same pattern (``6fuq80md75`` dead vs ``m3hdpka9sl`` real).

The dangling-type rules can never match any document (their condition value is not
a Stock Entry Type), so they are dead weight whose only effect is the collision
hazard. Verified on gk: zero Stock Entries of either type variant, zero historical
``*-SE-MIC-`` names, all four counters = 0.

Why this is a GATED, MANUAL patch (NOT wired into patches.txt)
--------------------------------------------------------------
It mutates naming configuration (same class of change as the reshard patch, which
this mirrors). Run dry-run FIRST, review the decision table, then confirm:

    bench --site <site> execute jewellery_erpnext.patches.dedupe_stock_entry_naming_rule_prefixes.dedupe
    bench --site <site> execute jewellery_erpnext.patches.dedupe_stock_entry_naming_rule_prefixes.dedupe --kwargs "{'confirm': True}"

Decision logic (generic, re-runnable on any site):
* Group ENABLED Stock Entry rules by prefix; only groups with >1 rule matter.
* A rule is a KEEPER when its ``stock_entry_type`` condition value exists in
  ``tabStock Entry Type``; it is DEAD when the value is dangling (no such type)
  AND no Stock Entry uses that value.
* Exactly one keeper + rest dead  -> disable the dead rules and seed the keeper's
  counter to GREATEST(all counters in the group, max numeric suffix of existing
  names on the prefix stem) so it can only move forward.
* Anything else (two real types sharing a prefix, a dead rule with usage, or no
  keeper) -> print ``REQUIRES MANUAL DECISION`` and change NOTHING, even with
  confirm=True. No automatic re-prefixing — a human picks the new prefix.

Reversible: ``rollback()`` re-enables the rules this run disabled (recorded per
invocation via the printed table; counters are forward-only, which is safe).
"""

import frappe

_DOCTYPE = "Stock Entry"


def _prefix_stem(prefix):
	"""Literal stem a rule's prefix produces before the first date/field token —
	``GE-SE-MIC-.YY.-`` -> ``GE-SE-MIC-`` (same shape as the reshard patch)."""
	return (prefix or "").split(".")[0]


def _max_existing_suffix(stem):
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


def _rule_type_condition(rule_name):
	"""The rule's stock_entry_type condition value (None when it has no such condition)."""
	rows = frappe.get_all(
		"Document Naming Rule Condition",
		filters={"parent": rule_name, "field": "stock_entry_type"},
		fields=["value"],
		limit=1,
	)
	return rows[0].value if rows else None


def dedupe(confirm=False):
	"""Dry-run (default) or apply the duplicate-prefix dedupe. See module docstring."""
	rules = frappe.get_all(
		"Document Naming Rule",
		filters={"document_type": _DOCTYPE, "disabled": 0},
		fields=["name", "prefix", "prefix_digits", "counter", "priority"],
	)
	by_prefix = {}
	for r in rules:
		by_prefix.setdefault(r.prefix or "", []).append(r)

	dup_groups = {p: rs for p, rs in by_prefix.items() if p and len(rs) > 1}
	if not dup_groups:
		print(
			"[dedupe] No duplicate-prefix enabled Stock Entry naming rules. Nothing to do."
		)
		return

	print(f"[dedupe] {len(dup_groups)} duplicate-prefix group(s). confirm={confirm}")
	changed = []
	for prefix, group in sorted(dup_groups.items()):
		keepers, dead, ambiguous = [], [], []
		for r in group:
			type_value = _rule_type_condition(r.name)
			type_exists = bool(
				type_value and frappe.db.exists("Stock Entry Type", type_value)
			)
			usage = (
				frappe.db.count(_DOCTYPE, {"stock_entry_type": type_value})
				if type_value
				else 0
			)
			row_info = (r, type_value, type_exists, usage)
			if type_exists:
				keepers.append(row_info)
			elif usage == 0:
				dead.append(row_info)
			else:
				ambiguous.append(row_info)

		print(f"\n  prefix {prefix!r}:")
		for r, tv, te, usage in keepers + dead + ambiguous:
			status = (
				"KEEPER"
				if te
				else ("DEAD (dangling type, unused)" if usage == 0 else "AMBIGUOUS")
			)
			print(
				f"    {r.name:<16} counter={int(r.counter or 0):>6} type={tv!r} "
				f"type_exists={te} usage={usage} -> {status}"
			)

		if len(keepers) != 1 or ambiguous:
			print(
				"    -> REQUIRES MANUAL DECISION (two real types on one prefix, or a used "
				"dangling type). No change applied."
			)
			continue

		keeper = keepers[0][0]
		seed_to = max(
			[int(keeper.counter or 0)]
			+ [int(r.counter or 0) for r, _tv, _te, _u in dead]
			+ [_max_existing_suffix(_prefix_stem(prefix))]
		)
		print(
			f"    -> plan: keep {keeper.name} (seed counter -> {seed_to}); disable "
			f"{', '.join(r.name for r, _tv, _te, _u in dead)}"
		)
		if confirm:
			if seed_to != int(keeper.counter or 0):
				frappe.db.set_value(
					"Document Naming Rule",
					keeper.name,
					"counter",
					seed_to,
					update_modified=False,
				)
			for r, _tv, _te, _u in dead:
				frappe.db.set_value(
					"Document Naming Rule", r.name, "disabled", 1, update_modified=False
				)
				frappe.get_doc("Document Naming Rule", r.name).clear_doctype_map()
				changed.append(r.name)
			frappe.get_doc("Document Naming Rule", keeper.name).clear_doctype_map()

	if confirm:
		frappe.db.commit()
		print(
			f"\n[dedupe] APPLIED: disabled {len(changed)} dead duplicate rule(s): {changed}"
		)
	else:
		print(
			"\n[dedupe] DRY-RUN only -- nothing changed. Re-run with confirm=True to apply."
		)


def rollback(rule_names=None):
	"""Re-enable rules disabled by this patch. Pass the printed list, e.g.
	--kwargs "{'rule_names': ['6k1h2o8k4m','6fuq80md75']}"."""
	if not rule_names:
		print("[dedupe] rollback needs rule_names=[...] (from the APPLIED output).")
		return
	for name in rule_names:
		frappe.db.set_value(
			"Document Naming Rule", name, "disabled", 0, update_modified=False
		)
		frappe.get_doc("Document Naming Rule", name).clear_doctype_map()
	frappe.db.commit()
	print(f"[dedupe] Re-enabled: {rule_names}")


def execute():
	"""Migrate entrypoint intentionally no-ops -- gated manual patch (see docstring)."""
	frappe.logger("jewellery_erpnext").info(
		"dedupe_stock_entry_naming_rule_prefixes: gated manual patch; execute() no-ops. "
		"Run the `dedupe` function explicitly after review."
	)
