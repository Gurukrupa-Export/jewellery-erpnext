"""
Pure-logic unit tests for the FG MWO photoshop-image guard.

The rule: on a Manufacturing Work Order with ``for_fg = 1`` whose Design Code
Item is flagged ``custom_is_photoshop_images``, the Finish **Front View** and
**Left View** images are mandatory **on the work order itself**.  The Item
master and the Master BOM are never read from or written to - each work order is
photographed separately, so mirroring onto the shared Item / BOM would let one
MWO overwrite another's images.  ``Item.custom_is_photoshop_images`` remains the
gate: it says *whether* the design needs images, not *where* they live.

``validate_photoshop_images`` short-circuits on ``frappe.flags.in_test``, so
every test that exercises it patches ``frappe.flags`` with an explicit
``in_test=False`` (same technique as test_make_receive_entry_mop_cap.py).

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_mwo_photoshop_images
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	MWO_IMAGE_FIELDS,
	REQUIRED_MWO_IMAGE_FIELDS,
	ManufacturingWorkOrder,
	_get_empty_mwo_image_fields,
	_photoshop_required_for_item,
	get_missing_photoshop_images,
	update_photoshop_images,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"

FRONT = "finish_front_view"
LEFT = "finish_left_view"

# frappe.flags replacement that lets the guard run instead of short-circuiting.
NOT_IN_TEST = frappe._dict(in_test=False)


class _StubMWO(frappe._dict):
	"""Stands in for a Manufacturing Work Order document.

	``frappe._dict`` already gives both attribute access and the ``get`` the
	image helpers use; this only adds the permission hook ``update_photoshop_images``
	calls.
	"""

	def check_permission(self, ptype):
		self.checked_permission = ptype


def _mwo(name="MWO-1", item_code="ITEM-1", for_fg=1, docstatus=0, **images):
	return _StubMWO(
		name=name,
		doctype="Manufacturing Work Order",
		item_code=item_code,
		for_fg=for_fg,
		docstatus=docstatus,
		**images,
	)


def _fake_get_value(is_photoshop=1):
	"""Stand in for frappe.db.get_value for the one shape the guard now uses:
	the Item flag (scalar)."""

	def _inner(doctype, name, fieldname, *args, **kwargs):
		if doctype == "Item" and fieldname == "custom_is_photoshop_images":
			return is_photoshop
		return None

	return _inner


class TestPhotoshopImageHelpers(IntegrationTestCase):
	"""The gap helpers carry no in_test guard, so they run normally."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_required_pair_is_front_then_left(self):
		self.assertEqual(REQUIRED_MWO_IMAGE_FIELDS, (FRONT, LEFT))

	def test_only_two_slots_exist(self):
		# "Only in the MWO, two images" - there are no optional extra views.
		self.assertEqual(sorted(MWO_IMAGE_FIELDS), sorted([FRONT, LEFT]))

	def test_gaps_preserve_front_then_left_order(self):
		self.assertEqual(_get_empty_mwo_image_fields(_mwo()), [FRONT, LEFT])

	def test_gaps_report_only_the_empty_slot(self):
		self.assertEqual(_get_empty_mwo_image_fields(_mwo(**{FRONT: "/f.png"})), [LEFT])

	def test_no_gaps_when_both_are_attached(self):
		doc = _mwo(**{FRONT: "/f.png", LEFT: "/l.png"})
		self.assertEqual(_get_empty_mwo_image_fields(doc), [])

	def test_blank_string_counts_as_empty(self):
		doc = _mwo(**{FRONT: "", LEFT: None})
		self.assertEqual(_get_empty_mwo_image_fields(doc), [FRONT, LEFT])

	@patch(f"{MOD}.frappe.db.get_value")
	def test_flag_lookup_reads_the_item(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(is_photoshop=1)
		self.assertTrue(_photoshop_required_for_item("ITEM-1"))
		mock_get_value.assert_called_once_with(
			"Item", "ITEM-1", "custom_is_photoshop_images"
		)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_unflagged_item_needs_no_images(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(is_photoshop=0)
		self.assertFalse(_photoshop_required_for_item("ITEM-1"))

	@patch(f"{MOD}.frappe.db.get_value")
	def test_missing_item_code_is_not_looked_up(self, mock_get_value):
		self.assertFalse(_photoshop_required_for_item(None))
		mock_get_value.assert_not_called()


class TestGetMissingPhotoshopImages(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MOD}.frappe.get_doc")
	def test_payload_skips_unflagged_items(self, mock_get_doc):
		mock_get_doc.return_value = _mwo()
		with patch(f"{MOD}.frappe.db.get_value", _fake_get_value(is_photoshop=0)):
			self.assertEqual(
				get_missing_photoshop_images("MWO-1"), {"check_required": False}
			)

	@patch(f"{MOD}.frappe.get_doc")
	def test_payload_skips_non_fg_work_orders(self, mock_get_doc):
		mock_get_doc.return_value = _mwo(for_fg=0)
		with patch(f"{MOD}.frappe.db.get_value", _fake_get_value(is_photoshop=1)):
			self.assertEqual(
				get_missing_photoshop_images("MWO-1"), {"check_required": False}
			)

	def test_payload_skips_a_call_with_no_work_order(self):
		# A cached copy of the previous client bundle posts item_code/master_bom
		# and no work order. It must degrade quietly, not raise.
		self.assertEqual(
			get_missing_photoshop_images(item_code="ITEM-1", master_bom="BOM-1"),
			{"check_required": False},
		)

	@patch(f"{MOD}.frappe.get_doc")
	def test_payload_reports_fieldnames_not_labels(self, mock_get_doc):
		mock_get_doc.return_value = _mwo(**{FRONT: "/f.png"})
		with patch(f"{MOD}.frappe.db.get_value", _fake_get_value(is_photoshop=1)):
			payload = get_missing_photoshop_images("MWO-1")

		self.assertTrue(payload["check_required"])
		self.assertEqual(payload["missing"], [LEFT])
		self.assertEqual(payload["image_fields"], MWO_IMAGE_FIELDS)

	@patch(f"{MOD}.frappe.get_doc")
	def test_payload_blocks_nothing_once_both_are_attached(self, mock_get_doc):
		mock_get_doc.return_value = _mwo(**{FRONT: "/f.png", LEFT: "/l.png"})
		with patch(f"{MOD}.frappe.db.get_value", _fake_get_value(is_photoshop=1)):
			payload = get_missing_photoshop_images("MWO-1")

		self.assertTrue(payload["check_required"])
		self.assertEqual(payload["missing"], [])

	@patch(f"{MOD}.frappe.get_doc")
	def test_payload_carries_no_item_or_bom_slots(self, mock_get_doc):
		mock_get_doc.return_value = _mwo()
		with patch(f"{MOD}.frappe.db.get_value", _fake_get_value(is_photoshop=1)):
			payload = get_missing_photoshop_images("MWO-1")

		for gone in ("optional_item", "item_image_fields", "bom_image_fields"):
			self.assertNotIn(gone, payload)


class TestUpdatePhotoshopImages(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MOD}.frappe.db.set_value")
	@patch(f"{MOD}.frappe.get_doc")
	def test_writes_the_work_order_and_nothing_else(self, mock_get_doc, mock_set_value):
		mock_get_doc.return_value = _mwo()
		result = update_photoshop_images("MWO-1", {FRONT: "/f.png", LEFT: "/l.png"})

		self.assertTrue(result["success"])
		mock_set_value.assert_called_once_with(
			"Manufacturing Work Order",
			"MWO-1",
			{FRONT: "/f.png", LEFT: "/l.png"},
			update_modified=True,
		)

	@patch(f"{MOD}.frappe.db.set_value")
	@patch(f"{MOD}.frappe.get_doc")
	def test_accepts_a_json_string(self, mock_get_doc, mock_set_value):
		mock_get_doc.return_value = _mwo()
		update_photoshop_images("MWO-1", f'{{"{FRONT}": "/f.png"}}')

		self.assertEqual(mock_set_value.call_args[0][2], {FRONT: "/f.png"})

	@patch(f"{MOD}.frappe.db.set_value")
	@patch(f"{MOD}.frappe.get_doc")
	def test_ignores_unknown_and_empty_slots(self, mock_get_doc, mock_set_value):
		mock_get_doc.return_value = _mwo()
		result = update_photoshop_images(
			"MWO-1",
			{
				FRONT: "/f.png",
				LEFT: "",
				"finish_top_view": "/t.png",
				"item_code": "HACKED",
			},
		)

		self.assertEqual(result["updated"], [FRONT])
		self.assertEqual(mock_set_value.call_args[0][2], {FRONT: "/f.png"})

	@patch(f"{MOD}.frappe.db.set_value")
	@patch(f"{MOD}.frappe.get_doc")
	def test_a_partial_upload_is_allowed(self, mock_get_doc, mock_set_value):
		# before_submit is the single gate; the dialog may save one slot at a time.
		mock_get_doc.return_value = _mwo()
		result = update_photoshop_images("MWO-1", {LEFT: "/l.png"})
		self.assertEqual(result["updated"], [LEFT])

	@patch(f"{MOD}.frappe.db.set_value")
	@patch(f"{MOD}.frappe.get_doc")
	def test_nothing_to_write_touches_no_row(self, mock_get_doc, mock_set_value):
		mock_get_doc.return_value = _mwo()
		result = update_photoshop_images("MWO-1", {})

		self.assertEqual(result["updated"], [])
		mock_set_value.assert_not_called()

	@patch(f"{MOD}.frappe.db.set_value")
	@patch(f"{MOD}.frappe.get_doc")
	def test_checks_write_permission(self, mock_get_doc, mock_set_value):
		doc = _mwo()
		mock_get_doc.return_value = doc
		update_photoshop_images("MWO-1", {FRONT: "/f.png"})
		self.assertEqual(doc.checked_permission, "write")

	@patch(f"{MOD}.frappe.db.set_value")
	@patch(f"{MOD}.frappe.get_doc")
	def test_refuses_a_submitted_work_order(self, mock_get_doc, mock_set_value):
		mock_get_doc.return_value = _mwo(docstatus=1)
		with self.assertRaises(frappe.ValidationError):
			update_photoshop_images("MWO-1", {FRONT: "/f.png"})
		mock_set_value.assert_not_called()


@patch(f"{MOD}.frappe.flags", NOT_IN_TEST)
class TestValidatePhotoshopImages(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MOD}.frappe.db.get_value")
	def test_no_op_when_item_is_not_flagged(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(is_photoshop=0)
		# No images attached, but the design does not need any.
		ManufacturingWorkOrder.validate_photoshop_images(_mwo())

	@patch(f"{MOD}.frappe.db.get_value")
	def test_no_op_on_non_fg_work_orders(self, mock_get_value):
		ManufacturingWorkOrder.validate_photoshop_images(_mwo(for_fg=0))
		mock_get_value.assert_not_called()

	@patch(f"{MOD}.frappe.db.get_value")
	def test_no_op_without_an_item_code(self, mock_get_value):
		ManufacturingWorkOrder.validate_photoshop_images(_mwo(item_code=None))
		mock_get_value.assert_not_called()

	@patch(f"{MOD}.frappe.db.get_value")
	def test_throws_naming_both_views_when_the_mwo_is_empty(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(is_photoshop=1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			ManufacturingWorkOrder.validate_photoshop_images(_mwo())
		message = str(ctx.exception)
		self.assertIn(MWO_IMAGE_FIELDS[FRONT], message)
		self.assertIn(MWO_IMAGE_FIELDS[LEFT], message)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_throws_naming_left_view_only(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(is_photoshop=1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			ManufacturingWorkOrder.validate_photoshop_images(_mwo(**{FRONT: "/f.png"}))
		message = str(ctx.exception)
		self.assertIn(MWO_IMAGE_FIELDS[LEFT], message)
		self.assertNotIn(MWO_IMAGE_FIELDS[FRONT], message)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_throws_naming_front_view_only(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(is_photoshop=1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			ManufacturingWorkOrder.validate_photoshop_images(_mwo(**{LEFT: "/l.png"}))
		message = str(ctx.exception)
		self.assertIn(MWO_IMAGE_FIELDS[FRONT], message)
		self.assertNotIn(MWO_IMAGE_FIELDS[LEFT], message)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_passes_once_both_are_attached_to_the_work_order(self, mock_get_value):
		mock_get_value.side_effect = _fake_get_value(is_photoshop=1)
		doc = _mwo(**{FRONT: "/f.png", LEFT: "/l.png"})
		ManufacturingWorkOrder.validate_photoshop_images(doc)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_never_reads_the_item_or_bom_images(self, mock_get_value):
		# The ONLY database read is the Item flag - not the Item's or the BOM's
		# own finish-image fields. That is what "not attached in Item and BOM" means.
		mock_get_value.side_effect = _fake_get_value(is_photoshop=1)
		doc = _mwo(master_bom="BOM-1", **{FRONT: "/f.png", LEFT: "/l.png"})
		ManufacturingWorkOrder.validate_photoshop_images(doc)

		mock_get_value.assert_called_once_with(
			"Item", "ITEM-1", "custom_is_photoshop_images"
		)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_an_unlinked_master_bom_no_longer_blocks(self, mock_get_value):
		# Used to throw "Master BOM Not Linked"; the BOM is irrelevant now.
		mock_get_value.side_effect = _fake_get_value(is_photoshop=1)
		doc = _mwo(master_bom=None, **{FRONT: "/f.png", LEFT: "/l.png"})
		ManufacturingWorkOrder.validate_photoshop_images(doc)

	@patch(f"{MOD}.frappe.db.get_value")
	def test_short_circuits_under_the_test_runner(self, mock_get_value):
		with patch(f"{MOD}.frappe.flags", frappe._dict(in_test=True)):
			ManufacturingWorkOrder.validate_photoshop_images(_mwo())
		mock_get_value.assert_not_called()
