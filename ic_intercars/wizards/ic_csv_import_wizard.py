"""Two-mode CSV importer for the IC ProductInformation feed.

Mode A — **Upload**: paste the raw ``.csv`` (or its ``.zip``) into the
wizard's file field and click Import. Streams the CSV directly into
Postgres via ``COPY``.

Mode B — **Fetch from IC**: use the ``csv_login`` / ``csv_password``
stored on the active ``ic.backend`` to download today's file from
``https://data.webapi.intercars.eu/customer/<login>/ProductInformation/``.
Same COPY on the way in.

The wizard reports rows inserted + seconds elapsed. Products are
created from the cache afterwards, by hand-picking rows in the
IC Products (cache) list and clicking *Create Products*.
"""

import base64
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class IcCsvImportWizard(models.TransientModel):
    _name = 'ic.csv.import.wizard'
    _description = 'Import IC ProductInformation CSV'

    backend_id = fields.Many2one(
        'ic.backend', string="Backend",
        default=lambda self: self.env['ic.backend']._get_default(),
        required=True,
    )
    source = fields.Selection([
        ('upload', 'Upload File'),
        ('fetch', 'Fetch from Inter Cars (using backend CSV credentials)'),
    ], default='upload', required=True)

    upload_file = fields.Binary(
        string="ProductInformation CSV/ZIP",
        help="The ProductInformation_YYYY-MM-DD.csv or .csv.zip file.",
    )
    upload_filename = fields.Char(string="Filename")

    # ── Action ───────────────────────────────────────────────────────────
    def action_import(self):
        self.ensure_one()
        if not self.backend_id:
            raise UserError(_(
                "No active IC backend. Configure one under "
                "Purchase → Configuration → Inter Cars."
            ))
        csv_bytes = self._resolve_bytes()
        stats = self.env['ic.product.info'].sudo().bulk_load_csv(
            csv_bytes, replace=True,
        )
        msg = _(
            "Imported %(rows)d IC products in %(secs).1fs."
        ) % {
            'rows': stats['rows'],
            'secs': stats['seconds'],
        }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('IC CSV Imported'),
                'message': msg,
                'type': 'success',
                'sticky': True,
            },
        }

    # ── Bytes resolution (upload OR HTTP fetch) ──────────────────────────
    def _resolve_bytes(self):
        self.ensure_one()
        if self.source == 'upload':
            if not self.upload_file:
                raise UserError(_(
                    "Attach a ProductInformation CSV or ZIP file."
                ))
            raw = base64.b64decode(self.upload_file)
            return self.backend_id._baf_unzip_csv_if_needed(
                raw, self.upload_filename or '')
        # source == 'fetch' — same downloader the daily cron uses
        return self.backend_id._baf_fetch_product_info_csv()
