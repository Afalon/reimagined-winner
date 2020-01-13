"""Handlers for borrowing books"""

import copy
import datetime
import time
import hmac
import re
import simplejson
import urllib
import urllib2
import logging

import web

from infogami import config
from infogami.utils import delegate
from infogami.utils.view import public
from infogami.infobase.utils import parse_datetime

from utils import render_template

from openlibrary.core import stats
from openlibrary.core import msgbroker
from openlibrary.core import lending
from openlibrary.core import waitinglist
from openlibrary.core import ab
from openlibrary.accounts.model import OpenLibraryAccount
from openlibrary import accounts
from openlibrary.utils import dateutil

from lxml import etree

import acs4

logger = logging.getLogger("openlibrary.borrow")

########## Constants

lending_library_subject = u'Lending library'
in_library_subject = u'In library'
lending_subjects = set([lending_library_subject, in_library_subject])
loanstatus_url = config.get('loanstatus_url')

# ACS4 resource ids start with 'urn:uuid:'.  The meta.xml on archive.org
# adds 'acs:epub:' or 'acs:pdf:' to distinguish the file type.
acs_resource_id_prefixes = ['urn:uuid:', 'acs:epub:', 'acs:pdf:']

# Max loans a user can have at once
user_max_loans = 5

# When we generate a loan offer (.acsm) for a user we assume that the loan has occurred.
# Once the loan fulfillment inside Digital Editions the book status server will know
# the loan has occurred.  We allow this timeout so that we don't delete the OL loan
# record before fulfillment because we can't find it in the book status server.
# $$$ If a user borrows an ACS4 book and immediately returns book loan will show as
#     "not yet downloaded" for the duration of the timeout.
#     BookReader loan status is always current.
loan_fulfillment_timeout_seconds = 60*5

# How long bookreader loans should last
bookreader_loan_seconds = 60*60*24*14

# How long the auth token given to the BookReader should last.  After the auth token
# expires the BookReader will not be able to access the book.  The BookReader polls
# OL periodically to get fresh tokens.
BOOKREADER_AUTH_SECONDS = dateutil.MINUTE_SECS * 10
READER_AUTH_SECONDS = dateutil.MINUTE_SECS * 2

# Base URL for BookReader
try:
    bookreader_host = config.bookreader_host
except AttributeError:
    bookreader_host = 'archive.org'

bookreader_stream_base = 'https://' + bookreader_host + '/stream'

########## Page Handlers

# Handler for /books/{bookid}/{title}/borrow
class checkout_with_ocaid(delegate.page):

    path = "/borrow/ia/(.*)"

    def GET(self, ocaid):
        """Redirect shim: Translate an IA identifier into an OL identifier and
        then redirects user to the canonical OL borrow page.
        """
        i = web.input()
        params = urllib.urlencode(i)
        ia_edition = web.ctx.site.get('/books/ia:%s' % ocaid)
        edition = web.ctx.site.get(ia_edition.location)
        url = '%s/x/borrow' % (edition.key)
        raise web.seeother(url + '?' + params)

    def POST(self, ocaid):
        """Redirect shim: Translate an IA identifier into an OL identifier and
        then forwards a borrow request to the canonical borrow
        endpoint with this OL identifier.
        """
        ia_edition = web.ctx.site.get('/books/ia:%s' % ocaid)
        borrow().POST(ia_edition.location)

# Handler for /books/{bookid}/{title}/borrow
class borrow(delegate.page):
    path = "(/books/.*)/borrow"

    def GET(self, key):
        return self.POST(key)

    def POST(self, key):
        """Called when the user wants to borrow the edition"""

        i = web.input(action='borrow', format=None, ol_host=None, _autoReadAloud=None, q="")

        if i.ol_host:
            ol_host = i.ol_host
        else:
            ol_host = 'openlibrary.org'

        edition = web.ctx.site.get(key)
        if not edition:
            raise web.notfound()

        # Make a call to availability v2 update the subjects according
        # to result if `open`, redirect to bookreader
        response = lending.get_availability_of_ocaid(edition.ocaid)
        availability = response[edition.ocaid] if response else {}
        archive_url = 'https://archive.org/stream/' + edition.ocaid + '?ref=ol'
        if i._autoReadAloud is not None:
            archive_url += '&_autoReadAloud=show'

        if i.q:
            _q = urllib.quote(i.q, safe='')
            archive_url += "#page/-/mode/2up/search/%s" % _q

        if availability and availability['status'] == 'open':
            raise web.seeother(archive_url)

        error_redirect = (archive_url)
        user = accounts.get_current_user()

        if user:
            account = OpenLibraryAccount.get_by_email(user.email)
            ia_itemname = account.itemname if account else None
        if not user or not ia_itemname:
            web.setcookie(config.login_cookie_name, "", expires=-1)
            redirect_url = "/account/login?redirect=%s/borrow?action=%s" % (edition.url(), i.action)
            if i._autoReadAloud is not None:
                redirect_url += '&_autoReadAloud=' + i._autoReadAloud
            raise web.seeother(redirect_url)

        action = i.action

        # Intercept a 'borrow' action if the user has already
        # borrowed the book and convert to a 'read' action.
        # Added so that direct bookreader links being routed through
        # here can use a single action of 'borrow', regardless of
        # whether the book has been checked out or not.
        if action == 'borrow' and user.has_borrowed(edition):
            action = 'read'

        bookPath = '/stream/' + edition.ocaid
        if i._autoReadAloud is not None:
            bookPath += '?_autoReadAloud=show'

        if action == 'borrow':
            resource_type = i.format or 'bookreader'

            if resource_type not in ['epub', 'pdf', 'bookreader']:
                raise web.seeother(error_redirect)

            user_meets_borrow_criteria = user_can_borrow_edition(user, edition, resource_type)

            if user_meets_borrow_criteria:
                # This must be called before the loan is initiated,
                # otherwise the user's waitlist status will be cleared
                # upon loan creation
                track_loan = False if is_users_turn_to_borrow(user, edition) else True

                loan = lending.create_loan(
                    identifier=edition.ocaid,
                    resource_type=resource_type,
                    user_key=ia_itemname,
                    book_key=key)

                if loan:
                    loan_link = loan['loan_link']
                    if resource_type == 'bookreader':
                        if track_loan:
                            # As of 2017-12-14, Petabox will be
                            # responsible for tracking borrows which
                            # are the result of waitlist redemptions,
                            # so we don't want to track them here to
                            # avoid double accounting. When a reader
                            # is at the head of a waitlist and goes to
                            # claim their loan, Petabox now checks
                            # whether the waitlist was initiated from
                            # OL, and if it was, petabox tracks
                            # ol.loans.bookreader accordingly via
                            # lending.create_loan.
                            stats.increment('ol.loans.bookreader')

                        raise web.seeother(make_bookreader_auth_link(
                            loan.get_key(), edition.ocaid,
                            bookPath, ol_host,
                            ia_userid=ia_itemname))
                    elif resource_type == 'pdf':
                        stats.increment('ol.loans.pdf')
                        raise web.seeother(loan_link)
                    elif resource_type == 'epub':
                        stats.increment('ol.loans.epub')
                        raise web.seeother(loan_link)
                else:
                    raise web.seeother(error_redirect)
            else:
                raise web.seeother(error_redirect)

        elif action == 'return':
            # Check that this user has the loan
            user.update_loan_status()
            loans = get_loans(user)

            # We pick the first loan that the user has for this book that is returnable.
            # Assumes a user can't borrow multiple formats (resource_type) of the same book.
            user_loan = None
            for loan in loans:
                # Handle the case of multiple edition records for the same
                # ocaid and the user borrowed from one and returning from another
                has_loan = (loan['book'] == edition.key or loan['ocaid'] == edition.ocaid)
                if has_loan:
                    user_loan = loan
                    break

            if not user_loan:
                # $$$ add error message
                raise web.seeother(error_redirect)

            user_loan.return_loan()

            # Show the page with "you've returned this". Use a dummy slug.
            # $$$ this would do better in a session variable that can be cleared
            #     after the message is shown once
            raise web.seeother(edition.url())

        elif action == 'read':
            # Look for loans for this book
            user.update_loan_status()
            loans = get_loans(user)
            for loan in loans:
                if loan['book'] == edition.key:
                    raise web.seeother(make_bookreader_auth_link(
                        loan['_key'], edition.ocaid, bookPath,
                        ol_host, ia_userid=ia_itemname
                    ))
        elif action == 'join-waitinglist':
            return self.POST_join_waitinglist(edition, user)
        elif action == 'leave-waitinglist':
            return self.POST_leave_waitinglist(edition, user, i)

        # Action not recognized
        raise web.seeother(error_redirect)

    def POST_join_waitinglist(self, edition, user):
        waitinglist.join_waitinglist(user.key, edition.key)
        stats.increment('ol.loans.joinWaitlist')
        raise web.redirect(edition.url())

    def POST_leave_waitinglist(self, edition, user, i):
        waitinglist.leave_waitinglist(user.key, edition.key)
        stats.increment('ol.loans.leaveWaitlist')
        if i.get("redirect"):
            raise web.redirect(i.redirect)
        else:
            raise web.redirect(edition.url())

# Handler for /books/{bookid}/{title}/_borrow_status
class borrow_status(delegate.page):
    path = "(/books/.*)/_borrow_status"

    def GET(self, key):
        global lending_subjects

        i = web.input(callback=None)

        edition = web.ctx.site.get(key)

        if not edition:
            raise web.notfound()

        edition.update_loan_status()
        available_formats = [loan['resource_type'] for loan in edition.get_available_loans()]
        loan_available = len(available_formats) > 0
        subjects = set([])

        for work in edition.get('works', []):
            for subject in work.get_subjects():
                if subject in lending_subjects:
                    subjects.add(subject)

        output = {
                'id' : key,
                'loan_available': loan_available,
                'available_formats': available_formats,
                'lending_subjects': [lending_subject for lending_subject in subjects]
        }

        output_text = simplejson.dumps( output )

        content_type = "application/json"
        if i.callback:
            content_type = "text/javascript"
            output_text = '%s ( %s );' % (i.callback, output_text)

        return delegate.RawText(output_text, content_type=content_type)


class borrow_admin(delegate.page):
    path = "(/books/.*)/borrow_admin"

    def GET(self, key):
        if not is_admin():
            return render_template('permission_denied', web.ctx.path, "Permission denied.")

        edition = web.ctx.site.get(key)
        if not edition:
            raise web.notfound()

        if edition.ocaid:
            lending.sync_loan(edition.ocaid)
            ebook_key = "ebooks/" + edition.ocaid
            ebook = web.ctx.site.store.get(ebook_key) or {}
        else:
            ebook = None

        edition_loans = get_edition_loans(edition)

        user_loans = []
        user = accounts.get_current_user()
        if user:
            user_loans = get_loans(user)

        return render_template("borrow_admin", edition, edition_loans, ebook, user_loans, web.ctx.ip)

    def POST(self, key):
        if not is_admin():
            return render_template('permission_denied', web.ctx.path, "Permission denied.")

        edition = web.ctx.site.get(key)
        if not edition:
            raise web.notfound()
        if not edition.ocaid:
            raise web.seeother(edition.url("/borrow_admin"))

        lending.sync_loan(edition.ocaid)
        i = web.input(action=None, loan_key=None)

        if i.action == 'delete' and i.loan_key:
            loan = lending.get_loan(edition.ocaid)
            if loan and loan['_key'] == i.loan_key:
                loan.delete()
        elif i.action == 'update_loan_info':
            waitinglist.update_waitinglist(edition.ocaid)
        raise web.seeother(web.ctx.path + '/borrow_admin')

class borrow_admin_no_update(delegate.page):
    path = "(/books/.*)/borrow_admin_no_update"

    def GET(self, key):
        if not is_admin():
            return render_template('permission_denied', web.ctx.path, "Permission denied.")

        edition = web.ctx.site.get(key)

        if not edition:
            raise web.notfound()

        edition_loans = get_edition_loans(edition)

        user_loans = []
        user = accounts.get_current_user()
        if user:
            user_loans = get_loans(user)

        return render_template("borrow_admin_no_update", edition, edition_loans, user_loans, web.ctx.ip)

    def POST(self, key):
        if not is_admin():
            return render_template('permission_denied', web.ctx.path, "Permission denied.")

        i = web.input(action=None, loan_key=None)

        if i.action == 'delete' and i.loan_key:
            delete_loan(i.loan_key)

        raise web.seeother(web.ctx.path) # $$$ why doesn't this redirect to borrow_admin_no_update?

class ia_loan_status(delegate.page):
    path = r"/ia_loan_status/(.*)"

    def GET(self, itemid):
        d = get_borrow_status(itemid, include_resources=False, include_ia=False)
        return delegate.RawText(simplejson.dumps(d), content_type="application/json")

@public
def get_borrow_status(itemid, include_resources=True, include_ia=True, edition=None):
    """Returns borrow status for each of the sources and formats.

    If the optinal argument editions is provided, it uses that edition instead
    of finding edition from itemid. This is added for performance reasons.
    """
    loan = lending.get_loan(itemid)
    has_loan = bool(loan)

    if edition:
        editions = [edition]
    else:
        edition_keys = web.ctx.site.things({"type": "/type/edition", "ocaid": itemid})
        editions = web.ctx.site.get_many(edition_keys)
    has_waitinglist = editions and any(e.get_waitinglist_size() > 0 for e in editions)

    d = {
        'identifier': itemid,
        'checkedout': has_loan or has_waitinglist,
        'has_loan': has_loan,
        'has_waitinglist': has_waitinglist,
    }
    if include_ia:
        ia_checkedout = lending.is_loaned_out_on_ia(itemid)
        d['checkedout'] = d['checkedout'] or ia_checkedout
        d['checkedout_on_ia'] = ia_checkedout

    if include_resources:
        d.update({
            'resource_bookreader': 'absent',
            'resource_pdf': 'absent',
            'resource_epub': 'absent',
        })
        if editions:
            resources = editions[0].get_lending_resources()
            resource_pattern = r'acs:(\w+):(.*)'
            for resource_urn in resources:
                if resource_urn.startswith('acs:'):
                    (resource_type, resource_id) = re.match(resource_pattern, resource_urn).groups()
                else:
                    resource_type, resource_id = "bookreader", resource_urn
                resource_type = "resource_" + resource_type
                if is_loaned_out(resource_id):
                    d[resource_type] = 'checkedout'
                else:
                    d[resource_type] = 'available'
    return web.storage(d)

# Handler for /iauth/{itemid}
class ia_auth(delegate.page):
    path = r"/ia_auth/(.*)"

    def GET(self, item_id):
        i = web.input(_method='GET', callback=None, loan=None, token=None)

        resource_id = 'bookreader:%s' % item_id
        content_type = "application/json"

        # check that identifier is valid

        user = accounts.get_current_user()
        auth_json = simplejson.dumps(
            get_ia_auth_dict(user, item_id, i.loan, i.token))

        output = auth_json

        if i.callback:
            content_type = "text/javascript"
            output = '%s ( %s );' % (i.callback, output)

        return delegate.RawText(output, content_type=content_type)


# Handler for /borrow/receive_notification - receive ACS4 status update notifications
class borrow_receive_notification(delegate.page):
    path = r"/borrow/receive_notification"

    def GET(self):
        web.header('Content-Type', 'application/json')
        output = simplejson.dumps({'success': False, 'error': 'Only POST is supported'})
        return delegate.RawText(output, content_type='application/json')

    def POST(self):
        data = web.data()
        try:
            notify_xml = etree.fromstring(data)

            # XXX verify signature?  Should be acs4 function...
            notify_obj = acs4.el_to_o(notify_xml)

            output = simplejson.dumps({'success':True})
        except Exception as e:
            output = simplejson.dumps({'success':False, 'error': str(e)})
        return delegate.RawText(output, content_type='application/json')


class ia_borrow_notify(delegate.page):
    """Invoked by archive.org to notify about change in loan/waiting list
    status of an item.

    The payload will be of the following format:

        {"identifier": "foo00bar"}
    """
    path = "/borrow/notify"

    def POST(self):
        payload = web.data()
        d = simplejson.loads(payload)
        identifier = d and d.get('identifier')
        if identifier:
            lending.sync_loan(identifier)
            waitinglist.on_waitinglist_update(identifier)

########## Public Functions

@public
def can_borrow(edition):
    return edition.can_borrow()

@public
def is_loan_available(edition, type):
    resource_id = edition.get_lending_resource_id(type)

    if not resource_id:
        return False

    return not is_loaned_out(resource_id)

@public
def datetime_from_isoformat(expiry):
    """Returns datetime object, or None"""
    if expiry is None:
        return None
    return parse_datetime(expiry)

@public
def datetime_from_utc_timestamp(seconds):
    return datetime.datetime.utcfromtimestamp(seconds)

@public
def can_return_resource_type(resource_type):
    """Returns true if this resource can be returned from the OL site."""
    if resource_type.startswith('bookreader'):
        return True
    return False

@public
def ia_identifier_is_valid(item_id):
    """Returns false if the item id is obviously malformed. Not currently checking length."""
    if re.match(r'^[a-zA-Z0-9][a-zA-Z0-9\.\-_]*$', item_id):
        return True
    return False

@public
def get_bookreader_stream_url(itemid):
    return bookreader_stream_base + '/' + itemid

@public
def get_bookreader_host():
    return bookreader_host



########## Helper Functions

def get_all_store_values(**query):
    """Get all values by paging through all results. Note: adds store_key with the row id."""
    query = copy.deepcopy(query)
    if 'limit' not in query:
        query['limit'] = 500
    query['offset'] = 0
    values = []
    got_all = False

    while not got_all:
        #new_values = web.ctx.site.store.values(**query)
        new_items = web.ctx.site.store.items(**query)
        for new_item in new_items:
            new_item[1].update({'store_key': new_item[0]})
            # XXX-Anand: Handling the existing loans
            new_item[1].setdefault("ocaid", None)
            values.append(new_item[1])
        if len(new_items) < query['limit']:
            got_all = True
        query['offset'] += len(new_items)
    return values

def get_all_loans():
    # return web.ctx.site.store.values(type='/type/loan')
    return get_all_store_values(type='/type/loan')

def get_loans(user):
    return lending.get_loans_of_user(user.key)

def get_edition_loans(edition):
    if edition.ocaid:
        loan = lending.get_loan(edition.ocaid)
        if loan:
            return [loan]
    return []

def get_loan_link(edition, type):
    """Get the loan link, which may be an ACS4 link or BookReader link depending on the loan type"""
    resource_id = edition.get_lending_resource_id(type)

    if type == 'bookreader':
        # link to bookreader
        return (resource_id, get_bookreader_stream_url(edition.ocaid))

    raise Exception('Unknown resource type %s for loan of edition %s', edition.key, type)


def get_loan_key(resource_id):
    """Get the key for the loan associated with the resource_id"""
    # Find loan in OL
    loan_keys = web.ctx.site.store.query('/type/loan', 'resource_id', resource_id)
    if not loan_keys:
        # No local records
        return None

    # Only support single loan of resource at the moment
    if len(loan_keys) > 1:
        #raise Exception('Found too many local loan records for resource %s' % resource_id)
        logger.error("Found too many loan records for resource %s: %s", resource_id, loan_keys)

    loan_key = loan_keys[0]['key']
    return loan_key

def get_loan_status(resource_id):
    """Should only be used for ACS4 loans.  Get the status of the loan from the ACS4 server,
       via the Book Status Server (BSS)

    Typical BSS response for ACS4 looks like this:
        [
        {
            "loanuntil": "2010-06-25T00:52:04",
            "resourceid": "a8b600e2-32fd-4aeb-a2b5-641103583254",
            "returned": "F",
            "until": "2010-06-25T00:52:04"
        }
    ]
    """
    global loanstatus_url

    if not loanstatus_url:
        raise Exception('No loanstatus_url -- cannot check loan status')

    url = '%s/is_loaned_out/%s' % (loanstatus_url, resource_id)
    try:
        response = simplejson.loads(urllib2.urlopen(url).read())
        if len(response) == 0:
            # No outstanding loans
            return None

        else:
            return response[0]

    except IOError:
        # status server is down
        # $$$ be more graceful
        #raise Exception('Loan status server not available - tried at %s', url)

        # XXX-Anand: don't crash
        return None

    raise Exception('Error communicating with loan status server for resource %s' % resource_id)

def get_all_loaned_out():
    """Returns array of BSS status for all resources currently loaned out (according to BSS)"""
    global loanstatus_url

    if not loanstatus_url:
        raise Exception('No loanstatus_url -- cannot check loan status')

    url = '%s/is_loaned_out/' % loanstatus_url
    try:
        response = simplejson.loads(urllib2.urlopen(url).read())
        return response
    except IOError:
        raise Exception('Loan status server not available')

def is_loaned_out(resource_id):
    # bookreader loan status is stored in the private data store

    # Check our local status
    loan_key = get_loan_key(resource_id)
    if not loan_key:
        # No loan recorded
        identifier = resource_id[len('bookreader:'):]
        return lending.is_loaned_out_on_ia(identifier)

    # Find the loan and check if it has expired
    loan = web.ctx.site.store.get(loan_key)
    if loan:
        if datetime_from_isoformat(loan['expiry']) < datetime.datetime.utcnow():
            return True

    return False

def is_loaned_out_from_status(status):
    if not status:
        return False
    else:
        if status['returned'] == 'T':
            # Current loan has been returned
            return False

    # Has status and not returned
    return True

def update_loan_status(resource_id):
    """Update the loan status in OL based off status in ACS4.  Used to check for early returns."""

    # Get local loan record
    loan_key = get_loan_key(resource_id)

    if not loan_key:
        # No loan recorded, nothing to do
        return

    loan = web.ctx.site.store.get(loan_key)
    _update_loan_status(loan_key, loan, None)

def _update_loan_status(loan_key, loan, bss_status = None):
    # If this is a BookReader loan, local version of loan is authoritative
    if loan['resource_type'] == 'bookreader':
        # delete loan record if has expired
        # $$$ consolidate logic for checking expiry.  keep loan record for some time after it expires.
        if loan['expiry'] and loan['expiry'] < datetime.datetime.utcnow().isoformat():
            logger.info("%s: loan expired. deleting...", loan_key)
            web.ctx.site.store.delete(loan_key)
            on_loan_delete(loan)
        return

    # Load status from book status server
    if bss_status is None:
        bss_status = get_loan_status(loan['resource_id'])
    update_loan_from_bss_status(loan_key, loan, bss_status)

def update_loan_from_bss_status(loan_key, loan, status):
    """Update the loan status in the private data store from BSS status"""
    global loan_fulfillment_timeout_seconds

    if not resource_uses_bss(loan['resource_id']):
        raise Exception('Tried to update loan %s with ACS4/BSS status when it should not use BSS' % loan_key)

    if not is_loaned_out_from_status(status):
        # No loan record, or returned or expired

        # Check if our local loan record is fresh -- allow some time for fulfillment
        if loan['expiry'] is None:
            now = time.time()
            # $$$ loan_at in the store is in timestamp seconds until updated (from BSS) to isoformat string
            if now - loan['loaned_at'] < loan_fulfillment_timeout_seconds:
                # Don't delete the loan record - give it time to complete
                return

        # Was returned, expired, or timed out
        web.ctx.site.store.delete(loan_key)
        logger.info("%s: loan returned or expired or timedout, deleting...", loan_key)
        on_loan_delete(loan)
        return

    # Book has non-returned status
    # Update expiry
    if loan['expiry'] != status['until']:
        loan['expiry'] = status['until']
        web.ctx.site.store[loan_key] = loan
        logger.info("%s: updated expiry to %s", loan_key, loan['expiry'])
        on_loan_update(loan)

def update_all_loan_status():
    """Update the status of all loans known to Open Library by cross-checking with the book status server.
    This is called once an hour from a cron job.
    """
    # Get book status records of everything loaned out
    bss_statuses = get_all_loaned_out()
    bss_resource_ids = [status['resourceid'] for status in bss_statuses]

    loans = web.ctx.site.store.values(type='/type/loan', limit=-1)
    acs4_loans = [loan for loan in loans if loan['resource_type'] in ['epub', 'pdf']]
    for i, loan in enumerate(acs4_loans):
        logger.info("processing loan %s (%s)", loan['_key'], i)
        bss_status = None
        if resource_uses_bss(loan['resource_id']):
            try:
                bss_status = bss_statuses[bss_resource_ids.index(loan['resource_id'])]
            except ValueError:
                bss_status = None
        _update_loan_status(loan['_key'], loan, bss_status)

def resource_uses_bss(resource_id):
    """Returns true if the resource should use the BSS for status"""
    global acs_resource_id_prefixes

    if resource_id:
        for prefix in acs_resource_id_prefixes:
            if resource_id.startswith(prefix):
                return True
    return False

def user_can_borrow_edition(user, edition, resource_type):
    """Returns True if the book is eligible for lending and available, and
    if the user is pemitted to borrow this edition given their current
    number of loans and their position on the waiting list (if
    applicable)
    """
    if not edition.in_borrowable_collection():
        return False

    if user.get_loan_count() >= user_max_loans:
        return False

    realtime_availability = edition.get_realtime_availability()
    availability_status = realtime_availability['status']
    waitlist_size = realtime_availability['num_waitlist']

    if waitlist_size:
        return is_users_turn_to_borrow(user, edition)

    #resource_type in [loan['resource_type'] for loan in edition.get_available_loans()]:
    return availability_status == 'borrow_available'

def is_users_turn_to_borrow(user, edition):
    """If this user is waiting on this edition, it can only borrowed if
    user is the user is the first in the waiting list.
    """
    waiting_loan = user.get_waiting_loan_for(edition)
    return (waiting_loan and waiting_loan['status'] == 'available'
            and waiting_loan['position'] == 1)

def is_admin():
    """"Returns True if the current user is in admin usergroup."""
    user = accounts.get_current_user()
    return user and user.key in [m.key for m in web.ctx.site.get('/usergroup/admin').members]

def return_resource(resource_id):
    """Return the book to circulation!  This object is invalid and should not be used after
    this is called.  Currently only possible for bookreader loans."""
    loan_key = get_loan_key(resource_id)
    if not loan_key:
        raise Exception('Asked to return %s but no loan recorded' % resource_id)

    loan = web.ctx.site.store.get(loan_key)

    delete_loan(loan_key, loan)

def delete_loan(loan_key, loan = None):
    if not loan:
        loan = web.ctx.site.store.get(loan_key)
        if not loan:
            raise Exception('Could not find store record for %s', loan_key)

    loan.delete()

def get_ia_auth_dict(user, item_id, user_specified_loan_key, access_token):
    """Returns response similar to one of these:
    {'success':true,'token':'1287185207-fa72103dd21073add8f87a5ad8bce845','borrowed':true}
    {'success':false,'msg':'Book is checked out','borrowed':false, 'resolution': 'You can visit <a href="http://openlibary.org/ia/someid">this book\'s page on Open Library</a>.'}
    """

    base_url = 'http://' + web.ctx.host
    resolution_dict = { 'base_url': base_url, 'item_id': item_id }

    error_message = None
    user_has_current_loan = False

    # Sanity checks
    if not ia_identifier_is_valid(item_id):
        return {
            'success': False,
            'msg': 'Invalid item id',
            'resolution': 'This book does not appear to have a valid item identifier.'
        }

    # Lookup loan information
    loan = lending.get_loan(item_id)
    loan_key = loan and loan.get_key()

    if loan_key is None:
        # Book is not checked out as a BookReader loan - may still be checked out in ACS4
        error_message = 'Lending Library Book'
        resolution_message = 'This book is part of the <a href="%(base_url)s/subjects/Lending_library">lending library</a>. Please <a href="%(base_url)s/ia/%(item_id)s/borrow">visit this book\'s page on Open Library</a> to access the book.' % resolution_dict

    else:
        # If we know who this user is, from third-party cookies and they are logged into openlibrary.org, check if they have the loan
        if user:

            if loan['user'] != user.key:
                # Borrowed by someone else - OR possibly came in through ezproxy and there's a stale login in on openlibrary.org
                error_message = 'This book is checked out'
                resolution_message = 'This book is currently checked out.  You can <a href="%(base_url)s/ia/%(item_id)s">visit this book\'s page on Open Library</a> or <a href="%(base_url)s/subjects/Lending_library">look at other books available to borrow</a>.' % resolution_dict

            elif loan['expiry'] < datetime.datetime.utcnow().isoformat():
                # User has the loan, but it's expired
                error_message = 'Your loan has expired'
                resolution_message = 'Your loan for this book has expired.  You can <a href="%(base_url)s/ia/%(item_id)s">visit this book\'s page on Open Library</a>.' % resolution_dict

            else:
                # User holds the loan - win!
                user_has_current_loan = True
        else:
            # Don't have user context - not logged in or third-party cookies disabled

            # Check if the loan id + token is valid
            if user_specified_loan_key and access_token and ia_token_is_current(item_id, access_token):
                # Win!
                user_has_current_loan = True

            else:
                # Couldn't validate using token - they need to go to Open Library
                error_message = "Lending Library Book"
                resolution_message = 'This book is part of the <a href="%(base_url)s/subjects/Lending_library" title="Open Library Lending Library">lending library</a>. Please <a href="%(base_url)s/ia/%(item_id)s/borrow" title="Borrow book page on Open Library">visit this book\'s page on Open Library</a> to access the book.  You must have cookies enabled for archive.org and openlibrary.org to access borrowed books.' % resolution_dict

    if error_message:
        return { 'success': False, 'msg': error_message, 'resolution': resolution_message }
    else:
        # No error message, make sure we thought the loan was current as sanity check
        if not user_has_current_loan:
            raise Exception('lending: no current loan for this user found but no error condition specified')

    return {
        'success': True,
        'token': make_ia_token(item_id, BOOKREADER_AUTH_SECONDS)
    }


def make_ia_token(item_id, expiry_seconds):
    """Make a key that allows a client to access the item on archive.org for the number of
       seconds from now.
    """
    # $timestamp = $time+600; //access granted for ten minutes
    # $hmac = hash_hmac('md5', "{$id}-{$timestamp}", configGetValue('ol-loan-secret'));
    # return "{$timestamp}-{$hmac}";

    try:
        access_key = config.ia_access_secret
    except AttributeError:
        raise Exception("config value config.ia_access_secret is not present -- check your config")

    timestamp = int(time.time() + expiry_seconds)
    token_data = '%s-%d' % (item_id, timestamp)

    token = '%d-%s' % (timestamp, hmac.new(access_key, token_data).hexdigest())
    return token

def ia_token_is_current(item_id, access_token):
    try:
        access_key = config.ia_access_secret
    except AttributeError:
        raise Exception("config value config.ia_access_secret is not present -- check your config")

    # Check if token has expired
    try:
        token_timestamp = access_token.split('-')[0]
    except:
        return False

    token_time = int(token_timestamp)
    now = int(time.time())
    if token_time < now:
        return False

    # Verify token is valid
    try:
        token_hmac = access_token.split('-')[1]
    except:
        return False

    expected_data = '%s-%s' % (item_id, token_timestamp)
    expected_hmac = hmac.new(access_key, expected_data).hexdigest()

    if token_hmac == expected_hmac:
        return True

    return False

def make_bookreader_auth_link(loan_key, item_id, book_path, ol_host, ia_userid=None):
    """
    Generate a link to BookReaderAuth.php that starts the BookReader
    with the information to initiate reading a borrowed book
    """
    auth_link = 'https://%s/bookreader/BookReaderAuth.php?' % bookreader_host
    params = {
        'uuid': loan_key,
        'token': make_ia_token(item_id, BOOKREADER_AUTH_SECONDS),
        'id': item_id,
        'bookPath': book_path,
        'olHost': ol_host,
        'olAuthUrl': "https://{0}/ia_auth/XXX".format(ol_host),
        'iaUserId': ia_userid,
        'iaAuthToken': make_ia_token(ia_userid, READER_AUTH_SECONDS)
    }
    return auth_link + urllib.urlencode(params)

def on_loan_update(loan):
    # update the waiting list and ebook document.
    waitinglist.update_waitinglist(loan['ocaid'])

def on_loan_delete(loan):
    # update the waiting list and ebook document.
    waitinglist.update_waitinglist(loan['ocaid'])

msgbroker.subscribe("loan-created", on_loan_update)
msgbroker.subscribe("loan-completed", on_loan_delete)
lending.setup(config)
