"""Mock of openlibrary.core.ia module.
"""
import pytest
from openlibrary.core import ia

@pytest.fixture
def mock_ia(request, monkeypatch):
    """pytest funcarg to mock openlibrary.core.ia module.

        from openlibrary.core import ia

        def test_ia(mock_ia):
            assert ia.get_meta_xml("foo") == {}

            mock_ia.set_meta_xml("foo", {"collection": ["a", "b"]})
            assert ia.get_meta_xml("foo") == {"collection": ["a", "b"]}
    """
    metaxml = {}

    class IA:
        def set_meta_xml(self, itemid, meta):
            metaxml[itemid] = meta

        def get_meta_xml(self, itemid):
            return metaxml.get(itemid, {})

    mock_ia = IA()
    monkeypatch.setattr(ia, "get_meta_xml", ia.get_meta_xml)

    return mock_ia
