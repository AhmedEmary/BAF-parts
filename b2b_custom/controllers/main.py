import io
import time
import re
import json
import logging
from odoo import http, _
from odoo.http import request, content_disposition
import xlsxwriter # noqa: PLC0415
_logger = logging.getLogger(__name__)

class B2BListToPart(http.Controller):

    def _parse_and_search(self, raw_text):
        """ 
        Reusable helper to parse text and find products.
        Returns: results (list), not_found (list)
        """
        lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
        
        # --- PHASE 1: Parsing ---
        input_map = {}
        all_codes = set()

        for line in lines:
            parts = re.split(r'\t|\s+', line)
            if len(parts) < 2: continue

            brand_raw = parts[0].strip()
            sku_raw = parts[1].strip()
            
            # Logic: BRAND(3 chars) + _ + SKU
            internal_code = f"{brand_raw[:3].upper()}_{sku_raw}"
            
            try:
                qty = int(float(parts[2].strip())) if len(parts) > 2 else 1
            except ValueError:
                qty = 1

            all_codes.add(internal_code)
            input_map.setdefault(internal_code, []).append({
                'brand': brand_raw,
                'sku': sku_raw,
                'qty': qty
            })

        results = []
        not_found = []
        
        if all_codes:
            query = """
                SELECT id, default_code 
                FROM product_product 
                WHERE default_code IN %s AND active = true
            """
            request.env.cr.execute(query, (tuple(all_codes),))
            db_results = request.env.cr.dictfetchall()
            
            found_codes = {row['default_code']: row['id'] for row in db_results}
            
            # --- PHASE 3: Mapping ---
            product_ids = list(found_codes.values())
            products_dict = {p.id: p for p in request.env['product.product'].sudo().browse(product_ids)}

            for code in all_codes:
                if code in found_codes:
                    product_obj = products_dict.get(found_codes[code])
                    # Get MOQ (Default to 1 if not set/zero)
                    # Assuming standard 'sale_delay' or similar field, using 1 here as requested
                    moq = 1 
                    
                    for item in input_map[code]:
                        results.append({
                            'brand': item['brand'],
                            'sku': item['sku'],
                            'product': product_obj,
                            'name': product_obj.name,
                            'moq': moq,
                            'uom': product_obj.uom_id.name,
                            'price': product_obj.list_price,
                            'qty': item['qty'],
                            'line_key': f"{product_obj.id}_{item['qty']}"
                        })
                else:
                    not_found.extend(input_map[code])
                    
        return results, not_found

    @http.route(['/b2b/list-to-part'], type='http', auth="user", website=True)
    def list_to_part(self, **post):
        return request.render("fratellileo_custom.list_to_part_page", {})

    @http.route(['/b2b/list-to-part/search'], type='http', auth="user", website=True, methods=['POST'])
    def search_parts(self, **post):
        raw_text = post.get('bulk_input', '')
        
        # 1. Run the shared search logic
        results, not_found = self._parse_and_search(raw_text)

        # 2. Pagination Logic
        try:
            current_page = int(post.get('page', 1))
        except ValueError:
            current_page = 1
            
        items_per_page = 50
        total_count = len(results)
        total_pages = (total_count + items_per_page - 1) // items_per_page
        
        if current_page < 1: current_page = 1
        if current_page > total_pages and total_pages > 0: current_page = total_pages

        start = (current_page - 1) * items_per_page
        end = start + items_per_page
        paged_results = results[start:end]

        return request.render("fratellileo_custom.list_to_part_results", {
            'results': paged_results,
            'not_found': not_found,
            'original_input': raw_text,
            'pager': {
                'current': current_page,
                'total': total_pages,
                'has_next': current_page < total_pages,
                'has_prev': current_page > 1
            }
        })

    @http.route(['/b2b/list-to-part/add_all'], type='http', auth="user", website=True, methods=['POST'])
    def add_all_to_cart(self, **post):
        import time
        start_time = time.time()
        
        raw_text = post.get('bulk_input', '')
        results, _ = self._parse_and_search(raw_text)
        
        if not results:
            return request.redirect("/shop/cart")

        # 1. Get/Create Order
        order = request.website._create_cart()
        
        # 2. Prepare Value List
        line_values = []
        for item in results:
            product = item['product']
            qty = item['qty']
            
            if qty > 0:
                line_values.append({
                    'order_id': order.id,
                    'product_id': product.id,
                    'product_uom_qty': qty,
                })

        if line_values:
            _logger.info("BATCH-CART: Creating %s lines via ORM...", len(line_values))
                        
            # Setup context to PREVENT chatter spam and preserve website pricing rules
            safe_context = dict(order.env.context)
            safe_context.update({
                'tracking_disable': True,          # Stops Odoo from creating tracking messages
                'mail_create_nosubscribe': True,   # Stops follower recalculation
                'mail_notrack': True,
            })
            
            # Use the environment with the safe context
            SaleOrderLine = request.env['sale.order.line'].with_context(**safe_context).sudo()
            
            # Chunking the insert to prevent PostgreSQL memory locks (1000 at a time)
            chunk_size = 1000
            for i in range(0, len(line_values), chunk_size):
                chunk = line_values[i:i + chunk_size]
                SaleOrderLine.create(chunk)
                _logger.info("BATCH-CART: Inserted chunk of %s lines...", len(chunk))
                
        duration = time.time() - start_time
        _logger.info("BATCH-CART: Process finished in %.2f seconds", duration)
        
        return request.redirect("/shop/cart")

    @http.route(['/b2b/list-to-part/export'], type='http', auth="user", website=True, methods=['POST'])
    def export_excel(self, **post):
        """ re-runs search and returns an Excel file """
        raw_text = post.get('bulk_input', '')
        results, not_found = self._parse_and_search(raw_text)

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet('Found Parts')
        
        # Headers
        headers = ['Brand', 'SKU', 'Name', 'MOQ', 'Unit', 'Price', 'Note', 'Qty']
        bold = workbook.add_format({'bold': True})
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, bold)

        # Data Rows
        for row_idx, item in enumerate(results, start=1):
            worksheet.write(row_idx, 0, item['brand'])
            worksheet.write(row_idx, 1, item['sku'])
            worksheet.write(row_idx, 2, item['name'])
            worksheet.write(row_idx, 3, item['moq'])
            worksheet.write(row_idx, 4, item['uom'])
            worksheet.write(row_idx, 5, item['price'])
            worksheet.write(row_idx, 6, "") # Note is empty
            worksheet.write(row_idx, 7, item['qty'])

        workbook.close()
        output.seek(0)
        
        return request.make_response(
            output.getvalue(),
            headers=[
                ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                ('Content-Disposition', content_disposition('bulk_parts_search.xlsx'))
            ]
        )
