"""Seed one ``tabSeries`` counter per stamping prefix from the numbers already issued.

``Serial No.custom_stamping_no`` is "2" + year code + sequence, e.g. ``2F0003``. The live
path now claims its sequence atomically from a ``tabSeries`` row
(``doc_events.serial_no.reserve_stamping_sequence``) instead of reading ``MAX(...) + 1``,
which let two concurrent Serial Number Creator submits hand two pieces the SAME number.

Without seeding, the very first claim on a backfilled site would start from zero and
re-issue ``2F0001`` -- a number already physically stamped on a piece.

Idempotent and MONOTONIC: ``GREATEST`` can only move a counter UP, so re-running this, or
running it after more pieces have been stamped, is always safe.

This patch is a deploy-time convenience, not the correctness guarantee: ``bench install-app``
marks every patch as applied without running it, so ``_ensure_stamping_series_row`` seeds
lazily at runtime as well.
"""

import frappe

from jewellery_erpnext.jewellery_erpnext.doc_events.serial_no import _STAMPING_SERIES_NS


def execute():
	if not frappe.db.has_column("Serial No", "custom_stamping_no"):
		# The column is provisioned by add_serial_no_stamping_no_field; on a site where that
		# never ran there is nothing to seed from, and the runtime seeder will handle it.
		return

	frappe.db.sql(
		"""
		INSERT INTO `tabSeries` (`name`, `current`)
		SELECT CONCAT(%(ns)s, LEFT(custom_stamping_no, 2)) AS `name`,
		       MAX(CAST(SUBSTRING(custom_stamping_no, 3) AS UNSIGNED)) AS `current`
		FROM `tabSerial No`
		WHERE custom_stamping_no REGEXP '^2[A-Z][0-9]+$'
		GROUP BY LEFT(custom_stamping_no, 2)
		ON DUPLICATE KEY UPDATE
			`current` = GREATEST(COALESCE(`tabSeries`.`current`, 0), VALUES(`current`))
		""",
		{"ns": _STAMPING_SERIES_NS},
	)

	# A plain (case-INSENSITIVE) REGEXP is deliberate. This site is utf8mb4_unicode_ci, so
	# '[A-Z]' also matches a stray lower-case '2f0099' -- and folding that into the '2F'
	# counter is CORRECT, because `tabSeries`.`name` is under the same ci collation and
	# '#JWL-STAMP-2f' therefore IS the '#JWL-STAMP-2F' row. Forcing a case-sensitive match
	# (REGEXP BINARY) would silently drop those rows from the high-water mark and let the
	# counter re-issue a number already stamped on a piece -- the exact failure this guards.
	#
	# COALESCE(current, 0) inside GREATEST is belt-and-braces: `tabSeries`.`current` is
	# `int NOT NULL DEFAULT 0`, so the NULL is not reachable today, but GREATEST(NULL, x) is
	# NULL -- which would WIPE a counter rather than raise it -- and that is far too sharp an
	# edge to leave to the column definition staying put.

	frappe.db.commit()

	seeded = frappe.db.sql(
		"SELECT `name`, `current` FROM `tabSeries` WHERE `name` LIKE %s",
		(f"{_STAMPING_SERIES_NS}%",),
	)
	frappe.logger().info(
		f"seed_serial_no_stamping_series: stamping counters now {dict(seeded)}"
	)
