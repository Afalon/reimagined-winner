# coding=utf8
from __future__ import print_function
import bz2
import codecs
import sys
import re
import simplejson as json
from catalog.marc.fast_parse import get_subfields, get_all_subfields, get_subfield_values
from unicodedata import normalize
import MySQLdb
from catalog.utils import pick_first_date
from time import time

re_marc_name = re.compile('^(.*), (.*)$')

def norm(s):
    return normalize('NFC', s)

def get_conn():
    return MySQLdb.connect(passwd='', user='', use_unicode=True, charset='utf8', db='wiki_people')

def get_cursor():
    return get_conn().cursor()

sys.stdout = codecs.getwriter('utf8')(sys.stdout)
re_skip = re.compile('^(History|Demograph(ics|y)|Lists?) of')

def list_names():
    for line in bz2.BZ2File('people.bz2'):
        cur = json.loads(line.decode('utf8'))
        title = cur['title']
        if re_skip.match(title):
            continue
        print(title)

def redirects():
    titles = set([line[:-1] for line in codecs.open('people_names', 'r', 'utf8')])

    for line in bz2.BZ2File('redirects.bz2'):
        (f, t) = json.loads(line.decode('utf8'))
        t = t.replace('_', ' ')
        if t in titles:
            print((f, t))

def redirect_dict():
    redirects = {}
    for line in open('people_redirects'):
        (f, t) = eval(line)
        t = t.replace('_', ' ')
        redirects.setdefault(t, []).append(f)
    print(redirects)

def add_redirects():
    redirects = eval(open('redirect_dict').read())
    for line in bz2.BZ2File('people.bz2'):
        cur = json.loads(line.decode('utf8'))
        title = cur['title']
        if re_skip.match(title):
            continue
        if title in redirects:
            cur['redirects'] = redirects[title]
        print(cur)

#add_redirects()
#redirect_dict()

re_syntax = re.compile(r'(.*?)(\||{{|}}|\[\[|\]\])', re.DOTALL)
re_html_comment = re.compile('<!-- .* -->')
re_space_or_underscore = re.compile('[ _]')
re_infobox_template = re.compile('^infobox[_ ]books?(?:\s*<!--.*-->)?\s*', re.I)
re_persondata = re.compile('^Persondata\s*', re.I)

re_line = re.compile('^\s*\|\s*([A-Z ]+?)\s*=\s*(.*?)\s*$')
def parse_template2(s):
    fields = {}
    for l in s.split('\n'):
        m = re_line.match(l)
        if not m:
            continue
        name, value = m.groups()
        fields[name.strip()] = value
    return fields

def parse_template(s, expected_name):
    template_depth = 1
    link_depth = 0
    pos = 2
    buf = ''

    data = []
    while template_depth > 0:
        m = re_syntax.match(s[pos:])

        pos = pos+m.end()
        buf += m.group(1)
        if m.group(2) == '{{':
            buf += m.group(2)
            template_depth += 1
            continue

        if m.group(2) == '[[':
            buf += m.group(2)
            link_depth += 1
            continue

        if template_depth == 1 and link_depth == 0:
            data.append(buf)
            buf = ''
        elif m.group(2) == '|':
            buf += '|'
        if m.group(2) == '}}':
            buf += m.group(2)
            template_depth -= 1
            continue
        if m.group(2) == ']]':
            buf += m.group(2)
            if link_depth > 0:
                link_depth -= 1
            continue
        assert m.group(2) == '|'
    if buf != '}}':
        return parse_template2(s)
    assert buf == '}}'

    template_name = data.pop(0)
    try:
        assert template_name.lstrip().lower().startswith(expected_name.lower())
        #assert re_persondata.match(infobox_template)
        #assert re_infobox_template.match(infobox_template)
    except AssertionError:
        print(template_name)
        raise

    fields = {}
    for line in data:
        line = line.strip();
        if line == '' or ((line.startswith('<!--') or line.startswith('< --')) and line.endswith('-->')) or line == 'PLEASE SEE [[WP:PDATA]]!':
            continue
        if '=' in line:
            name, value = line.split('=', 1)
        else:
            m = re_missing_equals.match(line)
            if not m:
                return parse_template2(s)
            name, value = m.groups()
        fields[name.strip()] = value.strip()
    return fields

re_missing_equals = re.compile('^([A-Z ]+) (.+)$')

def parse_pd(pd):
    lines = pd.split('\n')
    print(repr(lines[-1]))
    assert lines[-1] == '}}'

def read_person_data():
    expect = set([u'DATE OF DEATH', u'NAME', u'SHORT DESCRIPTION', u'ALTERNATIVE NAMES', u'PLACE OF BIRTH', u'DATE OF BIRTH', u'PLACE OF DEATH'])
    for line in open('people'):
        cur = eval(line)
        if 'persondata' not in cur:
            continue
        title = cur['title']
        if title == 'Murray Bookchin':
            continue
#        print 'title:', title
        pd = cur['persondata']
        k = set(parse_template(pd, 'persondata').keys())
        if k > expect:
            print(title)
            print(k)

def iter_people():
    return (eval(line) for line in open('people'))

def date_cats():
    re_date_cat = re.compile('^(.*\d.*) (birth|death)s$')
    cats = {'birth': {}, 'death':{}}
    for cur in iter_people():
        title = cur['title']
        #print [cat for cat in cur['cats'] if cat.endswith('births') or cat.endswith('deaths')]
        for cat in cur['cats']:
            m = re_date_cat.match(cat)
            if not m:
                continue
            cats[m.group(2)].setdefault(m.group(1), set()).add(title)
#        print 'birth:', [(i[0], len(i[1])) for i in sorted(cats['birth'].items(), reverse = True, key = lambda i: len(i[1]))[:5]]
#        print 'death:', [(i[0], len(i[1])) for i in sorted(cats['death'].items(), reverse = True, key = lambda i: len(i[1]))[:5]]
    print(cats)

#read_person_data()
#date_cats()

def fmt_line(fields):
    def bold(s):
        return ''.join(i + '\b' + i for i in s)
    return ''.join(bold("$" + k) + norm(v) for k, v in fields)

def strip_brackets(line):
    if line[4] == '[' and line[-2] == ']':
        return line[0:4] + line[5:-2] + line[-1]
    else:
        return line

def read_marc():
    for line in bz2.BZ2File('marc_authors.bz2'):
        line = eval(line)
        if '[Sound recording]' in line:
            continue
        line = strip_brackets(line)
        #print expr_in_utf8(get_all_subfields(line))
        print(fmt_line(get_subfields(line, 'abcd')))

#read_marc()

#   528,859 wikipedia
# 3,596,802 MARC


def get_names(cur):
    titles = [cur['title']] + cur.get('redirects', [])
    if 'persondata' in cur:
        pd = parse_template(cur['persondata'], 'persondata')
        if 'NAME' in pd and pd['NAME']:
            titles.append(pd['NAME'])
        if 'ALTERNATIVE NAMES' in pd:
            alt = pd['ALTERNATIVE NAMES']
            if len(alt) > 100 and ',' in alt and ';' not in alt:
                alt = alt.split(',')
            else:
                alt = alt.split(';')
            titles += [j for j in (i.strip() for i in alt) if j]
    return set(i.lower() for i in titles)

def read_people():
    from collections import defaultdict
#    wiki = []
#    title_lookup = defaultdict(list)
    maximum = 0
    for cur in iter_people():
#        wiki.append(cur)
        titles = [cur['title']] + cur.get('redirects', [])
        if 'persondata' in cur:
            pd = parse_template(cur['persondata'], 'persondata')
            if 'NAME' in pd and pd['NAME']:
                titles.append(pd['NAME'])
            if 'ALTERNATIVE NAMES' in pd:
                alt = pd['ALTERNATIVE NAMES']
                if len(alt) > 100 and ',' in alt and ';' not in alt:
                    alt = alt.split(',')
                else:
                    alt = alt.split(';')
                titles += [j for j in (i.strip() for i in alt) if j]
        cur_max = max(len(i) for i in titles)
        if cur_max > maximum:
            maximum = cur_max
            print(maximum)
            print(cur['title'])
            print(titles)
#        for t in set(titles):
#            title_lookup[t].append(cur)

# filter names: Robert Bob Adam Hincmar Anselm

# Personal life of Marcus Tullius Cicero

def load_db():
    c = get_cursor()
    c.execute('truncate people')
    c.execute('truncate names')
    c.execute('truncate redirects')
    for person in iter_people():
#        print person
        c.execute('insert into people (title, len, infobox, defaultsort, persondata, cats) values (%s, %s, %s, %s, %s, %s)', (person['title'], person['len'], person.get('infobox', None), person.get('defaultsort', None), person.get('persondata', None), repr(person.get('cats', []))))
        id = conn.insert_id()
        c.executemany('insert ignore into names (person_id, name) values (%s, %s)', [(id, n) for n in get_names(person)])
        if 'redirects' in person:
            redirects = set(r.lower() for r in person['redirects'])
            c.executemany('insert ignore into redirects (person_id, redirect) values (%s, %s)', [(id, r) for r in redirects])

re_lifetime = re.compile('\{\{lifetime\| *(\d+s?(?: BC)?|missing|unknown|\d\d?[a-z][a-z] century)? *(?:\| *(\d+s?(?: BC)?|living|unknown|missing|\d\d?[a-z][a-z] century)? *)?(?:\|([^|]*))?\}\}', re.I)

def load_lifetime():
    c = get_cursor()
    for person in iter_people():
        if 'lifetime' not in person:
            continue
        m = re_lifetime.match(person['lifetime'])
        if not m:
            continue
        (birth, death, defaultsort) = m.groups()
        cats = person.get('cats', [])
#        print "select id from people where title='%s'" % person['title']
        c.execute("select id from people where title=%s", (person['title'],))
        (id,) = c.fetchone()
        update_cats = False
        if birth and birth.lower() not in ('missing', 'unknown'):
            new_cat = birth + " births"
            if new_cat not in cats:
                cats.append(new_cat)
                update_cats = True
        if death and death.lower() not in ('missing', 'unknown', 'living'):
            new_cat = death + " deaths"
            if new_cat not in cats:
                cats.append(new_cat)
                update_cats = True
        if update_cats:
            print(person['title'])
#            print 'update people set cats=%s where id=%s' % (repr(cats), id)
            c.execute('update people set cats=%s where id=%s', (repr(cats), id))
        if defaultsort:
            add_to_names(c, id, defaultsort)

re_defaultsort = re.compile('^{{defaultsort(?:key)?[;:|]\n?(.*)\n?}}$', re.I)

re_comma = re.compile(', *')
re_comma_and_space = re.compile('[, ]+')

def add_to_names(c, id, name):
    name = re_comma.sub(' ', name).lower().strip()
    c.execute('insert ignore into names (person_id, name) values (%s, %s)', (id, name))

def add_default_sort():
    c = get_cursor()
    c.execute("select id, title, defaultsort from people where defaultsort is not null")
    for id, title, ds in c.fetchall():
#        print id, ds
        if title == 'Omar Gooding':
            ds = '{{DEFAULTSORT:Gooding, Omar}}'
        m = re_defaultsort.match(ds)
        if not m:
            print("http://en.wikipedia.org/wiki/" + title.replace(' ', '_'))
            print(ds)
        if m.group(1):
            add_to_names(c, id, m.group(1))

re_br_or_semicolon = re.compile('(?:</?br ?/?>|;)')
re_strip = re.compile("(?:\([^)]*\)|<!--.*?-->|\[\[.*?\]\]|'''?)")
re_strip2 = re.compile('(?:<.*?>|\{\{.*?\}\})')

def add_names_from_infobox():
    c = get_cursor()
    c.execute("select id, title, infobox from people where infobox is not null")
    for id, title, infobox in c.fetchall():
        try:
            infobox = parse_template(infobox, 'infobox')
        except AttributeError:
            continue
        for field in 'name', 'full name':
            if field not in infobox or not infobox[field]:
                continue
            v = re_strip.sub('', infobox[field])
#            v = infobox[field]
            v = [i for i in (re_strip2.sub('', i).strip(' ,:') for i in re_br_or_semicolon.split(v)) if i]
            for i in v:
                i = re_comma_and_space.sub(' ', i)
                if title != i:
                    add_to_names(c, id, i)
#            print title, ':', field, ':', infobox[field], v

def strip_commas_from_names():
    c = get_cursor()
    c.execute("select person_id, name from names where name like '%,%'")
    for id, name in c.fetchall():
        new = re_comma.sub(' ', name)
        if new == ' ' or new == name:
            print((id, name, new))
        assert new != ' ' and new != name
        c.execute("update ignore names set name=%s where person_id=%s and name=%s", (new, id, name))

#read_people()

#load_db()

def flip_name(name):
    m = re_marc_name.match(name)
    if m:
        return m.group(2) + ' ' + m.group(1)
    return name

re_digit = re.compile('\d+')
re_decade = re.compile('^(\d+)s$')
re_bc_date = re.compile('^(.*) B\.C\.?$')
re_cent = re.compile('^(\d+)[a-z][a-z] cent\.$')
re_century = re.compile('^(\d+)[a-z][a-z] century$')

def decade_match(a, start):
    end = start + 10
    if a.isdigit():
        return start <= int(a) < end
    return any((start <= int(c) < end) for c in re_digit.findall(a))

def year_approx_match(a, b):
    approx_century_match = False
    if a.startswith('ca. '):
        ca = True
        a = a[4:]
        range = 20
    else:
        ca = False
        range = 9
    if a == b:
        return True
    if a.replace('.', '') == b:
        return True # ca. 440 B.C.
    if a.endswith(' cent.') and b.endswith(' century') and b.startswith(a[:-1]):
        return True

    bc = False
    if b.endswith(' BC'):
        m = re_bc_date.match(a)
        if m:
            a = m.group(1)
            b = b[:-3]
            bc = True
    if approx_century_match and a.isdigit() and b.endswith(' century'):
        a = int(a)
        m = re_century.match(b)
        assert m
        cent = int(m.group(1))
        start = cent - 1 if not bc else cent
        end = cent if not bc else cent + 1
        #print cent, start, a, end
        if start * 100 <= a < end * 100:
            return True

    if b.isdigit():
        b = int(b)
        if a.isdigit() and (bc or b < 1850) and abs(int(a) - b) <= range:
            return True
        if approx_century_match and a.endswith(' cent.'):
            m = re_cent.match(a)
            if m:
                cent = int(m.group(1))
                start = cent - 1 if not bc else cent
                end = cent if not bc else cent + 1
                if start * 100 <= b < end * 100:
                    return True
        for c in re_digit.findall(a):
            c = int(c)
            if c == b:
                return True
            if (bc or b < 1850) and abs(c - b) <= range:
                return True
        return False
    m = re_decade.match(b)
    if not m:
        return False
    start = int(m.group(1))
    return decade_match(a, start)

def test_year_approx_match():
    assert not year_approx_match('1939', '1940')
    assert year_approx_match('582', '6th century')
    assert year_approx_match('13th cent.', '1240')
    assert year_approx_match('ca. 360 B.C.', '365 BC')
    assert year_approx_match('1889', '1890')
    assert year_approx_match('1883?', '1882')
    assert year_approx_match('1328?', '1320s')
    assert year_approx_match('11th cent.', '11th century')
    assert not year_approx_match('1330', '1320s')
    assert not year_approx_match('245 B.C.', '3rd century BC')

def date_match(dates, cats):
    match_found = False
    for f in ['birth', 'death']:
        if f + '_date' not in dates:
            continue
        marc = dates[f + '_date']
        this_cats = [i[:-(len(f)+2)] for i in cats if i.endswith(' %ss' % f)]
        if not this_cats:
            continue
        m = any(year_approx_match(marc, i) for i in this_cats)
        #print m, marc, this_cats
        if m:
            match_found = True
        else:
            return False
    return match_found

def test_date_match():
    # $aAngelico,$cfra,$dca. 1400-l455.
    dates = {'birth_date': u'ca. 1400', 'death_date': u'1455'}
    cats = [u'1395 births', u'1455 deaths']
    assert date_match(dates, cats)

    # $aAndocides,$dca. 440-ca. 390 B.C.
    dates = {'birth_date': u'ca. 440 B.C.', 'death_date': u'ca. 390 B.C.'}
    cats = [u'440 BC births', u'390 BC deaths', u'Ancient Athenians']
    assert date_match(dates, cats)

    # $aAlexander,$cof Hales,$dca. 1185-1245.
    dates = {'birth_date': u'ca. 1185', 'death_date': u'1245'}
    cats = [u'13th century philosophers', u'1245 deaths', u'Roman Catholic philosophers', u'English theologians', u'Franciscans', u'Scholastic philosophers', u'People from Gloucestershire']
    assert date_match(dates, cats)

    dates = {'birth_date': u'1922'}
    cats = [u'1830 births', u'1876 deaths']
    assert not date_match(dates, cats)

    dates = {'birth_date': u'1889', 'death_date': u'1947'}
    cats = [u'1890 births', u'1947 deaths']
    assert date_match(dates, cats)

    dates = {'birth_date': u'1889', 'death_date': u'1947'}
    cats = [u'1890 births', u'1947 deaths']
    assert date_match(dates, cats)

    dates = {}
    cats = [u'1890 births', u'1947 deaths']
    assert not date_match(dates, cats)

    dates = {'birth_date': u'1883?', 'death_date': u'1963'}
    cats = [u'1882 births', u'1963 deaths']
    assert date_match(dates, cats)

    dates = {'birth_date': u'1328?', 'death_date': u'1369'}
    cats = [u'Karaite rabbis', u'1320s births', u'1369 deaths']
    assert date_match(dates, cats)

    dates = {'birth_date': u'ca. 1110', 'death_date': u'ca. 1180'}
    cats = [u'1120s births', u'1198 deaths']
    assert date_match(dates, cats)

    # $aAbu Nuwas,$dca. 756-ca. 810.  # Abu Nuwas
    dates = {'birth_date': u'ca. 756', 'death_date': u'ca. 810'}
    cats = [u'750 births', u'810 deaths']
    assert date_match(dates, cats)

re_title_of = re.compile('^(.*) (of .*)$')

def name_lookup(c, fields):
    def join_fields(fields, want):
        return ' '.join(v for k, v in fields if k in want)
    if not any(k == 'd' for k, v in fields):
        return []
    ab = [v for k, v in fields if k in 'ab']
    name = ' '.join(ab)
    flipped = flip_name(name)
    names = set([name, flipped])
    #names = set([flipped])
    if any(k == 'c' for k, v in fields):
        name = join_fields(fields, 'abc')
        names.update([name, flip_name(name)])
        title = [v for k, v in fields if k in 'c']
        names.update([' '.join(title + ab), ' '.join(title + [flipped])])
        title = ' '.join(title)
        names.update(["%s (%s)" % (name, title), "%s (%s)" % (flipped, title)])
        sp = title.find(' ')
        if sp != -1:
            m = re_title_of.search(title)
            if m:
                role, of_place = m.groups()
                names.update([' '.join(ab + [of_place]), ' '.join([flipped, of_place])])
                names.update([' '.join([role] + ab + [of_place]), ' '.join([role, flipped, of_place])])

            t = title[:sp]
            names.update([' '.join([t] + ab), ' '.join([t, flipped])])

    found = []
    for n in set(re_comma.sub(' ', n) for n in names):
        c.execute("select title, cats, name, persondata from names, people where people.id = names.person_id and name=%s", (n,))
        found += c.fetchall()
    return found

# $aAleksandr Mikhaĭlovich,$cGrand Duke of Russia,$d1866-1933.
# == Grand Duke Alexander Mikhailovich of Russia

def pick_from_match(match):
    good = [(name, (cats, match_name)) for name, (cats, match_name) in match.items() if name.lower() == match_name]
    if len(good) == 1:
        return dict(good)
    return match

def more_than_one_match(match):
    for name, (cats, match_name) in match.items():
        print(name, cats, match_name)
        print("http://en.wikipedia.org/wiki/" + name.replace(' ', '_'))
    print()

#$aSmith, William,$d1769-1839
#William Smith (geologist) [u'English geologists', u'Canal engineers', u'People from Oxfordshire', u'Somerset coalfield', u'1769 births', u'1839 deaths', u'People from Scarborough, North Yorkshire', u'Wollaston Medal winners'] william smith
#http://en.wikipedia.org/wiki/William_Smith_(geologist)
#William Smith (South Carolina senator) [u'1762 births', u'1840 deaths', u'United States Senators from South Carolina', u'Democratic Party (United States) vice presidential nominees', u'South Carolina lawyers'] william smith
#http://en.wikipedia.org/wiki/William_Smith_(South_Carolina_senator)

noble_or_clergy = ['King', 'Queen', 'Prince', 'Princess', 'Duke', 'Archduke', 'Baron', 'Pope', 'Antipope', 'Bishop', 'Archbishop']
re_noble_or_clergy = re.compile('(' + '|'.join( noble_or_clergy ) + ')')

def db_marc_lookup():
    verbose = False
    c = get_cursor()
    articles = set()
    count = 0
    count_with_date = 0
    t0 = time()
    match_count = 0
    total = 3596802
    prev_fields = None
    fh = open('matches3', 'w')
    for line in bz2.BZ2File('marc_authors.bz2'):
        count+=1
        line = eval(line)
        line = strip_brackets(line)
        if count % 5000 == 0:
            t1 = time() - t0
            rec_per_sec = count / t1
            time_left = (total - count) / rec_per_sec
            print(fmt_line(get_subfields(line, 'abcd')))
            print(count, count_with_date, match_count, "%.2f%% %.2f mins left" % (float(match_count * 100.0) / float(count_with_date), time_left / 60))
        fields = tuple((k, v.strip(' /,;:')) for k, v in get_subfields(line, 'abcd'))
        if prev_fields == fields:
            continue
        prev_fields = fields
        dates = pick_first_date(v for k, v in fields if k == 'd')
        if dates.items()[0] == ('date', ''):
            continue
        count_with_date += 1
        if verbose:
            print(fmt_line(get_subfields(line, 'abcd')))
            print(dates)
        is_noble_or_clergy = any(re_noble_or_clergy.search(v) \
            for v in get_subfield_values(line, 'c'))
        found = name_lookup(c, fields)
        if not found:
            continue
            if is_noble_or_clergy:
                print('noble or clergy not found:')
                print(fmt_line(get_subfields(line, 'abcd')))
                print()
            continue
        match = {}
        seen = set()
        for name, cats, match_name, pd in found:
            if name in seen:
                continue
            seen.add(name)
            cats = eval(cats)
            if not any(cat.endswith(' births') or cat.endswith(' deaths') for cat in cats):
                continue
            dm = date_match(dates, cats)
            if dm:
                match[name] = (cats, match_name)
            if not verbose:
                continue
            print((name, match_name))
            print("cats =", cats)
            print(('match' if dm else 'no match'))
            for field in ['birth', 'death']:
                print(field + 's:', [i[:-(len(field)+2)] for i in cats if i.endswith(' %ss' % field)], end=' ')
            print()
        if verbose:
            print('---')

        if not match:
            continue
            if is_noble_or_clergy:
                print('noble or clergy not found:')
                print(fmt_line(get_subfields(line, 'abcd')))
                print(found)
                print()
            continue
        match_count+=1
#        articles.add(match.keys()[0])
        if len(match) != 1:
            match = pick_from_match(match)
        if len(match) != 1:
            print(count, match_count)
            print(fmt_line(get_subfields(line, 'abcd')))
            more_than_one_match(match)
        else:
            #print (list(get_subfields(line, 'abcd')), match.keys()[0])
            print((match.keys()[0], fields), file=fh)
        continue
#        print len(articles), match[0][0], fmt_line(get_subfields(line, 'abcd'))
        assert len(match) == 1
    print(match_count)
    fh.close()

#add_names_from_infobox()
#test_year_approx_match()
db_marc_lookup()
#test_date_match()
#add_default_sort()
#strip_commas_from_names()

#load_lifetime()
