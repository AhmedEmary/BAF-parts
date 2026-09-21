from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged

from .common import LexcomCommon, PASSWORD, USERNAME

AVAILABLE = b'<?xml version="1.0" encoding="UTF-8"?><commands-available/>'


@tagged("post_install", "-at_install")
class TestLexcomAuth(HttpCase, LexcomCommon):
    """Every rejection is HTTP 200 with an error document.

    The specification permits only 200 and 5xx, and a bad password is not a
    critical server error, so there is no 401 anywhere in this module.
    """

    def _post(self, headers=None):
        base = {"Content-Type": "text/xml"}
        base.update(headers or {})
        return self.url_open(
            "/lexcom/commands-available", data=AVAILABLE, headers=base
        )

    def _assert_rejected(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.status_code, 401)
        self.assertIn(b"<error", response.content)
        self.assertNotIn(b"commands-available-response", response.content)

    def test_valid_credentials_are_accepted(self):
        response = self._post({"Authorization": self.basic_auth()})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"commands-available-response", response.content)

    def test_missing_authorization_header(self):
        self._assert_rejected(self._post())

    def test_wrong_scheme(self):
        self._assert_rejected(self._post({"Authorization": "Bearer abc123"}))

    def test_malformed_base64(self):
        self._assert_rejected(
            self._post({"Authorization": "Basic !!!not-base64!!!"})
        )

    def test_unknown_username(self):
        self._assert_rejected(
            self._post({"Authorization": self.basic_auth("nobody", PASSWORD)})
        )

    def test_wrong_password(self):
        self._assert_rejected(
            self._post({"Authorization": self.basic_auth(USERNAME, "wrong")})
        )

    def test_disabled_endpoint_rejects_valid_credentials(self):
        """The kill switch must work without a deploy."""
        self.company.sudo().lexcom_enabled = False
        self.env.cr.flush()
        self._assert_rejected(self._post({"Authorization": self.basic_auth()}))

    def test_password_is_not_stored_in_plaintext(self):
        stored = self.company.sudo().lexcom_password_hash
        self.assertTrue(stored)
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))

    def test_password_check_is_correct(self):
        self.assertTrue(self.company.sudo()._lexcom_check_password(PASSWORD))
        self.assertFalse(self.company.sudo()._lexcom_check_password("nope"))
        self.assertFalse(self.company.sudo()._lexcom_check_password(""))

    # ── timing side channel ──────────────────────────────────────────────────

    def _pbkdf2_calls_for(self, headers):
        """How many PBKDF2 rounds a rejected request costs.

        PBKDF2 is deliberately expensive (260k iterations, tens of ms). If one
        rejection path runs it and another does not, the two are trivially
        distinguishable by response time even though their bodies are
        identical - which turns a guessed username into a confirmed one.
        """
        from odoo.addons.lexcom_integration.models import res_company

        calls = []
        real = res_company.hashlib.pbkdf2_hmac

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        with patch.object(res_company.hashlib, "pbkdf2_hmac", counting):
            response = self._post(headers)
        self.assertEqual(response.status_code, 200)
        return len(calls)

    def test_unknown_username_costs_the_same_as_wrong_password(self):
        """Both rejections must do equal hashing work.

        Otherwise an attacker times a list of candidate usernames: the slow one
        is real. See security review finding 2.
        """
        unknown = self._pbkdf2_calls_for(
            {"Authorization": self.basic_auth("no-such-user", "irrelevant")}
        )
        wrong_password = self._pbkdf2_calls_for(
            {"Authorization": self.basic_auth(USERNAME, "definitely-wrong")}
        )
        self.assertEqual(
            unknown, wrong_password,
            "an unknown username must cost the same PBKDF2 work as a wrong "
            "password, or response latency reveals which usernames exist",
        )
        self.assertGreater(unknown, 0, "both paths must actually hash")

    def test_disabled_endpoint_costs_the_same_as_wrong_password(self):
        """The kill switch must not become a timing oracle either."""
        wrong_password = self._pbkdf2_calls_for(
            {"Authorization": self.basic_auth(USERNAME, "definitely-wrong")}
        )
        self.company.sudo().lexcom_enabled = False
        self.env.cr.flush()
        disabled = self._pbkdf2_calls_for(
            {"Authorization": self.basic_auth()}
        )
        self.assertEqual(disabled, wrong_password)
