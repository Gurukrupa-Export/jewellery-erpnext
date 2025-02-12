frappe.ui.form.on("Customer", {
	setup: function (frm) {
		var child_fields = [
			["diamond_quality", "Diamond Quality"],
			["diamond_grade_1", "Diamond Grade"],
			["diamond_grade_2", "Diamond Grade"],
			["diamond_grade_3", "Diamond Grade"],
			["diamond_grade_4", "Diamond Grade"],
		];
		set_filters_on_child_table_fields(frm, child_fields, "diamond_grades");
		let metal_fields = [
			["metal_touch", "Metal Touch"],
			["metal_type", "Metal Type"],
		];
		set_filters_on_child_table_fields(frm, metal_fields, "metal_criteria");
		frm.set_query("metal_purity", "metal_criteria", function (doc, cdt, cdn) {
			var d = locals[cdt][cdn];
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: "Metal Purity", metal_touch: d.metal_touch },
			};
		});
	},
});

function set_filters_on_child_table_fields(frm, fields, table) {
	fields.map(function (field) {
		frm.set_query(field[0], table, function () {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}

frappe.ui.form.on("Customer", {
	refresh(frm) {
		frm.set_value("vendor_code", frm.doc.name);
	},
	validate(frm) {
		var touch = [];
		var type = [];
		$.each(frm.doc.metal_criteria || [], function (i, d) {
			// if (in_list(touch,d.metal_touch) && in_list(type,d.metal_type)) {
			if (touch.includes(d.metal_touch) && type.includes(d.metal_type)) {
				frappe.throw(__("Metal Touch must be Unique"));
			} else {
				touch.push(d.metal_touch);
				type.push(d.metal_type);
			}
		});
	},
});
