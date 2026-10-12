# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Tests for the Certificate ID copy from receiving rows onto batches.

Covers ``doc_events.stock_transactions.set_batch_certificate_id``, its two ``on_submit``
hook entries and ``patches.add_batch_certificate_id_fields``.

Most classes are pure logic per the suite convention: ``setUpClass`` is neutralized and the
module's ``frappe`` is replaced by a small recording fake, so no document is created. Three
classes are DB-backed and run only where the CI seed (``create_test_data``) exists:

- the three fields are provisioned;
- an existing field definition is never updated by the patch;
- a real Purchase Receipt submit stamps the batches minted during stock-ledger posting,
  which proves the row's bundle is in the database, submitted and Inward by the time the
  ``on_submit`` hook runs.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import stock_transactions
from jewellery_erpnext.jewellery_erpnext.doc_events.stock_transactions import (
	CERTIFICATE_FIELD,
	set_batch_certificate_id,
)
from jewellery_erpnext.patches import add_batch_certificate_id_fields as cert_patch

FIELD = CERTIFICATE_FIELD
PR_ITEM = "Purchase Receipt Item"
SI_ITEM = "Sales Invoice Item"
ALL_COLUMNS = {("Batch", FIELD), (PR_ITEM, FIELD), (SI_ITEM, FIELD)}

# Which bundle fields each item table has. Sales Invoice Item has no rejected bundle.
CHILD_BUNDLE_FIELDS = {
	PR_ITEM: {"serial_and_batch_bundle", "rejected_serial_and_batch_bundle"},
	SI_ITEM: {"serial_and_batch_bundle"},
}
CHILD_OF = {"Purchase Receipt": PR_ITEM, "Sales Invoice": SI_ITEM}

HOOK = "jewellery_erpnext.jewellery_erpnext.doc_events.stock_transactions.set_batch_certificate_id"
PR_T_ON_SUBMIT = "jewellery_erpnext.jewellery_erpnext.customization.purchase_receipt.purchase_receipt.on_submit"
SI_T_ON_SUBMIT = [
	"jewellery_erpnext.jewellery_erpnext.customization.sales_invoice.sales_invoice.on_submit",
	"jewellery_erpnext.jewellery_erpnext.doc_events.sales_invoice.on_submit",
]


class _FakeFrappe:
	"""Just enough of ``frappe`` for set_batch_certificate_id, recording every call.

	``rows`` maps an item row name to the bundle fields stored for it in the DATABASE.
	``bundles`` maps a bundle name to (type_of_transaction, docstatus, batch_nos).
	"""

	def __init__(self, rows=None, bundles=None, columns=ALL_COLUMNS):
		self.rows = rows or {}
		self.bundles = bundles or {}
		self.columns = set(columns)
		self.calls = []
		self.writes = []
		self.db = SimpleNamespace(
			has_column=self._has_column,
			get_value=self._get_value,
			set_value=self._set_value,
		)

	@property
	def reads(self):
		return [call for call in self.calls if call[0] != "has_column"]

	def _has_column(self, doctype, column):
		self.calls.append(("has_column", doctype, column))
		return (doctype, column) in self.columns

	def _get_value(self, doctype, name, fieldname=None, as_dict=False):
		self.calls.append(("get_value", doctype, name, tuple(fieldname)))
		if doctype == "Serial and Batch Bundle":
			bundle = self.bundles.get(name)
			if not bundle:
				return None
			return frappe._dict(type_of_transaction=bundle[0], docstatus=bundle[1])

		stored = self.rows.get(name)
		if stored is None:
			return None
		return frappe._dict({field: stored.get(field) for field in fieldname})

	def _set_value(self, doctype, name, fieldname, value=None, *args, **kwargs):
		self.writes.append((doctype, name, fieldname, value))

	def get_meta(self, doctype):
		return SimpleNamespace(has_field=CHILD_BUNDLE_FIELDS[doctype].__contains__)

	def get_all(self, doctype, filters=None, pluck=None, **kwargs):
		self.calls.append(("get_all", doctype, filters["parent"]))
		return list(self.bundles[filters["parent"]][2])


def _row(name="ROW-1", certificate="CERT-A"):
	# The in-memory row carries NO bundle: v16 writes it to the database during
	# stock-ledger posting, so the hook has to re-read it.
	return frappe._dict(name=name, custom_certificate_id=certificate)


def _doc(doctype="Purchase Receipt", is_return=0, rows=()):
	# NOTE: frappe._dict subclasses dict, so `doc.items` resolves to dict.items. The code
	# under test reaches rows through doc.get("items"), as this fake requires.
	doc = frappe._dict(doctype=doctype, is_return=is_return, items=list(rows))
	child = CHILD_OF[doctype]
	doc.meta = SimpleNamespace(
		get_field=lambda fieldname: SimpleNamespace(options=child)
	)
	return doc


def _run(fake, doc):
	with patch.object(stock_transactions, "frappe", fake):
		set_batch_certificate_id(doc, "on_submit")


def _stamp(batch_no, certificate="CERT-A"):
	return ("Batch", batch_no, FIELD, certificate)


class TestSetBatchCertificateId(IntegrationTestCase):
	"""What gets stamped, from which bundle, and what is left alone."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_stamps_every_batch_of_the_rows_database_bundle_in_sorted_order(self):
		fake = _FakeFrappe(
			rows={"ROW-1": {"serial_and_batch_bundle": "SABB-IN"}},
			bundles={"SABB-IN": ("Inward", 1, ["B-2", "B-1", None, "B-2"])},
		)
		_run(fake, _doc(rows=[_row()]))
		self.assertEqual(fake.writes, [_stamp("B-1"), _stamp("B-2")])

	def test_re_reads_only_the_bundle_fields_of_its_own_row(self):
		fake = _FakeFrappe(
			rows={"ROW-1": {"serial_and_batch_bundle": "SABB-IN"}},
			bundles={"SABB-IN": ("Inward", 1, ["B-1"])},
		)
		_run(fake, _doc(rows=[_row()]))
		self.assertIn(
			(
				"get_value",
				PR_ITEM,
				"ROW-1",
				("serial_and_batch_bundle", "rejected_serial_and_batch_bundle"),
			),
			fake.calls,
		)

	def test_rejected_bundle_is_stamped_too(self):
		fake = _FakeFrappe(
			rows={
				"ROW-1": {
					"serial_and_batch_bundle": "SABB-ACC",
					"rejected_serial_and_batch_bundle": "SABB-REJ",
				}
			},
			bundles={
				"SABB-ACC": ("Inward", 1, ["B-ACC"]),
				"SABB-REJ": ("Inward", 1, ["B-REJ"]),
			},
		)
		_run(fake, _doc(rows=[_row()]))
		self.assertEqual(fake.writes, [_stamp("B-ACC"), _stamp("B-REJ")])

	def test_each_row_stamps_its_own_certificate(self):
		fake = _FakeFrappe(
			rows={
				"ROW-1": {"serial_and_batch_bundle": "SABB-1"},
				"ROW-2": {"serial_and_batch_bundle": "SABB-2"},
			},
			bundles={
				"SABB-1": ("Inward", 1, ["B-1"]),
				"SABB-2": ("Inward", 1, ["B-2"]),
			},
		)
		_run(fake, _doc(rows=[_row("ROW-1", "CERT-A"), _row("ROW-2", "CERT-B")]))
		self.assertEqual(
			fake.writes, [_stamp("B-1", "CERT-A"), _stamp("B-2", "CERT-B")]
		)

	def test_row_without_a_certificate_reads_and_writes_nothing(self):
		fake = _FakeFrappe(
			rows={"ROW-1": {"serial_and_batch_bundle": "SABB-IN"}},
			bundles={"SABB-IN": ("Inward", 1, ["B-1"])},
		)
		_run(fake, _doc(rows=[_row(certificate=None), _row("ROW-2", "")]))
		self.assertEqual(fake.reads, [])
		self.assertEqual(fake.writes, [])

	def test_outward_bundle_writes_nothing(self):
		"""A Purchase Receipt return posts an Outward bundle."""
		fake = _FakeFrappe(
			rows={"ROW-1": {"serial_and_batch_bundle": "SABB-OUT"}},
			bundles={"SABB-OUT": ("Outward", 1, ["B-1"])},
		)
		_run(fake, _doc(is_return=1, rows=[_row()]))
		self.assertEqual(fake.writes, [])
		self.assertNotIn(("get_all", "Serial and Batch Entry", "SABB-OUT"), fake.calls)

	def test_draft_and_cancelled_bundles_write_nothing(self):
		for docstatus in (0, 2):
			with self.subTest(docstatus=docstatus):
				fake = _FakeFrappe(
					rows={"ROW-1": {"serial_and_batch_bundle": "SABB-IN"}},
					bundles={"SABB-IN": ("Inward", docstatus, ["B-1"])},
				)
				_run(fake, _doc(rows=[_row()]))
				self.assertEqual(fake.writes, [])

	def test_row_with_no_bundle_in_the_database_writes_nothing(self):
		fake = _FakeFrappe(rows={"ROW-1": {}})
		_run(fake, _doc(rows=[_row()]))
		self.assertEqual(fake.writes, [])

	def test_missing_bundle_header_writes_nothing(self):
		fake = _FakeFrappe(rows={"ROW-1": {"serial_and_batch_bundle": "SABB-GONE"}})
		_run(fake, _doc(rows=[_row()]))
		self.assertEqual(fake.writes, [])

	def test_overwrites_without_reading_the_batch_first(self):
		fake = _FakeFrappe(
			rows={"ROW-1": {"serial_and_batch_bundle": "SABB-IN"}},
			bundles={"SABB-IN": ("Inward", 1, ["B-1"])},
		)
		_run(fake, _doc(rows=[_row(certificate="CERT-NEW")]))
		self.assertEqual(fake.writes, [_stamp("B-1", "CERT-NEW")])
		self.assertFalse([call for call in fake.reads if call[1] == "Batch"])

	def test_regular_sales_invoice_returns_before_any_call(self):
		fake = _FakeFrappe(
			rows={"ROW-1": {"serial_and_batch_bundle": "SABB-OUT"}},
			bundles={"SABB-OUT": ("Outward", 1, ["B-1"])},
		)
		_run(fake, _doc("Sales Invoice", is_return=0, rows=[_row()]))
		self.assertEqual(fake.calls, [])
		self.assertEqual(fake.writes, [])

	def test_credit_note_stamps_its_inward_bundle(self):
		fake = _FakeFrappe(
			rows={"ROW-1": {"serial_and_batch_bundle": "SABB-RET"}},
			bundles={"SABB-RET": ("Inward", 1, ["B-1"])},
		)
		_run(fake, _doc("Sales Invoice", is_return=1, rows=[_row()]))
		self.assertEqual(fake.writes, [_stamp("B-1")])
		# Sales Invoice Item has no rejected bundle, so only the accepted field is read.
		self.assertIn(
			("get_value", SI_ITEM, "ROW-1", ("serial_and_batch_bundle",)), fake.calls
		)

	def test_no_batch_column_is_a_silent_no_op(self):
		fake = _FakeFrappe(
			rows={"ROW-1": {"serial_and_batch_bundle": "SABB-IN"}},
			bundles={"SABB-IN": ("Inward", 1, ["B-1"])},
			columns=ALL_COLUMNS - {("Batch", FIELD)},
		)
		_run(fake, _doc(rows=[_row()]))
		self.assertEqual(fake.reads, [])
		self.assertEqual(fake.writes, [])

	def test_no_item_table_column_is_a_silent_no_op(self):
		for doctype, is_return in (("Purchase Receipt", 0), ("Sales Invoice", 1)):
			with self.subTest(doctype=doctype):
				fake = _FakeFrappe(
					rows={"ROW-1": {"serial_and_batch_bundle": "SABB-IN"}},
					bundles={"SABB-IN": ("Inward", 1, ["B-1"])},
					columns=ALL_COLUMNS - {(CHILD_OF[doctype], FIELD)},
				)
				_run(fake, _doc(doctype, is_return=is_return, rows=[_row()]))
				self.assertEqual(fake.reads, [])
				self.assertEqual(fake.writes, [])

	def test_accepts_the_doc_event_calling_convention(self):
		"""Frappe calls a doc_event as ``fn(doc, method)``; a direct call passes only doc."""
		fake = _FakeFrappe()
		with patch.object(stock_transactions, "frappe", fake):
			set_batch_certificate_id(_doc(rows=[]))
			set_batch_certificate_id(_doc(rows=[]), "on_submit")
		self.assertEqual(fake.writes, [])


class TestCertificateHookWiring(IntegrationTestCase):
	"""The hook order is load-bearing: the stamp must run after T's own entries."""

	@classmethod
	def setUpClass(cls):
		pass

	def _doc_events(self):
		return frappe.get_hooks("doc_events", app_name="jewellery_erpnext")

	def test_purchase_receipt_on_submit_is_the_existing_entry_then_the_stamp(self):
		self.assertEqual(
			self._doc_events()["Purchase Receipt"]["on_submit"], [PR_T_ON_SUBMIT, HOOK]
		)

	def test_sales_invoice_on_submit_ends_with_the_stamp(self):
		self.assertEqual(
			self._doc_events()["Sales Invoice"]["on_submit"], [*SI_T_ON_SUBMIT, HOOK]
		)

	def test_stock_entry_hooks_do_not_stamp(self):
		for event, handlers in self._doc_events().get("Stock Entry", {}).items():
			with self.subTest(event=event):
				self.assertNotIn(HOOK, handlers)

	def test_wired_nowhere_else(self):
		wired = sorted(
			(doctype, event)
			for doctype, events in self._doc_events().items()
			for event, handlers in events.items()
			if HOOK in handlers
		)
		self.assertEqual(
			wired, [("Purchase Receipt", "on_submit"), ("Sales Invoice", "on_submit")]
		)

	def test_hook_path_resolves_to_the_function(self):
		self.assertIs(frappe.get_attr(HOOK), set_batch_certificate_id)


class _FakePatchFrappe:
	"""Just enough of ``frappe`` for the certificate-field patch."""

	def __init__(self, custom_fields=(), columns=(), docfields=()):
		self.custom_fields = set(custom_fields)
		self.columns = set(columns)
		self.docfields = set(docfields)
		self.log = MagicMock()
		self.db = SimpleNamespace(
			exists=self._exists,
			has_column=lambda doctype, column: (doctype, column) in self.columns,
			commit=MagicMock(),
		)

	def _exists(self, doctype, filters):
		if doctype == "Custom Field":
			return (filters["dt"], filters["fieldname"]) in self.custom_fields
		if doctype == "DocField":
			return (filters["parent"], filters["fieldname"]) in self.docfields
		raise AssertionError(f"unexpected exists() on {doctype}")

	def logger(self, *args, **kwargs):
		return self.log


def _definition(doctype):
	return [dict(df) for df in cert_patch.CUSTOM_FIELDS[doctype]]


class TestCertificateFieldPatch(IntegrationTestCase):
	"""Create only what is missing everywhere; never update; leave orphan columns alone."""

	@classmethod
	def setUpClass(cls):
		pass

	def _ensure(self, fake):
		creator = MagicMock()
		with (
			patch.object(cert_patch, "frappe", fake),
			patch.object(cert_patch, "create_custom_fields", creator),
			patch("builtins.print") as printed,
		):
			created = cert_patch.ensure_certificate_id_fields()
		return created, creator, printed

	def test_field_definitions(self):
		expected_anchor = {
			"Batch": "reference_name",
			PR_ITEM: "batch_no",
			SI_ITEM: "batch_no",
		}
		self.assertEqual(set(cert_patch.CUSTOM_FIELDS), set(expected_anchor))
		for doctype, anchor in expected_anchor.items():
			with self.subTest(doctype=doctype):
				(df,) = cert_patch.CUSTOM_FIELDS[doctype]
				self.assertEqual(df["fieldname"], FIELD)
				self.assertEqual(df["fieldtype"], "Data")
				self.assertEqual(df["label"], "Certificate ID")
				self.assertEqual(df["insert_after"], anchor)
				self.assertEqual(df["module"], "Jewellery Erpnext")
				# Not no_copy: a credit note made from an invoice carries the value.
				self.assertFalse(df.get("no_copy"))

	def test_fresh_site_creates_all_three_with_update_false(self):
		created, creator, _printed = self._ensure(_FakePatchFrappe())
		creator.assert_called_once_with(
			{doctype: _definition(doctype) for doctype in cert_patch.CUSTOM_FIELDS},
			ignore_validate=True,
			update=False,
		)
		self.assertEqual(
			created, [("Batch", FIELD), (PR_ITEM, FIELD), (SI_ITEM, FIELD)]
		)

	def test_creates_only_the_missing_pairs(self):
		fake = _FakePatchFrappe(
			# Batch: an existing (hand-made) Custom Field. PR Item: a standard column.
			custom_fields={("Batch", FIELD)},
			columns={("Batch", FIELD), (PR_ITEM, FIELD)},
			docfields={(PR_ITEM, FIELD)},
		)
		created, creator, printed = self._ensure(fake)
		creator.assert_called_once_with(
			{SI_ITEM: _definition(SI_ITEM)}, ignore_validate=True, update=False
		)
		self.assertEqual(created, [(SI_ITEM, FIELD)])
		fake.log.error.assert_not_called()
		self.assertNotIn("left as is", printed.call_args.args[0])

	def test_nothing_is_created_or_updated_when_all_three_exist(self):
		fake = _FakePatchFrappe(
			custom_fields={("Batch", FIELD), (PR_ITEM, FIELD), (SI_ITEM, FIELD)},
			columns=ALL_COLUMNS,
		)
		created, creator, _printed = self._ensure(fake)
		creator.assert_not_called()
		self.assertEqual(created, [])

	def test_custom_field_without_its_column_is_left_alone(self):
		fake = _FakePatchFrappe(custom_fields={("Batch", FIELD)})
		created, creator, _printed = self._ensure(fake)
		self.assertNotIn("Batch", creator.call_args.args[0])
		self.assertEqual(created, [(PR_ITEM, FIELD), (SI_ITEM, FIELD)])

	def test_orphan_column_is_logged_and_no_field_is_created(self):
		"""A column with no Custom Field and no DocField: log it, ship nothing for it."""
		fake = _FakePatchFrappe(columns={("Batch", FIELD)})
		created, creator, printed = self._ensure(fake)
		self.assertNotIn("Batch", creator.call_args.args[0])
		self.assertEqual(created, [(PR_ITEM, FIELD), (SI_ITEM, FIELD)])
		# print is the channel migrate output shows; the ERROR log line is the durable copy.
		orphan_message = printed.call_args_list[0].args[0]
		self.assertIn("left as is", orphan_message)
		self.assertIn(f"Batch.{FIELD}", orphan_message)
		fake.log.error.assert_called_once_with(orphan_message)

	def test_only_orphans_creates_nothing_but_logs_each(self):
		fake = _FakePatchFrappe(columns=ALL_COLUMNS)
		created, creator, printed = self._ensure(fake)
		creator.assert_not_called()
		self.assertEqual(created, [])
		printed.assert_called_once()
		message = printed.call_args.args[0]
		self.assertIn("left as is", message)
		for doctype in ("Batch", PR_ITEM, SI_ITEM):
			self.assertIn(f"{doctype}.{FIELD}", message)
		fake.log.error.assert_called_once_with(message)

	def test_execute_ensures_then_commits(self):
		fake = _FakePatchFrappe(
			custom_fields={("Batch", FIELD), (PR_ITEM, FIELD), (SI_ITEM, FIELD)}
		)
		with patch.object(cert_patch, "frappe", fake):
			cert_patch.execute()
		fake.db.commit.assert_called_once_with()


# --- DB-backed: the CI seed (create_test_data) ------------------------------------------

COMPANY = "Test_Company"
SUPPLIER = "Test_Supplier"
ITEM = "M-G-22KT-91.6-Y"
WAREHOUSE = "Central RM - T"
CERTIFICATE = "TEST-CERT-0001"


def _ci_seed_missing():
	"""Why this site cannot run the DB-backed cases, or None when it can."""
	for doctype, name in (
		("Company", COMPANY),
		("Supplier", SUPPLIER),
		("Item", ITEM),
		("Warehouse", WAREHOUSE),
	):
		if not frappe.db.exists(doctype, name):
			return (
				f"no {doctype} {name} (the create_test_data seed is not on this site)"
			)
	return None


class TestCertificateFieldsProvisioned(IntegrationTestCase):
	"""CI gets the fields only through create_test_data: the fixtures do not carry them."""

	def setUp(self):
		reason = _ci_seed_missing()
		if reason:
			self.skipTest(reason)

	def test_columns_and_custom_fields_exist(self):
		for doctype in cert_patch.CUSTOM_FIELDS:
			with self.subTest(doctype=doctype):
				self.assertTrue(frappe.db.has_column(doctype, FIELD))
				self.assertEqual(
					frappe.db.get_value(
						"Custom Field", {"dt": doctype, "fieldname": FIELD}, "fieldtype"
					),
					"Data",
				)


class TestCertificateFieldPatchOnDatabase(IntegrationTestCase):
	"""An existing definition survives a re-run byte for byte (update=False)."""

	SAVEPOINT = "batch_certificate_patch"

	def setUp(self):
		self.names = [
			frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": FIELD})
			for doctype in cert_patch.CUSTOM_FIELDS
		]
		if not all(self.names):
			self.skipTest("the three Certificate ID Custom Fields are not on this site")

		frappe.db.savepoint(self.SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=self.SAVEPOINT)
		no_commit = patch.object(frappe.db, "commit", lambda *args, **kwargs: None)
		no_commit.start()
		self.addCleanup(no_commit.stop)

	def _snapshot(self):
		return {
			name: frappe.db.get_value(
				"Custom Field",
				name,
				["label", "fieldtype", "insert_after", "no_copy", "modified"],
				as_dict=True,
			)
			for name in self.names
		}

	def test_existing_definitions_are_never_updated(self):
		for name in self.names:
			frappe.db.set_value(
				"Custom Field", name, "label", "Hand-made label", update_modified=False
			)
		before = self._snapshot()

		self.assertEqual(cert_patch.ensure_certificate_id_fields(), [])
		self.assertEqual(self._snapshot(), before)


class TestPurchaseReceiptSubmitStampsBatches(IntegrationTestCase):
	"""A real Purchase Receipt submit, end to end.

	The batch item has ``create_new_batch`` and the rows carry no batch, so v16 creates
	the bundle and the batch during stock-ledger posting and writes the bundle onto the
	row in the database only. A spy around the hook records what the database holds at
	the moment the hook runs.
	"""

	SAVEPOINT = "batch_certificate_pr"

	def setUp(self):
		reason = _ci_seed_missing()
		if reason:
			self.skipTest(reason)

		item = frappe.db.get_value(
			"Item",
			ITEM,
			["has_batch_no", "create_new_batch", "has_serial_no"],
			as_dict=True,
		)
		if not (item.has_batch_no and item.create_new_batch) or item.has_serial_no:
			self.skipTest(f"{ITEM} is not a batch-only item with create_new_batch")

		for doctype in ("Batch", PR_ITEM):
			if not frappe.db.has_column(doctype, FIELD):
				self.fail(f"{doctype}.{FIELD} is not provisioned; see create_test_data")

		frappe.db.savepoint(self.SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=self.SAVEPOINT)
		no_commit = patch.object(frappe.db, "commit", lambda *args, **kwargs: None)
		no_commit.start()
		self.addCleanup(no_commit.stop)

		self.seen = []
		real = stock_transactions.set_batch_certificate_id

		def spy(doc, method=None):
			if doc.doctype == "Purchase Receipt":
				self.seen.append(self._bundles_in_database(doc))
			return real(doc, method)

		spy_patch = patch.object(stock_transactions, "set_batch_certificate_id", spy)
		spy_patch.start()
		self.addCleanup(spy_patch.stop)

	@staticmethod
	def _bundles_in_database(doc):
		state = {}
		for row in doc.get("items"):
			stored = frappe.db.get_value(
				PR_ITEM,
				row.name,
				["serial_and_batch_bundle", "rejected_serial_and_batch_bundle"],
				as_dict=True,
			)
			state[row.name] = {
				field: (
					bundle,
					*frappe.db.get_value(
						"Serial and Batch Bundle",
						bundle,
						["type_of_transaction", "docstatus"],
					),
				)
				for field, bundle in stored.items()
				if bundle
			}
		return state

	@staticmethod
	def _batches(bundle):
		return frappe.get_all(
			"Serial and Batch Entry", filters={"parent": bundle}, pluck="batch_no"
		)

	def _submit_receipt(self, rows):
		meta = frappe.get_meta("Purchase Receipt")
		receipt = frappe.new_doc("Purchase Receipt")
		receipt.company = COMPANY
		receipt.supplier = SUPPLIER
		receipt.set_warehouse = WAREHOUSE
		if meta.has_field("custom_supplier_challan_no"):
			receipt.custom_supplier_challan_no = "TEST-CERT-CHALLAN"
		if not meta.has_field("purchase_type"):
			# The jewellery before_validate hook reads doc.purchase_type, a field the
			# CI fixtures do not define. A plain attribute keeps it on its default path.
			receipt.purchase_type = None

		branch = frappe.db.exists("Branch", {"branch_name": "Test Branch"})
		if branch and meta.has_field("branch"):
			receipt.branch = branch

		for row in rows:
			values = {
				"item_code": ITEM,
				"rate": 100,
				"warehouse": WAREHOUSE,
				**row,
			}
			if branch and frappe.get_meta(PR_ITEM).has_field("branch"):
				values["branch"] = branch
			receipt.append("items", values)

		receipt.insert(ignore_permissions=True)
		receipt.submit()
		return receipt

	def test_submit_stamps_the_batch_minted_during_ledger_posting(self):
		receipt = self._submit_receipt([{"qty": 2, FIELD: CERTIFICATE}, {"qty": 1}])
		certified, plain = receipt.items

		bundle = frappe.db.get_value(PR_ITEM, certified.name, "serial_and_batch_bundle")
		self.assertTrue(bundle)
		self.assertEqual(
			len(self.seen), 1, "the on_submit hook did not run exactly once"
		)
		self.assertEqual(
			self.seen[0][certified.name]["serial_and_batch_bundle"],
			(bundle, "Inward", 1),
			"at on_submit time the row's bundle must be in the database, submitted",
		)

		batches = self._batches(bundle)
		self.assertTrue(batches)
		for batch_no in batches:
			self.assertEqual(frappe.db.get_value("Batch", batch_no, FIELD), CERTIFICATE)

		# The row without a certificate leaves its batch untouched.
		plain_bundle = frappe.db.get_value(
			PR_ITEM, plain.name, "serial_and_batch_bundle"
		)
		for batch_no in self._batches(plain_bundle):
			self.assertFalse(frappe.db.get_value("Batch", batch_no, FIELD))

	def test_rejected_qty_batch_is_stamped_too(self):
		rejected_warehouse = next(
			(
				row.name
				for row in frappe.get_all(
					"Warehouse",
					filters={"company": COMPANY, "is_group": 0, "disabled": 0},
					fields=["name", "warehouse_type"],
					order_by="name asc",
				)
				if row.name != WAREHOUSE and row.warehouse_type != "Transit"
			),
			None,
		)
		if not rejected_warehouse:
			self.skipTest(f"no second warehouse on {COMPANY} for rejected qty")

		receipt = self._submit_receipt(
			[
				{
					"qty": 1,
					"rejected_qty": 1,
					"received_qty": 2,
					"rejected_warehouse": rejected_warehouse,
					FIELD: CERTIFICATE,
				}
			]
		)
		(row,) = receipt.items
		stored = frappe.db.get_value(
			PR_ITEM,
			row.name,
			["serial_and_batch_bundle", "rejected_serial_and_batch_bundle"],
			as_dict=True,
		)
		self.assertTrue(stored.serial_and_batch_bundle)
		self.assertTrue(stored.rejected_serial_and_batch_bundle)

		for bundle in stored.values():
			batches = self._batches(bundle)
			self.assertTrue(batches)
			for batch_no in batches:
				self.assertEqual(
					frappe.db.get_value("Batch", batch_no, FIELD), CERTIFICATE
				)
