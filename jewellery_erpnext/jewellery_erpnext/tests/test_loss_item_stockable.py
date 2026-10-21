# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the guard that keeps a Process Loss target item usable.

A loss variant must be able to RECEIVE stock; its template must not. The app
seeds the loss templates non-stock on purpose (``ML`` / ``FL`` carry
``is_stock_item = 0, has_variants = 1``), and ERPNext's ``create_variant`` copies
``is_stock_item`` down from template to variant whenever that field appears in
Item Variant Settings -- which it does by default. So a loss variant is born
non-stock unless something intervenes.

Two things then make it worse:

  * The app's own ``is_stock_item = 1`` safety net in ``doc_events/item.py`` never
    fires for loss variants. ``item_group`` is ``reqd``, so the variant inherits
    the TEMPLATE's group (``Diamond - T``), and that hook only matches ``* - V``.
  * ``Item.on_update`` -> ``update_variants`` re-pushes the template's flags onto
    every existing variant, in a background job once a template has more than 30
    of them. A loss item corrected weeks ago silently reverts the next time
    anyone saves its template.

The symptom is ERPNext's ``validate_item`` rejecting the produce row of the
Process Loss Stock Entry with a bare "<item> is not a stock Item", naming an item
the operator has never heard of.

DB-free per the suite convention -- ``setUpClass`` is neutralised and every
``frappe`` lookup is patched. ``frappe.db.get_value`` is patched only within this
module's namespace, never globally (that would hijack DocType meta loading).
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	ensure_loss_item_stockable,
)

MS = "jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip"

LOSS_ITEM = "DB-NT-RO-6B-+6-6.5"


class _Logger:
	"""Captures the repair log line; frappe.translate also calls .error()."""

	def __init__(self):
		self.messages = []

	def info(self, msg):
		self.messages.append(msg)

	def error(self, *args, **kwargs):
		pass


class TestEnsureLossItemStockable(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, current, item_code=LOSS_ITEM):
		"""Returns (result, list_of_set_value_calls, logger)."""
		writes = []
		logger = _Logger()
		patches = [
			patch(f"{MS}.frappe.db.get_value", return_value=current),
			patch(
				f"{MS}.frappe.db.set_value",
				side_effect=lambda *a, **k: writes.append((a, k)),
			),
			patch(f"{MS}.frappe.logger", return_value=logger),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		return ensure_loss_item_stockable(item_code), writes, logger

	@staticmethod
	def _item(is_stock_item=1, has_variants=0, disabled=0):
		return frappe._dict(
			{
				"is_stock_item": is_stock_item,
				"has_variants": has_variants,
				"disabled": disabled,
			}
		)

	def test_already_correct_item_is_not_written(self):
		"""The normal path costs one read and nothing else."""
		result, writes, logger = self._run(self._item())

		self.assertEqual(result, LOSS_ITEM)
		self.assertEqual(writes, [])
		self.assertEqual(logger.messages, [])

	def test_non_stock_item_is_repaired(self):
		"""The live failure: the item exists but cannot receive stock."""
		result, writes, logger = self._run(self._item(is_stock_item=0))

		self.assertEqual(result, LOSS_ITEM)
		self.assertEqual(len(writes), 1)
		args, kwargs = writes[0]
		self.assertEqual(args[0], "Item")
		self.assertEqual(args[1], LOSS_ITEM)
		self.assertEqual(args[2], {"is_stock_item": 1})
		self.assertTrue(logger.messages)

	def test_repair_does_not_bump_modified(self):
		"""update_modified=False keeps the repair out of the template's push.

		It also avoids doc.save(), whose Item.validate -> cant_change() throws
		outright when is_stock_item changes on an item that already has linked
		submitted documents -- turning a repairable state into a hard failure
		in the middle of a submit.
		"""
		_result, writes, _logger = self._run(self._item(is_stock_item=0))

		self.assertIs(writes[0][1].get("update_modified"), False)

	def test_template_flag_is_cleared_too(self):
		"""A loss item that somehow carries has_variants cannot hold stock."""
		_result, writes, _logger = self._run(
			self._item(is_stock_item=0, has_variants=1)
		)

		self.assertEqual(writes[0][0][2], {"is_stock_item": 1, "has_variants": 0})

	def test_only_the_wrong_flags_are_written(self):
		"""A correct is_stock_item is not rewritten just because has_variants is."""
		_result, writes, _logger = self._run(self._item(has_variants=1))

		self.assertEqual(writes[0][0][2], {"has_variants": 0})

	def test_disabled_item_throws(self):
		"""Silently stocking a disabled item would hide a real configuration error."""
		with self.assertRaises(ValidationError) as ctx:
			self._run(self._item(disabled=1))

		message = str(ctx.exception)
		self.assertIn(LOSS_ITEM, message)
		self.assertIn("disabled", message)

	def test_missing_item_is_left_to_the_caller(self):
		"""_resolve_loss_item's own guard and link validation say it better."""
		result, writes, _logger = self._run(None)

		self.assertEqual(result, LOSS_ITEM)
		self.assertEqual(writes, [])

	def test_a_non_mapping_read_is_ignored(self):
		"""This repair must never be the thing that breaks a submit.

		Several existing tests blanket-patch ``frappe.db.get_value`` with a single
		scalar return (``patch("frappe.db.get_value", return_value="Some Value")``
		in test_employee_ir_loss_baseline), so the ``as_dict=True`` read comes back
		as a string. Reaching for ``.disabled`` on that raised AttributeError and
		took the whole Employee IR submit down with it. Anything that is not the
		mapping we asked for means "cannot tell" -- fall through and let
		ERPNext's validate_item report an unusable item.
		"""
		for reading in ("Some Value", 0, [], MagicMock()):
			with self.subTest(reading=type(reading).__name__):
				result, writes, _logger = self._run(reading)
				self.assertEqual(result, LOSS_ITEM)
				self.assertEqual(writes, [])

	def test_blank_item_code_short_circuits(self):
		result, writes, _logger = self._run(self._item(is_stock_item=0), item_code=None)

		self.assertIsNone(result)
		self.assertEqual(writes, [])

	def test_string_flags_from_the_db_layer(self):
		"""cint, not truthiness: "0" is truthy in Python."""
		_result, writes, _logger = self._run(
			self._item(is_stock_item="0", has_variants="0", disabled="0")
		)
		self.assertEqual(writes[0][0][2], {"is_stock_item": 1})

		_result, writes2, _logger2 = self._run(
			self._item(is_stock_item="1", has_variants="0", disabled="0")
		)
		self.assertEqual(writes2, [])


class TestSetItemsFromAttributeStockable(IntegrationTestCase):
	"""The other half: a fresh loss variant must not be INSERTED non-stock."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_signature_defaults_to_template_behaviour(self):
		import inspect

		from jewellery_erpnext.utils import set_items_from_attribute

		params = inspect.signature(set_items_from_attribute).parameters
		self.assertIn("stockable", params)
		self.assertIs(
			params["stockable"].default,
			False,
			"ordinary variants must keep inheriting their template's flags",
		)

	def test_flag_is_applied_before_the_insert(self):
		"""Setting it after variant.save() would still write the row wrong."""
		import inspect

		from jewellery_erpnext.utils import set_items_from_attribute

		source = inspect.getsource(set_items_from_attribute)
		self.assertLess(
			source.index("variant.is_stock_item = 1"),
			source.index("variant.save()"),
			"is_stock_item must be set before the row is inserted",
		)
