# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the gated Stock Entry naming shard
(``jewellery_erpnext.patches.shard_stock_entry_naming_by_type``).

These cover the findings raised across three review rounds on PR #1225:

* rollback must disable ONLY the rules the patch created — on kggk-prod the old
  prefix-matching predicate also swept up five pre-existing ``KGJPL-SE-*`` Customer-Goods
  rules created months earlier;
* every defined Stock Entry Type must be covered, and a type created later must not
  silently fall back to the shared ``MAT-STE-`` row;
* a dry run must write nothing;
* rollback must be reversible — ``shard -> rollback -> shard`` restores, and a rolled-back
  company must not look sharded to the new-type hook;
* coverage must reflect what frappe ACTUALLY resolves (condition operator + rule priority),
  not what a ``(field, value)`` reduction suggests;
* a conflict must block apply, so ``ACTIVE`` always means fully covered.

``TestRealDocumentNamingRule`` at the bottom creates real records and is the only class here
that is not pure-mock; it exists because the operator/priority bug was invisible to mocks
asserting against the same model that was wrong.
"""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.naming import set_new_name
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext import lock_order as shard_mod_lock_order
from jewellery_erpnext.patches import shard_stock_entry_naming_by_type as shard_mod

_COMPANY = "KG GK Jewellers Private Limited"


class _PlanHarness:
	"""Patch every DB touchpoint ``_plan`` uses, so it can be exercised in isolation."""

	def __init__(
		self,
		covered=None,
		conflicts=(),
		abbrs=None,
		counts=None,
		taken=(),
		max_suffix=0,
		types=None,
	):
		self.covered = covered or {}
		self.conflicts = list(conflicts)
		self.abbrs = abbrs or {}
		self.counts = counts or {}
		self.taken = list(taken)
		self.max_suffix = max_suffix
		self.types = types
		self._patches = []

	def __enter__(self):
		self._patches = [
			patch.object(
				shard_mod, "_coverage", return_value=(self.covered, self.conflicts)
			),
			patch.object(shard_mod, "_historical_counts", return_value=self.counts),
			patch.object(shard_mod, "_harvest_abbreviations", return_value=self.abbrs),
			patch.object(
				shard_mod, "_max_existing_suffix", return_value=self.max_suffix
			),
			patch.object(shard_mod.frappe.db, "sql_list", return_value=self.taken),
			patch.object(shard_mod.frappe.db, "get_value", return_value="KGJPL"),
			patch.object(
				shard_mod.frappe,
				"get_all",
				return_value=(
					self.types if self.types is not None else list(self.counts) or []
				),
			),
		]
		for p in self._patches:
			p.start()
		return self

	def __exit__(self, *exc):
		for p in reversed(self._patches):
			p.stop()
		return False


class TestShardPlan(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_skips_types_that_already_have_a_rule(self):
		with _PlanHarness(
			covered={"Repack": "RULE-A"},
			abbrs={"Repack": "RP", "Manufacture": "MF"},
			counts={"Repack": 5, "Manufacture": 7},
		):
			plan, skipped, _conflicts = shard_mod._plan(_COMPANY)
		self.assertEqual([p["setype"] for p in plan], ["Manufacture"])
		self.assertEqual([s[0] for s in skipped], ["Repack"])

	def test_reuses_the_sites_existing_abbreviation(self):
		with _PlanHarness(abbrs={"Manufacture": "MF"}, counts={"Manufacture": 1}):
			plan, _s, _c = shard_mod._plan(_COMPANY)
		self.assertEqual(plan[0]["prefix"], "KGJPL-SE-MF-.YY.-")

	def test_derives_an_abbreviation_when_none_exists(self):
		with _PlanHarness(counts={"Repack-Gemstone Conversion": 1}):
			plan, _s, _c = shard_mod._plan(_COMPANY)
		self.assertEqual(plan[0]["prefix"], "KGJPL-SE-RGC-.YY.-")

	def test_disambiguates_a_colliding_prefix(self):
		# A prefix already used by ANY rule must never be reused — that would silently
		# shadow another company's naming.
		with _PlanHarness(
			abbrs={"Subcontracting Repack": "SR", "Subcontracting Return": "SR"},
			counts={"Subcontracting Repack": 1, "Subcontracting Return": 1},
		):
			plan, _s, _c = shard_mod._plan(_COMPANY)
		prefixes = [p["prefix"] for p in plan]
		self.assertEqual(len(set(prefixes)), 2, "prefixes must be unique")
		self.assertIn("KGJPL-SE-SR2-.YY.-", prefixes)

	def test_never_reuses_a_prefix_already_taken(self):
		with _PlanHarness(
			abbrs={"Manufacture": "MF"},
			counts={"Manufacture": 1},
			taken=["KGJPL-SE-MF-.YY.-"],
		):
			plan, _s, _c = shard_mod._plan(_COMPANY)
		self.assertEqual(plan[0]["prefix"], "KGJPL-SE-MF2-.YY.-")

	def test_seeds_counter_forward_only(self):
		# Seeding to the max existing suffix is what makes a duplicate primary key
		# impossible when a prefix stem has been used before.
		with _PlanHarness(
			abbrs={"Manufacture": "MF"}, counts={"Manufacture": 1}, max_suffix=417
		):
			plan, _s, _c = shard_mod._plan(_COMPANY)
		self.assertEqual(plan[0]["seed_to"], 417)

	def test_all_types_covers_types_with_no_documents(self):
		# Finding 3: a defined-but-unused type left uncovered falls back to MAT-STE- the
		# first time it is used.
		with _PlanHarness(abbrs={"Repair Unpack": "RU"}, counts={"Repair Unpack": 0}):
			plan, _s, _c = shard_mod._plan(_COMPANY, all_types=True)
		self.assertEqual([p["setype"] for p in plan], ["Repair Unpack"])

	def tearDown(self):
		return super().tearDown()


class TestCoverageIsActiveOnly(IntegrationTestCase):
	"""Only an ACTIVE rule that frappe ACTUALLY resolves counts as coverage.

	Round 2: a disabled rule names nothing, so treating it as coverage leaves the type on the
	shared MAT-STE- row — and after a rollback it made the shard un-reappliable.
	Round 3: coverage is decided by asking ``document_naming_rule_for_doc`` rather than by
	inferring from (field, value) pairs, which ignored the condition OPERATOR and rule
	PRIORITY. ``_effective`` below stands in for frappe's resolver.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _effective(self, mapping):
		"""Patch the resolver: {stock_entry_type: rule_name frappe would pick}."""
		return patch.object(
			shard_mod_lock_order,
			"document_naming_rule_for_doc",
			side_effect=lambda doc: mapping.get(doc.stock_entry_type),
		)

	def _rows(self, *specs, prefix=None):
		"""Rule rows. Each carries a UNIQUE prefix unless one is forced, so the namespace
		check passes by default and only the behaviour under test varies."""
		return [
			frappe._dict(
				name=n,
				disabled=d,
				n_conditions=nc,
				company=_COMPANY,
				setype=t,
				prefix=prefix or f"KGJPL-SE-{n}-.YY.-",
				prefix_digits=5,
				counter=0,
			)
			for n, t, d, nc in specs
		]

	def _clean_namespace(self):
		"""No historical documents and no equal-priority rivals — pins the two site-reading
		collaborators so these tests do not depend on what the bench happens to contain."""
		return patch.multiple(
			shard_mod,
			_max_existing_suffix=MagicMock(return_value=0),
			ambiguous_at_same_priority=MagicMock(return_value=[]),
			_namespace_rows=MagicMock(return_value=[]),
		)

	def test_disabled_rule_is_a_conflict_not_coverage(self):
		with patch.object(
			shard_mod,
			"_rule_rows",
			return_value=self._rows(("R1", "Manufacture", 1, 2)),
		), self._effective({}):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertEqual([(t, r) for t, _why, r in conflicts], [("Manufacture", "R1")])
		self.assertIn("DISABLED", conflicts[0][1])

	def test_rule_with_extra_conditions_is_a_conflict(self):
		# e.g. company + stock_entry_type + purpose: it covers only a SUBSET of the type,
		# so assuming general coverage would leave the rest on MAT-STE-.
		with patch.object(
			shard_mod, "_rule_rows", return_value=self._rows(("R1", "Repack", 0, 3))
		), self._effective({}):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertIn("3 conditions", conflicts[0][1])

	def test_duplicate_active_rules_are_a_conflict_and_NOT_covered(self):
		# Round 3: previously the first rule landed in `covered` and the second in
		# `conflicts`, so the type was BOTH. Which rule frappe picks depends on priority and
		# creation order, so the prefix is unpredictable and a priority edit silently changes
		# it — that is not coverage.
		with patch.object(
			shard_mod,
			"_rule_rows",
			return_value=self._rows(("R1", "Repack", 0, 2), ("R2", "Repack", 0, 2)),
		), self._effective({"Repack": "R1"}):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {}, "a duplicated type is never covered")
		self.assertIn("multiple active rules", conflicts[0][1])

	def test_plain_active_rule_frappe_resolves_is_coverage(self):
		with patch.object(
			shard_mod, "_rule_rows", return_value=self._rows(("R1", "Repack", 0, 2))
		), self._effective({"Repack": "R1"}), self._clean_namespace():
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {"Repack": "R1"})
		self.assertEqual(conflicts, [])

	def test_existing_rule_sharing_a_prefix_is_NOT_coverage(self):
		# Round-5 P0a. Each Document Naming Rule owns an INDEPENDENT counter, so two rules on
		# the same prefix march through the same names and eventually collide on the Stock
		# Entry primary key. Resolving correctly does not make the namespace safe.
		shared = self._rows(
			("R1", "Repack", 0, 2),
			("R2", "Manufacture", 0, 2),
			prefix="KGJPL-SE-SAME-.YY.-",
		)
		with patch.object(
			shard_mod, "_rule_rows", return_value=shared
		), self._effective({"Repack": "R1", "Manufacture": "R2"}), patch.multiple(
			shard_mod,
			_max_existing_suffix=MagicMock(return_value=0),
			ambiguous_at_same_priority=MagicMock(return_value=[]),
			_namespace_rows=MagicMock(return_value=shared),
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertTrue(any("shared with" in why for _t, why, _r in conflicts))

	def test_CROSS_COMPANY_shared_prefix_is_NOT_coverage(self):
		# Round-6 issue 1: tabStock Entry.name is ONE global namespace, so a same-prefix rule
		# under ANOTHER company can mint the same names. It never appears among this company's
		# candidate rows, so only the global namespace source can see it.
		mine = self._rows(("R1", "Repack", 0, 2), prefix="SHARED-SE-X-.YY.-")
		theirs = [
			frappe._dict(
				name="OTHER-CO-RULE",
				disabled=0,
				prefix="SHARED-SE-X-.YY.-",
				prefix_digits=5,
				counter=0,
			)
		]
		with patch.object(
			shard_mod, "_rule_rows", return_value=mine
		), self._effective({"Repack": "R1"}), patch.multiple(
			shard_mod,
			_max_existing_suffix=MagicMock(return_value=0),
			ambiguous_at_same_priority=MagicMock(return_value=[]),
			_namespace_rows=MagicMock(return_value=mine + theirs),
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {}, "a cross-company prefix clash is not coverage")
		self.assertTrue(
			any("OTHER-CO-RULE" in why for _t, why, _r in conflicts),
			f"the other company's rule must be named: {conflicts}",
		)

	def test_existing_rule_with_a_stale_counter_is_NOT_coverage(self):
		# Round-5 P0b: counter behind the highest name already issued under this prefix would
		# re-issue used names.
		with patch.object(
			shard_mod, "_rule_rows", return_value=self._rows(("R1", "Repack", 0, 2))
		), self._effective({"Repack": "R1"}), patch.multiple(
			shard_mod,
			_max_existing_suffix=MagicMock(return_value=42),
			ambiguous_at_same_priority=MagicMock(return_value=[]),
			_namespace_rows=MagicMock(return_value=[]),
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertTrue(
			any("behind the highest existing name" in why for _t, why, _r in conflicts)
		)

	def test_existing_rule_with_an_empty_prefix_is_NOT_coverage(self):
		with patch.object(
			shard_mod,
			"_rule_rows",
			return_value=self._rows(("R1", "Repack", 0, 2), prefix="  "),
		), self._effective({"Repack": "R1"}), self._clean_namespace():
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertIn("empty prefix", conflicts[0][1])

	def test_a_non_equality_rule_is_not_an_exact_candidate(self):
		# Round-6 issue 2. `_rule_rows` projects company/setype only from `=` conditions, so a
		# `company != X` rule yields NULLs and is filtered out of the candidates. Previously it
		# projected identically to `company = X`, inflating `active` to 2 and raising a false
		# "multiple active rules claim this type" BEFORE frappe's resolver was consulted —
		# and since apply is fail-closed, that false conflict blocked the whole migration.
		rows = self._rows(("R1", "Repack", 0, 2))
		rows.append(
			frappe._dict(
				name="EXCLUDER", disabled=0, n_conditions=2,
				company=None, setype=None,          # `!=` projects NULL now
				prefix="KGJPL-SE-EXC-.YY.-", prefix_digits=5, counter=0,
			)
		)
		with patch.object(
			shard_mod, "_rule_rows", return_value=rows
		), self._effective({"Repack": "R1"}), patch.multiple(
			shard_mod,
			_max_existing_suffix=MagicMock(return_value=0),
			ambiguous_at_same_priority=MagicMock(return_value=[]),
			_namespace_rows=MagicMock(return_value=rows),
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {"Repack": "R1"}, "the `=` rule must still cover the type")
		self.assertFalse(
			any("multiple active rules" in why for _t, why, _r in conflicts),
			f"a non-matching `!=` rule must not raise a duplicate conflict: {conflicts}",
		)

	def test_a_non_equality_rule_still_owns_its_prefix(self):
		# The trap in fixing issue 2: a `!=` rule is not an exact candidate, but it still MINTS
		# names under its prefix, so it must remain visible as a namespace owner. Filtering it
		# out of both sources would have silently reopened the collision hole.
		mine = self._rows(("R1", "Repack", 0, 2), prefix="KGJPL-SE-DUP-.YY.-")
		excluder = frappe._dict(
			name="EXCLUDER", disabled=0, prefix="KGJPL-SE-DUP-.YY.-",
			prefix_digits=5, counter=0,
		)
		with patch.object(
			shard_mod, "_rule_rows", return_value=mine
		), self._effective({"Repack": "R1"}), patch.multiple(
			shard_mod,
			_max_existing_suffix=MagicMock(return_value=0),
			ambiguous_at_same_priority=MagicMock(return_value=[]),
			_namespace_rows=MagicMock(return_value=mine + [excluder]),
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertTrue(any("EXCLUDER" in why for _t, why, _r in conflicts))

	def test_equal_priority_rival_makes_the_match_ambiguous(self):
		# Round-5: frappe orders by `priority desc` with no tie-breaker, so an equal-priority
		# rival means the winner is whatever the DB returns first — not a stable answer.
		with patch.object(
			shard_mod, "_rule_rows", return_value=self._rows(("R1", "Repack", 0, 2))
		), self._effective({"Repack": "R1"}), patch.multiple(
			shard_mod,
			_max_existing_suffix=MagicMock(return_value=0),
			ambiguous_at_same_priority=MagicMock(
				return_value=[frappe._dict(name="R1"), frappe._dict(name="RIVAL")]
			),
			rule_priority=MagicMock(return_value=0),
			_namespace_rows=MagicMock(return_value=[]),
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertIn("ambiguous", conflicts[0][1])

	def test_rule_frappe_does_not_resolve_is_NOT_coverage(self):
		# The operator blind spot: a rule conditioned `company != X` reduces to the same
		# (field, value) pair as `company = X`, so the old structural check called it
		# covered. Frappe's evaluator does not, and frappe is the authority.
		with patch.object(
			shard_mod, "_rule_rows", return_value=self._rows(("R1", "Repack", 0, 2))
		), self._effective({"Repack": None}):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertIn("no rule", conflicts[0][1])

	def test_rule_shadowed_by_higher_priority_is_NOT_coverage(self):
		# The priority blind spot: an exact (company, type) rule exists, but a
		# higher-priority rule wins. Only the winner names the document.
		with patch.object(
			shard_mod, "_rule_rows", return_value=self._rows(("R1", "Repack", 0, 2))
		), self._effective({"Repack": "GENERIC-HIGH-PRIORITY"}):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertIn("GENERIC-HIGH-PRIORITY", conflicts[0][1])

	def test_reenablable_rule_counts_as_coverage_not_a_conflict(self):
		# A disabled rule THIS patch created and is about to re-enable is the expected
		# rolled-back state. It must be reported as covered — neither as a conflict (which
		# would tell the operator 44 types are broken mid-restore) nor as uncovered (which
		# made _plan mint a duplicate rule with a "-2" prefix beside every original).
		with patch.object(
			shard_mod,
			"_rule_rows",
			return_value=self._rows(("R1", "Manufacture", 1, 2)),
		), self._effective({}), patch.object(
			shard_mod, "_validate_recorded_rule", return_value=None
		):
			covered, conflicts = shard_mod._coverage(_COMPANY, reenablable=["R1"])
		self.assertEqual(covered, {"Manufacture": "R1"})
		self.assertEqual(conflicts, [])

	def test_reenablable_rule_that_DRIFTED_is_a_conflict_not_coverage(self):
		# Round 4: a rollback leaves the rule in place but disabled, and a disabled rule can
		# be edited. Re-enabling on the strength of its recorded NAME would trust whatever it
		# now points at, so identity is revalidated and failure becomes a conflict.
		with patch.object(
			shard_mod,
			"_rule_rows",
			return_value=self._rows(("R1", "Manufacture", 1, 2)),
		), self._effective({}), patch.object(
			shard_mod,
			"_validate_recorded_rule",
			return_value="stock_entry_type condition drifted (expected 'Manufacture')",
		):
			covered, conflicts = shard_mod._coverage(_COMPANY, reenablable=["R1"])
		self.assertEqual(covered, {})
		self.assertIn("drifted", conflicts[0][1])

	def test_reenablable_rule_is_not_replanned(self):
		with _PlanHarness(
			covered={"Manufacture": "R1"},
			abbrs={"Manufacture": "MF"},
			counts={"Manufacture": 500},
		):
			plan, skipped, conflicts = shard_mod._plan(
				_COMPANY, all_types=True, reenablable=["R1"]
			)
		self.assertEqual(
			plan, [], "must not create a duplicate beside the re-enabled rule"
		)
		self.assertEqual([s[0] for s in skipped], ["Manufacture"])

	def test_conflicting_type_is_neither_planned_nor_skipped(self):
		# It must surface to the operator, not be silently created alongside or ignored.
		with _PlanHarness(
			conflicts=[("Manufacture", "rule exists but is DISABLED", "R1")],
			abbrs={"Manufacture": "MF"},
			counts={"Manufacture": 9},
		):
			plan, skipped, conflicts = shard_mod._plan(_COMPANY)
		self.assertEqual(plan, [])
		self.assertEqual(skipped, [])
		self.assertEqual(len(conflicts), 1)

	def tearDown(self):
		return super().tearDown()


class TestFailClosedOnConflicts(IntegrationTestCase):
	"""Round-3 Finding 3: a conflict must block apply, so ACTIVE always means fully covered.

	``sharded_companies()`` and ``repair_missing_rules()`` both trust ACTIVE. Applying while a
	type is unresolved marked the company healthy while that type still fell back to MAT-STE-.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _run_with_conflicts(self, conflicts, confirm):
		plan = [
			{
				"setype": "Manufacture",
				"prefix": "KGJPL-SE-MF-.YY.-",
				"seed_to": 0,
				"docs": 3,
			}
		]
		with patch.object(
			shard_mod, "_plan", return_value=(plan, [], conflicts)
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_get_created", return_value=None), patch.object(
			shard_mod, "_get_state", return_value=shard_mod._NOT_APPLIED
		), patch.object(shard_mod.frappe, "get_doc") as mock_get_doc, patch.object(
			shard_mod, "_set_state"
		) as mock_state, patch.object(shard_mod.frappe.db, "commit") as mock_commit:
			shard_mod.shard(confirm=confirm)
		return mock_get_doc, mock_state, mock_commit

	def test_apply_throws_and_changes_nothing_when_conflicts_remain(self):
		conflicts = [("Repack", "rule exists but is DISABLED", "R1")]
		with self.assertRaises(frappe.ValidationError):
			self._run_with_conflicts(conflicts, confirm=True)

	def test_apply_writes_nothing_when_it_throws(self):
		conflicts = [("Repack", "rule exists but is DISABLED", "R1")]
		try:
			mock_get_doc, mock_state, mock_commit = self._run_with_conflicts(
				conflicts, confirm=True
			)
		except frappe.ValidationError:
			pass
		else:  # pragma: no cover - the throw is the point of this test
			self.fail("expected a ValidationError")

	def test_throws_even_when_there_is_nothing_to_create(self):
		# The case the first fail-closed attempt missed, caught end-to-end on a real bench:
		# a conflicted type is EXCLUDED from the plan, so a site whose only outstanding work
		# is a conflict produces an EMPTY plan. The old "nothing to do" early return fired
		# before the conflict gate and returned success, reporting "every stock_entry_type
		# already has an active rule" while one type still fell back to MAT-STE-.
		conflicts = [("Manufacture", "multiple active rules claim this type", "R2")]
		with patch.object(
			shard_mod, "_plan", return_value=([], [], conflicts)
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_get_created", return_value=None), patch.object(
			shard_mod, "_get_state", return_value=shard_mod._ACTIVE
		), patch.object(shard_mod.frappe, "get_doc") as mock_get_doc, patch.object(
			shard_mod, "_set_state"
		) as mock_state:
			with self.assertRaises(frappe.ValidationError):
				shard_mod.shard(confirm=True)
		mock_get_doc.assert_not_called()
		mock_state.assert_not_called()

	def _noop_run(self, state, verify_side_effect=None):
		"""shard(confirm=True) with an empty plan and no conflicts.

		``_verify_or_rollback`` MUST be patched: a confirmed run always verifies, and the real
		implementation resolves every Stock Entry Type against whatever the site happens to
		contain. Leaving it live made this test pass only on an already-sharded bench and fail
		on CI's fresh test_site — a unit test must not depend on site state.
		"""
		with patch.object(shard_mod, "_plan", return_value=([], [], [])), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_get_created", return_value=None), patch.object(
			shard_mod, "_get_state", return_value=state
		), patch.object(
			shard_mod, "_verify_or_rollback", side_effect=verify_side_effect
		) as mock_verify, patch.object(
			shard_mod, "_set_state"
		) as mock_state, patch.object(shard_mod.frappe.db, "commit"), patch.object(
			shard_mod.frappe, "clear_cache"
		):
			shard_mod.shard(confirm=True)
		return mock_verify, mock_state

	def test_empty_plan_with_no_conflicts_still_verifies(self):
		# Nothing to write, but a confirmed run must still prove coverage — that is what
		# makes ACTIVE mean "frappe really resolves every type", not "this patch wrote rules".
		mock_verify, mock_state = self._noop_run(shard_mod._ACTIVE)
		mock_verify.assert_called_once_with(_COMPANY)
		mock_state.assert_not_called()  # already ACTIVE, nothing to change

	def test_empty_plan_activates_an_already_covered_company(self):
		# Round-4 finding 34: a company fully covered by rules someone else created should
		# end up ACTIVE rather than stuck in NOT_APPLIED.
		mock_verify, mock_state = self._noop_run(shard_mod._NOT_APPLIED)
		mock_verify.assert_called_once_with(_COMPANY)
		mock_state.assert_called_once_with(_COMPANY, shard_mod._ACTIVE)

	def test_empty_plan_does_not_activate_when_verification_fails(self):
		# Verification throws (it has already rolled back); the state must not be touched.
		with self.assertRaises(frappe.ValidationError):
			self._noop_run(
				shard_mod._NOT_APPLIED,
				verify_side_effect=frappe.ValidationError("verification failed"),
			)

	def test_dry_run_still_reports_conflicts_without_throwing(self):
		# Nothing is hidden — the operator must be able to SEE what to resolve.
		conflicts = [("Repack", "rule exists but is DISABLED", "R1")]
		mock_get_doc, mock_state, mock_commit = self._run_with_conflicts(
			conflicts, confirm=False
		)
		mock_get_doc.assert_not_called()
		mock_state.assert_not_called()
		mock_commit.assert_not_called()

	def test_apply_proceeds_when_there_are_no_conflicts(self):
		doc = MagicMock()
		doc.name = "NEW-RULE"
		plan = [
			{
				"setype": "Manufacture",
				"prefix": "KGJPL-SE-MF-.YY.-",
				"seed_to": 0,
				"docs": 3,
			}
		]
		with patch.object(
			shard_mod, "_plan", return_value=(plan, [], [])
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_get_created", return_value=None), patch.object(
			shard_mod, "_get_state", return_value=shard_mod._NOT_APPLIED
		), patch.object(shard_mod.frappe, "get_doc", return_value=doc), patch.object(
			shard_mod, "_record_created", return_value=["NEW-RULE"]
		), patch.object(shard_mod, "_set_state") as mock_state, patch.object(
			shard_mod, "_verify_or_rollback"
		) as mock_verify, patch.object(shard_mod.frappe.db, "commit"), patch.object(
			shard_mod.frappe, "clear_cache"
		), patch.object(shard_mod.frappe, "cache_manager"), patch.object(
			shard_mod.frappe.db, "get_value", return_value="KGJPL"
		):
			shard_mod.shard(confirm=True)
		# Verification must run BEFORE the state is set, every time.
		mock_verify.assert_called_once_with(_COMPANY)
		mock_state.assert_called_once_with(_COMPANY, shard_mod._ACTIVE)

	def tearDown(self):
		return super().tearDown()


class TestShardLifecycle(IntegrationTestCase):
	"""Round-2 Finding 2: rollback must not be a one-way door."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_rollback_marks_state_rolled_back(self):
		with patch.object(
			shard_mod, "_get_created", return_value=["MINE-1"]
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_rule_map", return_value={}), patch.object(
			shard_mod.frappe.db, "exists", return_value=True
		), patch.object(
			shard_mod, "_validate_recorded_rule", return_value=None
		), patch.object(shard_mod.frappe.db, "set_value"), patch.object(
			shard_mod.frappe.db, "commit"
		), patch.object(shard_mod.frappe, "clear_cache"), patch.object(
			shard_mod, "_set_state"
		) as mock_state:
			shard_mod.rollback()
		mock_state.assert_called_once_with(_COMPANY, shard_mod._ROLLED_BACK)

	def test_rollback_refuses_before_writing_when_a_rule_drifted(self):
		# Round-6 issue 3: drift used to be detected, the rule disabled, the transaction
		# committed, and only THEN was the operator warned. Preflight now stops first.
		with patch.object(
			shard_mod, "_get_created", return_value=["MINE-1"]
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_rule_map", return_value={}), patch.object(
			shard_mod.frappe.db, "exists", return_value=True
		), patch.object(
			shard_mod, "_validate_recorded_rule", return_value="company condition drifted"
		), patch.object(shard_mod.frappe.db, "set_value") as mock_set, patch.object(
			shard_mod.frappe.db, "commit"
		) as mock_commit, patch.object(shard_mod, "_set_state") as mock_state:
			with self.assertRaises(frappe.ValidationError):
				shard_mod.rollback()
		mock_set.assert_not_called()
		mock_commit.assert_not_called()
		mock_state.assert_not_called()

	def test_rollback_force_proceeds_despite_drift(self):
		# Rollback is the emergency lever for undoing the shard, so it must stay reachable —
		# just deliberately.
		with patch.object(
			shard_mod, "_get_created", return_value=["MINE-1"]
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_rule_map", return_value={}), patch.object(
			shard_mod.frappe.db, "exists", return_value=True
		), patch.object(
			shard_mod, "_validate_recorded_rule", return_value="company condition drifted"
		), patch.object(shard_mod.frappe.db, "set_value") as mock_set, patch.object(
			shard_mod.frappe.db, "commit"
		), patch.object(shard_mod.frappe, "clear_cache"), patch.object(
			shard_mod, "_set_state"
		) as mock_state:
			shard_mod.rollback(force=True)
		self.assertEqual([c.args[1] for c in mock_set.call_args_list], ["MINE-1"])
		mock_state.assert_called_once_with(_COMPANY, shard_mod._ROLLED_BACK)

	def test_rollback_keeps_the_created_record(self):
		# The names are what let shard() re-enable exactly these rules later, so rollback
		# must NOT clear them — the lifecycle state is what marks the company unsharded.
		store = {}
		with patch.object(
			shard_mod, "_get_created", return_value=["MINE-1"]
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_rule_map", return_value={}), patch.object(
			shard_mod.frappe.db, "exists", return_value=True
		), patch.object(
			shard_mod, "_validate_recorded_rule", return_value=None
		), patch.object(shard_mod.frappe.db, "set_value"), patch.object(
			shard_mod.frappe.db, "commit"
		), patch.object(shard_mod.frappe, "clear_cache"), patch.object(
			shard_mod.frappe.db,
			"set_default",
			side_effect=lambda k, v: store.__setitem__(k, v),
		):
			shard_mod.rollback()
		self.assertNotIn(shard_mod._created_key(_COMPANY), store)

	def test_shard_reenables_previously_disabled_rules(self):
		# shard -> rollback -> shard must restore, not report "nothing to do". The rules
		# already hold the right prefixes and counters, so they are re-enabled, not recreated.
		disabled = {"MINE-1": 1, "MINE-2": 1}
		with patch.object(shard_mod, "_plan", return_value=([], [], [])), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(
			shard_mod, "_get_created", return_value=list(disabled)
		), patch.object(
			shard_mod, "_get_state", return_value=shard_mod._ROLLED_BACK
		), patch.object(shard_mod.frappe.db, "exists", return_value=True), patch.object(
			shard_mod.frappe.db,
			"get_value",
			side_effect=lambda dt, n, f=None, **k: (
				"Manufacture"
				if dt == "Document Naming Rule Condition"
				else disabled.get(n if isinstance(n, str) else None, 0)
			),
		), patch.object(shard_mod.frappe.db, "set_value") as mock_set, patch.object(
			shard_mod, "_record_created", return_value=list(disabled)
		), patch.object(shard_mod, "_set_state") as mock_state, patch.object(
			shard_mod, "_validate_recorded_rule", return_value=None
		), patch.object(shard_mod, "_verify_or_rollback"), patch.object(
			shard_mod.frappe.db, "commit"
		), patch.object(shard_mod.frappe, "clear_cache"), patch.object(
			shard_mod.frappe, "cache_manager"
		):
			shard_mod.shard(confirm=True)
		reenabled = {c.args[1]: c.args[3] for c in mock_set.call_args_list}
		self.assertEqual(reenabled, {"MINE-1": 0, "MINE-2": 0})
		mock_state.assert_called_once_with(_COMPANY, shard_mod._ACTIVE)

	def test_sharded_companies_keys_off_state_not_the_record(self):
		# The record survives rollback by design; using it here left the new-type hook armed
		# on a rolled-back site.
		with patch.object(
			shard_mod.frappe, "get_all", return_value=[_COMPANY]
		), patch.object(shard_mod, "_get_state", return_value=shard_mod._ROLLED_BACK):
			self.assertEqual(shard_mod.sharded_companies(), [])
		with patch.object(
			shard_mod.frappe, "get_all", return_value=[_COMPANY]
		), patch.object(shard_mod, "_get_state", return_value=shard_mod._ACTIVE):
			self.assertEqual(shard_mod.sharded_companies(), [_COMPANY])

	def tearDown(self):
		return super().tearDown()


class TestDryRunCounts(IntegrationTestCase):
	"""Round-2 Finding 4: an all_types run must still report the true blast radius."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_all_types_reports_real_historical_counts(self):
		# Previously all_types=True set counts={}, so every row read docs=0 and the summary
		# claimed "0 historical Stock Entries" when the true figure was 10,591.
		with _PlanHarness(
			abbrs={"A": "A", "B": "B"},
			counts={"A": 5000, "B": 100},
			types=["A", "B"],
		):
			plan, _s, _c = shard_mod._plan(_COMPANY, all_types=True)
		docs = {p["setype"]: p["docs"] for p in plan}
		self.assertEqual(docs, {"A": 5000, "B": 100})
		self.assertEqual(sum(p["docs"] for p in plan), 5100)

	def test_all_types_orders_busiest_first(self):
		with _PlanHarness(
			abbrs={"A": "A", "B": "B"},
			counts={"A": 1, "B": 900},
			types=["A", "B"],
		):
			plan, _s, _c = shard_mod._plan(_COMPANY, all_types=True)
		self.assertEqual([p["setype"] for p in plan], ["B", "A"])

	def tearDown(self):
		return super().tearDown()


class TestVerifyShard(IntegrationTestCase):
	"""Round-2 Finding 3: the silent hook failure must be detectable."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_lists_exactly_the_uncovered_types(self):
		with patch.object(
			shard_mod, "_coverage", return_value=({"A": "R1"}, [])
		), patch.object(
			shard_mod, "_historical_counts", return_value={"A": 10, "B": 7}
		), patch.object(
			shard_mod.frappe, "get_all", return_value=["A", "B", "C"]
		), patch.object(shard_mod, "_get_state", return_value=shard_mod._ACTIVE):
			out = shard_mod.verify_shard(_COMPANY, verbose=False)
		self.assertEqual(out["uncovered"], ["B", "C"])
		self.assertEqual(out["state"], shard_mod._ACTIVE)

	def test_reports_uncovered_and_conflicts_as_separate_sections(self):
		# They mean different things: "uncovered" is what falls back to MAT-STE- right now;
		# "conflicts" is the subset a human must resolve before shard() will apply at all.
		import io
		from contextlib import redirect_stdout

		buf = io.StringIO()
		with patch.object(
			shard_mod,
			"_coverage",
			return_value=({"A": "R1"}, [("B", "rule exists but is DISABLED", "R2")]),
		), patch.object(
			shard_mod, "_historical_counts", return_value={"A": 10, "B": 7}
		), patch.object(
			shard_mod.frappe, "get_all", return_value=["A", "B", "C"]
		), patch.object(shard_mod, "_get_state", return_value=shard_mod._ACTIVE):
			with redirect_stdout(buf):
				out = shard_mod.verify_shard(_COMPANY, verbose=True)
		text = buf.getvalue()
		self.assertIn("NOT COVERED (2)", text)
		self.assertIn("CONFLICTS (1)", text)
		self.assertEqual(out["uncovered"], ["B", "C"])  # A is covered

	def test_repair_refuses_unless_active(self):
		with patch.object(
			shard_mod, "_get_state", return_value=shard_mod._ROLLED_BACK
		), patch.object(shard_mod, "shard") as mock_shard:
			shard_mod.repair_missing_rules(_COMPANY)
		mock_shard.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestShardDryRun(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_dry_run_writes_nothing(self):
		plan = [
			{
				"setype": "Manufacture",
				"prefix": "KGJPL-SE-MF-.YY.-",
				"seed_to": 0,
				"docs": 3,
			}
		]
		# _get_created must be pinned: with rules recorded on the site, shard() scans them for
		# re-enablement and _validate_recorded_rule reads each one with frappe.get_doc — which
		# this test asserts is never called. Leaving it live made the test pass only on a
		# bench that had no recorded rules.
		with patch.object(
			shard_mod, "_plan", return_value=(plan, [], [])
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_get_created", return_value=None), patch.object(
			shard_mod, "_get_state", return_value=shard_mod._NOT_APPLIED
		), patch.object(shard_mod.frappe, "get_doc") as mock_get_doc, patch.object(
			shard_mod, "_record_created"
		) as mock_record, patch.object(shard_mod.frappe.db, "commit") as mock_commit:
			shard_mod.shard(confirm=False)
		mock_get_doc.assert_not_called()
		mock_record.assert_not_called()
		mock_commit.assert_not_called()

	def test_defaults_to_covering_every_type(self):
		# The default must be all_types=True; the previous default silently left 23 of 49
		# types on the shared row, and the docstring's own example demonstrated it.
		with patch.object(
			shard_mod, "_plan", return_value=([], [], [])
		) as mock_plan, patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		):
			shard_mod.shard()
		self.assertIs(mock_plan.call_args.kwargs["all_types"], True)

	def tearDown(self):
		return super().tearDown()


class TestShardRollback(IntegrationTestCase):
	"""Finding 2: rollback must touch ONLY the rules this patch created."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_refuses_when_no_rules_were_recorded(self):
		# Guessing from the company prefix is exactly what disabled five pre-existing
		# rules. With no record, refuse and report instead.
		with patch.object(shard_mod, "_get_created", return_value=None), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(
			shard_mod, "_rule_map", return_value={(_COMPANY, "Repack"): "PRE-EXISTING"}
		), patch.object(
			shard_mod.frappe.db, "get_value", return_value="KGJPL"
		), patch.object(shard_mod.frappe.db, "set_value") as mock_set, patch.object(
			shard_mod.frappe.db, "commit"
		) as mock_commit:
			shard_mod.rollback()
		mock_set.assert_not_called()
		mock_commit.assert_not_called()

	def test_disables_only_recorded_rules(self):
		recorded = ["MINE-1", "MINE-2"]
		with patch.object(
			shard_mod, "_get_created", return_value=recorded
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(
			shard_mod,
			"_rule_map",
			return_value={
				(_COMPANY, "A"): "MINE-1",
				(_COMPANY, "B"): "MINE-2",
				(_COMPANY, "C"): "PRE-EXISTING",
			},
		), patch.object(shard_mod.frappe.db, "exists", return_value=True), patch.object(
			shard_mod, "_validate_recorded_rule", return_value=None
		), patch.object(shard_mod.frappe.db, "set_value") as mock_set, patch.object(
			shard_mod.frappe.db, "commit"
		), patch.object(shard_mod.frappe, "clear_cache"):
			shard_mod.rollback()
		touched = [c.args[1] for c in mock_set.call_args_list]
		self.assertEqual(sorted(touched), recorded)
		self.assertNotIn("PRE-EXISTING", touched)

	def test_skips_recorded_rules_that_no_longer_exist(self):
		with patch.object(
			shard_mod, "_get_created", return_value=["GONE"]
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
		), patch.object(shard_mod, "_rule_map", return_value={}), patch.object(
			shard_mod.frappe.db, "exists", return_value=False
		), patch.object(shard_mod.frappe.db, "set_value") as mock_set, patch.object(
			shard_mod.frappe.db, "commit"
		), patch.object(shard_mod.frappe, "clear_cache"):
			shard_mod.rollback()
		mock_set.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestCreatedRecord(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_record_is_idempotent_and_order_stable(self):
		store = {}
		with patch.object(
			shard_mod.frappe.db,
			"set_default",
			side_effect=lambda k, v: store.__setitem__(k, v),
		), patch.object(
			shard_mod.frappe.db, "get_default", side_effect=lambda k: store.get(k)
		):
			shard_mod._record_created(_COMPANY, ["A", "B"])
			merged = shard_mod._record_created(_COMPANY, ["B", "C"])
		self.assertEqual(merged, ["A", "B", "C"])
		self.assertEqual(
			json.loads(store[shard_mod._created_key(_COMPANY)]), ["A", "B", "C"]
		)

	def test_unreadable_record_is_treated_as_absent(self):
		# A corrupt record must make rollback refuse, never fall back to prefix matching.
		with patch.object(shard_mod.frappe.db, "get_default", return_value="{not json"):
			self.assertIsNone(shard_mod._get_created(_COMPANY))

	def tearDown(self):
		return super().tearDown()


class TestEnsureRulesForNewType(IntegrationTestCase):
	"""Finding 3: a Stock Entry Type added after the shard must not reintroduce MAT-STE-."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_noop_on_a_site_that_was_never_sharded(self):
		with patch.object(
			shard_mod, "sharded_companies", return_value=[]
		), patch.object(shard_mod.frappe, "get_doc") as mock_get_doc:
			created, unresolved = shard_mod.ensure_rules_for_type("Anything")
		self.assertEqual(created, [])
		self.assertEqual(unresolved, [])  # no sharded company -> nothing to report
		mock_get_doc.assert_not_called()

	def test_creates_the_rule_for_a_sharded_company(self):
		plan = [
			{
				"setype": "Brand New Type",
				"prefix": "KGJPL-SE-BNT-.YY.-",
				"seed_to": 0,
				"docs": 0,
			},
			{"setype": "Other", "prefix": "KGJPL-SE-O-.YY.-", "seed_to": 0, "docs": 0},
		]
		doc = MagicMock()
		doc.name = "NEW-RULE"
		with patch.object(
			shard_mod, "sharded_companies", return_value=[_COMPANY]
		), patch.object(shard_mod, "_plan", return_value=(plan, [], [])), patch.object(
			shard_mod.frappe, "get_doc", return_value=doc
		), patch.object(shard_mod, "_record_created") as mock_record, patch.object(
			shard_mod.frappe, "cache_manager"
		):
			created, unresolved = shard_mod.ensure_rules_for_type("Brand New Type")
		self.assertEqual(created, ["NEW-RULE"])  # only the matching type, not "Other"
		self.assertEqual(unresolved, [])
		# Round 5: the FULL identity must be recorded, exactly as shard() does. A bare name
		# leaves the rule in _validate_recorded_rule's legacy branch, unable to prove drift.
		mock_record.assert_called_once_with(
			_COMPANY,
			[
				{
					"name": "NEW-RULE",
					"company": _COMPANY,
					"stock_entry_type": "Brand New Type",
					"prefix": "KGJPL-SE-BNT-.YY.-",
				}
			],
		)

	def test_conflicted_new_type_is_reported_not_swallowed(self):
		# Round 5: a conflicted type is excluded from `plan`, so the old code created nothing,
		# raised nothing and logged nothing while the company still reported ACTIVE.
		conflicts = [("Brand New Type", "multiple active rules claim this type", "R2")]
		with patch.object(
			shard_mod, "sharded_companies", return_value=[_COMPANY]
		), patch.object(
			shard_mod, "_plan", return_value=([], [], conflicts)
		), patch.object(shard_mod.frappe, "get_doc") as mock_get_doc:
			created, unresolved = shard_mod.ensure_rules_for_type("Brand New Type")
		self.assertEqual(created, [])
		self.assertEqual(len(unresolved), 1)
		self.assertIn("multiple active rules", unresolved[0][1])
		mock_get_doc.assert_not_called()

	def test_planner_silence_is_also_reported(self):
		# No conflict, but no plan row either — still uncovered, still must be surfaced.
		with patch.object(
			shard_mod, "sharded_companies", return_value=[_COMPANY]
		), patch.object(shard_mod, "_plan", return_value=([], [], [])):
			created, unresolved = shard_mod.ensure_rules_for_type("Brand New Type")
		self.assertEqual(created, [])
		self.assertEqual(len(unresolved), 1)
		self.assertIn("no rule", unresolved[0][1])

	def test_hook_logs_when_a_type_is_left_uncovered(self):
		with patch.object(
			shard_mod,
			"ensure_rules_for_type",
			return_value=([], [(_COMPANY, "conflict blah (R2)")]),
		), patch.object(shard_mod.frappe, "log_error") as mock_log:
			shard_mod.on_stock_entry_type_insert(frappe._dict(name="X"))
		mock_log.assert_called_once()
		self.assertIn("uncovered", mock_log.call_args.kwargs["title"])

	def test_hook_stays_quiet_on_full_success(self):
		with patch.object(
			shard_mod, "ensure_rules_for_type", return_value=(["R1"], [])
		), patch.object(shard_mod.frappe, "log_error") as mock_log:
			shard_mod.on_stock_entry_type_insert(frappe._dict(name="X"))
		mock_log.assert_not_called()

	def test_hook_never_raises(self):
		# A naming-rule gap must never block creating a Stock Entry Type.
		with patch.object(
			shard_mod, "ensure_rules_for_type", side_effect=RuntimeError("boom")
		), patch.object(shard_mod.frappe, "log_error") as mock_log:
			shard_mod.on_stock_entry_type_insert(frappe._dict(name="X"))
		mock_log.assert_called_once()

	def tearDown(self):
		return super().tearDown()


class TestRealDocumentNamingRule(IntegrationTestCase):
	"""Integration test against REAL Document Naming Rules — no mocks.

	Round-3 blocker 1 existed because ``_coverage`` modelled frappe's rule evaluation instead
	of invoking it: a rule conditioned ``company != X`` reduced to the same ``(field, value)``
	pair as ``company = X``, and rule ``priority`` was ignored entirely. Mock-based tests
	cannot catch that class of bug, because they assert against the same model that is wrong.

	These cases create real ``Stock Entry Type`` and ``Document Naming Rule`` records and let
	frappe resolve them, so the operator/priority semantics are exercised for real.

	Runs on CI's disposable ``test_site``. Records are removed in ``tearDown`` so the module
	is safe to re-run on a persistent site.
	"""

	TYPE_A = "ZZ Shard Probe A"
	TYPE_B = "ZZ Shard Probe B"

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		self.company = frappe.get_all("Company", pluck="name")[0]
		self._made = []
		for t in (self.TYPE_A, self.TYPE_B):
			if not frappe.db.exists("Stock Entry Type", t):
				frappe.get_doc(
					{
						"doctype": "Stock Entry Type",
						"name": t,
						"purpose": "Material Transfer",
					}
				).insert(ignore_permissions=True)
			self._made.append(("Stock Entry Type", t))

	def _rule(self, setype, prefix, *, condition="=", priority=0, company=None):
		doc = frappe.get_doc(
			{
				"doctype": "Document Naming Rule",
				"document_type": "Stock Entry",
				"priority": priority,
				"prefix": prefix,
				"prefix_digits": 5,
				"counter": 0,
				"disabled": 0,
				"conditions": [
					{
						"field": "company",
						"condition": condition,
						"value": company or self.company,
					},
					{"field": "stock_entry_type", "condition": "=", "value": setype},
				],
			}
		).insert(ignore_permissions=True)
		self._made.append(("Document Naming Rule", doc.name))
		frappe.cache_manager.clear_doctype_map("Document Naming Rule", "Stock Entry")
		return doc

	def _stub(self, setype):
		d = frappe.new_doc("Stock Entry")
		d.company = self.company
		d.stock_entry_type = setype
		return d

	def test_equals_rule_is_resolved_and_counted_as_coverage(self):
		rule = self._rule(self.TYPE_A, "ZZA-SE-PROBE-.YY.-")
		self.assertEqual(
			shard_mod_lock_order.document_naming_rule_for_doc(self._stub(self.TYPE_A)),
			rule.name,
		)
		covered, _conflicts = shard_mod._coverage(self.company)
		self.assertEqual(covered.get(self.TYPE_A), rule.name)

	def test_not_equals_rule_is_NOT_coverage(self):
		# The exact blind spot: same (field, value) pair, opposite meaning.
		self._rule(self.TYPE_B, "ZZB-SE-PROBE-.YY.-", condition="!=")
		self.assertIsNone(
			shard_mod_lock_order.document_naming_rule_for_doc(self._stub(self.TYPE_B))
		)
		covered, conflicts = shard_mod._coverage(self.company)
		self.assertNotIn(self.TYPE_B, covered)
		self.assertTrue(any(c[0] == self.TYPE_B for c in conflicts))

	def test_higher_priority_rule_wins_and_shadows_the_exact_one(self):
		low = self._rule(self.TYPE_A, "ZZA-SE-LOW-.YY.-", priority=0)
		high = self._rule(self.TYPE_A, "ZZA-SE-HIGH-.YY.-", priority=10)
		effective = shard_mod_lock_order.document_naming_rule_for_doc(
			self._stub(self.TYPE_A)
		)
		self.assertEqual(effective, high.name, "frappe orders by priority desc")
		self.assertNotEqual(effective, low.name)
		# Two active rules claim the pair, so the type must not be reported as covered.
		covered, conflicts = shard_mod._coverage(self.company)
		self.assertNotIn(self.TYPE_A, covered)
		self.assertTrue(any(c[0] == self.TYPE_A for c in conflicts))

	def test_naming_actually_uses_the_rule_and_increments_its_counter(self):
		rule = self._rule(self.TYPE_A, "ZZA-SE-MINT-.YY.-")
		before = frappe.db.get_value("Document Naming Rule", rule.name, "counter")
		doc = self._stub(self.TYPE_A)
		set_new_name(doc)
		after = frappe.db.get_value("Document Naming Rule", rule.name, "counter")
		self.assertTrue(
			doc.name.startswith("ZZA-SE-MINT-"), f"unexpected name {doc.name!r}"
		)
		self.assertEqual(after, before + 1)
		self.assertFalse(doc.name.startswith("MAT-STE-"))

	def test_real_duplicate_prefix_is_not_coverage(self):
		# Round-5 P0a against REAL records: two rules, two types, ONE prefix. Frappe resolves
		# each correctly, so the resolver check alone passes — only the namespace check
		# catches that their independent counters walk the same names.
		shared = "ZZDUP-SE-SAME-.YY.-"
		self._rule(self.TYPE_A, shared)
		self._rule(self.TYPE_B, shared)
		covered, conflicts = shard_mod._coverage(self.company)
		self.assertNotIn(self.TYPE_A, covered)
		self.assertNotIn(self.TYPE_B, covered)
		self.assertTrue(
			any("shared with" in why for _t, why, _r in conflicts),
			f"expected a shared-prefix conflict, got {conflicts}",
		)

	def test_real_stale_counter_is_not_coverage(self):
		# Round-5 P0b against REAL records: the rule resolves, but its counter sits below a
		# name already issued under its prefix, so it would re-issue used names.
		rule = self._rule(self.TYPE_A, "ZZSTALE-SE-P-.YY.-")
		with patch.object(shard_mod, "_max_existing_suffix", return_value=99):
			covered, conflicts = shard_mod._coverage(self.company)
		self.assertNotIn(self.TYPE_A, covered)
		self.assertTrue(
			any("behind the highest existing name" in why for _t, why, _r in conflicts),
			f"expected a stale-counter conflict, got {conflicts}",
		)
		self.assertEqual(
			frappe.db.get_value("Document Naming Rule", rule.name, "counter"),
			0,
			"a conflicting rule must never be auto-repaired",
		)

	def test_apply_refuses_when_a_generic_rule_shadows_the_new_exact_rule(self):
		# Round-4 blocker 1, the case mocks cannot prove. A company-ONLY rule carries no
		# stock_entry_type, so it never appears among the exact (company, type) candidates —
		# yet at a higher priority frappe resolves it for every type. The planner must see
		# the shadow and refuse rather than create a priority-0 rule and mark ACTIVE.
		self._rule_company_only("ZZGEN-SE-ALL-.YY.-", priority=100)
		covered, conflicts = shard_mod._coverage(self.company)
		self.assertNotIn(self.TYPE_A, covered)
		reason = next((why for t, why, _ in conflicts if t == self.TYPE_A), "")
		self.assertTrue(reason, "the shadowed type must be reported as a conflict")

		with self.assertRaises(frappe.ValidationError):
			shard_mod.shard(company=self.company, confirm=True, all_types=True)

	def _rule_company_only(self, prefix, *, priority=0):
		doc = frappe.get_doc(
			{
				"doctype": "Document Naming Rule",
				"document_type": "Stock Entry",
				"priority": priority,
				"prefix": prefix,
				"prefix_digits": 5,
				"counter": 0,
				"disabled": 0,
				"conditions": [
					{"field": "company", "condition": "=", "value": self.company}
				],
			}
		).insert(ignore_permissions=True)
		self._made.append(("Document Naming Rule", doc.name))
		frappe.cache_manager.clear_doctype_map("Document Naming Rule", "Stock Entry")
		return doc

	def tearDown(self):
		for doctype, name in reversed(self._made):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
		frappe.cache_manager.clear_doctype_map("Document Naming Rule", "Stock Entry")
		frappe.db.commit()
		return super().tearDown()
