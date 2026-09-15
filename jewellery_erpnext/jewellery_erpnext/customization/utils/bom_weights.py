"""BOM weights for a finished-goods serial number.

An FG piece is serialised, and every piece carries its **own** as-built BOM on
``Serial No.custom_bom_no`` -- not the design item's active BOM, which belongs to a
different piece and would report different weights. The weights themselves live on
that BOM, not on the Serial No: ``Serial No`` carries only ``custom_bom_no`` and
``custom_gross_wt`` (itself a ``fetch_from`` mirror of ``custom_bom_no.gross_weight``).
So every weight read is the two-hop ``Serial No -> custom_bom_no -> BOM``.

This module is the single definition of that hop and of the field list, shared by the
Material Request desk form (via :func:`get_serial_fg_details`), the Material Request
save guard, and the Stock Entry row stamper. It is a leaf module -- it imports only
``jewellery_erpnext.utils`` -- so both ``customization.stock_entry`` and
``doc_events.material_request`` can import it without a circular import.

Field-name traps this module exists to contain:

* The source is ``BOM.finding_weight_`` with a **trailing underscore**. A separate
  ``BOM.finding_weight`` also exists and holds a different value.
* ``Sales Invoice Item`` has same-shaped fields (``custom_metal_weight``,
  ``custom_finding_weight``, ...) that are filled from *different* BOM columns
  (``total_metal_weight``, ``finding_weight``, ``total_diamond_weight_in_gms``) by
  ``doc_events/sales_invoice.py``. The ``custom_bom_`` prefix here keeps the two sets
  from being mistaken for each other.
"""

import frappe

from jewellery_erpnext.utils import bulk_map

# Target fieldname -> source BOM fieldname. Insertion order is the display order of
# the field block on Material Request Item and Stock Entry Detail; dicts preserve it.
BOM_WEIGHT_FIELDS = {
	"custom_bom_gross_weight": "gross_weight",
	"custom_bom_metal_weight": "metal_weight",
	"custom_bom_finding_weight": "finding_weight_",
	"custom_bom_diamond_weight": "diamond_weight",
	"custom_bom_total_diamond_pcs": "total_diamond_pcs",
	"custom_bom_gemstone_weight": "gemstone_weight",
	"custom_bom_total_gemstone_pcs": "total_gemstone_pcs",
}


def row_serials(serial_no):
	"""Serials on a row, blanks dropped.

	Deliberately **not** ``se_utils._row_serials``, which keeps interior blanks because
	it aligns a tag list line-for-line against the serial list. Here the list is only
	ever counted and indexed, so a stray blank line would misreport a single-serial row
	as a multi-serial one.
	"""
	return [s.strip() for s in (serial_no or "").splitlines() if s.strip()]


def single_serial(serial_no):
	"""The one serial on a row, or None when it carries zero or several.

	BOM weights are per-piece, so they are only meaningful on a row that holds exactly
	one serial. This is the gate every caller here shares.
	"""
	serials = row_serials(serial_no)
	return serials[0] if len(serials) == 1 else None


def get_weights_for_serials(serials):
	"""``{serial: {fieldname: value}}`` for the serials that resolve a BOM.

	Two queries regardless of how many serials are passed. That matters: one row per
	scanned serial means a single transfer can carry hundreds of rows, and a per-row
	``frappe.db.get_value`` would be a fresh N+1 on the hottest path in the flow.

	A serial that does not exist, or that has no ``custom_bom_no``, or whose BOM is
	missing, is simply absent from the result -- callers distinguish "no FG data" from
	"zero weights" by key presence, never by value.
	"""
	serial_map = bulk_map("Serial No", serials, ["custom_bom_no"])
	bom_map = bulk_map(
		"BOM",
		[row.custom_bom_no for row in serial_map.values()],
		list(BOM_WEIGHT_FIELDS.values()),
	)

	out = {}
	for serial, row in serial_map.items():
		bom = bom_map.get(row.custom_bom_no)
		if bom:
			out[serial] = {
				fieldname: bom.get(source)
				for fieldname, source in BOM_WEIGHT_FIELDS.items()
			}
	return out


def apply_bom_weights(rows, serial_getter=None):
	"""Stamp the BOM weights onto every single-serial row of ``rows``.

	A row this cannot resolve -- no serial, several serials, no ``custom_bom_no``, or a
	missing BOM -- is **left untouched rather than blanked**. Blanking is what the old
	per-row ``set_gross_wt`` did on a miss, which silently wiped a weight that a
	different code path had already stamped.
	"""
	serial_getter = serial_getter or (lambda row: getattr(row, "serial_no", None))

	pairs = [(row, single_serial(serial_getter(row))) for row in rows]
	pairs = [(row, serial) for row, serial in pairs if serial]
	if not pairs:
		return

	weights = get_weights_for_serials([serial for _, serial in pairs])
	for row, serial in pairs:
		for fieldname, value in (weights.get(serial) or {}).items():
			setattr(row, fieldname, value)


@frappe.whitelist()
def get_serial_fg_details(serial_no):
	"""``{item_code, bom_no, **weights}`` for one scanned serial.

	One round trip for the desk form, so a scan costs a single call instead of a
	Serial No read followed by a BOM read. Returns None when the serial is blank or
	unknown; returns the item without weight keys when the serial carries no BOM --
	that absence is what marks a row as "not an FG serial" on the client.
	"""
	frappe.has_permission("Serial No", throw=True)

	serial = single_serial(serial_no)
	if not serial:
		return None

	details = frappe.db.get_value(
		"Serial No", serial, ["item_code", "custom_bom_no"], as_dict=True
	)
	if not details:
		return None

	out = {"item_code": details.item_code, "bom_no": details.custom_bom_no}
	if details.custom_bom_no:
		bom = frappe.db.get_value(
			"BOM",
			details.custom_bom_no,
			list(BOM_WEIGHT_FIELDS.values()),
			as_dict=True,
		)
		if bom:
			out.update(
				{
					fieldname: bom.get(source)
					for fieldname, source in BOM_WEIGHT_FIELDS.items()
				}
			)
	return out
