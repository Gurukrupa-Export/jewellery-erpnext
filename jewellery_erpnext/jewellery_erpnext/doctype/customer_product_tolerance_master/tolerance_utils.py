# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Weight-band selection for Customer Product Tolerance Master rows.

A tolerance master holds several rows per dimension, each covering a different weight
band -- e.g. "0-50 g at +/-7%" and "51-100 g at +/-5%". For any one product exactly ONE
of those rows applies: the one whose band covers the BOM weight.

Shared by Parent Manufacturing Order.on_submit, which stamps the chosen band onto the
PMO, and by the Department IR transfer gate, which checks actual weights against it, so
the two can never disagree about which master row is in force.

The band convention is the app's existing one, lifted from
``refining.doctype.refinery_price_list.refinery_price_list.pick_price_slab``: inclusive
at both ends, ``to == 0`` means no upper bound, first covering row by document order
wins. Live masters rely on all three -- PTM-MHCU0009-00001's top row is "from 50 / to 0"
(and above), and PTM-MHCU0008-00001 has touching bands 3-5 and 5-10 where the first must
win deterministically.

Pure -- no DB access -- so it unit-tests without a site.
"""

from frappe.utils import flt

NO_UPPER_BOUND = 1e12


def covers(row, weight, from_field="from_weight", to_field="to_weight"):
	"""True when ``row``'s band contains ``weight``.

	A row with both bounds unset covers everything, which is how band-less rows such as
	the diamond table's "Universal" type are expressed.
	"""
	upper = flt(row.get(to_field)) or NO_UPPER_BOUND
	return flt(row.get(from_field)) <= flt(weight) <= upper


def pick_tolerance_row(rows, weight, from_field="from_weight", to_field="to_weight"):
	"""First row of ``rows`` whose band covers ``weight``, or ``None``.

	Callers must handle ``None`` -- it means the master has a gap at this weight, which
	is a master-data error rather than a "no tolerance applies" answer.
	"""
	for row in rows or []:
		if covers(row, weight, from_field, to_field):
			return row
	return None


def group_tolerance_rows(rows, key):
	"""``{key(row): [rows]}`` preserving document order within and across groups.

	The group key is everything about a row that is NOT its band, so each independent
	dimension a master defines -- Gross vs Net, Gold vs Silver, one sieve size vs
	another -- competes for its own single winner instead of all rows being applied at
	once.
	"""
	groups = {}
	for row in rows or []:
		groups.setdefault(key(row), []).append(row)
	return groups


# Group keys, shared so Customer Product Tolerance Master.validate and the Parent
# Manufacturing Order populators can never disagree about what constitutes one band
# schedule. The key is everything about a row EXCEPT its band.


def metal_group_key(row):
	return (row.weight_type or "", row.metal_type or "")


def diamond_group_key(row):
	"""weight_type decides what the band is scoped to.

	``Universal`` is the band-less catch-all over every diamond, so it scopes on nothing.
	"""
	if row.weight_type == "MM Size wise":
		return (row.weight_type, row.diamond_type or "", row.sieve_size or "")
	if row.weight_type == "Group Size wise":
		return (row.weight_type, row.diamond_type or "", row.sieve_size_range or "")
	if row.weight_type == "Universal":
		return (row.weight_type, "", "")
	return (row.weight_type or "", row.diamond_type or "", "")


def gemstone_group_key(row):
	if row.weight_type == "Weight Range":
		return (row.weight_type, row.gemstone_shape or "")
	if row.weight_type == "Gemstone Type Range":
		return (row.weight_type, row.gemstone_type or "")
	return (row.weight_type or "", "")
