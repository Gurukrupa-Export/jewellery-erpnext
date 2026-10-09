# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Unit tests for the Item handling in SketchOrder.make_items and SketchOrder.on_cancel.

make_items runs on submit:

* a Final Sketch Approval CMO row marked "Copy Paste Item" re-uses the design Item the
  designer picked: that Item is re-stamped with this Sketch Order (the order_form_type /
  order_form_id Dynamic Link pair and the two custom sketch-order ids) and no new Item is
  created. A Copy Paste row with no Item is skipped silently;
* every other row ("New Item" or blank) still creates a new design Item;
* a Purchase Sketch Order still creates one Item for the order itself.

on_cancel only sets workflow_state to "Cancelled" on the in-memory document.

DB-free per the suite convention: setUpClass is neutralised, the Item builders are
mocked and frappe.db.set_value / frappe.msgprint are replaced with MagicMocks.

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_sketch_order_items
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.gurukrupa_exports.doctype.sketch_order import (
	sketch_order as so_module,
)

SketchOrder = so_module.SketchOrder
CMO = "Final Sketch Approval CMO"


def _row(name, item_remark=None, item=None):
	return SimpleNamespace(doctype=CMO, name=name, item_remark=item_remark, item=item)


def _sketch_order(rows=(), order_type="Sales", **fields):
	values = {
		"doctype": "Sketch Order",
		"name": "SKO-0001",
		"sketch_order_form": "SOF-0001",
		"order_type": order_type,
		"final_sketch_approval_cmo": list(rows),
		"workflow_state": "Approved",
	}
	values.update(fields)
	return SimpleNamespace(**values)


class TestSketchOrderMakeItems(IntegrationTestCase):
	"""make_items(): Copy Paste rows re-stamp their Item; other rows create one."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		self.set_value = MagicMock()
		self.msgprint = MagicMock()
		self.create_template = MagicMock(
			side_effect=lambda doc, row_name: f"DES-{row_name}"
		)
		self.create_for_po = MagicMock(return_value="DES-PO")
		self.update_template = MagicMock()

		patchers = [
			patch.object(so_module.frappe.db, "set_value", self.set_value),
			patch.object(so_module.frappe, "msgprint", self.msgprint),
			patch.object(so_module, "_", side_effect=lambda msg: msg),
			patch.object(
				so_module,
				"get_link_to_form",
				side_effect=lambda dt, name: f"<{dt}:{name}>",
			),
			patch.object(
				so_module,
				"create_item_template_from_sketch_order",
				self.create_template,
			),
			patch.object(so_module, "create_item_for_po", self.create_for_po),
			patch.object(so_module, "update_item_template", self.update_template),
		]
		for patcher in patchers:
			patcher.start()
			self.addCleanup(patcher.stop)

	def _new_item_calls(self, doc, row):
		template = f"DES-{row.name}"
		return {
			"create": call(doc, row.name),
			"update": call(doc, template),
			"set_value": call(CMO, row.name, "item", template),
			"msgprint": call(f"New Item Created: <Item:{template}>"),
		}

	def test_copy_paste_row_restamps_existing_item(self):
		copy_row = _row("CMO-1", item_remark="Copy Paste Item", item="DES-EXISTING")
		new_row = _row("CMO-2", item_remark="New Item")
		blank_row = _row("CMO-3")
		doc = _sketch_order([copy_row, new_row, blank_row])

		SketchOrder.make_items(doc)

		new_calls = self._new_item_calls(doc, new_row)
		blank_calls = self._new_item_calls(doc, blank_row)
		self.assertEqual(
			self.set_value.call_args_list,
			[
				call(
					"Item",
					"DES-EXISTING",
					{
						"order_form_type": "Sketch Order",
						"order_form_id": "SKO-0001",
						"custom_sketch_order_form_id": "SOF-0001",
						"custom_sketch_order_id": "SKO-0001",
					},
				),
				new_calls["set_value"],
				blank_calls["set_value"],
			],
		)
		self.assertEqual(
			self.create_template.call_args_list,
			[new_calls["create"], blank_calls["create"]],
		)
		self.assertEqual(
			self.update_template.call_args_list,
			[new_calls["update"], blank_calls["update"]],
		)
		self.assertEqual(
			self.msgprint.call_args_list,
			[new_calls["msgprint"], blank_calls["msgprint"]],
		)
		self.create_for_po.assert_not_called()

	def test_copy_paste_restamp_keeps_dynamic_link_pair(self):
		doc = _sketch_order(
			[_row("CMO-1", item_remark="Copy Paste Item", item="DES-EXISTING")],
			name="SKO-0042",
		)

		SketchOrder.make_items(doc)

		self.set_value.assert_called_once()
		doctype, item, values = self.set_value.call_args.args
		self.assertEqual((doctype, item), ("Item", "DES-EXISTING"))
		self.assertEqual(values["order_form_type"], "Sketch Order")
		self.assertEqual(values["order_form_id"], "SKO-0042")
		self.assertEqual(values["custom_sketch_order_id"], "SKO-0042")
		self.create_template.assert_not_called()
		self.update_template.assert_not_called()
		self.msgprint.assert_not_called()

	def test_copy_paste_row_without_item_is_noop(self):
		for blank_item in (None, ""):
			with self.subTest(item=blank_item):
				self.set_value.reset_mock()
				self.create_template.reset_mock()
				self.update_template.reset_mock()
				self.msgprint.reset_mock()
				copy_row = _row("CMO-1", item_remark="Copy Paste Item", item=blank_item)
				new_row = _row("CMO-2", item_remark="New Item")
				doc = _sketch_order([copy_row, new_row])

				SketchOrder.make_items(doc)

				new_calls = self._new_item_calls(doc, new_row)
				self.assertEqual(
					self.set_value.call_args_list, [new_calls["set_value"]]
				)
				self.assertEqual(
					self.create_template.call_args_list, [new_calls["create"]]
				)
				self.assertEqual(
					self.update_template.call_args_list, [new_calls["update"]]
				)
				self.assertEqual(self.msgprint.call_args_list, [new_calls["msgprint"]])

	def test_new_item_rows_create_items_as_before(self):
		rows = [_row("CMO-1", item_remark="New Item"), _row("CMO-2")]
		doc = _sketch_order(rows)

		SketchOrder.make_items(doc)

		expected = [self._new_item_calls(doc, row) for row in rows]
		self.assertEqual(
			self.create_template.call_args_list, [c["create"] for c in expected]
		)
		self.assertEqual(
			self.update_template.call_args_list, [c["update"] for c in expected]
		)
		self.assertEqual(
			self.set_value.call_args_list, [c["set_value"] for c in expected]
		)
		self.assertEqual(
			self.msgprint.call_args_list, [c["msgprint"] for c in expected]
		)
		self.create_for_po.assert_not_called()

	def test_purchase_branch_unchanged(self):
		# CMO rows, including a Copy Paste row, are not visited for a Purchase order.
		doc = _sketch_order(
			[
				_row("CMO-1", item_remark="Copy Paste Item", item="DES-EXISTING"),
				_row("CMO-2", item_remark="New Item"),
			],
			order_type="Purchase",
		)

		SketchOrder.make_items(doc)

		self.create_for_po.assert_called_once_with(doc, "SKO-0001")
		self.update_template.assert_called_once_with(doc, "DES-PO")
		self.assertEqual(
			self.set_value.call_args_list,
			[call("Sketch Order", "SKO-0001", "item_code", "DES-PO")],
		)
		self.msgprint.assert_called_once_with("New Item Created: <Item:DES-PO>")
		self.create_template.assert_not_called()


class TestSketchOrderOnCancel(IntegrationTestCase):
	"""on_cancel(): the workflow state reads "Cancelled" on the in-memory document only."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_on_cancel_sets_cancelled_in_memory_only(self):
		set_value = MagicMock()
		doc = _sketch_order(
			[_row("CMO-1", item_remark="Copy Paste Item", item="DES-EXISTING")]
		)

		with patch.object(so_module.frappe.db, "set_value", set_value):
			SketchOrder.on_cancel(doc)

		self.assertEqual(doc.workflow_state, "Cancelled")
		set_value.assert_not_called()
