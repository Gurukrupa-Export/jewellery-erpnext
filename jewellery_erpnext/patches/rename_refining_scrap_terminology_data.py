"""SWAP the stored refining vocabulary. Run once, and only once, per site.

The business calls the department sweep "scrap" and calls material returned unused from
production "unused/loose material". The app already used "Scrap Refining" for the latter,
so this is a SWAP, not a rename:

    Refining Entry.refining_type       Dust Refining -> Scrap Refining
                                       Scrap Refining -> Unused/Loose Material Refining
    Refining Material Line.source_type Dust -> Scrap
                                       Scrap -> Unused/Loose Material
    Batch.custom_batch_type            Scrap -> Unused/Loose Material

Each table is done in ONE ``CASE WHEN`` statement. MariaDB evaluates the right-hand side
against each row's pre-update value, so a single statement swaps both populations with no
collapse and no intermediate state holding a sentinel value that would fail Select
validation on the next save. Raw SQL bypasses validation, hooks and ``docstatus``, and
deliberately does not bump ``modified`` — three statements instead of ~16,000 document
saves, and the audit trail is left intact.

Naming series are NOT touched. Documents created before the rename keep their ``RFN-DST-``
/ ``RFN-SCP-`` names, because document names are immutable and re-lettering the series
would give each type two historical prefixes.

**Not idempotent — running it twice swaps back.** Three independent fences:

1. Patch Log, which ``bench migrate`` honours.
2. A durable ``tabDefaultValue`` sentinel, because every patch in this app documents an
   ad-hoc ``bench execute`` path that Patch Log does not cover. Read and written with
   raw SQL so a rolled-back dry run cannot leave a redis cache asserting "applied".
3. A fail-closed pre-flight: none of the post-rename values can exist before the swap, so
   finding one while the sentinel is unset proves a partial manual run. Refuse to swap.

ROLLBACK (code revert plus)::

    UPDATE `tabRefining Entry` SET refining_type = CASE refining_type
        WHEN 'Scrap Refining' THEN 'Dust Refining'
        WHEN 'Unused/Loose Material Refining' THEN 'Scrap Refining'
        ELSE refining_type END
     WHERE refining_type IN ('Scrap Refining', 'Unused/Loose Material Refining');
    UPDATE `tabRefining Material Line` SET source_type = CASE source_type
        WHEN 'Scrap' THEN 'Dust'
        WHEN 'Unused/Loose Material' THEN 'Scrap'
        ELSE source_type END
     WHERE source_type IN ('Scrap', 'Unused/Loose Material');
    UPDATE `tabBatch` SET custom_batch_type = 'Scrap'
     WHERE custom_batch_type = 'Unused/Loose Material';

  then ``delete from tabDefaultValue where defkey = 'refining_terminology_swap_v1'``
  and ``frappe.clear_cache()``.
"""

import frappe
from frappe.utils import cint

SENTINEL = "refining_terminology_swap_v1"


def _sentinel_is_set():
	"""Read the fence straight from ``tabDefaultValue``, NOT via frappe.db.get_default.

	get_default answers from a redis cache that a transaction rollback cannot undo, so a
	rolled-back dry run of this patch would leave the cache asserting "already applied"
	and the real migrate would silently skip the swap. Raw SQL is transaction-truthful.
	"""
	row = frappe.db.sql(
		"select defvalue from tabDefaultValue where defkey = %s and parent = %s",
		(SENTINEL, "__default"),
	)
	return bool(row) and cint(row[0][0])


def _set_sentinel():
	frappe.db.sql(
		"""
		insert into tabDefaultValue (name, parent, parenttype, defkey, defvalue)
		values (%(name)s, '__default', '__default', %(key)s, '1')
		on duplicate key update defvalue = '1'
		""",
		{"name": f"__default-{SENTINEL}", "key": SENTINEL},
	)
	frappe.cache.delete_value("__default")


def _clear_sentinel():
	"""Drop the fence. Only for tests and for the documented rollback recipe."""
	frappe.db.sql("delete from tabDefaultValue where defkey = %s", (SENTINEL,))
	frappe.cache.delete_value("__default")


def execute():
	if _sentinel_is_set():
		frappe.logger().info(
			"rename_refining_scrap_terminology_data: sentinel set, already applied — skipping"
		)
		return

	has_batch_type = frappe.db.has_column("Batch", "custom_batch_type")

	already = (
		frappe.db.exists(
			"Refining Entry", {"refining_type": "Unused/Loose Material Refining"}
		)
		or frappe.db.exists(
			"Refining Material Line", {"source_type": "Unused/Loose Material"}
		)
		or (
			has_batch_type
			and frappe.db.exists(
				"Batch", {"custom_batch_type": "Unused/Loose Material"}
			)
		)
	)
	if already:
		# Post-rename values cannot pre-exist, so this is a partial/manual application.
		# Fail CLOSED and mark the sentinel: swapping now would move the already-renamed
		# population a second time.
		frappe.logger().error(
			"rename_refining_scrap_terminology_data: post-rename values already present "
			"but sentinel unset — refusing to swap, investigate before clearing the sentinel"
		)
		_set_sentinel()
		return

	# No commit between the statements: the patch runner commits once on success and rolls
	# the whole thing back on exception, so the three tables move together.
	frappe.db.sql(
		"""
		UPDATE `tabRefining Entry`
		   SET refining_type = CASE refining_type
				 WHEN 'Dust Refining'  THEN 'Scrap Refining'
				 WHEN 'Scrap Refining' THEN 'Unused/Loose Material Refining'
				 ELSE refining_type END
		 WHERE refining_type IN ('Dust Refining', 'Scrap Refining')
		"""
	)
	frappe.db.sql(
		"""
		UPDATE `tabRefining Material Line`
		   SET source_type = CASE source_type
				 WHEN 'Dust'  THEN 'Scrap'
				 WHEN 'Scrap' THEN 'Unused/Loose Material'
				 ELSE source_type END
		 WHERE source_type IN ('Dust', 'Scrap')
		"""
	)
	if has_batch_type:
		frappe.db.sql(
			"""
			UPDATE `tabBatch`
			   SET custom_batch_type = 'Unused/Loose Material'
			 WHERE custom_batch_type = 'Scrap'
			"""
		)

	_set_sentinel()
	frappe.clear_cache(doctype="Refining Entry")
	frappe.clear_cache(doctype="Refining Material Line")
	frappe.clear_cache(doctype="Batch")

	frappe.logger().info(
		"rename_refining_scrap_terminology_data: swapped refining_type / source_type"
		f"{' / custom_batch_type' if has_batch_type else ''}"
	)
