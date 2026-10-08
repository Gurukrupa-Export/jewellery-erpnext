# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""``batch_rename.create_child_batches`` against a batch table that remembers.

``test_conversion_lane_downstream`` pins which lane mints what, with every read
stubbed to "nothing exists". These tests give the allocator a stateful table so
existing children, full pools and colliding submits are visible to it -- the shapes
behind EMP-IR-Labh-2026-14111 (2026-09-29), where a 0.01 g process loss on
``GJCU0009-2F09-M-G-22KT-91.75-Y-12-A`` failed with "already uses all 26 child
suffixes" although that parent had no children.

Three layers:

* ``TestCreateChildBatchesNamespace`` -- an in-memory ``tabBatch`` answering the
  allocator's SQL the way MariaDB would (case-insensitive LIKE with backslash
  escapes, ``ORDER BY name DESC``), including "hidden" names that a stale
  REPEATABLE READ snapshot cannot see but the primary key still rejects.
* ``TestCreateChildBatchesRealTable`` -- real ``Batch`` inserts inside the class
  transaction (rolled back, never committed).
* ``TestCreateChildBatchesStaleSnapshot`` -- two real connections; disposable
  sites only.

Only the module object is imported, so this file still collects on code that
predates the helpers.
"""

import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename

CUSTOMER = "GJCU0009"
ITEM_22KT = "M-G-22KT-91.75-Y"
LOSS_ITEM = "ML-G-22KT-91.75-Y"
PARENT_12_A = "GJCU0009-2F09-M-G-22KT-91.75-Y-12-A"
COLLAPSED_POOL = "GJCU0009-2F09-ML-G-22KT-91.75-Y-A"
PROCESS_LOSS = "Process Loss"


def _like(name, pattern):
	"""MariaDB LIKE under utf8mb4_unicode_ci: case-insensitive, backslash escapes."""
	regex = []
	i = 0
	while i < len(pattern):
		char = pattern[i]
		if char == "\\" and i + 1 < len(pattern):
			regex.append(re.escape(pattern[i + 1]))
			i += 2
			continue
		regex.append(".*" if char == "%" else "." if char == "_" else re.escape(char))
		i += 1
	return (
		re.fullmatch("".join(regex), name, flags=re.IGNORECASE | re.DOTALL) is not None
	)


class _BatchTable:
	"""The slice of ``tabBatch`` the allocator reads and writes."""

	def __init__(self, names=(), hidden=()):
		self.names = set(names)
		# Committed by another submit after this transaction's snapshot: invisible to
		# plain reads, still rejected by the primary key.
		self.hidden = set(hidden)
		self.statements = []
		self.minted = []
		self.attempts = 0
		# InnoDB with innodb_snapshot_isolation on reports such a clash as a deadlock.
		self.deadlock = False

	def taken(self, name):
		folded = name.casefold()
		return any(n.casefold() == folded for n in self.names | self.hidden)

	def sql(self, query, values=None, *args, **kwargs):
		text = " ".join(str(query).split())
		lowered = text.lower()
		self.statements.append(lowered)
		if lowered.startswith(
			("savepoint", "rollback to savepoint", "release savepoint")
		):
			return []
		if "for update" in lowered:
			wanted = (values or {}).get("names") or ()
			return [(n,) for n in sorted(wanted) if n in self.names]
		if " like " in lowered:
			pattern = values[0] if isinstance(values, list | tuple) else values
			matched = [n for n in self.names if _like(n, pattern)]
			if "order by name desc" in lowered:
				matched.sort(key=str.casefold, reverse=True)
			if "limit 1" in lowered:
				matched = matched[:1]
			if kwargs.get("pluck"):
				return matched
			if kwargs.get("as_dict"):
				return [frappe._dict(name=n) for n in matched]
			return [(n,) for n in matched]
		return []

	def exists(self, doctype, name=None, *args, **kwargs):
		if doctype == "Batch" and isinstance(name, str):
			folded = name.casefold()
			return next((n for n in self.names if n.casefold() == folded), None)
		return None


class _FakeBatch:
	def __init__(self, table):
		self._table = table
		self.batch_id = None
		self.item = None
		self.custom_customer = None
		self.custom_inventory_type = None
		self.custom_metal_rate = 0.0
		self.custom_voucher_detail_no = None
		self.reference_doctype = None
		self.reference_name = None
		self.custom_customer_voucher_type = None

	def insert(self, ignore_permissions=False):
		self._table.attempts += 1
		if self._table.deadlock:
			raise frappe.QueryDeadlockError("1213")
		if self._table.taken(self.batch_id):
			# What BaseDocument.db_insert does on a primary-key clash.
			frappe.msgprint(f"Batch {self.batch_id} already exists")
			raise frappe.DuplicateEntryError("Batch", self.batch_id, None)
		self._table.names.add(self.batch_id)
		self._table.minted.append(self)
		return self


def _row(name, **fields):
	values = {
		"name": name,
		"item_code": ITEM_22KT,
		"s_warehouse": None,
		"t_warehouse": None,
		"batch_no": None,
		"inventory_type": "Customer Goods",
		"customer": CUSTOMER,
		"basic_rate": 100.0,
		"custom_metal_rate": 0.0,
		"qty": 0.01,
	}
	values.update(fields)
	row = SimpleNamespace(**values)
	row.get = lambda key, default=None: getattr(row, key, default)
	return row


def _consume(name, batch_no, item_code=ITEM_22KT, **fields):
	return _row(
		name,
		item_code=item_code,
		s_warehouse="Casting WIP WH 1 - KGJPL",
		batch_no=batch_no,
		**fields,
	)


def _produce(name, item_code=LOSS_ITEM, **fields):
	return _row(name, item_code=item_code, t_warehouse="Waxing Scrap - KGJPL", **fields)


def _entry(items, stock_entry_type=PROCESS_LOSS, customer=None, name="MAT-STE-TEST-1"):
	return SimpleNamespace(
		doctype="Stock Entry",
		name=name,
		items=items,
		_customer=customer,
		stock_entry_type=stock_entry_type,
		purpose="Repack",
	)


class TestCreateChildBatchesNamespace(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, table):
		with (
			patch("frappe.new_doc", side_effect=lambda doctype: _FakeBatch(table)),
			patch("frappe.db.sql", side_effect=table.sql),
			patch("frappe.db.exists", side_effect=table.exists),
		):
			batch_rename.create_child_batches(doc)
		return table.minted

	@staticmethod
	def _full_collapsed_pool():
		return {f"{COLLAPSED_POOL}-{chr(c)}" for c in range(ord("A"), ord("Z") + 1)}

	def test_incident_shape_mints_under_the_parents_own_path(self):
		"""EMP-IR-Labh-2026-14111: the shared '...-Y-A' pool is full, -12-A has no child."""
		table = _BatchTable({PARENT_12_A, *self._full_collapsed_pool()})
		produce = _produce("d7ff3r2j6g")
		doc = _entry([_consume("c1", PARENT_12_A), produce])

		minted = self._run(doc, table)

		self.assertEqual(produce.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-A")
		self.assertEqual(len(minted), 1)
		self.assertEqual(minted[0].custom_customer, CUSTOMER)
		self.assertEqual(minted[0].custom_inventory_type, "Customer Goods")
		self.assertEqual(minted[0].reference_name, doc.name)
		self.assertEqual(minted[0].custom_voucher_detail_no, "d7ff3r2j6g")

	def test_a_full_legacy_pool_continues_past_z(self):
		"""A parent whose name cannot be parsed keeps the legacy base -- and no longer stops at Z."""
		legacy_parent = "GJCU0009-2F09-OLD-FORMAT-A"
		table = _BatchTable({legacy_parent, *self._full_collapsed_pool()})
		produce = _produce("p1")
		self._run(_entry([_consume("c1", legacy_parent), produce]), table)
		self.assertEqual(produce.batch_no, f"{COLLAPSED_POOL}-AA")

	def test_two_losses_in_one_entry_get_distinct_names(self):
		parent = "GJCU0009-2F09-M-G-22KT-91.75-Y-10-A"
		first, second = _produce("p1"), _produce("p2")
		table = _BatchTable({parent})
		self._run(
			_entry([_consume("c1", parent), first, _consume("c2", parent), second]),
			table,
		)
		self.assertEqual(first.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-10-A-A")
		self.assertEqual(second.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-10-A-B")

	def test_sequential_entries_advance_the_suffix(self):
		table = _BatchTable({PARENT_12_A})
		first, second = _produce("p1"), _produce("p2")
		self._run(_entry([_consume("c1", PARENT_12_A), first], name="SE-1"), table)
		self._run(_entry([_consume("c2", PARENT_12_A), second], name="SE-2"), table)
		self.assertEqual(first.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-A")
		self.assertEqual(second.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-B")

	def test_each_loss_is_named_from_its_own_source(self):
		"""MAT-STE-17857: the loss drawn from -04 was named as a child of -03-A."""
		from_03_a, from_04 = _produce("p1"), _produce("p2")
		table = _BatchTable()
		doc = _entry(
			[
				_consume("c1", "GJCU0009-2F09-M-G-22KT-91.75-Y-03-A", qty=0.011),
				from_03_a,
				_consume("c2", "GJCU0009-2F09-M-G-22KT-91.75-Y-04", qty=0.004),
				from_04,
			]
		)
		self._run(doc, table)
		self.assertEqual(from_03_a.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-03-A-A")
		self.assertEqual(from_04.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-04-A")

	def test_a_multi_source_run_takes_the_first_source_of_its_own_run(self):
		"""Warehouse/tree groups: several consume rows, then the produce row."""
		first_run, second_run = _produce("p1"), _produce("p2")
		doc = _entry(
			[
				_consume("c1", "GJCU0009-2F09-M-G-22KT-91.75-Y-03-A"),
				first_run,
				_consume("c2", "GJCU0009-2F09-M-G-22KT-91.75-Y-05-A"),
				_consume("c3", "GJCU0009-2F09-M-G-22KT-91.75-Y-07-A"),
				second_run,
			]
		)
		self._run(doc, _BatchTable())
		self.assertEqual(first_run.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-03-A-A")
		self.assertEqual(second_run.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-05-A-A")

	def test_a_run_behind_a_regular_produce_row_still_pairs(self):
		"""Owner-split produce rows: [C_reg, C_cust, P_reg, P_cust]."""
		regular = _produce("p1", inventory_type="Regular Stock", customer=None)
		customer_scrap = _produce("p2")
		doc = _entry(
			[
				_consume(
					"c1",
					"KG2F093-MGL229175Y0-604EO",
					inventory_type="Regular Stock",
					customer=None,
				),
				_consume("c2", "GJCU0009-2F09-M-G-22KT-91.75-Y-08-A"),
				regular,
				customer_scrap,
			]
		)
		self._run(doc, _BatchTable())
		self.assertIsNone(regular.batch_no)
		self.assertEqual(
			customer_scrap.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-08-A-A"
		)

	def test_other_entry_types_keep_the_lane_first_parent(self):
		"""Pairing is Process Loss only; a conversion's output keeps today's parent rule."""
		first, second = (
			_produce("p1", item_code="M-G-18KT-75.0-Y"),
			_produce("p2", item_code="M-G-18KT-75.0-Y"),
		)
		doc = _entry(
			[
				_consume(
					"c1",
					"GJCU0009-2F09-M-G-24KT-99.9-Y-12",
					item_code="M-G-24KT-99.9-Y",
				),
				first,
				_consume(
					"c2",
					"GJCU0009-2F09-M-G-24KT-99.9-Y-13",
					item_code="M-G-24KT-99.9-Y",
				),
				second,
			],
			stock_entry_type="Repack-Metal Conversion",
			customer=CUSTOMER,
		)
		self._run(doc, _BatchTable())
		self.assertEqual(first.batch_no, "GJCU0009-2F09-M-G-18KT-75.0-Y-12-A")
		self.assertEqual(second.batch_no, "GJCU0009-2F09-M-G-18KT-75.0-Y-12-B")

	def test_a_finished_piece_from_converted_metal_carries_the_full_path(self):
		"""Customer FG batches get the longer name too -- announced in the release note."""
		fg = _produce("p1", item_code="RI00210-004", is_finished_item=1)
		doc = _entry(
			[_consume("c1", "GJCU0009-2F09-M-G-22KT-91.75-Y-03-A"), fg],
			stock_entry_type="Manufacture",
			customer=CUSTOMER,
		)
		self._run(doc, _BatchTable())
		self.assertEqual(fg.batch_no, "GJCU0009-2F09-RI00210-004-03-A-A")

	def test_no_batch_row_is_locked(self):
		"""The primary key already serialises two inserts of one name.

		An early lock on the parent would not refresh the snapshot; it would only hold a
		Batch row across the ledger posting, against transfers that lock batches in
		another order.
		"""
		table = _BatchTable(
			{
				"GJCU0009-2F09-M-G-22KT-91.75-Y-10-A",
				"GJCU0009-2F09-M-G-22KT-91.75-Y-03-A",
			}
		)
		doc = _entry(
			[
				_consume("c1", "GJCU0009-2F09-M-G-22KT-91.75-Y-10-A"),
				_produce("p1"),
				_consume("c2", "GJCU0009-2F09-M-G-22KT-91.75-Y-03-A"),
				_produce("p2"),
			]
		)
		self._run(doc, table)
		self.assertEqual(len(table.minted), 2)
		self.assertFalse(
			[
				s
				for s in table.statements
				if "for update" in s or "lock in share mode" in s
			]
		)

	def test_a_deadlock_is_not_retried(self):
		"""With snapshot isolation on, a clash is a deadlock and InnoDB has rolled the whole
		transaction back: retrying inside it would be wrong, so it must propagate."""
		table = _BatchTable({PARENT_12_A})
		table.deadlock = True
		with self.assertRaises(frappe.QueryDeadlockError):
			self._run(_entry([_consume("c1", PARENT_12_A), _produce("p1")]), table)
		self.assertEqual(table.attempts, 1)
		self.assertFalse(
			any(s.startswith("rollback to savepoint") for s in table.statements)
		)

	def test_a_stale_snapshot_collision_takes_the_next_name(self):
		"""Another submit committed '-A' after this one's snapshot: retry, don't fail."""
		taken = "GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-A"
		table = _BatchTable({PARENT_12_A}, hidden={taken})
		produce = _produce("p1")
		before = list(frappe.local.message_log)

		self._run(_entry([_consume("c1", PARENT_12_A), produce]), table)

		self.assertEqual(produce.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-B")
		self.assertEqual([b.batch_id for b in table.minted], [produce.batch_no])
		self.assertEqual(frappe.local.message_log, before)
		self.assertTrue(
			any(s.startswith("rollback to savepoint") for s in table.statements)
		)
		self.assertTrue(
			any(s.startswith("release savepoint") for s in table.statements)
		)

	def test_repeated_collisions_end_in_an_actionable_error(self):
		base = "GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A"
		hidden = {
			f"{base}-{batch_rename.encode_child_suffix(n)}"
			for n in range(1, batch_rename._CHILD_BATCH_ATTEMPTS + 10)
		}
		table = _BatchTable({PARENT_12_A}, hidden=hidden)
		with self.assertRaises(frappe.ValidationError) as caught:
			self._run(_entry([_consume("c1", PARENT_12_A), _produce("p1")]), table)
		message = str(caught.exception)
		self.assertIn("MAT-STE-TEST-1", message)
		self.assertIn(PARENT_12_A, message)
		self.assertNotIn("26", message)
		self.assertEqual(table.minted, [])
		self.assertEqual(table.attempts, batch_rename._CHILD_BATCH_ATTEMPTS)

	def test_an_overlong_name_is_refused_before_insert(self):
		long_item = "ML-" + "X" * 130
		table = _BatchTable({PARENT_12_A})
		with self.assertRaises(frappe.ValidationError):
			self._run(
				_entry(
					[_consume("c1", PARENT_12_A), _produce("p1", item_code=long_item)]
				),
				table,
			)
		self.assertEqual(table.minted, [])

	def test_no_customer_anywhere_still_mints_nothing(self):
		regular = _produce("p1", inventory_type="Regular Stock", customer=None)
		doc = _entry(
			[
				_consume(
					"c1",
					"KG2F093-MGL229175Y0-604EO",
					inventory_type="Regular Stock",
					customer=None,
				),
				regular,
			]
		)
		table = _BatchTable()
		self._run(doc, table)
		self.assertIsNone(regular.batch_no)
		self.assertEqual(table.statements, [])


def _isolation():
	"""``(isolation level, innodb_snapshot_isolation on?)`` of the current connection."""
	values = dict(
		frappe.db.sql(
			"""SHOW SESSION VARIABLES WHERE Variable_name IN
			('tx_isolation', 'transaction_isolation', 'innodb_snapshot_isolation')"""
		)
	)
	isolation = values.get("transaction_isolation") or values.get("tx_isolation")
	snapshot = str(values.get("innodb_snapshot_isolation", "OFF")).upper() in (
		"ON",
		"1",
	)
	return isolation, snapshot


def _raw_item(item_code):
	frappe.db.sql(
		"""INSERT INTO `tabItem` (name, item_code, item_name, item_group, stock_uom,
			has_batch_no, is_stock_item, creation, modified, owner, modified_by)
		VALUES (%s, %s, %s, 'All Item Groups', 'Nos', 1, 1, NOW(), NOW(),
			'Administrator', 'Administrator')""",
		(item_code, item_code, item_code),
	)


def _raw_customer(name):
	frappe.db.sql(
		"""INSERT INTO `tabCustomer` (name, customer_name, creation, modified, owner, modified_by)
		VALUES (%s, %s, NOW(), NOW(), 'Administrator', 'Administrator')""",
		(name, name),
	)


def _raw_batch(name, item_code):
	frappe.db.sql(
		"""INSERT INTO `tabBatch` (name, batch_id, item, creation, modified, owner, modified_by)
		VALUES (%s, %s, %s, NOW(), NOW(), 'Administrator', 'Administrator')""",
		(name, name, item_code),
	)


class _RealTableCase(IntegrationTestCase):
	"""Real ``Batch`` inserts on the site, inside the class transaction.

	Masters are raw rows named from a per-run token so the class depends on neither
	``create_test_data`` nor the CG-TEST fixtures, and nothing survives the class
	rollback. The Stock Entry is a stand-in with ``name=None``: the minted Batch's
	Dynamic Link then has nothing to resolve.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.token = "ZZCB" + frappe.generate_hash(length=6).upper()
		cls.customer = f"{cls.token}-CUST"
		cls.parent_item = f"{cls.token}-M-G-22KT"
		cls.loss_item = f"{cls.token}-ML-G-22KT"
		_raw_customer(cls.customer)
		_raw_item(cls.parent_item)
		_raw_item(cls.loss_item)
		# create_test_data seeds it on CI; a bare site may not have it.
		if not frappe.db.exists("Inventory Type", "Customer Goods"):
			frappe.get_doc(
				{"doctype": "Inventory Type", "inventory_type": "Customer Goods"}
			).insert(ignore_permissions=True)

	def _parent(self, serial_path):
		"""A fresh parent batch per test: the tests share one class transaction."""
		parent = f"{self.customer}-2F09-{self.parent_item}-{serial_path}"
		_raw_batch(parent, self.parent_item)
		return parent, f"{self.customer}-2F09-{self.loss_item}-{serial_path}"

	def _loss_entry(self, parent):
		produce = _produce("p1", item_code=self.loss_item, customer=self.customer)
		doc = _entry(
			[
				_consume(
					"c1", parent, item_code=self.parent_item, customer=self.customer
				),
				produce,
			],
			name=None,
		)
		return doc, produce


class TestCreateChildBatchesRealTable(_RealTableCase):
	def test_the_27th_child_is_aa(self):
		parent, base = self._parent("12-A")
		for c in range(ord("A"), ord("Z") + 1):
			_raw_batch(f"{base}-{chr(c)}", self.loss_item)
		doc, produce = self._loss_entry(parent)

		batch_rename.create_child_batches(doc)

		self.assertEqual(produce.batch_no, f"{base}-AA")
		self.assertEqual(
			frappe.db.get_value("Batch", produce.batch_no, "custom_customer"),
			self.customer,
		)

	def test_a_collation_equal_name_takes_the_retry_path(self):
		"""'-Ä' is not a letter suffix to the parser, but the case/accent-insensitive key rejects '-A'."""
		parent, base = self._parent("13-A")
		_raw_batch(f"{base}-Ä", self.loss_item)
		doc, produce = self._loss_entry(parent)
		before = list(frappe.local.message_log)

		batch_rename.create_child_batches(doc)

		self.assertEqual(produce.batch_no, f"{base}-B")
		self.assertEqual(frappe.local.message_log, before)


class TestCreateChildBatchesStaleSnapshot(_RealTableCase):
	"""A child committed by another connection after this transaction's snapshot.

	Commits on a second connection, so it runs only on a site flagged
	``customer_gold_disposable_site``. The old allocator read the stale snapshot,
	chose the same name and failed the whole submit with DuplicateEntryError. Needs
	REPEATABLE READ; with innodb_snapshot_isolation on, the clash is a deadlock that
	must propagate rather than be retried inside a rolled-back transaction.
	"""

	@classmethod
	def setUpClass(cls):
		if not frappe.conf.get("customer_gold_disposable_site"):
			raise unittest.SkipTest(
				"commits on a second connection: disposable sites only"
			)
		super().setUpClass()

	def test_a_child_committed_after_the_snapshot_is_skipped(self):
		parent, base = self._parent("14-A")
		taken = f"{base}-A"

		def _remove_committed_child():
			self._secondary_connection.sql(
				"DELETE FROM `tabBatch` WHERE name = %s", (taken,)
			)
			self._secondary_connection.commit()

		# Cleanups run LIFO: secondary_connection() registers its own rollback when the
		# block exits, which releases this transaction's lock on the clashing row before
		# the committed row is deleted and the primary connection restored.
		self.addCleanup(setattr, frappe.local, "db", self._primary_connection)
		self.addCleanup(_remove_committed_child)

		with self.primary_connection():
			isolation, snapshot_isolation = _isolation()
			if isolation != "REPEATABLE-READ":
				self.skipTest(f"needs REPEATABLE READ; this server runs {isolation}")
			frappe.db.sql("SELECT name FROM `tabBatch` LIMIT 1")  # fixes the read view

		with self.secondary_connection():
			frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 5")
			frappe.db.sql(
				"""INSERT INTO `tabBatch` (name, batch_id, creation, modified, owner, modified_by)
				VALUES (%s, %s, NOW(), NOW(), 'Administrator', 'Administrator')""",
				(taken, taken),
			)
			frappe.db.commit()

		frappe.local.db = self._primary_connection
		doc, produce = self._loss_entry(parent)
		with (
			self.primary_connection(),
			patch.object(frappe.db, "rollback", wraps=frappe.db.rollback) as rollback,
		):
			if snapshot_isolation:
				# MariaDB >= 11.6.2 default: the clash is ER_CHECKREAD, reported as a
				# deadlock; the submit fails whole and is simply resubmitted.
				with self.assertRaises(frappe.QueryDeadlockError):
					batch_rename.create_child_batches(doc)
				return
			batch_rename.create_child_batches(doc)

		self.assertEqual(produce.batch_no, f"{base}-B")
		# The name was taken by a clash and a savepoint rollback, not by a fresh read.
		self.assertTrue(
			any(c.kwargs.get("save_point") for c in rollback.call_args_list)
		)
