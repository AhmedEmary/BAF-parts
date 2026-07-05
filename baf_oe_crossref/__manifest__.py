{
    'name': "BAF OEM ↔ Aftermarket Cross-Reference",
    'summary': "Show Inter Cars aftermarket equivalents on the OEM product "
               "page and route the sale through the drop-ship flow.",
    'category': 'Website/eCommerce',
    'version': '1.0',
    'depends': [
        'ic_intercars',
        'website_sale',
        'b2b_custom',
        # contacts is only listed because b2b_custom references
        # contacts.menu_contacts without declaring the dep itself.
        'contacts',
    ],
    'installable': True,
    'data': [
        'security/ir.model.access.csv',
        'views/baf_oe_link_views.xml',
        'views/product_template_views.xml',
        'views/website_views.xml',
        'views/website_sale_templates.xml',
        'data/ir_cron.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'baf_oe_crossref/static/src/js/aftermarket_cart.js',
        ],
    },
    'author': 'Ahmed Elamery',
    'license': 'LGPL-3',
}
