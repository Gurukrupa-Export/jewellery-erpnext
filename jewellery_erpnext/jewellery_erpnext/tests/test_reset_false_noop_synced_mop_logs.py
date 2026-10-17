"""Coverage for the false-no-op MOP Log remediation script.

``patches/reset_false_noop_synced_mop_logs.py`` rewrites historical EOD state -- it flips
``MOP Log.is_synced`` back to 0 and commits -- so what it will and will not touch has to be
pinned before it is run on a production site. It is not registered in ``patches.txt`` and
never runs during ``bench migrate``; it is invoked by hand, dry-run first.

Every DB call is mocked (house pattern). ``frappe.db.sql`` is routed by a distinctive
fragment of each query, and the UPDATE is captured rather than executed, so these tests
assert the exact selection and the exact write without touching a row.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import reset_false_noop_synced_mop_logs as _script

_MOD = "jewellery_erpnext.patches.reset_false_noop_synced_mop_logs"


def _target(mwo, warehouse):
	return frappe._dict(mwo=mwo, target_warehouse=warehouse)


def _sre(name, warehouse, item_code="M-1", batch_no="B1", remaining=0.595):
	return frappe._dict(
		name=name,
		item_code=item_code,
		batch_no=batch_no,
		warehouse=warehouse,
		remaining=remaining,
	)


class _FakeSql:
	"""Routes the script's four reads and captures its one write."""

	def __init__(self, targets=None, buried=None, current_wh=None, stranded=None):
		self.targets = targets or []
		self.buried = buried or {}
		self.current_wh = current_wh or {}
		self.stranded = stranded or {}
		self.updates = []
		self.target_params = None
		self.stranded_params = []

	def __call__(self, query, params=None, as_dict=False):
		if "tabMOP EOD Sync Log Item" in query:
			self.target_params = (query, params)
			return list(self.targets)

		if "COUNT(*)" in query:
			return [[self.buried.get(params[0], 0)]]

		if "SELECT to_warehouse" in query:
			warehouse = self.current_wh.get(params[0])
			return [(warehouse,)] if warehouse else []

		if "tabStock Reservation Entry" in query:
			self.stranded_params.append(params)
			excluded = set(params["warehouses"])
			return [
				row
				for row in self.stranded.get(params["mwo"], [])
				if row.warehouse not in excluded
			]

		if query.strip().startswith("UPDATE"):
			self.updates.append((query, params))
			return None

		raise AssertionError(f"unexpected query: {query[:120]}")


class TestResetFalseNoopSyncedMopLogs(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, fake, **kwargs):
		"""Execute the script with its DB surface mocked.

		``flt`` is stubbed because the only call is in a progress ``print`` and its
		precision lookup would otherwise reach the database through the patched sql.
		"""
		with (
			patch(f"{_MOD}.frappe.db.sql", side_effect=fake),
			patch(f"{_MOD}.frappe.db.commit") as commit,
			patch(f"{_MOD}.flt", side_effect=lambda v, p=None: float(v or 0)),
		):
			_script.execute(**kwargs)
		return commit

	# -- dry run -----------------------------------------------------------------

	def test_dry_run_is_the_default_and_writes_nothing(self):
		"""The signature default must stay ``dry_run=True``: a bare
		``bench execute`` of this script is a report, never a mutation."""
		fake = _FakeSql(
			targets=[_target("MWO-1", "WH-DEPT")],
			buried={"MWO-1": 4},
			current_wh={"MWO-1": "WH-DEPT"},
			stranded={"MWO-1": [_sre("SRE-1", "WH-PREV")]},
		)
		commit = self._run(fake)

		self.assertEqual(fake.updates, [], "dry run must not issue the UPDATE")
		self.assertFalse(commit.called, "dry run must not commit")

	def test_dry_run_and_apply_select_the_same_mwos(self):
		"""The report is only useful if it predicts the write exactly."""

		def _fresh():
			return _FakeSql(
				targets=[_target("MWO-1", "WH-DEPT"), _target("MWO-2", "WH-DEPT")],
				buried={"MWO-1": 4, "MWO-2": 2},
				current_wh={"MWO-1": "WH-DEPT", "MWO-2": "WH-DEPT"},
				stranded={
					"MWO-1": [_sre("SRE-1", "WH-PREV")],
					"MWO-2": [_sre("SRE-2", "WH-PREV")],
				},
			)

		dry = _fresh()
		self._run(dry)
		applied = _fresh()
		self._run(applied, dry_run=False)

		self.assertEqual(dry.updates, [])
		self.assertEqual(applied.updates[0][1], {"mwos": ["MWO-1", "MWO-2"]})

	# -- the write ---------------------------------------------------------------

	def test_apply_resets_only_synced_uncancelled_logs_of_repairable_mwos(self):
		fake = _FakeSql(
			targets=[_target("MWO-1", "WH-DEPT")],
			buried={"MWO-1": 4},
			current_wh={"MWO-1": "WH-DEPT"},
			stranded={"MWO-1": [_sre("SRE-1", "WH-PREV")]},
		)
		commit = self._run(fake, dry_run=False)

		self.assertEqual(len(fake.updates), 1)
		query, params = fake.updates[0]
		self.assertEqual(params, {"mwos": ["MWO-1"]})
		# The write is narrow in all three dimensions, not just the MWO list.
		self.assertIn("SET is_synced = 0", query)
		self.assertIn("AND is_synced = 1", query)
		self.assertIn("AND is_cancelled = 0", query)
		self.assertTrue(commit.called)

	def test_nothing_is_written_when_no_noop_lines_match(self):
		fake = _FakeSql(targets=[])
		commit = self._run(fake, dry_run=False)

		self.assertEqual(fake.updates, [])
		self.assertFalse(commit.called)

	# -- selection scope ---------------------------------------------------------

	def test_genuine_noop_is_left_alone(self):
		"""No stranded reservation means the stock really had moved. Resetting such an
		MWO would re-run a transfer that legitimately never needed to happen."""
		fake = _FakeSql(
			targets=[_target("MWO-OK", "WH-DEPT")],
			buried={"MWO-OK": 3},
			current_wh={"MWO-OK": "WH-DEPT"},
			stranded={"MWO-OK": []},
		)
		commit = self._run(fake, dry_run=False)

		self.assertEqual(fake.updates, [])
		self.assertFalse(commit.called)

	def test_reservation_that_followed_the_metal_is_not_stranded(self):
		"""An Employee IR issue legitimately moves stock past the department onto the
		operator's WIP warehouse. A reservation sitting where the MOP Log now says the
		metal is has followed it, so the no-op was true."""
		fake = _FakeSql(
			targets=[_target("MWO-1", "WH-DEPT")],
			buried={"MWO-1": 3},
			current_wh={"MWO-1": "WH-OPERATOR"},
			stranded={"MWO-1": [_sre("SRE-1", "WH-OPERATOR")]},
		)
		self._run(fake, dry_run=False)

		self.assertEqual(fake.updates, [])
		# Both the no-op target and the current warehouse are excluded from the hunt.
		self.assertEqual(
			fake.stranded_params[0]["warehouses"], ["WH-DEPT", "WH-OPERATOR"]
		)

	def test_every_noop_target_the_mwo_ever_had_is_excluded(self):
		"""One MWO can carry several no-ops across runs; a reservation at any of those
		targets has settled and must not count as stranded."""
		fake = _FakeSql(
			targets=[_target("MWO-1", "WH-A"), _target("MWO-1", "WH-B")],
			buried={"MWO-1": 2},
			current_wh={"MWO-1": "WH-C"},
			stranded={"MWO-1": [_sre("SRE-1", "WH-B")]},
		)
		self._run(fake, dry_run=False)

		self.assertEqual(
			fake.stranded_params[0]["warehouses"], ["WH-A", "WH-B", "WH-C"]
		)
		self.assertEqual(fake.updates, [])

	def test_idempotent_second_run_finds_nothing_to_reset(self):
		"""After a repair the logs are unsynced, so the buried count is 0 and the MWO
		drops out before the reservation hunt. Safe to re-run."""
		fake = _FakeSql(
			targets=[_target("MWO-1", "WH-DEPT")],
			buried={"MWO-1": 0},
			current_wh={"MWO-1": "WH-DEPT"},
			stranded={"MWO-1": [_sre("SRE-1", "WH-PREV")]},
		)
		commit = self._run(fake, dry_run=False)

		self.assertEqual(fake.updates, [])
		self.assertFalse(commit.called)
		self.assertEqual(
			fake.stranded_params, [], "an already-repaired MWO is skipped early"
		)

	def test_limit_caps_the_number_of_repaired_mwos(self):
		fake = _FakeSql(
			targets=[_target(f"MWO-{i}", "WH-DEPT") for i in range(1, 4)],
			buried={f"MWO-{i}": 2 for i in range(1, 4)},
			current_wh={f"MWO-{i}": "WH-DEPT" for i in range(1, 4)},
			stranded={f"MWO-{i}": [_sre("SRE-1", "WH-PREV")] for i in range(1, 4)},
		)
		self._run(fake, dry_run=False, limit=2)

		self.assertEqual(fake.updates[0][1], {"mwos": ["MWO-1", "MWO-2"]})

	# -- filters -----------------------------------------------------------------

	def test_mwo_filter_narrows_the_candidate_query(self):
		fake = _FakeSql(targets=[])
		self._run(fake, mwo="MWO-1")

		query, params = fake.target_params
		self.assertIn("li.manufacturing_work_order = %(mwo)s", query)
		self.assertEqual(params["mwo"], "MWO-1")
		self.assertNotIn("li.parent", query)

	def test_sync_log_filter_narrows_the_candidate_query(self):
		fake = _FakeSql(targets=[])
		self._run(fake, sync_log="MOP-EOD-SYNC-2026-00068")

		query, params = fake.target_params
		self.assertIn("li.parent = %(sync_log)s", query)
		self.assertEqual(params["sync_log"], "MOP-EOD-SYNC-2026-00068")
		self.assertNotIn("li.manufacturing_work_order = %(mwo)s", query)

	def test_unfiltered_run_still_scopes_to_the_noop_marker(self):
		fake = _FakeSql(targets=[])
		self._run(fake)

		query, params = fake.target_params
		self.assertIn("li.error_message LIKE %(marker)s", query)
		self.assertEqual(params["marker"], _script.NOOP_MARKER)
		# Cancelled sync log lines are not evidence of anything.
		self.assertIn("li.docstatus < 2", query)

	def test_noop_marker_still_matches_what_the_sync_writes(self):
		"""The marker is a prefix LIKE because the EOD no-op line now appends the
		item/batch/qty detail. If that sentence is reworded the script silently stops
		finding anything, so pin the two together."""
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings import (
			mop_eod_sync,
		)

		self.assertTrue(_script.NOOP_MARKER.endswith("%"))
		prefix = _script.NOOP_MARKER[:-1]
		source = frappe.read_file(mop_eod_sync.__file__)
		self.assertIn(prefix, source)


if __name__ == "__main__":
	unittest.main()
