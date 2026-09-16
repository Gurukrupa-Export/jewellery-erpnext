"""One definition of how many pieces a BOM is hallmarked as.

``BOM.hallmarking_amount`` is a whole-BOM line total: it sits in the same sum as
``making_charge`` and ``gold_bom_amount`` everywhere a BOM is priced, so for an Earrings
BOM -- which is the PAIR -- it carries the charge for both pieces.

Hallmarking is billed per piece though, so the e-invoice hallmarking line reports that one
amount against a piece COUNT and shows ``amount / qty`` as the rate. Three places build
that line (``sales_order``, ``sales_invoice``, ``delivery_note``) and they used to disagree:
only ``sales_order`` counted an Earrings BOM as two. They all read the count from here now.
"""

HALLMARKING_PIECE_CATEGORY = "Earrings"
HALLMARKING_PIECES_PER_UNIT = 2


def hallmarking_pieces(bom):
	"""How many hallmarked PIECES one BOM stands for: 2 for a pair, 1 otherwise.

	Use it anywhere ``hallmarking_amount`` is split into (amount, qty). Do NOT use it where
	the amount is folded into a whole-BOM rate (``purchase_order.update_rate``, the
	``sales_invoice`` line rates) -- there the pair total is already the right figure and
	dividing it would under-bill the pair.
	"""
	category = getattr(bom, "item_category", None)
	return HALLMARKING_PIECES_PER_UNIT if category == HALLMARKING_PIECE_CATEGORY else 1
