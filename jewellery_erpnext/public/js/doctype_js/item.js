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

// Multiple Variants dialog: before creating, list the combinations the Not Allowed
// table blocks and ask to skip them. ERPNext's own Create handler runs after Yes.
if (erpnext.item && !erpnext.item.not_allowed_multiple_check_added) {
	const show_multiple_variants_dialog = erpnext.item.show_multiple_variants_dialog;
	erpnext.item.show_multiple_variants_dialog = function (frm) {
		// The dialog is built after async lookups, so attach once it is shown.
		$(document).on("frappe.ui.Dialog:shown.not_allowed", () => {
			if (cur_dialog && cur_dialog === erpnext.item.multiple_variant_dialog) {
				$(document).off("frappe.ui.Dialog:shown.not_allowed");
				check_not_allowed_combinations(frm, cur_dialog);
			}
		});
		show_multiple_variants_dialog.call(this, frm);
	};
	erpnext.item.not_allowed_multiple_check_added = true;
}

function check_not_allowed_combinations(frm, dialog) {
	const primary_btn = dialog.get_primary_btn().get(0);
	let confirmed = false;

	// Capture phase runs before ERPNext's click handler on the button.
	dialog.$wrapper.get(0).addEventListener(
		"click",
		(e) => {
			if (confirmed || !primary_btn.contains(e.target)) return;
			if (primary_btn.classList.contains("disabled")) return;
			e.stopPropagation();
			e.preventDefault();

			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.item.get_blocked_combinations",
				args: { args: get_multiple_variant_selection(frm, dialog) },
				callback: (r) => {
					const result = r.message || {};
					const proceed = () => {
						confirmed = true;
						primary_btn.click();
						confirmed = false;
					};
					if (!result.blocked) return proceed();

					const reasons = (result.reasons || []).map((reason) => `• ${reason}`).join("<br>");
					const remaining = result.total - result.blocked;
					if (!remaining) {
						frappe.msgprint({
							title: __("Not Allowed"),
							indicator: "red",
							message: __("None of the selected combinations are allowed:") + "<br>" + reasons,
						});
						return;
					}
					frappe.confirm(
						__("{0} of {1} combinations are not allowed and will be skipped:", [
							result.blocked,
							result.total,
						]) +
							"<br>" +
							reasons +
							"<br><br>" +
							__("Create the other {0}?", [remaining]),
						proceed
					);
				},
			});
		},
		true
	);
}

// Same selection ERPNext's Multiple Variants dialog sends: {attribute: [values]}.
function get_multiple_variant_selection(frm, dialog) {
	let selected = {};
	(frm.doc.attributes || []).forEach((row) => {
		if (row.disabled) return;
		let values = dialog.get_value(frappe.scrub(row.attribute));
		if (values && values.length) selected[row.attribute] = values;
	});
	return selected;
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
