{
    "name": "partslink24 / LexCom DMS Integration",
    "version": "19.0.1.0.0",
    "category": "Sales",
    "summary": "Serve the LexCom Standard DMS interface (3.1) so partslink24 and "
               "the LexCom EPCs can submit orders straight into Odoo.",
    "description": """
LexCom Standard DMS Integration
===============================

Unlike the Alzura integration, where Odoo polls a remote REST API, **here Odoo
is the server**: LexCom's COMboxWeb middleware POSTs XML to us over HTTPS with
HTTP Basic authentication.

Implemented commands (DMS Protocol Specification 3.1):

* ``commands-available`` - connectivity check, advertises what we support
* ``order-submit``       - creates a confirmed sale order, atomically
* ``order-list``         - lists the open orders an append may target
* ``order-append``       - appends items to an existing open order

LexCom uses ``order-list`` and ``order-append`` together: the EPC finds the
order with the first and adds parts with the second. ``stock-information`` is
deliberately deferred; ``order-get`` is out of scope.

**Status codes.** The specification permits exactly two: 200 for every outcome
the handler reaches (including a rejected credential and every business error),
and 5xx only for a critical server error. No code path returns 401.

**How to configure?**
---------------------
1. Go to **Settings -> partslink24 / LexCom DMS**
2. Enter the Dealer ID, the username, and set a password
3. Enable the endpoint

The password is stored only as a salted PBKDF2 hash.
    """,
    "author": "Mohamed Mamdouh",
    "website": "",
    "depends": ["base", "base_setup", "sale", "phone_validation",
                "general_system_custom"],
    "data": [
        "data/so_source_data.xml",
        "data/res_partner_data.xml",
        "views/res_config_settings_views.xml",
        "views/res_partner_views.xml",
        "views/product_brand_views.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
