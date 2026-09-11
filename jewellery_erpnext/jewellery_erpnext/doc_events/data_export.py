import frappe
from frappe.core.doctype.data_import.data_import import (
	download_template as original_download_template,
)
from frappe.core.doctype.data_import.exporter import Exporter
from frappe.model.meta import get_field_precision
from frappe.model.utils.user_settings import get_user_settings

# Float cells whose on-screen display precision we mirror in the exported file.
# Currency is deliberately excluded: Serial No has no Currency fields, and for a
# Currency field whose precision comes from the row's own currency,
# get_field_precision(doc=None) would fall back to the session default instead.
_PRECISION_FIELDTYPES = ("Float",)


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
	trailing zeros). The list-view "Action → Export" and the Data Import "Export
	Data" tab both land here, and both should produce what the user sees on screen.

	For ``Serial No``, round Float cells with the same precision the UI uses
	(``get_field_precision``), then emit the file; every other doctype is handed
	straight to the original so export fidelity is untouched.

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

	# Precision depends only on the column, so resolve it once per field instead
	# of once per cell. Row 0 is the header; data rows align one cell per field in
	# exporter.fields, and child-table rows leave parent cells blank (empty
	# strings), which the float guard below skips.
	rounding = [
		get_field_precision(frappe._dict(df))
		if df.fieldtype in _PRECISION_FIELDTYPES
		else None
		for df in exporter.fields
	]
	for row in exporter.csv_array[1:]:
		for index, value in enumerate(row):
			if isinstance(value, float) and rounding[index] is not None:
				# round() ties-to-even on purpose: fmt_money -- the Float display
				# path the UI renders -- also rounds via round(flt(amount), precision),
				# so this matches the screen. flt() would obey System Settings'
				# rounding method and diverge from what the user sees.
				row[index] = round(value, rounding[index])

	exporter.build_response()
