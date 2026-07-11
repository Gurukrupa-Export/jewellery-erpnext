# Copyright (c) 2026, Nirali and contributors
# See license.txt
#
# Pure-logic / mocked tests for the "Mould List ID" propagation across
# Manufacturing Plan -> Parent Manufacturing Order -> Manufacturing Work Order.
# The propagated value is the Mould record's docname (get via ``Mould.name``), NOT the
# Mould's own ``mould_no`` location string. No real DB access -- avoids the
# india_compliance GST bootstrap that aborts doctype-folder tests on this app.

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan import (
	manufacturing_plan as mp_mod,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mould.doc_events import mwo_sync, utils
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order import (
	parent_manufacturing_order as pmo_mod,
)


class _Doc(SimpleNamespace):
	"""SimpleNamespace that also supports Frappe-style ``.get()`` access."""

	def get(self, key, default=None):
		return getattr(self, key, default)


class TestMouldIdLookup(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	# ---- get_current_mould_id -------------------------------------------

	def test_returns_mould_docname(self):
		with patch.object(
			utils.frappe.db, "get_value", return_value="M-GEPL-NE-00001"
		) as get_value:
			result = utils.get_current_mould_id("NE00468-001")
		self.assertEqual(result, "M-GEPL-NE-00001")
		# It must query the Mould *name* (the Mould List ID), not mould_no.
		args = get_value.call_args[0]
		self.assertEqual(args[0], "Mould")
		self.assertEqual(args[1], {"item_code": "NE00468-001"})
		self.assertEqual(args[2], "name")

	def test_blank_item_code_short_circuits(self):
		with patch.object(utils.frappe.db, "get_value") as get_value:
			self.assertIsNone(utils.get_current_mould_id(None))
		get_value.assert_not_called()

	# ---- get_mould_id_map -----------------------------------------------

	def test_map_keyed_item_code_to_name_first_wins(self):
		rows = [
			frappe._dict(item_code="A", name="M-A-2"),  # newest (creation desc) -> kept
			frappe._dict(item_code="A", name="M-A-1"),
			frappe._dict(item_code="B", name="M-B-1"),
		]
		with patch.object(utils.frappe, "get_all", return_value=rows) as get_all:
			result = utils.get_mould_id_map(["A", "B", "A", None])
		self.assertEqual(result, {"A": "M-A-2", "B": "M-B-1"})
		self.assertEqual(get_all.call_args[1]["fields"], ["item_code", "name"])

	def test_map_empty_input_short_circuits(self):
		with patch.object(utils.frappe, "get_all") as get_all:
			self.assertEqual(utils.get_mould_id_map([None, ""]), {})
		get_all.assert_not_called()


class TestSyncMouldId(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_mwo_validate_hook_sets_mould_id(self):
		doc = _Doc(item_code="NE00468-001", mould_id=None)
		with patch.object(
			mwo_sync, "get_current_mould_id", return_value="M-GEPL-NE-00001"
		):
			mwo_sync.sync_mould_id(doc)
		self.assertEqual(doc.mould_id, "M-GEPL-NE-00001")

	def test_pmo_validate_sets_mould_id_on_insert(self):
		# The assignment must fire even when is_new() is True (before the early-return),
		# so a freshly-created PMO carries the Mould List ID.
		fake = _Doc(
			item_code="NE00468-001",
			mould_id=None,
			flags=SimpleNamespace(ignore_validations=False),
		)
		fake.is_new = lambda: True
		with patch.object(
			pmo_mod, "get_current_mould_id", return_value="M-GEPL-NE-00001"
		):
			pmo_mod.ParentManufacturingOrder.validate(fake)
		self.assertEqual(fake.mould_id, "M-GEPL-NE-00001")

	def test_pmo_validate_sets_mould_id_when_ignore_validations(self):
		fake = _Doc(
			item_code="NE00468-001",
			mould_id=None,
			flags=SimpleNamespace(ignore_validations=True),
		)
		fake.is_new = lambda: False
		with patch.object(
			pmo_mod, "get_current_mould_id", return_value="M-GEPL-NE-00001"
		):
			pmo_mod.ParentManufacturingOrder.validate(fake)
		self.assertEqual(fake.mould_id, "M-GEPL-NE-00001")


class TestManufacturingPlanRefresh(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_refresh_sets_every_row(self):
		rows = [
			_Doc(item_code="A", mould_id="stale"),
			_Doc(item_code="B", mould_id=None),
			_Doc(item_code="C", mould_id=None),  # no Mould -> blank
		]
		fake = _Doc(manufacturing_plan_table=rows)
		with patch.object(
			mp_mod, "get_mould_id_map", return_value={"A": "M-A-1", "B": "M-B-1"}
		):
			mp_mod.ManufacturingPlan.refresh_mould_ids(fake)
		self.assertEqual(rows[0].mould_id, "M-A-1")  # stale overwritten
		self.assertEqual(rows[1].mould_id, "M-B-1")
		self.assertIsNone(rows[2].mould_id)  # item with no Mould -> blank, not an error

	def test_refresh_noop_without_item_codes(self):
		fake = _Doc(manufacturing_plan_table=[_Doc(item_code=None, mould_id=None)])
		with patch.object(mp_mod, "get_mould_id_map") as get_map:
			mp_mod.ManufacturingPlan.refresh_mould_ids(fake)
		get_map.assert_not_called()


class TestMouldUniqueness(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_duplicate_item_throws(self):
		fake = _Doc(item_code="NE00468-001", name="M-NEW")
		with (
			patch.object(
				utils, "mould_exists_for_item", return_value="M-GEPL-NE-00001"
			),
			patch.object(utils.frappe, "throw", side_effect=RuntimeError) as throw,
		):
			with self.assertRaises(RuntimeError):
				utils.validate_unique_item_code(fake)
		self.assertIn("NE00468-001", throw.call_args[0][0])

	def test_unique_item_passes(self):
		fake = _Doc(item_code="NE00468-001", name="M-NEW")
		with (
			patch.object(utils, "mould_exists_for_item", return_value=None),
			patch.object(utils.frappe, "throw") as throw,
		):
			utils.validate_unique_item_code(fake)
		throw.assert_not_called()


class TestUpdateDetailsLocation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_computes_location_when_all_present(self):
		fake = _Doc(
			warehouse="WH-1",
			rake="b",
			tray_no="1",
			box_no="2",
			item_code="NE00468-001",
			mould_no=None,
		)
		with (
			patch.object(utils.frappe.db, "get_value", return_value="A"),
			patch.object(utils.frappe.db, "set_value") as set_value,
		):
			utils.update_details(fake)
		self.assertEqual(fake.mould_no, "A/B/01/02")
		set_value.assert_called_once_with("Item", "NE00468-001", "mould", "A/B/01/02")

	def test_skips_cleanly_when_field_missing(self):
		# Auto-created Mould (blank warehouse/rake/tray/box): no throw, no cache write.
		fake = _Doc(
			warehouse=None,
			rake=None,
			tray_no=None,
			box_no=None,
			item_code="NE00468-001",
			mould_no=None,
		)
		with (
			patch.object(utils.frappe.db, "get_value") as get_value,
			patch.object(utils.frappe.db, "set_value") as set_value,
			patch.object(utils.frappe, "throw", side_effect=RuntimeError),
		):
			utils.update_details(fake)
		self.assertIsNone(fake.mould_no)
		get_value.assert_not_called()
		set_value.assert_not_called()
