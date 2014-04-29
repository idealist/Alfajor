# Copyright Action Without Borders, Inc., the Alfajor authors and contributors.
# All rights reserved.  See AUTHORS.
#
# This file is part of 'alfajor' and is distributed under the BSD license.
# See LICENSE for more details.
import time

from nose.tools import raises

from . import browser, browser_test
from alfajor.browsers import _waitexpr


def test_boolean_expression():
    call_log = []

    class CanaryClause(object):

        def __init__(self, condition):
            self.condition = condition

        def __call__(self, browser):
            call_log.append(self.condition)
            return self.condition

    # simple AND
    cl = _waitexpr.AndExpression(CanaryClause(True))
    cl.append(CanaryClause(False))
    cl.append(CanaryClause(False))
    assert not cl('browser')
    assert call_log == [True, False]

    call_log[:] = []

    # simple OR
    cl = _waitexpr.OrExpression(CanaryClause(True))
    cl.append(CanaryClause(False))
    assert cl('browser')
    assert call_log == [True]

    call_log[:] = []

    # complex
    cl = _waitexpr.AndExpression(
        CanaryClause(True),
        CanaryClause(True),
        _waitexpr.OrExpression(
            CanaryClause(False),
            CanaryClause(True),
        )
    )
    assert cl('browser')
    assert call_log == [True, True, False, True]


class MockBrowser(object):
    current_timeout = 0

    def __init__(self):
        self.call_log = []

    def wait_for(self, condition, **kw):
        self.call_log.append(condition)
        return kw['retval']


WDWExp = _waitexpr.WebDriverWaitExpression
WDWCl = _waitexpr.WebDriverWaitClause


def test_callable_wait_clause():
    expr = lambda b, retval: retval
    cl = WDWCl(expr, retval=10)
    browser = MockBrowser()
    assert cl(browser) == 10


def test_webdriver_wait_expression_simple_and():
    browser = MockBrowser()
    we = (WDWExp()
          .element_present('#id1', retval=True)
          .element_present('#id2', retval=True)
          )
    assert we(browser)
    assert browser.call_log == ['element:#id1', 'element:#id2']


def test_webdriver_wait_expression_simple_and():
    browser = MockBrowser()
    we = (WDWExp()
          .element_present('#id1', retval=True)
          .element_present('#id2', retval=True)
          )
    assert we(browser)
    assert browser.call_log == ['element:#id1', 'element:#id2']


def test_webdriver_wait_expression_simple_and_2():
    browser = MockBrowser()
    we = (WDWExp()
          .element_present('#id1', retval=False)
          .element_present('#id2', retval=True)
          )
    assert not we(browser)
    assert browser.call_log == ['element:#id1']


def test_webdriver_wait_expression_simple_or():
    browser = MockBrowser()
    we = (WDWExp()
          .element_present('#id1', retval=False)
          .or_()
          .element_present('#id2', retval=True)
          )
    assert we(browser)
    assert browser.call_log == ['element:#id1', 'element:#id2']


def test_webdriver_wait_expression_compound():
    browser = MockBrowser()
    we = (WDWExp()
          .element_present('#id1', retval=False)
          .element_present('#id2', retval=False)
          .or_()
          .element_not_present('#id3', retval=False)
          )
    assert not we(browser)
    assert browser.call_log == ['element:#id1', '!element:#id3']


def test_Webdriver_wait_expression_compound_2():
    browser = MockBrowser()
    we = (WDWExp()
          .element_present('#id1', retval=False)
          .or_()
          .element_present('#id2', retval=True)
          .element_not_present('#id3', retval=True)
          )
    assert we(browser)
    assert browser.call_log == ['element:#id1', 'element:#id2', '!element:#id3']


def test_Webdriver_wait_expression_compound_3():
    browser = MockBrowser()
    we = (WDWExp()
          .element_present('#id1', retval=True)
          .or_()
          .element_present('#id2', retval=True)
          .element_not_present('#id3', retval=True)
          )
    assert we(browser)
    assert browser.call_log == ['element:#id1', '!element:#id3']


@browser_test()
def test_evaluate_element():
    if 'javascript' not in browser.capabilities:
        return
    browser.open('/form/submit')
    browser.document['input[name=x]'][0].value = 'abc'

    exp = browser.wait_expression().evaluate_element('[name=x]', 'value', 'abc')
    assert browser.wait_for(exp, timeout=0)

    exp = browser.wait_expression().evaluate_element('[name=x]', 'value', 'xyz')
    assert not browser.wait_for(exp, timeout=0)
