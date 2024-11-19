frappe.ui.form.on("Manufacturer", {
	validate(frm) {
		var purity = {};
		$.each(frm.doc.metal_criteria || [], function (i, d) {
			if (purity[d.metal_type]) {
				if (purity[d.metal_type].includes(d.metal_touch)) {
					frappe.throw(__("Metal Touch must be Unique"));
				}
				purity[d.metal_type].push(d.metal_touch);
			} else {
				purity[d.metal_type] = [d.metal_touch];
			}
			// if (purity.includes(d.metal_touch)) {
			// 	frappe.throw(__("Metal Touch must be Unique"));
			// } else {
			// 	purity.push(d.metal_touch);
			// }
		});
	},
	setup: function (frm) {
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
