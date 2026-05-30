import logging
import time

from odoo import api, models

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    @api.model
    def _cron_cleanup_archived(self):
        ICP = self.env['ir.config_parameter'].sudo()
        batch = int(ICP.get_param('product_archived_cleanup.batch_size', 1000))
        budget = int(ICP.get_param('product_archived_cleanup.time_budget', 240))
        start_ts = time.time()

        P = self.with_context(active_test=False)

        # Every column that FK-references product_product.id -> referenced variants
        # cannot be deleted. The table/column identifiers come from the Postgres
        # catalog (never user input), so the string interpolation below is safe.
        self.env.cr.execute("""
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND ccu.table_name = 'product_product' AND ccu.column_name = 'id'
        """)
        blocked_variants = set()
        for tbl, col in self.env.cr.fetchall():
            if tbl == 'product_product':
                continue
            self.env.cr.execute(
                'SELECT DISTINCT "%s" FROM "%s" WHERE "%s" IS NOT NULL' % (col, tbl, col))
            blocked_variants.update(r[0] for r in self.env.cr.fetchall())

        blocked_tmpl = set()
        if blocked_variants:
            self.env.cr.execute(
                "SELECT DISTINCT product_tmpl_id FROM product_product WHERE id IN %s",
                (tuple(blocked_variants),))
            blocked_tmpl = {r[0] for r in self.env.cr.fetchall()}

        deletable = list(set(P.search([('active', '=', False)]).ids) - blocked_tmpl)
        if not deletable:
            _logger.info("Archived cleanup: nothing deletable remaining")
            return

        done = 0
        for i in range(0, len(deletable), batch):
            if time.time() - start_ts > budget:
                _logger.info("Archived cleanup: time budget hit at %s, resuming next run", done)
                break
            chunk = deletable[i:i + batch]
            try:
                with self.env.cr.savepoint():
                    P.browse(chunk).unlink()
                done += len(chunk)
            except Exception:
                for pid in chunk:
                    try:
                        with self.env.cr.savepoint():
                            P.browse(pid).unlink()
                        done += 1
                    except Exception:
                        pass
            self.env.cr.commit()
        _logger.info("Archived cleanup: deleted %s this run", done)