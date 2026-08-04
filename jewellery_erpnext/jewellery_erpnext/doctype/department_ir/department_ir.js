// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Department IR", {
	refresh: function (frm) {
		set_html(frm);
	},
	onload(frm) {
		frm.fields_dict["department_ir_operation"].grid.add_new_row = false;
		$(frm.fields_dict["department_ir_operation"].grid.wrapper).find(".grid-add-row").hide();
	},
	async validate(frm) {
		await validate_department_ir_wo_limit(frm);
	},
	setup: function (frm) {
		frm.set_query("receive_against", function (doc) {
			return {
				filters: {
					current_department: frm.doc.previous_department,
					next_department: frm.doc.current_department,
				},
				query: "jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.department_receive_query",
			};
		});
		frm.set_query("current_department", department_filter(frm));
		frm.set_query("next_department", department_filter(frm));
		frm.set_query("previous_department", department_filter(frm));
		frm.set_query("manufacturing_operation", "department_ir_operation", function (doc, cdt, cdn) {
			var dir_status =
				frm.doc.type == "Receive" ? "In-Transit" : ["not in", ["In-Transit", "Received"]];
			var filter_dict = {
				department_ir_status: dir_status,
			};
			if (frm.doc.type == "Issue") {
				filter_dict["status"] = ["in", ["Finished", "Revert"]];
				filter_dict["department"] = frm.doc.current_department;
			} else {
				if (frm.doc.receive_against) {
					filter_dict["department_issue_id"] = frm.doc.receive_against;
				}
			}
			return {
				filters: filter_dict,
			};
		});
		frm.ignore_doctypes_on_cancel_all = ["Stock Entry", "Serial and Batch Bundle"];
	},
	// refresh:function(frm){
	// 	frm.add_custom_button(__('End Transit'), function() {
	// 		frappe.model.open_mapped_doc({
	// 			// method: "erpnext.stock.doctype.stock_entry.stock_entry.make_stock_in_entry",
	// 			method:"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.stock_entry_end_transit",
	// 			frm: frm,
	// 			args: {
	// 				doc: frm.doc.name
	// 			}
	// 		})
	// 	});
	// },
	type(frm) {
		frm.clear_table("department_ir_operation");
		frm.refresh_field("department_ir_operation");
	},
	receive_against(frm) {
		if (frm.doc.receive_against) {
			frappe.call({
				method: "jewellery_erpnext.utils.get_department_ir_transfer_departments",
				args: {
					department_ir: frm.doc.receive_against,
				},
				callback(r) {
					var value = r.message;
					frm.set_value({
						previous_department: value.current_department,
						current_department: value.next_department,
					});
				},
			});
			frappe.call({
				method: "get_manufacturing_operations_from_department_ir",
				doc: frm.doc,
				args: {
					docname: frm.doc.receive_against,
				},
				callback(r) {
					frm.refresh_field("department_ir_operation");
				},
			});
		} else {
			frm.clear_table("department_ir_operation");
			frm.refresh_field("department_ir_operation");
		}
	},
	async scan_mwo(frm) {
		if (frm.doc.scan_mwo) {
			frm.doc.department_ir_operation.forEach(function (item) {
				if (item.manufacturing_work_order == frm.doc.scan_mwo)
					frappe.throw(__("{0} Manufacturing Work Order already exists", [frm.doc.scan_mwo]));
			});
			// if (frm.doc.department_ir_operation.length > 30) {
			// 	frappe.throw(__("Only 30 MOP allowed in one document"));
			// }
			if (!frm.doc.current_department) {
				frappe.throw(__("Please select current department first"));
			}
			let wo_limit = await get_department_ir_wo_limit(frm);
			if (wo_limit.limit > 0 && frm.doc.department_ir_operation.length >= wo_limit.limit) {
				frappe.throw(
					__("Only {0} work order(s) allowed per Department IR for department {1}.", [
						wo_limit.limit,
						wo_limit.department,
					])
				);
			}
			var query_filters = {
				company: frm.doc.company,
				manufacturing_work_order: frm.doc.scan_mwo,
				department: frm.doc.current_department,
			};
			if (frm.doc.type == "Issue") {
				query_filters["department_ir_status"] = ["not in", ["In-Transit", "Revert"]];
				query_filters["status"] = ["in", ["Not Started"]];
				query_filters["employee"] = ["is", "not set"];
				query_filters["subcontractor"] = ["is", "not set"];
			} else {
				query_filters["department_ir_status"] = ["in", ["In-Transit", "Received"]];
			}
			if (frm.doc.next_department && frm.doc.is_finding == 0) {
				query_filters["is_finding"] = 0;
			}
			frappe.db
				.get_value("Manufacturing Operation", query_filters, [
					"name",
					"manufacturing_work_order",
					"status",
					"gross_wt",
					"diamond_wt",
					"net_wt",
					"finding_wt",
					"diamond_pcs",
					"gemstone_pcs",
					"gemstone_wt",
					"other_wt",
					"previous_mop",
					"is_finding",
				])
				.then((r) => {
					let values = r.message;
					frappe.db
						.get_value("Manufacturing Operation", values.previous_mop, [
							"gross_wt",
							"diamond_wt",
							"net_wt",
							"finding_wt",
							"diamond_pcs",
							"gemstone_pcs",
							"gemstone_wt",
							"other_wt",
							"received_gross_wt",
						])
						.then((v) => {
							if (values.manufacturing_work_order) {
								let row;
								if (values.is_finding) {
									// Finding: mirror the current operation exactly — no
									// previous-MOP fallback. A finding's "receive from work
									// order" legitimately empties the operation balance, so the
									// previous MOP's weights would be phantom values here.
									row = frm.add_child("department_ir_operation", {
										manufacturing_work_order: values.manufacturing_work_order,
										manufacturing_operation: values.name,
										status: values.status,
										gross_wt: values.gross_wt || 0,
										diamond_wt: values.diamond_wt || 0,
										net_wt: values.net_wt || 0,
										finding_wt: values.finding_wt || 0,
										gemstone_wt: values.gemstone_wt || 0,
										other_wt: values.other_wt || 0,
										diamond_pcs: values.diamond_pcs || 0,
										gemstone_pcs: values.gemstone_pcs || 0,
									});
								} else {
									let gr_wt = 0;
									if (values.gross_wt > 0) {
										gr_wt = values.gross_wt;
									} else if (v.message.received_gross_wt > 0 || v.message.gross_wt) {
										if (v.message.received_gross_wt > 0) {
											gr_wt = v.message.received_gross_wt;
										} else if (v.message.gross_wt > 0) {
											gr_wt = v.message.gross_wt;
										}
									}

									row = frm.add_child("department_ir_operation", {
										manufacturing_work_order: values.manufacturing_work_order,
										manufacturing_operation: values.name,
										status: values.status,
										gross_wt: gr_wt,
										diamond_wt:
											values.diamond_wt > 0 ? values.diamond_wt : v.message.diamond_wt,
										net_wt: values.net_wt > 0 ? values.net_wt : v.message.net_wt,
										finding_wt:
											values.finding_wt > 0 ? values.finding_wt : v.message.finding_wt,
										gemstone_wt:
											values.gemstone_wt > 0
												? values.gemstone_wt
												: v.message.gemstone_wt,
										other_wt: values.other_wt > 0 ? values.other_wt : v.message.other_wt,
										diamond_pcs:
											values.diamond_pcs > 0
												? values.diamond_pcs
												: v.message.diamond_pcs,
										gemstone_pcs:
											values.gemstone_pcs > 0
												? values.gemstone_pcs
												: v.message.gemstone_pcs,
									});
								}
								frm.refresh_field("department_ir_operation");
							} else {
								frappe.throw(__("No Manufacturing Operation Found"));
							}
						});
					frm.set_value("scan_mwo", "");
				});
		}
	},
	get_operations(frm) {
		if (!frm.doc.current_department) {
			frappe.throw(__("Please select current department first"));
		}
		var query_filters = {
			company: frm.doc.company,
		};
		if (frm.doc.type == "Issue") {
			query_filters["department_ir_status"] = ["not in", ["In-Transit", "Revert"]];
			query_filters["status"] = ["in", ["Not Started"]];
			query_filters["employee"] = ["is", "not set"];
			query_filters["subcontractor"] = ["is", "not set"];
		} else {
			query_filters["department_ir_status"] = "In-Transit";
			query_filters["department"] = frm.doc.current_department;
		}
		if (frm.doc.next_department && frm.doc.is_finding == 0) {
			query_filters["is_finding"] = 0;
		}
		erpnext.utils.map_current_doc({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.get_manufacturing_operations",
			source_doctype: "Manufacturing Operation",
			target: frm,
			setters: {
				manufacturing_work_order: undefined,
				company: frm.doc.company || undefined,
				department: frm.doc.current_department,
			},
			get_query_filters: query_filters,
			size: "extra-large",
		});
	},
});
var department_filter = function (frm) {
	return {
		filters: {
			company: frm.doc.company,
		},
	};
};

function set_html(frm) {
	var template = `
		<table class="table table-bordered table-hover" width="100%" style="border: 1px solid #d1d8dd;">
			<thead>
				<tr style = "text-align:center">
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Gross WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Net WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Finding WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Diamond WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Gemstone WT</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Diamond PCs</th>
					<th style="border: 1px solid #d1d8dd; font-size: 11px;">Gemstone PCs</th>
				</tr>
			</thead>
			<tbody>
			{% for item in data %}
				<tr style = "text-align:center">
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.gross_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.net_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.finding_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.diamond_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.gemstone_wt }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.diamond_pcs }}</td>
					<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.gemstone_pcs }}</td>
				</tr>
			{% endfor %}
			</tbody>
		</table>`;
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.department_ir_utils.get_summary_data",
		args: {
			doc: frm.doc,
		},
		callback: function (r) {
			if (r.message) {
				frm.get_field("summary").$wrapper.html(frappe.render_template(template, { data: r.message }));
			}
		},
	});
}

// Cap on the number of Manufacturing Work Orders in one Department IR (both Issue and
// Receive). The limit is maintained on the Department doctype
// (custom_department_ir_work_order_limit); 0 or unset means no limit. All three
// department fields are checked (current/previous/next, since which are populated
// depends on Issue vs Receive), and the strictest configured limit applies. Casting is
// exempted purely by setting the casting department's limit to 0 — no code check.
async function get_department_ir_wo_limit(frm) {
	const depts = [frm.doc.current_department, frm.doc.previous_department, frm.doc.next_department].filter(
		Boolean
	);
	const seen = new Set();
	let applicable = 0;
	let department = null;
	for (const d of depts) {
		if (seen.has(d)) continue;
		seen.add(d);
		const r = await frappe.db.get_value("Department", d, "custom_department_ir_work_order_limit");
		const lim = cint(r && r.message && r.message.custom_department_ir_work_order_limit);
		if (lim > 0 && (applicable === 0 || lim < applicable)) {
			applicable = lim;
			department = d;
		}
	}
	return { limit: applicable, department: department };
}

async function validate_department_ir_wo_limit(frm) {
	const { limit, department } = await get_department_ir_wo_limit(frm);
	const count = (frm.doc.department_ir_operation || []).length;
	if (limit > 0 && count > limit) {
		frappe.throw(
			__("Only {0} work order(s) allowed per Department IR for department {1}.", [limit, department])
		);
	}
}
