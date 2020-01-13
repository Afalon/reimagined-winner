"""Open Library Books API
"""

import dynlinks
import readlinks

import urlparse
import re
import urllib2
import web

from infogami.infobase import _json as simplejson
from infogami.utils import delegate
from infogami.plugins.api.code import jsonapi

class books_json(delegate.page):
    path = "/api/books"

    @jsonapi
    def GET(self):
        i = web.input(bibkeys='', callback=None, details="false")
        if web.ctx.path.endswith('.json'):
            i.format = 'json'
        return dynlinks.dynlinks(i.bibkeys.split(","), i)

class read_singleget(delegate.page):
    """Handle the single-lookup form of the Hathi-style API
    """
    path = r"/api/volumes/(brief|full)/(oclc|lccn|issn|isbn|htid|olid|recordnumber)/(.+)"
    encoding = "json"
    @jsonapi
    def GET(self, brief_or_full, idtype, idval):
        i = web.input()

        web.ctx.headers = []
        req = '%s:%s' % (idtype, idval)
        result = readlinks.readlinks(req, i)
        if req in result:
            result = result[req]
        else:
            result = []
        return simplejson.dumps(result)

class read_multiget(delegate.page):
    """Handle the multi-lookup form of the Hathi-style API
    """
    path = r"/api/volumes/(brief|full)/json/(.+)"
    path_re = re.compile(path)
    @jsonapi
    def GET(self, brief_or_full, req): # params aren't used, see below
        i = web.input()

        # Work around issue with gunicorn where semicolon and after
        # get truncated.  (web.input() still seems ok)
        # see https://github.com/benoitc/gunicorn/issues/215
        raw_uri = web.ctx.env.get("RAW_URI")
        if raw_uri:
            raw_path = urlparse.urlsplit(raw_uri).path

            # handle e.g. '%7C' for '|'
            decoded_path = urllib2.unquote(raw_path)

            m = self.path_re.match(decoded_path)
            if not len(m.groups()) == 2:
                return simplejson.dumps({})
            (brief_or_full, req) = m.groups()

        web.ctx.headers = []
        result = readlinks.readlinks(req, i)
        return simplejson.dumps(result)
