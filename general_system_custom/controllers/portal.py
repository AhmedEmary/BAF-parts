from odoo import http
from odoo.http import request
from odoo.addons.portal.controllers.portal import pager as portal_pager
from odoo.addons.sale.controllers.portal import CustomerPortal


class IntelliwisePortal(CustomerPortal):

    # ── Task 9: /my/orders — add search by ref + expose date range UI ─────────

    def _prepare_sale_portal_rendering_values(
        self, page=1, date_begin=None, date_end=None, sortby=None,
        search=None, quotation_page=False, **kwargs
    ):
        values = super()._prepare_sale_portal_rendering_values(
            page=page, date_begin=date_begin, date_end=date_end,
            sortby=sortby, quotation_page=quotation_page, **kwargs,
        )
        if not quotation_page and search:
            SaleOrder = request.env['sale.order']
            partner = request.env.user.partner_id
            domain = self._prepare_orders_domain(partner)
            if date_begin and date_end:
                domain += [('create_date', '>', date_begin), ('create_date', '<=', date_end)]
            domain += ['|', ('name', 'ilike', search), ('customer_po', 'ilike', search)]
            url_args = {
                k: v for k, v in {
                    'date_begin': date_begin, 'date_end': date_end, 'search': search,
                }.items() if v
            }
            pager_values = portal_pager(
                url='/my/orders',
                total=SaleOrder.search_count(domain) if SaleOrder.has_access('read') else 0,
                page=page,
                step=self._items_per_page,
                url_args=url_args,
            )
            orders = (
                SaleOrder.search(
                    domain, order='date_order desc',
                    limit=self._items_per_page, offset=pager_values['offset'],
                ).sudo()
                if SaleOrder.has_access('read') else SaleOrder
            )
            values.update({'orders': orders, 'pager': pager_values})
        values['search'] = search
        values['date_begin'] = date_begin
        values['date_end'] = date_end
        return values

    @http.route(['/my/orders', '/my/orders/page/<int:page>'],
                type='http', auth='user', website=True)
    def portal_my_orders(self, page=1, date_begin=None, date_end=None,
                         sortby=None, search=None, **kw):
        values = self._prepare_sale_portal_rendering_values(
            page=page, date_begin=date_begin, date_end=date_end,
            sortby=sortby, search=search,
        )
        return request.render('sale.portal_my_orders', values)
