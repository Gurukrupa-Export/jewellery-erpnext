import frappe


@frappe.whitelist()
def get_link_title(doctype: str, docname: str | int):
	"""Core `frappe.desk.search.get_link_title` returns None when a title-link doctype's
	title_field is empty. `link.js` then paints the input blank while the model still holds
	the value, so the field only appears after a page reload.

	`Manufacturing Operation` is the case that bites: show_title_field_in_link=1 with an
	optional `operation` title field, deliberately blank for MOPs off a split MWO and for
	FG MWOs with no is_last_operation Department Operation.

	Fall back to the docname -- exactly what the page-load path already does in
	frappe/desk/form/load.py::get_title_values_for_link_and_dynamic_link_fields (`link_title
	or doc_fieldvalue`). Sibling links like manufacturing_order render today only because
	their doctype has no show_title_field_in_link and so takes the `return docname` branch.
	"""
	meta = frappe.get_meta(doctype)

	if meta.show_title_field_in_link:
		doc = frappe.get_lazy_doc(doctype, docname)
		doc.check_permission()
		return doc.get(meta.title_field) or docname

	return docname
