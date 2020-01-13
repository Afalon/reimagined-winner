"""Controller for home page.
"""
import random
import web
import simplejson
import logging

from infogami.utils import delegate
from infogami.utils.view import render_template, public
from infogami.infobase.client import storify
from infogami import config

from openlibrary import accounts
from openlibrary.core import admin, cache, ia, inlibrary, lending, \
    helpers as h
from openlibrary.core.sponsorships import get_sponsorable_editions
from openlibrary.utils import dateutil
from openlibrary.plugins.upstream import borrow
from openlibrary.plugins.upstream.utils import get_blog_feeds
from openlibrary.plugins.worksearch import search, subjects
from openlibrary.plugins.openlibrary import lists


import six


logger = logging.getLogger("openlibrary.home")

CAROUSELS_PRESETS = {
    'preset:thrillers': '(creator:"Clancy, Tom" OR creator:"King, Stephen" OR creator:"Clive Cussler" OR creator:("Cussler, Clive") OR creator:("Dean Koontz") OR creator:("Koontz, Dean") OR creator:("Higgins, Jack")) AND !publisher:"Pleasantville, N.Y. : Reader\'s Digest Association" AND languageSorter:"English"',
    'preset:comics': '(subject:"comics" OR creator:("Gary Larson") OR creator:("Larson, Gary") OR creator:("Charles M Schulz") OR creator:("Schulz, Charles M") OR creator:("Jim Davis") OR creator:("Davis, Jim") OR creator:("Bill Watterson") OR creator:("Watterson, Bill") OR creator:("Lee, Stan"))',
    'preset:authorsalliance_mitpress': '(openlibrary_subject:(authorsalliance) OR collection:(mitpress) OR publisher:(MIT Press) OR openlibrary_subject:(mitpress)) AND (!loans__status__status:UNAVAILABLE)'
}


def get_homepage():
    if 'env' not in web.ctx:
        delegate.fakeload()
    try:
        stats = admin.get_stats()
    except Exception:
        logger.error("Error in getting stats", exc_info=True)
        stats = None
    blog_posts = get_blog_feeds()

    # render tempalte should be setting ctx.bodyid
    # but because get_homepage is cached, this doesn't happen
    # during subsequent called
    page = render_template(
        "home/index", stats=stats,
        blog_posts=blog_posts
    )
    page.v2 = True    
    return dict(page)

def get_cached_homepage():
    five_minutes = 5 * dateutil.MINUTE_SECS
    return cache.memcache_memoize(
        get_homepage, "home.homepage", timeout=five_minutes)()

class home(delegate.page):
    path = "/"

    def is_enabled(self):
        return "lending_v2" in web.ctx.features

    def GET(self):
        cached_homepage = get_cached_homepage()
        # when homepage is cached, home/index.html template
        # doesn't run ctx.setdefault to set the bodyid so we must do so here:
        web.template.Template.globals['ctx']['bodyid'] = 'home'
        return web.template.TemplateResult(cached_homepage)

class random_book(delegate.page):
    path = "/random"

    def GET(self):
        olid = lending.get_random_available_ia_edition()
        if olid:
            raise web.seeother('/books/%s' % olid)
        raise web.seeother("/")


def get_ia_carousel_books(query=None, subject=None, work_id=None, sorts=None,
                          _type=None, limit=None):
    if 'env' not in web.ctx:
        delegate.fakeload()

    elif query in CAROUSELS_PRESETS:
        query = CAROUSELS_PRESETS[query]

    limit = limit or lending.DEFAULT_IA_RESULTS
    books = lending.get_available(limit=limit, subject=subject, work_id=work_id,
                                  _type=_type, sorts=sorts, query=query)
    formatted_books = [format_book_data(book) for book in books if book != 'error']
    return formatted_books

def get_featured_subjects():
    # web.ctx must be initialized as it won't be available to the background thread.
    if 'env' not in web.ctx:
        delegate.fakeload()

    FEATURED_SUBJECTS = [
        'art', 'science_fiction', 'fantasy', 'biographies', 'recipes',
        'romance', 'textbooks', 'children', 'history', 'medicine', 'religion',
        'mystery_and_detective_stories', 'plays', 'music', 'science'
    ]
    return dict([(subject_name, subjects.get_subject('/subjects/' + subject_name, sort='edition_count'))
                 for subject_name in FEATURED_SUBJECTS])


def get_cachable_sponsorable_editions():
    if 'env' not in web.ctx:
        delegate.fakeload()

    return [format_book_data(ed) for ed in get_sponsorable_editions()]

@public
def get_cached_sponsorable_editions():
    return storify(cache.memcache_memoize(
        get_cachable_sponsorable_editions, "books.sponsorable_editions",
        timeout=dateutil.HOUR_SECS)())

@public
def get_cached_featured_subjects():
    return cache.memcache_memoize(
        get_featured_subjects, "home.featured_subjects", timeout=dateutil.HOUR_SECS)()

@public
def generic_carousel(query=None, subject=None, work_id=None, _type=None,
                     sorts=None, limit=None, timeout=None):
    memcache_key = 'home.ia_carousel_books'
    cached_ia_carousel_books = cache.memcache_memoize(
        get_ia_carousel_books, memcache_key, timeout=timeout or cache.DEFAULT_CACHE_LIFETIME)
    books = cached_ia_carousel_books(
        query=query, subject=subject, work_id=work_id, _type=_type,
        sorts=sorts, limit=limit)
    if not books:
        books = cached_ia_carousel_books.update(
            query=query, subject=subject, work_id=work_id, _type=_type,
            sorts=sorts, limit=limit)[0]
    return storify(books) if books else books

@public
def readonline_carousel():
    """Return template code for books pulled from search engine.
       TODO: If problems, use stock list.
    """
    try:
        data = random_ebooks()
        if len(data) > 60:
            data = random.sample(data, 60)
        return storify(data)

    except Exception:
        logger.error("Failed to compute data for readonline_carousel", exc_info=True)
        return None

def random_ebooks(limit=2000):
    solr = search.get_solr()
    sort = "edition_count desc"
    result = solr.select(
        query='has_fulltext:true -public_scan_b:false',
        rows=limit,
        sort=sort,
        fields=[
            'has_fulltext',
            'key',
            'ia',
            "title",
            "cover_edition_key",
            "author_key", "author_name",
        ])

    return [format_work_data(doc) for doc in result.get('docs', []) if doc.get('ia')]

# cache the results of random_ebooks in memcache for 15 minutes
random_ebooks = cache.memcache_memoize(random_ebooks, "home.random_ebooks", timeout=15*60)

def format_list_editions(key):
    """Formats the editions of a list suitable for display in carousel.
    """
    if 'env' not in web.ctx:
        delegate.fakeload()

    seed_list = web.ctx.site.get(key)
    if not seed_list:
        return []

    editions = {}
    for seed in seed_list.seeds:
        if not isinstance(seed, six.string_types):
            if seed.type.key == "/type/edition":
                editions[seed.key] = seed
            else:
                try:
                    e = pick_best_edition(seed)
                except StopIteration:
                    continue
                editions[e.key] = e
    return [format_book_data(e) for e in editions.values()]

# cache the results of format_list_editions in memcache for 5 minutes
format_list_editions = cache.memcache_memoize(format_list_editions, "home.format_list_editions", timeout=5*60)

def pick_best_edition(work):
    return (e for e in work.editions if e.ocaid).next()

def format_work_data(work):
    d = dict(work)

    key = work.get('key', '')
    # New solr stores the key as /works/OLxxxW
    if not key.startswith("/works/"):
        key = "/works/" + key

    d['url'] = key
    d['title'] = work.get('title', '')

    if 'author_key' in work and 'author_name' in work:
        d['authors'] = [{"key": key, "name": name} for key, name in
                        zip(work['author_key'], work['author_name'])]

    if 'cover_edition_key' in work:
        d['cover_url'] = h.get_coverstore_url() + "/b/olid/%s-M.jpg" % work['cover_edition_key']

    d['read_url'] = "//archive.org/stream/" + work['ia'][0]
    return d

def format_book_data(book):
    d = web.storage()
    d.key = book.get('key')
    d.url = book.url()
    d.title = book.title or None
    d.ocaid = book.get("ocaid")
    d.eligibility = book.get("eligibility", {})

    def get_authors(doc):
        return [web.storage(key=a.key, name=a.name or None) for a in doc.get_authors()]

    work = book.works and book.works[0]
    d.authors = get_authors(work if work else book)
    cover = work.get_cover() if work and work.get_cover() else book.get_cover()

    if cover:
        d.cover_url = cover.url("M")
    elif d.ocaid:
        d.cover_url = 'https://archive.org/services/img/%s' % d.ocaid

    if d.ocaid:
        collections = ia.get_meta_xml(d.ocaid).get("collection", [])

        if 'lendinglibrary' in collections or 'inlibrary' in collections:
            d.borrow_url = book.url("/borrow")
        else:
            d.read_url = book.url("/borrow")
    return d

def setup():
    pass
