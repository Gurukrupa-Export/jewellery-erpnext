"""Seed ``tabSeries`` counters for doctypes whose autoname was repointed off the
shared empty-prefix ``tabSeries[name='']`` row onto their own per-prefix counter.

Background
----------
Frappe's ``format:`` autoname parses each ``{...}`` brace in isolation
(``_format_autoname`` -> ``parse_naming_series([brace], ...)`` with an empty
accumulated ``name``), so a ``{#####}`` brace calls ``getseries("", digits)`` and
locks the single ``tabSeries[name='']`` row. The following doctypes were moved to
dot-style autoname so each gets its OWN series row:

* MOP EOD Sync Log, Custom Refining, Manufacturing Operation, Design Order,
  Manufacturing Plan Sales Order, Operation Card Transfer, Slip,
  Customer Product Tolerance Master, Supplier Services Price,
  Diamond Price List, Gemstone Price List — all moved from `format:...{#####}`
  (empty-row) to dot-style, keeping the SAME visible name format.

Because the visible format is unchanged, the new per-prefix counter must be seeded to the
max suffix already used or the next generated name would collide with an existing primary
key. The seeder only keeps prefixes that end in a separator (``- . /``) — exactly the shape
a dot-style ``PREFIX-.#####`` counter produces — so irregular legacy names (e.g. a site that
named Manufacturing Operations as ``MOP-<random alphanumeric>`` via a server script) don't
pollute ``tabSeries`` with junk prefix rows.

The empty-prefix row ``tabSeries[name='']`` is intentionally left frozen (never
deleted), so any straggler ``format:`` doctype keeps working.

Idempotent: ``ON DUPLICATE KEY UPDATE current = GREATEST(current, VALUES(current))``
never decrements, so re-running this patch (or running it after more documents have
been created) is safe.
"""

import frappe

# Doctypes repointed from empty-row ``format:`` naming to a dedicated per-prefix counter
# while KEEPING the same visible name format (so the counter must be seeded to avoid
# colliding with names already generated from the shared empty-prefix counter).
_DOCTYPES = (
	"MOP EOD Sync Log",
	"Custom Refining",
	"Manufacturing Operation",
	"Design Order",
	"Manufacturing Plan Sales Order",
	"Operation Card Transfer",
	"Slip",
	"Customer Product Tolerance Master",
	"Supplier Services Price",
	"Diamond Price List",
	"Gemstone Price List",
)


def execute():
	for doctype in _DOCTYPES:
		if not frappe.db.table_exists(doctype):
			continue
		table = f"tab{doctype}"

		# For every existing name, split the trailing numeric run as the counter and
		# everything before it as the series prefix (exactly what getseries() will use
		# as its key for the new dot-style autoname). Seed each prefix to the highest
		# number seen, never below the current value.
		frappe.db.sql(
			f"""
			INSERT INTO `tabSeries` (`name`, `current`)
			SELECT prefix, MAX(num) AS num
			FROM (
				SELECT
					LEFT(`name`, CHAR_LENGTH(`name`) - CHAR_LENGTH(suffix)) AS prefix,
					CAST(suffix AS UNSIGNED) AS num
				FROM (
					SELECT `name`, REGEXP_SUBSTR(`name`, '[0-9]+$') AS suffix
					FROM `{table}`
					WHERE `name` REGEXP '[0-9]+$'
				) AS extracted
			) AS parsed
			WHERE prefix <> '' AND prefix REGEXP '[-./]$'
			GROUP BY prefix
			ON DUPLICATE KEY UPDATE `current` = GREATEST(`tabSeries`.`current`, VALUES(`current`))
			"""
		)

	frappe.db.commit()
