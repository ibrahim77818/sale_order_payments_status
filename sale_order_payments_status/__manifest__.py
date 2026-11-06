# -*- coding: utf-8 -*-
{
    'name': 'Sales Order Payment Status',
    'version': '18.0.1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Payment Details tab on sales orders: paid / partially paid / not paid '
               'ribbons, invoiced, paid and due amounts, and Register Payment from the order.',
    'description': """
Sales Order Payment Status
==========================
* Dedicated *Payment Details* tab on the sales order form.
* Payment status ribbons: Paid, In Payment, Partially Paid, Not Paid, Fully Credited.
* Total invoiced, total paid, amount due (order currency and company currency),
  paid percentage and the list of posted customer invoices / credit notes.
* Stored, dependency-driven computations: partial payments, several invoices
  per order, credit notes, down payments and foreign currencies.
* Register Payment directly from the order (form button, header button and
  list action), restricted to accounting users and blocked while an
  unreconciled credit note would make the customer pay too much.
* Payment status column, filters and grouping in the sales lists.
* Full Arabic (ar_001) translation, RTL ready.
    """,
    'author': 'Ibrahim Yahia',
    'website': 'https://www.linkedin.com/in/ibrahim-yahia',
    'license': 'LGPL-3',
    'depends': ['sale', 'account'],
    'data': [
        'views/sale_order_views.xml',
        'views/sale_order_actions.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'sale_order_payments_status/static/src/scss/payment_status.scss',
        ],
    },
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
