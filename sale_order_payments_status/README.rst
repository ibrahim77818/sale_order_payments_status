==========================
Sales Order Payment Status
==========================

Odoo 17.0 (Community & Enterprise). Depends on ``sale`` and ``account``.

What it adds
============
* **Payment Details** tab (تفاصيل الدفع) on confirmed sales orders, with a status
  ribbon (Paid, In Payment, Partially Paid, Not Paid, Fully Credited), total
  invoiced, total paid, amount due (order and company currency), paid percentage
  and the posted invoices / credit notes of the order.
* **Register Payment** in the order header, in the tab, and as a list action for
  several orders. It opens the standard ``account.payment.register`` wizard on the
  order's open invoices, so journals, payment methods, partial payments, write-offs
  and grouping behave exactly as from the invoice.
* **Payments** smart button listing the payments reconciled with the order's invoices.
* Payment status and amount due columns, filters and a *Group By Payment Status*
  in the sales lists.

How the figures are computed
============================
Only *posted* customer invoices (``out_invoice``) and credit notes (``out_refund``)
linked through the order lines are counted; drafts and cancelled documents are
ignored.

* Total Invoiced = invoices − credit notes
* Amount Due = invoice residuals − credit-note residuals (negative = customer credit)
* Total Paid = Total Invoiced − Amount Due

Amounts are taken in the document currency when it matches the order currency
(no rounding drift), otherwise converted from the company-currency amounts at the
invoice date. *Amount Due (Company Currency)* sums ``amount_residual_signed`` and is
the figure to total across currencies.

All fields are stored and recomputed by ORM dependencies on
``order_line.invoice_lines.move_id`` (state, totals, residuals, payment state,
currency), so posting, paying, unreconciling or reversing an invoice updates the
order immediately and the fields can be searched and grouped.

Status rules
------------
==============  ==========================================================
Not Invoiced    no posted invoice or credit note
Not Paid        amount due ≥ total invoiced
Partially Paid  0 < amount due < total invoiced
In Payment      nothing due, an invoice is *In Payment* (Enterprise: payment
                not yet matched with the bank statement)
Paid            nothing due
Fully Credited  invoices fully cancelled by credit notes
==============  ==========================================================

Accounting controls
===================
* Registering a payment requires the *Billing* group (``account.group_account_invoice``)
  and create rights on ``account.payment``; sales users see the figures but not the button.
* If an invoice and a credit note of the order are both still open, registration is
  refused until the credit note is reconciled with the invoice (*Outstanding credits*
  on the invoice), so the customer is never asked for more than the amount due.
  In Odoo 17 a credit note made with *Reverse* is reconciled with its invoice when
  posted, so this mainly concerns credit notes entered by hand.

Tests
=====
``tests/test_payment_status.py`` (``TransactionCase`` via ``AccountTestInvoicingCommon``)::

    odoo-bin -d <db> -i sale_order_payments_status --test-tags /sale_order_payments_status

Author: Ibrahim Yahia — https://www.linkedin.com/in/ibrahim-yahia
