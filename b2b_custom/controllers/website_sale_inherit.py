import io
import math
import xlsxwriter
import logging
from odoo import _, fields, http
from odoo.addons.website_sale.controllers import cart
from odoo.exceptions import UserError
from odoo.http import request, content_disposition
# Import Cart from the correct path (Odoo 19 structure)
try:
    from odoo.addons.website_sale.controllers.cart import Cart
except ImportError:
    from odoo.addons.website_sale.controllers.main import WebsiteSale as Cart

_logger = logging.getLogger(__name__)

# Cart lines per page on the B2B cart. Shared by the full-page render and the
# AJAX (quantity-change) re-render so both paginate identically.
_BAF_CART_PPG = 10


def _get_partner_allowed_families(partner):
    """
    Return the list of baf_brand_family values that should be visible for a
    given partner.  Falls back to ['jlr'] for guests / public users.
    """
    if not partner:
        return ['jlr']
    allowed = []
    if getattr(partner, 'shop_show_jlr', True):
        allowed.append('jlr')
    if getattr(partner, 'shop_show_bmw_mini', False):
        allowed.append('bmw_mini')
    if getattr(partner, 'shop_show_mercedes', False):
        allowed.append('mercedes')
    if getattr(partner, 'shop_show_other', False):
        # 'other' visibility also covers products with no brand family set
        allowed.append('other')
    return allowed or ['jlr']

class WebsiteSalePagination(Cart):

    def _prepare_order_history(self):
        """Skip the stock quick-reorder history sidebar on the B2B cart.

        Stock website_sale iterates every line of the partner's last 10
        confirmed orders and calls _get_combination_info per line, unbatched.
        For B2B partners those 10 orders reach ~1700 lines, which turns a
        cart page load into ~10k SQL queries (~12 s). The same sidebar
        re-renders on every JSON cart mutation, so add/remove/qty pay the
        same cost. The B2B skin doesn't display the sidebar, so return an
        empty history and short-circuit the whole loop.
        """
        return {'order_history': []}

    def _baf_line_page(self, order, line_id, product_id):
        """1-based page of the cart line being edited, computed BEFORE the
        quantity update so a line removed by qty=0 still resolves to its page.
        Falls back to page 1 when the line can't be located."""
        if not order:
            return 1
        lines = order.website_order_line
        line = self.env['sale.order.line']
        if line_id:
            line = lines.filtered(lambda l: l.id == int(line_id))[:1]
        if not line and product_id:
            line = lines.filtered(lambda l: l.product_id.id == int(product_id))[:1]
        if not line or line.id not in lines.ids:
            return 1
        return (list(lines.ids).index(line.id) // _BAF_CART_PPG) + 1

    @http.route()
    def update_cart(self, line_id, quantity, product_id=None, **kwargs):
        """Make the AJAX quantity-change re-render match the full-page cart:
        website-aware and paginated.

        Core's update_cart renders 'website_sale.cart_lines' with
        ir.ui.view._render_template, which applies website-specific views only
        when 'website_id' is in the env context. On this jsonrpc route it isn't,
        so two things break:

        1. Layout — the B2B customization (cart_lines_pagination: no product
           image, SKU shown) is skipped and the stock layout comes back (image
           reappears, SKU vanishes).
        2. Pagination — the B2B cart paginates at _BAF_CART_PPG lines/page, but
           the update response passes neither website_sale_order_lines_paged nor
           pager, so the re-render shows every line without a pager.

        Re-render the fragment through the current website with the page the
        edited line sits on (computed before super, so a line cleared by qty=0
        still resolves) plus its pager — exactly like the full-page render.
        """
        page = self._baf_line_page(request.cart, line_id, product_id)
        values = super().update_cart(
            line_id, quantity, product_id=product_id, **kwargs)
        order_sudo = request.cart
        website = request.env['website'].get_current_website()
        lines = order_sudo.website_order_line if order_sudo else False
        if website and lines and 'website_sale.cart_lines' in values:
            max_page = max(1, math.ceil(len(lines) / _BAF_CART_PPG))
            page = min(max(page, 1), max_page)
            offset = (page - 1) * _BAF_CART_PPG
            pager = website.pager(
                url='/shop/cart', total=len(lines), page=page,
                step=_BAF_CART_PPG, scope=7)
            # Render with website_id in context so ir.ui.view resolves the
            # website-specific cart_lines_pagination override (the B2B layout).
            IrUiView = request.env['ir.ui.view'].with_context(website_id=website.id)
            values['website_sale.cart_lines'] = IrUiView._render_template(
                'website_sale.cart_lines', {
                    'website_sale_order': order_sudo,
                    'website_sale_order_lines_paged': lines[offset:offset + _BAF_CART_PPG],
                    'pager': pager,
                    'date': fields.Date.today(),
                    'suggested_products': order_sudo._cart_accessories(),
                })
        return values

    @http.route()
    def add_to_cart(self, product_template_id, product_id, quantity=1, **kwargs):
        """Ride the standard cart route so an alternative-vendor add gets the
        same cart notification, cart-icon bump and tracking as a normal add.

        `add_to_cart` forwards **kwargs straight into `_cart_add`, so
        `baf_alt_vendor_id` reaches the sale.order overrides untouched — which
        also means an untrusted client could name ANY partner and be charged
        that vendor's direct price. Validate it here before it gets that far.
        """
        vendor_id = kwargs.pop('baf_alt_vendor_id', None)
        if vendor_id:
            product = request.env['product.product'].sudo().browse(int(product_id))
            if not product.exists():
                raise UserError(_("This product is no longer available."))
            order = request.cart or request.website._create_cart()
            partner = order.partner_id
            default_price = product.baf_get_sales_price(
                partner=partner.sudo()._origin if partner else None)
            # Pass the partner so the delivery-window cap trims the allowed set
            # too; otherwise the check here would let vendors through that the
            # UI hides.
            allowed = {
                o['vendor_id']
                for o in product._baf_alternative_direct_vendors(
                    default_price,
                    partner=partner.sudo()._origin if partner else None,
                )
            }
            if int(vendor_id) not in allowed:
                raise UserError(
                    _("This vendor is no longer available for this product."))
            kwargs['baf_alt_vendor_id'] = int(vendor_id)
        return super().add_to_cart(
            product_template_id, product_id, quantity=quantity, **kwargs)

    @http.route([
        '/shop/cart',
        '/shop/cart/page/<int:page>'
    ], type='http', auth="public", website=True, sitemap=False)
    def cart(self, id=None, access_token=None, revive_method='', page=0, **post):
        _logger.info(f"DEBUG_CART: Entering custom cart. Page={page}")
        
        response = super().cart(id=id, access_token=access_token, revive_method=revive_method, **post)
        
        if hasattr(response, 'qcontext'):
            order = response.qcontext.get('website_sale_order')
            
            if order and order.website_order_line:
                if not page:
                    page = 1

                ppg = _BAF_CART_PPG
                total = len(order.website_order_line)
                pager = request.website.pager(
                    url='/shop/cart',
                    total=total,
                    page=page,
                    step=ppg,
                    scope=7,
                    url_args=post,
                )
                
                offset = (page - 1) * ppg
                paged_lines = order.website_order_line[offset:offset + ppg]
                
                _logger.info(f"DEBUG_CART: Paged lines count: {len(paged_lines)} (Offset {offset})")

                response.qcontext.update({
                    'website_sale_order_lines_paged': paged_lines,
                    'pager': pager,
                })
        
        return response

    @http.route(['/shop/cart/export'], type='http', auth="public", website=True)
    def export_cart_excel(self, **post):
        # 1. Get the current order
        order = request.cart
        if not order or not order.order_line:
            return request.redirect("/shop/cart")

        # 2. Setup Excel Buffer
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet('My Cart')
        
        # 3. Define Styles and Headers
        bold = workbook.add_format({'bold': True, 'bg_color': '#E9ECEF'})
        headers = ['Brand', 'SKU', 'Product Name', 'Quantity', 'Unit Price', 'Subtotal']
        
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, bold)

        # 4. Fill Data
        row = 1
        for line in order.order_line:
            # Using your custom SKU/Brand logic if available, else standard Odoo fields
            brand = line.product_id.brand.display_name if hasattr(line.product_id, 'brand') else ''
            sku = line.product_id.sku or ''
            
            worksheet.write(row, 0, brand)
            worksheet.write(row, 1, sku)
            worksheet.write(row, 2, line.name_short or line.name)
            worksheet.write(row, 3, line.product_uom_qty)
            worksheet.write(row, 4, line.price_unit)
            worksheet.write(row, 5, line.price_subtotal)
            row += 1

        # 5. Add Total
        worksheet.write(row + 1, 4, "Total Untaxed:", bold)
        worksheet.write(row + 1, 5, order.amount_untaxed)

        workbook.close()
        output.seek(0)
        
        # 6. Return the File
        file_name = f"Cart_{order.name}.xlsx"
        return request.make_response(
            output.getvalue(),
            headers=[
                ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                ('Content-Disposition', content_disposition(file_name))
            ]
        )

import logging
from odoo import http
from odoo.http import request

try:
    from odoo.addons.website_sale.controllers.main import WebsiteSale
except ImportError:
    WebsiteSale = object

_logger = logging.getLogger(__name__)

class WebsiteSaleCustomDelivery(WebsiteSale):

    # ── Brand-family visibility filter ───────────────────────────────────────

    def _get_search_domain(self, search, category, attrib_values, search_in_description=True):
        # 1. Get the standard Odoo domain first
        domain = super()._get_search_domain(search, category, attrib_values, search_in_description)

        # 2. Identify the current user and their partner record
        user = request.env.user
        partner = user.partner_id

        # 3. Base rule: Always show brands that are marked as "Publicly Available"
        # Note: In your product.template, the field is named 'brand'
        brand_domain = [('brand.is_public', '=', True)]

        # 4. If the user is logged in, also show brands explicitly assigned to them
        if not user._is_public() and partner.visible_brand_ids:
            # We use an OR '|' condition: Either it's public, OR it's in their allowed brands list
            brand_domain = [
                '|',
                ('brand.is_public', '=', True),
                ('brand.id', 'in', partner.visible_brand_ids.ids)
            ]

        # OPTIONAL: If you want products that have NO brand assigned to be visible to everyone,
        # you can uncomment the following line:
        # brand_domain = ['|', ('brand', '=', False)] + brand_domain

        # 5. Append the custom brand visibility domain to the main search domain
        domain += brand_domain

        return domain

    # ── Shipping cost API ─────────────────────────────────────────────────────

    @http.route('/shop/baf_shipping_cost', type='jsonrpc', auth='public', website=True)
    def baf_shipping_cost(self, **kwargs):
        """
        Return the computed BAF shipping cost for the current cart.

        Response: {'cost': float, 'free': bool, 'zone': str, 'package_type': str}
        """
        order = request.cart
        if not order or not order.order_line:
            return {'cost': 0.0, 'free': True, 'zone': 'n/a', 'package_type': 'standard'}

        shipping_partner = order.partner_shipping_id or order.partner_id
        country_code = shipping_partner.country_id.code if shipping_partner.country_id else ''

        total_weight = sum(
            (line.product_id.weight or 0.0) * line.product_uom_qty
            for line in order.order_line
            if not line.display_type
        )
        has_bulky = any(
            getattr(line.product_id, 'is_bulky_goods', False)
            for line in order.order_line
            if not line.display_type
        )

        DeliveryRule = request.env['baf.delivery.rule'].sudo()
        zone = DeliveryRule.get_zone_for_country(country_code)
        package_type = 'bulky' if has_bulky else 'standard'
        cost = DeliveryRule.compute_shipping_cost(
            order_amount=order.amount_untaxed,
            total_weight_kg=total_weight,
            country_code=country_code,
            has_bulky=has_bulky,
        )
        return {
            'cost': cost,
            'free': cost == 0.0,
            'zone': zone,
            'package_type': package_type,
        }

    @http.route()
    def shop_payment(self, **post):
        """ Override the payment route to capture custom delivery fields """
        
        # Log the raw POST data received from the checkout form
        _logger.info("DEBUG Checkout: Entered /shop/payment route.")
        _logger.info(f"DEBUG Checkout: Received POST data: {post}")
        
        # 1. Get the current active order from the website
        order = getattr(request, 'cart', False)
        
        if order:
            _logger.info(f"DEBUG Checkout: Found active cart/order: {order.name} (ID: {order.id})")
            update_vals = {}
            
            # 2. Extract values from the submitted form (post dictionary)
            if 'shipping_method' in post:
                update_vals['shipping_method'] = post.get('shipping_method')
                _logger.info(f"DEBUG Checkout: Extracted shipping_method: {update_vals['shipping_method']}")
            else:
                _logger.warning("DEBUG Checkout: 'shipping_method' NOT found in POST data.")
                
            if 'customer_po' in post:
                update_vals['customer_po'] = post.get('customer_po')
                _logger.info(f"DEBUG Checkout: Extracted customer_po: {update_vals['customer_po']}")
            else:
                _logger.info("DEBUG Checkout: 'customer_po' NOT found in POST data (or left empty).")
                
            # 3. Write the extracted values to the sale order
            if update_vals:
                try:
                    order.sudo().with_context(
                        tracking_disable=True, 
                        mail_create_nosubscribe=True,
                        mail_notrack=True
                    ).write(update_vals)
                    _logger.info(f"DEBUG Checkout: Successfully updated Order {order.name} with: {update_vals}")
                except Exception as e:
                    _logger.error(f"DEBUG Checkout: Failed to update Order {order.name}. Error: {e}")
            else:
                _logger.info("DEBUG Checkout: No custom delivery fields to update.")
        else:
            _logger.error("DEBUG Checkout: No active cart/order found in request.cart!")

        # 4. Call the super method to continue the standard Odoo checkout flow
        _logger.info("DEBUG Checkout: Proceeding to standard Odoo payment flow via super().")
        return super(WebsiteSaleCustomDelivery, self).shop_payment(**post)

  # Change type='json' to type='jsonrpc'
    @http.route(['/shop/update_custom_fields'], type='jsonrpc', auth="public", website=True)
    def update_custom_fields(self, shipping_method=None, customer_po=None, **kwargs):
        """ AJAX route to save custom fields silently in the background """
        
        # Get the current active order
        order = getattr(request, 'cart', False)
        
        if order:
            update_vals = {}
            if shipping_method is not None:
                update_vals['shipping_method'] = shipping_method
            if customer_po is not None:
                update_vals['customer_po'] = customer_po
                
            if update_vals:
                order.sudo().with_context(
                    tracking_disable=True, 
                    mail_create_nosubscribe=True,
                    mail_notrack=True
                ).write(update_vals)
                _logger.info(f"AJAX Save: Updated Order {order.name} with {update_vals}")
                
        return True

    @http.route(['/shop/cart/empty'], type='http', auth="public", website=True, sitemap=False)
    def custom_empty_cart(self, **kw):
        """ Instantly empties the shopping cart without conflicting with Odoo's native JS """
        order = request.cart
        
        if order:
            order.order_line.sudo().unlink()            

        return request.redirect('/shop/cart')
