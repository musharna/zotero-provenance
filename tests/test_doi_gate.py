"""A captured DOI has to be a DOI, and it has to still stand.

Two separate checks, and the cheap one found live damage first.

**Syntax.** The live library held four doi.org URLs whose path is not a DOI at
all: `10.1/ABC` and `10.x` (placeholders typed in prose), `GSE12345` (a GEO
accession given a doi.org prefix by mistake), and a bare `…` — an ellipsis the
extractor lifted out of truncated text and filed as a source. `is_excluded` now
rejects them, which means `prune` sweeps the four already in the collection: the
same mechanism the RFC-2606 reserved-name rule used, where adding the rule
cleaned the backlog rather than requiring a second list of what counts as junk.

The rule is syntax only. It rejects strings that cannot be a DOI, and never asks
whether a well-formed one resolves — `doi.org` answers that, and a valid DOI that
404s is a dead source, not a malformed one.

**Standing.** `ghostcite` already does the byline cross-check and carries a
Retraction Watch snapshot, and it is audited, so this shells out to it. A second
implementation of a check like this is a second thing to be wrong.

The property that matters in the wrapper: an empty findings list means "clean"
only when the tool actually ran. A missing binary producing zero findings, read
as a pass, is how a broken check becomes a silent all-clear.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from zotero_capture.doi_gate import doi_of, format_gate_report, run_ghostcite
from zotero_capture.url_processing import is_excluded

REAL = "https://doi.org/10.1002/2016jg003520"


# --- syntax -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://doi.org/10.1/ABC",
        "https://doi.org/10.x",
        "https://doi.org/GSE12345",
        "https://doi.org/…",
        "https://doi.org/",
        "https://dx.doi.org/nonsense",
    ],
)
def test_a_doi_org_url_that_is_not_a_doi_is_excluded(url: str) -> None:
    assert is_excluded(url) is True


@pytest.mark.parametrize(
    "url",
    [
        REAL,
        "https://doi.org/10.1038/s41586-020-2649-2",
        "https://dx.doi.org/10.1109/5.771073",
        "https://doi.org/10.1002/9781119281269.ch1",
    ],
)
def test_a_real_doi_is_not_excluded(url: str) -> None:
    """Positive control. A rule that excluded every doi.org URL would pass every
    assertion above while destroying the most valuable rows in the library."""
    assert is_excluded(url) is False


def test_the_rule_does_not_reach_other_hosts() -> None:
    """It is a doi.org rule, not a path rule. `/10.x` elsewhere is just a path.

    Deliberately not spelled with `example.org`: the RFC-2606 reserved-name rule
    already excludes that for its own reasons, and the first draft of this test
    used it and passed for entirely the wrong reason.
    """
    assert is_excluded("https://fixturehost.org/10.x") is False


# --- extracting the DOI -----------------------------------------------------


def test_a_doi_url_yields_its_doi() -> None:
    assert doi_of(REAL) == "10.1002/2016jg003520"


def test_a_preprint_url_yields_its_embedded_doi() -> None:
    assert (
        doi_of("https://www.biorxiv.org/content/10.1101/2020.01.01.900001v1")
        == "10.1101/2020.01.01.900001v1"
    )


def test_an_ordinary_url_yields_nothing() -> None:
    """Guessing wider would send arbitrary path fragments to CrossRef and report
    whatever came back as a finding about a source nobody cited."""
    assert doi_of("https://example.org/papers/10.1234/whatever") is None
    assert doi_of("https://arxiv.org/abs/2401.00001") is None


# --- the wrapper ------------------------------------------------------------


def _runner(payload: dict, *, returncode: int = 0):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, returncode, stdout=json.dumps(payload), stderr=""
        )

    return run


def test_findings_are_returned() -> None:
    payload = {
        "summary": {"total": 1, "with_doi": 1, "retraction_source": "RW snapshot"},
        "findings": [{"doi": "10.1/x", "tier": "retraction", "message": "retracted"}],
    }

    result = run_ghostcite(["10.1/x"], runner=_runner(payload))

    assert len(result.findings) == 1
    assert result.checked == 1
    assert result.unavailable == ""


def test_a_missing_binary_is_not_a_clean_result() -> None:
    """The property. Zero findings from a tool that never ran is not a pass."""

    def missing(cmd, **kwargs):
        raise FileNotFoundError("ghostcite")

    result = run_ghostcite(["10.1/x"], runner=missing)

    assert result.findings == []
    assert "not installed" in result.unavailable
    body = "\n".join(format_gate_report(result, by_url={}))
    assert "not a clean result" in body


def test_empty_output_is_not_a_clean_result() -> None:
    def silent(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    result = run_ghostcite(["10.1/x"], runner=silent)

    assert "no output" in result.unavailable


def test_a_clean_corpus_reads_as_clean() -> None:
    """Positive control for the two above: when the tool DOES run and finds
    nothing, that must be reportable as a real result."""
    payload = {"summary": {"total": 2, "with_doi": 2}, "findings": []}

    result = run_ghostcite(["10.1/x", "10.2/y"], runner=_runner(payload))

    assert result.unavailable == "" and result.findings == []
    assert "not a clean result" not in "\n".join(format_gate_report(result, by_url={}))


def test_unresolvable_dois_are_reported_as_unknown_not_clean() -> None:
    """ghostcite dropping a DOI it could not resolve must not read as a pass for
    that DOI. The live corpus had exactly this: two submitted, one counted."""
    payload = {"summary": {"total": 2, "with_doi": 1}, "findings": []}

    result = run_ghostcite(["10.1/x", "10.1/bogus"], runner=_runner(payload))
    body = "\n".join(format_gate_report(result, by_url={}))

    assert "NOT checked   : 1" in body
    assert "not clean, just unknown" in body


def test_a_finding_names_the_library_item_not_just_the_doi() -> None:
    payload = {
        "summary": {"total": 1, "with_doi": 1},
        "findings": [{"doi": "10.1002/2016jg003520", "tier": "retraction"}],
    }

    result = run_ghostcite(["10.1002/2016jg003520"], runner=_runner(payload))
    body = "\n".join(format_gate_report(result, by_url={"10.1002/2016jg003520": REAL}))

    assert REAL in body


def test_no_dois_means_no_subprocess() -> None:
    def explode(cmd, **kwargs):
        raise AssertionError("ghostcite must not run with nothing to check")

    assert run_ghostcite([], runner=explode).submitted == 0
