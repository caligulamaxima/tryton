# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import datetime
from decimal import Decimal

from sql import Null

from trytond.model import (
    DeactivableMixin, MatchMixin, ModelSQL, ModelView, Workflow, fields,
    sequence_ordered)
from trytond.modules.currency.fields import Monetary
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval, If
from trytond.transaction import Transaction


class Sale(metaclass=PoolMeta):
    __name__ = 'sale.sale'

    @classmethod
    @ModelView.button
    @Workflow.transition('quotation')
    def quote(cls, sales):
        pool = Pool()
        Line = pool.get('sale.line')

        super(Sale, cls).quote(sales)

        # State must be draft to add or delete lines
        # because extra must be set after to have correct amount
        cls.write(sales, {'state': 'draft'})
        removed = []
        for sale in sales:
            removed.extend(sale.set_extra())
        Line.delete(removed)
        cls.save(sales)

    def set_extra(self):
        'Set extra lines and fill lines_to_delete'
        pool = Pool()
        Extra = pool.get('sale.extra')
        removed = []
        extra_lines = Extra.get_lines(self)
        extra2lines = {line.extra: line for line in extra_lines}
        lines = list(self.lines)
        for line in list(lines):
            if line.type != 'line' or not line.extra:
                continue
            if line.extra in extra2lines:
                del extra2lines[line.extra]
                continue
            else:
                lines.remove(line)
                removed.append(line)
        if extra2lines:
            lines.extend(extra2lines.values())
        self.lines = lines
        return removed


class Line(metaclass=PoolMeta):
    __name__ = 'sale.line'

    extra = fields.Many2One('sale.extra.line', 'Extra', ondelete='RESTRICT')


class Extra(DeactivableMixin, ModelSQL, ModelView, MatchMixin):
    'Sale Extra'
    __name__ = 'sale.extra'

    name = fields.Char('Name', translate=True, required=True)
    company = fields.Many2One(
        'company.company', "Company", required=True,
        states={
            'readonly': Eval('id', 0) > 0,
            })
    start_date = fields.Date('Start Date',
        domain=['OR',
            ('start_date', '<=', If(~Eval('end_date', None),
                    datetime.date.max,
                    Eval('end_date', datetime.date.max))),
            ('start_date', '=', None),
            ])
    end_date = fields.Date('End Date',
        domain=['OR',
            ('end_date', '>=', If(~Eval('start_date', None),
                    datetime.date.min,
                    Eval('start_date', datetime.date.min))),
            ('end_date', '=', None),
            ])
    price_list = fields.Many2One('product.price_list', 'Price List',
        ondelete='CASCADE',
        domain=[
            ('company', '=', Eval('company', -1)),
            ])
    sale_amount = Monetary(
        "Sale Amount", currency='currency', digits='currency')
    currency = fields.Function(fields.Many2One(
            'currency.currency', "Currency"),
        'on_change_with_currency')
    lines = fields.One2Many('sale.extra.line', 'extra', 'Lines')

    @classmethod
    def __register__(cls, module_name):
        pool = Pool()
        PriceList = pool.get('product.price_list')
        transaction = Transaction()
        cursor = transaction.connection.cursor()
        update = transaction.connection.cursor()
        sql_table = cls.__table__()
        price_list = PriceList.__table__()

        super().__register__(module_name)

        table = cls.__table_handler__(module_name)
        # Migration from 3.6: price_list not required and new company
        table.not_null_action('price_list', 'remove')
        query = sql_table.join(price_list,
            condition=sql_table.price_list == price_list.id
            ).select(sql_table.id, price_list.company,
                where=sql_table.company == Null)
        cursor.execute(*query)
        for extra_id, company_id in cursor:
            query = sql_table.update([sql_table.company], [company_id],
                where=sql_table.id == extra_id)
            update.execute(*query)

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @fields.depends('company')
    def on_change_with_currency(self, name=None):
        if self.company:
            return self.company.currency.id

    @classmethod
    def _extras_domain(cls, sale):
        return [
            ['OR',
                ('start_date', '<=', sale.sale_date),
                ('start_date', '=', None),
                ],
            ['OR',
                ('end_date', '=', None),
                ('end_date', '>=', sale.sale_date),
                ],
            ['OR',
                ('price_list', '=', None),
                ('price_list', '=',
                    sale.price_list.id if sale.price_list else None),
                ],
            ('company', '=', sale.company.id),
            ]

    @classmethod
    def get_lines(cls, sale, pattern=None, line_pattern=None):
        'Yield extra sale lines'
        pool = Pool()
        Currency = pool.get('currency.currency')
        extras = cls.search(cls._extras_domain(sale))
        pattern = pattern.copy() if pattern is not None else {}
        line_pattern = line_pattern.copy() if line_pattern is not None else {}
        sale_amount = Currency.compute(
            sale.currency, sale.untaxed_amount, sale.company.currency)
        pattern.setdefault('sale_amount', sale_amount)
        line_pattern.setdefault('sale_amount', sale_amount)

        for extra in extras:
            if extra.match(pattern):
                for line in extra.lines:
                    if line.match(line_pattern):
                        yield line.get_line(sale)
                        break

    def match(self, pattern):
        pattern = pattern.copy()
        sale_amount = pattern.pop('sale_amount')

        match = super().match(pattern)

        if self.sale_amount is not None:
            if sale_amount < self.sale_amount:
                return False
        return match


class ExtraLine(sequence_ordered(), ModelSQL, ModelView, MatchMixin):
    'Sale Extra Line'
    __name__ = 'sale.extra.line'

    extra = fields.Many2One('sale.extra', 'Extra', required=True,
        ondelete='CASCADE')
    sale_amount = Monetary(
        "Sale Amount", currency='currency', digits='currency')
    product = fields.Many2One('product.product', 'Product', required=True,
        domain=[('salable', '=', True)])
    product_uom_category = fields.Function(
        fields.Many2One('product.uom.category', 'Product UoM Category'),
        'on_change_with_product_uom_category')
    quantity = fields.Float("Quantity", digits='unit', required=True)
    unit = fields.Many2One('product.uom', 'Unit', required=True,
        domain=[
            ('category', '=', Eval('product_uom_category', -1)),
            ])
    free = fields.Boolean('Free')
    currency = fields.Function(fields.Many2One(
            'currency.currency', "Currency"),
        'on_change_with_currency')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__access__.add('extra')
        cls._order.insert(1, ('extra', 'ASC'))

    @fields.depends('product')
    def on_change_with_product_uom_category(self, name=None):
        if self.product:
            return self.product.default_uom_category.id

    @fields.depends('product')
    def on_change_product(self):
        if self.product:
            self.unit = self.product.sale_uom

    @staticmethod
    def default_free():
        return False

    @fields.depends('extra', '_parent_extra.currency')
    def on_change_with_currency(self, name=None):
        if self.extra and self.extra.currency:
            return self.extra.currency.id

    def match(self, pattern):
        pattern = pattern.copy()
        sale_amount = pattern.pop('sale_amount')

        match = super().match(pattern)

        if self.sale_amount is not None:
            if sale_amount < self.sale_amount:
                return False
        return match

    def get_line(self, sale):
        pool = Pool()
        Line = pool.get('sale.line')

        sequence = None
        if sale.lines:
            last_line = sale.lines[-1]
            if last_line.sequence is not None:
                sequence = last_line.sequence + 1

        line = Line(
            sale=sale,
            sequence=sequence,
            type='line',
            product=self.product,
            quantity=self.quantity,
            unit=self.unit,
            extra=self,
            )
        line.on_change_product()
        if self.free:
            line.unit_price = line.amount = Decimal(0)
        return line
