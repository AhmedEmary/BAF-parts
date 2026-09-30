"""HTTP surface for the LexCom DMS interface.

Everything unusual about this controller comes from two facts.

**The specification allows exactly two status codes.** "The response HTTP code
for the command-specific URLs must always be 200. Only in case of a critical
server error a HTTP code 5xx may be returned." So a rejected credential, a
malformed document and a refused order are all HTTP 200 carrying an error
document; only an unhandled exception is 5xx. No path returns 401.

**Two Odoo 19 defaults work against a machine endpoint.** Both are handled at
the top of the handler and each is load-bearing:

* ``odoo/http.py:974`` derives the route's ``readonly`` default from
  ``default_auth == 'none'``, so an ``auth='none'`` route is READ-ONLY unless
  told otherwise. Odoo does not fail the write - it catches
  ``ReadOnlySqlTransaction`` and re-runs the entire handler on a read/write
  cursor (``http.py:2303-2311``). Without ``readonly=False`` every order would
  be parsed and resolved twice.
* ``odoo/http.py:2283`` builds the environment from ``session.uid``, and a
  machine client sends no session cookie, so ``uid`` is None. ``.sudo()`` does
  NOT fix this: ``odoo/orm/models.py:5956`` states superuser mode "does not
  change the current user". ``create()`` would write ``create_uid = NULL``.
  Hence the explicit ``SUPERUSER_ID`` environment.
"""

import base64
import logging
import time

from lxml import etree
from werkzeug.exceptions import BadRequest

from odoo import SUPERUSER_ID, _, api, http
from odoo.http import request

from ..models.lexcom_protocol import LexcomProtocolError
from ..models.res_company import verify_lexcom_password

_logger = logging.getLogger(__name__)

#: Reject oversized bodies before parsing rather than after.
MAX_BODY_BYTES = 4 * 1024 * 1024

XML_CONTENT_TYPE = "text/xml; charset=utf-8"

#: The spec allows up to five arbitrary custom headers on any command.
CUSTOM_HEADERS = (
    "X_LC_HEADER_01", "X_LC_HEADER_02", "X_LC_HEADER_03",
    "X_LC_HEADER_04", "X_LC_HEADER_05",
)


def _hardened_parser():
    """An lxml parser safe for bytes off the public internet.

    ``resolve_entities=False`` blocks both XXE and billion-laughs expansion;
    ``no_network`` stops any external fetch; ``huge_tree=False`` keeps lxml's
    own depth and size limits switched on.
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        load_dtd=False,
        dtd_validation=False,
    )


class LexcomController(http.Controller):

    @http.route(
        "/lexcom/<string:command>",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        readonly=False,      # see module docstring - NOT optional
        save_session=False,  # a machine client has no use for a session cookie
    )
    def lexcom_dispatch(self, command, **kwargs):
        started = time.monotonic()
        env = api.Environment(request.env.cr, SUPERUSER_ID, {})
        Protocol = env["lexcom.protocol"]

        body = request.httprequest.get_data() or b""
        headers = self._custom_headers()
        remote = request.httprequest.remote_addr
        dealer_id = False

        def finish(element, outcome, message=None, order=None, status=200):
            payload = Protocol._serialize(element)
            # One server-log line per request, as Alzura logs its fetches. The
            # payload is never logged: it carries addresses and payment ids.
            sent = {k: v for k, v in headers.items() if v}
            _logger.log(
                logging.WARNING if outcome == "auth_failed" else logging.INFO,
                "LexCom: %s from %s, dealer %s: %s%s%s (%d ms)%s",
                command, remote, dealer_id or "-", outcome,
                " %s" % order.name if order else "",
                " - %s" % message if message else "",
                (time.monotonic() - started) * 1000,
                " headers %s" % sent if sent else "",
            )
            return request.make_response(
                payload,
                headers=[
                    ("Content-Type", XML_CONTENT_TYPE),
                    ("Content-Length", str(len(payload))),
                ],
                status=status,
            )

        try:
            if len(body) > MAX_BODY_BYTES:
                return finish(
                    Protocol._render_error(
                        _("Request body exceeds %d bytes.") % MAX_BODY_BYTES
                    ),
                    "business_error",
                    _("Request body too large."),
                )

            company = self._authenticate(env)
            if company is None:
                # Per spec this is still 200: a rejected credential is not a
                # critical server error, and the spec defines no other code.
                return finish(
                    Protocol._render_error(_("Authentication failed.")),
                    "auth_failed",
                    _("Authentication failed."),
                )

            try:
                root = etree.fromstring(body, parser=_hardened_parser())
            except etree.XMLSyntaxError as exc:
                return finish(
                    Protocol._render_error(_("Malformed XML: %s") % exc),
                    "business_error",
                    _("Malformed XML."),
                )

            dealer_id = self._peek_dealer_id(root)

            try:
                # The savepoint is what makes the spec's atomicity rule real:
                # a business rejection raised anywhere below undoes every write
                # the command made before it.
                with request.env.cr.savepoint():
                    element, outcome, message, order = Protocol._dispatch(
                        command, root, company, headers
                    )
            except LexcomProtocolError as exc:
                return finish(
                    Protocol._render_error(str(exc)), "business_error", str(exc)
                )

            return finish(element, outcome, message, order)

        except Exception:  # noqa: BLE001 - deliberate catch-all
            # A bug on our side. The spec's one non-200 allowance exists for
            # exactly this, and answering 200 here would disguise a broken
            # deployment as a run of ordinary business rejections.
            _logger.exception(
                "LexCom: unhandled error serving command %s from %s, dealer %s",
                command, remote, dealer_id or "-",
            )
            payload = Protocol._serialize(
                Protocol._render_error(_("Internal server error."))
            )
            return request.make_response(
                payload,
                headers=[("Content-Type", XML_CONTENT_TYPE)],
                status=500,
            )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _authenticate(self, env):
        """Resolve the company from the Basic Auth header, or None.

        Returns None for every failure mode - missing header, wrong scheme,
        undecodable payload, unknown user, wrong password, endpoint disabled -
        so the caller cannot accidentally tell them apart in the response.
        """
        header = request.httprequest.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return None
        try:
            raw = base64.b64decode(header[6:].strip(), validate=True)
            username, _sep, password = raw.decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        if not username:
            return None

        company = env["res.company"].sudo().search(
            [("lexcom_username", "=", username)], limit=1
        )
        # Verify UNCONDITIONALLY, before testing whether the company exists or
        # is enabled. Returning early on an unknown username would skip PBKDF2
        # entirely, making that rejection ~60x faster than a wrong-password
        # rejection and letting an attacker confirm a valid username by timing
        # alone. All three conditions are therefore evaluated together, after
        # the hashing cost has been paid.
        password_ok = verify_lexcom_password(
            company.lexcom_password_hash if company else None, password
        )
        if not company or not company.lexcom_enabled or not password_ok:
            return None
        return company

    def _custom_headers(self):
        out = {}
        for index, name in enumerate(CUSTOM_HEADERS, start=1):
            value = request.httprequest.headers.get(name)
            if not value:
                # Werkzeug drops header names containing '_', which is how the
                # spec spells these, so only a proxy-renamed dashed form lands.
                value = request.httprequest.headers.get(name.replace("_", "-"))
            out["header_%02d" % index] = value or False
        return out

    def _peek_dealer_id(self, root):
        node = root.find("dealer-id")
        if node is None or not node.text:
            return False
        return node.text.strip() or False
