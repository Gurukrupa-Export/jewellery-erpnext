# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""C05 -- a configured Stock Entry Type must be first-class everywhere, not just at
batch creation.

`batch_rename.create_parent_batches` was taught to dispatch on the configured type. Three
other sites were not, and two of them are live. A site that configured its own receipt type
got a Stock Entry that validated, minted parent batches and computed pure quantities, and
then:

* produced **no Subcontracting Log rows at all** -- ``ENTRY_TYPE`` is a literal map and
  ``create_subcontracting_log`` returns early on a miss. No throw, no log, no trace; and
* contributed **no opening balance** to the Subcontracting report, while its downstream
  usage rows still appeared -- so every such batch rendered as over-consumed.

Pure-logic per the suite convention: nothing is written and every settings read is patched.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_log import (
	subcontracting_log as log_events,
)
from jewellery_erpnext.customer_subcontracting.report.subcontracting_report import (
	subcontracting_report as cgr_report,
)

LOG_MOD = "jewellery_erpnext.customer_subcontracting.doctype.subcontracting_log.subcontracting_log"
REPORT_MOD = "jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report"

LEGACY = "Customer Goods Received"
CONFIGURED = "CG Intake"


class TestSubcontractingLogDispatch(IntegrationTestCase):
	"""``_entry_config`` / ``_is_inventory_movement`` -- the two hardcodes."""

	@classmethod
	def setUpClass(cls):
		pass

	# -- legacy behaviour must be untouched -------------------------------------
	def test_legacy_types_still_resolve(self):
		for se_type in log_events.ENTRY_TYPE:
			self.assertIsNotNone(log_events._entry_config(se_type, None))

	def test_unknown_type_still_returns_none_when_unconfigured(self):
		self.assertIsNone(log_events._entry_config(CONFIGURED, None))

	def test_legacy_inventory_types_take_the_inventory_branch(self):
		for se_type in log_events._LEGACY_INVENTORY_TYPES:
			self.assertTrue(log_events._is_inventory_movement(se_type, None))

	def test_work_order_types_do_not_take_the_inventory_branch(self):
		for se_type in (
			"Material Transfer to Department",
			"Material Transfer (WORK ORDER)",
		):
			self.assertFalse(log_events._is_inventory_movement(se_type, None))
			self.assertFalse(log_events._is_inventory_movement(se_type, CONFIGURED))

	# -- the configured type ----------------------------------------------------
	def test_configured_type_resolves_to_the_receipt_config(self):
		config = log_events._entry_config(CONFIGURED, CONFIGURED)
		self.assertIsNotNone(config, "a configured receipt type produced no log config")
		self.assertEqual(config, log_events.ENTRY_TYPE[LEGACY])

	def test_configured_type_takes_the_inventory_branch(self):
		self.assertTrue(log_events._is_inventory_movement(CONFIGURED, CONFIGURED))

	def test_a_different_unconfigured_type_is_still_refused(self):
		self.assertIsNone(log_events._entry_config("Some Other Type", CONFIGURED))
		self.assertFalse(
			log_events._is_inventory_movement("Some Other Type", CONFIGURED)
		)

	def test_configured_type_does_not_displace_legacy(self):
		"""Extends, never replaces -- the legacy receipt must keep working."""
		self.assertIsNotNone(log_events._entry_config(LEGACY, CONFIGURED))
		self.assertTrue(log_events._is_inventory_movement(LEGACY, CONFIGURED))

		# -- the settings reader ----------------------------------------------------

	def test_configured_receipt_type_is_none_when_flag_off(self):
		with patch(f"{LOG_MOD}.__name__", LOG_MOD):
			with patch(
				"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings."
				"subcontracting_settings.is_customer_gold_enabled",
				return_value=False,
			):
				self.assertIsNone(log_events._configured_receipt_type())

	def test_configured_receipt_type_is_read_when_flag_on(self):
		with (
			patch(
				"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings."
				"subcontracting_settings.is_customer_gold_enabled",
				return_value=True,
			),
			patch(
				"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings."
				"subcontracting_settings.get_customer_gold_settings",
				return_value=frappe._dict(customer_goods_stock_entry_type=CONFIGURED),
			),
		):
			self.assertEqual(log_events._configured_receipt_type(), CONFIGURED)

	def test_blank_configured_type_reads_as_none(self):
		with (
			patch(
				"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings."
				"subcontracting_settings.is_customer_gold_enabled",
				return_value=True,
			),
			patch(
				"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings."
				"subcontracting_settings.get_customer_gold_settings",
				return_value=frappe._dict(customer_goods_stock_entry_type=""),
			),
		):
			self.assertIsNone(log_events._configured_receipt_type())


class TestSubcontractingReportReceiptTypes(IntegrationTestCase):
	"""The report must count receipts under a configured type as opening balance."""

	@classmethod
	def setUpClass(cls):
		pass

	def _types(self, enabled, configured=None):
		with (
			patch(
				"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings."
				"subcontracting_settings.is_customer_gold_enabled",
				return_value=enabled,
			),
			patch(
				"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings."
				"subcontracting_settings.get_customer_gold_settings",
				return_value=frappe._dict(customer_goods_stock_entry_type=configured),
			),
		):
			return cgr_report.get_customer_goods_receipt_types()

	def test_legacy_only_when_disabled(self):
		self.assertEqual(self._types(False), [LEGACY])

	def test_configured_type_is_included(self):
		types = self._types(True, CONFIGURED)
		self.assertIn(LEGACY, types)
		self.assertIn(CONFIGURED, types)

	def test_legacy_is_never_dropped(self):
		"""Extends, never replaces -- historical receipts must keep their balance."""
		self.assertIn(LEGACY, self._types(True, CONFIGURED))

	def test_configured_equal_to_legacy_is_not_duplicated(self):
		self.assertEqual(self._types(True, LEGACY), [LEGACY])

	def test_blank_configured_type_is_ignored(self):
		self.assertEqual(self._types(True, None), [LEGACY])

	def test_settings_failure_falls_back_to_legacy(self):
		"""A report must render even if settings cannot be read."""
		with patch(
			"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings."
			"subcontracting_settings.is_customer_gold_enabled",
			side_effect=Exception("settings unavailable"),
		):
			self.assertEqual(cgr_report.get_customer_goods_receipt_types(), [LEGACY])

	def test_types_are_passed_as_a_bound_parameter(self):
		"""They reach a SQL IN clause -- they must never be interpolated."""
		captured = {}

		def _sql(query, filters=None, **kwargs):
			captured["query"] = query
			captured["filters"] = filters
			return []

		with (
			patch(f"{REPORT_MOD}.frappe.db.sql", side_effect=_sql),
			patch(
				f"{REPORT_MOD}.get_customer_goods_receipt_types",
				return_value=[LEGACY, CONFIGURED],
			),
		):
			cgr_report.get_cgr_data({"item_code": "X"}, "")

		self.assertIn("%(cgr_stock_entry_types)s", captured["query"])
		self.assertNotIn(CONFIGURED, captured["query"])
		self.assertEqual(
			captured["filters"]["cgr_stock_entry_types"], [LEGACY, CONFIGURED]
		)

	def test_caller_filters_are_not_mutated(self):
		"""The caller's dict must not gain the internal key."""
		filters = {"item_code": "X"}
		with (
			patch(f"{REPORT_MOD}.frappe.db.sql", return_value=[]),
			patch(
				f"{REPORT_MOD}.get_customer_goods_receipt_types", return_value=[LEGACY]
			),
		):
			cgr_report.get_cgr_data(filters, "")
		self.assertNotIn("cgr_stock_entry_types", filters)
