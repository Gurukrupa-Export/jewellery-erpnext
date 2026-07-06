// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("MOP Settings", {
	refresh(frm) {
		_prefill_eod_sync_window(frm);
		_setup_eod_status_indicator(frm);
		_setup_eod_sync_button(frm);
		_apply_permission_restrictions(frm);
		_setup_eod_sync_log_progress(frm);
		_setup_open_sync_log_button(frm);
	},
});

// Pre-fill the manual EOD Sync window with today's start/end so the user always sees
// the effective default. A field is reset only when blank or holding a value from a
// previous day; a same-day custom edit is preserved. Values are set on frm.doc (not via
// set_value) so the form is not marked dirty.
function _prefill_eod_sync_window(frm) {
	const today = frappe.datetime.get_today();
	const defaults = {
		eod_sync_from_datetime: today + " 00:00:00",
		eod_sync_to_datetime: today + " 23:59:59",
	};
	Object.entries(defaults).forEach(([field, default_value]) => {
		const cur = frm.doc[field];
		const cur_date = cur ? String(cur).slice(0, 10) : null;
		if (!cur || cur_date < today) {
			frm.doc[field] = default_value;
			frm.refresh_field(field);
		}
	});
}

function _is_system_manager() {
	return frappe.user_roles && frappe.user_roles.includes("System Manager");
}

function _is_eod_locked(frm) {
	if (frm.doc.eod_sync_running) return true;
	if (frm.doc.eod_sync_lock_until) {
		const lock_until = frappe.datetime.str_to_obj(frm.doc.eod_sync_lock_until);
		if (lock_until && lock_until > new Date()) return true;
	}
	return false;
}

function _setup_eod_status_indicator(frm) {
	frm.dashboard.clear_headline();
	const status = frm.doc.eod_sync_status;
	if (frm.doc.eod_sync_running || status === "Running") {
		frm.dashboard.set_headline_alert(
			__("EOD Sync Running — Transactions are temporarily blocked"),
			"orange"
		);
	} else if (status === "Queued") {
		frm.dashboard.set_headline_alert(
			__("EOD Sync Queued — Job is waiting in the background queue"),
			"blue"
		);
	} else if (status === "Failed") {
		frm.dashboard.set_headline_alert(
			__("EOD Sync Failed — Check the Last EOD Error Log field below"),
			"red"
		);
	} else if (status === "Timeout Released") {
		frm.dashboard.set_headline_alert(
			__("EOD Sync lock expired and was auto-released — verify draft Stock Entries"),
			"yellow"
		);
	} else if (status === "Completed") {
		frm.dashboard.set_headline_alert(__("EOD Sync Idle"), "green");
	}
}

function _setup_eod_sync_button(frm) {
	const $btn = frm.fields_dict.sync_mop_log && frm.fields_dict.sync_mop_log.$input;
	if (!$btn) return;

	// Remove any previously attached handlers to prevent duplicate fires on each refresh.
	$btn.off("click.eod_sync").on("click.eod_sync", function () {
		if (!_is_system_manager()) {
			frappe.msgprint({
				title: __("Permission Denied"),
				message: __("Only System Manager can manually start EOD Sync."),
				indicator: "red",
			});
			return;
		}
		if (_is_eod_locked(frm)) {
			frappe.msgprint({
				title: __("EOD Sync In Progress"),
				message: __(
					"EOD sync is in progress. You cannot proceed to make any transactions. " +
						"Please contact your administrator or try again after 2 hours after the specified time."
				),
				indicator: "orange",
			});
			return;
		}
		frappe.call({
			method: "sync_mop_log",
			doc: frm.doc,
			callback: function () {
				frappe.show_alert({
					message: __(
						"EOD MOP Log Sync has been queued. " +
							"Transactions will be blocked while the sync is running."
					),
					indicator: "blue",
				});
				frm.reload_doc();
			},
		});
	});
}

function _apply_permission_restrictions(frm) {
	const is_sm = _is_system_manager();
	const is_locked = _is_eod_locked(frm);

	// Non-System Manager cannot change EOD Sync Time, the manual From/To window, or the filter table
	if (!is_sm) {
		[
			"eod_sync_time",
			"eod_sync_from_datetime",
			"eod_sync_to_datetime",
			"eod_sync_work_order_filter",
		].forEach((field) => {
			frm.set_df_property(field, "read_only", 1);
			frm.refresh_field(field);
		});
	}

	// Disable the EOD Sync button for non-SM or when lock is active
	const $btn = frm.fields_dict.sync_mop_log && frm.fields_dict.sync_mop_log.$input;
	if ($btn) {
		const should_disable = !is_sm || is_locked;
		$btn.prop("disabled", should_disable);
		if (should_disable) {
			$btn.attr(
				"title",
				is_locked
					? __("EOD sync is currently running or locked.")
					: __("Only System Manager can trigger EOD Sync.")
			);
		} else {
			$btn.removeAttr("title");
		}
	}
}

// ─── EOD Sync Log progress display ───────────────────────────────────────────

let _eod_poll_timer = null;

function _setup_eod_sync_log_progress(frm) {
	if (_eod_poll_timer) {
		clearTimeout(_eod_poll_timer);
		_eod_poll_timer = null;
	}

	const sync_log_name = frm.doc.eod_sync_last_sync_log;
	if (!sync_log_name) return;

	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.mop_eod_sync_log.mop_eod_sync_log.get_latest_eod_sync_progress",
		args: { sync_log_name },
		callback(r) {
			if (r.exc || !r.message) return;
			const d = r.message;
			_render_progress_html(frm, d, sync_log_name);
			const active_statuses = ["Queued", "Running"];
			if (active_statuses.includes(d.status)) {
				_eod_poll_timer = setTimeout(() => _setup_eod_sync_log_progress(frm), 5000);
			}
		},
	});
}

function _render_progress_html(frm, d, sync_log_name) {
	const eligible = d.eligible_qty || 0;
	const pct = d.progress_percent || 0;
	const status = d.status || "";

	const bar_color =
		status === "Completed"
			? "success"
			: status === "Failed"
			? "danger"
			: status === "Partially Completed"
			? "warning"
			: ["Queued", "Running"].includes(status)
			? "primary"
			: "secondary";

	const f = (v) => frappe.format(v || 0, { fieldtype: "Float", precision: 3 });
	const link = `/desk/mop-eod-sync-log/${encodeURIComponent(sync_log_name)}`;

	const html = `
<div style="background:#f9f9f9;border:1px solid #ddd;border-radius:6px;padding:12px 16px;margin:8px 0;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
    <b>Latest EOD Sync: <a href="${link}" target="_blank">${sync_log_name}</a></b>
    <span class="badge badge-${bar_color}" style="font-size:12px;">${frappe.utils.escape_html(status)}</span>
  </div>
  <div class="progress" style="height:14px;margin-bottom:8px;border-radius:3px;">
    <div class="progress-bar bg-${bar_color}" role="progressbar"
         style="width:${Math.min(pct, 100)}%;transition:width 0.5s ease;"
         aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"></div>
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:14px;font-size:12px;">
    <span><b>Eligible Qty:</b> ${f(eligible)}</span>
    <span style="color:#28a745;"><b>Synced:</b> ${f(d.synced_qty)}</span>
    <span style="color:#dc3545;"><b>Failed:</b> ${f(d.failed_qty)}</span>
    <span style="color:#6c757d;"><b>Unsynced:</b> ${f(d.unsynced_qty)}</span>
    <span style="color:#fd7e14;"><b>Excluded:</b> ${f(d.excluded_qty)}</span>
    <span><b>${pct.toFixed(1)}%</b></span>
  </div>
  ${
		d.progress_message
			? `<div style="margin-top:5px;color:#666;font-size:11px;">${frappe.utils.escape_html(
					d.progress_message
			  )}</div>`
			: ""
  }
</div>`;

	const field = frm.fields_dict["eod_sync_message"];
	if (field && field.$wrapper) {
		let $box = field.$wrapper.find(".eod-progress-inject");
		if (!$box.length) {
			$box = $("<div class='eod-progress-inject'></div>").prependTo(field.$wrapper);
		}
		$box.html(html);
	}
}

function _setup_open_sync_log_button(frm) {
	if (frm.doc.eod_sync_last_sync_log) {
		frm.add_custom_button(__("Open Latest EOD Sync Log"), () => {
			frappe.set_route("Form", "MOP EOD Sync Log", frm.doc.eod_sync_last_sync_log);
		});
	}
}
