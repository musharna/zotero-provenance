"""Ask whether a captured DOI is what it claims to be, and whether it stands.

A provenance library records what was consulted. It does not, on its own, notice
that one of those sources was retracted six months ago, or that the DOI points at
a different paper than the text around it implied. That is the failure a citation
audit exists to catch, and it is worth catching on the way in rather than at
submission.

`ghostcite` already does the hard part — CrossRef byline cross-check, a
Retraction Watch snapshot, optional PubMed/OpenAlex corroboration — and it is
audited. So this shells out to it rather than reimplementing a byline gate:
a second implementation of a check like this is a second thing to be wrong.

Unattended, like every other pass that touches the network in bulk. The Stop
hook never runs this.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# The doi.org forms capture actually stores. biorxiv/medrxiv carry the DOI in
# the path, which the title resolver already relies on.
_DOI_URL_RE = re.compile(
    r"^https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/\S+)$", re.IGNORECASE
)
_EMBEDDED_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s?#]+)")


def doi_of(url: str) -> str | None:
    """The DOI a captured URL carries, or None.

    Only doi.org URLs and the preprint hosts that embed a DOI in the path. A
    guess wider than that would send arbitrary path fragments to CrossRef and
    report whatever came back as a finding about a source nobody cited.
    """
    match = _DOI_URL_RE.match(url)
    if match:
        return match.group(1)
    host = url.split("/")[2].lower() if url.count("/") >= 2 else ""
    if host.endswith(("biorxiv.org", "medrxiv.org")):
        embedded = _EMBEDDED_DOI_RE.search(url)
        if embedded:
            return embedded.group(1)
    return None


@dataclass
class GateResult:
    submitted: int = 0
    checked: int = 0
    findings: list[dict] = field(default_factory=list)
    retraction_source: str = ""
    # Set when ghostcite could not be run at all, so an empty findings list is
    # never mistaken for a clean bill of health.
    unavailable: str = ""


def run_ghostcite(
    dois: list[str], *, runner=subprocess.run, max_rps: float = 2.0
) -> GateResult:
    """Feed DOIs to ghostcite and return its findings.

    `runner` is injected so the tests never touch the network. An empty findings
    list means "checked and clean" ONLY when `unavailable` is empty -- a missing
    binary and a clean corpus are otherwise the same output, which is the shape
    that turns a broken check into a silent pass.
    """
    result = GateResult(submitted=len(dois))
    if not dois:
        return result
    try:
        proc = runner(
            [
                "ghostcite",
                "--format",
                "doi",
                "--json",
                "--max-rps",
                str(max_rps),
                "-",
            ],
            input="\n".join(dois),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        result.unavailable = "ghostcite is not installed (pipx install ghostcite)"
        return result
    except Exception as e:  # a timeout, a killed process
        result.unavailable = f"ghostcite could not be run: {e}"
        return result

    if not (proc.stdout or "").strip():
        result.unavailable = (
            f"ghostcite produced no output (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:200]}"
        )
        return result
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        result.unavailable = "ghostcite output was not JSON"
        return result

    summary = payload.get("summary") or {}
    result.checked = int(summary.get("with_doi") or 0)
    result.retraction_source = summary.get("retraction_source") or ""
    result.findings = list(payload.get("findings") or [])
    return result


def format_gate_report(result: GateResult, *, by_url: dict[str, str]) -> list[str]:
    """`by_url` maps DOI -> the captured URL, so a finding names a library item."""
    if result.unavailable:
        return [
            f"could not check: {result.unavailable}",
            "No conclusion either way -- this is not a clean result.",
        ]

    lines = []
    for finding in result.findings:
        doi = finding.get("doi") or ""
        tier = finding.get("tier") or finding.get("kind") or "finding"
        detail = finding.get("message") or finding.get("detail") or ""
        lines.append(f"  [{tier}] {by_url.get(doi, doi)}")
        if detail:
            lines.append(f"          {detail[:200]}")

    lines.append(f"submitted     : {result.submitted}")
    lines.append(f"checked       : {result.checked}")
    lines.append(f"findings      : {len(result.findings)}")
    if result.submitted and result.checked < result.submitted:
        lines.append(
            f"NOT checked   : {result.submitted - result.checked} "
            f"(ghostcite could not resolve them; they are not clean, just unknown)"
        )
    if result.retraction_source:
        lines.append(f"retraction db : {result.retraction_source}")
    return lines
