"""
Pure-logic unit tests for the FG MWO photoshop-image guard.

The rule: on a Manufacturing Work Order with ``for_fg = 1`` whose Finished Item
is flagged ``custom_is_photoshop_images``, the Finish **Front View** and **Left
View** images are mandatory on the Item, and (mirrored from it) on the Master
BOM.  The Item is the master - BOM images are never read back into the Item.

``validate_photoshop_images`` short-circuits on ``frappe.flags.in_test``, so
every test that exercises it patches ``frappe.flags`` with an explicit
``in_test=False`` (same technique as test_make_receive_entry_mop_cap.py).

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_mwo_photoshop_images
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	BOM_IMAGE_FIELDS,
	ITEM_IMAGE_FIELDS,
	REQUIRED_BOM_IMAGE_FIELDS,
	REQUIRED_ITEM_IMAGE_FIELDS,
	ManufacturingWorkOrder,
	_get_empty_bom_image_fields,
	_get_empty_item_image_fields,
	_get_missing_photoshop_images,
	get_missing_photoshop_images,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"

FRONT = "finish_front_view"
LEFT = "finish_left_view"
BOM_FRONT = "front_view_finish"
BOM_LEFT = "left_view_finish"

# frappe.flags replacement that lets the guard run instead of short-circuiting.
NOT_IN_TEST = frappe._dict(in_test=False)


def _mwo(item_code="ITEM-1", master_bom="BOM-1", for_fg=1):
	return SimpleNamespace(item_code=item_code, master_bom=master_bom, for_fg=for_fg)


def _fake_get_value(item_row=None, bom_row=None, is_photoshop=1):
	"""Stand in for frappe.db.get_value across the three call shapes the guard
	uses: the Item flag (scalar), the Item image fields and the BOM image fields
	(both list-of-fields + as_dict=True)."""
	item_row = item_row if item_row is not None else {}
	bom_row = bom_row if bom_row is not None else {}

	def _inner(doctype, name, fieldname, *args, **kwargs):
		if doctype == "Item":
			if fieldname == "custom_is_photoshop_images":
				return is_photoshop
			return {f: item_row.get(f) for f in fieldname}
		if doctype == "BOM":
			return {f: bom_row.get(f) for f in fieldname}
		return None

	return _inner


class TestPhotoshopImageHelpers(IntegrationTestCase):
	"""The gap helpers carry no in_test guard, so they run normally."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_required_pair_is_front_then_left(self):
		self.assertEqual(REQUIRED_ITEM_IMAGE_FIELDS, (FRONT, LEFT))
		self.assertEqual(REQUIRED_BOM_IMAGE_FIELDS, (BOM_FRONT, BOM_LEFT))

	@patch(f"{MOD}.frappe.db.get_value")
	def test_item_gaps_default_to_the_mandatory_pair(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(item_row={FRONT: "/f.png"})
		self.assertEqual(_get_empty_item_image_fields("ITEM-1"), [LEFT])

	@patch(f"{MOD}.frappe.db.get_value")
	def test_item_gaps_preserve_front_then_left_order(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(item_row={})
		self.assertEqual(_get_empty_item_image_fields("ITEM-1"), [FRONT, LEFT])

	@patch(f"{MOD}.frappe.db.get_value")
	def test_item_gaps_accept_an_explicit_field_list(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(item_row={FRONT: "/f.png"})
		gaps = _get_empty_item_image_fields("ITEM-1", list(ITEM_IMAGE_FIELDS))
		self.assertNotIn(FRONT, gaps)
		self.assertIn(LEFT, gaps)
		self.assertIn("finish__back_view", gaps)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_missing_item_or_bom_reports_everything_missing(self, mock_get_value):
		# frappe.db.get_value returns None for a non-existent record.
		mock_get_value.return_value = None
		self.assertEqual(_get_empty_item_image_fields("NOPE"), [FRONT, LEFT])
		self.assertEqual(_get_empty_bom_image_fields("NOPE"), [BOM_FRONT, BOM_LEFT])

	@patch(f"{MOD}.frappe.db.get_value")
	def test_bom_gaps_default_to_the_mandatory_pair(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(bom_row={BOM_LEFT: "/l.png"})
		self.assertEqual(_get_empty_bom_image_fields("BOM-1"), [BOM_FRONT])


class TestGetMissingPhotoshopImages(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MOD}.frappe.db.get_value")
	def test_reports_fieldnames_not_labels(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(item_row={FRONT: "/f.png"})
		self.assertEqual(
			_get_missing_photoshop_images("ITEM-1", "BOM-1"), {"item": [LEFT]}
		)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_clean_item_with_bom_linked_blocks_nothing(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(
			item_row={FRONT: "/f.png", LEFT: "/l.png"}
		)
		self.assertEqual(_get_missing_photoshop_images("ITEM-1", "BOM-1"), {})

	@patch(f"{MOD}.frappe.db.get_value")
	def test_unset_master_bom_is_reported_as_a_bom_gap(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(
			item_row={FRONT: "/f.png", LEFT: "/l.png"}
		)
		self.assertEqual(
			_get_missing_photoshop_images("ITEM-1", None),
			{"bom": [BOM_FRONT, BOM_LEFT]},
		)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_payload_skips_unflagged_items(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(is_photoshop=0)
		self.assertEqual(
			get_missing_photoshop_images("ITEM-1", "BOM-1"), {"check_required": False}
		)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_payload_separates_blocking_gaps_from_optional_slots(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(item_row={FRONT: "/f.png"})
		payload = get_missing_photoshop_images("ITEM-1", "BOM-1")

		self.assertTrue(payload["check_required"])
		self.assertEqual(payload["missing"], {"item": [LEFT]})
		# The four non-mandatory views are offered but never block.
		self.assertNotIn(FRONT, payload["optional_item"])
		self.assertNotIn(LEFT, payload["optional_item"])
		self.assertEqual(
			sorted(payload["optional_item"]),
			sorted(
				[
					"finish__back_view",
					"finish_bottom_view",
					"finish_right_view",
					"finish_top_view",
				]
			),
		)
		self.assertEqual(payload["required_item_fields"], [FRONT, LEFT])
		self.assertEqual(payload["item_image_fields"], ITEM_IMAGE_FIELDS)
		self.assertEqual(payload["bom_image_fields"], BOM_IMAGE_FIELDS)


@patch(f"{MOD}.frappe.flags", NOT_IN_TEST)
class TestValidatePhotoshopImages(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MOD}._sync_item_images_to_bom")
	@patch(f"{MOD}.frappe.db.get_value")
	def test_no_op_when_item_is_not_flagged(self, mock_get_value, mock_sync):
		mock_get_value.side_effect = _fake_get_value(is_photoshop=0)
		ManufacturingWorkOrder.validate_photoshop_images(_mwo())
		mock_sync.assert_not_called()

	@patch(f"{MOD}._sync_item_images_to_bom")
	@patch(f"{MOD}.frappe.db.get_value")
	def test_no_op_on_non_fg_work_orders(self, mock_get_value, mock_sync):
		mock_get_value.side_effect = _fake_get_value(item_row={})
		ManufacturingWorkOrder.validate_photoshop_images(_mwo(for_fg=0))
		mock_get_value.assert_not_called()
		mock_sync.assert_not_called()

	@patch(f"{MOD}._sync_item_images_to_bom")
	@patch(f"{MOD}.frappe.db.get_value")
	def test_throws_naming_both_views_when_item_is_empty(
		self, mock_get_value, mock_sync
	):
		mock_get_value.side_effect = _fake_get_value(item_row={})
		with self.assertRaises(frappe.ValidationError) as ctx:
			ManufacturingWorkOrder.validate_photoshop_images(_mwo())
		message = str(ctx.exception)
		self.assertIn(ITEM_IMAGE_FIELDS[FRONT], message)
		self.assertIn(ITEM_IMAGE_FIELDS[LEFT], message)
		# Never mirror a half-empty Item onto the BOM.
		mock_sync.assert_not_called()

	@patch(f"{MOD}._sync_item_images_to_bom")
	@patch(f"{MOD}.frappe.db.get_value")
	def test_throws_naming_left_view_only(self, mock_get_value, mock_sync):
		mock_get_value.side_effect = _fake_get_value(item_row={FRONT: "/f.png"})
		with self.assertRaises(frappe.ValidationError) as ctx:
			ManufacturingWorkOrder.validate_photoshop_images(_mwo())
		message = str(ctx.exception)
		self.assertIn(ITEM_IMAGE_FIELDS[LEFT], message)
		self.assertNotIn(ITEM_IMAGE_FIELDS[FRONT], message)
		mock_sync.assert_not_called()

	@patch(f"{MOD}._sync_item_images_to_bom")
	@patch(f"{MOD}.frappe.db.get_value")
	def test_throws_naming_front_view_only(self, mock_get_value, mock_sync):
		mock_get_value.side_effect = _fake_get_value(item_row={LEFT: "/l.png"})
		with self.assertRaises(frappe.ValidationError) as ctx:
			ManufacturingWorkOrder.validate_photoshop_images(_mwo())
		message = str(ctx.exception)
		self.assertIn(ITEM_IMAGE_FIELDS[FRONT], message)
		self.assertNotIn(ITEM_IMAGE_FIELDS[LEFT], message)
		mock_sync.assert_not_called()

	@patch(f"{MOD}._sync_item_images_to_bom")
	@patch(f"{MOD}.frappe.db.get_value")
	def test_passes_without_mirroring_when_bom_already_has_the_pair(
		self, mock_get_value, mock_sync
	):
		mock_get_value.side_effect = _fake_get_value(
			item_row={FRONT: "/f.png", LEFT: "/l.png"},
			bom_row={BOM_FRONT: "/f.png", BOM_LEFT: "/l.png"},
		)
		ManufacturingWorkOrder.validate_photoshop_images(_mwo())
		mock_sync.assert_not_called()

	@patch(f"{MOD}._sync_item_images_to_bom")
	@patch(f"{MOD}.frappe.db.get_value")
	def test_mirrors_to_bom_then_passes(self, mock_get_value, mock_sync):
		item_row = {FRONT: "/f.png", LEFT: "/l.png"}
		bom_row = {}
		mock_get_value.side_effect = _fake_get_value(item_row=item_row, bom_row=bom_row)

		# The mirror lands: the re-read then sees the pair on the BOM.
		def _sync(item_code, master_bom):
			bom_row[BOM_FRONT] = item_row[FRONT]
			bom_row[BOM_LEFT] = item_row[LEFT]

		mock_sync.side_effect = _sync

		ManufacturingWorkOrder.validate_photoshop_images(_mwo())
		mock_sync.assert_called_once_with("ITEM-1", "BOM-1")

	@patch(f"{MOD}._sync_item_images_to_bom")
	@patch(f"{MOD}.frappe.db.get_value")
	def test_throws_when_the_mirror_does_not_land(self, mock_get_value, mock_sync):
		# Mirror is a no-op (e.g. the BOM custom field is absent on this site).
		mock_get_value.side_effect = _fake_get_value(
			item_row={FRONT: "/f.png", LEFT: "/l.png"}, bom_row={}
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			ManufacturingWorkOrder.validate_photoshop_images(_mwo())
		message = str(ctx.exception)
		self.assertIn("BOM-1", message)
		self.assertIn(BOM_IMAGE_FIELDS[BOM_FRONT], message)
		self.assertIn(BOM_IMAGE_FIELDS[BOM_LEFT], message)
		mock_sync.assert_called_once_with("ITEM-1", "BOM-1")

	@patch(f"{MOD}._sync_item_images_to_bom")
	@patch(f"{MOD}.frappe.db.get_value")
	def test_throws_without_mirroring_when_master_bom_is_unset(
		self, mock_get_value, mock_sync
	):
		mock_get_value.side_effect = _fake_get_value(
			item_row={FRONT: "/f.png", LEFT: "/l.png"}
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			ManufacturingWorkOrder.validate_photoshop_images(_mwo(master_bom=None))
		message = str(ctx.exception)
		self.assertIn(BOM_IMAGE_FIELDS[BOM_FRONT], message)
		self.assertIn(BOM_IMAGE_FIELDS[BOM_LEFT], message)
		# Nothing to mirror onto.
		mock_sync.assert_not_called()
