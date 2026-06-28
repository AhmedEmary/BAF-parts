{
    "name": "Alzura B2B Integration",
    "version": "19.0.1.0.0",
    "category": "Sales/Purchase",
    "summary": "Connect Odoo to the Alzura B2B automotive marketplace for tires, rims & spare parts.",
    "description": """

This module integrates your Odoo instance with the Alzura B2B REST API , allowing you to:

- Authenticate with the Alzura API directly from Odoo Settings
- Store the authentication token securely per company
- Use the token to communicate with Alzura API endpoints

**How to configure?**
----------------------
1. Go to **Settings → Alzura B2B**
2. Enter your Alzura ID and Password
3. Click **Get Token**
4. A success message confirms the token is saved and ready to use

The password is **never stored** — only the authentication token is saved.
Token is valid for 24 hours and must be refreshed before expiry.
    """,
    "author": "Mohamed Mamdouh",
    "website": "",
    "depends": ["base", "base_setup", "sale", "general_system_custom"],
    "data": [
        "data/so_source_data.xml",
        "data/alzura_cron.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
