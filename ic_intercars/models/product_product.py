"""IC identity fields on product.product + lazy materialisation.

BAF's catalog holds only OEM parts. IC aftermarket alternatives are only
turned into real ``product.product`` records **when they are ordered**,
so the catalog does not bloat with IC's full range. The helper
``_baf_find_or_create_ic()`` is what the equivalents block calls at
add-to-cart time to get a stable Odoo product.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Materialised on the template so vendor-compare, POs, and the
    # webshop can filter or route on it. Only IC-created products carry
    # these; native BAF templates leave them empty.
    ic_sku = fields.Char(string="IC SKU", index=True, copy=False)
    ic_index = fields.Char(string="IC Index", index=True, copy=False)
    ic_generic_article_id = fields.Char(
        string="IC Generic Article ID", index=True, copy=False,
        help="genericArticleId from IC catalog. Products that share this "
             "value are aftermarket equivalents of each other.",
    )
    # Quality tier — needed here (not in baf_oe_crossref) so the
    # lazy-materialised aftermarket product can be tagged at create time
    # and so IC drop-ship POs can filter on it without a cross-module
    # dependency in the reverse direction.
    part_quality = fields.Selection(
        selection=[
            ('oem', 'OEM (Original)'),
            ('oes', 'OES (Original Equipment Supplier)'),
            ('aftermarket', 'Aftermarket'),
            ('exchange', 'Exchange'),
        ],
        string="Part Quality", default='oem',
        help="OEM = the original manufacturer part BAF sells natively. "
             "Aftermarket = Inter Cars equivalent, drop-shipped from IC.",
    )


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def _baf_find_ic_product(self, ic_sku):
        """Find an already-materialised IC product by SKU.

        Only *aftermarket* products qualify — an OEM template that
        carries a stray ``ic_sku`` (typed into the wrong field on the
        Inter Cars tab) must never be hijacked by materialisation,
        which would overwrite its price and supplier data. Archived
        products are included so a re-order can reactivate them
        instead of tripping the unique default_code constraint.
        """
        if not ic_sku:
            return self.env['product.product']
        return self.sudo().with_context(active_test=False).search([
            ('ic_sku', '=', ic_sku),
            ('part_quality', '=', 'aftermarket'),
        ], limit=1)

    @api.model
    def _baf_ic_live_cost(self, backend, ic_sku, quantity=1):
        """Live ``customerPriceNet`` for one SKU, or raise.

        This is the materialisation pre-flight: if IC refuses to quote
        (``ICF201``, discontinued SKU, credit block, network error) we
        must NOT create a zero-cost product or cart line — surface a
        clear error instead. Used by the shop cart controller and any
        future flow that materialises on demand.
        """
        client = backend.get_client()
        error = None
        try:
            quote = client.get_price(
                lines=[{'sku': ic_sku, 'quantity': quantity}],
                ship_to=backend.ship_to or None,
            )
        except UserError as exc:
            error, quote = str(exc), {}
        except Exception as exc:  # noqa: BLE001
            _logger.warning("IC live cost fetch failed for %s: %s",
                            ic_sku, exc)
            error, quote = str(exc), {}
        for line in (quote.get('lines') or []):
            price = line.get('price') or {}
            if price.get('customerPriceNet') is not None:
                return float(price['customerPriceNet'])
        raise UserError(_(
            "Inter Cars declined to quote SKU %(sku)s right now. "
            "This usually means the item was discontinued or is not "
            "orderable through your account.\n\nIC response: %(err)s"
        ) % {'sku': ic_sku, 'err': error or 'no price returned'})

    @api.model
    def _baf_ic_live_availability(self, backend, ic_sku):
        """Live availability for one SKU, or None when unknown.

        Returns the summed availability across IC locations (capped at
        IC's stock_cap per location). ``None`` means the check could
        not run — callers should not block on unknown.
        """
        client = backend.get_client()
        try:
            rows = client.get_stock(
                skus=[ic_sku], ship_to=backend.ship_to or None,
            ) or []
        except Exception as exc:  # noqa: BLE001
            _logger.warning("IC availability check failed for %s: %s",
                            ic_sku, exc)
            return None
        return sum(
            int(r.get('availability') or 0)
            for r in rows if r.get('sku') == ic_sku
        )

    @api.model
    def _baf_ic_list_price(self, price_net):
        """Customer-facing sale price for a materialised IC product.

        The BAF pricing engine treats ``list_price`` as the UPE and
        prices guests at UPE + surcharge — so the materialised product
        MUST carry the marked-up price there, otherwise cart lines
        fall back to Odoo's 1.0 default. Markup source of truth is the
        same system parameter the shop cards use
        (``baf.ic_markup_pct``, default 25 %).
        """
        if not price_net:
            return 0.0
        param = self.env['ir.config_parameter'].sudo().get_param(
            'baf.ic_markup_pct', default='25.0',
        )
        try:
            markup = float(param)
        except (TypeError, ValueError):
            markup = 25.0
        return round(float(price_net) * (1.0 + markup / 100.0), 2)

    @api.model
    def _baf_find_or_create_ic(self, backend, ic_data, price_net=None):
        """Return a live product.product for an IC catalog entry.

        ``ic_data`` is a dict shaped like an item of ``/ic/catalog/products``
        ``products[]``. If a product with the same SKU already exists it
        is returned as-is (idempotent). Otherwise a new consumable is
        created, tagged with the drop-ship route and a ``supplierinfo``
        entry pointing to the IC vendor.

        ``price_net`` is the IC ``customerPriceNet`` (BAF's cost). If
        given, it's written on the supplierinfo line.
        """
        if not backend:
            raise UserError(_(
                "Cannot materialise Inter Cars product: no IC backend is "
                "configured."
            ))
        ic_sku = ic_data.get('sku')
        if not ic_sku:
            raise UserError(_("IC catalog entry has no 'sku'."))

        existing = self._baf_find_ic_product(ic_sku)
        if existing:
            # Idempotent hardening: whether we created this record now
            # or the CSV importer / a previous cart click did, the
            # invariants the ordering flow needs (drop-ship route,
            # supplierinfo pointing at the IC vendor with a fresh
            # cost) must hold every time.
            if not existing.product_tmpl_id.active:
                # A re-order of an archived IC product: the caller only
                # gets here after IC quoted the SKU, so it is orderable
                # again — reactivate instead of colliding on the
                # default_code constraint with a fresh copy.
                existing.product_tmpl_id.sudo().write({'active': True})
            route = self.env.ref(
                'stock_dropshipping.route_drop_shipping',
                raise_if_not_found=False,
            )
            if route and route not in existing.product_tmpl_id.route_ids:
                existing.product_tmpl_id.sudo().write(
                    {'route_ids': [(4, route.id)]},
                )
            existing._baf_upsert_ic_supplierinfo(backend, price_net)
            # Keep the customer price in step with IC's current cost.
            if price_net:
                new_list = self._baf_ic_list_price(price_net)
                if new_list and existing.product_tmpl_id.list_price != new_list:
                    existing.product_tmpl_id.sudo().write(
                        {'list_price': new_list},
                    )
            return existing

        brand_name = (ic_data.get('brand') or '').strip() or _("Aftermarket")
        brand = self.env['product.brand'].sudo().search(
            [('name', '=', brand_name)], limit=1,
        )
        if not brand:
            brand = self.env['product.brand'].sudo().create({
                'name': brand_name,
            })

        name = (
            ic_data.get('shortDescription')
            or ic_data.get('description')
            or ic_data.get('articleNumber')
            or f"IC {ic_sku}"
        )
        eans = ic_data.get('eans') or []
        barcode = eans[0] if eans else False
        generic_id = ''
        for ref in ic_data.get('genericArticleReferences') or []:
            if ref.get('primary') and ref.get('genericArticleId'):
                generic_id = str(ref['genericArticleId'])
                break
            if not generic_id and ref.get('genericArticleId'):
                generic_id = str(ref['genericArticleId'])

        vals = {
            'name': name,
            'type': 'consu',
            'sale_ok': True,
            'purchase_ok': True,
            # BAF convention: `sku` carries the raw part number and the
            # internal reference is derived from it by b2b_custom's
            # compute (brand prefix + '_' + sku, e.g. VAL_BEF134). We
            # set sku and let that compute own default_code so IC
            # products look exactly like the rest of the catalog.
            'sku': ic_sku,
            'ic_sku': ic_sku,
            'ic_index': ic_data.get('index') or False,
            'ic_generic_article_id': generic_id or False,
            'brand': brand.id,
            'part_quality': 'aftermarket',
            'list_price': self._baf_ic_list_price(price_net),
            'weight': ic_data.get('packageWeight') or 0.0,
            'height': ic_data.get('packageHeight') or 0.0,
            'width': ic_data.get('packageWidth') or 0.0,
            'length': ic_data.get('packageLength') or 0.0,
        }
        if barcode:
            vals['barcode'] = barcode

        # b2b_custom derives default_code as '<BRAND[:3]>_<sku>'. If an
        # unrelated product already owns that reference (the number
        # collides across brands), pre-empt the unique-constraint crash
        # by writing an explicit, suffixed reference — provided values
        # win over the compute at create time.
        prefix = (brand.name[:3].upper() if len(brand.name) >= 3
                  else brand.name.upper())
        prospective = f"{prefix}_{ic_sku}"
        clash = self.env['product.template'].sudo().with_context(
            active_test=False,
        ).search_count([('default_code', '=', prospective)])
        if clash:
            vals['default_code'] = f"{prospective}_IC"

        tmpl = self.env['product.template'].sudo().create(vals)
        variant = tmpl.product_variant_id

        # Safety net: if the brand+sku compute produced nothing (e.g.
        # blank brand name), fall back to the raw IC SKU so the
        # product always has an internal reference.
        if variant and ic_sku and not variant.default_code:
            variant.sudo().write({'default_code': ic_sku})

        # Drop-ship route — pin it if the module exposes the standard route.
        route = self.env.ref('stock_dropshipping.route_drop_shipping',
                             raise_if_not_found=False)
        if route:
            tmpl.sudo().write({'route_ids': [(4, route.id)]})

        # Shop label (requirement: every aftermarket product is clearly
        # marked). The badge shows on the shop grid and product page.
        ribbon = self.env.ref('ic_intercars.ribbon_aftermarket',
                              raise_if_not_found=False)
        if ribbon and 'website_ribbon_id' in tmpl._fields:
            tmpl.sudo().write({'website_ribbon_id': ribbon.id})

        variant._baf_upsert_ic_supplierinfo(backend, price_net)
        _logger.info(
            "Materialised IC product sku=%s brand=%s odoo_id=%s",
            ic_sku, brand_name, variant.id,
        )
        return variant

    def _baf_upsert_ic_supplierinfo(self, backend, price_net):
        """Ensure this product has a supplierinfo line pointing at IC.

        Called both when the product is first materialised and later
        when IC's ``customerPriceNet`` changes.
        """
        self.ensure_one()
        if not backend or not backend.vendor_id:
            return
        Info = self.env['product.supplierinfo'].sudo()
        line = Info.search([
            ('product_tmpl_id', '=', self.product_tmpl_id.id),
            ('partner_id', '=', backend.vendor_id.id),
        ], limit=1)
        vals = {}
        if price_net is not None:
            vals['price'] = float(price_net)
        if backend.currency_id:
            vals['currency_id'] = backend.currency_id.id
        if not line:
            base = {
                'partner_id': backend.vendor_id.id,
                'product_tmpl_id': self.product_tmpl_id.id,
                'min_qty': 0.0,
                'price': float(price_net) if price_net is not None else 0.0,
            }
            if backend.currency_id:
                base['currency_id'] = backend.currency_id.id
            Info.create(base)
        elif vals:
            line.write(vals)
