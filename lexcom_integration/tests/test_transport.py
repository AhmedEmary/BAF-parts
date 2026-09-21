from unittest.mock import patch

from odoo import SUPERUSER_ID, api
from odoo.tests.common import HttpCase, tagged
from odoo.tools import mute_logger

from odoo.addons.lexcom_integration.controllers.lexcom import LexcomController
from odoo.addons.lexcom_integration.models.lexcom_protocol import LexcomProtocol

from .common import LexcomCommon

_CTRL_LOGGER = "odoo.addons.lexcom_integration.controllers.lexcom"

AVAILABLE = b'<?xml version="1.0" encoding="UTF-8"?><commands-available/>'


@tagged("post_install", "-at_install")
class TestLexcomTransport(HttpCase, LexcomCommon):
    """Transport-level behaviour that only a real HTTP request can exercise."""

    def _post(self, path="/lexcom/commands-available", data=AVAILABLE,
              auth=True, content_type="text/xml"):
        headers = {"Content-Type": content_type}
        if auth:
            headers["Authorization"] = self.basic_auth()
        return self.url_open(path, data=data, headers=headers)

    # ── the Odoo 19 traps ────────────────────────────────────────────────────

    def test_route_is_read_write_not_readonly(self):
        """Guards odoo/http.py:974.

        `readonly` defaults to True for auth='none'. If that default ever comes
        back, Odoo silently re-runs the entire handler on every write instead of
        failing, so nothing else in this suite would notice.
        """
        routing = LexcomController.lexcom_dispatch.original_routing
        self.assertIn("readonly", routing)
        self.assertFalse(
            routing["readonly"],
            "the route must be read/write; see odoo/http.py:974",
        )

    def test_route_declares_auth_none_and_no_csrf(self):
        routing = LexcomController.lexcom_dispatch.original_routing
        self.assertEqual(routing["auth"], "none")
        self.assertFalse(routing["csrf"])
        self.assertEqual(routing["methods"], ["POST"])

    # ── status codes: only 200 and 5xx exist ─────────────────────────────────

    def test_valid_request_returns_200_xml(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/xml", response.headers.get("Content-Type", ""))
        self.assertIn(b"commands-available-response", response.content)

    def test_malformed_xml_returns_200_with_error(self):
        response = self._post(data=b"<not-xml")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<error", response.content)

    def test_unknown_command_returns_200_with_error(self):
        response = self._post(path="/lexcom/no-such-command")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<error", response.content)

    def test_wrong_version_returns_200_with_error(self):
        body = (b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<order-submit version="2.0"><dealer-id>BAF1</dealer-id>'
                b'<country>DEU</country><brand>Audi</brand></order-submit>')
        response = self._post(path="/lexcom/order-submit", data=body)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<error", response.content)

    @mute_logger(_CTRL_LOGGER)
    def test_internal_error_returns_5xx(self):
        """The spec's single non-200 allowance, used only for our own bugs.

        Answering 200 here would make a broken deployment indistinguishable
        from a run of ordinary business rejections.
        """
        with patch.object(
            LexcomProtocol, "_handle_commands_available",
            side_effect=RuntimeError("boom"),
        ):
            response = self._post()
        self.assertGreaterEqual(response.status_code, 500)

    def test_no_response_is_ever_401(self):
        """The spec permits exactly two codes: 200, and 5xx for a server error."""
        for kwargs in (
            {"auth": False},
            {},
        ):
            response = self._post(**kwargs)
            self.assertNotEqual(response.status_code, 401)
            self.assertNotIn("WWW-Authenticate", response.headers)

    # ── XML hardening ────────────────────────────────────────────────────────

    def test_external_entity_is_not_resolved(self):
        """XXE: the response must never carry the contents of a local file."""
        body = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b'<commands-available>&xxe;</commands-available>'
        )
        response = self._post(data=body)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"root:", response.content)

    def test_entity_expansion_is_not_performed(self):
        """Billion laughs: the response must stay small."""
        body = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
            b'<commands-available>&lol3;</commands-available>'
        )
        response = self._post(data=body)
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 10000)

    def test_oversized_body_is_rejected_before_parsing(self):
        from odoo.addons.lexcom_integration.controllers.lexcom import (
            MAX_BODY_BYTES,
        )
        response = self._post(data=b"x" * (MAX_BODY_BYTES + 1))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<error", response.content)

    # ── request log durability ───────────────────────────────────────────────

    def test_log_row_is_written_on_an_independent_cursor(self):
        """The log commits outside the request transaction.

        Proof without a rollback (Odoo forbids one inside a test): a row
        written on the *test* cursor is invisible to any other connection until
        the test commits, which it never does. So if a second, independent
        cursor can see this row while our transaction is still open, the write
        must have happened on a cursor of its own - which is exactly what makes
        it survive the rollback or the service_model.retrying re-run that would
        otherwise discard it.
        """
        marker = "independent-cursor-probe"
        self.env["lexcom.request.log"]._lexcom_log({
            "command": marker, "outcome": "ok", "http_status": 200,
        })

        registry = self.env.registry
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            found = env["lexcom.request.log"].search([("command", "=", marker)])
            self.assertTrue(
                found,
                "an independent connection must see the row, proving it was "
                "not written on the request cursor",
            )
            found.unlink()
