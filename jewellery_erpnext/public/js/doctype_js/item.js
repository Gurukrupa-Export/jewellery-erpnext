frappe.ui.form.on("Item", {
	onload_post_render: function (frm) {
		if (frappe.session.user == "Administrator") {
			frm.set_df_property("is_system_item", "read_only", 0);
		} else {
			frm.set_df_property("is_system_item", "read_only", 1);
		}
	},
	setup: function (frm) {
		frm.set_query("subcategory", function (doc) {
			return { filters: { attribute_type: "subcategory" } };
		});
	},
});

// Create Variant dialog: hide attribute values blocked by the Not Allowed table
// of the values already selected (Attribute Value -> not_allowed_attribute_values),
// e.g. Gemstone Type Coral blocks Cut or Cab Faceted.
if (erpnext.item && !erpnext.item.not_allowed_filter_added) {
	const show_single_variant_dialog = erpnext.item.show_single_variant_dialog;
	erpnext.item.show_single_variant_dialog = function (frm) {
		show_single_variant_dialog.call(this, frm);
		filter_not_allowed_attribute_values(frm, cur_dialog);
	};
	erpnext.item.not_allowed_filter_added = true;
}

function filter_not_allowed_attribute_values(frm, dialog) {
	if (!dialog) return;
	const attributes = (frm.doc.attributes || []).map((row) => row.attribute);
	const get_selected = () => {
		let selected = {};
		attributes.forEach((attribute) => {
			let value = dialog.get_value(attribute);
			if (value) selected[attribute] = value;
		});
		return selected;
	};

	attributes.forEach((attribute) => {
		const field = dialog.fields_dict[attribute];
		if (!field || field.df.fieldtype !== "Data" || !field.$input) return;
		const input = field.$input.get(0);

		// Replace ERPNext's unfiltered get_item_attribute lookup.
		field.$input.off("input").on("input", function (e) {
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.item.get_allowed_attribute_values",
				args: { item_attribute: attribute, txt: e.target.value, selected: get_selected() },
				callback: (r) => {
					input.awesomplete.list = r.message || [];
				},
			});
		});

		// Clear any selected value that the new selection blocks.
		field.$input.on("change awesomplete-selectcomplete", function () {
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.item.get_blocked_attributes",
				args: { selected: get_selected() },
				callback: (r) => {
					(r.message || []).forEach((blocked) => dialog.set_value(blocked, ""));
				},
			});
		});
	});
}

frappe.ui.form.on("Cad To Finish Weight Estimated Details", {
	cad_weight(frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		if (!row.cad_weight) return;
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.item.calculate_item_wt_details",
			args: {
				doc: row,
				bom: frm.doc.master_bom,
				item: frm.doc.name,
			},
			callback: function (r) {
				console.log(r.message);
				frappe.model.sync(r.message);
				frm.refresh();
			},
		});
	},
	touch(frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		console.log(row.touch);
		if (row.touch == "10KT") {
			frappe.model.set_value(cdt, cdn, "estimated_finish_gold_wt", row.estimated_10kt_gold_wt);
		} else if (row.touch == "14KT") {
			frappe.model.set_value(cdt, cdn, "estimated_finish_gold_wt", row.estimated_14kt_gold_wt);
		} else if (row.touch == "18KT") {
			frappe.model.set_value(cdt, cdn, "estimated_finish_gold_wt", row.estimated_18kt_gold_wt);
		} else if (row.touch == "22KT") {
			frappe.model.set_value(cdt, cdn, "estimated_finish_gold_wt", row.estimated_22kt_gold_wt);
		} else if (row.touch == "Silver") {
			frappe.model.set_value(cdt, cdn, "estimated_finish_gold_wt", row.estimated_silver_wt);
		}
	},
	estimated_finish_gold_wt(frm, cdt, cdn) {
		var row = locals[cdt][cdn];
		frappe.model.set_value(
			cdt,
			cdn,
			"total_gold_wt",
			flt(row.estimated_finish_gold_wt) + flt(row.estimated_finding_gold_wt_bom)
		);
	},
});
