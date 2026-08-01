"""Website-facing search over the IC ProductInformation cache.

The cache holds ~2M rows, so the customer search path must never scan
the table. Identifier-style queries (IC index, TecDoc/article number,
EAN) are answered from *normalised key* columns — upper-cased with
separators stripped, so "op 520", "OP520" and "OP 520" all hit the
same key — backed by ``varchar_pattern_ops`` indexes that serve both
equality and prefix lookups. The keys are rebuilt with one SQL UPDATE
right after each CSV COPY (the loader bypasses the ORM, so computed
fields cannot do this).

Materialisation reuses the ic_intercars machinery unchanged: live IC
quote first (never a product at price 0), then
``_baf_find_or_create_ic``; the only website-specific step is
publishing the template so the visitor can actually open it.
"""

import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Keep customer queries bounded: identifiers are never this long and
# unbounded LIKE patterns are a cheap way to hurt the database.
_MAX_QUERY_LEN = 64


def _normalize(query):
    """'61 13 1 359 287' → '61131359287'; 'op-520' → 'OP520'."""
    return re.sub(r'[^A-Za-z0-9]', '', query or '').upper()[:_MAX_QUERY_LEN]


class IcProductInfo(models.Model):
    _inherit = 'ic.product.info'

    # Normalised lookup keys — written by SQL in _baf_refresh_norm_keys,
    # never by the ORM (the CSV loader COPYes around it anyway).
    norm_index = fields.Char(string="Index (normalised)", readonly=True)
    norm_article = fields.Char(string="Article Nr (normalised)",
                               readonly=True)
    norm_tecdoc = fields.Char(string="TecDoc (normalised)", readonly=True)
    norm_barcodes = fields.Char(string="EANs (normalised)", readonly=True)

    def init(self):
        super().init()
        # varchar_pattern_ops serves both `=` and left-anchored LIKE;
        # the default-collation indexes Odoo creates do not help LIKE.
        for column in ('norm_index', 'norm_article', 'norm_tecdoc',
                       'norm_barcodes'):
            self.env.cr.execute(
                f"CREATE INDEX IF NOT EXISTS ic_product_info_{column}_pat "
                f"ON ic_product_info ({column} varchar_pattern_ops)"
            )

    @api.model
    def bulk_load_csv(self, csv_bytes, replace=True):
        stats = super().bulk_load_csv(csv_bytes, replace=replace)
        self._baf_refresh_norm_keys()
        return stats

    @api.model
    def _baf_refresh_norm_keys(self):
        """Rebuild the normalised key columns after a COPY load."""
        self.env.cr.execute("""
            UPDATE ic_product_info SET
                norm_index = NULLIF(regexp_replace(
                    upper(coalesce(ic_index, '')), '[^A-Z0-9]', '', 'g'), ''),
                norm_article = NULLIF(regexp_replace(
                    upper(coalesce(article_number, '')),
                    '[^A-Z0-9]', '', 'g'), ''),
                norm_tecdoc = NULLIF(regexp_replace(
                    upper(coalesce(tec_doc, '')), '[^A-Z0-9]', '', 'g'), ''),
                norm_barcodes = NULLIF(regexp_replace(
                    coalesce(barcodes, ''), '[^0-9,]', '', 'g'), '')
        """)
        self.env.cr.execute("ANALYZE ic_product_info")
        self.env.invalidate_all()

    # ── Customer search ─────────────────────────────────────────────────
    @api.model
    def _baf_website_search(self, query, limit=20):
        """Cache rows matching a customer query, exact hits first.

        Tiered so the common case stays on index-only plans:
        1. exact normalised identifier (index/article/TecDoc/IC SKU),
           plus exact EAN when the query is numeric;
        2. left-anchored prefix on the same keys;
        3. only if the identifier tiers found nothing and the query is
           word-like: a bounded ILIKE over the description columns.
        """
        norm = _normalize(query)
        if len(norm) < 3:
            return self.browse()

        found_ids = []

        def collect(sql, params, remaining):
            self.env.cr.execute(sql + " LIMIT %s", params + [remaining])
            for (row_id,) in self.env.cr.fetchall():
                if row_id not in found_ids:
                    found_ids.append(row_id)

        exact = ("SELECT id FROM ic_product_info "
                 "WHERE norm_index = %s OR norm_article = %s "
                 "OR norm_tecdoc = %s OR tow_kod = %s")
        params = [norm, norm, norm, norm]
        if norm.isdigit() and len(norm) >= 8:
            exact += " OR ',' || norm_barcodes || ',' LIKE %s"
            params.append(f'%,{norm},%')
        collect(exact, params, limit)

        if len(found_ids) < limit:
            like = norm + '%'
            collect(
                "SELECT id FROM ic_product_info "
                "WHERE (norm_index LIKE %s OR norm_article LIKE %s "
                "OR norm_tecdoc LIKE %s OR norm_barcodes LIKE %s) "
                "ORDER BY tow_kod",
                [like, like, like, like],
                limit - len(found_ids),
            )

        if not found_ids and len((query or '').strip()) >= 4:
            words = (query or '').strip()[:_MAX_QUERY_LEN]
            collect(
                "SELECT id FROM ic_product_info "
                "WHERE short_description ILIKE %s OR manufacturer ILIKE %s",
                [f'%{words}%', f'%{words}%'],
                limit,
            )

        return self.browse(found_ids)

    # ── On-demand materialisation ────────────────────────────────────────
    def _baf_materialize_for_website(self):
        """Turn one cache row into a live, *published* shop product.

        Quote first: IC declining to quote (discontinued, not orderable
        on this account) raises before anything is created — the shop
        must never show a product it cannot price. Existing products
        are refreshed and re-published rather than duplicated.
        """
        self.ensure_one()
        backend = self.env['ic.backend']._get_default()
        if not backend:
            raise UserError(_(
                "The Inter Cars catalog is not available right now."
            ))
        Product = self.env['product.product']
        price_net = Product._baf_ic_live_cost(backend, self.tow_kod)
        variant = Product._baf_find_or_create_ic(
            backend, self._to_ic_data(), price_net=price_net,
        )
        template = variant.product_tmpl_id
        if 'is_published' in template._fields and not template.is_published:
            template.sudo().write({'is_published': True})
        return variant
