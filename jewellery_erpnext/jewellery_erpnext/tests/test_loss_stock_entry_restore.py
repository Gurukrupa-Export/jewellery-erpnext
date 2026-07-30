# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for restoring reduced reservations when an Employee IR is cancelled.

The bug: ``Stock Reservation Entry.custom_replaced_sre_snapshot`` was referenced by
``loss_stock_entry`` but declared nowhere in the repo -- no patch, no ``patches.txt``
entry, no ``custom/*.json``, no fixture. It existed only on the ``gk`` dev site, where it
had been hand-created in the UI. Everywhere else the SELECT in ``_restore_reduced_sres``
raised ``1054 Unknown column 'custom_replaced_sre_snapshot'``, which blocked *every*
Employee IR cancel.

The write side failed silently, which is why it went unnoticed: assigning an unknown
fieldname to a Document is not an error -- ``get_valid_dict()`` filters it out -- so
``insert()`` simply dropped the snapshot and the reduction left no trace.

``TestSnapshotFieldProvisioned`` is the regression guard for the provisioning itself
(compare ``test_fetch_from_columns.py``, which guards the same class of bug for
``fetch_from`` targets). The rest are mocked/pure-logic in the style of
``test_loss_stock_entry_spent_sre.py``: SimpleNamespace fakes, no DB.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	loss_stock_entry as lse,
)

_LSE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry"


def _loss_row(**fields):
	base = {
		"idx": 1,
		"item_code": "M-G-22KT-91.9-Y",
		"batch_no": "BATCH-A",
		"manufacturing_operation": "MOP-CURRENT",
		"proportionally_loss": 0.143,
	}
	base.update(fields)
	return SimpleNamespace(**base)


def _sb(batch_no, qty, delivered_qty=0.0):
	return SimpleNamespace(
		batch_no=batch_no, qty=qty, delivered_qty=delivered_qty, idx=1
	)


def _sre(sb_entries, reserved, **extra):
	base = {
		"name": "SRE-1",
		"warehouse": "WH",
		"reserved_qty": reserved,
		"delivered_qty": 0.0,
		"transferred_qty": 0.0,
		"consumed_qty": 0.0,
		"available_qty": reserved,
		"voucher_qty": reserved,
		"voucher_type": "Material Request",
		"voucher_no": "MR-1",
		"voucher_detail_no": "MRI-1",
		"reservation_based_on": "Serial and Batch",
		"sb_entries": sb_entries,
		"cancel": MagicMock(),
	}
	base.update(extra)
	return SimpleNamespace(**base)


def _clone(sb_entries):
	return SimpleNamespace(
		sb_entries=sb_entries,
		flags=SimpleNamespace(ignore_permissions=False),
		insert=MagicMock(),
		submit=MagicMock(),
	)


class TestSnapshotFieldProvisioned(IntegrationTestCase):
	"""The column must exist, or every Employee IR cancel raises 1054."""

	def test_snapshot_column_exists(self):
		self.assertTrue(
			frappe.db.has_column(
				"Stock Reservation Entry", "custom_replaced_sre_snapshot"
			),
			"Stock Reservation Entry.custom_replaced_sre_snapshot is missing -- "
			"_restore_reduced_sres would raise 1054 on every Employee IR cancel. "
			"Run jewellery_erpnext.patches.add_sre_replaced_snapshot_field.execute",
		)

	def test_patch_is_wired_into_patches_txt(self):
		"""The column only reaches other sites if migrate runs the patch."""
		patches = frappe.get_file_items(
			frappe.get_app_path("jewellery_erpnext", "patches.txt")
		)

		self.assertIn(
			"jewellery_erpnext.patches.add_sre_replaced_snapshot_field", patches
		)

	def test_both_restore_markers_exist(self):
		"""_restore_reduced_sres queries employee_ir too; it must be a real column."""
		for column in ("employee_ir", "custom_replaced_sre_snapshot"):
			self.assertTrue(
				frappe.db.has_column("Stock Reservation Entry", column),
				f"Stock Reservation Entry.{column} is missing -- the restore lookup "
				f"would raise 1054",
			)

	def test_snapshot_field_is_long_text_and_no_copy(self):
		"""Data would truncate a multi-batch snapshot; a copied snapshot names a foreign EIR."""
		meta = frappe.get_meta("Stock Reservation Entry")
		field = meta.get_field("custom_replaced_sre_snapshot")

		self.assertIsNotNone(field)
		self.assertEqual(field.fieldtype, "Long Text")
		self.assertTrue(field.no_copy)


class TestReduceSreStampsMarkers(IntegrationTestCase):
	"""_reduce_sre must record BOTH markers, or the reduction cannot be undone."""

	def test_stamps_employee_ir_and_snapshot(self):
		sre = _sre([_sb("BATCH-A", 3.0), _sb("BATCH-B", 2.0)], 5.0)
		clone = _clone([_sb("BATCH-A", 3.0), _sb("BATCH-B", 2.0)])

		with patch("frappe.copy_doc", return_value=clone):
			lse._reduce_sre(
				SimpleNamespace(name="EIR-1"),
				_loss_row(batch_no="BATCH-A"),
				sre,
				0.5,
				"employee_loss_details",
			)

		self.assertEqual(clone.employee_ir, "EIR-1")
		snapshot = json.loads(clone.custom_replaced_sre_snapshot)
		self.assertEqual(snapshot["employee_ir"], "EIR-1")
		self.assertEqual(snapshot["original_reserved_qty"], 5.0)
		self.assertEqual(snapshot["batch_no"], "BATCH-A")
		self.assertEqual(snapshot["original_sb_qty"], 3.0)

	def test_snapshot_records_remaining_not_gross_for_delivered_sre(self):
		"""reserved 5 / delivered 4: restore must give back 1, not 5."""
		sre = _sre([_sb("BATCH-A", 5.0, delivered_qty=4.0)], 5.0, delivered_qty=4.0)
		clone = _clone([_sb("BATCH-A", 5.0, delivered_qty=4.0)])

		with patch("frappe.copy_doc", return_value=clone):
			lse._reduce_sre(
				SimpleNamespace(name="EIR-1"),
				_loss_row(batch_no="BATCH-A"),
				sre,
				0.5,
				"employee_loss_details",
			)

		snapshot = json.loads(clone.custom_replaced_sre_snapshot)
		self.assertEqual(snapshot["original_reserved_qty"], 5.0)
		self.assertEqual(snapshot["original_delivered_qty"], 4.0)
		self.assertEqual(snapshot["original_sb_qty"], 1.0)

	def test_spent_sre_is_never_stamped(self):
		"""A spent reservation is left alone, so there is nothing to mark."""
		sre = MagicMock()
		sre.reserved_qty = 3.396
		sre.delivered_qty = 3.396
		sre.transferred_qty = 0.0
		sre.consumed_qty = 0.0

		lse._reduce_sre(
			SimpleNamespace(name="EIR-1"),
			_loss_row(),
			sre,
			0.143,
			"employee_loss_details",
		)

		sre.cancel.assert_not_called()


class TestRestoreLookup(IntegrationTestCase):
	"""The lookup must match the new exact key AND the legacy snapshot rows."""

	def _run(self, rows):
		db = MagicMock()
		db.sql.return_value = rows
		with patch("frappe.db", db):
			lse._restore_reduced_sres(SimpleNamespace(name="EIR-1"))
		return db.sql.call_args

	def test_queries_employee_ir_exactly_and_snapshot_as_legacy_fallback(self):
		query, params = self._run([])[0]

		self.assertIn("employee_ir = %(eir)s", query)
		self.assertIn("custom_replaced_sre_snapshot LIKE %(legacy)s", query)
		self.assertEqual(params["eir"], "EIR-1")
		self.assertEqual(params["legacy"], '%"employee_ir": "EIR-1"%')

	def test_returns_the_number_of_reservations_found(self):
		"""cancel_loss_stock_entries relies on this to spot unrestorable reductions."""
		db = MagicMock()
		db.sql.return_value = []
		with patch("frappe.db", db):
			self.assertEqual(
				lse._restore_reduced_sres(SimpleNamespace(name="EIR-1")), 0
			)


class TestRestoreClearsMarkers(IntegrationTestCase):
	"""A restored reservation is whole again and must not match a second cancel."""

	def test_clears_both_markers_on_the_restored_entry(self):
		snapshot = json.dumps(
			{
				"employee_ir": "EIR-1",
				"original_reserved_qty": 5.0,
				"original_delivered_qty": 0.0,
				"batch_no": "BATCH-A",
				"original_sb_qty": 3.0,
			}
		)
		sre_doc = _sre([_sb("BATCH-A", 2.5)], 2.5)
		restored = _clone([_sb("BATCH-A", 2.5)])
		restored.employee_ir = "EIR-1"
		restored.custom_replaced_sre_snapshot = snapshot

		db = MagicMock()
		# as_dict=True yields frappe._dict, which the restore loop reads by attribute.
		db.sql.return_value = [
			frappe._dict(name="SRE-2", custom_replaced_sre_snapshot=snapshot)
		]
		with (
			patch("frappe.db", db),
			patch("frappe.get_doc", return_value=sre_doc),
			patch("frappe.copy_doc", return_value=restored),
			patch(f"{_LSE}._reservation_voucher_qty", return_value=5.0),
		):
			lse._restore_reduced_sres(SimpleNamespace(name="EIR-1"))

		self.assertIsNone(restored.employee_ir)
		self.assertIsNone(restored.custom_replaced_sre_snapshot)
		# Only the snapshot's batch row is restored, not every row.
		self.assertEqual(restored.sb_entries[0].qty, 3.0)
		restored.submit.assert_called_once()


class TestOrphanedReductionGuard(IntegrationTestCase):
	"""Historical reductions carry no marker; cancelling them would silently short stock."""

	def _eir(self):
		return SimpleNamespace(name="EIR-1")

	def test_no_throw_when_markers_exist(self):
		db = MagicMock()
		with (
			patch("frappe.db", db),
			patch(f"{_LSE}._restore_marker_count", return_value=3),
		):
			lse._assert_no_orphaned_reductions(self._eir(), ["SE-1"])

		db.sql.assert_not_called()

	def test_no_throw_when_nothing_was_ever_reduced(self):
		"""Every SRE spent or fully consumed: zero markers is the correct outcome."""
		db = MagicMock()
		db.sql.side_effect = [
			[("2026-06-18 10:00:00", "2026-06-18 10:00:05")],  # SE window
			[],  # no orphan signature
		]
		with (
			patch("frappe.db", db),
			patch(f"{_LSE}._restore_marker_count", return_value=0),
		):
			lse._assert_no_orphaned_reductions(self._eir(), ["SE-1"])

	def test_throws_on_the_orphan_signature(self):
		db = MagicMock()
		db.sql.side_effect = [
			[("2026-06-18 10:00:00", "2026-06-18 10:00:05")],  # SE window
			[("SRE-OLD", "SRE-NEW")],  # cancelled -> unmarked replacement
		]
		with (
			patch("frappe.db", db),
			patch(f"{_LSE}._restore_marker_count", return_value=0),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				lse._assert_no_orphaned_reductions(self._eir(), ["SE-1"])

		message = str(ctx.exception)
		self.assertIn("EIR-1", message)
		self.assertIn("SRE-OLD", message)
		self.assertIn("SRE-NEW", message)

	def test_no_throw_when_the_stock_entries_have_no_creation_window(self):
		db = MagicMock()
		db.sql.side_effect = [[(None, None)]]
		with (
			patch("frappe.db", db),
			patch(f"{_LSE}._restore_marker_count", return_value=0),
		):
			lse._assert_no_orphaned_reductions(self._eir(), ["SE-1"])
