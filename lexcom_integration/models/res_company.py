import hashlib
import hmac
import os

from odoo import fields, models

# Iteration count for the stored credential hash. Basic Auth sends the password
# on every single request, so this is verified per call: high enough to matter,
# low enough not to dominate request latency.
PBKDF2_ITERATIONS = 260000
_HASH_PREFIX = "pbkdf2_sha256"


def verify_lexcom_password(stored, password):
    """Check ``password`` against ``stored``, at a cost independent of ``stored``.

    ``stored`` may legitimately be ``None`` - no credential configured, or no
    such user at all. Every one of those cases must cost the same as a real
    verification, because PBKDF2 is deliberately expensive (260k iterations,
    tens of milliseconds) and skipping it is externally visible.

    An attacker who can time responses would otherwise submit a list of
    candidate usernames with a junk password: the ones that come back slowly
    exist, the ones that come back fast do not. Identical response bodies do
    not help - the clock is the oracle. So the failure paths below hash too,
    and the caller must pass a missing credential through here rather than
    returning early.
    """
    encoded = (password or "").encode("utf-8")
    try:
        prefix, iterations, salt_hex, digest_hex = (stored or "").split("$")
        if prefix != _HASH_PREFIX:
            raise ValueError("unsupported hash scheme")
        iterations = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        hashlib.pbkdf2_hmac("sha256", encoded, b"\x00" * 16, PBKDF2_ITERATIONS)
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", encoded, salt, iterations)
    return hmac.compare_digest(candidate, expected)


class ResCompany(models.Model):
    _inherit = "res.company"

    lexcom_dealer_id = fields.Char(
        string="LexCom Dealer ID",
        groups="base.group_system",
        help="The dealer-id LexCom sends in every request. A request whose "
             "dealer-id does not match this value is rejected.",
    )
    lexcom_username = fields.Char(
        string="LexCom Username",
        groups="base.group_system",
        help="HTTP Basic Auth username. Chosen freely by us and given to LexCom.",
    )
    lexcom_password_hash = fields.Char(
        string="LexCom Password Hash",
        groups="base.group_system",
        readonly=True,
        help="Salted PBKDF2 hash of the Basic Auth password. The password "
             "itself is never stored.",
    )
    lexcom_enabled = fields.Boolean(
        string="LexCom Endpoint Enabled",
        groups="base.group_system",
        default=False,
        help="Kill switch. When off, every request is rejected without "
             "touching the database. Takes effect immediately, no deploy.",
    )
    lexcom_country = fields.Char(
        string="LexCom Country",
        groups="base.group_system",
        default="DEU",
        help="ISO 3166-1 alpha-3 country code echoed back in every response. "
             "Stored as a plain string: the spec uses alpha-3 here and Odoo's "
             "res.country only carries alpha-2, so there is nothing to resolve.",
    )

    def _lexcom_set_password(self, password):
        """Store a salted PBKDF2 hash of ``password``; never the password."""
        self.ensure_one()
        if not password:
            self.sudo().lexcom_password_hash = False
            return
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        self.sudo().lexcom_password_hash = "%s$%d$%s$%s" % (
            _HASH_PREFIX,
            PBKDF2_ITERATIONS,
            salt.hex(),
            digest.hex(),
        )

    def _lexcom_check_password(self, password):
        """True when ``password`` matches this company's stored hash."""
        self.ensure_one()
        return verify_lexcom_password(self.sudo().lexcom_password_hash, password)
