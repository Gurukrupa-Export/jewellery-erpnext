import frappe
from frappe.core.doctype.data_import.data_import import (
	download_template as original_download_template,
)
from frappe.core.doctype.data_import.exporter import Exporter
from frappe.model.meta import get_field_precision
from frappe.model.utils.user_settings import get_user_settings
from frappe.utils import flt

# Float cells whose on-screen display precision we mirror in the exported file.
# Currency is deliberately excluded: Serial No has no Currency fields, and for a
# Currency field whose precision comes from the row's own currency,
# get_field_precision(doc=None) would fall back to the session default instead.
_PRECISION_FIELDTYPES = ("Float",)


def _round_float_cells(fields, rows):
	"""Round Float cells to each column's display precision, in place.

	Precision is resolved once per column (:func:`get_field_precision`); Float
	fields fall back to ``System Settings > float_precision`` exactly as the Desk
	formatter does. Currency is deliberately excluded -- a Serial No export has no
	Currency columns, and a Currency field's precision would need the row's own
	currency to resolve correctly.

	Rounding goes through :func:`frappe.utils.flt`, so exported values follow the
	same ``System Settings > rounding_method`` (Banker's / Commercial / legacy)
	that the client-side Float formatter applies. A bare ``round()`` ties-to-even
	would diverge at half-way values such as 2.675. Child-table rows leave parent
	cells blank (empty strings); the float guard below skips those and every other
	non-float cell, so non-Float columns stay byte-for-byte intact.
	"""
	rounding = [
		get_field_precision(frappe._dict(df))
		if df.fieldtype in _PRECISION_FIELDTYPES
		else None
		for df in fields
	]
	for row in rows:
		for index, value in enumerate(row):
			if isinstance(value, float) and rounding[index] is not None:
				row[index] = flt(value, rounding[index])


@frappe.whitelist()
def download_template(
	doctype,
	export_fields=None,
	export_records=None,
	export_filters=None,
	file_type="CSV",
):
	"""Mirror of ``frappe.core.doctype.data_import.data_import.download_template``.

	The upstream exporter writes raw DB values, so ``Serial No`` weights export as
	``25.3796`` while the list view and form show ``25.38`` (Float renders through
	``get_field_precision``, rounding to the configured decimal places and trimming
	trailing zeros). The list-view "Action -> Export" and the Data Import "Export
	Data" / "Download Template" buttons all land here -- they build the same
	``DataExporter`` with ``exporting_for="Insert New Records"`` and POST an identical
	payload, so the server cannot tell them apart and all of them round.

	For ``Serial No``, round Float cells with the same precision the UI uses
	(``get_field_precision``), then emit the file; every other doctype is handed
	straight to the original so export fidelity is untouched.

	KNOWN, ACCEPTED RISK -- the Data Import template rounds too. That file is meant
	to be edited and re-uploaded, and nothing upstream stops the rounded value being
	written back: ``Column.parse`` sets ``skip_import`` only for missing, duplicate
	or "Don't Import" columns (read-only is not one of them), and
	``Importer.update_record`` saves whenever ``get_diff`` is non-empty, which a
	rounded cell always makes true. So a Serial No "Update Existing Records" round
	trip that includes a Float column persists the rounded weight over the precise
	stored one.

	Not gated, by decision. The server cannot distinguish the callers, so gating it
	would need a client-side flag, and the default "Update Existing Records" template
	ships only the ID column -- the user has to tick the Float column for the loss to
	happen. If Serial No Data Imports ever start being used here, close the round trip
	on the import side (restore read-only Float fields from the DB when
	``frappe.flags.in_import`` is set) rather than by gating this endpoint: a guard
	there holds for any file, however it was produced.

	Not every export path is covered. The Report view's "Menu -> Export" posts
	``frappe.desk.reportview.export_query``, a different endpoint that is not
	overridden, so a Serial No exported that way still carries raw stored values.

	Both CSV and Excel round. The XLSX already *rendered* at ``float_precision`` via
	the Float ``num_format`` the XLSX writer registers, but the cell still held the
	exact value, so formulas and copy-out saw it; the requirement is that the exported
	value matches the UI, not just its rendering. Note a CSV cannot carry the UI's
	trailing zero -- 4.46 and 4.460 are the same number -- so choose Excel when the
	file has to read exactly as the form does.

	MIRROR WARNING: the Serial No branch below duplicates the upstream endpoint's
	body on purpose -- there is no hook between ``Exporter`` construction and
	``build_response`` to intercept the exported rows, so the two must be kept in
	sync manually. Keep porting the upstream body (currently
	frappe/core/doctype/data_import/data_import.py::download_template) when it
	changes; the read-permission gate in particular must stay.
	"""
	# Mirror of the upstream first line: throw on missing read permission rather
	# than leak a template / data rows to a user who cannot read the doctype.
	frappe.has_permission(doctype, "read", throw=True)

	if doctype != "Serial No":
		return original_download_template(
			doctype,
			export_fields=export_fields,
			export_records=export_records,
			export_filters=export_filters,
			file_type=file_type,
		)

	export_fields = frappe.parse_json(export_fields)
	export_filters = frappe.parse_json(export_filters)
	export_data = export_records != "blank_template"

	list_settings = frappe.parse_json(get_user_settings(doctype)).get("List", {})
	sort_by = list_settings.get("sort_by")
	sort_order = list_settings.get("sort_order")

	if sort_by and not frappe.get_meta(doctype).get_field(sort_by):
		sort_by = None

	if sort_order and sort_order.upper() not in ("ASC", "DESC"):
		sort_order = None

	order_by = f"{sort_by} {sort_order}" if sort_by and sort_order else None

	exporter = Exporter(
		doctype,
		export_fields=export_fields,
		export_data=export_data,
		export_filters=export_filters,
		file_type=file_type,
		export_page_length=5 if export_records == "5_records" else None,
		order_by=order_by,
	)

	# build_response() appends the blank-template filler rows itself, so the loop
	# must run here -- before it -- over only the real data rows.
	_round_float_cells(exporter.fields, exporter.csv_array[1:])

	exporter.build_response()
