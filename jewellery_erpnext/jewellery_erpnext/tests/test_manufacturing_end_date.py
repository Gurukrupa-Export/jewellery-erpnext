# Copyright (c) 2026, Nirali and contributors
# See license.txt
#
# Pure-logic / mocked tests for the "Est. MFG End Date" flow across
# Manufacturing Plan (header) -> Manufacturing Plan Table (rows) -> Parent
# Manufacturing Order. No real DB access -- avoids the india_compliance GST
# bootstrap that aborts doctype-folder tests on this app.

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan import (
	manufacturing_plan as mp_mod,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order import (
	parent_manufacturing_order as pmo_mod,
)


class _Doc(SimpleNamespace):
	"""SimpleNamespace that also supports Frappe-style ``.get()`` access."""

	def get(self, key, default=None):
		return getattr(self, key, default)


class _NewDoc(_Doc):
	"""Stands in for ``frappe.new_doc`` output: records the insert instead of writing."""

	def insert(self, *args, **kwargs):
		self.inserted = True


class TestApplyManufacturingEndDate(IntegrationTestCase):
	"""The header is a bulk-apply helper -- see ManufacturingPlan.apply_manufacturing_end_date."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_header_stamps_every_row(self):
		rows = [
			_Doc(manufacturing_end_date=None),
			_Doc(manufacturing_end_date=None),
		]
		fake = _Doc(manufacturing_end_date="2026-09-01", manufacturing_plan_table=rows)
		mp_mod.ManufacturingPlan.apply_manufacturing_end_date(fake)
		self.assertEqual(rows[0].manufacturing_end_date, "2026-09-01")
		self.assertEqual(rows[1].manufacturing_end_date, "2026-09-01")

	def test_header_overwrites_differing_row_values(self):
		# The invariant: a non-empty header means every row is meant to carry it, so the
		# header wins over whatever a row already held.
		rows = [
			_Doc(manufacturing_end_date="2026-09-10"),
			_Doc(manufacturing_end_date="2026-08-20"),
		]
		fake = _Doc(manufacturing_end_date="2026-09-01", manufacturing_plan_table=rows)
		mp_mod.ManufacturingPlan.apply_manufacturing_end_date(fake)
		self.assertEqual(rows[0].manufacturing_end_date, "2026-09-01")
		self.assertEqual(rows[1].manufacturing_end_date, "2026-09-01")

	def test_empty_header_leaves_rows_alone(self):
		# Blanking the header must NOT wipe rows -- it only stops future pushes.
		rows = [
			_Doc(manufacturing_end_date="2026-09-01"),
			_Doc(manufacturing_end_date="2026-09-10"),
		]
		fake = _Doc(manufacturing_end_date=None, manufacturing_plan_table=rows)
		mp_mod.ManufacturingPlan.apply_manufacturing_end_date(fake)
		self.assertEqual(rows[0].manufacturing_end_date, "2026-09-01")
		self.assertEqual(rows[1].manufacturing_end_date, "2026-09-10")


class TestHeaderPushSatisfiesRowMandatory(IntegrationTestCase):
	"""`reqd` lives on the child row, never on the header.

	A mandatory header would be unsaveable by design: the client blanks it the moment one
	row is given its own date. The row-level requirement is still satisfiable from the
	header alone because Document.insert runs run_before_save_methods() -- and therefore
	apply_manufacturing_end_date() -- ahead of _validate()."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_header_push_fills_rows_before_mandatory_would_see_them(self):
		rows = [_Doc(manufacturing_end_date=None), _Doc(manufacturing_end_date=None)]
		fake = _Doc(manufacturing_end_date="2026-09-01", manufacturing_plan_table=rows)
		mp_mod.ManufacturingPlan.apply_manufacturing_end_date(fake)
		# Nothing is left blank for the framework's mandatory check to reject.
		self.assertTrue(all(row.manufacturing_end_date for row in rows))

	def test_blank_header_with_rows_already_dated_is_a_valid_state(self):
		# THE REGRESSION CASE: exactly what the client produces after a per-row edit, and
		# exactly what `reqd: 1` on the *header* used to reject outright.
		rows = [
			_Doc(
				manufacturing_end_date="2026-09-01",
				sales_order=None,
				delivery_date=None,
			),
			_Doc(
				manufacturing_end_date="2026-09-10",
				sales_order=None,
				delivery_date=None,
			),
		]
		fake = _Doc(manufacturing_end_date=None, manufacturing_plan_table=rows)
		with (
			patch.object(mp_mod.frappe, "get_all", return_value=[]),
			patch.object(mp_mod.frappe, "throw", side_effect=RuntimeError) as throw,
		):
			mp_mod.ManufacturingPlan.apply_manufacturing_end_date(fake)
			mp_mod.ManufacturingPlan.validate_manufacturing_end_date(fake)
		throw.assert_not_called()
		# The push must not have wiped the individually-set row dates.
		self.assertEqual(rows[0].manufacturing_end_date, "2026-09-01")
		self.assertEqual(rows[1].manufacturing_end_date, "2026-09-10")


class TestValidateManufacturingEndDate(IntegrationTestCase):
	"""ManufacturingPlan.validate_manufacturing_end_date mirrors the PMO's validate_mfg_date."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, rows, sales_orders=None):
		fake = _Doc(manufacturing_plan_table=rows)
		with (
			patch.object(mp_mod.frappe, "get_all", return_value=sales_orders or []),
			patch.object(mp_mod.frappe, "throw", side_effect=RuntimeError) as throw,
		):
			try:
				mp_mod.ManufacturingPlan.validate_manufacturing_end_date(fake)
			except RuntimeError:
				pass
		return throw

	def test_no_dated_rows_short_circuits(self):
		rows = [
			_Doc(
				manufacturing_end_date=None,
				sales_order="SO-1",
				delivery_date="2026-09-15",
			)
		]
		fake = _Doc(manufacturing_plan_table=rows)
		with patch.object(mp_mod.frappe, "get_all") as get_all:
			mp_mod.ManufacturingPlan.validate_manufacturing_end_date(fake)
		get_all.assert_not_called()

	def test_date_before_delivery_date_passes(self):
		rows = [
			_Doc(
				idx=1,
				manufacturing_end_date="2026-09-01",
				sales_order="SO-1",
				delivery_date="2026-09-15",
			)
		]
		throw = self._run(rows)
		throw.assert_not_called()

	def test_date_equal_to_delivery_date_throws(self):
		# The PMO guard is `>=`, so same-day is not allowed either.
		rows = [
			_Doc(
				idx=1,
				manufacturing_end_date="2026-09-15",
				sales_order="SO-1",
				delivery_date="2026-09-15",
			)
		]
		throw = self._run(rows)
		throw.assert_called_once()

	def test_date_after_delivery_date_throws_naming_the_row(self):
		rows = [
			_Doc(
				idx=1,
				manufacturing_end_date="2026-09-01",
				sales_order="SO-1",
				delivery_date="2026-09-15",
			),
			_Doc(
				idx=2,
				manufacturing_end_date="2026-10-01",
				sales_order="SO-1",
				delivery_date="2026-09-15",
			),
		]
		throw = self._run(rows)
		throw.assert_called_once()
		self.assertIn("Row #2", throw.call_args[0][0])

	def test_updated_delivery_date_on_sales_order_wins(self):
		# SO delivery date pushed out to 2026-11-01: a row date of 2026-10-01 is now fine,
		# and checking the row's stale delivery_date alone would have falsely blocked it.
		rows = [
			_Doc(
				idx=1,
				manufacturing_end_date="2026-10-01",
				sales_order="SO-1",
				delivery_date="2026-09-15",
			)
		]
		throw = self._run(
			rows,
			sales_orders=[_Doc(name="SO-1", custom_updated_delivery_date="2026-11-01")],
		)
		throw.assert_not_called()

	def test_row_without_delivery_date_is_skipped(self):
		rows = [
			_Doc(
				idx=1,
				manufacturing_end_date="2026-09-01",
				sales_order=None,
				delivery_date=None,
			)
		]
		throw = self._run(rows)
		throw.assert_not_called()


class TestMakeManufacturingOrderCarriesDate(IntegrationTestCase):
	"""The plan row's date must actually reach the PMO -- it was silently dropped before."""

	@classmethod
	def setUpClass(cls):
		pass

	def _row(self, **overrides):
		row = _Doc(
			sales_order="SO-1",
			mwo=None,
			custom_tracking_bom=None,
			docname="SOI-1",
			item_code="NE00468-001",
			customer_sample=None,
			customer_voucher_no=None,
			customer_gold="No",
			customer_diamond="No",
			customer_stone="No",
			customer_good="No",
			customer_weight=0,
			repair_type=None,
			product_type=None,
			qty_per_manufacturing_order=1,
			name="MPT-ROW-1",
			manufacturing_end_date="2026-09-01",
		)
		for key, value in overrides.items():
			setattr(row, key, value)
		return row

	def test_sales_order_branch_sets_date(self):
		source_doc = _Doc(
			company="Test_Company",
			select_manufacture_order="Manufacturing",
			name="MP-1",
		)
		created = _NewDoc()
		with patch.object(pmo_mod.frappe, "new_doc", return_value=created):
			pmo_mod.make_manufacturing_order(
				source_doc, self._row(), master_bom="BOM-1"
			)
		self.assertEqual(created.manufacturing_end_date, "2026-09-01")
		self.assertTrue(created.inserted)

	def test_finding_mwo_branch_sets_date(self):
		source_doc = _Doc(
			company="Test_Company",
			select_manufacture_order="Manufacturing",
			name="MP-1",
		)
		created = _NewDoc()
		row = self._row(sales_order=None, mwo="MWO-1")
		# Both mp_context keys are populated so the branch never reaches its
		# frappe.defaults / frappe.db.get_value fallbacks.
		mp_context = {"manufacturer": "Shubh", "finding_default_department": "DEPT-1"}
		with patch.object(pmo_mod.frappe, "new_doc", return_value=created):
			pmo_mod.make_manufacturing_order(
				source_doc, row, so_det={"master_bom": "BOM-1"}, mp_context=mp_context
			)
		self.assertEqual(created.manufacturing_end_date, "2026-09-01")
		self.assertTrue(created.inserted)

	def test_empty_row_date_leaves_pmo_blank(self):
		source_doc = _Doc(
			company="Test_Company",
			select_manufacture_order="Manufacturing",
			name="MP-1",
		)
		created = _NewDoc()
		with patch.object(pmo_mod.frappe, "new_doc", return_value=created):
			pmo_mod.make_manufacturing_order(
				source_doc, self._row(manufacturing_end_date=None), master_bom="BOM-1"
			)
		self.assertIsNone(created.manufacturing_end_date)


class TestUpdatedManufacturingEndDateIsEffective(IntegrationTestCase):
	"""updated_manufacturing_end_date mirrors custom_updated_delivery_date: when set it wins."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_due_days_prefers_updated_date(self):
		fake = _Doc(
			delivery_date="2026-09-20",
			posting_date="2026-09-01",
			custom_updated_delivery_date=None,
			manufacturing_end_date="2026-09-11",
			updated_manufacturing_end_date="2026-09-06",
		)
		pmo_mod.update_due_days(fake)
		self.assertEqual(fake.manufacturing_end_due_days, 5)

	def test_due_days_falls_back_to_original_date(self):
		fake = _Doc(
			delivery_date="2026-09-20",
			posting_date="2026-09-01",
			custom_updated_delivery_date=None,
			manufacturing_end_date="2026-09-11",
			updated_manufacturing_end_date=None,
		)
		pmo_mod.update_due_days(fake)
		self.assertEqual(fake.manufacturing_end_due_days, 10)

	def test_validate_throws_on_updated_date_past_delivery_date(self):
		# The original date is fine; only the revision is bad. Before this change the
		# revision was invisible to the guard.
		fake = _Doc(
			delivery_date="2026-09-20",
			posting_date="2026-09-01",
			custom_updated_delivery_date=None,
			manufacturing_end_date="2026-09-11",
			updated_manufacturing_end_date="2026-09-25",
		)
		with patch.object(pmo_mod.frappe, "throw", side_effect=RuntimeError) as throw:
			with self.assertRaises(RuntimeError):
				pmo_mod.validate_mfg_date(fake)
		throw.assert_called_once()

	def test_validate_passes_when_updated_date_rescues_a_bad_original(self):
		fake = _Doc(
			delivery_date="2026-09-20",
			posting_date="2026-09-01",
			custom_updated_delivery_date=None,
			manufacturing_end_date="2026-09-25",
			updated_manufacturing_end_date="2026-09-11",
		)
		with patch.object(pmo_mod.frappe, "throw", side_effect=RuntimeError) as throw:
			pmo_mod.validate_mfg_date(fake)
		throw.assert_not_called()

	def test_missing_delivery_date_does_not_crash(self):
		# Finding Manufacturing PMOs are created without a sales_order, so delivery_date
		# stays empty -- and they now arrive from the plan carrying a
		# manufacturing_end_date. The bare `mfg_end_date >= date` compare raised
		# TypeError on their first save after insert (on_submit -> save()).
		fake = _Doc(
			delivery_date=None,
			posting_date="2026-09-01",
			custom_updated_delivery_date=None,
			manufacturing_end_date="2026-09-11",
			updated_manufacturing_end_date=None,
		)
		with patch.object(pmo_mod.frappe, "throw", side_effect=RuntimeError) as throw:
			pmo_mod.validate_mfg_date(fake)
		throw.assert_not_called()

	def test_date_object_and_string_compare_cleanly(self):
		# A DB-loaded delivery_date is a datetime.date while a browser-supplied
		# manufacturing_end_date is a str; getdate() on both sides keeps that mixed pair
		# from raising TypeError.
		fake = _Doc(
			delivery_date=date(2026, 9, 20),
			posting_date="2026-09-01",
			custom_updated_delivery_date=None,
			manufacturing_end_date="2026-09-25",
			updated_manufacturing_end_date=None,
		)
		with patch.object(pmo_mod.frappe, "throw", side_effect=RuntimeError) as throw:
			with self.assertRaises(RuntimeError):
				pmo_mod.validate_mfg_date(fake)
		throw.assert_called_once()

	def test_both_dates_empty_does_not_throw(self):
		fake = _Doc(
			delivery_date="2026-09-20",
			posting_date="2026-09-01",
			custom_updated_delivery_date=None,
			manufacturing_end_date=None,
			updated_manufacturing_end_date=None,
		)
		with patch.object(pmo_mod.frappe, "throw", side_effect=RuntimeError) as throw:
			pmo_mod.validate_mfg_date(fake)
		throw.assert_not_called()
