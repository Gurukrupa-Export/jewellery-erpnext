// A List Client Script for this doctype ("Parent Manufacturing Order-List-81" on kggk sites)
// assigns frappe.listview_settings[...] again after this file loads, which threw away the
// onload that binds the bulk actions -- the list then ran Frappe's reason-less bulk action.
// So the entry is a property: whatever settings object is assigned, its own onload runs and
// then the bulk actions are bound.
(function () {
	const doctype = "Parent Manufacturing Order";
	let settings;
	Object.defineProperty(frappe.listview_settings, doctype, {
		configurable: true,
		enumerable: true,
		get: () => settings,
		set: (value) => {
			settings = with_bulk_actions(value || {});
		},
	});
	frappe.listview_settings[doctype] = {
		onload: function (listview) {
			listview.page.add_inner_button("PMO PMO", () => {
				window.open("/pmo_home", "_blank");
			});
		},
	};
})();

function with_bulk_actions(settings) {
	if (settings.__pmo_bulk_actions) return settings;
	const onload = settings.onload;
	settings.onload = function (listview) {
		if (onload) onload.apply(this, arguments);
		bind_bulk_actions(listview);
	};
	settings.__pmo_bulk_actions = true;
	return settings;
}

// Hold / Open / Close for the selected PMOs. Frappe's own list workflow action carries no
// reason, so each PMO refused it; these ask for one reason, as the form does, and run the
// form's flow for every PMO (close_open.apply_bulk_action). Frappe still enables an action
// only when every selected PMO allows it.
function bind_bulk_actions(listview) {
	const items = listview.workflow_action_items || {};
	["Hold", "Open", "Close"].forEach((action) => {
		const $link = items[action];
		if (!$link) return;
		const click = () => ask_bulk_reason(listview, action);
		// The Actions dropdown replays the handler stored on the row; the link keeps its own.
		$link.closest("li").data("menu_click", click);
		$link.off("click").on("click", () => {
			click();
			return false;
		});
	});
}

function ask_bulk_reason(listview, action) {
	const names = listview.get_checked_items(true);
	if (!names.length) return;
	frappe.xcall(close_open_method("bulk_action_reasons"), { action: action }).then((reasons) => {
		const dialog = new frappe.ui.Dialog({
			title: __("{0}: {1} Parent Manufacturing Orders", [__(action), names.length]),
			fields: [
				{ fieldtype: "HTML", options: `<p>${bulk_message(action, names.length)}</p>` },
				{
					fieldname: "reason",
					fieldtype: "Select",
					label: __("Reason"),
					options: ["", ...reasons].join("\n"),
					default: reasons.length === 1 ? reasons[0] : undefined,
					reqd: 1,
				},
			],
			primary_action_label: __("Yes"),
			primary_action(values) {
				dialog.hide();
				run_bulk_action(listview, action, names, values.reason);
			},
			secondary_action_label: __("No"),
			secondary_action() {
				dialog.hide();
			},
		});
		dialog.show();
	});
}

function bulk_message(action, count) {
	if (action === "Hold") {
		return __(
			"Put {0} Parent Manufacturing Orders on Hold? Each one's work orders, operations and Material Requests are frozen until it is opened.",
			[count]
		);
	}
	if (action === "Open") {
		return __(
			"Open {0} Parent Manufacturing Orders? A held one goes back to where it was before the Hold; a Close Requested one drops its Close request.",
			[count]
		);
	}
	return __(
		"Close {0} Parent Manufacturing Orders? Each goes through the same checks as on its form: one whose work orders are not back in Manufacturing Plan & Management with no metal goes to Close Requested instead, and one whose Material Requests moved stock is closed in the background after its Stock Entries are cancelled.",
		[count]
	);
}

function run_bulk_action(listview, action, names, reason) {
	frappe.dom.freeze(__("{0}…", [__(action)]));
	frappe
		.xcall(close_open_method("apply_bulk_action"), {
			pmos: names,
			action: action,
			reason: reason,
		})
		.then((result) => {
			if (result.background) {
				frappe.show_alert({
					message: __(
						"{0} of {1} Parent Manufacturing Orders runs in the background; you will be notified when it is done.",
						[__(action), result.count]
					),
					indicator: "blue",
				});
				return;
			}
			frappe.msgprint({
				title: __("{0}: Parent Manufacturing Orders", [__(action)]),
				message: result.summary,
				indicator: Object.keys(result.failed || {}).length ? "orange" : "green",
			});
		})
		.finally(() => {
			frappe.dom.unfreeze();
			listview.clear_checked_items();
			listview.refresh();
		});
}

function close_open_method(name) {
	return `jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.close_open.${name}`;
}
