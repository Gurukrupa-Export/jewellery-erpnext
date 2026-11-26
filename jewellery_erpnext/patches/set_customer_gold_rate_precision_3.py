"""Customer Gold rate fields on ``Stock Entry`` show 3 decimals, like the rest of the flow.

``add_customer_gold_rate_snapshot_fields`` and ``add_customer_gold_rate_check_fields`` created
the rate fields at precision 4. Both now say 3, but a patch runs once, so this re-runs them:
``create_custom_fields`` updates the existing Custom Fields in place, which also narrows the
columns to ``decimal(21,3)``.

On kggk-uat (25 Nov 2026), every stored raw rate, per-gram rate and reference rate already had
at most 3 decimals. 51 ``custom_gold_rate_check_ratio`` values (the outlier check's ratio) lose
a 4th decimal, changing by at most 0.0005.
"""

from jewellery_erpnext.patches import (
	add_customer_gold_rate_check_fields,
	add_customer_gold_rate_snapshot_fields,
)


def execute():
	add_customer_gold_rate_snapshot_fields.execute()
	add_customer_gold_rate_check_fields.execute()
