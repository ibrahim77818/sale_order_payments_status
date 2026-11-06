# -*- coding: utf-8 -*-
from contextlib import contextmanager
from unittest.mock import patch

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tools import float_compare


@tagged('post_install', '-at_install', 'sale_order_payments_status')
class TestSaleOrderPaymentStatus(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data['company']
        cls.bank_journal = cls.company_data['default_journal_bank']
        # Odoo 18: currency_data is gone. 3 decimals, rate 2.0 since 2017.
        cls.gold = cls.setup_other_currency('EUR', rounding=0.001)

        cls.product = cls.env['product.product'].create({
            'name': 'Consulting Day',
            'type': 'service',
            'invoice_policy': 'order',
            'list_price': 100.0,
            'taxes_id': [Command.clear()],
        })
        cls.pricelist_company = cls.env['product.pricelist'].create({
            'name': 'Company currency',
            'currency_id': cls.company.currency_id.id,
        })
        cls.pricelist_gold = cls.env['product.pricelist'].create({
            'name': 'Gold',
            'currency_id': cls.gold.id,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_order(self, lines=((10, 100.0),), pricelist=None, partner=None, confirm=True):
        order = self.env['sale.order'].create({
            'partner_id': (partner or self.partner_a).id,
            'pricelist_id': (pricelist or self.pricelist_company).id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': qty,
                'price_unit': price,
                'tax_id': [Command.clear()],
            }) for qty, price in lines],
        })
        if confirm:
            order.action_confirm()
        return order

    def _invoice(self, order, post=True):
        invoice = order._create_invoices(final=True)
        if post:
            invoice.action_post()
        return invoice

    def _pay(self, orders, amount=None, currency=None, date=None):
        action = orders.action_register_order_payment()
        self.assertEqual(action['res_model'], 'account.payment.register')
        vals = {
            'journal_id': self.bank_journal.id,
            'payment_date': date or fields.Date.today(),
        }
        if amount is not None:
            vals['amount'] = amount
        if currency is not None:
            vals['currency_id'] = currency.id
        wizard = self.env['account.payment.register'].with_context(**action['context']).create(vals)
        return wizard._create_payments()

    def _action_moves(self, action):
        """Invoices targeted by a Register Payment action.

        Odoo 17 passes the journal items (active_model account.move.line) to the wizard,
        Odoo 16 passed the invoices themselves.
        """
        ctx = action['context']
        records = self.env[ctx['active_model']].browse(ctx['active_ids'])
        return records.move_id if ctx['active_model'] == 'account.move.line' else records

    def _manual_credit_note(self, order, quantity, price=100.0):
        """A credit note created by hand (not through Reverse), linked to the order line."""
        line = order.order_line[0]
        refund = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': order.partner_id.id,
            'invoice_date': fields.Date.today(),
            'currency_id': order.currency_id.id,
            'invoice_line_ids': [Command.create({
                'product_id': line.product_id.id,
                'quantity': quantity,
                'price_unit': price,
                'tax_ids': [Command.clear()],
                'sale_line_ids': [Command.set(line.ids)],
            })],
        })
        refund.action_post()
        return refund

    def _refund(self, invoice, quantity=None, cancel=False):
        # Odoo 17: the wizard has no refund_method any more. "Reverse" (refund_moves) creates
        # a draft credit note; "Reverse and Create Invoice" (modify_moves) posts a credit note
        # reconciled with the invoice and opens a new draft copy of it.
        reversal = self.env['account.move.reversal'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({
            'reason': 'Test credit note',
            'journal_id': invoice.journal_id.id,
        })
        if cancel:
            reversal.modify_moves()
            return invoice.reversal_move_ids[-1:]
        reversal.refund_moves()
        refund = invoice.reversal_move_ids[-1:]
        if quantity is not None:
            line = refund.invoice_line_ids[0]
            refund.write({'invoice_line_ids': [Command.update(line.id, {'quantity': quantity})]})
        refund.action_post()
        return refund

    def assertAmounts(self, order, invoiced, paid, due, status):
        rounding = order.currency_id.rounding
        for field, expected in (('order_invoiced_amount', invoiced),
                                ('order_paid_amount', paid),
                                ('order_amount_due', due)):
            self.assertEqual(
                float_compare(order[field], expected, precision_rounding=rounding), 0,
                '%s: expected %s, got %s' % (field, expected, order[field]),
            )
        self.assertEqual(order.order_payment_status, status)

    @contextmanager
    def _payments_without_journal_entry(self):
        """Odoo 18 Enterprise default: the bank journal has no outstanding account, so a
        registered payment gets no journal entry until the bank statement is reconciled."""
        lines = self.bank_journal.inbound_payment_method_line_ids
        accounts = {line: line.payment_account_id for line in lines}
        lines.payment_account_id = False
        with patch.object(type(self.env['account.move']), '_get_invoice_in_payment_state',
                          lambda move: 'in_payment'), \
                patch.object(type(self.env['account.payment']), '_get_outstanding_account',
                             lambda payment, payment_type: self.env['account.account']):
            yield
        for line, account in accounts.items():
            line.payment_account_id = account

    def _fully_paid_status(self, invoices):
        # Community returns 'paid'; Enterprise (account_accountant) returns 'in_payment'
        # until the payment is matched with a bank statement line.
        return 'in_payment' if 'in_payment' in invoices.mapped('payment_state') else 'paid'

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def test_01_no_invoice(self):
        order = self._create_order()
        self.assertAmounts(order, 0.0, 0.0, 0.0, 'no_invoice')
        self.assertEqual(order.order_open_invoice_count, 0)
        self.assertEqual(order.order_paid_percent, 0.0)
        with self.assertRaises(UserError):
            order.action_register_order_payment()

    def test_02_draft_and_cancelled_invoices_are_ignored(self):
        order = self._create_order()
        invoice = self._invoice(order, post=False)
        self.assertAmounts(order, 0.0, 0.0, 0.0, 'no_invoice')
        invoice.action_post()
        self.assertAmounts(order, 1000.0, 0.0, 1000.0, 'not_paid')
        invoice.button_draft()
        invoice.button_cancel()
        self.assertAmounts(order, 0.0, 0.0, 0.0, 'no_invoice')

    def test_03_not_paid_partial_paid(self):
        order = self._create_order()
        invoice = self._invoice(order)
        self.assertAmounts(order, 1000.0, 0.0, 1000.0, 'not_paid')
        self.assertEqual(order.order_open_invoice_count, 1)

        self._pay(order, amount=400.0)
        self.assertAmounts(order, 1000.0, 400.0, 600.0, 'partial')
        self.assertAlmostEqual(order.order_paid_percent, 40.0)
        self.assertEqual(invoice.payment_state, 'partial')

        self._pay(order)  # wizard defaults to the remaining 600
        self.assertAmounts(order, 1000.0, 1000.0, 0.0, self._fully_paid_status(invoice))
        self.assertAlmostEqual(order.order_paid_percent, 100.0)
        self.assertEqual(order.order_open_invoice_count, 0)
        self.assertEqual(order.order_payment_count, 2)
        self.assertEqual(order.order_amount_due_company, 0.0)
        with self.assertRaises(UserError):
            order.action_register_order_payment()

    def test_04_in_payment_status(self):
        """Fully paid but not yet matched with the bank (Enterprise behaviour)."""
        order = self._create_order()
        invoice = self._invoice(order)
        with patch.object(type(self.env['account.move']), '_get_invoice_in_payment_state',
                          lambda move: 'in_payment'):
            self._pay(order)
            self.assertEqual(invoice.payment_state, 'in_payment')
            self.assertAmounts(order, 1000.0, 1000.0, 0.0, 'in_payment')

    def test_06_partial_payment_without_journal_entry(self):
        """Odoo 18: the invoice turns 'In Payment' but keeps its residual; the order must
        still show what was really paid."""
        order = self._create_order()
        invoice = self._invoice(order)
        with self._payments_without_journal_entry():
            payment = self._pay(order, amount=400.0)
            self.assertFalse(payment.move_id)
            self.assertEqual(payment.state, 'in_process')
            self.assertEqual(invoice.payment_state, 'in_payment')
            self.assertEqual(invoice.amount_residual, 1000.0)
            self.assertAmounts(order, 1000.0, 400.0, 600.0, 'partial')
            self.assertEqual(order.order_open_invoice_count, 1)
            self.assertEqual(order.order_amount_due_company, 600.0)
            self.assertEqual(order.order_payment_ids, payment)

            # The rest can still be registered from the order
            action = order.action_register_order_payment()
            self.assertEqual(self._action_moves(action), invoice)
            self._pay(order, amount=600.0)
            self.assertAmounts(order, 1000.0, 1000.0, 0.0, 'in_payment')
            self.assertEqual(order.order_open_invoice_count, 0)
            self.assertEqual(order.order_payment_count, 2)
            with self.assertRaises(UserError):
                order.action_register_order_payment()

    def test_07_grouped_payment_without_journal_entry(self):
        """One payment without journal entry for two orders is spread oldest invoice first."""
        order_1 = self._create_order(lines=((1, 100.0),))
        order_2 = self._create_order(lines=((1, 250.0),))
        invoices = self._invoice(order_1) | self._invoice(order_2)
        with self._payments_without_journal_entry():
            action = (order_1 | order_2).action_register_order_payment()
            self.env['account.payment.register'].with_context(**action['context']).create({
                'journal_id': self.bank_journal.id,
                'group_payment': True,
                'amount': 200.0,
            })._create_payments()
            self.assertEqual(set(invoices.mapped('payment_state')), {'in_payment'})
            self.assertAmounts(order_1, 100.0, 100.0, 0.0, 'in_payment')
            self.assertAmounts(order_2, 250.0, 100.0, 150.0, 'partial')

    def test_08_cancelled_payment_without_journal_entry(self):
        order = self._create_order()
        self._invoice(order)
        with self._payments_without_journal_entry():
            payment = self._pay(order, amount=300.0)
            self.assertAmounts(order, 1000.0, 300.0, 700.0, 'partial')
            payment.action_cancel()
            self.assertAmounts(order, 1000.0, 0.0, 1000.0, 'not_paid')
            self.assertFalse(order.order_payment_ids)

    def test_05_payment_unreconciled_updates_order(self):
        order = self._create_order()
        self._invoice(order)
        payment = self._pay(order, amount=250.0)
        self.assertAmounts(order, 1000.0, 250.0, 750.0, 'partial')
        payment.action_draft()
        self.assertAmounts(order, 1000.0, 0.0, 1000.0, 'not_paid')
        payment.action_post()
        self.assertAmounts(order, 1000.0, 0.0, 1000.0, 'not_paid')  # posted but not reconciled

    # ------------------------------------------------------------------
    # Several invoices per order
    # ------------------------------------------------------------------

    def test_10_multi_invoice_order(self):
        order = self._create_order(lines=((2, 150.0),))
        invoice_1 = self._invoice(order)
        order.write({'order_line': [Command.create({
            'product_id': self.product.id,
            'product_uom_qty': 1,
            'price_unit': 700.0,
            'tax_id': [Command.clear()],
        })]})
        invoice_2 = self._invoice(order)
        self.assertEqual(order.order_payment_move_ids, invoice_1 | invoice_2)
        self.assertAmounts(order, 1000.0, 0.0, 1000.0, 'not_paid')
        self.assertEqual(order.order_open_invoice_count, 2)

        # Pay the first invoice only
        self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=invoice_1.ids,
        ).create({'journal_id': self.bank_journal.id})._create_payments()
        self.assertAmounts(order, 1000.0, 300.0, 700.0, 'partial')
        self.assertEqual(order.order_open_invoice_count, 1)

        # The order button now targets only the remaining invoice
        action = order.action_register_order_payment()
        self.assertEqual(self._action_moves(action), invoice_2)
        self._pay(order)
        self.assertAmounts(order, 1000.0, 1000.0, 0.0, self._fully_paid_status(invoice_1 | invoice_2))

    def test_11_down_payment_then_final_invoice(self):
        order = self._create_order()
        wizard = self.env['sale.advance.payment.inv'].with_context(
            active_model='sale.order', active_ids=order.ids, active_id=order.id,
        ).create({'advance_payment_method': 'percentage', 'amount': 30})
        wizard.create_invoices()
        down_payment = order.invoice_ids
        down_payment.action_post()
        self.assertAmounts(order, 300.0, 0.0, 300.0, 'not_paid')
        self._pay(order)
        self.assertAmounts(order, 300.0, 300.0, 0.0, self._fully_paid_status(down_payment))

        final = self._invoice(order)
        self.assertEqual(final.amount_total, 700.0)
        self.assertAmounts(order, 1000.0, 300.0, 700.0, 'partial')
        self._pay(order)
        self.assertAmounts(order, 1000.0, 1000.0, 0.0, self._fully_paid_status(down_payment | final))

    def test_12_register_payment_for_several_orders(self):
        order_1 = self._create_order(lines=((1, 100.0),))
        order_2 = self._create_order(lines=((1, 250.0),))
        invoices = self._invoice(order_1) | self._invoice(order_2)
        action = self.env.ref('sale_order_payments_status.action_server_register_order_payment').with_context(
            active_model='sale.order', active_ids=(order_1 | order_2).ids,
        ).run()
        self.assertEqual(self._action_moves(action), invoices)
        self.env['account.payment.register'].with_context(**action['context']).create({
            'journal_id': self.bank_journal.id,
            'group_payment': True,
        })._create_payments()
        self.assertAmounts(order_1, 100.0, 100.0, 0.0, self._fully_paid_status(invoices))
        self.assertAmounts(order_2, 250.0, 250.0, 0.0, self._fully_paid_status(invoices))

    # ------------------------------------------------------------------
    # Credit notes
    # ------------------------------------------------------------------

    def test_20_partial_credit_note(self):
        """Odoo 17: a credit note made with Reverse is reconciled with its invoice on posting."""
        order = self._create_order()
        invoice = self._invoice(order)
        refund = self._refund(invoice, quantity=3)
        self.assertEqual(refund.amount_total, 300.0)
        self.assertIn(refund, order.order_payment_move_ids)
        self.assertEqual(refund.payment_state, 'paid')
        self.assertEqual(invoice.payment_state, 'partial')
        self.assertEqual(invoice.amount_residual, 700.0)
        # Net invoiced and net due are both 700; the credit is not a payment
        self.assertAmounts(order, 700.0, 0.0, 700.0, 'not_paid')
        self.assertEqual(order.order_open_invoice_count, 1)
        self.assertEqual(order.order_amount_due_company, 700.0)

        action = order.action_register_order_payment()
        self.assertEqual(self._action_moves(action), invoice)
        self._pay(order)
        self.assertAmounts(order, 700.0, 700.0, 0.0, self._fully_paid_status(invoice))

    def test_23_open_manual_credit_note_blocks_payment(self):
        """A credit note created by hand stays open: the customer must not be charged 1000."""
        order = self._create_order()
        invoice = self._invoice(order)
        refund = self._manual_credit_note(order, quantity=3)
        self.assertEqual(refund.payment_state, 'not_paid')
        self.assertAmounts(order, 700.0, 0.0, 700.0, 'not_paid')
        self.assertEqual(order.order_open_invoice_count, 2)

        with self.assertRaises(UserError):
            order.action_register_order_payment()

        refund_line = refund.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
        invoice.js_assign_outstanding_line(refund_line.id)
        self.assertEqual(invoice.payment_state, 'partial')
        self.assertAmounts(order, 700.0, 0.0, 700.0, 'not_paid')
        self.assertEqual(order.order_open_invoice_count, 1)

        self._pay(order)
        self.assertAmounts(order, 700.0, 700.0, 0.0, self._fully_paid_status(invoice))

    def test_21_full_reversal_is_fully_credited(self):
        order = self._create_order()
        invoice = self._invoice(order)
        self._refund(invoice, cancel=True)
        self.assertEqual(invoice.payment_state, 'reversed')
        # "Reverse and Create Invoice" leaves a new draft invoice on the order: it must be ignored
        self.assertIn('draft', order.invoice_ids.mapped('state'))
        self.assertAmounts(order, 0.0, 0.0, 0.0, 'credited')
        self.assertEqual(order.order_paid_percent, 0.0)
        self.assertEqual(order.order_open_invoice_count, 0)

    def test_22_credit_note_after_payment_gives_customer_credit(self):
        order = self._create_order()
        invoice = self._invoice(order)
        self._pay(order)
        self._refund(invoice, quantity=2)
        # Paid 1000 on a net 800: the customer has a 200 credit
        self.assertAmounts(order, 800.0, 1000.0, -200.0, self._fully_paid_status(invoice))
        self.assertEqual(order.order_open_invoice_count, 1)
        # Only the credit note is open: registering pays the customer back
        action = order.action_register_order_payment()
        refund = order.order_payment_move_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(self._action_moves(action), refund)

    # ------------------------------------------------------------------
    # Multi-currency precision
    # ------------------------------------------------------------------

    def test_30_foreign_currency_order(self):
        """Gold has 3 decimals and a rate of 2.0 against the company currency."""
        order = self._create_order(lines=((3, 333.33),), pricelist=self.pricelist_gold)
        self.assertEqual(order.currency_id, self.gold)
        invoice = self._invoice(order)
        self.assertEqual(invoice.currency_id, self.gold)
        self.assertAmounts(order, 999.99, 0.0, 999.99, 'not_paid')
        self.assertEqual(order.order_amount_due_company, invoice.amount_residual_signed)
        self.assertAlmostEqual(order.order_amount_due_company, 500.0, places=2)

        # Partial payment in Gold: exact residual to the 3rd decimal
        self._pay(order, amount=333.333, currency=self.gold)
        self.assertAmounts(order, 999.99, 333.333, 666.657, 'partial')
        self.assertEqual(order.order_amount_due, 666.657)
        self.assertEqual(invoice.amount_residual, 666.657)
        self.assertEqual(order.order_amount_due_company, invoice.amount_residual_signed)
        self.assertAlmostEqual(order.order_paid_percent, 33.33, places=2)
        self.assertEqual(
            float_compare(order.order_paid_amount + order.order_amount_due,
                          order.order_invoiced_amount, precision_rounding=self.gold.rounding), 0)

        # Payment in company currency (2 decimals) on the Gold invoice (3 decimals)
        self._pay(order, amount=166.66, currency=self.company.currency_id)
        self.assertTrue(0 < invoice.amount_residual < 666.657)
        self.assertAmounts(order, 999.99, 999.99 - invoice.amount_residual,
                           invoice.amount_residual, 'partial')
        self.assertEqual(order.order_amount_due_company, invoice.amount_residual_signed)
        self.assertEqual(
            float_compare(order.order_paid_amount + order.order_amount_due,
                          order.order_invoiced_amount, precision_rounding=self.gold.rounding), 0)

        self._pay(order, currency=self.gold)
        self.assertAmounts(order, 999.99, 999.99, 0.0, self._fully_paid_status(invoice))
        self.assertTrue(self.company.currency_id.is_zero(order.order_amount_due_company))

    def test_31_invoice_currency_differs_from_order(self):
        """An invoice switched to another currency is converted at the invoice date."""
        order = self._create_order(lines=((1, 1000.0),))
        invoice = order._create_invoices()
        invoice.write({'currency_id': self.gold.id})
        invoice.action_post()
        # Order is in company currency: the Gold invoice is read from its company amount
        self.assertAlmostEqual(order.order_invoiced_amount, abs(invoice.amount_total_signed), places=2)
        self.assertAlmostEqual(order.order_amount_due, abs(invoice.amount_residual_signed), places=2)
        self.assertEqual(order.order_payment_status, 'not_paid')

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    def test_40_access_rights(self):
        order = self._create_order()
        self._invoice(order)
        salesman = self.env['res.users'].create({
            'name': 'Salesman Only',
            'login': 'sops_salesman',
            'company_id': self.company.id,
            'company_ids': [Command.set(self.company.ids)],
            'groups_id': [Command.set(self.env.ref('sales_team.group_sale_salesman_all_leads').ids)],
        })
        order_as_salesman = order.with_user(salesman)
        # Sales users still see the figures...
        self.assertEqual(order_as_salesman.order_payment_status, 'not_paid')
        self.assertEqual(order_as_salesman.order_amount_due, 1000.0)
        # ...but cannot register payments
        with self.assertRaises(AccessError):
            order_as_salesman.action_register_order_payment()

        billing = self.env['res.users'].create({
            'name': 'Billing User',
            'login': 'sops_billing',
            'company_id': self.company.id,
            'company_ids': [Command.set(self.company.ids)],
            'groups_id': [Command.set([
                self.env.ref('sales_team.group_sale_salesman_all_leads').id,
                self.env.ref('account.group_account_invoice').id,
            ])],
        })
        action = order.with_user(billing).action_register_order_payment()
        self.assertEqual(action['res_model'], 'account.payment.register')

    # ------------------------------------------------------------------
    # Stored fields are searchable / groupable
    # ------------------------------------------------------------------

    def test_50_search_and_group(self):
        order_paid = self._create_order(lines=((1, 100.0),))
        self._invoice(order_paid)
        self._pay(order_paid)
        order_open = self._create_order(lines=((1, 100.0),))
        self._invoice(order_open)
        orders = order_paid | order_open
        domain = [('id', 'in', orders.ids)]
        self.assertEqual(
            self.env['sale.order'].search(domain + [('order_amount_due', '>', 0)]), order_open)
        groups = self.env['sale.order'].read_group(domain, ['order_amount_due:sum'], ['order_payment_status'])
        by_status = {g['order_payment_status']: g['order_amount_due'] for g in groups}
        self.assertEqual(by_status['not_paid'], 100.0)

    # ------------------------------------------------------------------
    # Arabic translation
    # ------------------------------------------------------------------

    def test_60_arabic_translation(self):
        self.env['res.lang']._activate_lang('ar_001')
        self.env['ir.module.module']._load_module_terms(['sale_order_payments_status'], ['ar_001'], overwrite=True)
        field = self.env['ir.model.fields']._get('sale.order', 'order_payment_status')
        self.assertEqual(field.with_context(lang='ar_001').field_description, 'حالة الدفع')
        selection = dict(self.env['sale.order'].with_context(lang='ar_001')
                         ._fields['order_payment_status']._description_selection(
                             self.env['sale.order'].with_context(lang='ar_001').env))
        self.assertEqual(selection['paid'], 'مدفوع')
        self.assertEqual(selection['partial'], 'مسدد جزئيًا')
        self.assertEqual(selection['not_paid'], 'غير مسدد')
        arch = self.env['sale.order'].with_context(lang='ar_001').get_view(
            self.env.ref('sale.view_order_form').id, 'form')['arch']
        self.assertIn('تفاصيل الدفع', arch)
        self.assertIn('تسجيل الدفع', arch)
