"""Move the Alzura order number onto customer_po and drop the internal number.

The Alzura order number ("POE...") is what buyers quote when they contact us,
so it becomes the Customer PO Number of the imported order — and, since the
import no longer writes b2b_so, the key the re-import de-dup matches on. Every
already-imported order therefore has to carry it in customer_po before the next
cron run, or those orders would be imported a second time.

b2b_so is cleared on the moved orders: it is no longer written by the import
and its value now lives in customer_po. The alzura_internal_number column is
dropped; orders that carry it as their customer reference keep that value, it
is simply no longer stored in a field of its own.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # Early imports predate the so_source stamp but are still recognisable by
    # their order number; a manually typed B2B SO never looks like one.
    source_ids = env["sale.order"]._alzura_source_ids().ids
    cr.execute(
        """
        UPDATE sale_order
           SET customer_po = b2b_so,
               b2b_so = NULL
         WHERE b2b_so IS NOT NULL
           AND (so_source IN %s OR b2b_so LIKE 'POE%%')
        """,
        (tuple(source_ids) or (None,),),
    )
    moved_orders = cr.rowcount

    cr.execute("ALTER TABLE sale_order DROP COLUMN IF EXISTS alzura_internal_number")
    env["sale.order"].invalidate_model()

    _logger.info(
        "Alzura: moved the order number of %s order(s) from b2b_so to "
        "customer_po.",
        moved_orders,
    )
