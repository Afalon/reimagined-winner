from __future__ import print_function

import web
import urllib2
import simplejson
import re
from collections import defaultdict
from isbnlib import canonical

from infogami import config
from infogami.infobase import client
from infogami.utils.view import safeint
from infogami.utils import stats

from openlibrary.core import models, ia
from openlibrary.core.models import Image
from openlibrary.core import lending

from openlibrary.plugins.search.code import SearchProcessor
from openlibrary.plugins.worksearch.code import works_by_author, sorted_work_editions
from openlibrary.utils.isbn import isbn_10_to_isbn_13, isbn_13_to_isbn_10
from openlibrary.utils.solr import Solr

from utils import get_coverstore_url, MultiDict, parse_toc, get_edition_config
import account
import borrow
import logging

import six


def follow_redirect(doc):
    if isinstance(doc, six.string_types) and doc.startswith("/a/"):
        #Some edition records have authors as ["/a/OL1A""] insead of [{"key": "/a/OL1A"}].
        # Hack to fix it temporarily.
        doc = web.ctx.site.get(doc.replace("/a/", "/authors/"))

    if doc and doc.type.key == "/type/redirect":
        key = doc.location
        return web.ctx.site.get(key)
    else:
        return doc

class Edition(models.Edition):

    def get_title(self):
        if self['title_prefix']:
            return self['title_prefix'] + ' ' + self['title']
        else:
            return self['title']

    def get_title_prefix(self):
        return ''

    # let title be title_prefix + title
    title = property(get_title)
    title_prefix = property(get_title_prefix)

    def get_authors(self):
        """Added to provide same interface for work and edition"""
        authors = [follow_redirect(a) for a in self.authors]
        authors = [a for a in authors if a and a.type.key == "/type/author"]
        return authors

    def get_next(self):
        """Next edition of work"""
        if len(self.get('works', [])) != 1:
            return
        wkey = self.works[0].get_olid()
        if not wkey:
            return
        editions = sorted_work_editions(wkey)
        try:
            i = editions.index(self.get_olid())
        except ValueError:
            return
        if i + 1 == len(editions):
            return
        return editions[i + 1]

    def get_prev(self):
        """Previous edition of work"""
        if len(self.get('works', [])) != 1:
            return
        wkey = self.works[0].get_olid()
        if not wkey:
            return
        editions = sorted_work_editions(wkey)
        try:
            i = editions.index(self.get_olid())
        except ValueError:
            return
        if i == 0:
            return
        return editions[i - 1]

    def get_covers(self):
        return [Image(self._site, 'b', c) for c in self.covers if c > 0]

    def get_cover(self):
        covers = self.get_covers()
        return covers and covers[0] or None

    def get_cover_url(self, size):
        cover = self.get_cover()
        if cover:
            return cover.url(size)
        elif self.ocaid:
            return self.get_ia_cover(self.ocaid, size)

    def get_ia_cover(self, itemid, size):
        image_sizes = dict(S=(116, 58), M=(180, 360), L=(500, 500))
        w, h = image_sizes[size.upper()]
        return "https://archive.org/download/%s/page/cover_w%s_h%s.jpg" % (itemid, w, h)

    def get_isbn10(self):
        """Fetches either isbn_10 or isbn_13 from record and returns canonical
        isbn_10
        """
        isbn_10 = self.isbn_10 and canonical(self.isbn_10[0])
        if not isbn_10:
            isbn_13 = self.get_isbn13()
            return isbn_13 and isbn_13_to_isbn_10(isbn_13)
        return isbn_10

    def get_isbn13(self):
        """Fetches either isbn_13 or isbn_10 from record and returns canonical
        isbn_13
        """
        isbn_13 = self.isbn_13 and canonical(self.isbn_13[0])
        if not isbn_13:
            isbn_10 = self.isbn_10 and self.isbn_10[0]
            return isbn_10 and isbn_10_to_isbn_13(isbn_10)
        return isbn_13
    
    def get_identifiers(self):
        """Returns (name, value) pairs of all available identifiers."""
        names = ['ocaid', 'isbn_10', 'isbn_13', 'lccn', 'oclc_numbers']
        return self._process_identifiers(get_edition_config().identifiers, names, self.identifiers)

    def get_ia_meta_fields(self):
        # Check for cached value
        # $$$ we haven't assigned _ia_meta_fields the first time around but there's apparently
        #     some magic that lets us check this way (and breaks using hasattr to check if defined)
        if self._ia_meta_fields:
            return self._ia_meta_fields

        if not self.get('ocaid', None):
            meta = {}
        else:
            meta = ia.get_meta_xml(self.ocaid)
            meta.setdefault("external-identifier", [])
            meta.setdefault("collection", [])

        self._ia_meta_fields = meta
        return self._ia_meta_fields

    def is_daisy_encrypted(self):
        meta_fields = self.get_ia_meta_fields()
        if not meta_fields:
            return
        v = meta_fields['collection']
        return 'printdisabled' in v or 'lendinglibrary' in v

#      def is_lending_library(self):
#         collections = self.get_ia_collections()
#         return 'lendinglibrary' in collections

    def get_lending_resources(self):
        """Returns the loan resource identifiers (in meta.xml format for ACS4 resources) for books hosted on archive.org

        Returns e.g. ['bookreader:lettertoannewarr00west',
                      'acs:epub:urn:uuid:0df6f344-7ce9-4038-885e-e02db34f2891',
                      'acs:pdf:urn:uuid:7f192e62-13f5-4a62-af48-be4bea67e109']
        """

        # The entries in meta.xml look like this:
        # <external-identifier>
        #     acs:epub:urn:uuid:0df6f344-7ce9-4038-885e-e02db34f2891
        # </external-identifier>

        itemid = self.ocaid
        if not itemid:
            return []

        lending_resources = []
        # Check if available for in-browser lending - marked with 'browserlending' collection
        browserLendingCollections = ['browserlending']
        for collection in self.get_ia_meta_fields()['collection']:
            if collection in browserLendingCollections:
                lending_resources.append('bookreader:%s' % self.ocaid)
                break

        lending_resources.extend(self.get_ia_meta_fields()['external-identifier'])

        return lending_resources

    def get_lending_resource_id(self, type):
        if type == 'bookreader':
            desired = 'bookreader:'
        else:
            desired = 'acs:%s:' % type

        for urn in self.get_lending_resources():
            if urn.startswith(desired):
                # Got a match
                # $$$ a little icky - prune the acs:type if present
                if urn.startswith('acs:'):
                    urn = urn[len(desired):]

                return urn

        return None

    def get_current_and_available_loans(self):
        current_loans = borrow.get_edition_loans(self)
        current_and_available_loans = (current_loans, self._get_available_loans(current_loans))
        return current_and_available_loans

    def get_current_loans(self):
        return borrow.get_edition_loans(self)

    def get_available_loans(self):
        """
        Get the resource types currently available to be loaned out for this edition.  Does NOT
        take into account the user's status (e.g. number of books out, in-library status, etc).
        This is like checking if this book is on the shelf.

        Returns [{'resource_id': uuid, 'resource_type': type, 'size': bytes}]

        size may be None"""
        # no ebook
        if not self.ocaid:
            return []

        # already checked out
        if lending.is_loaned_out(self.ocaid):
            return []

        # find available loans. there are no current loans
        return self._get_available_loans([])

    def _get_available_loans(self, current_loans):

        default_type = 'bookreader'

        loans = []

        # Check if we have a possible loan - may not yet be fulfilled in ACS4
        if current_loans:
            # There is a current loan or offer
            return []

        # Create list of possible loan formats
        resource_pattern = r'acs:(\w+):(.*)'
        for resource_urn in self.get_lending_resources():
            if resource_urn.startswith('acs:'):
                (type, resource_id) = re.match(resource_pattern, resource_urn).groups()
                loans.append({
                    'resource_id': resource_id,
                    'resource_type': type,
                    'size': None
                })
            elif resource_urn.startswith('bookreader'):
                loans.append({
                    'resource_id': resource_urn,
                    'resource_type': 'bookreader',
                    'size': None
                })

        # Put default type at start of list, then sort by type name
        def loan_key(loan):
            if loan['resource_type'] == default_type:
                return '1-%s' % loan['resource_type']
            else:
                return '2-%s' % loan['resource_type']
        loans = sorted(loans, key=loan_key)

        # For each possible loan, check if it is available We
        # shouldn't be out of sync (we already checked
        # get_edition_loans for current loans) but we fail safe, for
        # example the book may have been borrowed in a dev instance
        # against the live ACS4 server
        for loan in loans:
            if borrow.is_loaned_out(loan['resource_id']):
                # Only a single loan of an item is allowed
                # $$$ log out of sync state
                return []

        return loans

    def update_loan_status(self):
        """Update the loan status"""
        if self.ocaid:
            lending.sync_loan(self.ocaid)

    def _process_identifiers(self, config, names, values):
        id_map = {}
        for id in config:
            id_map[id.name] = id
            id.setdefault("label", id.name)
            id.setdefault("url_format", None)

        d = MultiDict()

        def process(name, value):
            if value:
                if not isinstance(value, list):
                    value = [value]

                id = id_map.get(name) or web.storage(name=name, label=name, url_format=None)
                for v in value:
                    d[id.name] = web.storage(
                        name=id.name,
                        label=id.label,
                        value=v,
                        url=id.get('url') and id.url.replace('@@@', v))

        for name in names:
            process(name, self[name])

        for name in values:
            process(name, values[name])

        return d

    def set_identifiers(self, identifiers):
        """Updates the edition from identifiers specified as (name, value) pairs."""
        names = ('isbn_10', 'isbn_13', 'lccn', 'oclc_numbers', 'ocaid',
                 'dewey_decimal_class', 'lc_classifications')

        d = {}
        for id in identifiers:
            # ignore bad values
            if 'name' not in id or 'value' not in id:
                continue
            name, value = id['name'], id['value']
            d.setdefault(name, []).append(value)

        # clear existing value first
        for name in names:
           self._getdata().pop(name, None)

        self.identifiers = {}

        for name, value in d.items():
            # ocaid is not a list
            if name == 'ocaid':
                self.ocaid = value[0]
            elif name in names:
                self[name] = value
            else:
                self.identifiers[name] = value

    def get_classifications(self):
        names = ["dewey_decimal_class", "lc_classifications"]
        return self._process_identifiers(get_edition_config().classifications,
                                         names,
                                         self.classifications)

    def set_classifications(self, classifications):
        names = ["dewey_decimal_class", "lc_classifications"]
        d = defaultdict(list)
        for c in classifications:
            if 'name' not in c or 'value' not in c or not web.re_compile("[a-z0-9_]*").match(c['name']):
                continue
            d[c['name']].append(c['value'])

        for name in names:
            self._getdata().pop(name, None)
        self.classifications = {}

        for name, value in d.items():
            if name in names:
                self[name] = value
            else:
                self.classifications[name] = value

    def get_weight(self):
        """returns weight as a storage object with value and units fields."""
        w = self.weight
        return w and UnitParser(["value"]).parse(w)

    def set_weight(self, w):
        self.weight = w and UnitParser(["value"]).format(w)

    def get_physical_dimensions(self):
        d = self.physical_dimensions
        return d and UnitParser(["height", "width", "depth"]).parse(d)

    def set_physical_dimensions(self, d):
        # don't overwrite physical dimensions if nothing was passed in - there
        # may be dimensions in the database that don't conform to the d x d x d format
        if d:
            self.physical_dimensions = UnitParser(["height", "width", "depth"]).format(d)

    def get_toc_text(self):
        def format_row(r):
            return "*" * r.level + " " + " | ".join([r.label, r.title, r.pagenum])

        return "\n".join(format_row(r) for r in self.get_table_of_contents())

    def get_table_of_contents(self):
        def row(r):
            if isinstance(r, six.string_types):
                level = 0
                label = ""
                title = r
                pagenum = ""
            else:
                level = safeint(r.get('level', '0'), 0)
                label = r.get('label', '')
                title = r.get('title', '')
                pagenum = r.get('pagenum', '')

            r = web.storage(level=level, label=label, title=title, pagenum=pagenum)
            return r

        d = [row(r) for r in self.table_of_contents]
        return [row for row in d if any(row.values())]

    def set_toc_text(self, text):
        self.table_of_contents = parse_toc(text)

    def get_links(self):
        links1 = [web.storage(url=url, title=title)
                  for url, title in zip(self.uris, self.uri_descriptions)]
        links2 = list(self.links)
        return links1 + links2

    def get_olid(self):
        return self.key.split('/')[-1]

    @property
    def wp_citation_fields(self):
        """
        Builds a wikipedia citation as defined by http://en.wikipedia.org/wiki/Template:Cite#Citing_books
        """
        result = {
            "title": self.title.replace("[", "&#91").replace("]", "&#93"),
            "publication-date": self.get('publish_date'),
            "ol": str(self.get_olid())[2:]
        }

        if self.ocaid:
            result['url'] = "https://archive.org/details/"+self.ocaid

        if self.lccn:
            result['lccn'] = self.lccn[0]

        if self.issn:
            result['issn'] = self.issn[0]

        if self.get('isbn_10'):
            result['isbn'] = self['isbn_13'][0] if self.get('isbn_13') else self['isbn_10'][0]

        if self.get('oclc_numbers'):
            result['oclc'] = self.oclc_numbers[0]

        if self.works[0].get('first_publish_year'):
            result['origyear'] = self.works[0]['first_publish_year']

        if self.get('publishers'):
            result['publisher'] = self['publishers'][0]

        if self.get('publish_places'):
            result['publication-place'] = self['publish_places'][0]

        authors = [ar.author for ar in self.works[0].authors]
        if len(authors) == 1:
            result['author'] = authors[0].name
        else:
            for i, a in enumerate(authors):
                result['author%s' % (i + 1)] = a.name
        return result

    def is_fake_record(self):
        """Returns True if this is a record is not a real record from database,
        but created on the fly.

        The /books/ia:foo00bar records are not stored in the database, but
        created at runtime using the data from archive.org metadata API.
        """
        return "/ia:" in self.key

class Author(models.Author):
    def get_photos(self):
        return [Image(self._site, "a", id) for id in self.photos if id > 0]

    def get_photo(self):
        photos = self.get_photos()
        return photos and photos[0] or None

    def get_photo_url(self, size):
        photo = self.get_photo()
        return photo and photo.url(size)

    def get_olid(self):
        return self.key.split('/')[-1]

    def get_books(self):
        i = web.input(sort='editions', page=1, rows=20, mode="")
        try:
            # safegaurd from passing zero/negative offsets to solr
            page = max(1, int(i.page))
        except ValueError:
            page = 1
        return works_by_author(self.get_olid(), sort=i.sort,
                               page=page, rows=i.rows,
                               has_fulltext=i.mode=="ebooks")

    def get_work_count(self):
        """Returns the number of works by this author.
        """
        # TODO: avoid duplicate works_by_author calls
        result = works_by_author(self.get_olid(), rows=0)
        return result.num_found

re_year = re.compile(r'(\d{4})$')

def get_solr():
    base_url = "http://%s/solr" % config.plugin_worksearch.get('solr')
    return Solr(base_url)

class Work(models.Work):
    def get_olid(self):
        return self.key.split('/')[-1]

    def get_covers(self, use_solr=True):
        if self.covers:
            return [Image(self._site, "w", id) for id in self.covers if id > 0]
        elif use_solr:
            return self.get_covers_from_solr()
        else:
            return []

    def get_covers_from_solr(self):
        try:
            w = self._solr_data
        except Exception as e:
            logging.getLogger("openlibrary").exception('Unable to retrieve covers from solr')
            return []
        if w:
            if 'cover_id' in w:
                return [Image(self._site, "w", int(w['cover_id']))]
            elif 'cover_edition_key' in w:
                cover_edition = web.ctx.site.get("/books/" + w['cover_edition_key'])
                cover = cover_edition and cover_edition.get_cover()
                if cover:
                    return [cover]
        return []

    def _get_solr_data(self):
        fields = [
            "cover_edition_key", "cover_id", "edition_key", "first_publish_year",
            "has_fulltext", "lending_edition_s", "checked_out", "public_scan_b", "ia"]

        solr = get_solr()
        stats.begin("solr", query={"key": self.key}, fields=fields)
        try:
            d = solr.select({"key": self.key}, fields=fields)
        except Exception as e:
            logging.getLogger("openlibrary").exception("Failed to get solr data")
            return None
        finally:
            stats.end()

        if d.num_found > 0:
            w = d.docs[0]
        else:
            w = None

        # Replace _solr_data property with the attribute
        self.__dict__['_solr_data'] = w
        return w

    _solr_data = property(_get_solr_data)

    def get_cover(self, use_solr=True):
        covers = self.get_covers(use_solr=use_solr)
        return covers and covers[0] or None

    def get_cover_url(self, size, use_solr=True):
        cover = self.get_cover(use_solr=use_solr)
        return cover and cover.url(size)

    def get_author_names(self, blacklist=None):
        author_names = []
        for author in self.get_authors():
            author_name = (author if isinstance(author, six.string_types)
                           else author.name)
            if not blacklist or author_name.lower() not in blacklist:
                author_names.append(author_name)
        return author_names

    def get_authors(self):
        authors =  [a.author for a in self.authors]
        authors = [follow_redirect(a) for a in authors]
        authors = [a for a in authors if a and a.type.key == "/type/author"]
        return authors

    def get_subjects(self):
        """Return subject strings."""
        subjects = self.subjects

        def flip(name):
            if name.count(",") == 1:
                a, b = name.split(",")
                return b.strip() + " " + a.strip()
            return name

        if subjects and not isinstance(subjects[0], six.string_types):
            subjects = [flip(s.name) for s in subjects]
        return subjects

    @staticmethod
    def filter_problematic_subjects(subjects, filter_unicode=True):
        blacklist = ['accessible_book', 'protected_daisy',
                     'in_library', 'overdrive', 'large_type_books',
                     'internet_archive_wishlist', 'fiction',
                     'popular_print_disabled_books',
                     'fiction_in_english', 'open_library_staff_picks',
                     'inlibrary', 'printdisabled', 'browserlending',
                     'biographies', 'open_syllabus_project', 'history',
                     'long_now_manual_for_civilization', 'Popular works']
        blacklist_chars = ['(', ',', '\'', ':', '&', '-', '.']
        ok_subjects = []
        for subject in subjects:
            _subject = subject.lower().replace(' ', '_')
            subject = subject.replace('_', ' ')
            if (_subject not in blacklist and
                (not filter_unicode or (
                    subject.replace(' ', '').isalnum() and 
                    not isinstance(subject, six.text_type))) and
                all([char not in subject for char in blacklist_chars])):
                ok_subjects.append(subject)
        return ok_subjects        

    def get_related_books_subjects(self, filter_unicode=True):
        return self.filter_problematic_subjects(self.get_subjects())

    def get_representative_edition(self):
        """When we have confidence we can direct patrons to the best edition
        of a work (for them), return qualifying edition key. Attempts
        to find best (most available) edition of work using
        archive.org work availability API. May be extended to support language

        :rtype str: infogami edition key or url which resolves to an edition
        """
        work_id = self.key.replace('/works/', '')
        availability = lending.get_work_availability(work_id)
        if work_id in availability:
            if 'openlibrary_edition' in availability[work_id]:
                return '/books/%s' % availability[work_id]['openlibrary_edition']

    def get_sorted_editions(self):
        """
        Get this work's editions sorted by publication year
        :rtype: list[Edition]
        """
        use_solr_data = self._solr_data and \
                        self._solr_data.get('edition_key') and \
                        len(self._solr_data.get('edition_key')) == self.edition_count

        if use_solr_data:
            edition_keys = ["/books/" + olid for olid in self._solr_data.get('edition_key')]
        else:
            db_query = {"type": "/type/edition", "works": self.key, "limit": 10000}
            edition_keys = web.ctx.site.things(db_query)

        editions = web.ctx.site.get_many(edition_keys)
        editions.sort(key=lambda ed: ed.get_publish_year(), reverse=True)

        availability = lending.get_availability_of_ocaids([
            ed.ocaid for ed in editions if ed.ocaid
        ])
        for ed in editions:
            ed.availability = availability.get(ed.ocaid) or {"status": "error"}

        return editions

    def has_ebook(self):
        w = self._solr_data or {}
        return w.get("has_fulltext", False)

    first_publish_year = property(lambda self: self._solr_data.get("first_publish_year"))

    def get_edition_covers(self):
        editions = web.ctx.site.get_many(web.ctx.site.things({"type": "/type/edition", "works": self.key, "limit": 1000}))
        exisiting = set(int(c.id) for c in self.get_covers())
        covers = [e.get_cover() for e in editions]
        return [c for c in covers if c and int(c.id) not in exisiting]

class Subject(client.Thing):
    def _get_solr_result(self):
        if not self._solr_result:
            name = self.name or ""
            q = {'subjects': name, "facets": True}
            self._solr_result = SearchProcessor().search(q)
        return self._solr_result

    def get_related_subjects(self):
        # dummy subjects
        return [web.storage(name='France', key='/subjects/places/France'), web.storage(name='Travel', key='/subjects/Travel')]

    def get_covers(self, offset=0, limit=20):
        editions = self.get_editions(offset, limit)
        olids = [e['key'].split('/')[-1] for e in editions]

        try:
            url = '%s/b/query?cmd=ids&olid=%s' % (get_coverstore_url(), ",".join(olids))
            data = urllib2.urlopen(url).read()
            cover_ids = simplejson.loads(data)
        except IOError as e:
            print('ERROR in getting cover_ids', str(e), file=web.debug)
            cover_ids = {}

        def make_cover(edition):
            edition = dict(edition)
            edition.pop('type', None)
            edition.pop('subjects', None)
            edition.pop('languages', None)

            olid = edition['key'].split('/')[-1]
            if olid in cover_ids:
                edition['cover_id'] = cover_ids[olid]

            return edition

        return [make_cover(e) for e in editions]

    def get_edition_count(self):
        d = self._get_solr_result()
        return d['matches']

    def get_editions(self, offset, limit=20):
        if self._solr_result and offset+limit < len(self._solr_result):
            result = self._solr_result[offset:offset+limit]
        else:
            name = self.name or ""
            result = SearchProcessor().search({"subjects": name, 'offset': offset, 'limit': limit})
        return result['docs']

    def get_author_count(self):
        d = self._get_solr_result()
        return len(d['facets']['authors'])

    def get_authors(self):
        d = self._get_solr_result()
        return [web.storage(name=a, key='/authors/OL1A', count=count) for a, count in d['facets']['authors']]

    def get_publishers(self):
        d = self._get_solr_result()
        return [web.storage(name=p, count=count) for p, count in d['facets']['publishers']]


class SubjectPlace(Subject):
    pass


class SubjectPerson(Subject):
    pass


class User(models.User):

    def get_name(self):
        return self.displayname or self.key.split('/')[-1]
    name = property(get_name)

    def get_edit_history(self, limit=10, offset=0):
        return web.ctx.site.versions({"author": self.key, "limit": limit, "offset": offset})

    def get_users_settings(self):
        settings = web.ctx.site.get('%s/preferences' % self.key)
        return settings.dict().get('notifications') if settings else {}

    def get_creation_info(self):
        if web.ctx.path.startswith("/admin"):
            d = web.ctx.site.versions({'key': self.key, "sort": "-created", "limit": 1})[0]
            return web.storage({"ip": d.ip, "member_since": d.created})

    def get_edit_count(self):
        if web.ctx.path.startswith("/admin"):
            return web.ctx.site._request('/count_edits_by_user', data={"key": self.key})
        else:
            return 0

    def get_loan_count(self):
        return len(borrow.get_loans(self))

    def get_loans(self):
        self.update_loan_status()
        return lending.get_loans_of_user(self.key)

    def update_loan_status(self):
        """Update the status of this user's loans."""
        loans = lending.get_loans_of_user(self.key)
        for loan in loans:
            lending.sync_loan(loan['ocaid'])

class UnitParser:
    """Parsers values like dimentions and weight.

        >>> p = UnitParser(["height", "width", "depth"])
        >>> p.parse("9 x 3 x 2 inches")
        <Storage {'units': 'inches', 'width': '3', 'depth': '2', 'height': '9'}>
        >>> p.format({"height": "9", "width": 3, "depth": 2, "units": "inches"})
        '9 x 3 x 2 inches'
    """
    def __init__(self, fields):
        self.fields = fields

    def format(self, d):
        return " x ".join(str(d.get(k, '')) for k in self.fields) + ' ' + d.get('units', '')

    def parse(self, s):
        """Parse the string and return storage object with specified fields and units."""
        pattern = "^" + " *x *".join("([0-9.]*)" for f in self.fields) + " *(.*)$"
        rx = web.re_compile(pattern)
        m = rx.match(s)
        return m and web.storage(zip(self.fields + ["units"], m.groups()))

class Changeset(client.Changeset):
    def can_undo(self):
        return False

    def _get_doc(self, key, revision):
        if revision == 0:
            return {
                "key": key,
                "type": {"key": "/type/delete"}
            }
        else:
            d = web.ctx.site.get(key, revision).dict()
            return d

    def process_docs_before_undo(self, docs):
        """Hook to process docs before saving for undo.

        This is called by _undo method to allow subclasses to check
        for validity or redirects so that undo doesn't fail.

        The subclasses may overwrite this as required.
        """
        return docs

    def _undo(self):
        """Undo this transaction."""
        docs = [self._get_doc(c['key'], c['revision']-1) for c in self.changes]
        docs = self.process_docs_before_undo(docs)

        data = {
            "parent_changeset": self.id
        }
        comment = 'undo ' + self.comment
        return web.ctx.site.save_many(docs, action="undo", data=data, comment=comment)

    def get_undo_changeset(self):
        """Returns the changeset that undone this transaction if one exists, None otherwise.
        """
        try:
            return self._undo_changeset
        except AttributeError:
            pass

        changesets = web.ctx.site.recentchanges({
            "kind": "undo",
            "data": {
                "parent_changeset": self.id
            }
        })
        # return the first undo changeset
        self._undo_changeset = changesets and changesets[-1] or None
        return self._undo_changeset

class NewAccountChangeset(Changeset):
    def get_user(self):
        keys = [c.key for c in self.get_changes()]
        user_key = "/people/" + keys[0].split("/")[2]
        return web.ctx.site.get(user_key)

class MergeAuthors(Changeset):
    def can_undo(self):
        return self.get_undo_changeset() is None

    def get_master(self):
        master = self.data.get("master")
        return master and web.ctx.site.get(master, lazy=True)

    def get_duplicates(self):
        duplicates = self.data.get("duplicates")
        changes = dict((c['key'], c['revision']) for c in self.changes)

        return duplicates and [web.ctx.site.get(key, revision=changes[key]-1, lazy=True) for key in duplicates if key in changes]

class Undo(Changeset):
    def can_undo(self):
        return False

    def get_undo_of(self):
        undo_of = self.data['undo_of']
        return web.ctx.site.get_change(undo_of)

    def get_parent_changeset(self):
        parent = self.data['parent_changeset']
        return web.ctx.site.get_change(parent)

class AddBookChangeset(Changeset):
    def get_work(self):
        book = self.get_edition()
        return (book and book.works and book.works[0]) or None

    def get_edition(self):
        for doc in self.get_changes():
            if doc.key.startswith("/books/"):
                return doc

    def get_author(self):
        for doc in self.get_changes():
            if doc.key.startswith("/authors/"):
                return doc

class ListChangeset(Changeset):
    def get_added_seed(self):
        added = self.data.get("add")
        if added and len(added) == 1:
            return self.get_seed(added[0])

    def get_removed_seed(self):
        removed = self.data.get("remove")
        if removed and len(removed) == 1:
            return self.get_seed(removed[0])

    def get_list(self):
        return self.get_changes()[0]

    def get_seed(self, seed):
        """Returns the seed object."""
        if isinstance(seed, dict):
            seed = self._site.get(seed['key'])
        return models.Seed(self.get_list(), seed)

def setup():
    models.register_models()

    client.register_thing_class('/type/edition', Edition)
    client.register_thing_class('/type/author', Author)
    client.register_thing_class('/type/work', Work)

    client.register_thing_class('/type/subject', Subject)
    client.register_thing_class('/type/place', SubjectPlace)
    client.register_thing_class('/type/person', SubjectPerson)
    client.register_thing_class('/type/user', User)

    client.register_changeset_class(None, Changeset) # set the default class
    client.register_changeset_class('merge-authors', MergeAuthors)
    client.register_changeset_class('undo', Undo)

    client.register_changeset_class('add-book', AddBookChangeset)
    client.register_changeset_class('lists', ListChangeset)
    client.register_changeset_class('new-account', NewAccountChangeset)
