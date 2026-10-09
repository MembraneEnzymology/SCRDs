#!/usr/bin/env python3
"""
scrd_autofill.py - from a DOI (or a local PDF / text file) to a Synthetic Cell
Reporting Datasheet draft.

Pipeline
    1. retrieve   metadata and, where possible, full text (Crossref, Unpaywall,
                  Europe PMC, bioRxiv) - or a local PDF / text file
    2. classify   score the text against SCRD 1 / 2 / 3 and pick the best fit
    3. extract    pull out the fields that can be found reliably
    4. build      assemble a draft in the exact shape the SCRD HTML tool loads
    5. validate   report every empty REQUIRED field and every invalid value

The script never invents values. A field is filled only when it was matched in
the text, and every filled field is listed in the provenance block with the
sentence it came from, so a human can check it. Everything else is left empty
for someone to complete in the form.

Usage
    python scrd_autofill.py 10.1111/febs.15337 --email you@university.nl
    python scrd_autofill.py --pdf paper.pdf  --scrd 3
    python scrd_autofill.py --text methods.txt --out drafts/
    python scrd_autofill.py 10.1038/s41467-022-29272-x --llm   # optional LLM pass

Exit codes
    0  draft written, no required field missing
    1  draft written, but required fields are missing  (warnings printed)
    2  could not retrieve enough text to attempt a draft
    3  usage / configuration error

Dependencies
    stdlib only for the core.  Optional:
        requests     nicer HTTP (falls back to urllib)
        pypdf        --pdf input
        anthropic    --llm pass
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import textwrap
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None
import urllib.parse
import urllib.request

UA = "scrd-autofill/1.0 (synthetic cell reporting datasheets; mailto:%s)"

# --------------------------------------------------------------------------
# 0. where the datasheet HTML files live
# --------------------------------------------------------------------------
# Point these at your copies of the datasheets. The schema is read straight out
# of the HTML, so the script follows the datasheets whenever they are revised -
# new fields, renamed vocabularies and changed units are picked up automatically.
SCRD_FILES = {
    1: "SCRD1_Functional_Proteins.html",
    2: "SCRD2_Nucleic_Acids_and_Cell-Free_Expression.html",
    3: "SCRD3_Vesicles.html",
}
SCRD_TITLES = {
    1: "Functional proteins",
    2: "Nucleic acids and cell-free expression",
    3: "Vesicles",
}


def load_schema(path: str) -> dict:
    """Pull the JSON schema out of an SCRD HTML file."""
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    m = re.search(
        r'<script id="schema" type="application/json">(.*?)</script>', html, re.S
    )
    if not m:
        raise ValueError(f"no schema block found in {path}")
    return json.loads(m.group(1))


def find_schema_dir(explicit: Optional[str]) -> str:
    for cand in [explicit, os.getcwd(), os.path.dirname(os.path.abspath(__file__))]:
        if cand and all(os.path.exists(os.path.join(cand, f)) for f in SCRD_FILES.values()):
            return cand
    raise SystemExit(
        "Could not find the three SCRD html files. Put them next to this script, "
        "or pass --schema-dir /path/to/them."
    )


# --------------------------------------------------------------------------
# 1. retrieval
# --------------------------------------------------------------------------
@dataclass
class Paper:
    doi: str = ""
    title: str = ""
    journal: str = ""
    year: str = ""
    authors: List[str] = field(default_factory=list)
    abstract: str = ""
    fulltext: str = ""
    source: str = ""          # where the full text came from
    open_access: Optional[bool] = None

    @property
    def first_author_surname(self) -> str:
        if not self.authors:
            return "Unknown"
        return re.sub(r"[^A-Za-z]", "", self.authors[0].split()[-1]) or "Unknown"

    @property
    def text(self) -> str:
        return (self.fulltext or self.abstract or "")

    @property
    def has_methods(self) -> bool:
        t = self.text.lower()
        return len(t) > 6000 and any(
            k in t for k in ("methods", "materials and methods", "experimental")
        )


def _get(url: str, email: str, as_json: bool = True, timeout: int = 30):
    headers = {"User-Agent": UA % (email or "unknown@example.org")}
    try:
        if requests is not None:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code != 200:
                return None
            return r.json() if as_json else r.text
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if as_json else raw
    except Exception:
        return None


def normalise(text: str) -> str:
    """One long line of text: line breaks inside sentences otherwise defeat every
    pattern ('200 nm polycarbonate\\nfilter'). Also unify the common unit spellings."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("‐", "-").replace("‑", "-").replace("–", "-")
    text = re.sub(r"-\s*\n\s*", "", text)          # de-hyphenate across line breaks
    text = re.sub(r"\s+", " ", text)
    return text


def strip_tags(xml: str) -> str:
    xml = re.sub(r"<(ref-list|back|fig|table-wrap)\b.*?</\1>", " ", xml, flags=re.S)
    xml = re.sub(r"<[^>]+>", " ", xml)
    xml = unicodedata.normalize("NFKC", xml)
    return re.sub(r"[ \t ]+", " ", xml)


def fetch_crossref(doi: str, email: str, paper: Paper) -> None:
    data = _get(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}", email)
    if not data:
        return
    msg = data.get("message", {})
    paper.title = (msg.get("title") or [""])[0]
    paper.journal = (msg.get("container-title") or [""])[0]
    parts = (msg.get("issued", {}).get("date-parts") or [[]])[0]
    paper.year = str(parts[0]) if parts else ""
    paper.authors = [
        " ".join(filter(None, [a.get("given"), a.get("family")]))
        for a in msg.get("author", [])
    ]
    paper.abstract = strip_tags(msg.get("abstract", "")) if msg.get("abstract") else ""


def fetch_europepmc(doi: str, email: str, paper: Paper) -> bool:
    """Europe PMC: metadata search, then the open-access full text if there is one."""
    q = urllib.parse.quote(f'DOI:"{doi}"')
    data = _get(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        f"?query={q}&resultType=core&format=json",
        email,
    )
    if not data:
        return False
    hits = data.get("resultList", {}).get("result", [])
    if not hits:
        return False
    hit = hits[0]
    paper.title = paper.title or hit.get("title", "")
    paper.abstract = paper.abstract or hit.get("abstractText", "")
    paper.open_access = hit.get("isOpenAccess") == "Y"
    if hit.get("hasTextMinedTerms") or hit.get("inEPMC") == "Y":
        pmcid = hit.get("pmcid")
        if pmcid:
            xml = _get(
                f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
                email,
                as_json=False,
            )
            if xml and len(xml) > 5000:
                paper.fulltext = strip_tags(xml)
                paper.source = f"Europe PMC {pmcid}"
                return True
    return False


def fetch_biorxiv(doi: str, email: str, paper: Paper) -> bool:
    """bioRxiv/medRxiv: find a preprint of this DOI (often the only open version)."""
    data = _get(f"https://api.biorxiv.org/details/biorxiv/{doi}", email)
    if not data or not data.get("collection"):
        return False
    rec = data["collection"][-1]
    paper.title = paper.title or rec.get("title", "")
    paper.abstract = paper.abstract or rec.get("abstract", "")
    paper.source = f"bioRxiv preprint {rec.get('doi')} (CHECK against the published version)"
    return True


def fetch_unpaywall(doi: str, email: str, paper: Paper) -> Optional[str]:
    if not email:
        return None
    data = _get(f"https://api.unpaywall.org/v2/{doi}?email={urllib.parse.quote(email)}", email)
    if not data:
        return None
    paper.open_access = data.get("is_oa", paper.open_access)
    loc = data.get("best_oa_location") or {}
    return loc.get("url_for_pdf") or loc.get("url")


def pdf_to_text(path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            raise SystemExit("Reading a PDF needs pypdf:  pip install pypdf")
    reader = PdfReader(path)
    return normalise("\n".join(p.extract_text() or "" for p in reader.pages))


def retrieve(args) -> Paper:
    paper = Paper(doi=args.doi or "")
    if args.text:
        paper.fulltext = normalise(open(args.text, encoding="utf-8").read())
        paper.source = f"local text file {os.path.basename(args.text)}"
        paper.title = paper.title or os.path.splitext(os.path.basename(args.text))[0]
    elif args.pdf:
        paper.fulltext = pdf_to_text(args.pdf)
        paper.source = f"local PDF {os.path.basename(args.pdf)}"
        paper.title = paper.title or os.path.splitext(os.path.basename(args.pdf))[0]
    if args.doi:
        fetch_crossref(args.doi, args.email, paper)
        if not paper.fulltext:
            if not fetch_europepmc(args.doi, args.email, paper):
                fetch_biorxiv(args.doi, args.email, paper)
                oa = fetch_unpaywall(args.doi, args.email, paper)
                if oa:
                    paper.source = (paper.source + " | " if paper.source else "") + \
                        f"open version available at {oa} (download it and pass --pdf)"
    return paper


# --------------------------------------------------------------------------
# 2. classification
# --------------------------------------------------------------------------
# Weighted terms. Weight 3 = only this datasheet, 2 = strong, 1 = supporting.
CLASSIFIER: Dict[int, List[Tuple[str, int]]] = {
    1: [
        (r"\bk ?cat\b", 3), (r"\bK ?M\b", 3), (r"\bV ?MAX\b", 3), (r"turnover number", 3),
        (r"michaelis", 3), (r"\bk ?on\b", 2), (r"\bk ?off\b", 2), (r"\bK ?[di]\b", 2),
        (r"specific activity", 2), (r"enzyme assay", 2), (r"active[- ]site titration", 3),
        (r"SEC-?MALS", 2), (r"oligomeric state", 2), (r"purified protein", 1),
        (r"substrate inhibition", 2), (r"antiport|symport|uniport|transporter", 2),
        (r"critical concentration", 3), (r"persistence length", 3), (r"polymeriz", 2),
        (r"co-?sedimentation", 2), (r"binding stoichiometry", 2),
    ],
    2: [
        (r"PURE ?frex|PURExpress|PURE system", 3), (r"myTXTL|TX-?TL", 3),
        (r"cell-?free (protein )?(expression|synthesis)", 3), (r"\bS30\b|\bS12\b", 3),
        (r"lysate", 2), (r"ribosom", 2), (r"T7 (RNA )?polymerase", 2),
        (r"plasmid|linear template|PCR product", 1), (r"ribosome[- ]binding site|\bRBS\b", 3),
        (r"promoter", 2), (r"transcription", 2), (r"translation", 2),
        (r"sfGFP|deGFP|mCherry|luciferase", 1), (r"creatine phosphate", 2),
        (r"protein yield", 2), (r"in vitro transcription", 2),
    ],
    3: [
        (r"\bGUVs?\b", 3), (r"\bLUVs?\b", 3), (r"\bSUVs?\b", 3), (r"liposom", 3),
        (r"vesicl", 3), (r"proteoliposom", 3), (r"extrusion|extruded", 3),
        (r"electroformation", 3), (r"emulsion transfer|inverted emulsion|cDICE", 3),
        (r"gel-?assisted swelling", 3), (r"\bDOPC\b|\bPOPC\b|\bDOPG\b|\bPOPG\b|\bDOPE\b", 2),
        (r"lipid (mixture|composition|film)", 2), (r"permeability coefficient", 3),
        (r"encapsulat", 2), (r"lamellarit|unilamellar", 3), (r"cryo-?EM|cryo-?TEM", 1),
        (r"dynamic light scattering|\bDLS\b", 2), (r"supported lipid bilayer", 1),
        (r"polydispersity|\bPDI\b", 2), (r"osmolality|osmotic", 1),
    ],
}


def pretty(pattern: str) -> str:
    """Turn a classifier regex back into something a reader recognises."""
    t = pattern.replace("\\b", "").replace("\\s", " ")
    t = re.sub(r"\(\?:|\[|\]|\)|\(|\\", "", t)
    t = re.sub(r"[?+*^$]", "", t)
    t = t.replace("|", " / ")
    return re.sub(r"\s{2,}", " ", t).strip(" /")


@dataclass
class Verdict:
    scores: Dict[int, int]
    primary: int
    secondary: List[int]
    hits: Dict[int, List[str]]

    def explain(self) -> str:
        out = []
        for s in sorted(self.scores, key=lambda k: -self.scores[k]):
            terms = ", ".join(sorted(set(self.hits[s]))[:8])
            out.append(f"   SCRD {s} ({SCRD_TITLES[s]}): score {self.scores[s]:>4}   {terms}")
        return "\n".join(out)


def classify(paper: Paper, min_score: int = 10) -> Verdict:
    text = paper.text
    scores, hits = {}, {}
    for scrd, terms in CLASSIFIER.items():
        total, found = 0, []
        for pat, w in terms:
            n = len(re.findall(pat, text, re.I))
            if n:
                total += w * min(n, 8)        # cap so one word cannot dominate
                found.append(pretty(pat))
        scores[scrd], hits[scrd] = total, found
    primary = max(scores, key=lambda k: scores[k])
    best = scores[primary]
    # A second datasheet is generated on its own evidence, not relative to the winner:
    # a cell-free reaction inside vesicles is genuinely two datasheets, and the vesicle
    # half usually scores higher simply because it has more distinctive vocabulary.
    secondary = [s for s in scores
                 if s != primary and scores[s] >= max(min_score, 0.20 * best)]
    secondary.sort(key=lambda k: -scores[k])
    return Verdict(scores, primary, secondary, hits)


# --------------------------------------------------------------------------
# 3. extraction
# --------------------------------------------------------------------------
@dataclass
class Found:
    value: str
    evidence: str
    how: str = "regex"


def sentence_around(text: str, m: re.Match, width: int = 180) -> str:
    a = max(0, m.start() - width // 2)
    b = min(len(text), m.end() + width // 2)
    return " ".join(text[a:b].split())


def first(text: str, pattern: str, group: int = 0, flags=re.I) -> Optional[Found]:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    val = (m.group(group) or "").strip()
    return Found(val, sentence_around(text, m)) if val else None


def all_matches(text: str, pattern: str, flags=re.I) -> List[re.Match]:
    return list(re.finditer(pattern, text, flags))


NUM = r"[-+]?\d+(?:[.,]\d+)?(?:\s*[x×]\s*10\^?-?\d+|[eE][-+]?\d+)?"
PM = r"(?:±|\+/-|\+-)"

COMMON_PATTERNS = {
    # degree sign is frequently lost in PDF extraction, so it is optional
    "Temperature_C": (rf"\bat\s+({NUM})\s*(?:\u00b0\s*|deg(?:rees)?\s*)?C\b", 1),
    "pH": (r"\bpH\s*(\d(?:\.\d+)?)", 1),
    "Buffer": (rf"({NUM}\s*mM\s+(?:potassium|sodium|K|Na)?[- ]?"
               rf"(?:phosphate|KPi|NaPi|HEPES[-\w]*|Tris[-\w/]*|MOPS|MES|PIPES|bicine|tricine)"
               rf"[^,.;()]{{0,30}})", 1),
    "Instrument": (r"((?:Applied Photophysics|Jasco|BioTek|Tecan|BMG|Wyatt|Zeiss|Nikon|Leica|"
                   r"Agilent|Shimadzu|PerkinElmer|Thermo|Bruker|Malvern|Avestin|Hamamatsu)"
                   r"[^,.;()]{0,45})", 1),
    "Raw_data": (r"(10\.5281/zenodo\.\d+|10\.6084/m9\.figshare\.\d+|"
                 r"https?://github\.com/[\w./-]+)", 1),
}


def extract_common(text: str) -> Dict[str, Found]:
    out = {}
    for key, (pat, grp) in COMMON_PATTERNS.items():
        f = first(text, pat, grp)
        if f:
            out[key] = f
    return out


# ---- SCRD 1 ----------------------------------------------------------------
PARAM_PATTERNS = [
    ("Km", rf"\bK\s?_?M\b[^.]{{0,40}}?of\s+({NUM})\s*(µM|uM|mM|nM|M)\s*(?:{PM}\s*({NUM}))?"),
    ("Km", rf"\bK\s?_?M\b[^.]{{0,25}}?({NUM})\s*{PM}\s*({NUM})\s*(µM|uM|mM|nM|M)"),
    ("kcat", rf"\bk\s?_?cat\b[^.]{{0,40}}?({NUM})\s*{PM}\s*({NUM})\s*s\^?-?1"),
    ("kcat", rf"\bk\s?_?cat\b[^.]{{0,40}}?of\s+({NUM})\s*s\^?-?1"),
    ("Vmax", rf"\bV\s?_?MAX\b[^.]{{0,40}}?({NUM})\s*{PM}\s*({NUM})\s*"
              rf"((?:µmol|umol|nmol|µM|uM|mM)(?:[\s·./]*(?:min|s|h)\s*\^?-?1?)?"
              rf"(?:[\s·./]*mg\s*\^?-?1?)?)"),
    ("Kd", rf"\bK\s?_?[dD]\b[^.]{{0,40}}?({NUM})\s*(µM|uM|mM|nM|pM)"),
    ("Ki", rf"\bK\s?_?[iI]\b[^.]{{0,40}}?({NUM})\s*(µM|uM|mM|nM)"),
]


def extract_scrd1(text: str, paper: Paper) -> Tuple[Dict[str, Found], List[Dict[str, Found]]]:
    enz: Dict[str, Found] = {}
    f = first(text, r"\bEC\s*(\d+\.\d+\.\d+\.\d+)", 1)
    if f:
        enz["EC_number"] = f
    f = first(text, r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9])\b", 1, 0)
    if f:
        enz["UniProt_ID"] = f
    f = first(text, r"\b((?:Escherichia|Lactococcus|Bacillus|Saccharomyces|Thermus|Pseudomonas|"
                    r"Streptococcus|Enterococcus|Pyrococcus|Halobacterium)\s+\w+(?:\s+[A-Z0-9][\w-]*)?)", 1)
    if f:
        enz["Source_organism"] = f
    f = first(text, r"expressed in\s+([A-Z][\w.]*\s+\w+[^,.;()]{0,30})", 1)
    if f:
        enz["Production_host"] = f
    if re.search(r"His[- ]?tag|6\s?x\s?His|10\s?x\s?His|poly-?histidine", text, re.I):
        m = re.search(r"(N-|C-)?terminal\s+(\d{1,2})?\s?His", text, re.I)
        side = (m.group(1) or "").upper() if m else ""
        enz["Affinity_tag"] = Found(
            "N-terminal His-tag" if side.startswith("N") else
            ("C-terminal His-tag" if side.startswith("C") else "other"),
            sentence_around(text, m) if m else "His-tag mentioned")
    if re.search(r"A ?280|extinction coefficient", text, re.I):
        enz["Concentration_method"] = Found(
            "A280 with calculated extinction coefficient",
            "A280 / extinction coefficient mentioned - CHECK whether it was calculated or measured")
    elif re.search(r"\bBradford\b", text, re.I):
        enz["Concentration_method"] = Found("Bradford", "Bradford assay mentioned")
    if re.search(r"SEC-?MALS|multi-?angle light scattering", text, re.I):
        enz["Purity_method"] = Found("size-exclusion chromatography",
                                     "SEC / SEC-MALS mentioned")
    if re.search(r"active[- ]site titration", text, re.I):
        enz["Active_fraction_method"] = Found("active-site titration", "active-site titration mentioned")
    else:
        enz["Active_fraction"] = Found("not determined",
                                       "no active-site titration found in the text - CHECK")
    cls = "enzyme"
    if re.search(r"antiport|symport|uniport|transport(er|ed)\b", text, re.I):
        cls = "transporter"
    if re.search(r"critical concentration|persistence length|filament|polymeriz", text, re.I):
        cls = "structural protein"
    enz["Protein_class"] = Found(cls, f"classified from the text as '{cls}' - CHECK")

    common = extract_common(text)
    meas: List[Dict[str, Found]] = []
    seen = set()
    for pname, pat in PARAM_PATTERNS:
        for m in all_matches(text, pat):
            groups = [g for g in m.groups() if g]
            value = groups[0]
            unit = next((g for g in groups[1:] if re.match(r"[µun]?[Mm]|s|µmol|umol|nmol", g)), "")
            unc = next((g for g in groups[1:] if re.fullmatch(NUM, g or "")), "")
            key = (pname, value, unit)
            if key in seen:
                continue
            seen.add(key)
            rec = {
                "Parameter": Found(pname, sentence_around(text, m)),
                "Value": Found(value.replace(",", "."), sentence_around(text, m)),
            }
            if unit:
                rec["Unit"] = Found(unit.replace("uM", "µM").replace("umol", "µmol"),
                                    sentence_around(text, m))
            elif pname == "kcat":
                rec["Unit"] = Found("s^-1", sentence_around(text, m))
            if unc:
                rec["Uncertainty_value"] = Found(unc.replace(",", "."), sentence_around(text, m))
            for k, v in common.items():
                if k in ("Temperature_C", "pH", "Buffer", "Instrument", "Raw_data"):
                    rec.setdefault(k, v)
            n = first(text, r"\bn\s*=\s*(\d+)", 1)
            if n:
                rec["n_biological_replicates"] = Found(
                    n.value, n.evidence + "  [CHECK: biological or technical?]")
            meas.append(rec)
    return enz, meas[:12]


# ---- SCRD 3 ----------------------------------------------------------------
FORMATION_MAP = [
    (r"electroformation", "electroformation"),
    (r"emulsion transfer|inverted emulsion|cDICE|double emulsion", "emulsion transfer (inverted emulsion)"),
    (r"gel-?assisted swelling|PVA|agarose swelling", "gel-assisted swelling"),
    (r"glass bead", "glass-bead method"),
    (r"microfluidic|octanol-assisted|jetting", "microfluidics"),
    (r"detergent removal|Bio-?Beads|destabili[sz]ed with (Triton|DDM)", "detergent removal"),
    (r"extrud|extrusion", "film hydration + freeze-thaw + extrusion"),
]


def extract_scrd3(text: str, schema: dict) -> Tuple[Dict[str, Found], List[Dict[str, Found]], List[dict]]:
    prep: Dict[str, Found] = {}
    sys_vocab = schema["vocab"].get("system_type", [])
    if re.search(r"\bGUVs?\b|giant unilamellar", text, re.I):
        pick = next((v for v in sys_vocab if v.startswith("GUV")), None)
    elif re.search(r"\bSUVs?\b|small unilamellar", text, re.I):
        pick = next((v for v in sys_vocab if v.startswith("SUV")), None)
    else:
        pick = next((v for v in sys_vocab if v.startswith("LUV")), None)
    if pick:
        prep["System_type"] = Found(pick, "vesicle class inferred from the text - CHECK")

    for pat, val in FORMATION_MAP:
        if re.search(pat, text, re.I):
            prep["Formation_method"] = Found(val, first(text, pat).evidence)
            break

    f = first(text, r"(\d{2,4})\s*[- ]?nm\s+(?:pore[- ]size\s+)?(?:polycarbonate\s+)?(?:filter|membrane)", 0)
    if f:
        size = re.search(r"(\d{2,4})", f.value).group(1)
        prep["Extrusion_filter"] = Found(f"Polycarbonate, {size} nm", f.evidence)
    f = first(text, r"(?:extruded|passed)\s+(\d{1,2})\s*(?:times|x)", 1)
    if f:
        prep["Extrusion_passes"] = Found(f"{f.value} passes", f.evidence)
    f = first(text, r"(\w+|\d+)\s+(?:cycles of )?freeze[-– ]?thaw", 1)
    if f:
        prep["Freeze_thaw_cycles"] = Found(f"{f.value} cycles", f.evidence)
    f = first(text, r"(?:stored|storage)[^.]{0,80}?(-?\d{1,2}\s*°C|liquid nitrogen)", 0)
    if f:
        prep["Storage_conditions"] = Found(" ".join(f.value.split()), f.evidence)

    # lipid composition: names from the datasheet's own vocabulary
    lipids: List[dict] = []
    vocab = schema["vocab"].get("lipid", [])
    src_default = schema.get("lipid_source_default", {})
    vendor = first(text, r"(Avanti Polar Lipids[^,.;()]{0,40}|Sigma-?Aldrich[^,.;()]{0,25})", 1)
    for name in sorted(vocab, key=len, reverse=True):
        if name in ("other",):
            continue
        pat = rf"(?:(\d{{1,3}}(?:\.\d+)?)\s*(?:mol ?%|%)\s*(?:of\s*)?{re.escape(name)})" \
              rf"|(?:{re.escape(name)}[^.;]{{0,25}}?(\d{{1,3}}(?:\.\d+)?)\s*(?:mol ?%|%))"
        m = re.search(pat, text)
        if not m and re.search(rf"\b{re.escape(name)}\b", text):
            m = re.search(rf"\b{re.escape(name)}\b", text)
            mol = ""
        else:
            mol = (m.group(1) or m.group(2)) if m else ""
        if m:
            lipids.append({
                "name": name,
                "src": src_default.get(name, "other"),
                "mol": mol,
                "vendor": vendor.value if vendor else "",
                "purity": "",
                "conc": "",
                "unit": "",
            })
    if len(lipids) > 8:          # a review-like text mentioning everything: do not guess
        lipids = []

    common = extract_common(text)
    meas: List[Dict[str, Found]] = []
    pvocab = schema["vocab"].get("parameter", [])

    def new_meas(param: str, **kw) -> Dict[str, Found]:
        rec = {"Parameter": Found(param, kw.pop("ev", ""))}
        rec.update(kw)
        for k in ("Temperature_C", "pH", "Buffer", "Instrument", "Raw_data"):
            if k in common and k in ("Temperature_C", "pH", "Buffer", "Instrument", "Raw_data"):
                rec.setdefault(k, common[k])
        return rec

    m = re.search(rf"(?:mean |average )?(?:diameter|radius)[^.]{{0,40}}?({NUM})\s*(nm|µm|um)", text, re.I)
    if m and "Mean diameter" in pvocab:
        rec = new_meas("Mean diameter", ev=sentence_around(text, m))
        rec["Value"] = Found(m.group(1), sentence_around(text, m))
        rec["Unit"] = Found(m.group(2).replace("um", "µm"), sentence_around(text, m))
        if "radius" in m.group(0).lower():
            rec["Parameter"] = Found("other", "the text reports a RADIUS, not a diameter - CHECK")
        meas.append(rec)
    m = re.search(rf"(?:permeability coefficient|\bP\b)[^.]{{0,40}}?({NUM})\s*(?:cm\s*(?:/|·)?\s*s|cm s\^?-?1)", text, re.I)
    if m and "Permeability coefficient (P)" in pvocab:
        rec = new_meas("Permeability coefficient (P)", ev=sentence_around(text, m))
        rec["Value"] = Found(m.group(1), sentence_around(text, m))
        rec["Unit"] = Found("cm s^-1", sentence_around(text, m))
        meas.append(rec)
    m = re.search(rf"(?:PDI|polydispersity index)[^.]{{0,25}}?({NUM})", text, re.I)
    if m and "Polydispersity index (PDI)" in pvocab:
        rec = new_meas("Polydispersity index (PDI)", ev=sentence_around(text, m))
        rec["Value"] = Found(m.group(1), sentence_around(text, m))
        meas.append(rec)
    if not meas:
        meas.append(new_meas(pvocab[0] if pvocab else "", ev="no value found; empty card for manual entry"))
    return prep, meas, lipids


# ---- SCRD 2 ----------------------------------------------------------------
def extract_scrd2(text: str, schema: dict) -> Tuple[Dict[str, Found], Dict[str, Found], Dict[str, Found], Dict[str, Found]]:
    sysb: Dict[str, Found] = {}
    kit = first(text, r"(PURE ?frex ?[\d.]*|PURExpress|myTXTL|TXTL [\w.]+)", 1)
    if kit:
        sysb["System_architecture"] = Found("Reconstituted (PURE-type)" if "PURE" in kit.value
                                            else "Extract-based (lysate)", kit.evidence)
        sysb["System_source"] = Found("Commercial", kit.evidence)
        sysb["System_name"] = Found(kit.value, kit.evidence)
    elif re.search(r"\bS30\b|\bS12\b|cell[- ]free extract|lysate", text, re.I):
        sysb["System_architecture"] = Found("Extract-based (lysate)", "extract / lysate mentioned")
        sysb["System_source"] = Found("Laboratory-prepared", "no commercial kit name found - CHECK")
        f = first(text, r"\b(S30|S12)\b", 1)
        if f:
            sysb["Extract_type"] = f
    f = first(text, r"\b(Escherichia coli [A-Z][\w()Δ-]*(?: ?\(DE3\))?)", 1)
    if f:
        sysb["Source_organism_and_strain"] = f
    for pat, val in [(r"sonicat", "sonication"), (r"bead beat", "bead beating"),
                     (r"French press|homogeni[sz]er|high-?pressure disrupt", "French press or homogeniser"),
                     (r"freeze-?thaw lysis", "freeze-thaw lysis")]:
        if re.search(pat, text, re.I):
            sysb["Cell_disruption"] = Found(val, first(text, pat).evidence)
            break
    if re.search(r"creatine phosphate", text, re.I):
        sysb["Energy_regeneration_system"] = Found("creatine phosphate with creatine kinase",
                                                   first(text, r"creatine phosphate").evidence)
    elif re.search(r"phosphoenolpyruvate|\bPEP\b", text):
        sysb["Energy_regeneration_system"] = Found("phosphoenolpyruvate (PEP) with pyruvate kinase",
                                                   "PEP mentioned")
    elif re.search(r"3-?phosphoglycerate|3-?PGA", text, re.I):
        sysb["Energy_regeneration_system"] = Found("3-phosphoglycerate (3-PGA), enzymes from the extract",
                                                   "3-PGA mentioned")
    if re.search(r"T7 (RNA )?polymerase", text, re.I):
        sysb["Transcription_mode"] = Found("added T7 RNA polymerase", "T7 polymerase mentioned")

    tpl: Dict[str, Found] = {"Template_ID": Found("T1", "assigned by the script")}
    if re.search(r"linear (template|dsDNA)|PCR product", text, re.I):
        tpl["Template_type"] = Found("linear dsDNA or PCR product", "linear template mentioned")
    elif re.search(r"plasmid", text, re.I):
        tpl["Template_type"] = Found("supercoiled plasmid DNA",
                                     "plasmid mentioned - CHECK supercoiled vs relaxed")
    if re.search(r"T7 promoter|pT7|T7 g10", text, re.I):
        tpl["Promoter"] = Found("T7 promoter", "T7 promoter mentioned")
    f = first(text, r"(Addgene[ #]*\d+|GenBank [A-Z]{1,2}\d{5,8})", 1)
    if f:
        tpl["Sequence_identifier"] = f
    f = first(text, rf"({NUM})\s*(nM|ng\s*(?:/|µ?L\^?-?1|µL\^?-?1)?)\s*(?:of\s*)?(?:plasmid|template|DNA)", 0)
    if f:
        tpl["_final_concentration"] = f

    cond: Dict[str, Found] = {"Condition_ID": Found("C1", "assigned by the script")}
    common = extract_common(text)
    if "Temperature_C" in common:
        cond["Temperature"] = Found(common["Temperature_C"].value + " °C",
                                    common["Temperature_C"].evidence)
    if "pH" in common:
        cond["pH"] = Found(common["pH"].value, common["pH"].evidence)
    f = first(text, rf"({NUM}\s*µ?[uL]?L)\s+(?:reaction|total volume)", 1)
    if f:
        cond["Reaction_volume"] = f
    f = first(text, rf"(?:Mg\(OAc\)2|magnesium acetate|MgCl2|Mg2\+)[^.;]{{0,25}}?({NUM}\s*mM)", 0)
    if f:
        cond["Magnesium"] = Found(" ".join(f.value.split()), f.evidence)
    f = first(text, rf"(?:potassium glutamate|K-?glutamate|KCl)[^.;]{{0,25}}?({NUM}\s*mM)", 0)
    if f:
        cond["Potassium"] = Found(" ".join(f.value.split()), f.evidence)

    out: Dict[str, Found] = {}
    f = first(text, rf"(?:yield|produced|synthesi[sz]ed)[^.]{{0,60}}?({NUM})\s*(mg\s*/?\s*mL|µg\s*/?\s*mL|µM|nM)", 0)
    if f:
        m = re.search(rf"({NUM})\s*(mg\s*/?\s*mL|µg\s*/?\s*mL|µM|nM)", f.value)
        out["Value"] = Found(m.group(1), f.evidence)
        unit = m.group(2).replace("/", " ").replace("  ", " ").strip()
        unit = {"mg mL": "mg mL^-1", "µg mL": "µg mL^-1"}.get(unit, unit)
        out["Unit"] = Found(unit, f.evidence)
    return sysb, tpl, cond, out



# --------------------------------------------------------------------------
# 3a. units
# --------------------------------------------------------------------------
# Papers write the same unit in a dozen ways: uM, µM, mM-1 s-1, umol/min/mg,
# cm s^-1, cm/s, nmol.min-1.mg-1. The datasheets accept exactly one spelling.
# Everything is canonicalised and then matched against the datasheet's OWN
# vocabulary, so a revised unit list is followed without touching this code.

UNIT_ALIASES = {
    "molar": "M", "millimolar": "mM", "micromolar": "\u00b5M", "nanomolar": "nM",
    "sec^-1": "s^-1", "second^-1": "s^-1", "per second": "s^-1",
    "degrees celsius": "\u00b0C", "a.u": "a.u.", "au": "a.u.",
    "dimensionless": "dimensionless", "fold": "fold",
}


def canon_unit(raw: str) -> str:
    """One canonical spelling: micro sign, exponent notation, single spaces."""
    u = unicodedata.normalize("NFKC", str(raw)).strip().strip(".,;")
    if not u:
        return ""
    u = u.replace("\u03bc", "\u00b5")                      # Greek mu -> micro sign
    u = re.sub(r"\bu(?=M\b|mol|g\b|L\b|m\b|s\b)", "\u00b5", u)   # uM, umol, ug, uL, um, us
    u = u.replace("\u00b7", " ").replace("*", " ")
    if not u.lower().startswith("a.u"):                 # 'nmol.min-1.mg-1'
        u = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z])", " ", u)
    if not u.lstrip().startswith("%"):                  # keep '% w/v' intact
        if "/" in u:
            parts = [x.strip() for x in u.split("/") if x.strip()]
            u = parts[0] + "".join(f" {x}^-1" for x in parts[1:])
    u = re.sub(r"(?<=[A-Za-z\)])\s*[-\u2212\u2013]\s*1\b", "^-1", u)   # s-1 -> s^-1
    u = re.sub(r"\^\s*[-\u2212]\s*", "^-", u)
    u = re.sub(r"\s+", " ", u).strip()
    return UNIT_ALIASES.get(u.lower(), u)


def resolve_unit(raw: str, vocab: List[str]) -> Tuple[str, str, str]:
    """Map a unit found in the text onto the datasheet vocabulary.

    Returns (value, specify, note).
      value    - an entry of the vocabulary, or 'other', or ''
      specify  - the original spelling, when 'other' is used
      note     - what happened, for the provenance file
    """
    if not raw:
        return "", "", ""
    target = canon_unit(raw)
    table = {canon_unit(v): v for v in vocab}
    if target in table:
        return table[target], "", f"unit {raw!r} recognised as {table[target]!r}"
    low = {k.lower(): v for k, v in table.items()}
    if target.lower() in low:                            # mM^-1 S^-1 vs mM^-1 s^-1
        hit = low[target.lower()]
        return hit, "", f"unit {raw!r} matched {hit!r} ignoring case"
    if "other" in vocab:
        return ("other", target,
                f"unit {raw!r} (canonical: {target!r}) is not in this datasheet's list; "
                f"entered as 'other' with the original spelling - CHECK")
    return "", "", f"unit {raw!r} is not in this datasheet's list and 'other' is not allowed; left empty"


def unit_allowed_for(schema: dict, field: str, scope: dict) -> Optional[List[str]]:
    """The datasheets narrow the unit list by parameter (a Km cannot be in s^-1).
    Read that rule straight out of the schema's own 'filters' block."""
    for flt in schema.get("filters", []) or []:
        if flt.get("field") == field:
            return (flt.get("map") or {}).get(scope.get(flt.get("on"), ""), None)
    return None


def fix_units(block: Iterable[dict], scope: dict, schema: dict, prov: dict, where: str) -> None:
    """Normalise every unit-like select in a record, and check it against the
    parameter it belongs to."""
    for f in block:
        if "key" not in f or f.get("type") != "select":
            continue
        voc = f.get("vocab", "")
        if "unit" not in voc:
            continue
        raw = scope.get(f["key"], "")
        if not raw:
            continue
        value, specify, note = resolve_unit(raw, schema["vocab"].get(voc, []))
        scope[f["key"]] = value
        if specify:
            scope[f["key"] + "_other"] = specify
        allowed = unit_allowed_for(schema, f["key"], scope)
        if value and allowed and value not in allowed and value != "other":
            note += (f"  WARNING: {value!r} is not an expected unit for "
                     f"{scope.get('Parameter', 'this parameter')!r} "
                     f"(expected one of {allowed}) - CHECK the parameter and the unit")
        prov[f"{where}.{f['key']}"] = {"value": value, "how": "unit-normalised", "evidence": note}


# --------------------------------------------------------------------------
# 4. build the draft in the shape the SCRD tool loads
# --------------------------------------------------------------------------
def blank(block: Iterable[dict]) -> Dict[str, Any]:
    rec = {}
    for f in block:
        if "key" not in f or f.get("type") == "derived":
            continue
        rec[f["key"]] = [] if f.get("type") == "multi" else ""
    return rec


def apply(rec: Dict[str, Any], found: Dict[str, Found], prov: Dict[str, Any], where: str) -> None:
    for k, v in found.items():
        if k.startswith("_") or k not in rec:
            continue
        rec[k] = v.value
        prov[f"{where}.{k}"] = {"value": v.value, "how": v.how, "evidence": v.evidence}


def sanitise(block: Iterable[dict], scope: dict, vocab: dict, prov: dict, where: str) -> None:
    """Never write a value a drop-down cannot hold. Anything that does not match the
    datasheet's own vocabulary is removed and reported, so a revised datasheet can
    never silently produce an unloadable draft."""
    for f in block:
        if "key" not in f or f.get("type") != "select":
            continue
        voc = f.get("vocab", "")
        val = scope.get(f["key"], "")
        if val and voc in vocab and val not in vocab[voc]:
            scope[f["key"]] = ""
            prov[f"{where}.{f['key']}"] = {
                "value": "", "how": "rejected",
                "evidence": f"extracted {val!r} but it is not in vocabulary "
                            f"'{voc}' of this datasheet version; left empty"}


def uid_for(paper: Paper, scrd: int) -> str:
    stem = re.sub(r"\W+", "", paper.first_author_surname)[:18] or "Unknown"
    yr = paper.year or _dt.date.today().strftime("%Y")
    return f"{stem}{yr}-{_dt.date.today().isoformat()}"


def build_draft(scrd: int, schema: dict, paper: Paper, found: Any) -> Tuple[dict, dict]:
    prov: Dict[str, Any] = {}
    src = (f"{paper.title or 'untitled'}. "
           f"{paper.journal + ' ' if paper.journal else ''}{paper.year}. "
           f"doi:{paper.doi or 'n/a'}. Full text: {paper.source or 'not retrieved'}. "
           f"Draft generated automatically by scrd_autofill.py on "
           f"{_dt.date.today().isoformat()}; every value must be checked against the "
           f"paper before use. Empty fields were not found in the text.")

    if scrd == 1:
        enz_found, meas_found = found
        enz = blank(schema["enzyme"])
        enz["Unique_identifier"] = uid_for(paper, 1)
        enz["Protein_name"] = paper.title[:80] if paper.title else ""
        apply(enz, enz_found, prov, "enzyme")
        sanitise(schema["enzyme"], enz, schema["vocab"], prov, "enzyme")
        meas = []
        for mf in meas_found:
            rec = blank(schema["measurement"])
            apply(rec, mf, prov, f"measurement[{len(meas)}]")
            fix_units(schema["measurement"], rec, schema, prov, f"measurement[{len(meas)}]")
            sanitise(schema["measurement"], rec, schema["vocab"], prov, f"measurement[{len(meas)}]")
            meas.append(rec)
        if not meas:
            meas = [blank(schema["measurement"])]
        return {"_source": src, "enzyme": enz, "measurements": meas}, prov

    if scrd == 3:
        prep_found, meas_found, lipids = found
        prep = blank(schema["prep"])
        prep["Preparation_name"] = uid_for(paper, 3)
        apply(prep, prep_found, prov, "prep")
        fix_units(schema["prep"], prep, schema, prov, "prep")
        sanitise(schema["prep"], prep, schema["vocab"], prov, "prep")
        for lip in lipids:
            if lip.get("unit"):
                v, sp, note = resolve_unit(lip["unit"], schema["vocab"].get("lipid_conc_unit", []))
                lip["unit"] = v
                prov[f"prep.Lipid_composition.{lip['name']}.unit"] = {
                    "value": v, "how": "unit-normalised", "evidence": note}
        prep["Lipid_composition"] = lipids
        if lipids:
            prov["prep.Lipid_composition"] = {
                "value": ", ".join(f"{l['name']} {l['mol'] or '?'} mol%" for l in lipids),
                "how": "regex", "evidence": "lipid names matched against the datasheet vocabulary; "
                                            "mol% only where it stood next to the name"}
        meas = []
        for mf in meas_found:
            rec = blank(schema["measurement"])
            apply(rec, mf, prov, f"measurement[{len(meas)}]")
            fix_units(schema["measurement"], rec, schema, prov, f"measurement[{len(meas)}]")
            sanitise(schema["measurement"], rec, schema["vocab"], prov, f"measurement[{len(meas)}]")
            meas.append(rec)
        return {"_source": src, "prep": prep, "measurements": meas}, prov

    sysb_found, tpl_found, cond_found, out_found = found
    sysb = blank(schema["system"])
    sysb["Unique_identifier"] = uid_for(paper, 2)
    apply(sysb, sysb_found, prov, "system")
    sanitise(schema["system"], sysb, schema["vocab"], prov, "system")
    tpl = blank(schema["template"])
    apply(tpl, tpl_found, prov, "template")
    fix_units(schema["template"], tpl, schema, prov, "template")
    sanitise(schema["template"], tpl, schema["vocab"], prov, "template")
    cond = blank(schema["condition"])
    apply(cond, cond_found, prov, "condition")
    sanitise(schema["condition"], cond, schema["vocab"], prov, "condition")
    meas = blank(schema["measurement"])
    meas["Template_used"] = tpl.get("Template_ID", "T1")
    meas["Condition_set_used"] = cond.get("Condition_ID", "C1")
    if "_final_concentration" in tpl_found:
        meas["Template_final_concentration"] = tpl_found["_final_concentration"].value
        prov["measurement.Template_final_concentration"] = {
            "value": tpl_found["_final_concentration"].value, "how": "regex",
            "evidence": tpl_found["_final_concentration"].evidence}
    meas["_cat"] = "Expression output"
    outputs = {}
    if out_found.get("Value"):
        row = {f["key"]: "" for f in schema["output_row"]}
        row["Value"] = out_found["Value"].value
        row["Unit"] = out_found["Unit"].value if "Unit" in out_found else ""
        allowed = schema["output_units"].get("protein yield per volume", [])
        value, _spec, note = resolve_unit(row["Unit"], allowed)
        row["Unit"] = value if value in allowed else ""
        prov["measurement.outputs.protein yield per volume.Unit"] = {
            "value": row["Unit"], "how": "unit-normalised",
            "evidence": note + ("" if row["Unit"] else
                                f"  not one of the units this output accepts ({allowed}); left empty")}
        outputs["protein yield per volume"] = row
        prov["measurement.outputs.protein yield per volume"] = {
            "value": row["Value"] + " " + row["Unit"], "how": "regex",
            "evidence": out_found["Value"].evidence}
    meas["outputs"] = outputs
    return ({"_source": src, "system": sysb, "tables": {}, "templates": [tpl],
             "conditions": [cond], "measurements": [meas]}, prov)


def sheet_id(scrd: int, draft: dict) -> str:
    """The identifier other datasheets refer to, with its SCRD prefix."""
    if scrd == 1:
        raw = draft["enzyme"].get("Unique_identifier", "")
    elif scrd == 2:
        raw = draft["system"].get("Unique_identifier", "")
    else:
        raw = draft["prep"].get("Preparation_name", "")
    return f"SCRD{scrd}-{raw}" if raw else ""


def cross_link(produced: List[Tuple[int, dict, dict, str]]) -> None:
    """One article often feeds several datasheets - a cell-free reaction run inside
    vesicles is an SCRD 2 and an SCRD 3. They are written as separate sheets and
    pointed at each other through Related_datasheets, which is what that field is for."""
    if len(produced) < 2:
        return
    ids = {scrd: sheet_id(scrd, draft) for scrd, draft, _, _ in produced}
    for scrd, draft, prov, _ in produced:
        others = [v for k, v in ids.items() if k != scrd and v]
        if not others:
            continue
        scope = draft["enzyme"] if scrd == 1 else (draft["system"] if scrd == 2 else draft["prep"])
        if "Related_datasheets" in scope:
            scope["Related_datasheets"] = "; ".join(others)
            prov["Related_datasheets"] = {
                "value": "; ".join(others), "how": "cross-link",
                "evidence": "the same article also produced these datasheets in this run"}


# --------------------------------------------------------------------------
# 5. validation
# --------------------------------------------------------------------------
def visible(f: dict, scope: dict) -> bool:
    for key, want in (("show", True), ("show_when", True), ("hide", False)):
        c = f.get(key)
        if not c:
            continue
        hit = (scope.get(c["field"]) or "") in c["equals"]
        if want != hit:
            return False
    return True


def check_block(block: Iterable[dict], scope: dict, vocab: dict, label: str,
                missing: List[str], errors: List[str]) -> Tuple[int, int]:
    req_total = req_filled = 0
    for f in block:
        if "key" not in f or f.get("type") == "derived" or not visible(f, scope):
            continue
        val = scope.get(f["key"], "")
        voc = f.get("vocab", "")
        if f.get("type") == "select" and val and voc in vocab and val not in vocab[voc]:
            errors.append(f"{label}: '{f['key']}' = {val!r} is not in vocabulary '{voc}'")
        if f.get("req") == "R":
            req_total += 1
            if val:
                req_filled += 1
            else:
                missing.append(f"{label}: {f.get('display') or f['key']}")
    return req_total, req_filled


def validate(scrd: int, schema: dict, draft: dict) -> Tuple[List[str], List[str], Tuple[int, int]]:
    missing: List[str] = []
    errors: List[str] = []
    tot = fil = 0
    V = schema["vocab"]

    def add(t):
        nonlocal tot, fil
        tot += t[0]; fil += t[1]

    if scrd == 1:
        add(check_block(schema["enzyme"], draft["enzyme"], V, "Protein", missing, errors))
        for i, m in enumerate(draft["measurements"], 1):
            add(check_block(schema["measurement"], m, V, f"Measurement {i}", missing, errors))
    elif scrd == 3:
        add(check_block(schema["prep"], draft["prep"], V, "Preparation", missing, errors))
        lf = {f["key"]: f for f in schema["lipid_fields"]}
        for lip in draft["prep"].get("Lipid_composition", []):
            for k, f in lf.items():
                if f.get("req") == "R":
                    tot += 1
                    if lip.get(k):
                        fil += 1
                    else:
                        missing.append(f"Lipid {lip.get('name', '?')}: {f['label']}")
                if f.get("type") == "select" and lip.get(k) and lip[k] not in V.get(f["vocab"], []):
                    errors.append(f"Lipid {lip.get('name')}: {k} = {lip[k]!r} invalid")
        if not draft["prep"].get("Lipid_composition"):
            missing.append("Preparation: Membrane lipid composition (no lipids identified)")
        for i, m in enumerate(draft["measurements"], 1):
            add(check_block(schema["measurement"], m, V, f"Measurement {i}", missing, errors))
    else:
        add(check_block(schema["system"], draft["system"], V, "System", missing, errors))
        for t in draft["templates"]:
            add(check_block(schema["template"], t, V, f"Template {t.get('Template_ID')}", missing, errors))
        for c in draft["conditions"]:
            add(check_block(schema["condition"], c, V, f"Condition {c.get('Condition_ID')}", missing, errors))
        for m in draft["measurements"]:
            add(check_block(schema["measurement"], m, V, "Measurement", missing, errors))
            for name, row in (m.get("outputs") or {}).items():
                units = schema["output_units"].get(name, [])
                if row.get("Unit") and row["Unit"] not in units:
                    errors.append(f"Output '{name}': unit {row['Unit']!r} not in {units}")
                for f in schema["output_row"]:
                    if f.get("req") == "R":
                        tot += 1
                        if row.get(f["key"]):
                            fil += 1
                        else:
                            missing.append(f"Output '{name}': {f.get('display') or f['key']}")
    return missing, errors, (tot, fil)


# --------------------------------------------------------------------------
# optional LLM pass
# --------------------------------------------------------------------------
LLM_PROMPT = """You extract reporting metadata from a scientific methods section.

Below is a JSON object of EMPTY fields from a reporting datasheet. For each field
you are given its name, the question it asks, and - for fields with a fixed
vocabulary - the only allowed values.

Rules, in order of importance:
1. Only fill a field if the text states it. Never infer, never average, never
   convert units, never carry a value over from your own knowledge.
2. For a vocabulary field, return one of the allowed values verbatim, or "".
3. Return "" for anything the text does not state. An empty field is correct and
   useful; a guessed field is harmful.
4. For every field you fill, give the sentence it came from.

Return ONLY JSON: {"fields": {"<name>": {"value": "...", "evidence": "..."}}}

FIELDS:
%s

TEXT:
%s
"""


def llm_fill(scrd: int, schema: dict, draft: dict, text: str, prov: dict, model: str) -> int:
    try:
        import anthropic
    except ImportError:
        print("   !  --llm needs the anthropic package:  pip install anthropic", file=sys.stderr)
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("   !  --llm needs ANTHROPIC_API_KEY in the environment", file=sys.stderr)
        return 0

    blocks = {1: [("enzyme", schema["enzyme"], draft["enzyme"])],
              3: [("prep", schema["prep"], draft["prep"])],
              2: [("system", schema["system"], draft["system"]),
                  ("condition", schema["condition"], draft["conditions"][0])]}[scrd]
    spec, targets = {}, {}
    for bname, block, scope in blocks:
        for f in block:
            if "key" not in f or f.get("type") in ("derived", "lipids", "multi"):
                continue
            if scope.get(f["key"]) or not visible(f, scope):
                continue
            name = f"{bname}.{f['key']}"
            entry = {"question": f.get("help") or f.get("display") or f["key"]}
            if f.get("vocab") in schema["vocab"]:
                entry["allowed_values"] = schema["vocab"][f["vocab"]]
            spec[name] = entry
            targets[name] = scope
    if not spec:
        return 0

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model, max_tokens=4000,
        messages=[{"role": "user",
                   "content": LLM_PROMPT % (json.dumps(spec, ensure_ascii=False, indent=1),
                                            text[:120000])}])
    raw = msg.content[0].text
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return 0
    try:
        data = json.loads(m.group(0)).get("fields", {})
    except json.JSONDecodeError:
        return 0

    n = 0
    for name, item in data.items():
        val = (item or {}).get("value", "").strip()
        if not val or name not in targets:
            continue
        key = name.split(".", 1)[1]
        allowed = spec[name].get("allowed_values")
        if allowed and val not in allowed:        # refuse anything off-vocabulary
            continue
        targets[name][key] = val
        prov[name] = {"value": val, "how": f"llm:{model}",
                      "evidence": (item.get("evidence") or "")[:300]}
        n += 1
    return n


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate an SCRD draft from a DOI, a PDF or a text file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            The script fills only what it can find and flags the rest. Treat every
            filled field as a proposal to be checked: the provenance file lists the
            sentence each value came from."""))
    ap.add_argument("doi", nargs="?", help="DOI, e.g. 10.1111/febs.15337")
    ap.add_argument("--pdf", help="local PDF to read instead of (or alongside) the DOI")
    ap.add_argument("--text", help="local plain-text file (e.g. the methods section)")
    ap.add_argument("--scrd", type=int, choices=[1, 2, 3], help="force a datasheet instead of detecting it")
    ap.add_argument("--primary-only", action="store_true",
                    help="write only the best-fitting datasheet (default: write every datasheet "
                         "that fits, cross-linked to each other)")
    ap.add_argument("--out", default=".", help="output directory (default: current)")
    ap.add_argument("--schema-dir", help="directory holding the three SCRD html files")
    ap.add_argument("--email", default=os.environ.get("SCRD_EMAIL", ""),
                    help="contact e-mail for Crossref/Unpaywall (recommended)")
    ap.add_argument("--min-score", type=int, default=10,
                    help="classifier score below which a warning is raised (default 10)")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to write a draft when the score is below --min-score")
    ap.add_argument("--llm", action="store_true", help="fill remaining fields with an LLM pass")
    ap.add_argument("--model", default="claude-sonnet-4-5", help="model for --llm")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not (args.doi or args.pdf or args.text):
        ap.error("give a DOI, --pdf or --text")

    schema_dir = find_schema_dir(args.schema_dir)
    os.makedirs(args.out, exist_ok=True)
    say = (lambda *a: None) if args.quiet else print

    say("1. retrieving")
    paper = retrieve(args)
    if len(paper.text) < 400:
        sys.stdout.flush()
        print("\n   STOP: no usable text was retrieved.", file=sys.stderr)
        print("   The article is probably not open access. Download the PDF and run:",
              file=sys.stderr)
        print(f"       python {os.path.basename(__file__)} {args.doi or ''} --pdf paper.pdf",
              file=sys.stderr)
        if paper.source:
            print(f"   Note: {paper.source}", file=sys.stderr)
        return 2
    say(f"   title   : {paper.title[:90] or '(unknown)'}")
    say(f"   source  : {paper.source or 'metadata only'}   ({len(paper.text):,} characters)")
    if not paper.has_methods:
        say("   !  this looks like an abstract or a partial text, not a methods section;")
        say("      expect many empty fields.")

    say("\n2. classifying")
    v = classify(paper, args.min_score)
    say(v.explain())
    if args.scrd:
        chosen = [args.scrd]
    elif args.primary_only:
        chosen = [v.primary]
    else:
        chosen = [v.primary] + v.secondary
    if not args.scrd and v.scores[v.primary] == 0:
        sys.stdout.flush()
        print("\n   STOP: the text matches none of the three datasheets.", file=sys.stderr)
        return 2
    if not args.scrd and v.scores[v.primary] < args.min_score:
        msg = (f"   !  low confidence: best score {v.scores[v.primary]} is under "
               f"{args.min_score}. Short texts and abstracts score low; a full methods "
               f"section normally scores far higher.")
        if args.strict:
            print("\n" + msg.replace("!  low confidence", "STOP"), file=sys.stderr)
            return 2
        say("\n" + msg)
    if len(chosen) > 1:
        say(f"   this article feeds {len(chosen)} datasheets: "
            f"{', '.join('SCRD ' + str(c) for c in chosen)}. "
            f"Each one is written separately and they are cross-linked.")
    elif v.secondary and args.primary_only:
        say(f"   note: SCRD {', '.join(map(str, v.secondary))} also scores highly but was "
            f"skipped because of --primary-only.")

    worst = 0
    produced: List[Tuple[int, dict, dict, str]] = []   # (scrd, draft, prov, base path)
    for scrd in chosen:
        schema = load_schema(os.path.join(schema_dir, SCRD_FILES[scrd]))
        say(f"\n3. extracting for SCRD {scrd} ({SCRD_TITLES[scrd]})")
        if scrd == 1:
            found = extract_scrd1(paper.text, paper)
        elif scrd == 3:
            found = extract_scrd3(paper.text, schema)
        else:
            found = extract_scrd2(paper.text, schema)

        draft, prov = build_draft(scrd, schema, paper, found)
        if args.llm:
            n = llm_fill(scrd, schema, draft, paper.text, prov, args.model)
            say(f"   LLM pass filled {n} further field(s)")

        missing, errors, (tot, fil) = validate(scrd, schema, draft)
        pct = 100.0 * fil / tot if tot else 0.0

        stem = re.sub(r"\W+", "_", (paper.doi or paper.title or "draft"))[:60].strip("_")
        base = os.path.join(args.out, f"SCRD{scrd}_{stem}")
        produced.append((scrd, draft, prov, base))

    cross_link(produced)
    for scrd, draft, prov, base in produced:
        schema = load_schema(os.path.join(schema_dir, SCRD_FILES[scrd]))
        missing, errors, (tot, fil) = validate(scrd, schema, draft)
        pct = 100.0 * fil / tot if tot else 0.0
        with open(base + "_draft.json", "w", encoding="utf-8") as fh:
            json.dump(draft, fh, indent=2, ensure_ascii=False)
        with open(base + "_provenance.json", "w", encoding="utf-8") as fh:
            json.dump({"doi": paper.doi, "source": paper.source,
                       "classifier_scores": v.scores,
                       "datasheets_generated": [c for c, _, _, _ in produced],
                       "required_fields": {"total": tot, "filled": fil, "percent": round(pct, 1)},
                       "fields": prov, "missing_required": missing,
                       "schema_errors": errors}, fh, indent=2, ensure_ascii=False)

        say(f"\n4. result for SCRD {scrd} ({SCRD_TITLES[scrd]})")
        say(f"   required fields filled : {fil}/{tot}  ({pct:.0f}%)")
        say(f"   draft                  : {base}_draft.json")
        say(f"   provenance             : {base}_provenance.json")
        if errors:
            worst = max(worst, 1)
            say("\n   SCHEMA ERRORS (these will not load correctly):")
            for e in errors:
                say("     x " + e)
        if missing:
            worst = max(worst, 1)
            say(f"\n   WARNING: {len(missing)} required field(s) are empty and must be "
                f"completed by hand:")
            for mtxt in missing[:40]:
                say("     - " + mtxt)
            if len(missing) > 40:
                say(f"     ... and {len(missing) - 40} more (see the provenance file)")
        else:
            say("\n   No required field is empty.")
        if pct < 40:
            say("\n   NOTE: fewer than 40% of required fields were found. That usually means "
                "the\n   methods section was not retrieved, not that the paper omits the data.")
    if produced:
        say("\n   Every filled value is a proposal. Check each one against the paper using "
            "the\n   provenance file before loading the draft into the datasheet.")

    return worst


if __name__ == "__main__":
    sys.exit(main())
