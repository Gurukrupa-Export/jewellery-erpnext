"""Index the MOP EOD Sync window filter on tabMOP Log.

The daily sync reads ``WHERE is_synced = 0 AND is_cancelled = 0 AND creation BETWEEN
<window>`` (mop_eod_sync._get_unsynced_mop_groups), and counts the pre-window backlog with
``creation < <window start>``. The existing ``mop_eod_sync_idx`` is
(is_synced, is_cancelled, manufacturing_work_order), which does not cover ``creation``, so
both queries read every unsynced row (~647k on GEPL) to find today's handful. This index
lets the window query read only the window.
"""

from jewellery_erpnext.patches.add_mop_eod_sync_fields_and_indexes import (
	_add_index_if_missing,
)


def execute():
	_add_index_if_missing(
		"tabMOP Log",
		"mop_eod_window_idx",
		("is_synced", "is_cancelled", "creation"),
	)
