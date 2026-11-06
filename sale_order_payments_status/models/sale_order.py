# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, float_is_zero

PAYMENT_STATUS_SELECTION = [
    ('no_invoice', 'Not Invoiced'),
    ('not_paid', 'Not Paid'),
    ('partial', 'Partially Paid'),
    ('in_payment', 'In Payment'),
    ('paid', 'Paid'),
    ('credited', 'Fully Credited'),
]

# Customer documents that carry an amount the customer owes (+) or is owed (-).
_CUSTOMER_MOVE_SIGN = {'out_invoice': 1, 'out_refund': -1}

# Invoice payment states that still leave something to collect.
_OPEN_PAYMENT_STATES = ('not_paid', 'partial')

# Every stored field of the related customer documents that can change the figures.
_MOVE_DEPENDENCIES = [
    'order_line.invoice_lines.move_id.state',
    'order_line.invoice_lines.move_id.move_type',
    'order_line.invoice_lines.move_id.currency_id',
    'order_line.invoice_lines.move_id.invoice_date',
    'order_line.invoice_lines.move_id.amount_total',
    'order_line.invoice_lines.move_id.amount_residual',
    'order_line.invoice_lines.move_id.amount_total_signed',
    'order_line.invoice_lines.move_id.amount_residual_signed',
    'order_line.invoice_lines.move_id.payment_state',
]


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    order_payment_company_currency_id = fields.Many2one(
        related='company_id.currency_id',
        string='Company Currency',
    )
    order_invoiced_amount = fields.Monetary(
        string='Total Invoiced',
        compute='_compute_order_payment_amounts',
        store=True,
        currency_field='currency_id',
        help='Posted customer invoices of this order minus its posted credit notes, '
             'in the order currency.',
    )
    order_paid_amount = fields.Monetary(
        string='Total Paid',
        compute='_compute_order_payment_amounts',
        store=True,
        currency_field='currency_id',
        help='Part of the invoiced amount that is settled by payments, in the order currency.',
    )
    order_amount_due = fields.Monetary(
        string='Amount Due',
        compute='_compute_order_payment_amounts',
        store=True,
        currency_field='currency_id',
        help='Amount the customer still has to pay on the posted invoices of this order, '
             'net of open credit notes, in the order currency. '
             'A negative amount is a credit in favour of the customer.',
    )
    order_amount_due_company = fields.Monetary(
        string='Amount Due (Company Currency)',
        compute='_compute_order_payment_amounts',
        store=True,
        currency_field='order_payment_company_currency_id',
        help='Amount Due expressed in the company currency, as booked in the receivable '
             'account. Use it to total amounts across orders in different currencies.',
    )
    order_paid_percent = fields.Float(
        string='Paid (%)',
        compute='_compute_order_payment_amounts',
        store=True,
        group_operator='avg',
        digits=(16, 2),
    )
    order_payment_status = fields.Selection(
        selection=PAYMENT_STATUS_SELECTION,
        string='Payment Status',
        compute='_compute_order_payment_amounts',
        store=True,
        index=True,
        help='* Not Invoiced: no posted customer invoice yet.\n'
             '* Not Paid: invoiced, nothing paid.\n'
             '* Partially Paid: part of the invoiced amount is paid.\n'
             '* In Payment: fully paid, payment not yet matched with the bank statement.\n'
             '* Paid: fully paid.\n'
             '* Fully Credited: every invoice is cancelled out by credit notes.',
    )
    order_open_invoice_count = fields.Integer(
        string='Open Invoices',
        compute='_compute_order_payment_amounts',
        store=True,
        help='Number of posted customer invoices and credit notes that are not fully paid.',
    )
    order_payment_move_ids = fields.Many2many(
        comodel_name='account.move',
        string='Invoices & Credit Notes',
        compute='_compute_order_payment_move_ids',
    )
    order_payment_ids = fields.Many2many(
        comodel_name='account.payment',
        string='Payments',
        compute='_compute_order_payment_ids',
        groups='account.group_account_invoice',
    )
    order_payment_count = fields.Integer(
        string='Payment Count',
        compute='_compute_order_payment_ids',
        groups='account.group_account_invoice',
    )

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _get_order_payment_moves(self):
        """Posted customer invoices and credit notes linked to the orders."""
        return self.order_line.invoice_lines.move_id.filtered(
            lambda m: m.state == 'posted' and m.move_type in _CUSTOMER_MOVE_SIGN
        )

    def _get_order_move_amounts(self, move):
        """Return (total, residual) of ``move`` expressed in the order currency, unsigned.

        Invoices created from an order normally share its currency, in which case the
        document amounts are used as they are, with no rounding drift. Otherwise the
        company-currency amounts are converted at the invoice date.
        """
        self.ensure_one()
        currency = self.currency_id or self.company_id.currency_id
        if move.currency_id == currency:
            return abs(move.amount_total), abs(move.amount_residual)
        date = move.invoice_date or move.date or fields.Date.context_today(self)
        company_currency = move.company_currency_id
        total = company_currency._convert(abs(move.amount_total_signed), currency, move.company_id, date)
        residual = company_currency._convert(abs(move.amount_residual_signed), currency, move.company_id, date)
        return total, residual

    # -------------------------------------------------------------------------
    # Computes
    # -------------------------------------------------------------------------

    @api.depends('currency_id', 'company_id', *_MOVE_DEPENDENCIES)
    def _compute_order_payment_amounts(self):
        for order in self:
            currency = order.currency_id or order.company_id.currency_id
            company_currency = order.company_id.currency_id
            moves = order._get_order_payment_moves()

            invoiced = due = due_company = 0.0
            for move in moves:
                sign = _CUSTOMER_MOVE_SIGN[move.move_type]
                total, residual = order._get_order_move_amounts(move)
                invoiced += sign * total
                due += sign * residual
                # amount_residual_signed is already negative for credit notes
                due_company += move.amount_residual_signed

            invoiced = currency.round(invoiced)
            due = currency.round(due)
            paid = currency.round(invoiced - due)

            order.order_invoiced_amount = invoiced
            order.order_amount_due = due
            order.order_paid_amount = paid
            order.order_amount_due_company = company_currency.round(due_company)
            order.order_open_invoice_count = len(
                moves.filtered(lambda m: m.payment_state in _OPEN_PAYMENT_STATES)
            )
            if invoiced > 0:
                order.order_paid_percent = max(0.0, min(100.0, paid / invoiced * 100.0))
            else:
                # Nothing invoiced net (fully credited) or only credit notes: nothing was paid
                order.order_paid_percent = 0.0
            order.order_payment_status = order._get_order_payment_status(moves, invoiced, due, currency)

    def _get_order_payment_status(self, moves, invoiced, due, currency):
        """Decide the payment status from the aggregated figures (order currency)."""
        rounding = currency.rounding
        if not moves:
            return 'no_invoice'
        if float_is_zero(invoiced, precision_rounding=rounding) and float_is_zero(due, precision_rounding=rounding):
            return 'credited'
        if float_compare(due, 0.0, precision_rounding=rounding) <= 0:
            invoices = moves.filtered(lambda m: m.move_type == 'out_invoice')
            if any(m.payment_state == 'in_payment' for m in invoices):
                return 'in_payment'
            return 'paid'
        if float_compare(due, invoiced, precision_rounding=rounding) >= 0:
            return 'not_paid'
        return 'partial'

    @api.depends(*_MOVE_DEPENDENCIES)
    def _compute_order_payment_move_ids(self):
        for order in self:
            order.order_payment_move_ids = order._get_order_payment_moves()

    @api.depends(*_MOVE_DEPENDENCIES)
    def _compute_order_payment_ids(self):
        for order in self:
            payments = order._get_order_payment_moves()._get_reconciled_payments()
            order.order_payment_ids = payments
            order.order_payment_count = len(payments)

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def _check_order_payment_access(self):
        if not self.env.user.has_group('account.group_account_invoice'):
            raise AccessError(_(
                'Only accounting users (Billing or above) can register payments '
                'from a sales order.'
            ))
        self.env['account.payment'].check_access_rights('create')

    def action_register_order_payment(self):
        """Open the standard Register Payment wizard on the open invoices of the orders."""
        self._check_order_payment_access()
        moves = self._get_order_payment_moves()
        open_moves = moves.filtered(lambda m: m.payment_state in _OPEN_PAYMENT_STATES)
        if not open_moves:
            raise UserError(_(
                'There is nothing left to pay: the selected orders have no posted invoice '
                'with an amount due.'
            ))
        open_moves.check_access_rights('write')
        open_moves.check_access_rule('write')

        open_invoices = open_moves.filtered(lambda m: m.move_type == 'out_invoice')
        open_refunds = open_moves - open_invoices
        if open_invoices and open_refunds:
            raise UserError(_(
                'The credit note(s) %(refunds)s are not yet reconciled with an invoice.\n'
                'Open the invoice %(invoices)s and add the outstanding credit before '
                'registering a payment, so the customer is not charged more than the '
                'amount due.',
                refunds=', '.join(open_refunds.mapped('name')),
                invoices=', '.join(open_invoices.mapped('name')),
            ))
        return open_moves.action_register_payment()

    def action_view_order_payments(self):
        self.ensure_one()
        payments = self.order_payment_ids
        action = self.env['ir.actions.act_window']._for_xml_id('account.action_account_payments')
        action['context'] = {'create': False, 'default_payment_type': 'inbound',
                             'default_partner_type': 'customer'}
        if len(payments) == 1:
            action.update({
                'views': [(self.env.ref('account.view_account_payment_form').id, 'form')],
                'res_id': payments.id,
                'view_mode': 'form',
            })
        else:
            action['domain'] = [('id', 'in', payments.ids)]
        return action
