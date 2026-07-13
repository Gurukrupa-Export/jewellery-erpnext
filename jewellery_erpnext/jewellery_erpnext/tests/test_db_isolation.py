# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the opt-in READ COMMITTED isolation hook (db_isolation)."""

from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext import db_isolation


class TestSetReadCommitted(IntegrationTestCase):
	"""set_read_committed() must pin the session per the site_config flag -- READ
	COMMITTED when `use_read_committed` is truthy, REPEATABLE READ when it is off
	(symmetric toggle so warm connections revert) -- and must never raise (an
	isolation hint may not break a request/job)."""

	@classmethod
	def setUpClass(cls):
		pass

	@patch.object(db_isolation.frappe, "db")
	@patch.object(db_isolation.frappe, "conf")
	def test_resets_repeatable_read_when_flag_off(self, mock_conf, mock_db):
		mock_conf.get.return_value = None
		db_isolation.set_read_committed()
		mock_conf.get.assert_called_once_with("use_read_committed")
		mock_db.sql.assert_called_once_with(
			"SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
		)

	@patch.object(db_isolation.frappe, "db")
	@patch.object(db_isolation.frappe, "conf")
	def test_sets_read_committed_when_flag_on(self, mock_conf, mock_db):
		mock_conf.get.return_value = 1
		db_isolation.set_read_committed()
		mock_db.sql.assert_called_once_with(
			"SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED"
		)

	@patch.object(db_isolation.frappe, "logger")
	@patch.object(db_isolation.frappe, "db")
	@patch.object(db_isolation.frappe, "conf")
	def test_swallows_errors_flag_on(self, mock_conf, mock_db, mock_logger):
		# A failed SET must never propagate out of the hook.
		mock_conf.get.return_value = 1
		mock_db.sql.side_effect = RuntimeError("boom")
		try:
			db_isolation.set_read_committed()
		except Exception as e:  # noqa: BLE001
			self.fail(f"set_read_committed must not raise, got {e!r}")
		mock_db.sql.assert_called_once()

	@patch.object(db_isolation.frappe, "logger")
	@patch.object(db_isolation.frappe, "db")
	@patch.object(db_isolation.frappe, "conf")
	def test_swallows_errors_flag_off(self, mock_conf, mock_db, mock_logger):
		# The reset branch must be equally failure-proof.
		mock_conf.get.return_value = None
		mock_db.sql.side_effect = RuntimeError("boom")
		try:
			db_isolation.set_read_committed()
		except Exception as e:  # noqa: BLE001
			self.fail(f"set_read_committed must not raise, got {e!r}")
		mock_db.sql.assert_called_once()

	def tearDown(self):
		return super().tearDown()
