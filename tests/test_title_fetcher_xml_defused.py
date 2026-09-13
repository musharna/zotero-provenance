"""arXiv Atom feeds are remote input and must go through defusedxml.

Negative control: an entity-expansion payload is rejected. Positive control in the
same test: a plain document still parses, so a broken parser cannot read as "blocked".
"""

import pytest

from zotero_capture import title_fetcher

BOMB = (
    '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "xxxxxxxxxx">'
    '<!ENTITY y "&x;&x;&x;&x;&x;&x;&x;&x;&x;&x;">]><a>&y;</a>'
)


def test_atom_parser_rejects_entities_and_parses_plain():
    assert title_fetcher.ElementTree.fromstring("<a><b>1</b></a>").find("b").text == "1"
    with pytest.raises(Exception, match=r"(?i)entit"):
        title_fetcher.ElementTree.fromstring(BOMB)
