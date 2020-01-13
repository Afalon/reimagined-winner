from openlibrary.core import models

# this should be moved to openlibrary.core
from openlibrary.plugins.upstream.models import UnitParser

class MockSite:
    def get(self, key):
        return models.Thing(self, key, data={})

    def _get_backreferences(self, thing):
        return {}

class MockLendableEdition(models.Edition):
    def get_ia_collections(self):
        return ['lendinglibrary']

class MockPrivateEdition(models.Edition):
    def get_ia_collections(self):
        return ['lendinglibrary', 'georgetown-university-law-library-rr']


class TestEdition:
    def mock_edition(self, edition_class):
        data = {
            "key": "/books/OL1M",
            "type": {"key": "/type/edition"},
            "title": "foo"
        }
        return edition_class(MockSite(), "/books/OL1M", data=data)

    def test_url(self):
        e = self.mock_edition(models.Edition)
        assert e.url() == "/books/OL1M/foo"
        assert e.url(v=1) == "/books/OL1M/foo?v=1"
        assert e.url(suffix="/add-cover") == "/books/OL1M/foo/add-cover"

        data = {
            "key": "/books/OL1M",
            "type": {"key": "/type/edition"},
        }
        e = models.Edition(MockSite(), "/books/OL1M", data=data)
        assert e.url() == "/books/OL1M/untitled"

    def test_get_ebook_info(self):
        e = self.mock_edition(models.Edition)
        assert e.get_ebook_info() == {}

    def test_is_not_in_private_collection(self):
        e = self.mock_edition(MockLendableEdition)
        assert not e.is_in_private_collection()

    def test_can_borrow_cuz_not_in_private_collection(self):
        e = self.mock_edition(MockLendableEdition)
        assert e.can_borrow()

    def test_is_in_private_collection(self):
        e = self.mock_edition(MockPrivateEdition)
        assert e.is_in_private_collection()

    def test_can_not_borrow_cuz_in_private_collection(self):
        e = self.mock_edition(MockPrivateEdition)
        assert not e.can_borrow()


class TestAuthor:
    def test_url(self):
        data = {
            "key": "/authors/OL1A",
            "type": {"key": "/type/author"},
            "name": "foo"
        }

        e = models.Author(MockSite(), "/authors/OL1A", data=data)

        assert e.url() == "/authors/OL1A/foo"
        assert e.url(v=1) == "/authors/OL1A/foo?v=1"
        assert e.url(suffix="/add-photo") == "/authors/OL1A/foo/add-photo"

        data = {
            "key": "/authors/OL1A",
            "type": {"key": "/type/author"},
        }
        e = models.Author(MockSite(), "/authors/OL1A", data=data)
        assert e.url() == "/authors/OL1A/unnamed"


class TestSubject:
    def test_url(self):
        subject = models.Subject({
            "key": "/subjects/love"
        })
        assert subject.url() == "/subjects/love"
        assert subject.url("/lists") == "/subjects/love/lists"


class TestList:
    def test_owner(self):
        models.register_models()
        self._test_list_owner("/people/anand")
        self._test_list_owner("/people/anand-test")
        self._test_list_owner("/people/anand_test")

    def _test_list_owner(self, user_key):
        from openlibrary.mocks.mock_infobase import MockSite
        site = MockSite()
        list_key = user_key + "/lists/OL1L"

        self.save_doc(site, "/type/user", user_key)
        self.save_doc(site, "/type/list", list_key)

        list =  site.get(list_key)
        assert list is not None
        assert isinstance(list, models.List)

        assert list.get_owner() is not None
        assert list.get_owner().key == user_key

    def save_doc(self, site, type, key, **fields):
        d = {
            "key": key,
            "type": {"key": type}
        }
        d.update(fields)
        site.save(d)

class TestLibrary:
    def test_class(self, mock_site):
        mock_site.save({
            "key": "/libraries/ia",
            "type": {"key": "/type/library"}
        })
        doc = mock_site.get("/libraries/ia")
        assert doc.__class__.__name__ == "Library"

    def test_parse_ip_ranges(self):
        doc = models.Library(None, "/libraries/foo")
        def compare_ranges(test, expect):
            result = list(doc.parse_ip_ranges(test))
            assert result == expect
        compare_ranges("", [])
        compare_ranges("1.2.3.4", ["1.2.3.4"])
        compare_ranges("1.2.3.4", ["1.2.3.4"])
        compare_ranges("1.1.1.1\n2.2.2.2", ["1.1.1.1", "2.2.2.2"])
        compare_ranges("1.1.1.1-2.2.2.2", [("1.1.1.1", "2.2.2.2")])
        compare_ranges("1.1.1.1 # comment \n2.2.2.2", ["1.1.1.1", "2.2.2.2"])
        compare_ranges("1.1.1.1\n # comment \n2.2.2.2", ["1.1.1.1", "2.2.2.2"])
        compare_ranges("1.2.3.0/24", ["1.2.3.0/24"])
        compare_ranges("1.2.3.*", ["1.2.3.0/24"])
        compare_ranges("1.2.*.*", ["1.2.0.0/16"])
        compare_ranges("1.*.*.*", ["1.0.0.0/8"])
        compare_ranges("*", [])
        compare_ranges("*.1", [])
        compare_ranges("1.2.3-10.*", [("1.2.3.0", "1.2.10.255")])
        compare_ranges("1.2.3.", [("1.2.3.0", "1.2.3.255")])
        compare_ranges("1.1.", [])
        compare_ranges("1.2.3.1-254", [("1.2.3.1", "1.2.3.254")])
        compare_ranges("216.63.14.0/24\n207.193.121.0/24\n207.193.118.0/24", ["216.63.14.0/24", "207.193.121.0/24", "207.193.118.0/24"])
        compare_ranges("208.70.20-30.", [])

    def test_bad_ip_ranges(self):
        doc = models.Library(None, "/libraries/foo")
        def test_ranges(test, expect):
            result = doc.find_bad_ip_ranges(test)
            assert result == expect
        test_ranges("", [])
        test_ranges("1.2.3.4", [])
        test_ranges("1.1.1.1\n2.2.2.2", [])
        test_ranges("1.1.1.1-2.2.2.2", [])
        test_ranges("1.1.1.1 # comment \n2.2.2.2", [])
        test_ranges("1.1.1.1\n # comment \n2.2.2.2", [])
        test_ranges("1.2.3.0/24", [])
        test_ranges("1.2.3.*", [])
        test_ranges("1.2.*.*", [])
        test_ranges("1.*.*.*", [])
        test_ranges("*", ["*"])
        test_ranges("*.1", ["*.1"])
        test_ranges("1.2.3-10.*", [])
        test_ranges("1.2.3.", [])
        test_ranges("1.1.", ['1.1.'])
        test_ranges("1.2.3.1-254", [])
        test_ranges("216.63.14.0/24\n207.193.121.0/24\n207.193.118.0/24", [])
        test_ranges("1.2.3.4,2.3.4.5", ["1.2.3.4,2.3.4.5"])
        test_ranges("1.2-3.*", ["1.2-3.*"])

    def test_has_ip(self, mock_site):
        mock_site.save({
            "key": "/libraries/ia",
            "type": {"key": "/type/library"},
            "ip_ranges": "1.1.1.1\n2.2.2.0/24"
        })

        ia = mock_site.get("/libraries/ia")
        assert ia.has_ip("1.1.1.1")
        assert not ia.has_ip("1.1.1.2")

        assert ia.has_ip("2.2.2.10")
        assert not ia.has_ip("2.2.10.2")

        mock_site.save({
            "key": "/libraries/ia",
            "type": {"key": "/type/library"},
            "ip_ranges": "1.1.1.",
        })

        ia = mock_site.get("/libraries/ia")
        assert ia.has_ip("1.1.1.1")
        assert ia.has_ip("1.1.1.2")

        assert not ia.has_ip("2.2.2.10")
        assert not ia.has_ip("2.2.10.2")

        mock_site.save({
            "key": "/libraries/ia",
            "type": {"key": "/type/library"},
            "ip_ranges": "1.1.",
        })

        ia = mock_site.get("/libraries/ia")

        assert not ia.has_ip("2.2.2.2")
