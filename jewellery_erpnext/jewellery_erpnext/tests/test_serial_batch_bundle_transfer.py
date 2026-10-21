"""CustomSerialBatchBundle.make_serial_batch_no_bundle on both ERPNext signatures.

ERPNext 16.51 changed ``make_serial_batch_no_bundle_for_material_transfer`` to take the bundle
to copy and added a transit-package re-link; the override called it with no argument and every
inward transfer row threw "missing 1 required positional argument: 'bundle'". DB-free.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.doc_events import (
	utils as sbb_utils,
)

CustomSerialBatchBundle = sbb_utils.CustomSerialBatchBundle


def _bundle(actual_qty=1.0, voucher_type="Stock Entry"):
	obj = CustomSerialBatchBundle.__new__(CustomSerialBatchBundle)
	obj.sle = frappe._dict(
		actual_qty=actual_qty,
		voucher_type=voucher_type,
		voucher_no="STE-1",
		voucher_detail_no="SED-1",
		serial_and_batch_bundle="SBB-NEW",
		is_cancelled=0,
	)
	obj.validate_item = MagicMock()
	obj.is_material_transfer = MagicMock(return_value=True)
	return obj


class TestMaterialTransferBundleCall(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, obj):
		mock_db = MagicMock()
		mock_db.get_value.return_value = "SBB-FROM-ROW"
		with patch(f"{sbb_utils.__name__}.frappe.db", mock_db):
			obj.make_serial_batch_no_bundle()
		return mock_db

	def test_new_signature_is_handed_the_rows_bundle(self):
		obj = _bundle()
		calls = []

		def helper(bundle):
			calls.append(bundle)

		obj.make_serial_batch_no_bundle_for_material_transfer = helper
		obj.get_transit_package = lambda: None
		self._run(obj)
		self.assertEqual(calls, ["SBB-FROM-ROW"])

	def test_old_signature_is_called_without_arguments(self):
		obj = _bundle()
		calls = []

		def helper():
			calls.append("called")

		obj.make_serial_batch_no_bundle_for_material_transfer = helper
		self._run(obj)
		self.assertEqual(calls, ["called"])

	def test_a_transit_package_is_relinked_first(self):
		obj = _bundle(voucher_type="Delivery Note")
		calls = []
		obj.make_serial_batch_no_bundle_for_material_transfer = lambda bundle: calls.append(
			bundle
		)
		obj.get_transit_package = lambda: "SBB-TRANSIT"
		obj.is_packed_entry = lambda: False
		mock_db = self._run(obj)
		self.assertEqual(calls, ["SBB-TRANSIT"])
		obj.validate_item.assert_not_called()
		mock_db.set_value.assert_called_once_with(
			"Delivery Note Item", "SED-1", "serial_and_batch_bundle", "SBB-NEW"
		)

	def test_an_outward_row_never_takes_the_transfer_path(self):
		obj = _bundle(actual_qty=-1.0)
		obj.make_serial_batch_no_bundle_for_material_transfer = MagicMock()
		obj.get_transit_package = MagicMock()
		with patch.object(sbb_utils, "CustomSerialBatchCreation") as creation:
			creation.return_value.make_serial_and_batch_bundle.return_value = SimpleNamespace()
			obj.is_rejected_entry = lambda: False
			obj.set_serial_and_batch_bundle = MagicMock()
			obj.item_code = "ITEM"
			obj.warehouse = "WH"
			obj.company = "C"
			obj.sle.posting_date = obj.sle.posting_time = obj.sle.incoming_rate = None
			self._run(obj)
		obj.make_serial_batch_no_bundle_for_material_transfer.assert_not_called()
		obj.get_transit_package.assert_not_called()
