// frm.set_query("warehouse", "warehouses", function (frm, cdt, cdn) {
// 	let d = locals[cdt][cdn];
// 	return {
// 		filters: {
// 			company: d.company,
// 		},
// 	};
// });
frappe.ui.form.on("Operation", {
	setup: function (frm, cdt, cdn) {
		frm.set_query("warehouse", "warehouses", function (frm, cdt, cdn) {
			let d = locals[cdt][cdn];
			return {
				filters: {
					company: d.company,
				},
			};
		});
	},
});
