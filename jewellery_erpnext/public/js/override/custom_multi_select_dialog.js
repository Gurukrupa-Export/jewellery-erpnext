/**
 * CustomMultiSelectDialog - Overrides MultiSelectDialog to enhance pagination.
 * Loads 150 records initially and adds a "More" button with selectable increments (20, 40, 60, 100).
 * Applies to both main and child results in the dialog.
 */

frappe.ui.form.CustomMultiSelectDialog = class CustomMultiSelectDialog extends frappe.ui.form.MultiSelectDialog {
    init() {
        super.init();
        this.page_length = 150;
        this.child_page_length = 150;
        this.get_results();
    }

    make() {
        this.dialog = new frappe.ui.Dialog({
            title: __("Select {0}", [this.for_select ? __("value") : __(this.doctype)]),
            fields: this.fields,
            size: this.size,
            primary_action_label: this.primary_action_label || __("Get Items"),
            secondary_action_label: __("Make {0}", [__(this.doctype)]),
            primary_action: () => {
                let filters_data = this.get_custom_filters();
                const data_values = cur_dialog.get_values();
                const filtered_children = this.get_selected_child_names();
                const selected_documents = [...this.get_checked_values(), ...this.get_parent_name_of_selected_children()];
                this.action(selected_documents, { ...this.args, ...data_values, ...filters_data, filtered_children });
            },
            secondary_action: this.make_new_document.bind(this),
        });
        if (this.add_filters_group) this.make_filter_area();
        this.args = {};
        this.setup_results();
        this.bind_events();
        this.dialog.show();
    }

    get_result_fields() {
        const page_length_options = [20, 40, 60, 100];
        const show_next_page = () => {
            this.page_length += parseInt(this.dialog.fields_dict.page_length_selector.get_value()) || 20;
            this.get_results();
        };

        return [
            { fieldtype: "HTML", fieldname: "results_area" },
            {
                fieldtype: "Select",
                fieldname: "page_length_selector",
                label: __("Page Size"),
                options: page_length_options.map(opt => ({ label: opt, value: opt })),
                default: 20,
                description: __("Select how many records to load when clicking More")
            },
            { fieldtype: "Button", fieldname: "more_btn", label: __("More"), click: show_next_page.bind(this) },
        ];
    }

    get_child_selection_fields() {
        const page_length_options = [20, 40, 60, 100];
        const show_more_child_results = () => {
            this.child_page_length += parseInt(this.dialog.fields_dict.child_page_length_selector.get_value()) || 20;
            this.show_child_results();
        };

        const fields = [];
        if (this.allow_child_item_selection && this.child_fieldname) {
            fields.push({ fieldtype: "HTML", fieldname: "child_selection_area" });
            fields.push({
                fieldtype: "Select",
                fieldname: "child_page_length_selector",
                label: __("Child Page Size"),
                options: page_length_options.map(opt => ({ label: opt, value: opt })),
                default: 20,
            });
            fields.push({
                fieldtype: "Button",
                fieldname: "more_child_btn",
                hidden: 1,
                label: __("More"),
                click: show_more_child_results.bind(this),
            });
        }
        return fields;
    }
};

frappe.ui.form.MultiSelectDialog = frappe.ui.form.CustomMultiSelectDialog;