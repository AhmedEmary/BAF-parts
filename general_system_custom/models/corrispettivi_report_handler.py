from odoo import models

class IntelliwiseCorrispettiviReportHandler(models.AbstractModel):
    _name = 'intelliwise.corrispettivi.report.handler'
    _inherit = 'account.journal.report.handler'
    _description = 'Corrispettivi Report Handler (Receipts Only)'

    def _get_corrispettivi_domain(self):
        """ Domain to filter ONLY Sales Receipts (Corrispettivi). """
        return [('move_id.move_type', '=', 'out_receipt')]

    def _custom_options_initializer(self, report, options, previous_options):
        """
        Inject the domain globally when the report loads.
        This automatically filters the UI totals, the expanded lines,
        the Native Tax Summary, and the PDF/XLSX exports all at once!
        """
        super()._custom_options_initializer(report, options, previous_options)

        # Append our strict 'out_receipt' filter globally
        options['forced_domain'] = options.get('forced_domain', []) + self._get_corrispettivi_domain()

    def _report_custom_engine_journal_report(self, expressions, options, date_scope, current_groupby, next_groupby, offset=0, limit=None, warnings=None):

        """
        Intercept the web UI engine to inject the receipts domain.
        Using **kwargs ensures we safely pass whatever arguments Odoo's core engine
        requires without causing TypeErrors.
        """
        options['forced_domain'] = options.get('forced_domain', []) + self._get_corrispettivi_domain()
        options['order_by'] = 'journal_id.type DESC, date ASC, name ASC'

        return super()._report_custom_engine_journal_report(
            expressions, options, date_scope, current_groupby, next_groupby, offset=offset, limit=limit, warnings=warnings
        )

    def _generate_document_data_for_export(self, report, options, export_type='pdf'):
        """ Intercept the PDF/XLSX export engine to inject the receipts domain """
        options['forced_domain'] = options.get('forced_domain', []) + self._get_corrispettivi_domain()

        return super()._generate_document_data_for_export(report, options, export_type=export_type)
