# -*- coding: utf-8 -*-
from collections import defaultdict

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

# Invoice payment states that may still leave something to collect. Since Odoo 18 an
# 'in_payment' invoice can still be partly open (payment without journal entry).
_OPEN_PAYMENT_STATES = ('not_paid', 'partial', 'in_payment')

# States of a payment registered without journal entry (Odoo 18) that settle an invoice.
_PENDING_PAYMENT_STATES = ('in_process', 'paid')

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
    # Odoo 18: payments without journal entry are linked through matched_payment_ids
    'order_line.invoice_lines.move_id.matched_payment_ids',
    'order_line.invoice_lines.move_id.matched_payment_ids.state',
    'order_line.invoice_lines.move_id.matched_payment_ids.move_id',
    'order_line.invoice_lines.move_id.matched_payment_ids.amount',
    'order_line.invoice_lines.move_id.matched_payment_ids.currency_id',
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
        aggregator='avg',
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

    @api.model
    def _get_order_pending_payment_amounts(self, moves):
        """Part of each move settled by payments that have no journal entry yet.

        Odoo 18 (typically Enterprise, when the bank journal has no outstanding account)
        registers a payment without journal entry: the invoice becomes 'In Payment' but its
        residual is unchanged until the bank statement is reconciled. Such a payment is
        spread over the invoices it was registered for, oldest due date first, capped by
        their residuals.

        :return: {move id: amount covered, in the move currency, unsigned}
        """
        payments = moves.matched_payment_ids.filtered(
            lambda p: not p.move_id and p.state in _PENDING_PAYMENT_STATES
        )
        covered = defaultdict(float)
        if not payments:
            return covered
        targets = payments.invoice_ids.filtered(lambda m: m.state == 'posted')
        remaining = {m.id: abs(m.amount_residual) for m in targets}
        for payment in payments.sorted(lambda p: (p.date, p.id)):
            left = payment.amount
            invoices = payment.invoice_ids.filtered(lambda m: m.id in remaining).sorted(
                lambda m: (m.invoice_date_due or m.date, m.id))
            for move in invoices:
                if payment.currency_id.is_zero(left):
                    break
                available = remaining[move.id]
                if move.currency_id.is_zero(available):
                    continue
                left_in_move_currency = payment.currency_id._convert(
                    left, move.currency_id, move.company_id, payment.date)
                taken = min(available, left_in_move_currency)
                covered[move.id] += taken
                remaining[move.id] -= taken
                left -= move.currency_id._convert(taken, payment.currency_id, move.company_id, payment.date)
        return covered

    def _get_order_move_residuals(self, moves):
        """{move id: amount still to settle, in the move currency, unsigned}."""
        covered = self._get_order_pending_payment_amounts(moves)
        return {
            move.id: max(0.0, move.currency_id.round(abs(move.amount_residual) - covered.get(move.id, 0.0)))
            for move in moves
        }

    def _get_order_move_amounts(self, move, residual):
        """Return (total, residual) of ``move`` expressed in the order currency, unsigned.

        ``residual`` is the amount still to settle in the move currency. Invoices created
        from an order normally share its currency, in which case the document amounts are
        used as they are, with no rounding drift. Otherwise the company-currency amounts are
        converted at the invoice date.
        """
        self.ensure_one()
        currency = self.currency_id or self.company_id.currency_id
        if move.currency_id == currency:
            return abs(move.amount_total), residual
        date = move.invoice_date or move.date or fields.Date.context_today(self)
        company_currency = move.company_currency_id
        total = company_currency._convert(abs(move.amount_total_signed), currency, move.company_id, date)
        residual_company = self._get_order_residual_company(move, residual)
        return total, company_currency._convert(residual_company, currency, move.company_id, date)

    @api.model
    def _get_order_residual_company(self, move, residual):
        """Unsigned residual of ``move`` in company currency, net of pending payments."""
        move_residual = abs(move.amount_residual)
        if move.currency_id.is_zero(move_residual):
            return 0.0
        if move.currency_id.compare_amounts(residual, move_residual) == 0:
            return abs(move.amount_residual_signed)
        return abs(move.amount_residual_signed) * residual / move_residual

    # -------------------------------------------------------------------------
    # Computes
    # -------------------------------------------------------------------------

    @api.depends('currency_id', 'company_id', *_MOVE_DEPENDENCIES)
    def _compute_order_payment_amounts(self):
        for order in self:
            currency = order.currency_id or order.company_id.currency_id
            company_currency = order.company_id.currency_id
            moves = order._get_order_payment_moves()
            residuals = order._get_order_move_residuals(moves)

            invoiced = due = due_company = 0.0
            for move in moves:
                sign = _CUSTOMER_MOVE_SIGN[move.move_type]
                total, residual = order._get_order_move_amounts(move, residuals[move.id])
                invoiced += sign * total
                due += sign * residual
                due_company += sign * order._get_order_residual_company(move, residuals[move.id])

            invoiced = currency.round(invoiced)
            due = currency.round(due)
            paid = currency.round(invoiced - due)

            order.order_invoiced_amount = invoiced
            order.order_amount_due = due
            order.order_paid_amount = paid
            order.order_amount_due_company = company_currency.round(due_company)
            order.order_open_invoice_count = len(order._get_order_open_moves(moves, residuals))
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
            if any(m.payment_state == 'in_payment' for m in invoices) or any(
                    p.state == 'in_process' for p in invoices.matched_payment_ids):
                return 'in_payment'
            return 'paid'
        if float_compare(due, invoiced, precision_rounding=rounding) >= 0:
            return 'not_paid'
        return 'partial'

    @api.depends(*_MOVE_DEPENDENCIES)
    def _compute_order_payment_move_ids(self):
        for order in self:
            order.order_payment_move_ids = order._get_order_payment_moves()

    @api.model
    def _get_order_open_moves(self, moves, residuals=None):
        """Posted invoices / credit notes that still have something to settle."""
        if residuals is None:
            residuals = self._get_order_move_residuals(moves)
        return moves.filtered(
            lambda m: m.payment_state in _OPEN_PAYMENT_STATES and not m.currency_id.is_zero(residuals[m.id])
        )

    @api.depends(*_MOVE_DEPENDENCIES)
    def _compute_order_payment_ids(self):
        for order in self:
            # Odoo 18: reconciled_payment_ids also holds payments without journal entry
            payments = order._get_order_payment_moves().reconciled_payment_ids.filtered(
                lambda p: p.state not in ('draft', 'canceled', 'rejected'))
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
        self.env['account.payment'].check_access('create')

    def action_register_order_payment(self):
        """Open the standard Register Payment wizard on the open invoices of the orders."""
        self._check_order_payment_access()
        moves = self._get_order_payment_moves()
        open_moves = self._get_order_open_moves(moves)
        if not open_moves:
            raise UserError(_(
                'There is nothing left to pay: the selected orders have no posted invoice '
                'with an amount due.'
            ))
        open_moves.check_access('write')

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
