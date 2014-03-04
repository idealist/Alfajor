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
from cStringIO import StringIO
from functools import partial
import json
from logging import getLogger
import re
import requests
import time
from urlparse import urljoin
from warnings import warn

from blinker import signal
from werkzeug import UserAgent, url_encode

from alfajor.browsers._lxml import (
    _append_text_value,
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
from alfajor.browsers._waitexpr import SeleniumWaitExpression, WaitExpression
from alfajor.utilities import lazy_property
from alfajor._compat import property

import requests
import logging

# these two lines enable debugging at httplib level (requests->urllib3->httplib)
# you will see the REQUEST, including HEADERS and DATA, and RESPONSE with HEADERS but without DATA.
# the only thing missing will be the response.body which is not logged.
import httplib
httplib.HTTPConnection.debuglevel = 1

logging.basicConfig() # you need to initialize logging, otherwise you will not see anything from requests
logging.getLogger().setLevel(logging.DEBUG)
requests_log = logging.getLogger("requests.packages.urllib3")
requests_log.setLevel(logging.DEBUG)
requests_log.propagate = True


__all__ = ['WebDriver']
logger = getLogger('tests.browser')
after_browser_activity = signal('after_browser_activity')
before_browser_activity = signal('before_browser_activity')
after_page_load = signal('after_page_load')
before_page_load = signal('before_page_load')
_enterable_chars_re = re.compile(r'(\\[a-z]|\\\d+|.)')
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

    wait_expression = SeleniumWaitExpression

    def __init__(self, server_url, browser_capabilites=None, base_url=None,
                 default_timeout=16000, **kw):
        self.webdriver = WebDriverRemote(
            server_url, browser_capabilites, default_timeout)
        self._base_url = base_url

        self.status_code = 0
        self.status = ''
        self.response = None
        self.headers = {}
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
        if wait_for != 'page':
            self.wait_for(wait_for, timeout)
        after_browser_activity.send(self, url=url)
        self.sync_document()
        after_page_load.send(self, url=url)

    @property
    def backend(self):
        return self.selenium

    def reset(self):
        self.webdriver('DELETE', 'cookie')

    @property
    def user_agent(self):
        if not self.webdriver._user_agent:
            return dict.fromkeys(('browser', 'platform', 'version'), 'unknown')
        ua = UserAgent(self.webdriver._user_agent)
        return {
            'browser': ua.browser,
            'platform': ua.platform,
            'version': ua.version,
            }

    def sync_document(self, wait_for=None, timeout=None):
        self.wait_for(wait_for, timeout)
        self.response = self.webdriver('GET', 'source')['value']
        self.__dict__.pop('document', None)

    @property
    def location(self):
        return self.webdriver('GET', 'url')['value']

    def wait_for(self, condition, timeout=None):
        # TODO
        return
        try:
            if not condition:
                return
            if condition == 'ajax':
                condition = self.wait_expression(['ajax_complete'])
            if isinstance(condition, WaitExpression):
                condition = u'js:' + unicode(condition)

            if condition == 'duration':
                if timeout:
                    time.sleep(timeout / 1000.0)
                return
            if timeout is None:
                timeout = self.webdriver._current_timeout
            if condition == 'page':
                self.webdriver('waitForPageToLoad', timeout)
            elif condition.startswith('js:'):
                expr = condition[3:]
                js = ('var window = webdriver.browserbot.getCurrentWindow(); ' +
                      expr)
                self.webdriver('waitForCondition', js, timeout)
            elif condition.startswith('element:'):
                expr = condition[8:]
                self.webdriver.wait_for_element_present(expr, timeout)
            elif condition.startswith('!element:'):
                expr = condition[9:]
                self.webdriver.wait_for_element_not_present(expr, timeout)
        except RuntimeError, detail:
            raise AssertionError('WebDriver encountered an error:  %s' % detail)

    @property
    def cookies(self):
        """A dictionary of cookie names and values."""
        return {c.name: c for c in self.webdriver('GET', 'cookie')['value']}

    def set_cookie(self, name, value, **kw):
        max_age = kw.pop('max_age')
        if max_age and 'expiry' not in kw:
            kw['expiry'] = max_age
        cookie = dict(name=name, value=value)
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


class WebDriverRemote(object):

    def __init__(self, server_url, browser_capabilities=None, default_timeout=None):
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
        #self('getNewBrowserSession', self._browser_cmd,
        #                        browser_url, extension_js, opts)
        self.set_timeout(self._default_timeout)
        #self._user_agent = self.get_eval('navigator.userAgent')

    getNewBrowserSession = get_new_browser_session

    def test_complete(self):
        self('testComplete')
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

        logger.debug('webdriver(%s, %r)', command, args)
        response = self._req_session.request(
                method, self._server_url + '/' + command, data=json.dumps(kw))
        if not response.status_code < 300:
            raise RuntimeError('Invalid Request: ' + response.text)
        data = None
        if response.status_code == 200:
            data = response.json()

        if data and data['status'] != 0:
            raise RuntimeError(data['message'].encode('utf-8'))

        return data

    def __call__(self, method, command, **kw):
        if not self._session_id:
            raise Exception('No webdriver session.')
        endpoint = 'session/' + self._session_id + '/' + unicode(command)
        return self._raw_call(method, endpoint, **kw)

        data = response[3:]
        if return_list:
            rows = list(csv.reader(StringIO(data)))
            return [transform(col) for col in rows[0]]

        elif return_dict:
            rows = list(csv.reader(StringIO(data), 'cookies'))

            if rows:
                return dict(
                    # Transform mycookie="abc=def" into ['mycookie', 'abc=def']
                    map(lambda x: x.strip('"'), x.split('=', 1))
                        for x in rows[0])
            else:
                return {}
        else:
            return transform(data)

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
            # http://code.google.com/p/webdriver/issues/detail?id=408
            self('POST', 'url', url=url)

    def wait_for_element_present(self, expression, timeout=None):
        with self._scoped_timeout(timeout):
            self('waitForElementPresent', expression)

    def wait_for_element_not_present(self, expression, timeout=None):
        with self._scoped_timeout(timeout):
            self('waitForElementNotPresent', expression)

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


def event_sender(name):
    webdriver_name = toCamelCase(name)

    def handler(self, wait_for=None, timeout=None):
        before_browser_activity.send(self.browser)
        self.browser.webdriver(webdriver_name, self._locator)
        # XXX:dc: when would a None wait_for be a good thing?
        if wait_for:
            self.browser.wait_for(wait_for, timeout)
        time.sleep(0.2)
        after_browser_activity.send(self.browser)
        self.browser.sync_document()
    handler.__name__ = name
    handler.__doc__ = "Emit %s on this element." % webdriver_name
    return handler


class FormElement(FormElement):
    """A <form/> that can be submitted."""

    submit = event_sender('submit')

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


def type_text(element, text, wait_for=None, timeout=0, allow_newlines=False):
    # webdriver.type_keys() doesn't work with non-printables like backspace
    webdriver, locator = element.browser.webdriver, element._locator
    # Store the original value
    field_value = element.value
    for char in _enterable_chars_re.findall(text):
        field_value = _append_text_value(field_value, char, allow_newlines)
        if len(char) == 1 and ord(char) < 32:
            char = r'\%i' % ord(char)
        webdriver.key_down(locator, char)
        # Most browsers do not allow events to do the actual typing,
        # so we need to set the value
        if element.browser.user_agent['browser'] != 'firefox':
            webdriver.type(locator, field_value)
        webdriver.key_press(locator, char)
        webdriver.key_up(locator, char)
    if wait_for and timeout:
        element.browser.wait_for(wait_for, timeout)
        element.browser.sync_document()


class InputElement(InputElement):
    """Input fields that can be filled in."""

    @property
    def value(self):
        """The value= of this input."""
        if self.checkable:
            # doesn't seem possible to mutate get value- via webdriver
            return self.attrib.get('value', '')
        return self.browser.webdriver('getValue', self._locator)

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
            self.browser.webdriver('type', self._locator, value)

    @value.deleter
    def value(self):
        if self.checkable:
            self.checked = False
        else:
            if 'value' in self.attrib:
                del self.attrib['value']
            self.browser.webdriver('type', self._locator, u'')

    @property
    def checked(self):
        if not self.checkable:
            raise AttributeError('Not a checkable input type')
        status = self.browser.webdriver.is_checked(self._locator)
        if status:
            self.attrib['checked'] = ''
        else:
            self.attrib.pop('checked', None)
        return status

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
            self.browser.webdriver('check', self._locator)
            self.attrib['checked'] = ''
            for el in self.form.inputs[self.name]:
                if el.value != self.value:
                    el.attrib.pop('checked', None)
        else:
            if value:
                self.browser.webdriver('check', self._locator)
                self.attrib['checked'] = ''
            else:
                self.browser.webdriver('uncheck', self._locator)
                self.attrib.pop('checked', None)
        # XXX:eo webdriver doesn't necessarily trigger this event for us,
        # so do it manually
        self.fire_event('change')

    def set(self, key, value):
        if key != 'checked':
            super(InputElement, self).set(key, value)
        self.checked = True

    def enter(self, text, wait_for='duration', timeout=0.1):
        type_text(self, text, wait_for, timeout)


class TextareaElement(TextareaElement):

    @property
    def value(self):
        """The value= of this input."""
        return self.browser.webdriver('getValue', self._locator)

    @value.setter
    def value(self, value):
        self.attrib['value'] = value
        self.browser.webdriver('type', self._locator, value)

    def enter(self, text, wait_for='duration', timeout=0.1):
        type_text(self, text, wait_for, timeout, allow_newlines=True)


def _get_value_and_locator_from_option(option):
    if 'value' in option.attrib:
        if option.get('value') is None:
            return None, u'value=regexp:^$'
        else:
            return option.get('value'), u'value=%s' % option.get('value')
    option_text = (option.text or u'').strip()
    return option_text, u'label=%s' % option_text


class SelectElement(SelectElement):

    def _value__set(self, value):
        super(SelectElement, self)._value__set(value)
        selected = [el for el in _options_xpath(self)
                    if 'selected' in el.attrib]
        if self.multiple:
            values = value
        else:
            values = [value]
        for el in selected:
            val, option_locator = _get_value_and_locator_from_option(el)
            if val not in values:
                raise AssertionError("Option with value %r not present in "
                                     "remote document!" % val)
            if self.multiple:
                self.browser.webdriver('addSelection', self._locator,
                                        option_locator)
            else:
                self.browser.webdriver('select', self._locator, option_locator)
                break

    value = property(SelectElement._value__get, _value__set)


class DOMElement(DOMElement):
    """Behavior for all lxml Element types."""

    @property
    def _locator(self):
        """The fastest WebDriver locator expression for this element."""
        try:
            return 'id=' + self.attrib['id']
        except KeyError:
            return 'xpath=' + self.fq_xpath

    click = event_sender('click')
    double_click = event_sender('double_click')
    mouse_over = event_sender('mouse_over')
    mouse_out = event_sender('mouse_out')
    context_menu = event_sender('context_menu')
    focus = event_sender('focus')

    def fire_event(self, name):
        before_browser_activity.send(self.browser)
        self.browser.webdriver('fireEvent', self._locator, name)
        after_browser_activity.send(self.browser)

    @property
    def is_visible(self):
        return self.browser.webdriver.is_visible(self._locator)


webdriver_elements = {
    '*': DOMElement,
    'form': FormElement,
    'input': InputElement,
    'select': SelectElement,
    'textarea': TextareaElement,
    }
