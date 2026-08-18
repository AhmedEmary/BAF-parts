import logging
import time

from odoo import _, api, models

_logger = logging.getLogger(__name__)


class BafProgressNotifier(models.AbstractModel):
    """Toasts for long synchronous jobs, pushed while the job is still running."""

    _name = 'baf.progress.notifier'
    _description = 'BAF Progress Notifier'

    def _baf_notify(self, title, message, immediate=False):
        """Toast the current user.

        bus.bus only writes its rows on commit, so by default this just queues
        the toast and the caller's next commit delivers it -- near-free for a
        job that already commits per batch. `immediate` sends on a separate
        cursor instead, ~30ms and a spare connection, for jobs that cannot
        commit mid-run: the vendor pricing import stages into an ON COMMIT DROP
        temp table, so committing would destroy the staged rows.
        """
        payload = {
            'title': title,
            'message': message,
            'type': 'info',
            'sticky': False,
        }
        try:
            if not immediate:
                self.env['bus.bus']._sendone(
                    self.env.user.partner_id, 'simple_notification', payload)
                return
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, self.env.uid, self.env.context)
                env['bus.bus']._sendone(
                    env.user.partner_id, 'simple_notification', payload)
        except Exception:
            # Feedback is never worth failing the job over.
            _logger.warning("Progress toast failed", exc_info=True)

    def _baf_notify_progress(self, title, verb, unit, done, total, started_at,
                             immediate=False):
        """Toast '<verb> done / total <unit> (pct). ETA ~Xm Ys'.

        `total` is 0 when the source is streamed and its size is unknown, in
        which case there is no percentage or ETA to report.
        """
        elapsed = time.time() - started_at
        if total and total > 0:
            eta_s = int((elapsed / done) * (total - done)) if done else 0
            message = _(
                "%(verb)s %(d)s / %(t)s %(unit)s (%(p).1f%%). ETA ~%(em)dm %(es)ds"
            ) % {
                'verb': verb, 'unit': unit,
                'd': '{:,}'.format(done), 't': '{:,}'.format(total),
                'p': min(100.0, (done / total) * 100.0),
                'em': eta_s // 60, 'es': eta_s % 60,
            }
        else:
            message = _("%(verb)s %(d)s %(unit)s so far (%(e).0fs elapsed)") % {
                'verb': verb, 'unit': unit,
                'd': '{:,}'.format(done), 'e': elapsed,
            }
        self._baf_notify(title, message, immediate=immediate)
