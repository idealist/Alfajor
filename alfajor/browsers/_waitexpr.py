# Copyright Action Without Borders, Inc., the Alfajor authors and contributors.
# All rights reserved.  See AUTHORS.
#
# This file is part of 'Alfajor' and is distributed under the BSD license.
# See LICENSE for more details.

"""Compound wait_for expression support."""

import operator
import re
import time

__all__ = 'WaitExpression', 'SeleniumWaitExpression', 'WebDriverWaitExpression'

OR = object()


class WaitExpression(object):
    """Generic wait_for expression generator and compiler.

    Expression objects chain in a jQuery/SQLAlchemy-esque fashion::

      expr = (browser.wait_expression().
              element_present('#druid').
              ajax_complete())

    Or can be configured at instantiation:

      expr = browser.wait_expression(['element_present', '#druid'],
                                     ['ajax_complete'])

    Expression components are and-ed (&&) together.  To or (||), separate
    components with :meth:`or_`::

      element_present('#druid').or_().ajax_complete()

    The expression object can be supplied to any operation which accepts
    a ``wait_for`` argument.

    """

    def __init__(self, *expressions, **kw):
        for spec in expressions:
            directive = spec[0]
            args = spec[1:]
            getattr(self, directive)(*args)

    def __call__(self, browser, timeout=None):
        return browser.wait_for(self, timeout=timeout)

    def or_(self):
        """Combine the next expression with an OR instead of default AND."""
        return self

    def element_present(self, finder):
        """True if *finder* is present on the page.

        :param finder: a CSS selector or document element instance

        """
        return self

    def element_not_present(self, expr):
        """True if *finder* is not present on the page.

        :param finder: a CSS selector or document element instance

        """
        return self

    def element_visible(self, finder):
        """True if *finder* is visible.

        :param finder: a CSS selector or document element instance

        """
        return self

    def element_not_visible(self, expr):
        """True if *finder* is not visible.

        :param finder: a CSS selector or document element instance

        """
        return self

    def evaluate_element(self, finder, attr, reference, predicate=None):
        """True if *finder* is present on the page and evaluated by *expr*.

        :param finder: a CSS selector or document element instance

        :param attr: The attribute on the found element to evluate

        :param reference: The expected value of the element

        :param predicate: The predicate used to compare element.attr and
                          reference.  Default:  operator.eq

        """
        return self

    def ajax_pending(self):
        """True if jQuery ajax requests are pending."""
        return self

    def ajax_complete(self):
        """True if no jQuery ajax requests are pending."""
        return self

    def __unicode__(self):
        """The rendered value of the expression."""
        return u''


class SeleniumWaitExpression(WaitExpression):
    """Compound wait_for expression compiler for Selenium browsers."""

    ajax_pending_expr = ('var pending = window.jQuery && '
                         'window.jQuery.active != 0;')
    ajax_complete_expr = ('var complete = window.jQuery && '
                          'window.jQuery.active == 0;')

    def __init__(self, *expressions, **kw):
        self._expressions = []
        WaitExpression.__init__(self, *expressions, **kw)

    def or_(self):
        self._expressions.append(OR)
        return self

    def predicate_log(self, label, var_name='value'):
        """Return JS for logging a result test in the Selenium console."""
        js = "LOG.info('wait_for %s ==' + %s);" % (js_quote(label), var_name)
        return js

    def evaluation_log(self, label, var_name='value', *args):
        """Return JS for logging an expression eval in the Selenium console."""
        inner = ', '.join(map(js_quote, args))
        js = "LOG.info('wait_for %s(%s)=' + %s);" % (js_quote(label), inner,
                                                     var_name)
        return js

    def element_present(self, finder):
        js = self._is_element_present('element_present', finder, 'true')
        self._expressions.append(js)
        return self

    def element_not_present(self, finder):
        js = self._is_element_present('element_not_present', finder, 'false')
        self._expressions.append(js)
        return self

    def element_visible(self, finder):
        js = self._is_element_visible('element_visible', finder, 'true')
        self._expressions.append(js)
        return self

    def element_not_visible(self, finder):
        js = self._is_element_visible('element_not_visible', finder, 'false')
        self._expressions.append(js)
        return self

    def evaluate_element(self, finder, expr, ref=None, predicate=None):
        locator = self.to_locator(finder)
        if ref is not None:
            # SW: Retrain backward compatibility for old behaviour
            expr = "element.%s == '%s'" % (expr, ref)
        if predicate:
            raise NotImplemented("Passing a predicate to evaluate_element "
                                 "is not supported by selenium backend.")
        log = self.evaluation_log('evaluate_element', 'result', locator, expr)
        js = """\
(function () {
  var element;
  try {
    element = selenium.browserbot.findElement('%s');
  } catch (e) {
    element = null;
  };
  var result = false;
  if (element !== null)
    result = %s;
  %s
  return result;
})()""" % (js_quote(locator), expr, log)
        self._expressions.append(js)
        return self

    def ajax_pending(self):
        js = """\
(function() {
  %s
  %s
  return pending;
})()""" % (self.ajax_pending_expr,
           self.predicate_log('ajax_pending', 'pending'))
        self._expressions.append(js)
        return self

    def ajax_complete(self):
        js = """\
(function() {
  %s
  %s
  return complete;
})()""" % (self.ajax_complete_expr,
           self.predicate_log('ajax_complete', 'complete'))
        self._expressions.append(js)
        return self

    def _is_element_present(self, label, finder, result):
        locator = self.to_locator(finder)
        log = self.evaluation_log(label, 'found', locator)
        return u"""\
(function () {
  var found = true;
  try {
    selenium.browserbot.findElement('%s');
  } catch (e) {
    found = false;
  };
  %s
  return found == %s;
})()""" % (js_quote(locator), log, result)

    def _is_element_visible(self, label, finder, result):
        locator = self.to_locator(finder)
        log = self.evaluation_log(label, 'visible', locator)
        return u"""
(function() {
    var visible;
    try {
        visible = selenium.isVisible("%s");
    } catch(e) {
        visible = false;
    }
    return visible == %s;
})()
""" % (js_quote(locator), result)

    def __unicode__(self):
        last = None
        components = []
        for expr in self._expressions:
            if expr is OR:
                components.append(u'||')
            else:
                if last not in (None, OR):
                    components.append(u'&&')
                components.append(expr)
            last = expr
        predicate = u' '.join(components).replace('\n', ' ')
        return predicate

    def to_locator(self, expr):
        """Convert a css selector or document element into selenium locator."""
        if isinstance(expr, basestring):
            return 'css=' + expr
        elif hasattr(expr, '_locator'):
            return expr._locator
        else:
            raise RuntimeError("Unknown page element %r" % expr)


class JQuerySeleniumWaitExpression(SeleniumWaitExpression):
    pass


class PrototypeSeleniumWaitExpression(SeleniumWaitExpression):
    ajax_pending_expr = ('var value = window.Ajax && '
                         'window.Ajax.activeRequestCount != 0;')
    ajax_complete_expr = ('var value = window.Ajax && '
                          'window.Ajax.activeRequestCount == 0;')


class DojoSeleniumWaitExpression(SeleniumWaitExpression):
    ajax_pending_expr = ('var value = window.dojo && '
        'window.dojo.io.XMLHTTPTransport.inFlight.length != 0;')
    ajax_complete_expr = ('var value = window.dojo && '
        'window.dojo.io.XMLHTTPTransport.inFlight.length == 0;')


class _BooleanExpression(list):

    def __init__(self, *clauses):
        self.extend(clauses)


class AndExpression(_BooleanExpression):

    def __call__(self, browser):
        return all((e(browser) for e in self))


class OrExpression(_BooleanExpression):

    def __call__(self, browser):
        return any((e(browser) for e in self))


class WebDriverWaitClause(object):
    # kwargs facilitate testing

    def __init__(self, condition, **kw):
        if isinstance(condition, self.__class__):
            return condition
        self.condition = condition
        kw.pop('timeout', None)
        self.kw = kw

    def __call__(self, browser):
        if callable(self.condition):
            return self.condition(browser, **self.kw)
        return browser.wait_for(self.condition, timeout=0, **self.kw)


class WebDriverWaitExpression(WaitExpression):
    _expression = None
    wait_clause_factory = WebDriverWaitClause

    ajax_pending_expr = ('window.jQuery && window.jQuery.active != 0;')
    ajax_complete_expr = ('window.jQuery && window.jQuery.active == 0;')
    page_loading_expr = ('window.__alfajor_webdriver_page__ === true')
    page_ready_expr = ('window.__alfajor_webdriver_page__ === undefined')

    def __init__(self, *expressions, **kw):
        clauses = [self.wait_clause_factory(e) for e in expressions]
        self._expression = AndExpression(*clauses)

    def __call__(self, browser, timeout=None):
        # Because we want to respect the normal timeout for the whole
        # compound intruction expression we manage the timeout/retry manually
        # and pass a zero timeout to the webdriver browser for each of the
        # component checks.
        timeout = browser.current_timeout if timeout is None else timeout
        start_time = time.time()
        while True:
            rv = None
            try:
                rv = self._expression(browser)
            except AssertionError:
                pass
            if rv:
                return True
            if (time.time() - start_time) * 1000 >= timeout:
                break
        return False

    def or_(self):
        """Combine the next expression with an OR instead of default AND."""
        self._expression = OrExpression(self._expression)
        return self

    def _append(self, clause):
        self._expression.append(clause)
        if isinstance(self._expression, OrExpression):
            # Close out the or clause after we have a second argument to it.
            self._expression = AndExpression(self._expression)

    def element_present(self, expr, **kw):
        locator = self._to_locator(expr)
        self._append(self.wait_clause_factory('element:' + locator, **kw))
        return self

    def element_not_present(self, expr, **kw):
        locator = self._to_locator(expr)
        self._append(self.wait_clause_factory('!element:' + locator, **kw))
        return self

    def element_visible(self, expr, **kw):
        locator = self._to_locator(expr)
        self._append(self.wait_clause_factory('visible:' + locator, **kw))
        return self

    def element_not_visible(self, expr, **kw):
        locator = self._to_locator(expr)
        self._append(self.wait_clause_factory('!visible:' + locator, **kw))
        return self

    def page_ready(self, **kw):
        js = self.page_ready_expr
        self._append(self.wait_clause_factory('js:' + js, **kw))
        return self

    def page_loading(self, **kw):
        js = self.page_loading_expr
        self._append(self.wait_clause_factory('js:' + js, **kw))
        return self

    def ajax_pending(self, **kw):
        js = self.ajax_pending_expr
        self._append(self.wait_clause_factory('js:' + js, **kw))
        return self

    def ajax_complete(self, **kw):
        js = self.ajax_complete_expr
        self._append(self.wait_clause_factory('js:' + js, **kw))
        return self

    def evaluate_element(self, finder, attr, reference, predicate=None, **kw):
        # This guy never waits, but the value checks are useful in branchy
        # boolean expression logic.
        def evaluate_it(browser, **kw):
            element = self.to_element(finder, browser)
            pred = predicate or operator.eq
            value = element.attrib[attr]
            return pred(value, reference)

        self._append(self.wait_clause_factory(evaluate_it, **kw))
        return self

    def predicate_log(self, label):
        return ''

    def evaluation_log(self, *args, **kw):
        return ''

    _locator_re = re.compile('(\w+?)=(.+)')

    def _to_locator(self, expression):
        """When given element, return its locator; else default to css"""
        if hasattr(expression, '_locator'):
            return expression._locator
        match = self._locator_re.match(expression)
        if match:
            return expression
        else:
            return 'css=%s' % expression

    def to_element(self, expr, browser):
        """Convert a css selector to a document element."""
        if hasattr(expr, '_locator'):
            return expr
        try:
            return browser.document.cssselect(expr)[0]
        except Exception:
            raise RuntimeError("Unknown page element %r" % expr)


class JQueryWebDriverWaitExpression(WebDriverWaitExpression):
    pass


def js_quote(string):
    """Prepare a string for use in a 'single quoted' JS literal."""
    string = string.replace('\\', r'\\')
    string = string.replace('\'', r'\'')
    return string
