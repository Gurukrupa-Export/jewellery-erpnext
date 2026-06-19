# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the per-key conflict serializer (serialize)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext import serialize
from jewellery_erpnext.jewellery_erpnext.serialize import (
	LockTimeoutError,
	conflict_key,
	conflict_lock,
)


class TestConflictKey(FrappeTestCase):
	def test_prefix_and_join(self):
		self.assertEqual(conflict_key("bin", "ITEM", "WH"), "jewl_conflict_bin__ITEM__WH")

	def test_skips_none_and_empty_parts(self):
		self.assertEqual(conflict_key("bin", None, "", "WH"), "jewl_conflict_bin__WH")

	def test_is_filesystem_safe(self):
		key = conflict_key("a/b", "c:d", "e f", "g\\h")
		for bad in '/\\: ':
			self.assertNotIn(bad, key)

	def test_is_length_bounded(self):
		self.assertLessEqual(len(conflict_key("x" * 500)), 200)

	def test_same_parts_same_key_stable(self):
		self.assertEqual(conflict_key("se", "MR-1"), conflict_key("se", "MR-1"))


class TestConflictLock(FrappeTestCase):
	def test_acquires_filelock_with_derived_name_and_timeout(self):
		@contextmanager
		def _fake(name, timeout=30):
			_fake.called_with = (name, timeout)
			yield

		with patch.object(serialize, "filelock", _fake):
			with conflict_lock("bin", "ITEM", "WH", timeout=15):
				pass
		self.assertEqual(
			_fake.called_with, ("jewl_conflict_bin__ITEM__WH", 15)
		)

	def test_propagates_lock_timeout(self):
		def _raises(name, timeout=30):
			raise LockTimeoutError("held")

		with patch.object(serialize, "filelock", _raises):
			with self.assertRaises(LockTimeoutError):
				with conflict_lock("k"):
					pass

	def test_lock_timeout_error_is_reexported(self):
		# Callers import LockTimeoutError from serialize; ensure it is the real class.
		from frappe.utils.file_lock import LockTimeoutError as RealLTE

		self.assertIs(LockTimeoutError, RealLTE)
