"""Admin functionality.
"""

import calendar
import datetime
import web
from infogami import config
from infogami.utils import stats

from . import cache

class Stats:
    def __init__(self, docs, key, total_key):
        self.key = key
        self.docs = docs
        try:
            self.latest = docs[-1].get(key, 0)
        except IndexError:
            self.latest = 0

        try:
            self.previous = docs[-2].get(key, 0)
        except IndexError:
            self.previous = 0

        try:
            # Last available total count
            self.total = (x for x in reversed(docs) if total_key in x).next()[total_key]
        except (KeyError, StopIteration):
            self.total = ""

    def get_counts(self, ndays = 28, times = False):
        """Returns the stats for last n days as an array useful for
        plotting. i.e. an array of [x, y] tuples where y is the value
        and `x` the x coordinate.

        If times is True, the x coordinate in the tuple will be
        timestamps for the day.
        """
        def _convert_to_milli_timestamp(d):
            """Uses the `_id` of the document `d` to create a UNIX
            timestamp and coverts it to milliseconds"""
            t = datetime.datetime.strptime(d, "counts-%Y-%m-%d")
            return calendar.timegm(t.timetuple()) * 1000

        if times:
            return [[_convert_to_milli_timestamp(x['_key']), x.get(self.key,0)] for x in self.docs[-ndays:]]
        else:
            return zip(range(0, ndays*5, 5),
                       (x.get(self.key, 0) for x in self.docs[-ndays:])) # The *5 and 5 are for the bar widths

    def get_summary(self, ndays = 28):
        """Returns the summary of counts for past n days.

        Summary can be either sum or average depending on the type of stats.
        This is used to find counts for last 7 days and last 28 days.
        """
        return sum(x[1] for x in self.get_counts(ndays))

@cache.memoize(engine="memcache", key="admin._get_count_docs", expires=5*60)
def _get_count_docs(ndays):
    """Returns the count docs from admin stats database.

    This function is memoized to avoid accessing the db for every request.
    """
    today = datetime.datetime.utcnow().date()
    dates = [today-datetime.timedelta(days=i) for i in range(ndays)]

    # we want the dates in reverse order
    dates = dates[::-1]

    docs = [web.ctx.site.store.get(d.strftime("counts-%Y-%m-%d")) for d in dates]
    return [d for d in docs if d]

def get_stats(ndays = 30):
    """Returns the stats for the past `ndays`"""
    docs = _get_count_docs(ndays)
    retval = dict(human_edits = Stats(docs, "human_edits", "human_edits"),
                  bot_edits   = Stats(docs, "bot_edits", "bot_edits"),
                  lists       = Stats(docs, "lists", "total_lists"),
                  visitors    = Stats(docs, "visitors", "visitors"),
                  loans       = Stats(docs, "loans", "loans"),
                  members     = Stats(docs, "members", "total_members"),
                  works       = Stats(docs, "works", "total_works"),
                  editions    = Stats(docs, "editions", "total_editions"),
                  ebooks      = Stats(docs, "ebooks", "total_ebooks"),
                  covers      = Stats(docs, "covers", "total_covers"),
                  authors     = Stats(docs, "authors", "total_authors"),
                  subjects    = Stats(docs, "subjects", "total_subjects"))
    return retval
