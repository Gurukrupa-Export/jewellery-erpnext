"""A repair PMO's "Serial No BOM" is the BOM of its own serial.

The quotation / plan row carries the item's design BOM for a repair, so the PMO derives the
BOM from the serial itself (``utils.get_serial_bom``) in ``after_insert``. DB-free.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	ParentManufacturingOrder,
)
from jewellery_erpnext.utils import get_serial_bom

PMO_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order"


class TestPMOAfterInsertSerialIdBom(IntegrationTestCase):
	def _run(self, serial_no, serial_id_bom, tagged_bom, serial_custom_bom=None):
		written = {}
		fake = SimpleNamespace(
			custom_tracking_bom=None,
			serial_no=serial_no,
			serial_id_bom=serial_id_bom,
			db_set=lambda field, value: written.__setitem__(field, value),
		)
		mock_db = MagicMock()
		mock_db.exists.return_value = tagged_bom
		mock_db.get_value.return_value = serial_custom_bom
		with patch(f"{PMO_MOD}.frappe.db", mock_db):
			ParentManufacturingOrder.after_insert(fake)
		return written

	def test_derives_from_tag_when_row_had_none(self):
		written = self._run("SN-1", None, "BOM-FROM-TAG")
		self.assertEqual(written.get("serial_id_bom"), "BOM-FROM-TAG")

	def test_the_serials_tagged_bom_replaces_a_design_bom_already_on_the_pmo(self):
		written = self._run("SN-1", "BOM-DESIGN", "BOM-FROM-TAG")
		self.assertEqual(written.get("serial_id_bom"), "BOM-FROM-TAG")

	def test_the_serials_own_bom_no_wins_over_a_tagged_bom(self):
		written = self._run(
			"SN-1", "BOM-DESIGN", "BOM-FROM-TAG", serial_custom_bom="BOM-AS-BUILT"
		)
		self.assertEqual(written.get("serial_id_bom"), "BOM-AS-BUILT")

	def test_a_value_already_equal_is_not_rewritten(self):
		self.assertNotIn("serial_id_bom", self._run("SN-1", "BOM-FROM-TAG", "BOM-FROM-TAG"))

	def test_the_existing_value_stands_when_the_serial_has_no_bom_of_its_own(self):
		self.assertNotIn("serial_id_bom", self._run("SN-1", "BOM-EXISTING", None))

	def test_no_serial_means_no_derivation(self):
		self.assertNotIn("serial_id_bom", self._run(None, None, "BOM-FROM-TAG"))

	def test_untagged_serial_leaves_it_blank(self):
		self.assertNotIn("serial_id_bom", self._run("SN-1", None, None))


class TestGetSerialBom(IntegrationTestCase):
	"""utils.get_serial_bom: the serial's own BOM, never a Template that shares its tag."""

	def _bom(self, custom_bom_no, tagged):
		mock_db = MagicMock()
		mock_db.get_value.return_value = custom_bom_no
		mock_db.exists.return_value = tagged
		with patch("jewellery_erpnext.utils.frappe.db", mock_db):
			return get_serial_bom("SN-1"), mock_db

	def test_the_serials_own_bom_no_is_used(self):
		self.assertEqual(self._bom("BOM-AS-BUILT", "BOM-TAGGED")[0], "BOM-AS-BUILT")

	def test_a_tagged_bom_is_the_fallback(self):
		self.assertEqual(self._bom(None, "BOM-TAGGED")[0], "BOM-TAGGED")

	def test_only_a_submitted_finish_goods_bom_counts_as_tagged(self):
		_bom, db = self._bom(None, "BOM-TAGGED")
		self.assertEqual(
			db.exists.call_args[0],
			("BOM", {"tag_no": "SN-1", "bom_type": "Finish Goods", "docstatus": 1}),
		)

	def test_no_serial_or_no_bom_gives_none(self):
		self.assertIsNone(get_serial_bom(None))
		self.assertIsNone(self._bom(None, None)[0])
