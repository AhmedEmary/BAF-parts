from odoo import models, api

class CorrispettiviReport(models.AbstractModel):
    _name = 'report.general_system_custom.report_corrispettivi_template'
    _description = 'Corrispettivi Report Parser'

    @api.model
    def _get_report_values(self, docids, data=None):
        docs = self.env['account.move'].browse(docids)

        grouped_taxes = {}
        grand_total = 0.0

        for move in docs:
            # Default to 'IT' if country is not set
            country_code = move.partner_id.country_id.code or 'IT'
            grand_total += move.amount_total

            # Loop through the actual products on the receipt
            for line in move.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):

                # Extract Tax ID and Tax Name to group properly
                tax = line.tax_ids[0] if line.tax_ids else False
                tax_id = tax.id if tax else 0
                tax_name = tax.name if tax else 'Esente/No Tax'

                # Group by Country and Tax ID
                group_key = f"{country_code}_{tax_id}"

                if group_key not in grouped_taxes:
                    grouped_taxes[group_key] = {
                        'country': country_code,
                        'tax_name': tax_name,
                        'base': 0.0,
                        'tax': 0.0,
                        'total': 0.0
                    }

                # Base is price_subtotal (without tax), Total is price_total (with tax)
                base_amount = line.price_subtotal
                line_total = line.price_total
                tax_amount = line_total - base_amount

                grouped_taxes[group_key]['base'] += base_amount
                grouped_taxes[group_key]['tax'] += tax_amount
                grouped_taxes[group_key]['total'] += line_total

        # Convert the dictionary to a list so the QWeb template can loop through it easily
        grouped_list = list(grouped_taxes.values())

        return {
            'doc_ids': docids,
            'doc_model': 'account.move',
            'docs': docs,
            'grouped_list': grouped_list,
            'grand_total': grand_total,
        }
