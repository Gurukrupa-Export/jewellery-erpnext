# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Pins the guards on the two EOD reservation repairs and on the stuck-draft retry.

* THAT ``dry_run`` only writes on an explicit 0 / false / no / off -- ``dry_run=true`` sent
  over HTTP used to run the repair for real, because ``cint("true")`` is 0.
* THAT both repairs accept POST only and refuse a user without System Manager before doing
  any work, while Administrator (``bench execute``) still runs them.
* THAT the retry script skips an MWO whose EOD draft was edited by a person, and still
  retries an untouched one.

DB-free: ``setUpClass`` is neutralised and every DB read is mocked.

Run with:
  bench --site gk run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_mop_eod_repair_guards
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings import mop_eod_sync as eod
from jewellery_erpnext.patches import retry_stuck_eod_submit_mwos as retry

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync"
REPAIRS = ("backfill_missing_wip_reservations", "heal_reservation_shortfall")


class TestParseDryRun(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_only_an_explicit_no_writes(self):
		cases = {
			True: True,
			False: False,
			1: True,
			0: False,
			"1": True,
			"0": False,
			"true": True,
			"True": True,
			"false": False,
			"FALSE": False,
			" no ": False,
			"off": False,
			"yes": True,
			"": True,
			None: True,
			"anything": True,
		}
		for value, expected in cases.items():
			self.assertIs(eod._parse_dry_run(value), expected, repr(value))


class TestRepairEndpointGuards(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_post_only(self):
		for name in REPAIRS:
			fn = getattr(eod, name)
			self.assertEqual(
				frappe.allowed_http_methods_for_whitelisted_func.get(fn), ["POST"], name
			)

	def test_non_manager_is_refused_before_any_work(self):
		for name in REPAIRS:
			with patch.object(
				frappe.local, "session", frappe._dict(user="clerk@example.invalid")
			), patch.object(frappe, "get_roles", return_value=["Stock User"]), patch(
				f"{MOD}.frappe.db", new_callable=MagicMock
			) as db:
				with self.assertRaises(frappe.PermissionError):
					getattr(eod, name)(mwo="MWO-1", dry_run=True)
			self.assertEqual(db.method_calls, [], name)

	def test_administrator_passes_the_role_check(self):
		with patch.object(frappe.local, "session", frappe._dict(user="Administrator")):
			self.assertIsNone(eod._require_repair_role())

	def test_system_manager_passes_the_role_check(self):
		with patch.object(
			frappe.local, "session", frappe._dict(user="lead@example.invalid")
		), patch.object(frappe, "get_roles", return_value=["System Manager"]):
			self.assertIsNone(eod._require_repair_role())


class TestBackfillDryRunOverHttp(IntegrationTestCase):
	"""The same mocks as test_mop_settings' backfill test, with ``dry_run`` as HTTP sends it."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, dry_run):
		with patch(f"{MOD}._list_open_sre_other_warehouses", return_value=[]), patch(
			f"{MOD}._reserve_batch_at_physical_warehouse"
		) as reserve, patch(f"{MOD}._active_sre_exists", return_value=False), patch(
			f"{MOD}.frappe.db", new_callable=MagicMock
		) as db, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
			return_value=[
				{
					"item_code": "ITEM-1",
					"batch_no": "B1",
					"qty": 10.0,
					"to_warehouse": "WH-A",
				}
			],
		):
			db.get_value.return_value = ("Op1", "Test Co")
			eod.backfill_missing_wip_reservations(mwo="MWO-1", dry_run=dry_run)
		return reserve

	def test_true_string_only_reports(self):
		self._run("true").assert_not_called()

	def test_false_string_repairs(self):
		self._run("false").assert_called_once_with(
			"MWO-1", "ITEM-1", "B1", 10.0, "Op1", "Test Co"
		)


class TestRetrySkipsEditedDrafts(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _db(self, edited_by=None, version_by_other=False):
		"""Two MWOs, each with one EOD draft. ``edited_by`` saves MWO-A's draft as that user."""
		db = MagicMock()

		def get_value(doctype, name, fields, as_dict=False):
			if fields == ["docstatus", "custom_eod_sync_source"]:
				return {"docstatus": 0, "custom_eod_sync_source": "MOP EOD Sync"}
			if fields == ["owner", "modified_by"]:
				editor = edited_by if name == "SE-A" else None
				return frappe._dict(
					owner="Administrator", modified_by=editor or "Administrator"
				)
			raise AssertionError((doctype, name, fields))

		versions = [("SE-A", "clerk@example.invalid")] if version_by_other else []
		versions += [
			("SE-A", "Administrator"),
			("SE-B", "Administrator"),
		]  # EOD's own saves

		def owner_matches(owner, rule):
			if isinstance(rule, list) and rule[0] == "!=":
				return owner != rule[1]
			return owner == rule

		def exists(doctype, filters):
			if doctype == "Version":
				return any(
					docname == filters["docname"]
					and owner_matches(owner, filters["owner"])
					for docname, owner in versions
				)
			if doctype == "MOP Log":
				return True  # both still have unsynced logs
			raise AssertionError((doctype, filters))

		db.get_value.side_effect = get_value
		db.exists.side_effect = exists
		return db

	def _classify(self, db):
		rows = [
			frappe._dict(manufacturing_work_order="MWO-A", draft_stock_entry="SE-A"),
			frappe._dict(manufacturing_work_order="MWO-B", draft_stock_entry="SE-B"),
		]
		with patch.object(retry.frappe, "get_all", return_value=rows), patch.object(
			retry.frappe, "db", db
		), patch("builtins.print"):
			return retry._classify()

	def test_untouched_drafts_are_retried(self):
		retryable, skipped = self._classify(self._db())

		self.assertEqual(retryable, {"MWO-A": ["SE-A"], "MWO-B": ["SE-B"]})
		self.assertEqual(skipped, {})

	def test_draft_saved_by_someone_else_is_skipped(self):
		retryable, skipped = self._classify(self._db(edited_by="clerk@example.invalid"))

		self.assertEqual(retryable, {"MWO-B": ["SE-B"]})
		self.assertEqual(skipped, {"MWO-A": ["SE-A"]})

	def test_version_by_someone_else_is_skipped(self):
		retryable, skipped = self._classify(self._db(version_by_other=True))

		self.assertEqual(retryable, {"MWO-B": ["SE-B"]})
		self.assertEqual(skipped, {"MWO-A": ["SE-A"]})

	def test_missing_stock_entry_counts_as_untouched(self):
		db = MagicMock()
		db.get_value.return_value = None
		with patch.object(retry.frappe, "db", db):
			self.assertFalse(retry._edited_by_a_person("SE-GONE"))
