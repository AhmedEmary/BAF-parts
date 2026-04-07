import logging
import time
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class WebsiteBackorders(http.Controller):

    @http.route(['/backorders', '/backorders/page/<int:page>'], type='http', auth="user", website=True)
    def backorders(self, page=1, date_begin=None, date_end=None,
                   search=None, brand_id=None, sku=None, **kw):
        t0 = time.time()

        partner = request.env.user.partner_id
        items_per_page = 20

        domain = [
            ('order_id.partner_id', '=', partner.id),
            ('state', 'in', ['sale', 'done']),
            ('product_uom_qty', '>', 0),
            ('product_template_id.type', '=', 'consu'),
            ('invoice_status', '!=', 'invoiced'),
        ]

        # Filters
        if date_begin and date_end:
            domain += [
                ('order_id.date_order', '>=', date_begin),
                ('order_id.date_order', '<=', date_end),
            ]
        if search:
            domain += ['|',
                ('order_id.name', 'ilike', search),
                ('order_id.customer_po', 'ilike', search),
            ]
        if brand_id:
            domain += [('brand_id', '=', int(brand_id))]
        if sku:
            domain += [('product_id.sku', '=', sku)]

        total_count = request.env['sale.order.line'].sudo().search_count(domain)

        url_args = {k: v for k, v in {
            'date_begin': date_begin, 'date_end': date_end,
            'search': search, 'brand_id': brand_id, 'sku': sku,
        }.items() if v}

        pager = request.website.pager(
            url='/backorders',
            total=total_count,
            page=page,
            step=items_per_page,
            url_args=url_args,
        )

        lines_to_display = request.env['sale.order.line'].sudo().search(
            domain,
            order='create_date desc',
            limit=items_per_page,
            offset=pager['offset'],
        )

        # Prefetch fields used in template to avoid N+1 queries
        if lines_to_display:
            for line in lines_to_display:
                _ = line.order_id.name
                _ = line.product_id.brand.name
                _ = line.order_id.purchase_ids

        brands = request.env['product.brand'].sudo().search([])

        values = {
            'lines': lines_to_display,
            'pager': pager,
            'brands': brands,
            'search': search or '',
            'date_begin': date_begin or '',
            'date_end': date_end or '',
            'selected_brand_id': int(brand_id) if brand_id else 0,
            'sku': sku or '',
        }

        t3 = time.time()
        _logger.info(f"BACKORDERS: Total Controller Time: {t3 - t0:.4f}s")

        return request.render("b2b_custom.backorders_page", values)
