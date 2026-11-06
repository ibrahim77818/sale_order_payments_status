# Sales Order Payment Status (Odoo 18.0)

See on every sales order whether the customer has paid, paid part, or paid nothing, with the
exact amount still due, and register the payment from the order itself.
Community and Enterprise. English and Arabic (`ar_001`), right-to-left ready.

Author: [Ibrahim Yahia](https://www.linkedin.com/in/ibrahim-yahia) · License: LGPL-3

## Branches

Each branch holds the code for one Odoo version:

| Branch | Odoo | `sale_order_payments_status` |
|---|---|---|
| `16.0` | 16.0 | 16.0.1.0.0 |
| `17.0` | 17.0 | 17.0.1.0.0 |
| `18.0` | 18.0 | 18.0.1.0.0 |
| `19.0` | 19.0 | 19.0.1.0.0 |

This branch: **18.0** (`sale_order_payments_status` 18.0.1.0.0).

Depends on `sale` and `account`.

## Features

- **Payment Details** tab on confirmed sales orders: a status ribbon (Paid, In Payment, Partially Paid, Not Paid, Fully Credited), total invoiced, total paid, amount due (order currency and company currency), paid percentage, and the posted invoices and credit notes of the order with their own payment status.
- **Register Payment** from the order: in the header, in the tab, and as a list action for several orders. It opens Odoo's standard payment wizard on the order's open invoices, so journals, payment methods, partial payments, write-offs and grouping behave exactly as from the invoice.
- **Payments** smart button listing the payments matched with the order's invoices.
- Payment Status and Amount Due columns in the sales lists, with totals; filters *Not Paid*, *Partially Paid*, *In Payment*, *Paid*, *With Amount Due*; *Group By › Payment Status*.
- Stored, dependency-driven figures: several invoices per order, down payments, credit notes and foreign currencies; posting, paying, unreconciling or reversing an invoice updates the order immediately.
- Payments registered **without journal entry** (Odoo 18+, typically Enterprise when the bank journal has no outstanding account) are counted as paid on the order, spread over the invoices they were registered for, oldest due date first.
- Accounting controls: only *Billing* users and above can register payments; while an invoice and a credit note of the order are both open, registration is refused so the customer is never charged more than the amount due.

## Payment statuses

| Status | Meaning |
|---|---|
| Not Invoiced | no posted invoice or credit note yet |
| Not Paid | amount due ≥ total invoiced |
| Partially Paid | something paid, something still due |
| In Payment | nothing due, a payment not yet matched with the bank statement (Enterprise) |
| Paid | nothing due |
| Fully Credited | invoices fully cancelled by credit notes |

Total Invoiced = invoices − credit notes · Amount Due = invoice residuals − credit-note
residuals · Total Paid = Total Invoiced − Amount Due.

## Notes for Odoo 18.0

- Since Odoo 17, a credit note created with **Reverse** is reconciled with its invoice when posted, so the open-credit-note block mainly concerns credit notes entered by hand.
- Since Odoo 18, a payment can exist without journal entry: the invoice shows *In Payment* with its full residual until the bank statement is reconciled. The order deducts such payments from the amount due. Odoo's own Pay wizard still proposes the full residual for those invoices (with a "payments in progress" warning).

## Installation

1. Put the `sale_order_payments_status` folder in your addons path (on Odoo.sh, push this
   branch to your repository).
2. Install **Sales Order Payment Status**. No configuration: existing orders are computed at
   install.
3. Open a confirmed order in **Sales › Orders › Sales Orders** and go to the
   **Payment Details** tab.

## Tests

20 automated `TransactionCase` tests:

```
odoo-bin -d <db> -i sale_order_payments_status --test-tags /sale_order_payments_status
```

## Other versions

`16.0`, `17.0`, `19.0`
