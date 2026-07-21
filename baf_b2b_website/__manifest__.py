{
    'name': 'BAF B2B Website',
    'version': '1.0',
    'category': 'Website',
    'author': 'Ahmed Elamery',
    'summary': 'Static BAF Parts B2B website',
    'description': 'Static export of the BAF Parts B2B website (home, B2B order page, pricefile, about, contact, help).',
    'depends': ['website', 'b2b_custom'],
    'data': [
        'data/pages.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'baf_b2b_website/static/src/scss/baf_pages.scss',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
