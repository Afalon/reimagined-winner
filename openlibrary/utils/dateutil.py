"""Generic date utilities.
"""

import datetime
import calendar


MINUTE_SECS = 60
HALF_HOUR_SECS = MINUTE_SECS * 30
HOUR_SECS = MINUTE_SECS * 60
HALF_DAY_SECS = HOUR_SECS * 12
DAY_SECS = HOUR_SECS * 24
WEEK_SECS = DAY_SECS * 7


def days_in_current_month():
    now = datetime.datetime.now()
    return calendar.monthrange(now.year, now.month)[1]


def date_n_days_ago(n=None, start=None):
    """
    Args:
        n (int) - number of days since start
        start (date) - date to start counting from (default: today)
    Returns:
        A (datetime.date) of `n` days ago if n is provided, else None
    """
    _start = start or datetime.date.today()
    return (_start - datetime.timedelta(days=n)) if n else None


DATE_ONE_MONTH_AGO = date_n_days_ago(n=days_in_current_month())
DATE_ONE_WEEK_AGO = date_n_days_ago(n=7)

def parse_date(datestr):
    """Parses date string.

        >>> parse_date("2010")
        datetime.date(2010, 01, 01)
        >>> parse_date("2010-02")
        datetime.date(2010, 02, 01)
        >>> parse_date("2010-02-04")
        datetime.date(2010, 02, 04)
    """
    tokens = datestr.split("-")
    _resize_list(tokens, 3)

    yyyy, mm, dd = tokens[:3]
    return datetime.date(int(yyyy), mm and int(mm) or 1, dd and int(dd) or 1)

def parse_daterange(datestr):
    """Parses date range.

        >>> parse_daterange("2010-02")
        (datetime.date(2010, 02, 01), datetime.date(2010, 03, 01))
    """
    date = parse_date(datestr)
    tokens = datestr.split("-")

    if len(tokens) == 1: # only year specified
        return date, nextyear(date)
    elif len(tokens) == 2: # year and month specified
        return date, nextmonth(date)
    else:
        return date, nextday(date)

def nextday(date):
    return date + datetime.timedelta(1)

def nextmonth(date):
    """Returns a new date object with first day of the next month."""
    year, month = date.year, date.month
    month = month + 1

    if month > 12:
        month = 1
        year += 1

    return datetime.date(year, month, 1)

def nextyear(date):
    """Returns a new date object with first day of the next year."""
    return datetime.date(date.year+1, 1, 1)

def _resize_list(x, size):
    """Increase the size of the list x to the specified size it is smaller.
    """
    if len(x) < size:
        x += [None] * (size - len(x))

