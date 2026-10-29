frappe.listview_settings["Manufacturing Work Order"] = {
	add_fields: ["status", "docstatus"],
	// Frappe labels every submitted work order "Submitted" whatever its status field says, so
	// a work order closed along with its PMO would look untouched. Draft is handled here as
	// well because has_indicator_for_draft switches Frappe's own Draft label off.
	has_indicator_for_draft: true,
	get_indicator(doc) {
		if (doc.status === "Closed") return [__("Closed"), "gray", "status,=,Closed"];
		if (doc.status === "Hold") return [__("Hold"), "orange", "status,=,Hold"];
		if (cint(doc.docstatus) === 0) return [__("Draft"), "red", "docstatus,=,0"];
	},
};
