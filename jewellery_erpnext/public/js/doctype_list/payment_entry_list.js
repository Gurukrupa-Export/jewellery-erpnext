frappe.listview_settings["Payment Entry"] = {
	onload(listview) {
		listview.page.add_inner_button(
			"Inter Branch Contra",
			() => open_ib_dialog(listview)
		).addClass("btn-primary");
	}
};

function open_ib_dialog(listview) {
	const dialog = new frappe.ui.Dialog({
		title: "Create Inter-branch Contra",
		size: "extra-large",
		fields: [
			{
				fieldname: "company",
				label: "Company",
				fieldtype: "Link",
				options: "Company",
				default: frappe.defaults.get_user_default("Company"),
				reqd: 1,
				onchange() {
					_reset_company_dependents(dialog);
				}
			},
			{
				fieldtype: "Column Break",
				fieldname: "column_break_0"
			},
			{
				fieldname: "section_break_0",
				fieldtype: "Section Break",
			},
			{
				fieldname: "source_branch",
				label: "Source Branch",
				fieldtype: "Link",
				options: "Branch",
				default: frappe.defaults.get_user_default("Branch"),
				reqd: 1,
				onchange() {
					_guard_same_branch(dialog);
				}
			},
			{
				fieldname: "source_bank",
				label: "Source Bank Account",
				fieldtype: "Link",
				options: "Account",
				reqd: 1,
				get_query: () => {
					return {filters: {account_type: "Bank", is_group: 0}};
				}
			},
			{
				fieldtype: "Column Break",
				fieldname: "column_break_1"
			},
			{
				fieldname: "target_branch",
				label: "Target Branch",
				fieldtype: "Link",
				options: "Branch",
				reqd: 1,
				onchange() {
					_guard_same_branch(dialog);
				}
			},
			{
				fieldname: "target_bank",
				label: "Target Bank Account",
				fieldtype: "Link",
				options: "Account",
				reqd: 1,
				get_query: () => {
					return {filters: {account_type: "Bank", is_group: 0}};
				}
			},
			{
				fieldtype: "Section Break",
				fieldname: "section_break_1"
			},
			{
				fieldname: "amount",
				label: "Amount",
				fieldtype: "Currency",
				reqd: 1
			},
			{
				fieldname: "posting_date",
				label: "Posting Date",
				fieldtype: "Date",
				reqd: 1,
				default: frappe.datetime.get_today()
			},
			{
				fieldtype: "Column Break",
				fieldname: "column_break_2"
			},
			{
				fieldname: "reference_no",
				label: "Reference No",
				fieldtype: "Data"
			},
			{
				fieldname: "reference_date",
				label: "Reference Date",
				fieldtype: "Date"
			},
			{
				fieldtype: "Section Break",
				fieldname: "section_break_2"
			},
			{
				fieldname: "remarks",
				label: "Remarks",
				fieldtype: "Small Text"
			}
		],
		primary_action_label: "Create",
		primary_action(values) {
			if (values.source_branch === values.target_branch) {
				frappe.msgprint({
					message: __("Source and Target Branch cannot be the same."),
					indicator: "red"
				});
				return;
			}
			if (!values.amount || Number(values.amount) <= 0) {
				frappe.msgprint({
					message: __("Amount must be greater than zero."),
					indicator: "red"
				});
				return;
			}

			frappe.call({
				method: "jewellery_erpnext.interbranch.create_inter_branch_contra_entry",
				args: values,
				freeze: true,
				freeze_message: __("Creating inter-branch contra entries..."),
				callback(r) {
					dialog.hide();
					const names = r.message || [];
					if (Array.isArray(names) && names.length) {
						const links = names
							.map(n => `<a href="/app/journal-entry/${frappe.utils.escape_html(n)}">${frappe.utils.escape_html(n)}</a>`)
							.join(", ");
						frappe.msgprint(`Created Journal Entries: ${links}`);
					} else {
						frappe.msgprint("No Journal Entries were created.");
					}
					listview.refresh();
				}
			});
		}
	});

	dialog.show();
}

function _guard_same_branch(dialog) {
	// const v = dialog.get_values();
	let source_branch = dialog.get_value("source_branch");
	let target_branch = dialog.get_value("target_branch");

	if (!source_branch || !target_branch) {
		return;
	}
	if (source_branch && target_branch && source_branch === target_branch) {
		frappe.show_alert({
			message: __("Source and Target Branch cannot be the same."),
			indicator: "red"
		}, 5);
	}
}
