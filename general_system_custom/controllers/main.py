import os

from odoo import http, _
from odoo.http import request
from odoo.exceptions import AccessError, MissingError
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.tools import file_open
from io import BytesIO

try:
    import openpyxl
    from openpyxl.styles import Font
except ImportError:
    openpyxl = None
    Font = None


class BafDiscountTemplateDownload(http.Controller):
    """Serves the master discount-matrix import template (all brand sheets)."""

    @http.route(
        '/general_system_custom/discount_matrix_template',
        type='http', auth='user',
    )
    def download_discount_matrix_template(self, **kw):
        if not openpyxl:
            return request.not_found()

        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        self._build_bmw_mini_sheet(wb)
        self._build_jlr_sheet(wb)
        self._build_mercedes_sheet(wb)

        output = BytesIO()
        wb.save(output)
        output.seek(0)
        data = output.read()
        output.close()

        return request.make_response(
            data,
            headers=[
                ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                ('Content-Disposition', 'attachment; filename="discount_matrix_template.xlsx"'),
            ],
        )

    # ── Sheet builders ──────────────────────────────────────────────────
    # Layouts mirror the constants in baf.discount.import.wizard so that a
    # file produced from this template can be re-uploaded without editing.

    @staticmethod
    def _bold_header(ws, cell, value):
        ws[cell] = value
        ws[cell].font = Font(bold=True)

    def _build_bmw_mini_sheet(self, wb):
        ws = wb.create_sheet('BMW-MINI-MOTORRAD')

        # Row 1: section headers
        self._bold_header(ws, 'A1', 'DC')
        self._bold_header(ws, 'B1', 'PURCHAGES BMW-MINI')
        self._bold_header(ws, 'J1', 'Purchages Motocycle')
        for col, label in (('K1', 'SALE PRICE GR1'), ('O1', 'SALE PRICE GR2'),
                           ('S1', 'SALE PRICE GR3'), ('W1', 'SALE PRICE GR4'),
                           ('AA1', 'GR_MOTORCYCLE')):
            self._bold_header(ws, col, label)

        # Row 2: type sub-headers (BMW T1-2 / T3-9 / MINI T1-2 / T3-9)
        bmw_mini_subs = ['BMW T1-2', 'BMW T3-9', 'MINI T1-2', 'MINI T3-9']
        # Purchase: SUP1/SUP2 pairs per type (8 cols starting at B), then SUP3 moto at J
        purchase_row2 = ['BMW T1-2', 'BMW T1-2', 'BMW T3-9', 'BMW T3-9',
                         'MINI T1-2', 'MINI T1-2', 'MINI T3-9', 'MINI T3-9', 'MOTO']
        for offset, val in enumerate(purchase_row2):
            ws.cell(row=2, column=2 + offset, value=val).font = Font(bold=True)
        # Sales sections: 4 sub-cols each, base cols 11, 15, 19, 23, 27 (1-indexed)
        for base_col in (11, 15, 19, 23, 27):
            for i, sub in enumerate(bmw_mini_subs):
                ws.cell(row=2, column=base_col + i, value=sub).font = Font(bold=True)

        # Row 3: supplier sub-labels for purchase columns
        purchase_row3 = ['Supplier1', 'Supplier2'] * 4 + ['Supplier3']
        for offset, val in enumerate(purchase_row3):
            ws.cell(row=3, column=2 + offset, value=val).font = Font(bold=True)

        # Sample data row to make the format obvious (row 4: discount code 10)
        ws['A4'] = '10'

    def _build_jlr_sheet(self, wb):
        ws = wb.create_sheet('JLR')
        headers = ['DC', 'GR8', 'GR7', 'GR6', 'GR5', 'GR4 (Purchase + Sales)',
                   'GR3', 'GR2', 'GR1']
        for idx, label in enumerate(headers, start=1):
            ws.cell(row=1, column=idx, value=label).font = Font(bold=True)
        # Sample alphanumeric DC to advertise that codes can contain chars
        ws['A2'] = '1A'

    def _build_mercedes_sheet(self, wb):
        ws = wb.create_sheet('MERCEDES')
        headers = ['DC', 'Purchages', 'SALES GR1']
        for idx, label in enumerate(headers, start=1):
            ws.cell(row=1, column=idx, value=label).font = Font(bold=True)
        ws['A2'] = 'M03'


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
