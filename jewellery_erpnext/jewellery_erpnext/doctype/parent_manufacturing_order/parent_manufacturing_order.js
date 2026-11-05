// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Parent Manufacturing Order", {
	before_workflow_action(frm) {
		// Hold / Open / Close: a dry run of the checks first, so a refusal comes before any
		// dialog; then one dialog with the confirmation and the reason, chosen from the reasons
		// MOP Settings lists for the action. The action runs through apply_action, which records
		// the reason with it, so Frappe's own workflow call is always cancelled (rejected). A
		// Close whose Material Requests moved stock is queued instead (enqueue_close).
		const action = frm.selected_workflow_action;
		if (!["Hold", "Open", "Close"].includes(action)) return;
		// handle_workflow_action freezes the screen before calling this and only unfreezes
		// once apply_workflow settles, so a rejection here would leave the page frozen and
		// the freeze overlay sits over any dialog opened meanwhile. Lift it for the checks
		// and the dialogs; the confirming button freezes again while the action runs.
		frappe.dom.unfreeze();
		return new Promise((resolve, reject) => {
			frappe.call({
				method: close_open_method("check_action"),
				args: { pmo: frm.doc.name, action: action },
				// a refusal shows its own error message, so the action just stops here
				error: () => reject(),
				callback: (r) => {
					if (r.exc) return reject();
					const plan = r.message || {};
					if (action === "Close" && (plan.entries || []).length) {
						return ask_close_with_stock(frm, plan, reject);
					}
					ask_reason(frm, action, plan.reasons || [], reject);
				},
			});
		});
	},
	setup(frm) {
		filter_departments(frm, "diamond_department");
		filter_departments(frm, "gemstone_department");
		filter_departments(frm, "finding_department");
		filter_departments(frm, "other_material_department");
		filter_departments(frm, "metal_department");
		var parent_fields = [
			["diamond_grade", "Diamond Grade"],
			["metal_colour", "Metal Colour"],
			["metal_purity", "Metal Purity"],
		];
		set_filters_on_parent_table_fields(frm, parent_fields);
	},
	refresh(frm) {
		if ((frm.doc.customer || frm.doc.ref_customer) && frm.doc.diamond_quality) {
			frm.set_query("diamond_grade", function () {
				return {
					query: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.filters_query.get_diamond_grade",
					searchfield: "diamond_grade",
					filters: {
						// ref_customer has to travel with the customer: the server prefers it, and
						// set_auto_diamond_grade already resolves against it. Sending only customer
						// made the picker list grades the save would immediately replace.
						customer: frm.doc.customer,
						ref_customer: frm.doc.ref_customer,
						diamond_quality: frm.doc.diamond_quality,
						use_custom_diamond_grade: frm.doc.use_custom_diamond_grade ? 1 : 0,
						is_customer_diamond: frm.doc.is_customer_diamond ? 1 : 0,
						sales_type: frm.doc.sales_type,
					},
				};
			});
		}

		// Always control read-only dynamically
		frm.set_df_property("diamond_grade", "read_only", !frm.doc.use_custom_diamond_grade);

		apply_customer_details_visibility(frm);
		apply_grade_override_hint(frm);

		if (!frm.doc.__islocal) {
			frm.add_custom_button(__("Send For Customer Approval"), function () {
				frm.trigger("create_customer_transfer");
			});
		}
		if (frm.doc.docstatus == 1) {
			frm.add_custom_button(__("Create MWO"), function () {
				frappe.prompt(
					[
						{
							fieldname: "reason",
							label: "Reason",
							fieldtype: "Select",
							reqd: 1,
							options: [
								"Cpx rpt",
								"Mould broken & extra need for bulk order",
								"Prong thickness & height for wax setting",
								"Mumbai CAD, if CAD image show in (ppc wax cad) then we have to transfer for rubber mould work",
							].join("\n"),
						},
					],
					function (data) {
						// Run only after selecting reason
						frappe.call({
							method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.create_mwo",
							args: {
								pmo: frm.doc.name,
								doc: frm.doc,
								reason: data.reason, // passing reason also
							},
							callback: function (r) {
								if (!r.exc) {
									frm.reload_doc();
								}
							},
						});
					},
					__("Select Reason"), // Dialog Title
					__("Submit") // Submit button label
				);
			});
		}
	},

	customer(frm) {
		set_auto_diamond_grade(frm);
	},

	diamond_quality(frm) {
		set_auto_diamond_grade(frm);
	},

	is_customer_diamond(frm) {
		set_auto_diamond_grade(frm);
		apply_grade_override_hint(frm);
		clear_disallowed_custom_grade(frm);
	},

	use_custom_diamond_grade(frm) {
		frm.set_df_property("diamond_grade", "read_only", !frm.doc.use_custom_diamond_grade);
		set_auto_diamond_grade(frm);
		apply_grade_override_hint(frm);
		clear_disallowed_custom_grade(frm);
	},

	create_customer_transfer: function (frm) {
		frm.call({
			doc: frm.doc,
			method: "send_to_customer_for_approval",
			freeze: true,
			freeze_message: __("Transfering to Central...."),
			callback: (r) => {
				if (!r.exc) {
					frappe.msgprint(__("Manufacturing Entry has been created."));
					frm.refresh();
				}
			},
		});
	},
	sales_order_item: function (frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.production_order.production_order.get_item_code",
			args: {
				sales_order_item: frm.doc.sales_order_item,
			},
			type: "GET",
			callback: function (r) {
				console.log(r.message);
				frm.doc.item_code = r.message;
				frm.set_value("item_code", r.message);
				refresh_field("item_code");
				frm.trigger("item_code");
			},
		});
	},
	after_workflow_action(frm) {
		const onHoldStates = ["On Hold"];

		if (onHoldStates.includes(frm.doc.workflow_state)) {
			frappe.prompt(
				[
					{
						label: "Reason For Hold",
						fieldname: "update_reason",
						fieldtype: "Data",
						reqd: 1,
					},
				],
				(values) => {
					if (!values.update_reason) {
						frappe.msgprint(__("Please provide a reason for putting the order on hold."));
						return;
					}

					frappe.call({
						method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.add_hold_comment",
						args: {
							doctype: frm.doctype,
							docname: frm.docname,
							reason: values.update_reason,
						},
						callback: function (r) {
							if (!r.exc) {
								frappe.msgprint(__("Comment added successfully."));
								frm.refresh();
							} else {
								frappe.msgprint(__("Failed to add comment. Please try again."));
							}
						},
						error: function (err) {
							console.error("Error in frappe.call:", err);
							frappe.msgprint(
								__("An error occurred while adding the comment. Please try again.")
							);
						},
					});
				}
			);
		}
	},
});

// The section break plus every field in it: hiding the break alone leaves the fields reachable if
// anything later re-renders them, and the list is short enough to be exact.
const CUSTOMER_DETAIL_FIELDS = [
	"customer_details_section",
	"is_customer_gold",
	"is_customer_diamond",
	"is_customer_gemstone",
	"is_customer_material",
];

// Sales Type -> the is_customer_diamond it implies, mirroring SALES_TYPE_CUSTOMER_DIAMOND in
// doc_events/filters_query.py. Only used to explain an empty dropdown; the server decides the list.
const SALES_TYPE_CUSTOMER_DIAMOND = { Outright: 0, Outwork: 1 };

// Cosmetic only. The values still reach the browser and the REST API -- this keeps the section off
// the form, it does not stop a non-manager writing those fields another way.
function apply_customer_details_visibility(frm) {
	// Set both ways, not just to 1: df properties live on the client-side doctype meta and
	// persist across forms for the session, so a one-way toggle leaks into the next form.
	const hidden = frappe.user.has_role("System Manager") ? 0 : 1;
	CUSTOMER_DETAIL_FIELDS.forEach((field) => {
		frm.set_df_property(field, "hidden", hidden);
	});
}

// The controller now rejects a Sales Type that disagrees with Is Customer Diamond, in both
// automatic and custom mode. Say so on the form rather than letting the user find out on save.
function apply_grade_override_hint(frm) {
	// Sales Type is a Link, so its value is whatever Sales Type records exist -- test for the
	// two values this map holds rather than for "not undefined", which a name like "constructor"
	// would satisfy off Object.prototype.
	const expected = SALES_TYPE_CUSTOMER_DIAMOND[frm.doc.sales_type];
	const has_rule = expected === 0 || expected === 1;
	const mismatch = has_rule && expected !== (frm.doc.is_customer_diamond ? 1 : 0);

	frm.set_df_property(
		"diamond_grade",
		"description",
		mismatch
			? __(
					"A {0} order requires Is Customer Diamond to be {1}. Saving is blocked until that is corrected.",
					[frm.doc.sales_type, expected ? __("checked") : __("unchecked")]
			  )
			: ""
	);
}

// Turning the override on leaves whatever the automatic rule resolved sitting in the field, and
// that value may be one the override is not allowed to keep -- the reported case is a customer
// whose only configured grade is a customer-diamond one on an Outright order. Clear it here
// rather than let the user discover it as a save error on a field they never touched.
function clear_disallowed_custom_grade(frm) {
	if (!frm.doc.use_custom_diamond_grade || !frm.doc.diamond_grade) {
		return;
	}

	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.filters_query.get_allowed_custom_diamond_grades",
		args: {
			customer: frm.doc.customer,
			ref_customer: frm.doc.ref_customer,
			diamond_quality: frm.doc.diamond_quality,
			sales_type: frm.doc.sales_type,
			is_customer_diamond: frm.doc.is_customer_diamond ? 1 : 0,
		},
		callback: function (r) {
			const allowed = r.message || [];
			if (!allowed.includes(frm.doc.diamond_grade)) {
				frm.set_value("diamond_grade", null);
			}
		},
	});
}

// Must never be called from refresh: frm.set_value marks the form dirty, so resolving the grade on
// load makes a freshly opened record show as unsaved. Only run it when the user changes an input;
// on save the controller resolves the same value through resolve_diamond_grade.
function set_auto_diamond_grade(frm) {
	if (frm.doc.use_custom_diamond_grade) {
		return;
	}
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.filters_query.get_auto_diamond_grade",
		args: {
			customer: frm.doc.customer,
			ref_customer: frm.doc.ref_customer,
			diamond_quality: frm.doc.diamond_quality,
			is_customer_diamond: frm.doc.is_customer_diamond ? 1 : 0,
		},
		callback: function (r) {
			if (r.message) {
				frm.set_value("diamond_grade", r.message);
			}
		},
	});
}

function filter_departments(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				company: frm.doc.company,
			},
		};
	});
}

function set_filters_on_parent_table_fields(frm, fields) {
	fields.map(function (field) {
		frm.set_query(field[0], function (doc) {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}
function set_html(frm) {
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.get_stock_summary",
		args: {
			pmo_name: frm.doc.name,
		},
		callback: function (r) {
			frm.get_field("stock_summery").$wrapper.html(r.message);
		},
	});

	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.get_linked_stock_entries",
		doc: frm.doc,
		args: {
			pmo_name: frm.doc.name,
		},
		callback: function (r) {
			frm.get_field("stock_entry_details").$wrapper.html(r.message);
		},
	});
}

function reason_field(reasons) {
	// The reasons MOP Settings lists for the action; one has to be chosen.
	return {
		fieldname: "reason",
		fieldtype: "Select",
		label: __("Reason"),
		options: ["", ...reasons].join("\n"),
		default: reasons.length === 1 ? reasons[0] : undefined,
		reqd: 1,
	};
}

function action_message(frm, action) {
	if (action === "Hold") return hold_message(frm);
	if (action === "Open") return open_message();
	return close_message(frm);
}

function ask_reason(frm, action, reasons, reject) {
	// The workflow action itself never runs from here (the promise is always rejected):
	// apply_action does it, so the reason row is written with it or not at all.
	const dialog = new frappe.ui.Dialog({
		title: __(action),
		fields: [
			{ fieldtype: "HTML", options: `<p>${action_message(frm, action)}</p>` },
			reason_field(reasons),
		],
		primary_action_label: __("Yes"),
		primary_action(values) {
			dialog.hide();
			frappe.dom.freeze();
			frappe
				.xcall(close_open_method("apply_action"), {
					pmo: frm.doc.name,
					action: action,
					reason: values.reason,
				})
				.then(() => frm.reload_doc())
				.finally(() => frappe.dom.unfreeze());
		},
		secondary_action_label: __("No"),
		secondary_action() {
			dialog.hide();
		},
	});
	dialog.onhide = () => reject();
	dialog.show();
}

function hold_message(frm) {
	if (frm.doc.docstatus === 0) {
		return __("Put this draft Parent Manufacturing Order on Hold? It can then be submitted or closed.");
	}
	return __(
		"Put this Parent Manufacturing Order on Hold? Its Manufacturing Work Orders, their current operations and its Material Requests are set to Hold, and nothing moves for it until it is opened again. Reserved stock stays reserved."
	);
}

function open_message() {
	return __(
		"Open this Parent Manufacturing Order? Its Manufacturing Work Orders, their operations and its Material Requests go back to where they were before the Hold."
	);
}

function close_message(frm) {
	if (frm.doc.docstatus === 0) {
		return __("Close this draft Parent Manufacturing Order? It will never be submitted.");
	}
	return __(
		"Close this Parent Manufacturing Order? Its Manufacturing Work Orders are closed, their waiting operations finished and its Material Requests closed. A Close cannot be undone."
	);
}

function ask_close_with_stock(frm, plan, reject) {
	// The workflow action itself never runs from here (the promise is always rejected): the
	// background job applies the Close once the Stock Entries are cancelled.
	const rows = plan.entries
		.map(
			(e) =>
				`<tr><td>${frappe.utils.get_form_link("Stock Entry", e.name, true)}</td>` +
				`<td>${frappe.utils.escape_html(e.stock_entry_type || "")}</td>` +
				`<td>${frappe.datetime.str_to_user(e.posting_date)}</td></tr>`
		)
		.join("");
	const stopped = (plan.stopped || []).length
		? `<p>${__("The Stopped Material Requests {0} are re-opened first.", [plan.stopped.join(", ")])}</p>`
		: "";
	const dialog = new frappe.ui.Dialog({
		title: __("Close: cancel Stock Entries first"),
		fields: [
			{
				fieldtype: "HTML",
				options:
					`<p>${__(
						"Its Material Requests moved stock. These Stock Entries are cancelled in the background, latest first, and then the Parent Manufacturing Order is closed:"
					)}</p>` +
					`<table class="table table-bordered table-condensed"><thead><tr>` +
					`<th>${__("Stock Entry")}</th><th>${__("Type")}</th><th>${__(
						"Date"
					)}</th></tr></thead><tbody>${rows}</tbody></table>` +
					stopped +
					`<p>${__(
						"It is on Hold until then. If a cancel fails it stays on Hold with what was done, and Close carries on from there."
					)}</p>`,
			},
			reason_field(plan.reasons || []),
		],
		primary_action_label: __("Cancel Entries and Close"),
		primary_action(values) {
			dialog.hide();
			frappe
				.xcall(close_open_method("enqueue_close"), {
					pmo: frm.doc.name,
					reason: values.reason,
				})
				.finally(() => frm.reload_doc());
		},
		secondary_action_label: __("No"),
		secondary_action() {
			dialog.hide();
		},
	});
	dialog.onhide = () => reject();
	dialog.show();
}

function close_open_method(name) {
	return `jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.close_open.${name}`;
}
