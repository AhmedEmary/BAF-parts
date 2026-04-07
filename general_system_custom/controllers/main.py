from odoo import http, _
from odoo.http import request
from odoo.exceptions import AccessError, MissingError
from odoo.addons.portal.controllers.portal import CustomerPortal
from io import BytesIO

try:
    import openpyxl
    from openpyxl.styles import Font
except ImportError:
    openpyxl = None
    Font = None


class PortalExcelExport(CustomerPortal):

    def _generate_excel_response(self, filename, workbook):
        """Helper to create the HTTP response that downloads the file"""
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        xls_data = output.read()
        output.close()

        return request.make_response(
            xls_data,
            headers=[
                ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                ('Content-Disposition', f'attachment; filename="{filename}.xlsx"'),
            ]
        )

    @http.route(['/my/orders/<int:order_id>/export_excel'], type='http', auth="public", website=True)
    def portal_order_export_excel(self, order_id, access_token=None, **kw):
        """Handles Excel Export from the Sales Order Website Portal"""
        try:
            order_sudo = self._document_check_access('sale.order', order_id, access_token=access_token)
        except (AccessError, MissingError):
            return request.redirect('/my')

        if not openpyxl:
            return request.redirect('/my')

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sales Order"
        bold_font = Font(bold=True)

        headers = ["SKU", "Product", "Quantity", "Unit Price", "Subtotal"]
        ws.append(headers)
        for col_num in range(1, len(headers) + 1):
            ws.cell(row=1, column=col_num).font = bold_font

        for line in order_sudo.order_line:
            ws.append([
                line.product_id.default_code or "",
                line.product_id.name or "",
                line.product_uom_qty,
                line.price_unit,
                line.price_subtotal
            ])

        ws.append(["", "", "", "Untaxed Amount:", order_sudo.amount_untaxed])
        ws.append(["", "", "", "Taxes:", order_sudo.amount_tax])
        ws.append(["", "", "", "Total:", order_sudo.amount_total])

        for row_idx in range(ws.max_row - 2, ws.max_row + 1):
            ws.cell(row=row_idx, column=4).font = bold_font
            ws.cell(row=row_idx, column=5).font = bold_font

        return self._generate_excel_response(f"Order_{order_sudo.name}", wb)

    @http.route(['/my/invoices/<int:invoice_id>/export_excel'], type='http', auth="public", website=True)
    def portal_invoice_export_excel(self, invoice_id, access_token=None, **kw):
        """Handles Excel Export from the Invoice Website Portal"""
        try:
            invoice_sudo = self._document_check_access('account.move', invoice_id, access_token=access_token)
        except (AccessError, MissingError):
            return request.redirect('/my')

        if not openpyxl:
            return request.redirect('/my')

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Invoice"
        bold_font = Font(bold=True)

        headers = ["SKU", "Product", "Quantity", "Unit Price", "Subtotal"]
        ws.append(headers)
        for col_num in range(1, len(headers) + 1):
            ws.cell(row=1, column=col_num).font = bold_font

        for line in invoice_sudo.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            ws.append([
                line.product_id.default_code or "",
                line.product_id.name or "",
                line.quantity,
                line.price_unit,
                line.price_subtotal
            ])

        ws.append(["", "", "", "Untaxed Amount:", invoice_sudo.amount_untaxed])
        ws.append(["", "", "", "Taxes:", invoice_sudo.amount_tax])
        ws.append(["", "", "", "Total:", invoice_sudo.amount_total])

        for row_idx in range(ws.max_row - 2, ws.max_row + 1):
            ws.cell(row=row_idx, column=4).font = bold_font
            ws.cell(row=row_idx, column=5).font = bold_font

        return self._generate_excel_response(f"Invoice_{invoice_sudo.name.replace('/', '_')}", wb)
