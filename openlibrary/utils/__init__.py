"""Generic utilities"""

from urllib import quote_plus
import re

to_drop = set(''';/?:@&=+$,<>#%"{}|\\^[]`\n\r''')

def str_to_key(s):
    return ''.join(c if c != ' ' else '_' for c in s.lower() if c not in to_drop)

def url_quote(s):
    return quote_plus(s.encode('utf-8')) if s else ''

def finddict(dicts, **filters):
    """Find a dictionary that matches given filter conditions.

        >>> dicts = [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
        >>> finddict(dicts, x=1)
        {'x': 1, 'y': 2}
    """
    for d in dicts:
        if (all(d.get(k) == v for k, v in filters.iteritems())):
            return d

re_solr_range = re.compile(r'\[.+\bTO\b.+\]', re.I)
re_bracket = re.compile(r'[\[\]]')
def escape_bracket(q):
    if re_solr_range.search(q):
        return q
    return re_bracket.sub(lambda m:'\\'+m.group(), q)

def uniq(values, key=None):
    """Returns the unique entries from the given values in the original order.

    The value of the optional `key` parameter should be a function that takes
    a single argument and returns a key to test the uniqueness.
    TODO: Moved this to core/utils.py
    """
    key = key or (lambda x: x)
    s = set()
    result = []
    for v in values:
        k = key(v)
        if k not in s:
            s.add(k)
            result.append(v)
    return result

def dicthash(d):
    """Dictionaries are not hashable. This function converts dictionary into nested tuples, so that it can hashed.
    """
    if isinstance(d, dict):
        return tuple((k, dicthash(v)) for k, v in d.iteritems())
    elif isinstance(d, list):
        return tuple(dicthash(v) for v in d)
    else:
        return d

author_olid_re = re.compile(r'^OL\d+A$')
def is_author_olid(s):
    """Case sensitive check for strings like 'OL123A'."""
    return bool(author_olid_re.match(s))

work_olid_re = re.compile(r'^OL\d+W$')
def is_work_olid(s):
    """Case sensitive check for strings like 'OL123W'."""
    return bool(work_olid_re.match(s))

def extract_numeric_id_from_olid(olid):
    """
    >>> "OL123W"
    123
    >>> "/authors/OL123A"
    123
    """
    if '/' in olid:
        olid = olid.split('/')[-1]
    if olid.lower().startswith('ol'):
        olid = olid[2:]
    if not is_number(olid[-1].lower()):
        olid = olid[:-1]
    return olid

def is_number(s):
    try:
        int(s)
        return True
    except ValueError:
        return False
