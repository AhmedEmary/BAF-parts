"""Live IC catalog search — Odoo-side UI for /ic/catalog/products + friends.

BAF staff types a SKU / index / category, the wizard calls IC's live
catalog, then enriches each hit with a live price (``customerPriceNet``)
and warehouse availability. From each row an admin can populate
``ic_seed_sku`` on any BAF ``product.template`` — the "assign this
IC part to that OEM part" workflow — without leaving Odoo.

Uses the existing ``ic.backend.get_client()`` API client. No caching:
this is a deliberately live view, unlike the CSV-backed
``ic.product.info`` browser.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class IcCatalogSearchWizard(models.TransientModel):
    _name = 'ic.catalog.search.wizard'
    _description = 'Live Inter Cars Catalog Search'

    backend_id = fields.Many2one(
        'ic.backend', string="Backend", required=True,
        default=lambda self: self.env['ic.backend']._get_default(),
    )

    # ── Search inputs ────────────────────────────────────────────────────
    search_type = fields.Selection(
        [
            ('sku', 'By IC SKU'),
            ('index', 'By IC Index'),
            ('category', 'By IC Category'),
        ],
        default='sku', required=True,
        help="IC's public catalog only accepts SKU, index, or category "
             "as a lookup key. Free-text search is not exposed.",
    )
    search_value = fields.Char(
        string="Search Value", required=True,
        help="The exact value to look up. IC does not do fuzzy matches; "
             "if you're not sure of the spelling, use the local cache "
             "(Purchase → Configuration → IC Products) first.",
    )
    language = fields.Selection(
        [
            ('de', 'German'),  ('en', 'English'), ('pl', 'Polish'),
            ('cs', 'Czech'),   ('sk', 'Slovak'),  ('hu', 'Hungarian'),
            ('ro', 'Romanian'),('sl', 'Slovenian'),('hr', 'Croatian'),
            ('bs', 'Bosnian'), ('sr', 'Serbian'), ('bg', 'Bulgarian'),
            ('el', 'Greek'),   ('et', 'Estonian'),('lv', 'Latvian'),
            ('lt', 'Lithuanian'), ('uk', 'Ukrainian'),
        ],
        default='de', required=True,
        help="Language for the catalog Accept-Language header.",
    )
    page_size = fields.Integer(
        string="Max Results", default=25,
        help="1..100 per IC's contract. Anything higher is clamped.",
    )

    fetch_price = fields.Boolean(
        string="Include Live Prices", default=True,
        help="Call POST /ic/pricing/quote for the found SKUs. "
             "Adds one round-trip per search.",
    )
    fetch_stock = fields.Boolean(
        string="Include Live Availability", default=True,
        help="Call GET /ic/inventory/stock for the found SKUs. "
             "Adds one round-trip per search.",
    )

    # ── Results ──────────────────────────────────────────────────────────
    result_ids = fields.One2many(
        'ic.catalog.search.result', 'wizard_id', string="Results",
    )
    result_count = fields.Integer(
        string="Result Count", compute='_compute_result_count',
    )
    search_summary = fields.Text(
        string="Search Summary", readonly=True,
    )

    @api.depends('result_ids')
    def _compute_result_count(self):
        for w in self:
            w.result_count = len(w.result_ids)

    # ── Search action ────────────────────────────────────────────────────
    def action_search(self):
        self.ensure_one()
        if not self.backend_id:
            raise UserError(_(
                "No active Inter Cars backend. Configure one under "
                "Purchase → Configuration → Inter Cars."
            ))
        if not (self.search_value or '').strip():
            raise UserError(_("Enter a value to search for."))

        client = self.backend_id.get_client()

        # 1) Catalog lookup
        kwargs = {'page_size': max(1, min(self.page_size or 25, 100)),
                  'language': self.language}
        if self.search_type == 'sku':
            kwargs['sku'] = self.search_value.strip()
        elif self.search_type == 'index':
            kwargs['index'] = self.search_value.strip()
        else:  # category
            kwargs['category_id'] = self.search_value.strip()

        try:
            res = client.get_catalog_products(**kwargs)
        except UserError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UserError(_(
                "Inter Cars catalog call failed: %s"
            ) % exc)

        products = res.get('products') or []
        skus = [p['sku'] for p in products if p.get('sku')]

        # 2) Live prices (optional)
        price_map = {}
        if self.fetch_price and skus:
            try:
                pq = client.get_price(
                    lines=[{'sku': s, 'quantity': 1} for s in skus[:100]],
                    ship_to=self.backend_id.ship_to or None,
                )
                for line in (pq.get('lines') or []):
                    if line.get('sku') and line.get('price'):
                        price_map[line['sku']] = line['price']
            except Exception as exc:  # noqa: BLE001
                _logger.warning("IC live search: price fetch failed: %s", exc)

        # 3) Live stock (optional)
        stock_map = {}
        if self.fetch_stock and skus:
            try:
                stock_rows = client.get_stock(
                    skus=skus[:100],
                    ship_to=self.backend_id.ship_to or None,
                )
                for row in stock_rows or []:
                    sku = row.get('sku')
                    if not sku:
                        continue
                    stock_map.setdefault(sku, 0)
                    stock_map[sku] += int(row.get('availability') or 0)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("IC live search: stock fetch failed: %s", exc)

        # 4) Materialise rows
        self.result_ids = [(5,)]  # wipe previous run
        rows = []
        for p in products:
            sku = p.get('sku')
            generic = ''
            for ref in (p.get('genericArticleReferences') or []):
                if ref.get('primary') and ref.get('genericArticleId'):
                    generic = str(ref['genericArticleId'])
                    break
                if not generic and ref.get('genericArticleId'):
                    generic = str(ref['genericArticleId'])
            price = price_map.get(sku) or {}
            eans = p.get('eans') or []
            rows.append((0, 0, {
                'tow_kod': sku,
                'ic_index': p.get('index') or '',
                'tec_doc': p.get('tecDoc') or '',
                'manufacturer': p.get('brand') or '',
                'article_number': p.get('articleNumber') or '',
                'short_description': p.get('shortDescription') or '',
                'description': p.get('description') or '',
                'generic_article_id': generic,
                'barcodes': ','.join(eans),
                'customer_price_net': price.get('customerPriceNet') or 0.0,
                'list_price_net': price.get('listPriceNet') or 0.0,
                'currency_code': price.get('currencyCode') or 'EUR',
                'availability': stock_map.get(sku, 0),
                'has_stock_data': sku in stock_map,
                'has_price_data': sku in price_map,
            }))
        self.result_ids = rows

        self.search_summary = _(
            "IC catalog returned %(n)d products for %(k)s=%(v)s "
            "(totalResults reported by IC: %(t)s, processingTime %(pt)s ms). "
            "Prices fetched: %(pr)s. Stock fetched: %(st)s."
        ) % {
            'n': len(products),
            'k': self.search_type,
            'v': self.search_value,
            't': res.get('totalResults', '?'),
            'pt': res.get('requestProcessingTime', '?'),
            'pr': len(price_map), 'st': len(stock_map),
        }

        # Reopen the same wizard so results are displayed.
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'context': self.env.context,
        }


class IcCatalogSearchResult(models.TransientModel):
    _name = 'ic.catalog.search.result'
    _description = 'Live IC Catalog Search Result'
    _order = 'customer_price_net'

    wizard_id = fields.Many2one(
        'ic.catalog.search.wizard', ondelete='cascade', required=True,
    )
    tow_kod = fields.Char(string="IC SKU", readonly=True)
    ic_index = fields.Char(string="IC Index", readonly=True)
    tec_doc = fields.Char(string="TecDoc ArtNr", readonly=True)
    article_number = fields.Char(string="Article Number", readonly=True)
    manufacturer = fields.Char(string="Brand", readonly=True)
    short_description = fields.Char(
        string="Description", readonly=True,
    )
    description = fields.Text(string="Full Description", readonly=True)
    generic_article_id = fields.Char(
        string="Generic Article ID", readonly=True,
        help="TecDoc genericArticleId — shared across true equivalents.",
    )
    barcodes = fields.Char(string="EANs", readonly=True)

    customer_price_net = fields.Float(
        string="BAF Cost (net)", readonly=True,
        digits='Product Price',
        help="Live customerPriceNet from /ic/pricing/quote — what BAF "
             "pays IC. The customer-visible price uses BAF's markup.",
    )
    list_price_net = fields.Float(
        string="List Price (net)", readonly=True,
        digits='Product Price',
    )
    currency_code = fields.Char(string="Currency", readonly=True)
    availability = fields.Integer(
        string="Available", readonly=True,
        help="Live availability capped at IC's stock_cap (usually 10).",
    )
    has_price_data = fields.Boolean(readonly=True)
    has_stock_data = fields.Boolean(readonly=True)

    # ── Per-row actions ──────────────────────────────────────────────────
    def action_assign_to_baf_template(self):
        """Open a small picker: choose a BAF template, set its
        ``ic_seed_sku`` to this row's ``tow_kod``."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Assign to BAF Template"),
            'res_model': 'ic.assign.to.baf.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_ic_sku': self.tow_kod,
                'default_ic_manufacturer': self.manufacturer,
                'default_ic_description': self.short_description,
            },
        }

    def action_view_local_cache(self):
        """Open the ic.product.info form for this SKU, if it exists."""
        self.ensure_one()
        rec = self.env['ic.product.info'].sudo().search(
            [('tow_kod', '=', self.tow_kod)], limit=1,
        )
        if not rec:
            raise UserError(_(
                "SKU %s is not in the local cache. Re-import the "
                "ProductInformation CSV to refresh."
            ) % self.tow_kod)
        return {
            'type': 'ir.actions.act_window',
            'name': _("IC Product %s") % self.tow_kod,
            'res_model': 'ic.product.info',
            'res_id': rec.id,
            'view_mode': 'form',
            'target': 'current',
        }


class IcAssignToBafWizard(models.TransientModel):
    """Tiny picker so a search hit can be pushed onto a BAF template's
    ``ic_seed_sku``. Kept transient — no persistent state."""

    _name = 'ic.assign.to.baf.wizard'
    _description = 'Assign IC SKU as seed to a BAF template'

    ic_sku = fields.Char(string="IC SKU", readonly=True)
    ic_manufacturer = fields.Char(string="IC Brand", readonly=True)
    ic_description = fields.Char(string="IC Description", readonly=True)
    template_id = fields.Many2one(
        'product.template', string="BAF Product Template", required=True,
        domain=[('active', '=', True)],
    )
    current_seed = fields.Char(
        string="Current ic_seed_sku",
        compute='_compute_current_seed',
    )

    @api.depends('template_id')
    def _compute_current_seed(self):
        for w in self:
            w.current_seed = w.template_id.ic_seed_sku or ''

    def action_apply(self):
        self.ensure_one()
        if 'ic_seed_sku' not in self.template_id._fields:
            raise UserError(_(
                "The ic_seed_sku field is not present on product.template. "
                "Install the baf_oe_crossref module to enable seed mapping."
            ))
        self.template_id.sudo().write({'ic_seed_sku': self.ic_sku})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Seed Assigned"),
                'message': _(
                    "Template %(name)s → ic_seed_sku = %(sku)s"
                ) % {
                    'name': self.template_id.display_name,
                    'sku': self.ic_sku,
                },
                'type': 'success',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
