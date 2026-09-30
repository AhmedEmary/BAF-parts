from unittest.mock import patch

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
        response = self.url_open(path, data=data, headers=headers)
        # Every answer, success or error, must satisfy LexCom's own schema.
        self.assert_lexcom_schema(response.content)
        return response

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

    @mute_logger(_CTRL_LOGGER)
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

    # ── custom headers ───────────────────────────────────────────────────────

    def _post_with_headers(self, extra):
        headers = {"Content-Type": "text/xml", "Authorization": self.basic_auth()}
        headers.update(extra)
        with self.assertLogs(_CTRL_LOGGER, "INFO") as logs:
            self.url_open(
                "/lexcom/commands-available", data=AVAILABLE, headers=headers
            )
        return "\n".join(logs.output)

    def test_custom_headers_are_logged(self):
        """The spec gives X_LC_HEADER_01..05 no meaning, so they are only logged."""
        log = self._post_with_headers(
            {"X-LC-HEADER-%02d" % i: "hdr-%d" % i for i in range(1, 6)}
        )
        for i in range(1, 6):
            self.assertIn("'header_%02d': 'hdr-%d'" % (i, i), log)

    def test_underscore_custom_headers_never_reach_odoo(self):
        """Pins a deployment requirement, not our code.

        The spec spells the names X_LC_HEADER_01, and werkzeug drops every
        header whose name contains '_' (werkzeug/serving.py, make_environ)
        before Odoo sees the request. A reverse proxy must forward them
        dash-separated, or they are lost.
        """
        log = self._post_with_headers({"X_LC_HEADER_01": "hdr-1"})
        self.assertIn("commands-available", log)
        self.assertNotIn("hdr-1", log)

    # ── server log ───────────────────────────────────────────────────────────

    def test_request_is_logged_without_its_payload(self):
        """One odoo.log line per request, as Alzura logs its fetches."""
        with self.assertLogs(_CTRL_LOGGER, "INFO") as logs:
            self._post()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("commands-available", logs.output[0])
        self.assertIn(": ok", logs.output[0])
        self.assertNotIn("<", logs.output[0], "the XML payload must not be logged")

    def test_failed_authentication_is_a_warning(self):
        with self.assertLogs(_CTRL_LOGGER, "WARNING") as logs:
            self._post(auth=False)
        self.assertIn("auth_failed", logs.output[0])
