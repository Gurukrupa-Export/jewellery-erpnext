# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit tests for Customer Goods ownership on Process Loss produce rows.

The bug: the tree and warehouse loss builders consumed a Customer Goods batch and left the ML
produce row bare, so the blanket "default blank to Regular Stock" in doc_events/stock_entry.py
booked a customer's metal as company scrap and the batch minted off that row inherited it
(GE-SE-PS-26-02910: 0.1g of MHCU0012's gold).

Mocked/pure-logic style (see test_sample_goods_guard.py): SimpleNamespace fake docs, no DB.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.utils import row_ownership as ro
from jewellery_erpnext.jewellery_erpnext.doc_events import warehouse_stock_entry as wse
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events import (
	tree_stock_entry as tse,
)

_RO = "jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership"


class _FakeSE:
	"""Minimal stand-in for a Stock Entry: rows are plain dicts appended to `items`."""

	def __init__(self, stock_entry_type="Process Loss", items=None):
		self.stock_entry_type = stock_entry_type
		self.items = list(items or [])

	def append(self, _table, row):
		self.items.append(row)
		return row

	def set(self, _table, rows):
		self.items = list(rows)

	def get(self, field):
		return getattr(self, field, None)


def _row(**fields):
	base = {
		"idx": 1,
		"item_code": "M-G-18KT-75.4-Y",
		"s_warehouse": None,
		"t_warehouse": None,
		"batch_no": None,
		"inventory_type": None,
		"customer": None,
		"qty": 1.0,
	}
	base.update(fields)
	return base


class TestNormalizeOwnership(IntegrationTestCase):
	"""The three rules every loss builder must share."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_customer_goods_with_customer_passes_through(self):
		self.assertEqual(
			ro.normalize_ownership("Customer Goods", "MHCU0012"),
			("Customer Goods", "MHCU0012"),
		)

	def test_customer_goods_without_customer_downgrades(self):
		# The live landmine: gk holds 28 batches that are Customer Goods with a NULL customer.
		# Emitting a customer type with no customer defeats is_process_loss_repack and hard-fails
		# the submit with "This item is not allowed as Customer Goods".
		with patch(f"{_RO}.frappe", MagicMock()):
			self.assertEqual(
				ro.normalize_ownership("Customer Goods", None),
				("Regular Stock", None),
			)

	def test_regular_stock_drops_stray_customer(self):
		self.assertEqual(
			ro.normalize_ownership("Regular Stock", "MHCU0012"), ("Regular Stock", None)
		)

	def test_blank_defaults_to_regular(self):
		self.assertEqual(ro.normalize_ownership(None, None), ("Regular Stock", None))


class TestTreeLossPairOwnership(IntegrationTestCase):
	"""tree_stock_entry._append_repack_loss_pair — the reported defect."""

	@classmethod
	def setUpClass(cls):
		pass

	def _pair(self, inventory_type, customer):
		se = _FakeSE()
		tse._append_repack_loss_pair(
			se,
			"M-G-18KT-75.4-Y",
			"ML-G-18KT-75.4-Y",
			0.1,
			"Casting MSL WH 2 - GEPL",
			"Waxing Scrap - GEPL",
			batch_no="MHCU0012-2F06-M-G-18KT-75.4-Y-01-A",
			inventory_type=inventory_type,
			customer=customer,
		)
		consume, produce = se.items
		return consume, produce

	def test_produce_row_inherits_customer_goods(self):
		# The regression: this row used to come back with no inventory_type at all.
		consume, produce = self._pair("Customer Goods", "MHCU0012")
		self.assertEqual(produce["inventory_type"], "Customer Goods")
		self.assertEqual(produce["customer"], "MHCU0012")
		self.assertEqual(consume["inventory_type"], "Customer Goods")
		self.assertEqual(consume["customer"], "MHCU0012")

	def test_produce_row_keeps_no_batch_so_it_mints_on_submit(self):
		consume, produce = self._pair("Customer Goods", "MHCU0012")
		self.assertEqual(consume["batch_no"], "MHCU0012-2F06-M-G-18KT-75.4-Y-01-A")
		self.assertIsNone(produce.get("batch_no"))
		self.assertEqual(produce["t_warehouse"], "Waxing Scrap - GEPL")
		self.assertIsNone(produce["s_warehouse"])

	def test_regular_source_does_not_gain_a_customer(self):
		consume, produce = self._pair("Regular Stock", None)
		self.assertEqual(produce["inventory_type"], "Regular Stock")
		self.assertIsNone(produce.get("customer"))

	def test_customer_goods_without_customer_downgrades_not_throws(self):
		# KACU0043-2F05-M-G-18KT-75.4-Y-A-A and friends: must degrade cleanly.
		with patch(f"{_RO}.frappe", MagicMock()):
			consume, produce = self._pair("Customer Goods", None)
		self.assertEqual(produce["inventory_type"], "Regular Stock")
		self.assertIsNone(produce.get("customer"))
		self.assertEqual(consume["inventory_type"], "Regular Stock")


class TestWarehouseLossProduceStamping(IntegrationTestCase):
	"""warehouse_stock_entry._stamp_loss_produce_rows — ownership arrives only after FIFO."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_single_owner_stamps_produce_in_place(self):
		se = _FakeSE()
		se.items = [
			_DictRow(
				_row(
					s_warehouse="Assembly MSL WH 2 - GEPL",
					batch_no="B1",
					inventory_type="Customer Goods",
					customer="TNCU0022",
					qty=0.64,
				)
			),
			_DictRow(
				_row(
					idx=2,
					item_code="ML-G-18KT-75.4-Y",
					t_warehouse="Waxing Scrap - GEPL",
					qty=0.64,
				)
			),
		]
		with patch(f"{_RO}.frappe", MagicMock()):
			wse._stamp_loss_produce_rows(se)
		produce = se.items[1]
		self.assertEqual(produce.inventory_type, "Customer Goods")
		self.assertEqual(produce.customer, "TNCU0022")
		self.assertEqual(
			len(se.items), 2, "single owner must not split the produce row"
		)

	def test_multi_owner_splits_produce_pro_rata(self):
		se = _FakeSE()
		se.items = [
			_DictRow(
				_row(
					s_warehouse="Assembly MSL WH 2 - GEPL",
					batch_no="B1",
					inventory_type="Customer Goods",
					customer="TNCU0022",
					qty=0.75,
				)
			),
			_DictRow(
				_row(
					s_warehouse="Assembly MSL WH 2 - GEPL",
					batch_no="B2",
					inventory_type="Customer Goods",
					customer="KACU0043",
					qty=0.25,
				)
			),
			_DictRow(
				_row(
					idx=3,
					item_code="ML-G-18KT-75.4-Y",
					t_warehouse="Waxing Scrap - GEPL",
					qty=1.0,
				)
			),
		]
		with patch(f"{_RO}.frappe", MagicMock()):
			with patch.object(wse, "_se_precision", return_value=3):
				wse._stamp_loss_produce_rows(se)

		produces = [r for r in se.items if r.get("t_warehouse")]
		self.assertEqual(len(produces), 2, "one produce row per distinct owner")
		owners = {
			(p.get("inventory_type"), p.get("customer")): p.get("qty") for p in produces
		}
		self.assertEqual(owners[("Customer Goods", "TNCU0022")], 0.75)
		self.assertEqual(owners[("Customer Goods", "KACU0043")], 0.25)
		# The pair must stay balanced.
		self.assertAlmostEqual(sum(p.get("qty") for p in produces), 1.0, places=3)

	def test_plain_dict_rows_are_stamped(self):
		# Rows reach this helper as plain dicts before the SE is inserted (the child Documents
		# only exist after append/insert). Setting attributes on a dict raises AttributeError,
		# so the write must be type-agnostic.
		se = _FakeSE()
		se.items = [
			_row(
				s_warehouse="Assembly MSL WH 2 - GEPL",
				batch_no="B1",
				inventory_type="Customer Goods",
				customer="TNCU0022",
				qty=0.1,
			),
			_row(
				idx=2,
				item_code="ML-G-18KT-75.4-Y",
				t_warehouse="Waxing Scrap - GEPL",
				qty=0.1,
			),
		]
		with patch(f"{_RO}.frappe", MagicMock()):
			wse._stamp_loss_produce_rows(se)
		self.assertEqual(se.items[1]["inventory_type"], "Customer Goods")
		self.assertEqual(se.items[1]["customer"], "TNCU0022")

	def test_regular_source_leaves_produce_regular(self):
		se = _FakeSE()
		se.items = [
			_DictRow(
				_row(
					s_warehouse="Assembly MSL WH 2 - GEPL",
					batch_no="B1",
					inventory_type="Regular Stock",
					qty=0.5,
				)
			),
			_DictRow(
				_row(
					idx=2,
					item_code="ML-G-18KT-75.4-Y",
					t_warehouse="Waxing Scrap - GEPL",
					qty=0.5,
				)
			),
		]
		with patch(f"{_RO}.frappe", MagicMock()):
			wse._stamp_loss_produce_rows(se)
		self.assertEqual(se.items[1].inventory_type, "Regular Stock")
		self.assertIsNone(se.items[1].customer)


class TestLossOwnershipGuard(IntegrationTestCase):
	"""The loud guard: a blank produce row on a customer-owned Process Loss is a builder bug."""

	@classmethod
	def setUpClass(cls):
		pass

	def _se(self, se_type="Process Loss", produce_inventory_type=None):
		return _FakeSE(
			stock_entry_type=se_type,
			items=[
				_DictRow(
					_row(
						s_warehouse="Casting MSL WH 2 - GEPL",
						batch_no="MHCU0012-2F06-M-G-18KT-75.4-Y-01-A",
						inventory_type="Customer Goods",
						customer="MHCU0012",
					)
				),
				_DictRow(
					_row(
						idx=2,
						item_code="ML-G-18KT-75.4-Y",
						t_warehouse="Waxing Scrap - GEPL",
						inventory_type=produce_inventory_type,
					)
				),
			],
		)

	def test_throws_when_produce_row_never_stamped(self):
		se = self._se(produce_inventory_type=None)
		with patch(f"{_RO}.frappe") as mock_frappe:
			mock_frappe.throw.side_effect = Exception("thrown")
			with self.assertRaises(Exception):
				ro.validate_loss_ownership_carried(se)
			self.assertTrue(mock_frappe.throw.called)

	def test_passes_when_ownership_carried(self):
		se = self._se(produce_inventory_type="Customer Goods")
		with patch(f"{_RO}.frappe") as mock_frappe:
			ro.validate_loss_ownership_carried(se)
			self.assertFalse(mock_frappe.throw.called)

	def test_deliberate_regular_stock_produce_is_allowed(self):
		# melting_loss books the ML row "Regular Stock" by policy. An explicit stamp is a
		# decision, not the bug -- only a blank row is the bug.
		se = self._se(produce_inventory_type="Regular Stock")
		with patch(f"{_RO}.frappe") as mock_frappe:
			ro.validate_loss_ownership_carried(se)
			self.assertFalse(mock_frappe.throw.called)

	def test_ignores_non_process_loss_entries(self):
		se = self._se(se_type="Manufacture", produce_inventory_type=None)
		with patch(f"{_RO}.frappe") as mock_frappe:
			ro.validate_loss_ownership_carried(se)
			self.assertFalse(mock_frappe.throw.called)

	def test_ignores_regular_stock_consumption(self):
		se = _FakeSE(
			items=[
				_DictRow(
					_row(
						s_warehouse="Casting MSL WH 2 - GEPL",
						batch_no="B1",
						inventory_type="Regular Stock",
					)
				),
				_DictRow(
					_row(
						idx=2,
						item_code="ML-G-18KT-75.4-Y",
						t_warehouse="Waxing Scrap - GEPL",
					)
				),
			]
		)
		with patch(f"{_RO}.frappe") as mock_frappe:
			ro.validate_loss_ownership_carried(se)
			self.assertFalse(mock_frappe.throw.called)


class _DictRow:
	"""A row that supports both ``row.get(f)`` and ``row.field = x``, like a Frappe child doc."""

	def __init__(self, data):
		self.__dict__.update(data)

	def get(self, field, default=None):
		return self.__dict__.get(field, default)

	def as_dict(self):
		return dict(self.__dict__)

	def items(self):
		return self.__dict__.items()


class TestOwnershipGuardCoversRepack(IntegrationTestCase):
	"""``validate_loss_ownership_carried`` guards Repack as well as Process Loss.

	A purity Repack mints a new batch out of consumed metal exactly as a loss
	write-off does. The EIR gain injection's Repack leg used to leave its produce
	row bare, so a customer's pure metal was silently repacked into company alloy.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@staticmethod
	def _se(se_type, produce_inventory_type=None):
		return frappe._dict(
			stock_entry_type=se_type,
			items=[
				frappe._dict(
					idx=1,
					item_code="M-G-24KT",
					batch_no="B-CG",
					s_warehouse="MSL",
					t_warehouse=None,
					inventory_type="Customer Goods",
					customer="CUST-1",
				),
				frappe._dict(
					idx=2,
					item_code="M-G-18KT",
					s_warehouse=None,
					t_warehouse="DEPT",
					inventory_type=produce_inventory_type,
				),
			],
		)

	def test_repack_with_bare_produce_row_throws(self):
		with self.assertRaises(frappe.ValidationError):
			ro.validate_loss_ownership_carried(self._se("Repack"))

	def test_process_loss_with_bare_produce_row_still_throws(self):
		with self.assertRaises(frappe.ValidationError):
			ro.validate_loss_ownership_carried(self._se("Process Loss"))

	def test_repack_carrying_ownership_passes(self):
		self.assertIsNone(
			ro.validate_loss_ownership_carried(
				self._se("Repack", produce_inventory_type="Customer Goods")
			)
		)

	def test_deliberate_company_write_off_passes(self):
		# melting_loss books the produced row to the company by policy. A non-blank
		# value is exactly what distinguishes that from a builder that forgot.
		self.assertIsNone(
			ro.validate_loss_ownership_carried(
				self._se("Repack", produce_inventory_type="Regular Stock")
			)
		)

	def test_unrelated_stock_entry_types_are_ignored(self):
		for se_type in (
			"Material Transfer",
			"Material Transfer (WORK ORDER)",
			"Material Receipt",
		):
			self.assertIsNone(ro.validate_loss_ownership_carried(self._se(se_type)))
