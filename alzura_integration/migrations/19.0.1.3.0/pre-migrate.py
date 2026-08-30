"""Remove the ir.ui.view records that used to expose alzura_internal_number.

Version 1.3.0 dropped both the field and the view file that referenced it, but
the ir.ui.view rows from previous deployments stay in the database until the
owning module upgrade cleans them up. Any other module that inherits
sale.view_order_form (b2b_custom's form view, for one) is validated before
alzura_integration's own cleanup runs, and the combined arch still merges
these stale views in -- which then fails because the field is gone from the
model.

Deleting the records here, before any view validation happens for the new
version, is what lets the upgrade go through.
"""

import logging

_logger = logging.getLogger(__name__)

STALE_VIEW_XMLIDS = (
    "alzura_integration.sale_order_view_form_alzura",
    "alzura_integration.sale_order_view_tree_alzura",
    "alzura_integration.sale_order_view_search_alzura",
)


def migrate(cr, version):
    cr.execute(
        """
        DELETE FROM ir_ui_view
              WHERE id IN (
                    SELECT res_id
                      FROM ir_model_data
                     WHERE model = 'ir.ui.view'
                       AND module || '.' || name IN %s
              )
        """,
        (STALE_VIEW_XMLIDS,),
    )
    removed = cr.rowcount
    cr.execute(
        """
        DELETE FROM ir_model_data
              WHERE model = 'ir.ui.view'
                AND module || '.' || name IN %s
        """,
        (STALE_VIEW_XMLIDS,),
    )
    if removed:
        _logger.info(
            "Alzura: removed %s stale sale.order view(s) that referenced the "
            "dropped alzura_internal_number field.",
            removed,
        )
