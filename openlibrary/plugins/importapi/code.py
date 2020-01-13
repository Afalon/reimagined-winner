"""Open Library Import API
"""

from infogami.plugins.api.code import add_hook
from infogami import config
from openlibrary.plugins.openlibrary.code import can_write
from openlibrary.catalog.marc.marc_binary import MarcBinary, MarcException
from openlibrary.catalog.marc.marc_xml import MarcXml
from openlibrary.catalog.marc.parse import read_edition
from openlibrary.catalog import add_book
from openlibrary.catalog.get_ia import get_marc_record_from_ia, get_from_archive_bulk
from openlibrary import accounts
from openlibrary import records
from openlibrary.core import ia

import web

import base64
import json
import re
import urllib

import import_opds
import import_rdf
import import_edition_builder
from lxml import etree
import logging

IA_BASE_URL = config.get('ia_base_url')
MARC_LENGTH_POS = 5
logger = logging.getLogger('openlibrary.importapi')

class DataError(ValueError):
    pass

def parse_meta_headers(edition_builder):
    # parse S3-style http headers
    # we don't yet support augmenting complex fields like author or language
    # string_keys = ['title', 'title_prefix', 'description']

    re_meta = re.compile('HTTP_X_ARCHIVE_META(?:\d{2})?_(.*)')
    for k, v in web.ctx.env.items():
        m = re_meta.match(k)
        if m:
            meta_key = m.group(1).lower()
            edition_builder.add(meta_key, v, restrict_keys=False)

def parse_data(data):
    """
    Takes POSTed data and determines the format, and returns an Edition record
    suitable for adding to OL.

    :param str data: Raw data
    :rtype: (dict|None, str|None)
    :return: (Edition record, format (rdf|opds|marcxml|json|marc)) or (None, None)
    """
    data = data.strip()
    if -1 != data[:10].find('<?xml'):
        root = etree.fromstring(data)
        if '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF' == root.tag:
            edition_builder = import_rdf.parse(root)
            format = 'rdf'
        elif '{http://www.w3.org/2005/Atom}entry' == root.tag:
            edition_builder = import_opds.parse(root)
            format = 'opds'
        elif '{http://www.loc.gov/MARC21/slim}record' == root.tag:
            if root.tag == '{http://www.loc.gov/MARC21/slim}collection':
                root = root[0]
            rec = MarcXml(root)
            edition = read_edition(rec)
            edition_builder = import_edition_builder.import_edition_builder(init_dict=edition)
            format = 'marcxml'
        else:
            raise DataError('unrecognized-XML-format')
    elif data.startswith('{') and data.endswith('}'):
        obj = json.loads(data)
        edition_builder = import_edition_builder.import_edition_builder(init_dict=obj)
        format = 'json'
    else:
        #Marc Binary
        if len(data) < MARC_LENGTH_POS or len(data) != int(data[:MARC_LENGTH_POS]):
            raise DataError('no-marc-record')
        rec = MarcBinary(data)

        edition = read_edition(rec)
        edition_builder = import_edition_builder.import_edition_builder(init_dict=edition)
        format = 'marc'

    parse_meta_headers(edition_builder)
    return edition_builder.get_dict(), format


class importapi:
    """/api/import endpoint for general data formats.
    """

    def error(self, error_code, error='Invalid item', **kwargs):
        content = {
            'success': False,
            'error_code': error_code,
            'error': error
        }
        content.update(kwargs)
        raise web.HTTPError('400 Bad Request', data=json.dumps(content))

    def POST(self):
        web.header('Content-Type', 'application/json')
        if not can_write():
            raise web.HTTPError('403 Forbidden')

        data = web.data()

        try:
            edition, format = parse_data(data)
        except DataError as e:
            return self.error(str(e), 'Failed to parse import data')

        if not edition:
            return self.error('unknown_error', 'Failed to parse import data')

        try:
            reply = add_book.load(edition)
        except add_book.RequiredField as e:
            return self.error('missing-required-field', str(e))
        return json.dumps(reply)

    def reject_non_book_marc(self, marc_record, **kwargs):
        details = "Item rejected"
        # Is the item a serial instead of a book?
        marc_leaders = marc_record.leader()
        if marc_leaders[7] == 's':
            return self.error('item-is-serial', details, **kwargs)

        # insider note: follows Archive.org's approach of
        # Item::isMARCXMLforMonograph() which excludes non-books
        if not (marc_leaders[7] == 'm' and marc_leaders[6] == 'a'):
            return self.error('item-not-book', details, **kwargs)


class ia_importapi(importapi):
    """/api/import/ia import endpoint for Archive.org items, requiring an ocaid identifier rather than direct data upload.
    Request Format:

        POST /api/import/ia
        Content-Type: application/json
        Authorization: Basic base64-of-username:password

        {
            "identifier": "<ocaid>",
            "require_marc": "true",
            "bulk_marc": "false"
        }
    """
    def POST(self):
        web.header('Content-Type', 'application/json')

        if not can_write():
            raise web.HTTPError('403 Forbidden')

        i = web.input()

        require_marc = not (i.get('require_marc') == 'false')
        bulk_marc = i.get('bulk_marc') == 'true'

        if 'identifier' not in i:
            return self.error('bad-input', 'identifier not provided')
        identifier = i.identifier

        # First check whether this is a non-book, bulk-marc item
        if bulk_marc:
            # Get binary MARC by identifier = ocaid/filename:offset:length
            re_bulk_identifier = re.compile("([^/]*)/([^:]*):(\d*):(\d*)")
            try:
                ocaid, filename, offset, length = re_bulk_identifier.match(identifier).groups()
                data, next_offset, next_length = get_from_archive_bulk(identifier)
                next_data = {'next_record_offset': next_offset, 'next_record_length': next_length}
                rec = MarcBinary(data)
                edition = read_edition(rec)
            except MarcException as e:
                details = "%s: %s" % (identifier, str(e))
                logger.error("failed to read from bulk MARC record %s", details)
                return self.error('invalid-marc-record', details, **next_data)

            actual_length = int(rec.leader()[:MARC_LENGTH_POS])
            edition['source_records'] = 'marc:%s/%s:%s:%d' % (ocaid, filename, offset, actual_length)

            local_id = i.get('local_id')
            if local_id:
                local_id_type = web.ctx.site.get('/local_ids/' + local_id)
                prefix = local_id_type.urn_prefix
                id_field, id_subfield = local_id_type.id_location.split('$')
                def get_subfield(field, id_subfield):
                    if isinstance(field, str):
                        return field
                    subfields = field[1].get_subfield_values(id_subfield)
                    return subfields[0] if subfields else None
                _ids = [get_subfield(f, id_subfield) for f in rec.read_fields([id_field]) if f and get_subfield(f, id_subfield)]
                edition['local_id'] = ['urn:%s:%s' % (prefix, _id) for _id in _ids]

            # Don't add the book if the MARC record is a non-book item
            self.reject_non_book_marc(rec, **next_data)
            result = add_book.load(edition)

            # Add next_data to the response as location of next record:
            result.update(next_data)
            return json.dumps(result)

        # Case 1 - Is this a valid Archive.org item?
        try:
            item_json = ia.get_item_json(identifier)
            item_server = item_json['server']
            item_path = item_json['dir']
        except KeyError:
            return self.error("invalid-ia-identifier", "%s not found" % identifier)
        metadata = ia.extract_item_metadata(item_json)
        if not metadata:
            return self.error("invalid-ia-identifier")

        # Case 2 - Does the item have an openlibrary field specified?
        # The scan operators search OL before loading the book and add the
        # OL key if a match is found. We can trust them and attach the item
        # to that edition.
        if metadata.get("mediatype") == "texts" and metadata.get("openlibrary"):
            edition_data = self.get_ia_record(metadata)
            edition_data["openlibrary"] = metadata["openlibrary"]
            edition_data = self.populate_edition_data(edition_data, identifier)
            return self.load_book(edition_data)

        # Case 3 - Can the item be loaded into Open Library?
        status = ia.get_item_status(identifier, metadata,
                                    item_server=item_server, item_path=item_path)
        if status != 'ok':
            return self.error(status, "Prohibited Item")

        # Case 4 - Does this item have a marc record?
        marc_record = self.get_marc_record(identifier)
        if marc_record:
            self.reject_non_book_marc(marc_record)
            try:
                edition_data = read_edition(marc_record)
            except MarcException as e:
                logger.error("failed to read from MARC record %s: %s", identifier, str(e))
                return self.error("invalid-marc-record")

        elif require_marc:
            return self.error("no-marc-record")

        else:
            try:
                edition_data = self.get_ia_record(metadata)
            except KeyError:
                return self.error("invalid-ia-metadata")

        # Add IA specific fields: ocaid, source_records, and cover
        edition_data = self.populate_edition_data(edition_data, identifier)

        return self.load_book(edition_data)

    def get_ia_record(self, metadata):
        """
        Generate Edition record from Archive.org metadata, in lieu of a MARC record

        :param dict metadata: metadata retrieved from metadata API
        :rtype: dict
        :return: Edition record
        """
        authors = [{'name': name} for name in metadata.get('creator', '').split(';')]
        description = metadata.get('description')
        isbn = metadata.get('isbn')
        language = metadata.get('language')
        lccn = metadata.get('lccn')
        subject = metadata.get('subject')
        oclc = metadata.get('oclc-id')
        d = {
            'title': metadata.get('title', ''),
            'authors': authors,
            'publish_date': metadata.get('date'),
            'publisher': metadata.get('publisher'),
        }
        if description:
            d['description'] = description
        if isbn:
            d['isbn'] = isbn
        if language and len(language) == 3:
            d['languages'] = [language]
        if lccn:
            d['lccn'] = [lccn]
        if subject:
            d['subjects'] = subject
        if oclc:
            d['oclc'] = oclc
        return d

    def load_book(self, edition_data):
        """
        Takes a well constructed full Edition record and sends it to add_book
        to check whether it is already in the system, and to add it, and a Work
        if they do not already exist.

        :param dict edition_data: Edition record
        :rtype: dict
        """
        result = add_book.load(edition_data)
        return json.dumps(result)

    def populate_edition_data(self, edition, identifier):
        """
        Adds archive.org specific fields to a generic Edition record, based on identifier.

        :param dict edition: Edition record
        :param str identifier: ocaid
        :rtype: dict
        :return: Edition record
        """
        edition['ocaid'] = identifier
        edition['source_records'] = "ia:" + identifier
        edition['cover'] = "{0}/download/{1}/{1}/page/title.jpg".format(IA_BASE_URL, identifier)
        return edition

    def get_marc_record(self, identifier):
        try:
            return get_marc_record_from_ia(identifier)
        except IOError:
            return None

    def find_edition(self, identifier):
        """
        Checks if the given identifier has already been imported into OL.

        :param str identifier: ocaid
        :rtype: str
        :return: OL item key of matching item: '/books/OL..M'
        """
        # match ocaid
        q = {"type": "/type/edition", "ocaid": identifier}
        keys = web.ctx.site.things(q)
        if keys:
            return keys[0]

        # Match source_records
        # When there are multiple scans for the same edition, only source_records is updated.
        q = {"type": "/type/edition", "source_records": "ia:" + identifier}
        keys = web.ctx.site.things(q)
        if keys:
            return keys[0]

    def status_matched(self, key):
        reply = {
            'success': True,
            'edition': {'key': key, 'status': 'matched'}
        }
        return json.dumps(reply)


class ils_search:
    """Search and Import API to use in Koha.

    When a new catalog record is added to Koha, it makes a request with all
    the metadata to find if OL has a matching record. OL returns the OLID of
    the matching record if exists, if not it creates a new record and returns
    the new OLID.

    Request Format:

        POST /api/ils_search
        Content-Type: application/json
        Authorization: Basic base64-of-username:password

        {
            'title': '',
            'authors': ['...','...',...]
            'publisher': '...',
            'publish_year': '...',
            'isbn': [...],
            'lccn': [...],
        }

    Response Format:

        {
            'status': 'found | notfound | created',
            'olid': 'OL12345M',
            'key': '/books/OL12345M',
            'cover': {
                'small': 'https://covers.openlibrary.org/b/12345-S.jpg',
                'medium': 'https://covers.openlibrary.org/b/12345-M.jpg',
                'large': 'https://covers.openlibrary.org/b/12345-L.jpg',
            },
            ...
        }

    When authorization header is not provided and match is not found,
    status='notfound' is returned instead of creating a new record.
    """
    def POST(self):
        try:
            rawdata = json.loads(web.data())
        except ValueError as e:
            raise self.error("Unparseable JSON input \n %s" % web.data())

        # step 1: prepare the data
        data = self.prepare_input_data(rawdata)

        # step 2: search
        matches = self.search(data)

        # step 3: Check auth
        try:
            auth_header = http_basic_auth()
            self.login(auth_header)
        except accounts.ClientException:
            raise self.auth_failed("Invalid credentials")

        # step 4: create if logged in
        keys = []
        if auth_header:
            keys = self.create(matches)

        # step 4: format the result
        d = self.format_result(matches, auth_header, keys)
        return json.dumps(d)

    def error(self, reason):
        d = json.dumps({ "status" : "error", "reason" : reason})
        return web.HTTPError("400 Bad Request", {"Content-Type": "application/json"}, d)


    def auth_failed(self, reason):
        d = json.dumps({ "status" : "error", "reason" : reason})
        return web.HTTPError("401 Authorization Required", {"WWW-Authenticate": 'Basic realm="http://openlibrary.org"', "Content-Type": "application/json"}, d)

    def login(self, authstring):
        if not authstring:
            return
        authstring = authstring.replace("Basic ","")
        username, password = base64.decodestring(authstring).split(':')
        accounts.login(username, password)

    def prepare_input_data(self, rawdata):
        data = dict(rawdata)
        identifiers = rawdata.get('identifiers',{})
        #TODO: Massage single strings here into lists. e.g. {"google" : "123"} into {"google" : ["123"]}.
        for i in ["oclc_numbers", "lccn", "ocaid", "isbn"]:
            if i in data:
                val = data.pop(i)
                if not isinstance(val, list):
                    val = [val]
                identifiers[i] = val
        data['identifiers'] = identifiers

        if "authors" in data:
            authors = data.pop("authors")
            data['authors'] = [{"name" : i} for i in authors]

        return {"doc" : data}

    def search(self, params):
        matches = records.search(params)
        return matches

    def create(self, items):
        return records.create(items)

    def format_result(self, matches, authenticated, keys):
        doc = matches.pop("doc", {})
        if doc and doc['key']:
            doc = web.ctx.site.get(doc['key']).dict()
            # Sanitise for only information that we want to return.
            for i in ["created", "last_modified", "latest_revision", "type", "revision"]:
                doc.pop(i)
            # Main status information
            d = {
                'status': 'found',
                'key': doc['key'],
                'olid': doc['key'].split("/")[-1]
            }
            # Cover information
            covers = doc.get('covers') or []
            if covers and covers[0] > 0:
                d['cover'] = {
                    "small": "https://covers.openlibrary.org/b/id/%s-S.jpg" % covers[0],
                    "medium": "https://covers.openlibrary.org/b/id/%s-M.jpg" % covers[0],
                    "large": "https://covers.openlibrary.org/b/id/%s-L.jpg" % covers[0],
                }

            # Pull out identifiers to top level
            identifiers = doc.pop("identifiers",{})
            for i in identifiers:
                d[i] = identifiers[i]
            d.update(doc)

        else:
            if authenticated:
                d = { 'status': 'created' , 'works' : [], 'authors' : [], 'editions': [] }
                for i in keys:
                    if i.startswith('/books'):
                        d['editions'].append(i)
                    if i.startswith('/works'):
                        d['works'].append(i)
                    if i.startswith('/authors'):
                        d['authors'].append(i)
            else:
                d = {
                    'status': 'notfound'
                    }
        return d

def http_basic_auth():
    auth = web.ctx.env.get('HTTP_AUTHORIZATION')
    return auth and web.lstrips(auth, "")


class ils_cover_upload:
    """Cover Upload API for Koha.

    Request Format: Following input fields with enctype multipart/form-data

        * olid: Key of the edition. e.g. OL12345M
        * file: image file
        * url: URL to image
        * redirect_url: URL to redirect after upload

        Other headers:
           Authorization: Basic base64-of-username:password

    One of file or url can be provided. If the former, the image is
    directly used. If the latter, the image at the URL is fetched and
    used.

    On Success:
          If redirect URL specified,
                redirect to redirect_url?status=ok
          else
                return
                {
                  "status" : "ok"
                }

    On Failure:
          If redirect URL specified,
                redirect to redirect_url?status=error&reason=bad+olid
          else
                return
                {
                  "status" : "error",
                  "reason" : "bad olid"
                }
    """
    def error(self, i, reason):
        if i.redirect_url:
            url = self.build_url(i.redirect_url, status="error", reason=reason)
            return web.seeother(url)
        else:
            d = json.dumps({ "status" : "error", "reason" : reason})
            return web.HTTPError("400 Bad Request", {"Content-Type": "application/json"}, d)


    def success(self, i):
        if i.redirect_url:
            url = self.build_url(i.redirect_url, status="ok")
            return web.seeother(url)
        else:
            d = json.dumps({ "status" : "ok" })
            return web.ok(d, {"Content-type": "application/json"})

    def auth_failed(self, reason):
        d = json.dumps({ "status" : "error", "reason" : reason})
        return web.HTTPError("401 Authorization Required", {"WWW-Authenticate": 'Basic realm="http://openlibrary.org"', "Content-Type": "application/json"}, d)

    def build_url(self, url, **params):
        if '?' in url:
            return url + "&" + urllib.urlencode(params)
        else:
            return url + "?" + urllib.urlencode(params)

    def login(self, authstring):
        if not authstring:
            raise self.auth_failed("No credentials provided")
        authstring = authstring.replace("Basic ","")
        username, password = base64.decodestring(authstring).split(':')
        accounts.login(username, password)

    def POST(self):
        i = web.input(olid=None, file={}, redirect_url=None, url="")

        if not i.olid:
            self.error(i, "olid missing")

        key = '/books/' + i.olid
        book = web.ctx.site.get(key)
        if not book:
            raise self.error(i, "bad olid")

        try:
            auth_header = http_basic_auth()
            self.login(auth_header)
        except accounts.ClientException:
            raise self.auth_failed("Invalid credentials")

        from openlibrary.plugins.upstream import covers
        add_cover = covers.add_cover()

        data = add_cover.upload(key, i)
        coverid = data.get('id')

        if coverid:
            add_cover.save(book, coverid)
            raise self.success(i)
        else:
            raise self.error(i, "upload failed")


add_hook("import", importapi)
add_hook("ils_search", ils_search)
add_hook("ils_cover_upload", ils_cover_upload)
add_hook("import/ia", ia_importapi)
