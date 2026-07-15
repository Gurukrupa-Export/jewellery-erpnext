// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("FG BOM Field Configuration", {
	refresh(frm) {
		// Populate the "FG BOM Field" select in the child grid with the BOM doctype's
		// actual data-holding fields, so admins can only map to existing BOM fields.
		frappe.model.with_doctype("BOM", () => {
			const skip = [
				"Section Break",
				"Column Break",
				"Tab Break",
				"HTML",
				"Table",
				"Table MultiSelect",
				"Button",
				"Fold",
				"Heading",
				"Image",
			];
			const meta = frappe.get_meta("BOM");
			const options = (meta.fields || [])
				.filter((df) => df.fieldname && !skip.includes(df.fieldtype))
				.map((df) => df.fieldname)
				.sort();
			const grid = frm.fields_dict.field_config.grid;
			grid.update_docfield_property("fg_bom_field", "options", ["", ...options].join("\n"));
			grid.refresh();
		});
	},
});
