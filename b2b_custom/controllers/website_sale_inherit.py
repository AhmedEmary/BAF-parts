import io
import xlsxwriter
import logging
from odoo import http
from odoo.addons.website_sale.controllers import cart
from odoo.http import request, content_disposition
# Import Cart from the correct path (Odoo 19 structure)
try:
    from odoo.addons.website_sale.controllers.cart import Cart
except ImportError:
    from odoo.addons.website_sale.controllers.main import WebsiteSale as Cart

_logger = logging.getLogger(__name__)

class WebsiteSalePagination(Cart):

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
                
                ppg = 10
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
            brand = line.product_id.brand.name if hasattr(line.product_id, 'brand') else ''
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

    @http.route(['/shop/payment'], type='http', auth="public", website=True, sitemap=False)
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
