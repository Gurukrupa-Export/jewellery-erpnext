# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the gated Stock Entry naming shard
(``jewellery_erpnext.patches.shard_stock_entry_naming_by_type``).

These cover the three findings raised on PR #1225:

* rollback must disable ONLY the rules the patch created — on kggk-prod the old
  prefix-matching predicate also swept up five pre-existing ``KGJPL-SE-*`` Customer-Goods
  rules created months earlier;
* every defined Stock Entry Type must be covered, and a type created later must not
  silently fall back to the shared ``MAT-STE-`` row;
* a dry run must write nothing.
"""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

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
	"""Round-2 Finding 1: only an ACTIVE, exactly-matching rule counts as coverage.

	A disabled rule names nothing, so treating it as coverage leaves the type on the shared
	MAT-STE- row — and after a rollback it made the whole shard un-reappliable.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _rows(self, *specs):
		return [
			frappe._dict(
				name=n, disabled=d, n_conditions=nc, company=_COMPANY, setype=t
			)
			for n, t, d, nc in specs
		]

	def test_disabled_rule_is_a_conflict_not_coverage(self):
		with patch.object(
			shard_mod,
			"_rule_rows",
			return_value=self._rows(("R1", "Manufacture", 1, 2)),
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertEqual([(t, r) for t, _why, r in conflicts], [("Manufacture", "R1")])
		self.assertIn("DISABLED", conflicts[0][1])

	def test_rule_with_extra_conditions_is_a_conflict(self):
		# e.g. company + stock_entry_type + purpose: it covers only a SUBSET of the type,
		# so assuming general coverage would leave the rest on MAT-STE-.
		with patch.object(
			shard_mod, "_rule_rows", return_value=self._rows(("R1", "Repack", 0, 3))
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {})
		self.assertIn("3 conditions", conflicts[0][1])

	def test_duplicate_active_rules_are_a_conflict(self):
		with patch.object(
			shard_mod,
			"_rule_rows",
			return_value=self._rows(("R1", "Repack", 0, 2), ("R2", "Repack", 0, 2)),
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {"Repack": "R1"})
		self.assertIn("multiple active rules", conflicts[0][1])

	def test_plain_active_rule_is_coverage(self):
		with patch.object(
			shard_mod, "_rule_rows", return_value=self._rows(("R1", "Repack", 0, 2))
		):
			covered, conflicts = shard_mod._coverage(_COMPANY)
		self.assertEqual(covered, {"Repack": "R1"})
		self.assertEqual(conflicts, [])

	def test_reenablable_rule_counts_as_coverage_not_a_conflict(self):
		# A disabled rule THIS patch created and is about to re-enable is the expected
		# rolled-back state. It must be reported as covered — neither as a conflict (which
		# would tell the operator 44 types are broken mid-restore) nor as uncovered (which
		# made _plan mint a duplicate rule with a "-2" prefix beside every original).
		with patch.object(
			shard_mod,
			"_rule_rows",
			return_value=self._rows(("R1", "Manufacture", 1, 2)),
		):
			covered, conflicts = shard_mod._coverage(_COMPANY, reenablable=["R1"])
		self.assertEqual(covered, {"Manufacture": "R1"})
		self.assertEqual(conflicts, [])

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
		), patch.object(shard_mod.frappe.db, "set_value"), patch.object(
			shard_mod.frappe.db, "commit"
		), patch.object(shard_mod.frappe, "clear_cache"), patch.object(
			shard_mod, "_set_state"
		) as mock_state:
			shard_mod.rollback()
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
			side_effect=lambda dt, n, f, **k: disabled.get(n, 0),
		), patch.object(shard_mod.frappe.db, "set_value") as mock_set, patch.object(
			shard_mod, "_record_created", return_value=list(disabled)
		), patch.object(shard_mod, "_set_state") as mock_state, patch.object(
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
		with patch.object(
			shard_mod, "_plan", return_value=(plan, [], [])
		), patch.object(
			shard_mod, "_default_company", return_value=_COMPANY
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
			shard_mod.frappe.db, "set_value"
		) as mock_set, patch.object(shard_mod.frappe.db, "commit"), patch.object(
			shard_mod.frappe, "clear_cache"
		):
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
			created = shard_mod.ensure_rules_for_type("Anything")
		self.assertEqual(created, [])
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
			created = shard_mod.ensure_rules_for_type("Brand New Type")
		self.assertEqual(created, ["NEW-RULE"])  # only the matching type, not "Other"
		mock_record.assert_called_once_with(_COMPANY, ["NEW-RULE"])

	def test_hook_never_raises(self):
		# A naming-rule gap must never block creating a Stock Entry Type.
		with patch.object(
			shard_mod, "ensure_rules_for_type", side_effect=RuntimeError("boom")
		), patch.object(shard_mod.frappe, "log_error") as mock_log:
			shard_mod.on_stock_entry_type_insert(frappe._dict(name="X"))
		mock_log.assert_called_once()

	def tearDown(self):
		return super().tearDown()
