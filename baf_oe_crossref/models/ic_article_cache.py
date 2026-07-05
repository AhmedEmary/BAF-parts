"""Small TTL cache for IC equivalents lookups.

Live IC calls are expensive (multiple round-trips, one language header).
Rendering a product page shouldn't hammer the IC API each time; but IC
prices *do* move, so we cap the freshness at a short TTL — 15 min by
default, overridable via ir.config_parameter ``baf.ic_cache_ttl_sec``.

The stored value is a JSON blob of the ``cards`` list returned by
:py:meth:`~ProductTemplate._baf_ic_resolve_equivalents`.
"""

import json
import logging
import time

from odoo import fields, models

_logger = logging.getLogger(__name__)

_DEFAULT_TTL_SEC = 15 * 60


class IcArticleCache(models.Model):
    _name = 'ic.article.cache'
    _description = 'Inter Cars Equivalents Cache'

    key = fields.Char(string="Cache Key", index=True, required=True)
    payload = fields.Text(string="Payload (JSON)")
    expires_at = fields.Float(
        string="Expires at (epoch)",
        help="Unix timestamp when this cache entry stops being valid.",
    )

    def _ttl(self):
        val = self.env['ir.config_parameter'].sudo().get_param(
            'baf.ic_cache_ttl_sec', default=str(_DEFAULT_TTL_SEC),
        )
        try:
            return max(60, int(val))
        except (TypeError, ValueError):
            return _DEFAULT_TTL_SEC

    def get(self, key):
        """Return the cached list, or None if missing / expired."""
        entry = self.sudo().search([('key', '=', key)], limit=1)
        if not entry:
            return None
        if entry.expires_at and entry.expires_at < time.time():
            entry.unlink()
            return None
        try:
            return json.loads(entry.payload or '[]')
        except (TypeError, ValueError):
            return None

    def put(self, key, value):
        ttl = self._ttl()
        payload = json.dumps(value or [])
        entry = self.sudo().search([('key', '=', key)], limit=1)
        vals = {
            'payload': payload,
            'expires_at': time.time() + ttl,
        }
        if entry:
            entry.write(vals)
        else:
            vals['key'] = key
            self.sudo().create(vals)

    def gc(self, batch=1000):
        """Delete expired rows — called periodically by cron."""
        now = time.time()
        expired = self.sudo().search([
            ('expires_at', '<', now),
        ], limit=batch)
        if expired:
            expired.unlink()
