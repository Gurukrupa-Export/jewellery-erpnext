"""Give REF-SVC-001 the weight UOMs the external refining Purchase Order now needs.

The service line's qty used to be a piece count (1 Nos) with the whole charge in ``rate``;
it is now the summed material weight with a per-unit rate, so the line's ``uom`` is
Gram / Litre / Carat depending on what was sent.

Delegates to ``seed_refining_masters._ensure_service_item_uoms`` so the definition lives in
one place. A NEW patches.txt entry is required because ``seed_refining_masters`` is long
since marked complete on production.

    bench --site <site> execute jewellery_erpnext.patches.ensure_refining_service_item_uoms.execute
"""

from jewellery_erpnext.patches.seed_refining_masters import _ensure_service_item_uoms


def execute():
	_ensure_service_item_uoms()
