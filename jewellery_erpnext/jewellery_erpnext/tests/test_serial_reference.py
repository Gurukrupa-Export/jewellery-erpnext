"""Pure-logic unit tests for the Serial No sales-reference pointer.

Covers the decision table in ``doc_events/serial_reference.py``: forward-only claiming,
reclaim from a dead incumbent, the no-op idempotency guard, and release-on-removal.

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_serial_reference
"""

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import serial_reference

MOD = "jewellery_erpnext.jewellery_erpnext.doc_events.serial_reference"


class _Row(SimpleNamespace):
	"""SimpleNamespace that also supports Frappe-style ``.get()`` access."""

	def get(self, key, default=None):
		return getattr(self, key, default)


class _Doc(_Row):
	def get_doc_before_save(self):
		return getattr(self, "_before", None)


def _doc(doctype, name, serials, before=None):
	return _Doc(
		doctype=doctype,
		name=name,
		items=[_Row(serial_no=s) for s in serials],
		_before=before,
	)


def _pointer(name, ref_dt=None, ref_dn=None):
	"""A row as frappe.get_all("Serial No", fields=[...]) would return it."""
	return _Row(
		name=name,
		custom_reference_doctype=ref_dt,
		custom_reference_docname=ref_dn,
	)


class TestSplitSerials(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_empty_values(self):
		self.assertEqual(serial_reference._split_serials(None), [])
		self.assertEqual(serial_reference._split_serials(""), [])
		self.assertEqual(serial_reference._split_serials("   \n  "), [])

	def test_single_link_value(self):
		# Sales Order Item.serial_no is a Link -- exactly one serial.
		self.assertEqual(serial_reference._split_serials("SN-001"), ["SN-001"])

	def test_newline_separated_text(self):
		# Delivery Note / Sales Invoice Item.serial_no are core Text fields.
		self.assertEqual(
			serial_reference._split_serials("SN-001\nSN-002\n"), ["SN-001", "SN-002"]
		)

	def test_comma_separated_and_whitespace(self):
		self.assertEqual(
			serial_reference._split_serials(" SN-001 , SN-002 "), ["SN-001", "SN-002"]
		)


class TestSerialsOf(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_dedupes_and_preserves_order(self):
		doc = _doc("Delivery Note", "DN-1", ["SN-002", "SN-001", "SN-002"])
		self.assertEqual(serial_reference._serials_of(doc), ["SN-002", "SN-001"])

	def test_falls_back_to_custom_serial_no(self):
		# The gke product-return client flow fills custom_serial_no as well.
		doc = _Doc(
			doctype="Sales Invoice",
			name="SI-1",
			items=[_Row(serial_no=None, custom_serial_no="SN-009")],
		)
		self.assertEqual(serial_reference._serials_of(doc), ["SN-009"])

	def test_custom_serial_no_never_double_counts(self):
		doc = _Doc(
			doctype="Sales Invoice",
			name="SI-1",
			items=[_Row(serial_no="SN-009", custom_serial_no="SN-009")],
		)
		self.assertEqual(serial_reference._serials_of(doc), ["SN-009"])

	def test_rows_without_serials_are_skipped(self):
		# e.g. the Hybrid "Subcontracting Charges" row.
		doc = _doc("Sales Order", "SO-1", [None, "SN-001", ""])
		self.assertEqual(serial_reference._serials_of(doc), ["SN-001"])


class TestDecideClaims(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_claims_unset_pointer(self):
		rows = [_pointer("SN-001")]
		self.assertEqual(
			serial_reference._decide_claims("Sales Order", "SO-1", rows), ["SN-001"]
		)

	def test_no_write_when_already_ours(self):
		# The idempotency guard: re-saving an unchanged document writes nothing.
		rows = [_pointer("SN-001", "Sales Order", "SO-1")]
		self.assertEqual(
			serial_reference._decide_claims("Sales Order", "SO-1", rows), []
		)

	def test_same_stage_latest_wins(self):
		rows = [_pointer("SN-001", "Sales Order", "SO-A")]
		self.assertEqual(
			serial_reference._decide_claims("Sales Order", "SO-B", rows), ["SN-001"]
		)

	def test_advances_forward_so_to_dn(self):
		rows = [_pointer("SN-001", "Sales Order", "SO-1")]
		self.assertEqual(
			serial_reference._decide_claims("Delivery Note", "DN-1", rows), ["SN-001"]
		)

	def test_advances_forward_dn_to_si(self):
		rows = [_pointer("SN-001", "Delivery Note", "DN-1")]
		self.assertEqual(
			serial_reference._decide_claims("Sales Invoice", "SI-1", rows), ["SN-001"]
		)

	@patch(f"{MOD}._incumbent_is_live", return_value=True)
	def test_draft_so_cannot_steal_from_live_si(self, _live):
		# The stale-draft protection: write-on-validate must not let a brand-new draft
		# Sales Order claim a piece that is already on a live Sales Invoice.
		rows = [_pointer("SN-001", "Sales Invoice", "SI-1")]
		self.assertEqual(
			serial_reference._decide_claims("Sales Order", "SO-9", rows), []
		)

	@patch(f"{MOD}._incumbent_is_live", return_value=False)
	def test_reclaims_from_dead_incumbent(self, _live):
		# Cancelled / deleted / returned incumbent -> the piece is re-sellable.
		rows = [_pointer("SN-001", "Sales Invoice", "SI-1")]
		self.assertEqual(
			serial_reference._decide_claims("Sales Order", "SO-9", rows), ["SN-001"]
		)

	@patch(f"{MOD}._incumbent_is_live")
	def test_liveness_probe_only_runs_for_backward_moves(self, live):
		# Forward and same-stage decisions must not cost a query.
		rows = [
			_pointer("SN-001"),
			_pointer("SN-002", "Sales Order", "SO-A"),
			_pointer("SN-003", "Delivery Note", "DN-1"),
		]
		serial_reference._decide_claims("Sales Invoice", "SI-1", rows)
		live.assert_not_called()

	@patch(f"{MOD}._incumbent_is_live", return_value=True)
	def test_unknown_incumbent_doctype_is_always_claimable(self, live):
		# rank >= _STAGE_RANK.get(unknown, 0) is always true, so no probe is needed.
		rows = [_pointer("SN-001", "Stock Entry", "SE-1")]
		self.assertEqual(
			serial_reference._decide_claims("Sales Order", "SO-1", rows), ["SN-001"]
		)
		live.assert_not_called()

	def test_half_written_pointer_is_treated_as_unclaimed(self):
		rows = [_pointer("SN-001", "Sales Invoice", None)]
		self.assertEqual(
			serial_reference._decide_claims("Sales Order", "SO-1", rows), ["SN-001"]
		)


class TestIncumbentIsLive(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_unmanaged_doctype_is_dead(self):
		self.assertFalse(serial_reference._incumbent_is_live("Stock Entry", "SE-1"))

	@patch(f"{MOD}.frappe.db.get_value", return_value=None)
	def test_deleted_document_is_dead(self, get_value):
		self.assertFalse(serial_reference._incumbent_is_live("Sales Order", "SO-1"))

	@patch(f"{MOD}.frappe.db.get_value", return_value={"docstatus": 2})
	def test_cancelled_document_is_dead(self, get_value):
		self.assertFalse(serial_reference._incumbent_is_live("Sales Order", "SO-1"))

	@patch(f"{MOD}.frappe.db.get_value", return_value={"docstatus": 1, "is_return": 1})
	def test_return_is_dead(self, get_value):
		self.assertFalse(serial_reference._incumbent_is_live("Sales Invoice", "SI-1"))

	@patch(f"{MOD}.frappe.db.get_value", return_value={"docstatus": 1, "is_return": 0})
	def test_submitted_forward_document_is_live(self, get_value):
		self.assertTrue(serial_reference._incumbent_is_live("Sales Invoice", "SI-1"))

	@patch(f"{MOD}.frappe.db.get_value", return_value={"docstatus": 0})
	def test_draft_is_live(self, get_value):
		self.assertTrue(serial_reference._incumbent_is_live("Sales Order", "SO-1"))

	@patch(f"{MOD}.frappe.db.get_value", return_value={"docstatus": 1})
	def test_sales_order_is_not_queried_for_is_return(self, get_value):
		# Sales Order has no is_return column; asking for it would raise 1054.
		serial_reference._incumbent_is_live("Sales Order", "SO-1")
		self.assertEqual(get_value.call_args[0][2], ["docstatus"])

	@patch(f"{MOD}.frappe.db.get_value", return_value={"docstatus": 1, "is_return": 0})
	def test_delivery_note_is_queried_for_is_return(self, get_value):
		serial_reference._incumbent_is_live("Delivery Note", "DN-1")
		self.assertEqual(get_value.call_args[0][2], ["docstatus", "is_return"])


@patch(f"{MOD}._has_pointer_fields", return_value=True)
class TestSetSerialReference(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MOD}._write_pointer")
	@patch(f"{MOD}._release")
	@patch(f"{MOD}.frappe.get_all")
	def test_claims_every_serial_on_the_document(
		self, get_all, release, write, _fields
	):
		get_all.return_value = [_pointer("SN-001"), _pointer("SN-002")]
		serial_reference.set_serial_reference(
			_doc("Sales Order", "SO-1", ["SN-001", "SN-002"])
		)
		write.assert_called_once_with(["SN-001", "SN-002"], "Sales Order", "SO-1")
		release.assert_not_called()

	@patch(f"{MOD}._write_pointer")
	@patch(f"{MOD}._release")
	@patch(f"{MOD}.frappe.get_all")
	def test_releases_serials_dropped_since_last_save(
		self, get_all, release, write, _fields
	):
		before = _doc("Sales Order", "SO-1", ["SN-001", "SN-002"])
		doc = _doc("Sales Order", "SO-1", ["SN-001"], before=before)
		get_all.return_value = [_pointer("SN-001", "Sales Order", "SO-1")]

		serial_reference.set_serial_reference(doc)

		release.assert_called_once_with(["SN-002"], "Sales Order", "SO-1")
		# SN-001 is already ours -> nothing to write.
		write.assert_called_once_with([], "Sales Order", "SO-1")

	@patch(f"{MOD}._write_pointer")
	@patch(f"{MOD}._release")
	@patch(f"{MOD}.frappe.get_all")
	def test_emptying_the_document_releases_everything(
		self, get_all, release, write, _fields
	):
		before = _doc("Delivery Note", "DN-1", ["SN-001"])
		doc = _doc("Delivery Note", "DN-1", [], before=before)

		serial_reference.set_serial_reference(doc)

		release.assert_called_once_with(["SN-001"], "Delivery Note", "DN-1")
		# No serials left -> no SELECT, no write.
		get_all.assert_not_called()
		write.assert_not_called()

	@patch(f"{MOD}._write_pointer")
	@patch(f"{MOD}.frappe.get_all")
	def test_ignores_unmanaged_doctypes(self, get_all, write, _fields):
		serial_reference.set_serial_reference(_doc("Quotation", "QTN-1", ["SN-001"]))
		get_all.assert_not_called()
		write.assert_not_called()

	@patch(f"{MOD}._write_pointer")
	@patch(f"{MOD}.frappe.get_all")
	def test_ignores_document_without_a_name(self, get_all, write, _fields):
		serial_reference.set_serial_reference(_doc("Sales Order", None, ["SN-001"]))
		get_all.assert_not_called()
		write.assert_not_called()


class TestSetSerialReferenceWithoutFields(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MOD}._has_pointer_fields", return_value=False)
	@patch(f"{MOD}.frappe.get_all")
	def test_degrades_to_no_op_when_patch_has_not_run(self, get_all, _fields):
		# A site missing the columns must not take down all sales entry.
		serial_reference.set_serial_reference(_doc("Sales Order", "SO-1", ["SN-001"]))
		get_all.assert_not_called()

	@patch(f"{MOD}._has_pointer_fields", return_value=False)
	@patch(f"{MOD}._release")
	def test_clear_degrades_to_no_op_too(self, release, _fields):
		serial_reference.clear_serial_reference(_doc("Sales Order", "SO-1", ["SN-001"]))
		release.assert_not_called()


@patch(f"{MOD}._has_pointer_fields", return_value=True)
class TestClearSerialReference(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MOD}._release")
	def test_releases_all_serials_scoped_to_this_document(self, release, _fields):
		serial_reference.clear_serial_reference(
			_doc("Sales Invoice", "SI-1", ["SN-001", "SN-002"])
		)
		release.assert_called_once_with(["SN-001", "SN-002"], "Sales Invoice", "SI-1")

	@patch(f"{MOD}._release")
	def test_ignores_unmanaged_doctypes(self, release, _fields):
		serial_reference.clear_serial_reference(_doc("Quotation", "QTN-1", ["SN-001"]))
		release.assert_not_called()
