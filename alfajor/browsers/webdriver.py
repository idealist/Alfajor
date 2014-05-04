# Copyright Action Without Borders, Inc., the Alfajor authors and contributors.
# All rights reserved.  See AUTHORS.
#
# This file is part of 'Alfajor' and is distributed under the BSD license.
# See LICENSE for more details.

"""Bridge to live web browsers via WebDriver RC."""
from __future__ import with_statement

from contextlib import contextmanager
import copy
import csv
from functools import partial
import json
import logging
import re
import requests
import time
from urlparse import urljoin

from blinker import signal

from alfajor.browsers._lxml import (
    _group_key_value_pairs,
    DOMElement,
    DOMMixin,
    FormElement,
    InputElement,
    SelectElement,
    TextareaElement,
    _options_xpath,
    html_parser_for,
    )
from alfajor.browsers._waitexpr import WebDriverWaitExpression, WaitExpression
from alfajor.utilities import lazy_property
from alfajor._compat import property


# these two lines enable debugging at httplib level
# (requests->urllib3->httplib)
# you will see the REQUEST, including HEADERS and DATA, and RESPONSE with
# HEADERS but without DATA.
# the only thing missing will be the response.body which is not logged.
# import httplib
# httplib.HTTPConnection.debuglevel = 1


logging.basicConfig()  # you need to initialize logging, otherwise you
                       # will not see anything from requests
logging.getLogger().setLevel(logging.DEBUG)
requests_log = logging.getLogger("requests.packages.urllib3")
requests_log.setLevel(logging.DEBUG)
requests_log.propagate = True


__all__ = ['WebDriver']
logger = logging.getLogger('tests.browser')
logger.setLevel(logging.DEBUG)
logger.propagate = True


after_browser_activity = signal('after_browser_activity')
before_browser_activity = signal('before_browser_activity')
after_page_load = signal('after_page_load')
before_page_load = signal('before_page_load')
csv.register_dialect('cookies', delimiter=';',
                     skipinitialspace=True,
                     quoting=csv.QUOTE_NONE)


class WebDriver(DOMMixin):

    capabilities = [
        'cookies',
        'javascript',
        'visibility',
        'webdriver',
        ]

    wait_expression = WebDriverWaitExpression

    def __init__(self, server_url, browser_capabilites=None, base_url=None,
                 default_timeout=16000, **kw):
        self.webdriver = WebDriverRemote(
            server_url, browser_capabilites, default_timeout)
        self._base_url = base_url

        self.status_code = 0
        self.status = ''
        self.response = None
        self.headers = {}
        self.selenium = SeleniumCompatibilityShim(self)
        self.wait_expression = kw.pop('wait_expression', self.wait_expression)

    def open(self, url, wait_for='page', timeout=None):
        logger.info('open(%s)', url)
        before_browser_activity.send(self)
        before_page_load.send(self, url=url)
        if self._base_url:
            url = urljoin(self._base_url, url)
        if not self.webdriver._session_id:
            self.webdriver.get_new_browser_session()
        self.webdriver.open(url, timeout)
        self.wait_for(wait_for, timeout)
        after_browser_activity.send(self, url=url)
        self.sync_document()
        after_page_load.send(self, url=url)

    @property
    def backend(self):
        return self.webdriver

    @property
    def current_timeout(self):
        return self.webdriver._current_timeout

    def reset(self):
        self.webdriver('DELETE', 'cookie')

    @property
    def user_agent(self):
        result = self.webdriver('GET', '')['value']
        return {
            'browser': result['browserName'],
            'platform': result['platform'],
            'version': result['version'],
            }

    def sync_document(self, wait_for=None, timeout=None):
        self.wait_for(wait_for, timeout)
        self.response = self.webdriver('GET', 'source')['value']
        self.__dict__.pop('document', None)

    @property
    def location(self):
        return self.webdriver('GET', 'url')['value']

    def wait_for(self, condition, timeout=None, frequency=None):
        wd = self.webdriver
        try:
            if not condition:
                return
            if timeout is None:
                timeout = self.current_timeout
            if condition == 'page':
                condition = self.wait_expression().page_ready()
            if condition == 'ajax':
                condition = self.wait_expression().ajax_complete()
            if isinstance(condition, WaitExpression):
                return condition(self, timeout=timeout)

            if condition.startswith('duration'):
                if ':' in condition:
                    timeout = int(condition.split(':', 1)[1])
                if timeout:
                    time.sleep(timeout / 1000.0)
                return
            if condition.startswith('js:'):
                js = condition[3:]
                return wd.wait_for_condition(js, timeout, frequency)
            elif condition.startswith('element:'):
                expr = condition[8:]
                return wd.wait_for_element_present(expr, timeout, frequency)
            elif condition.startswith('!element:'):
                expr = condition[9:]
                return wd.wait_for_element_not_present(expr, timeout, frequency)
            elif condition.startswith('visible:'):
                expr = condition[8:]
                return wd.wait_for_element_visible(expr, timeout, frequency)
            elif condition.startswith('!visible:'):
                expr = condition[9:]
                return wd.wait_for_element_invisible(expr, timeout, frequency)

        except RuntimeError, detail:
            raise AssertionError(
                'WebDriver encountered an error:  %s' % detail)

    @property
    def cookies(self):
        """A dictionary of cookie names and values."""
        cookies = {c['name']: c['value'] for c in
                   self.webdriver('GET', 'cookie')['value']}
        return {k: v[1:-1] if v.startswith('"') and v.endswith('"')
                else v for k, v in cookies.items()}

    def set_cookie(self, name, value, **kw):
        max_age = kw.pop('max_age', None)
        if max_age and 'expiry' not in kw:
            kw['expiry'] = max_age
        cookie = dict(name=name, value=unicode(value))
        for key in ('path', 'domain', 'secure', 'expiry'):
            if key in kw:
                cookie[key] = kw[key]
        self.webdriver('POST', 'cookie', cookie=cookie)

    def delete_cookie(self, name, domain=None, path=None):
        self.webdriver('DELETE', 'cookie' + '/' + name)

    # temporary...
    def stop(self):
        self.webdriver.test_complete()

    @lazy_property
    def _lxml_parser(self):
        return html_parser_for(self, webdriver_elements)


class SeleniumCompatibilityShim(object):

    def __init__(self, browser):
        self.browser = browser

    def __getattr__(self, key):
        # proxy methods calls through to Selenium, converting
        # python_form to camelCase
        if '_' in key:
            key = toCamelCase(key)
        kw = {}
        if key.startswith('is') or key.startswith('getWhether'):
            kw['transform'] = 'bool'
        elif (key.startswith('get') and
              any(x in key for x in ('Speed', 'Position',
                                     'Height', 'Width',
                                     'Index', 'Count'))):
            kw['transform'] = 'int'
        if key.startswith('get') and key[-1] == 's':
            kw['list'] = True
        return partial(self, key, **kw)

    def __call__(self, *args, **kw):
        if args[0] == 'runScript':
            kw['script'] = 'return %s' % args[1]
            kw['args'] = []
            return self.browser.webdriver('POST', 'execute', **kw)
        raise NotImplementedError("No compatibility shim for %s" % args[0])


class WebDriverRemote(object):

    def __init__(self, server_url, browser_capabilities=None,
                 default_timeout=None):
        self._server_url = server_url.rstrip('/') + '/wd/hub'
        self._user_agent = None
        self._session_id = None
        self._desired_capabilities = browser_capabilities
        self._default_timeout = default_timeout
        self._current_timeout = None
        self._req_session = None

    def get_new_browser_session(self, **capabilities):
        self._req_session = requests.Session()
        self._req_session.headers.update({
            'Accept': 'application/json; charset=UTF-8',
            'Content-Type': 'application/json'
            })
        caps = copy.copy(self._desired_capabilities or {})
        caps.update(capabilities)
        if 'browserName' not in caps:
            caps['browserName'] = 'phantomjs'

        result = self._raw_call('POST', 'session', desiredCapabilities=caps)
        self._session_id = result['sessionId']
        self.set_timeout(self._default_timeout)
        #self._user_agent = self.get_eval('navigator.userAgent')

    getNewBrowserSession = get_new_browser_session

    def test_complete(self):
        self('DELETE')
        self._session_id = None

    testComplete = test_complete

    def _raw_call(self, method, command, *args, **kw):
        #transform = _transformers[kw.pop('transform', 'unicode')]
        #return_list = kw.pop('list', False)
        #return_dict = kw.pop('dict', False)
        #assert not kw, 'Unknown keyword argument.'

        #payload = {'cmd': command, 'sessionId': self._session_id}
        #for idx, arg in enumerate(args):
        #    payload[str(idx + 1)] = arg

        logger.debug('webdriver(%s, %r, %r)', command, args, kw)
        response = self._req_session.request(method,
                                             self._server_url + '/' + command,
                                             data=json.dumps(kw))
        if not response.status_code < 300:
            exc = RuntimeError
            try:
                data = response.json()
                error = jsonwire_errors[data['status']]
                exc = globals()[error['summary']]
                msg = data['value'].get('state') or error['detail']
            except:
                msg = 'Invalid Request: %s' % response.text
            raise exc(msg)
        data = None
        if response.status_code == 200:
            data = response.json()

        return data

    def __call__(self, method, command='', **kw):
        if not self._session_id:
            raise Exception('No webdriver session.')
        endpoint = 'session/' + self._session_id + '/' + unicode(command)
        return self._raw_call(method, endpoint, **kw)

    def __getattr__(self, key):
        # proxy methods calls through to WebDriver, converting
        # python_form to camelCase
        if '_' in key:
            key = toCamelCase(key)
        kw = {}
        if key.startswith('is') or key.startswith('getWhether'):
            kw['transform'] = 'bool'
        elif (key.startswith('get') and
              any(x in key for x in ('Speed', 'Position',
                                     'Height', 'Width',
                                     'Index', 'Count'))):
            kw['transform'] = 'int'
        if key.startswith('get') and key[-1] == 's':
            kw['list'] = True
        return partial(self, key, **kw)

    def set_timeout(self, value):
        # May be a no-op if the current session timeout is the same as the
        # requested value.
        if value is None:
            return
        if value != self._current_timeout:
            self('POST', 'timeouts', type='page load', ms=value)
        self._current_timeout = value

    def open(self, url, timeout=None):
        with self._scoped_timeout(timeout):
            # Workaround for XHR ERROR failure on non-200 responses
            # http://code.google.com/p/selenium/issues/detail?id=408
            self('POST', 'url', url=url)

    _default_frequency = 250

    def _exec_with_timeout(self, operation, timeout=None, frequency=None):
        if timeout is None:
            timeout = self._current_timeout or self._default_timeout or 2000
        if frequency is None:
            frequency = self._default_frequency
        frequency = frequency / 1000.0
        end_time = time.time() + timeout / 1000.0

        while(True):
            result = operation()
            if result:
                return result
            time.sleep(frequency)
            if time.time() > end_time:
                break
        # The selenium browser raises AssertionError
        raise AssertionError('timeout')

    def wait_for_condition(self, expression, timeout=None, frequency=None):
        script = "return (function() { var value = %s; return value; })()"
        operation = lambda: self('POST', 'execute', script=script % expression,
                                 args=[])['value']
        return self._exec_with_timeout(operation, timeout, frequency)

    def _to_locator(self, expression):
        if '=' in expression:
            strategy, value = expression.split('=', 1)
            # others?
            if strategy == 'css':
                strategy = 'css selector'
        else:
            strategy = 'xpath'
            value = expression
        return strategy, value

    def wait_for_element_present(self, expression, timeout=None,
                                 frequency=None):
        def _find_element(driver):
            try:
                strategy, value = self._to_locator(expression)
                driver('POST', 'element', using=strategy, value=value)
                return True
            except NoSuchElement:
                return False
        operation = lambda: _find_element(self)
        return self._exec_with_timeout(operation, timeout, frequency)

    def wait_for_element_not_present(self, expression, timeout=None,
                                     frequency=None):
        def _find_element(driver):
            try:
                strategy, value = self._to_locator(expression)
                driver('POST', 'element', using=strategy, value=value)
                return False
            except NoSuchElement:
                return True
        operation = lambda: _find_element(self)
        return self._exec_with_timeout(operation, timeout, frequency)

    def wait_for_element_visible(self, expression, timeout=None,
                                 frequency=None):
        def _element_visible(driver):
            try:
                strategy, value = self._to_locator(expression)
                el = driver('POST', 'element', using=strategy, value=value
                            )['value']['ELEMENT']
                displayed = driver('GET', 'element/%s/displayed' % el)['value']
                return displayed
            except ElementNotVisible:
                return False
        operation = lambda: _element_visible(self)
        return self._exec_with_timeout(operation, timeout, frequency)

    def wait_for_element_invisible(self, expression, timeout=None,
                                   frequency=None):
        def _element_visible(driver):
            try:
                strategy, value = self._to_locator(expression)
                el = driver('POST', 'element', using=strategy, value=value
                            )['value']['ELEMENT']
                displayed = driver('GET', 'element/%s/displayed' % el)['value']
                return not displayed
            except ElementNotVisible:
                # if an element doesn't exist, it's invisible, right? or raise?
                return True
        operation = lambda: _element_visible(self)
        return self._exec_with_timeout(operation, timeout, frequency)

    @contextmanager
    def _scoped_timeout(self, timeout):
        """Used in 'with' statements to temporarily apply *timeout*."""
        current_timeout = self._current_timeout
        need_custom = timeout is not None and timeout != current_timeout
        if not need_custom:
            # Nothing to do: timeout is already in effect.
            yield
        else:
            # Set the temporary timeout value.
            self.set_timeout(timeout)
            try:
                yield
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception, exc:
                try:
                    # Got an error, try to reset the timeout.
                    self.set_timeout(current_timeout)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except:
                    # Oh well.
                    pass
                raise exc
            else:
                # Reset the timeout to what it used to be.
                self.set_timeout(current_timeout)


_transformers = {
    'unicode': lambda d: unicode(d, 'utf-8'),
    'int': int,
    'bool': lambda d: {'true': True, 'false': False}.get(d, None),
    }

_underscrore_re = re.compile(r'_([a-z])')
_camel_convert = lambda match: match.group(1).upper()


def toCamelCase(string):
    """Convert a_underscore_string to aCamelCase string."""
    return re.sub(_underscrore_re, _camel_convert, string)


def event_sender(name, default_wait_for=None):
    webdriver_name = toCamelCase(name)

    def handler(self, wait_for=default_wait_for, timeout=None):
        before_browser_activity.send(self.browser)
        if wait_for == 'page':
            # set a flag on window which will be undefined once page reloads,
            # signalling that page is ready
            self.browser.webdriver('POST', 'execute',
                script='window.__alfajor_webdriver_page__ = true', args=[])
        element = self.wd_id()
        if 'doubleclick' in name:
            self.browser.webdriver('POST', 'moveto', element=element)
            self.browser.webdriver('POST', '%s' % (element, webdriver_name))
        elif name == 'mouse_over':
            # compatibility w/ selenium rc
            self.browser.webdriver('POST', 'moveto', element=element)
        else:
            self.browser.webdriver('POST', 'element/%s/%s' % (element, webdriver_name))
        # XXX:dc: when would a None wait_for be a good thing?
        if wait_for:
            self.browser.wait_for(wait_for, timeout)
        after_browser_activity.send(self.browser)
        self.browser.sync_document()
    handler.__name__ = name
    handler.__doc__ = "Emit %s on this element." % webdriver_name
    return handler


class FormElement(FormElement):
    """A <form/> that can be submitted."""

    submit = event_sender('submit', 'page')

    def fill(self, values, wait_for=None, timeout=None, with_prefix=u''):
        grouped = _group_key_value_pairs(values, with_prefix)
        _fill_form_async(self, grouped, wait_for, timeout)


def _fill_fields(fields, values):
    """Fill all possible *fields* with key/[value] pairs from *values*.

    :return: subset of *values* that raised ValueError on fill (e.g. a select
      could not be filled in because JavaScript has not yet set its values.)

    """
    unfilled = []
    for name, field_values in values:
        if len(field_values) == 1:
            value = field_values[0]
        else:
            value = field_values
        try:
            fields[name] = value
        except ValueError:
            unfilled.append((name, field_values))
    return unfilled


def _fill_form_async(form, values, wait_for=None, timeout=None):
    """Fill *form* with *values*, retrying fields that fail with ValueErrors.

    If multiple passes are required to set all fields in *values, the document
    will be re-synchronizes between attempts with *wait_for* called between
    each attempt.

    """
    browser = form.browser
    unset_count = len(values)
    while values:
        values = _fill_fields(form.fields, values)
        if len(values) == unset_count:
            # nothing was able to be set
            raise ValueError("Unable to set fields %s" % (
                ', '.join(pair[0] for pair in values)))
        if wait_for:
            browser.wait_for(wait_for, timeout)
        browser.sync_document()
        # replace *form* with the new lxml element from the refreshed document
        form = browser.document.xpath(form.fq_xpath)[0]
        unset_count = len(values)


def type_text(element, text, allow_newlines=False):
    webdriver = element.browser.webdriver
    # Store the original value
    webdriver('POST', 'element/%s/value' % element.wd_id(),
              value=[c for c in text])


class InputElement(InputElement):
    """Input fields that can be filled in."""

    @property
    def value(self):
        """The value= of this input."""
        return self.browser.webdriver('GET',
                                      'element/%s/value' % self.wd_id()
                                      )['value']

    @value.setter
    def value(self, value):
        if self.checkable:
            group = self.form['input[name=%s]' % self.name]
            if self.type == 'radio':
                target = self.form.cssselect(
                    'input[name=%s][value=%s]' % (self.name, value))
                if target:
                    target[0].checked = True
            if self.type == 'checkbox':
                if len(group) > 1:
                    if isinstance(value, basestring):
                        discriminator = lambda i, v: i.value == value
                    else:
                        discriminator = lambda i, v: i.value in value
                    for input in group:
                        if discriminator(input.value, value):
                            input.checked = True
                        else:
                            input.checked = False
                elif len(group) == 1:
                    self.checked = bool(value)
        else:
            self.attrib['value'] = value
            self.browser.webdriver('POST', 'element/%s/clear' % self.wd_id())
            type_text(self, value)

    @value.deleter
    def value(self):
        if self.checkable:
            self.checked = False
        else:
            if 'value' in self.attrib:
                del self.attrib['value']
            self.browser.webdriver('POST', 'element/%s/value' % self.wd_id(),
                                   value=[])

    @property
    def checked(self):
        if not self.checkable:
            raise AttributeError('Not a checkable input type')
        return self.browser.webdriver('GET',
                                      'element/%s/selected' % self.wd_id()
                                      )['value']

    @checked.setter
    def checked(self, value):
        """True if a checkable type is checked.  Assignable."""
        current_state = self.checked
        if value == current_state:
            return
        # can't un-check a radio button
        if self.type == 'radio' and current_state:
            return
        elif self.type == 'radio':
            self.browser.webdriver('POST', 'element/%s/click' % self.wd_id())
            self.attrib['checked'] = ''
            for el in self.form.inputs[self.name]:
                if el.value != self.value:
                    el.attrib.pop('checked', None)
        else:
            self.browser.webdriver('POST', 'element/%s/click' % self.wd_id())

    def set(self, key, value):
        if key != 'checked':
            super(InputElement, self).set(key, value)
        self.checked = True

    def enter(self, text, wait_for='duration', timeout=0.1):
        type_text(self, text)


class TextareaElement(TextareaElement):

    @property
    def value(self):
        """The value= of this input."""
        return self.browser.webdriver('GET',
                                      'element/%s/value' % self.wd_id()
                                      )['value']

    @value.setter
    def value(self, value):
        self.attrib['value'] = value
        self.browser.webdriver('POST', 'element/%s/clear' % self.wd_id())
        self.browser.webdriver('POST', 'element/%s/value' % self.wd_id(),
                               value=[c for c in value])

    def enter(self, text, wait_for='duration', timeout=0.1):
        type_text(self, text)


def _get_value_and_locator_from_option(webdriver, option):
    id = option.wd_id()
    if 'value' in option.attrib:
        if option.get('value') is None:
            value = None
        else:
            value = option.get('value')
    else:
        value = webdriver('GET', 'element/%s/text' % id)['value']
    return value, id


class SelectElement(SelectElement):

    def _value__set(self, value):
        super(SelectElement, self)._value__set(value)
        selected = [el for el in _options_xpath(self)
                    if 'selected' in el.attrib]
        if self.multiple:
            values = value
            # TODO: decide when to send ctrl vs command key?
            # send command for multiple-select
            self.browser.webdriver('POST', 'keys', value=[u'\ue03d'])
        else:
            values = [value]
        for el in selected:
            val, option_locator = _get_value_and_locator_from_option(
                self.browser.webdriver, el)
            if val not in values:
                raise AssertionError("Option with value %r not present in "
                                     "remote document!" % val)
            if self.multiple:
                self.browser.webdriver('POST',
                                       'element/%s/click' % option_locator)
            else:
                self.browser.webdriver('POST',
                                       'element/%s/click' % option_locator)
                break
        if self.multiple:
            # clear modifier
            self.browser.webdriver('POST', 'keys', value=[u'\ue000'])

    value = property(SelectElement._value__get, _value__set)


class DOMElement(DOMElement):
    """Behavior for all lxml Element types."""

    @property
    def _locator(self):
        """The fastest locator expression for this element."""
        try:
            return ('id', self.attrib['id'])
        except KeyError:
            return ('xpath', self.fq_xpath)

    click = event_sender('click', 'page')
    double_click = event_sender('double_click')
    mouse_over = event_sender('mouse_over')
    mouse_out = event_sender('mouse_out')
    context_menu = event_sender('context_menu')
    focus = event_sender('focus')

    def wd_id(self):
        using, selector = self._locator
        return self.browser.webdriver('POST', 'element', using=using,
                                      value=selector)['value']['ELEMENT']

    def fire_event(self, name):
        before_browser_activity.send(self.browser)
        self.browser.webdriver('fireEvent', self._locator, name)
        after_browser_activity.send(self.browser)

    @property
    def is_visible(self):
        return self.browser.webdriver('GET',
                                      'element/%s/displayed' % self.wd_id()
                                      )['value']


webdriver_elements = {
    '*': DOMElement,
    'form': FormElement,
    'input': InputElement,
    'select': SelectElement,
    'textarea': TextareaElement,
    }


jsonwire_errors = {
    0: {'detail': 'The command executed successfully.', 'summary': 'Success'},
    7: {'detail': ('An element could not be located on the page using the '
                   'givensearch parameters.'),
        'summary': 'NoSuchElement'},
    8: {'detail': ('A request to switch to a frame could not be satisfied'
                   ' because the frame could not be found.'),
        'summary': 'NoSuchFrame'},
    9: {'detail': ('The requested resource could not be found, or a request '
                   'was received using an HTTP method that is not supported'
                   ' by the mapped resource.'),
        'summary': 'UnknownCommand'},
    10: {'detail': ('An element command failed because the referenced element '
                    ' is no longer attached to the DOM.'),
         'summary': 'StaleElementReference'},
    11: {'detail': ('An element command could not be completed because the'
                    ' element is not visible on the page.'),
         'summary': 'ElementNotVisible'},
    12: {'detail': ('An element command could not be completed because the '
                    ' element is in an invalid state (e.g. attempting to '
                    'click a disabled element).'),
         'summary': 'InvalidElementState'},
    13: {'detail': ('An unknown server-side error occurred while processing'
                    ' the command.'),
         'summary': 'UnknownError'},
    15: {'detail': ('An attempt was made to select an element that cannot '
                    'be selected.'),
         'summary': 'ElementIsNotSelectable'},
    17: {'detail': ('An error occurred while executing user supplied'
                    ' JavaScript.'),
         'summary': 'JavaScriptError'},
    19: {'detail': ('An error occurred while searching for an element by'
                    ' XPath.'),
         'summary': 'XPathLookupError'},
    21: {'detail': 'An operation did not complete before its timeout expired.',
         'summary': 'Timeout'},
    23: {'detail': ('A request to switch to a different window could not be'
                    ' satisfied because the window could not be found.'),
         'summary': 'NoSuchWindow'},
    24: {'detail': ('An illegal attempt was made to set a cookie under a '
                    'different domain than the current page.'),
         'summary': 'InvalidCookieDomain'},
    25: {'detail': "A request to set a cookie's value could not be satisfied.",
         'summary': 'UnableToSetCookie'},
    26: {'detail': 'A modal dialog was open, blocking this operation',
         'summary': 'UnexpectedAlertOpen'},
    27: {'detail': ('An attempt was made to operate on a modal dialog when '
                    'one was not open.'),
         'summary': 'NoAlertOpenError'},
    28: {'detail': 'A script did not complete before its timeout expired.',
         'summary': 'ScriptTimeout'},
    29: {'detail': ('The coordinates provided to an interactions operation '
                    'are invalid.'),
         'summary': 'InvalidElementCoordinates'},
    30: {'detail': 'IME was not available.', 'summary': 'IMENotAvailable'},
    31: {'detail': 'An IME engine could not be started.',
         'summary': 'IMEEngineActivationFailed'},
    32: {'detail': 'Argument was an invalid selector (e.g. XPath/CSS).',
         'summary': 'InvalidSelector'}
}


class WebDriverException(Exception):
    pass


class NoSuchElement(WebDriverException):
    pass


class NoSuchFrame(WebDriverException):
    pass


class UnknownCommand(WebDriverException):
    pass


class StaleElementReference(WebDriverException):
    pass


class ElementNotVisible(WebDriverException):
    pass


class InvalidElementState(WebDriverException):
    pass


class UnknownError(WebDriverException):
    pass


class ElementIsNotSelectable(WebDriverException):
    pass


class JavaScriptError(WebDriverException):
    pass


class XPathLookupError(WebDriverException):
    pass


class Timeout(WebDriverException):
    pass


class NoSuchWindow(WebDriverException):
    pass


class InvalidCookieDomain(WebDriverException):
    pass


class UnableToSetCookie(WebDriverException):
    pass


class UnexpectedAlertOpen(WebDriverException):
    pass


class NoAlertOpenError(WebDriverException):
    pass


class ScriptTimeout(WebDriverException):
    pass


class InvalidElementCoordinates(WebDriverException):
    pass


class IMENotAvailable(WebDriverException):
    pass


class IMEEngineActivationFailed(WebDriverException):
    pass


class InvalidSelector(WebDriverException):
    pass
