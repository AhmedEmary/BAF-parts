# Copyright 2026 AgenticBrains
# License OPL-1 (see LICENSE file for full text).
{
    "name": "AgenticBrains MCP Server for Odoo",
    "version": "19.0.3.0.0",
    "summary": (
        "Plug AI assistants into Odoo through a native Model Context "
        "Protocol endpoint - by AgenticBrains."
    ),
    "description": """
AgenticBrains MCP Server for Odoo
=================================

Turn Odoo into a first-class Model Context Protocol server so AI assistants
(Claude, ChatGPT, Copilot, Gemini, Cursor, VS Code, ...) can talk to your
data over natural language - safely, and under your control.

Highlights
----------
* Native MCP endpoint at ``POST /mcp`` (Streamable HTTP, JSON-RPC 2.0) -
  point any MCP client straight at your Odoo URL, no side-car process.
* Full CRUD toolkit: search, get, create, update, delete, aggregate, and
  invoke public business methods on opted-in models.
* User context on connect: the initialize handshake advertises the caller's
  timezone plus active and allowed companies (and a ``get_current_context``
  tool for clients that ignore instructions).
* ``MCP only`` API key scope: mint a key from My Profile that only
  authenticates on ``/mcp`` - a much smaller blast radius when it leaks.
* Read-only OAuth consent: the consent screen exposes an
  ``Allow creating and modifying data`` switch users can uncheck to grant a
  read-only ``mcp:read`` session (write tools are hidden and refused).
* Curated custom tools: admins expose named verbs (e.g. ``confirm_sale_order``)
  by wrapping an ``ir.actions.server`` Python-Code action.
* Per-model access matrix, secure API-key auth, per-user rate limiting and a
  full audit trail (``mcp.log``).
* Built-in OAuth 2.1 Authorization Server with dynamic client registration
  (RFC 7591) and PKCE - so browser-hosted clients can log in with Odoo.
* Modernised configuration UI grouped under Settings > MCP Server.

Setup at a glance
-----------------
1. Install the module and flip ``Enable MCP Server`` in Settings.
2. Whitelist the models you want to expose with their per-operation flags.
3. Either mint an API key (My Profile > Account Security) or let clients
   sign in through the built-in OAuth flow.

Requires Odoo 19.0. The ``/mcp`` endpoint needs no extra software; the
module's legacy XML-RPC surface remains available for RPC bridges.
    """,
    "author": "AgenticBrains",
    "website": "https://agenticbrain.tech/",
    "support": "info@agenticbrain.tech",
    "maintainer": "AgenticBrains",
    "category": "Productivity",
    "depends": ["base", "base_setup", "mail", "rpc", "web"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "wizard/mcp_model_selection_wizard_views.xml",
        "wizard/oauth_bulk_confirm_wizard_views.xml",
        "views/mcp_enabled_models_views.xml",
        "views/mcp_custom_tool_views.xml",
        "views/mcp_log_views.xml",
        "views/oauth_views.xml",
        "views/res_config_settings_views.xml",
        "views/res_users_apikeys_views.xml",
        "views/mcp_menu.xml",
        "views/oauth_consent_templates.xml",
        "data/oauth_cron.xml",
    ],
    "demo": [],
    "images": [
        "static/description/banner.gif",
        "static/description/icon.png",
    ],
    "external_dependencies": {
        "python": [
            "authlib>=1.6.12,<1.7.0",
            "defusedxml",
            "packaging",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "OPL-1",
}
