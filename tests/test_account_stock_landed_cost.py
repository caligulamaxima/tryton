# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import doctest
import unittest

import trytond.tests.test_tryton
from trytond.tests.test_tryton import (
    ModuleTestCase, doctest_checker, doctest_teardown)


class AccountStockLandedCostTestCase(ModuleTestCase):
    'Test Account Stock Landed Cost module'
    module = 'account_stock_landed_cost'


def suite():
    suite = trytond.tests.test_tryton.suite()
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(
        AccountStockLandedCostTestCase))
    suite.addTests(doctest.DocFileSuite(
            'scenario_account_stock_landed_cost.rst',
            tearDown=doctest_teardown, encoding='utf-8',
            checker=doctest_checker,
            optionflags=doctest.REPORT_ONLY_FIRST_FAILURE))
    return suite
