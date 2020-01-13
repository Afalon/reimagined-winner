"""Handlers for adding and editing books."""

import web
import urllib
import urllib2
import simplejson
from collections import defaultdict
from StringIO import StringIO
import csv
import datetime

from infogami import config
from infogami.core import code as core
from infogami.core.db import ValidationException
from infogami.utils import delegate
from infogami.utils.view import safeint, add_flash_message
from infogami.infobase.client import ClientException

from openlibrary.plugins.openlibrary.processors import urlsafe
from openlibrary.utils import is_author_olid, is_work_olid
from openlibrary.utils.solr import Solr
from openlibrary.i18n import gettext as _
from openlibrary import accounts
import logging

import utils
from utils import render_template, fuzzy_find

from account import as_admin
from openlibrary.plugins.recaptcha import recaptcha
from . import spamcheck

import six


logger = logging.getLogger("openlibrary.book")

SYSTEM_SUBJECTS = ["Accessible Book", "Lending Library", "In Library", "Protected DAISY"]


def get_solr():
    base_url = "http://%s/solr" % config.plugin_worksearch.get('solr')
    return Solr(base_url)


def get_recaptcha():
    def recaptcha_exempt():
        """Check to see if account is an admin, or more than two years old."""
        user = web.ctx.site.get_user()
        if user and (user.is_admin() or user.is_librarian()):
            return True
        account = user and user.get_account()
        if not account:
            return False
        create_dt = account.creation_time()
        now_dt = datetime.datetime.utcnow()
        delta = now_dt - create_dt
        return delta.days > 30

    def is_plugin_enabled(name):
        plugin_names = delegate.get_plugins()
        return name in plugin_names or "openlibrary.plugins." + name in plugin_names

    if is_plugin_enabled('recaptcha') and not recaptcha_exempt():
        public_key = config.plugin_recaptcha.public_key
        private_key = config.plugin_recaptcha.private_key
        return recaptcha.Recaptcha(public_key, private_key)
    else:
        return None


def make_work(doc):
    w = web.storage(doc)
    w.key = "/works/" + w.key

    def make_author(key, name):
        key = "/authors/" + key
        return web.ctx.site.new(key, {
            "key": key,
            "type": {"key": "/type/author"},
            "name": name
        })

    w.authors = [make_author(key, name) for key, name in zip(doc['author_key'], doc['author_name'])]
    w.cover_url="/images/icons/avatar_book-sm.png"

    w.setdefault('ia', [])
    w.setdefault('first_publish_year', None)
    return w


def new_doc(type_, **data):
    """
    Create an new OL doc item.
    :param str type_: object type e.g. /type/edition
    :rtype: doc
    :return: the newly created document
    """
    key = web.ctx.site.new_key(type_)
    data['key'] = key
    data['type'] = {"key": type_}
    return web.ctx.site.new(key, data)


class DocSaveHelper:
    """Simple utility to collect the saves and save them together at the end."""
    def __init__(self):
        self.docs = []

    def save(self, doc):
        """Adds the doc to the list of docs to be saved."""
        if not isinstance(doc, dict): # thing
            doc = doc.dict()
        self.docs.append(doc)

    def commit(self, **kw):
        """Saves all the collected docs."""
        if self.docs:
            web.ctx.site.save_many(self.docs, **kw)


class addbook(delegate.page):
    path = "/books/add"

    def GET(self):
        """Main user interface for adding a book to Open Library."""

        if not self.has_permission():
            return render_template("permission_denied", "/books/add", "Permission denied to add a book to Open Library.")

        i = web.input(work=None, author=None)
        work = i.work and web.ctx.site.get(i.work)
        author = i.author and web.ctx.site.get(i.author)

        return render_template('books/add', work=work, author=author, recaptcha=get_recaptcha())

    def has_permission(self):
        """
        Can a book be added?
        :rtype: bool
        """
        return web.ctx.site.can_write("/books/add")

    def POST(self):
        i = web.input(title="", author_name="", author_key="", publisher="", publish_date="", id_name="", id_value="", _test="false")

        if spamcheck.is_spam(i):
            return render_template("message.html",
                "Oops",
                'Something went wrong. Please try again later.')

        if not web.ctx.site.get_user():
            recap = get_recaptcha()
            if recap and not recap.validate():
                return render_template('message.html',
                    'Recaptcha solution was incorrect',
                    'Please <a href="javascript:history.back()">go back</a> and try again.'
                )

        match = self.find_matches(i)

        saveutil = DocSaveHelper()

        if i.author_key == '__new__':
            if i._test != 'true':
                a = new_doc('/type/author', name=i.author_name)
                comment = utils.get_message('comment_new_author')
                # Save, but don't commit, new author.
                # It will be committed when the Edition is created below.
                saveutil.save(a)
                i.author_key = a.key
            # since new author is created it must be a new record
            match = None

        if i._test == 'true' and not isinstance(match, list):
            if match:
                return 'Matched <a href="%s">%s</a>' % (match.key, match.key)
            else:
                return 'No match found'

        if isinstance(match, list):
            # multiple matches
            return render_template('books/check', i, match)

        elif match and match.key.startswith('/books'):
            # work match and edition match, match is an Edition
            return self.work_edition_match(match)

        elif match and match.key.startswith('/works'):
            # work match but not edition
            work = match
            return self.work_match(saveutil, work, i)
        else:
            # no match
            return self.no_match(saveutil, i)

    def find_matches(self, i):
        """
        Tries to find an edition, or work, or multiple work candidates that match the given input data.

        Case#1: No match. None is returned.
        Case#2: Work match but not edition. Work is returned.
        Case#3: Work match and edition match. Edition is returned
        Case#4: Multiple work match. List of works is returned.

        :param web.utils.Storage i: addbook user supplied formdata
        :rtype: None or list or Work or Edition
        :return: None or Work or Edition or list of Works that are likely matches.
        """

        i.publish_year = i.publish_date and self.extract_year(i.publish_date)

        # work is set from the templates/books/check.html page.
        work_key = i.get('work')

        # work_key is set to none-of-these when user selects none-of-these link.
        if work_key == 'none-of-these':
            return None  # Case 1, from check page

        work = work_key and web.ctx.site.get(work_key)
        if work:
            edition = self.try_edition_match(work=work,
                publisher=i.publisher, publish_year=i.publish_year,
                id_name=i.id_name, id_value=i.id_value)
            return edition or work  # Case 3 or 2, from check page

        edition = self.try_edition_match(
            title=i.title,
            author_key=i.author_key,
            publisher=i.publisher,
            publish_year=i.publish_year,
            id_name=i.id_name,
            id_value=i.id_value)

        if edition:
            return edition  # Case 2 or 3 or 4, from add page

        solr = get_solr()
        author_key = i.author_key and i.author_key.split("/")[-1]
        # Less exact solr search than try_edition_match(), search by supplied title and author only.
        result = solr.select({'title': i.title, 'author_key': author_key}, doc_wrapper=make_work, q_op="AND")

        if result.num_found == 0:
            return None  # Case 1, from add page
        elif result.num_found == 1:
            return result.docs[0]  # Case 2
        else:
            return result.docs  # Case 4

    def extract_year(self, value):
        """
        Extract just the 4 digit year from a date string.

        :param str value: A freeform string representing a publication date.
        :rtype: str
        :return: a four digit year
        """
        m = web.re_compile(r"(\d\d\d\d)").search(value)
        return m and m.group(1)

    def try_edition_match(self,
        work=None, title=None, author_key=None,
        publisher=None, publish_year=None, id_name=None, id_value=None):
        """
        Searches solr for potential edition matches.

        :param str work: work key e.g. /works/OL1234W
        :param str title:
        :param str author_key: e.g. /author/OL1234A
        :param str publisher:
        :param str publish_year: yyyy
        :param str id_name: from list of values in mapping below
        :param str id_value:
        :rtype: None or Edition or list
        :return: None, an Edition, or a list of Works
        """
        # insufficient data
        if not publisher and not publish_year and not id_value:
            return

        q = {}
        work and q.setdefault('key', work.key.split("/")[-1])
        title and q.setdefault('title', title)
        author_key and q.setdefault('author_key', author_key.split('/')[-1])
        publisher and q.setdefault('publisher', publisher)
        # There are some errors indexing of publish_year. Use publish_date until it is fixed
        publish_year and q.setdefault('publish_date', publish_year)

        mapping = {
            'isbn_10': 'isbn',
            'isbn_13': 'isbn',
            'lccn': 'lccn',
            'oclc_numbers': 'oclc',
            'ocaid': 'ia'
        }
        if id_value and id_name in mapping:
            if id_name.startswith('isbn'):
                id_value = id_value.replace('-', '')
            q[mapping[id_name]] = id_value

        solr = get_solr()
        result = solr.select(q, doc_wrapper=make_work, q_op="AND")

        if len(result.docs) > 1:
            # found multiple work matches
            return result.docs
        elif len(result.docs) == 1:
            # found one work match
            work = result.docs[0]
            publisher = publisher and fuzzy_find(publisher, work.publisher,
                                                 stopwords=("publisher", "publishers", "and"))

            editions = web.ctx.site.get_many(["/books/" + key for key in work.edition_key])
            for e in editions:
                d = {}
                if publisher:
                    if not e.publishers or e.publishers[0] != publisher:
                        continue
                if publish_year:
                    if not e.publish_date or publish_year != self.extract_year(e.publish_date):
                        continue
                if id_value and id_name in mapping:
                    if not id_name in e or id_value not in e[id_name]:
                        continue
                # return the first good likely matching Edition
                return e

    def work_match(self, saveutil, work, i):
        """
        Action for when a work, but not edition, is matched.
        Saves a new edition of work, created form the formdata i.
        Redirects the user to the newly created edition page in edit
        mode to add more details.

        :param DocSaveHelper saveutil:
        :param Work work: the matched work for this book
        :param web.utils.Storage i: user supplied book formdata
        :rtype: None
        """
        edition = self._make_edition(work, i)

        saveutil.save(edition)
        comment = utils.get_message("comment_add_book")
        saveutil.commit(comment=comment, action="add-book")

        raise web.seeother(edition.url("/edit?mode=add-book"))

    def work_edition_match(self, edition):
        """
        Action for when an exact work and edition match have been found.
        Redirect user to the found item's edit page to add any missing details.
        :param Edition edition:
        """
        raise web.seeother(edition.url("/edit?mode=found"))

    def no_match(self, saveutil, i):
        """
        Action to take when no matches are found.
        Creates and saves both a Work and Edition.
        Redirects the user to the work/edition edit page
        in `add-work` mode.

        :param DocSaveHelper saveutil:
        :param web.utils.Storage i:
        :rtype: None
        """
        # Any new author has been created and added to
        # saveutil, and author_key added to i
        work = new_doc("/type/work",
            title=i.title,
            authors=[{"author": {"key": i.author_key}}]
        )

        edition = self._make_edition(work, i)

        saveutil.save(work)
        saveutil.save(edition)

        comment = utils.get_message("comment_add_book")
        saveutil.commit(action="add-book", comment=comment)

        raise web.seeother(edition.url("/edit?mode=add-work"))

    def _make_edition(self, work, i):
        """
        Uses formdata 'i' to create (but not save) an edition
        of 'work'.

        :param Work work:
        :param web.utils.Storage i:
        :rtype: Edition
        :return:
        """
        edition = new_doc("/type/edition",
            works=[{"key": work.key}],
            title=i.title,
            publishers=[i.publisher],
            publish_date=i.publish_date,
        )
        if i.get("id_name") and i.get("id_value"):
            edition.set_identifiers([dict(name=i.id_name, value=i.id_value)])
        return edition


# remove existing definitions of addbook and addauthor
delegate.pages.pop('/addbook', None)
delegate.pages.pop('/addauthor', None)


class addbook(delegate.page):
    def GET(self):
        raise web.redirect("/books/add")


class addauthor(delegate.page):
    def GET(self):
        raise web.redirect("/authors")


def trim_value(value):
    """Trim strings, lists and dictionaries to remove empty/None values.

        >>> trim_value("hello ")
        'hello'
        >>> trim_value("")
        >>> trim_value([1, 2, ""])
        [1, 2]
        >>> trim_value({'x': 'a', 'y': ''})
        {'x': 'a'}
        >>> trim_value({'x': [""]})
        None
    """
    if isinstance(value, six.string_types):
        value = value.strip()
        return value or None
    elif isinstance(value, list):
        value = [v2 for v in value
                    for v2 in [trim_value(v)]
                    if v2 is not None]
        return value or None
    elif isinstance(value, dict):
        value = dict((k, v2) for k, v in value.items()
                             for v2 in [trim_value(v)]
                             if v2 is not None)
        return value or None
    else:
        return value


def trim_doc(doc):
    """Replace empty values in the document with Nones.
    """
    return web.storage((k, trim_value(v)) for k, v in doc.items() if k[:1] not in "_{")


class SaveBookHelper:
    """Helper to save edition and work using the form data coming from edition edit and work edit pages.

    This does the required trimming and processing of input data before saving.
    """
    def __init__(self, work, edition):
        """
        :param openlibrary.plugins.upstream.models.Work|None work: None if editing an orphan edition
        :param openlibrary.plugins.upstream.models.Edition|None edition: None if just editing work
        """
        self.work = work
        self.edition = edition

    def save(self, formdata):
        """
        Update work and edition documents according to the specified formdata.
        :param web.storage formdata:
        :rtype: None
        """
        comment = formdata.pop('_comment', '')

        user = accounts.get_current_user()
        delete = user and user.is_admin() and formdata.pop('_delete', '')

        formdata = utils.unflatten(formdata)
        work_data, edition_data = self.process_input(formdata)

        self.process_new_fields(formdata)

        saveutil = DocSaveHelper()

        if delete:
            if self.edition:
                self.delete(self.edition.key, comment=comment)

            if self.work and self.work.edition_count == 0:
                self.delete(self.work.key, comment=comment)
            return

        just_editing_work = edition_data is None
        if work_data:
            # Create any new authors that were added
            for i, author in enumerate(work_data.get("authors") or []):
                if author['author']['key'] == "__new__":
                    a = self.new_author(formdata['authors'][i])
                    author['author']['key'] = a.key
                    saveutil.save(a)

            if not just_editing_work:
                # Handle orphaned editions
                new_work_key = (edition_data.get('works') or [{'key': None}])[0]['key']
                if self.work is None and (new_work_key is None or new_work_key == '__new__'):
                    # i.e. not moving to another work, create empty work
                    self.work = self.new_work(self.edition)
                    edition_data.works = [{'key': self.work.key}]
                    work_data.key = self.work.key
                elif self.work is not None and new_work_key is None:
                    # we're trying to create an orphan; let's not do that
                    edition_data.works = [{'key': self.work.key}]

            if self.work is not None:
                self.work.update(work_data)
                saveutil.save(self.work)

        if self.edition and edition_data:
            # Create a new work if so desired
            new_work_key = (edition_data.get('works') or [{'key': None}])[0]['key']
            if new_work_key == "__new__" and self.work is not None:
                self.work = self.new_work(self.edition)
                edition_data.works = [{'key': self.work.key}]
                saveutil.save(self.work)

            identifiers = edition_data.pop('identifiers', [])
            self.edition.set_identifiers(identifiers)

            classifications = edition_data.pop('classifications', [])
            self.edition.set_classifications(classifications)

            self.edition.set_physical_dimensions(edition_data.pop('physical_dimensions', None))
            self.edition.set_weight(edition_data.pop('weight', None))
            self.edition.set_toc_text(edition_data.pop('table_of_contents', ''))

            if edition_data.pop('translation', None) != 'yes':
                edition_data.translation_of = None
                edition_data.translated_from = None

            self.edition.update(edition_data)
            saveutil.save(self.edition)

        saveutil.commit(comment=comment, action="edit-book")

    @staticmethod
    def new_work(edition):
        """
        :param openlibrary.plugins.upstream.models.Edition edition:
        :rtype: openlibrary.plugins.upstream.models.Work
        """
        work_key = web.ctx.site.new_key('/type/work')
        work = web.ctx.site.new(work_key, {
            'key': work_key,
            'title': edition.get('title'),
            'subtitle': edition.get('subtitle'),
            'type': {'key': '/type/work'},
            'covers': edition.get('covers', []),
        })
        return work

    @staticmethod
    def new_author(name):
        """
        :param str name:
        :rtype: openlibrary.plugins.upstream.models.Author
        """
        key = web.ctx.site.new_key("/type/author")
        return web.ctx.site.new(key, {
            "key": key,
            "type": {"key": "/type/author"},
            "name": name
        })

    @staticmethod
    def delete(key, comment=""):
        doc = web.ctx.site.new(key, {
            "key": key,
            "type": {"key": "/type/delete"}
        })
        doc._save(comment=comment)

    def process_new_fields(self, formdata):
        def f(name):
            val = formdata.get(name)
            return val and simplejson.loads(val)

        new_roles = f('select-role-json')
        new_ids = f('select-id-json')
        new_classifications = f('select-classification-json')

        if new_roles or new_ids or new_classifications:
            edition_config = web.ctx.site.get('/config/edition')

            #TODO: take care of duplicate names

            if new_roles:
                edition_config.roles += [d.get('value') or '' for d in new_roles]

            if new_ids:
                edition_config.identifiers += [{
                        "name": d.get('value') or '',
                        "label": d.get('label') or '',
                        "website": d.get("website") or '',
                        "notes": d.get("notes") or ''}
                    for d in new_ids]

            if new_classifications:
                edition_config.classifications += [{
                        "name": d.get('value') or '',
                        "label": d.get('label') or '',
                        "website": d.get("website") or '',
                        "notes": d.get("notes") or ''}
                    for d in new_classifications]

            as_admin(edition_config._save)("add new fields")

    def process_input(self, i):
        if 'edition' in i:
            edition = self.process_edition(i.edition)
        else:
            edition = None

        if 'work' in i and self.use_work_edits(i):
            work = self.process_work(i.work)
        else:
            work = None

        return work, edition

    def process_edition(self, edition):
        """Process input data for edition."""
        edition.publishers = edition.get('publishers', '').split(';')
        edition.publish_places = edition.get('publish_places', '').split(';')
        edition.distributors = edition.get('distributors', '').split(';')

        edition = trim_doc(edition)

        if edition.get('physical_dimensions') and edition.physical_dimensions.keys() == ['units']:
            edition.physical_dimensions = None

        if edition.get('weight') and edition.weight.keys() == ['units']:
            edition.weight = None

        for k in ['roles', 'identifiers', 'classifications']:
            edition[k] = edition.get(k) or []

        self._prevent_ocaid_deletion(edition)
        return edition

    def process_work(self, work):
        """
        Process input data for work.
        :param web.storage work: form data work info
        :rtype: web.storage
        """
        def read_subject(subjects):
            if not subjects:
                return

            f = StringIO(subjects.encode('utf-8')) # no unicode in csv module
            dedup = set()
            for s in csv.reader(f, dialect='excel', skipinitialspace=True).next():
                s = s.decode('utf-8')
                if s.lower() not in dedup:
                    yield s
                    dedup.add(s.lower())

        work.subjects = list(read_subject(work.get('subjects', '')))
        work.subject_places = list(read_subject(work.get('subject_places', '')))
        work.subject_times = list(read_subject(work.get('subject_times', '')))
        work.subject_people = list(read_subject(work.get('subject_people', '')))
        if ': ' in work.get('title', ''):
            work.title, work.subtitle = work.title.split(': ', 1)
        else:
            work.subtitle = None

        for k in ('excerpts', 'links'):
            work[k] = work.get(k) or []

        # ignore empty authors
        work.authors = [a for a in work.get('authors', []) if a.get('author', {}).get('key', '').strip()]

        self._prevent_system_subjects_deletion(work)
        return trim_doc(work)

    def _prevent_system_subjects_deletion(self, work):
        # Allow admins to modify system systems
        user = accounts.get_current_user()
        if user and user.is_admin():
            return

        # Note: work is the new work object from the formdata and self.work is the work doc from the database.
        old_subjects = self.work and self.work.get("subjects") or []

        # If condition is added to handle the possibility of bad data
        set_old_subjects = set(s.lower() for s in old_subjects if isinstance(s, six.string_types))
        set_new_subjects = set(s.lower() for s in work.subjects)

        for s in SYSTEM_SUBJECTS:
            # if a system subject has been removed
            if s.lower() in set_old_subjects and s.lower() not in set_new_subjects:
                work_key = self.work and self.work.key
                logger.warn("Prevented removal of system subject %r from %s.", s, work_key)
                work.subjects.append(s)

    def _prevent_ocaid_deletion(self, edition):
        # Allow admins to modify ocaid
        user = accounts.get_current_user()
        if user and user.is_admin():
            return

        # read ocaid from form data
        try:
            ocaid = [id['value'] for id in edition.get('identifiers', []) if id['name'] == 'ocaid'][0]
        except IndexError:
            ocaid = None

        # 'self.edition' is the edition doc from the db and 'edition' is the doc from formdata
        if self.edition and self.edition.get('ocaid') and self.edition.get('ocaid') != ocaid:
            logger.warn("Attempt to change ocaid of %s from %r to %r.", self.edition.key, self.edition.get('ocaid'), ocaid)
            raise ValidationException("Changing Internet Archive ID is not allowed.")

    @staticmethod
    def use_work_edits(formdata):
        """
        Check if the form data's work OLID matches the form data's edition's work OLID.
        If they don't, then we ignore the work edits.
        :param web.storage formdata: form data (parsed into a nested dict)
        :rtype: bool
        """
        if 'edition' not in formdata:
            # No edition data -> just editing work, so work data matters
            return True

        has_edition_work = 'works' in formdata.edition and \
                           formdata.edition.works and \
                           formdata.edition.works[0].key

        if has_edition_work:
            old_work_key = formdata.work.key
            new_work_key = formdata.edition.works[0].key
            return old_work_key == new_work_key
        else:
            # i.e. editing an orphan; so we care about the work
            return True


class book_edit(delegate.page):
    path = "(/books/OL\d+M)/edit"

    def GET(self, key):
        i = web.input(v=None)
        v = i.v and safeint(i.v, None)

        if not web.ctx.site.can_write(key):
            return render_template("permission_denied", web.ctx.fullpath, "Permission denied to edit " + key + ".")

        edition = web.ctx.site.get(key, v)
        if edition is None:
            raise web.notfound()

        work = edition.works and edition.works[0]

        if not work:
            # HACK: create dummy work when work is not available
            work = web.ctx.site.new('', {
                'key': '',
                'type': {'key': '/type/work'},
                'title': edition.title,
                'authors': [{'type': {'key': '/type/author_role'}, 'author': {'key': a['key']}} for a in edition.get('authors', [])],
                'subjects': edition.get('subjects', []),
            })

        return render_template('books/edit', work, edition, recaptcha=get_recaptcha())

    def POST(self, key):
        i = web.input(v=None, _method="GET")

        if spamcheck.is_spam():
            return render_template("message.html",
                "Oops",
                'Something went wrong. Please try again later.')

        recap = get_recaptcha()
        if recap and not recap.validate():
            return render_template("message.html",
                'Recaptcha solution was incorrect',
                'Please <a href="javascript:history.back()">go back</a> and try again.'
            )
        v = i.v and safeint(i.v, None)
        edition = web.ctx.site.get(key, v)

        if edition is None:
            raise web.notfound()
        if edition.works:
            work = edition.works[0]
        else:
            work = None

        add = (edition.revision == 1 and work and work.revision == 1 and work.edition_count == 1)

        try:
            helper = SaveBookHelper(work, edition)
            helper.save(web.input())

            if add:
                add_flash_message("info", utils.get_message("flash_book_added"))
            else:
                add_flash_message("info", utils.get_message("flash_book_updated"))

            raise web.seeother(edition.url())
        except ClientException as e:
            add_flash_message('error', e.message or e.json)
            return self.GET(key)
        except ValidationException as e:
            add_flash_message('error', str(e))
            return self.GET(key)


class work_edit(delegate.page):
    path = "(/works/OL\d+W)/edit"

    def GET(self, key):
        i = web.input(v=None, _method="GET")
        v = i.v and safeint(i.v, None)

        if not web.ctx.site.can_write(key):
            return render_template("permission_denied", web.ctx.fullpath, "Permission denied to edit " + key + ".")

        work = web.ctx.site.get(key, v)
        if work is None:
            raise web.notfound()

        return render_template('books/edit', work, recaptcha=get_recaptcha())

    def POST(self, key):
        i = web.input(v=None, _method="GET")

        if spamcheck.is_spam():
            return render_template("message.html",
                "Oops",
                'Something went wrong. Please try again later.')

        recap = get_recaptcha()

        if recap and not recap.validate():
            return render_template("message.html",
                'Recaptcha solution was incorrect',
                'Please <a href="javascript:history.back()">go back</a> and try again.'
            )

        v = i.v and safeint(i.v, None)
        work = web.ctx.site.get(key, v)
        if work is None:
            raise web.notfound()

        try:
            helper = SaveBookHelper(work, None)
            helper.save(web.input())
            add_flash_message("info", utils.get_message("flash_work_updated"))
            raise web.seeother(work.url())
        except (ClientException, ValidationException) as e:
            add_flash_message('error', str(e))
            return self.GET(key)


class author_edit(delegate.page):
    path = "(/authors/OL\d+A)/edit"

    def GET(self, key):
        if not web.ctx.site.can_write(key):
            return render_template("permission_denied", web.ctx.fullpath, "Permission denied to edit " + key + ".")

        author = web.ctx.site.get(key)
        if author is None:
            raise web.notfound()
        return render_template("type/author/edit", author)

    def POST(self, key):
        author = web.ctx.site.get(key)
        if author is None:
            raise web.notfound()

        i = web.input(_comment=None)
        formdata = self.process_input(i)
        try:
            if not formdata:
                raise web.badrequest()
            elif "_save" in i:
                author.update(formdata)
                author._save(comment=i._comment)
                raise web.seeother(key)
            elif "_delete" in i:
                author = web.ctx.site.new(key, {"key": key, "type": {"key": "/type/delete"}})
                author._save(comment=i._comment)
                raise web.seeother(key)
        except (ClientException, ValidationException) as e:
            add_flash_message('error', str(e))
            author.update(formdata)
            author['comment_'] = i._comment
            return render_template("type/author/edit", author)

    def process_input(self, i):
        i = utils.unflatten(i)
        if 'author' in i:
            author = trim_doc(i.author)
            alternate_names = author.get('alternate_names', None) or ''
            author.alternate_names = [name.strip() for name in alternate_names.replace("\n", ";").split(';') if name.strip()]
            author.links = author.get('links') or []
            return author


class edit(core.edit):
    """Overwrite ?m=edit behaviour for author, book and work pages."""
    def GET(self, key):
        page = web.ctx.site.get(key)

        if web.re_compile('/(authors|books|works)/OL.*').match(key):
            if page is None:
                raise web.seeother(key)
            else:
                raise web.seeother(page.url(suffix="/edit"))
        else:
            return core.edit.GET(self, key)


class daisy(delegate.page):
    path = "(/books/.*)/daisy"

    def GET(self, key):
        page = web.ctx.site.get(key)

        if not page:
            raise web.notfound()

        return render_template("books/daisy", page)


def to_json(d):
    web.header('Content-Type', 'application/json')
    return delegate.RawText(simplejson.dumps(d))


class languages_autocomplete(delegate.page):
    path = "/languages/_autocomplete"

    def GET(self):
        i = web.input(q="", limit=5)
        i.limit = safeint(i.limit, 5)

        languages = [lang for lang in utils.get_languages() if lang.name.lower().startswith(i.q.lower())]
        return to_json(languages[:i.limit])

class works_autocomplete(delegate.page):
    path = "/works/_autocomplete"

    def GET(self):
        i = web.input(q="", limit=5)
        i.limit = safeint(i.limit, 5)

        solr = get_solr()

        q = solr.escape(i.q).strip()
        if is_work_olid(q.upper()):
            # ensure uppercase; key is case sensitive in solr
            solr_q = 'key:"/works/%s"' % q.upper()
        else:
            solr_q = 'title:"%s"^2 OR title:(%s*)' % (q, q)

        params = {
            'q_op': 'AND',
            'sort': 'edition_count desc',
            'rows': i.limit,
            'fq': 'type:work',
            # limit the fields returned for better performance
            'fl': 'key,title,subtitle,cover_i,first_publish_year,author_name,edition_count'
        }

        data = solr.select(solr_q, **params)
        # exclude fake works that actually have an edition key
        docs = [d for d in data['docs'] if d['key'][-1] == 'W']

        for d in docs:
            # Required by the frontend
            d['name'] = d['key'].split('/')[-1]
            d['full_title'] = d['title']
            if 'subtitle' in d:
                d['full_title'] += ": " + d['subtitle']
        return to_json(docs)

class authors_autocomplete(delegate.page):
    path = "/authors/_autocomplete"

    def GET(self):
        i = web.input(q="", limit=5)
        i.limit = safeint(i.limit, 5)

        solr = get_solr()

        q = solr.escape(i.q).strip()
        solr_q = ''
        if is_author_olid(q.upper()):
            # ensure uppercase; key is case sensitive in solr
            solr_q = 'key:"/authors/%s"' % q.upper()
        else:
            prefix_q = q + "*"
            solr_q = 'name:(%s) OR alternate_names:(%s)' % (prefix_q, prefix_q)

        params = {
            'q_op': 'AND',
            'sort': 'work_count desc',
            'rows': i.limit,
            'fq': 'type:author'
        }

        data = solr.select(solr_q, **params)
        docs = data['docs']

        for d in docs:
            if 'top_work' in d:
                d['works'] = [d.pop('top_work')]
            else:
                d['works'] = []
            d['subjects'] = d.pop('top_subjects', [])

        return to_json(docs)


class work_identifiers(delegate.view):
    suffix = "identifiers"
    types = ["/type/edition"]

    def POST(self, edition):
        saveutil = DocSaveHelper()
        i = web.input(isbn = "")
        isbn = i.get("isbn")
        # Need to do some simple validation here. Perhaps just check if it's a number?
        if len(isbn) == 10:
            typ = "ISBN 10"
            data = [{'name': u'isbn_10', 'value': isbn}]
        elif len(isbn) == 13:
            typ = "ISBN 13"
            data = [{'name': u'isbn_13', 'value': isbn}]
        else:
            add_flash_message("error", "The ISBN number you entered was not valid")
            raise web.redirect(web.ctx.path)
        if edition.works:
            work = edition.works[0]
        else:
            work = None
        edition.set_identifiers(data)
        saveutil.save(edition)
        saveutil.commit(comment="Added an %s identifier."%typ, action="edit-book")
        add_flash_message("info", "Thank you very much for improving that record!")
        raise web.redirect(web.ctx.path)


def setup():
    """Do required setup."""
    pass
