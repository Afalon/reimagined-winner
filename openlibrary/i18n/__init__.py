from __future__ import print_function
import web
import os
from StringIO import StringIO

import babel
from babel.support import Translations
from babel.messages import Catalog
from babel.messages.pofile import read_po, write_po
from babel.messages.mofile import write_mo
from babel.messages.extract import extract_from_file, extract_from_dir, extract_python

root = os.path.dirname(__file__)

def _compile_translation(po, mo):
    try:
        catalog = read_po(open(po))

        f = open(mo, 'wb')
        write_mo(f, catalog)
        f.close()
        print('compiled', po, file=web.debug)
    except:
        print('failed to compile', po, file=web.debug)

def get_locales():
    return [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]

def extract_templetor(fileobj, keywords, comment_tags, options):
    """Extract i18n messages from web.py templates."""
    try:
        # Replace/remove inline js '\$' which interferes with the Babel python parser:
        code = web.template.Template.generate_code(fileobj.read().replace('\$', ''), fileobj.name)
        f = StringIO(code)
        f.name = fileobj.name
    except Exception as e:
        print(fileobj.name + ':', str(e), file=web.debug)
        return []
    return extract_python(f, keywords, comment_tags, options)

def extract_messages(dirs):
    catalog = Catalog(
        project='Open Library',
        copyright_holder='Internet Archive'
    )
    METHODS = [
        ("**.py", "python"),
        ("**.html", "openlibrary.i18n:extract_templetor")
    ]
    COMMENT_TAGS = ["NOTE:"]

    for d in dirs:
        if '.html' in d:
            extracted = [(d,) + extract for extract in extract_from_file("openlibrary.i18n:extract_templetor", d)]
        else:
            extracted = extract_from_dir(d, METHODS, comment_tags=COMMENT_TAGS, strip_comment_tags=True)
        for filename, lineno, message, comments, context in extracted:
            catalog.add(message, None, [(filename, lineno)], auto_comments=comments)

    path = os.path.join(root, 'messages.pot')
    f = open(path, 'w')
    write_po(f, catalog)
    f.close()

    print('wrote template to', path)

def compile_translations():
    for locale in get_locales():
        po_path = os.path.join(root, locale, 'messages.po')
        mo_path = os.path.join(root, locale, 'messages.mo')

        if os.path.exists(po_path):
            _compile_translation(po_path, mo_path)

def update_translations():
    pot_path = os.path.join(root, 'messages.pot')
    template = read_po(open(pot_path))

    for locale in get_locales():
        po_path = os.path.join(root, locale, 'messages.po')
        mo_path = os.path.join(root, locale, 'messages.mo')

        if os.path.exists(po_path):
            catalog = read_po(open(po_path))
            catalog.update(template)

            f = open(po_path, 'w')
            write_po(f, catalog)
            f.close()
            print('updated', po_path)

    compile_translations()

@web.memoize
def load_translations(lang):
    po = os.path.join(root, lang, 'messages.po')
    mo_path = os.path.join(root, lang, 'messages.mo')

    if os.path.exists(mo_path):
        return Translations(open(mo_path))

@web.memoize
def load_locale(lang):
    try:
        return babel.Locale(lang)
    except babel.UnknownLocaleError:
        pass

class GetText:
    def __call__(self, string, *args, **kwargs):
        """Translate a given string to the language of the current locale."""
        translations = load_translations(web.ctx.get('lang', 'en'))
        value = (translations and translations.ugettext(string)) or string

        if args:
            value = value % args
        elif kwargs:
            value = value % kwargs

        return value

    def __getattr__(self, key):
        from infogami.utils.i18n import strings
        # for backward-compatability
        return strings.get('', key)

class LazyGetText:
    def __call__(self, string, *args, **kwargs):
        """Translate a given string lazily."""
        return LazyObject(lambda: GetText()(string, *args, **kwargs))

class LazyObject:
    def __init__(self, creator):
        self._creator = creator

    def __str__(self):
        return web.safestr(self._creator())

    def __repr__(self):
        return repr(self._creator())

    def __add__(self, other):
        return self._creator() + other

    def __radd__(self, other):
        return other + self._creator()

def ungettext(s1, s2, _n, *a, **kw):
    translations = load_translations(web.ctx.get('lang', 'en'))
    value = (translations and translations.ungettext(s1, s2, _n))
    if not value:
        # fallback when translation is not provided
        if _n == 1:
            value = s1
        else:
            value = s2

    if a:
        return value % a
    elif kw:
        return value % kw
    else:
        return value

def gettext_territory(code):
    """Returns the territory name in the current locale.
    """
    locale = load_locale(web.ctx.get('lang', 'en'))
    return locale.territories.get(code, code)

gettext = GetText()
ugettext = gettext
lgettext = LazyGetText()
_ = gettext
