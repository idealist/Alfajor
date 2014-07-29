# Copyright Action Without Borders, Inc., the Alfajor authors and contributors.
# All rights reserved.  See AUTHORS.
#
# This file is part of 'alfajor' and is distributed under the BSD license.
# See LICENSE for more details.

import os
import tempfile

from jinja2 import Template
from werkzeug import Response, Request, SharedDataMiddleware
from werkzeug.exceptions import NotFound, HTTPException
from werkzeug.routing import Map, Rule

from alfajor._compat import json_dumps as dumps


class WebApp(object):

    url_map = Map([
        # Uurls like /form/fill get turned into templates/form_fill.html
        # automatically in __call__ and don't need a Rule & endpoint.
        #
        # We only need Rules and endpoints for alternate mappings or to do
        # dynamic processing.
        Rule('/', endpoint='index'),
        Rule('/assign-cookie/1', endpoint='assign_cookie'),
        Rule('/assign-cookie/2', endpoint='assign_cookies'),
        ])

    def __call__(self, environ, start_response):
        request = Request(environ)
        urls = self.url_map.bind_to_environ(environ)
        try:
            endpoint, args = urls.match()
        except NotFound, exc:
            # Convert unknown /path/names into endpoints named path_names
            endpoint = request.path.lstrip('/').replace('/', '_')
            args = {}
        environ['routing_args'] = args
        environ['endpoint'] = endpoint

        try:
            # endpoints can be methods on this class
            handler = getattr(self, endpoint)
        except AttributeError:
            # or otherwise assumed to be files in templates/<endpoint>.html
            handler = self.generic_template_renderer
        self.call_count = getattr(self, 'call_count', 0) + 1
        try:
            response = handler(request)
        except HTTPException, exc:
            # ok, maybe it really was a bogus URL.
            return exc(environ, start_response)
        return response(environ, start_response)

    def generic_template_renderer(self, request):
        path = '%s/templates/%s.html' % (
            os.path.dirname(__file__), request.environ['endpoint'])
        try:
            source = open(path).read()
        except IOError:
            raise NotFound()
        template = Template(source)
        files = []
        for name, file in request.files.items():
            # Save the uploaded files to tmp storage.
            # The calling test should delete the files.
            fh, fname = tempfile.mkstemp()
            os.close(fh)
            file.save(fname)
            files.append(
                (name, (file.filename, file.content_type,
                        file.content_length, fname)))
        context = dict(
            request=request,
            request_id=self.call_count,
            args=dumps(sorted(request.args.items(multi=True))),
            form=dumps(sorted(request.form.items(multi=True))),
            data=dumps(sorted(request.args.items(multi=True) +
                              request.form.items(multi=True))),
            files=dumps(sorted(files)),
            referrer=request.referrer or '',
            #args=..
            )
        body = template.render(context)
        return Response(body, mimetype='text/html')

    def seq_c(self, request):
        rsp = self.generic_template_renderer(request)
        rsp.status = '301 Redirect'
        rsp.location = request.host_url.rstrip('/') + '/seq/d'
        return rsp

    def assign_cookie(self, request):
        rsp = self.generic_template_renderer(request)
        rsp.set_cookie('cookie1', 'value1', path='/')

        if request.args.get('bounce'):
            rsp.status = '301 Redirect'
            rsp.location = request.args['bounce']
        return rsp

    def assign_cookies(self, request):
        rsp = self.generic_template_renderer(request)
        rsp.set_cookie('cookie1', 'value1', path='/')
        rsp.set_cookie('cookie2', 'value 2', path='/')
        if request.args.get('bounce'):
            rsp.status = '301 Redirect'
            rsp.location = request.args['bounce']
        return rsp


def webapp():
    static_path = os.path.join(os.path.dirname(__file__), 'static')
    return SharedDataMiddleware(WebApp(), {'/javascript': static_path})


def run(bind_address='0.0.0.0', port=8008):
    """Run the webapp in a simple server process."""
    from werkzeug import run_simple
    print "* Starting on %s:%s" % (bind_address, port)
    run_simple(bind_address, port, webapp(),
               use_reloader=False, threaded=True)
