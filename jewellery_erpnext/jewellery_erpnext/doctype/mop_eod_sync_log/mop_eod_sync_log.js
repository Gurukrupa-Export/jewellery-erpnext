// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("MOP EOD Sync Log", {
	refresh(frm) {
		_setup_status_indicator(frm);
		_setup_progress_html(frm);
		_setup_buttons(frm);
		_maybe_start_polling(frm);
	},
});

function _setup_status_indicator(frm) {
	const status_map = {
		Running: ["orange", "EOD Sync Running — Transactions blocked"],
		Queued: ["blue", "EOD Sync Queued — Waiting in queue"],
		Completed: ["green", "EOD Sync Completed"],
		"Partially Completed": ["yellow", "EOD Sync Partially Completed — Some items failed"],
		Failed: ["red", "EOD Sync Failed — Check Error Log"],
		"Timeout Released": ["yellow", "EOD Sync lock expired and auto-released"],
	};
	const s = status_map[frm.doc.status];
	if (s) {
		frm.dashboard.set_headline_alert(s[1], s[0]);
	}
}

function _setup_progress_html(frm) {
	const d = frm.doc;
	if (!d.total_qty && !d.eligible_qty) return;

	const eligible = d.eligible_qty || 0;
	const pct = d.progress_percent || 0;

	const bar_color =
		d.status === "Completed"
			? "success"
			: d.status === "Failed"
			? "danger"
			: d.status === "Partially Completed"
			? "warning"
			: "primary";

	const html = `
<div class="eod-progress-box" style="padding:12px 0;">
  <div style="margin-bottom:6px;">
    <strong>EOD Sync Progress</strong>
    <span class="badge badge-${bar_color}" style="margin-left:8px;">${pct.toFixed(1)}%</span>
  </div>
  <div class="progress" style="height:18px;margin-bottom:10px;">
    <div class="progress-bar bg-${bar_color}" role="progressbar"
         style="width:${pct}%;transition:width 0.4s ease;"
         aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
    </div>
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:16px;font-size:13px;">
    <span><b>Eligible Qty:</b> ${frappe.format(eligible, { fieldtype: "Float", precision: 3 })}</span>
    <span style="color:#28a745;"><b>Synced:</b> ${frappe.format(d.synced_qty || 0, {
		fieldtype: "Float",
		precision: 3,
	})}</span>
    <span style="color:#dc3545;"><b>Failed:</b> ${frappe.format(d.failed_qty || 0, {
		fieldtype: "Float",
		precision: 3,
	})}</span>
    <span style="color:#6c757d;"><b>Unsynced:</b> ${frappe.format(d.unsynced_qty || 0, {
		fieldtype: "Float",
		precision: 3,
	})}</span>
    <span style="color:#fd7e14;"><b>Excluded:</b> ${frappe.format(d.excluded_qty || 0, {
		fieldtype: "Float",
		precision: 3,
	})}</span>
  </div>
  ${
		d.progress_message
			? `<div style="margin-top:6px;color:#555;font-size:12px;">${d.progress_message}</div>`
			: ""
  }
</div>`;

	frm.dashboard.add_indicator(__("Progress"), bar_color);
	// Render in the form intro area
	frm.set_intro(html, false);
}

function _setup_buttons(frm) {
	frm.add_custom_button(__("Refresh Progress"), () => frm.reload_doc());

	if (frm.doc.submitted_stock_entries) {
		frm.add_custom_button(
			__("Open Submitted Stock Entries"),
			() => {
				const names = frm.doc.submitted_stock_entries
					.split(",")
					.map((s) => s.trim())
					.filter(Boolean);
				if (names.length === 1) {
					frappe.set_route("Form", "Stock Entry", names[0]);
				} else {
					frappe.set_route("List", "Stock Entry", { name: ["in", names] });
				}
			},
			__("Actions")
		);
	}

	if (frm.doc.draft_stock_entries) {
		frm.add_custom_button(
			__("Open Draft Stock Entries"),
			() => {
				const names = frm.doc.draft_stock_entries
					.split(",")
					.map((s) => s.trim())
					.filter(Boolean);
				if (names.length === 1) {
					frappe.set_route("Form", "Stock Entry", names[0]);
				} else {
					frappe.set_route("List", "Stock Entry", { name: ["in", names] });
				}
			},
			__("Actions")
		);
	}
}

function _maybe_start_polling(frm) {
	const active = ["Queued", "Running"];
	if (!active.includes(frm.doc.status)) return;

	// Poll every 5 seconds while status is active
	if (frm._eod_poll_timer) clearTimeout(frm._eod_poll_timer);
	frm._eod_poll_timer = setTimeout(() => {
		frappe.db.get_value("MOP EOD Sync Log", frm.doc.name, "status").then((r) => {
			const new_status = r && r.message && r.message.status;
			if (active.includes(new_status)) {
				frm.reload_doc();
			} else {
				frm.reload_doc();
			}
		});
	}, 5000);
}
