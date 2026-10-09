# scrd_autofill

Draft a **Synthetic Cell Reporting Datasheet** (SCRD) straight from a DOI or a PDF.

The script finds the paper, works out which datasheet it belongs to, fills in every
field it can verify in the text, and warns you about every required field it could not.
Values are never invented: each filled field comes with the sentence it was taken from.

---

## Requirements

- **Python 3.9 or newer.** The core runs on the standard library alone.
- The three datasheet files, in the same folder as the script (or anywhere, with
  `--schema-dir`):
  - `SCRD1_Functional_Proteins.html`
  - `SCRD2_Nucleic_Acids_and_Cell-Free_Expression.html`
  - `SCRD3_Vesicles.html`

Optional packages, each enabling one feature:

| Package | Needed for | Install |
|---|---|---|
| `requests` | more robust HTTP (otherwise `urllib` is used) | `pip install requests` |
| `pypdf` | reading a local PDF with `--pdf` | `pip install pypdf` |
| `anthropic` | the optional `--llm` pass | `pip install anthropic` |

```bash
git clone https://github.com/<org>/scrd-autofill.git
cd scrd-autofill
pip install -r requirements.txt          # requests, pypdf, anthropic - all optional
python scrd_autofill.py --help
```

---

## Quick start

```bash
# open-access paper, straight from the DOI
python scrd_autofill.py 10.1038/s41467-022-29272-x --email you@university.edu

# paywalled paper: download the PDF yourself, then
python scrd_autofill.py 10.1111/febs.15337 --pdf paper.pdf

# just a methods section pasted into a text file
python scrd_autofill.py --text methods.txt --out drafts/
```

Giving `--email` is recommended: Crossref and Unpaywall ask for a contact address and
give faster, more reliable service when one is supplied. You can also set it once:

```bash
export SCRD_EMAIL=you@university.edu
```

---

## What you get

For every datasheet the paper feeds, two files are written:

```
SCRD3_10_1038_s41467_022_29272_x_draft.json        <- load this into the datasheet
SCRD3_10_1038_s41467_022_29272_x_provenance.json   <- evidence and warnings
```

Open the matching datasheet HTML in a browser, press **Load draft**, choose the
`_draft.json` file, and complete the flagged fields.

The provenance file records, per run:

- the classifier scores for all three datasheets
- every filled field, with the sentence it came from and how it was obtained
  (`regex`, `unit-normalised`, `cross-link`, `llm:<model>`)
- every value that was rejected for not matching a controlled vocabulary
- the list of empty required fields
- the count and percentage of required fields found

Terminal output ends with the same warning list:

```
4. result for SCRD 3 (Vesicles)
   required fields filled : 27/52  (52%)

   WARNING: 25 required field(s) are empty and must be completed by hand:
     - Preparation: Oil phase
     - Preparation: Internal solution
     - Lipid DOPC: Concentration
     ...
```

---

## Options

| Option | Meaning |
|---|---|
| `doi` | DOI of the paper, e.g. `10.1111/febs.15337` |
| `--pdf FILE` | read a local PDF instead of, or in addition to, the DOI |
| `--text FILE` | read a local plain-text file |
| `--scrd {1,2,3}` | force one datasheet instead of detecting it |
| `--primary-only` | write only the best-fitting datasheet (default: write all that fit) |
| `--out DIR` | output directory (default: current) |
| `--schema-dir DIR` | where the three SCRD html files live |
| `--email ADDR` | contact address for Crossref and Unpaywall |
| `--min-score N` | score below which a datasheet is not generated and a warning is raised (default 10) |
| `--strict` | refuse to write anything when the score is below `--min-score` |
| `--llm` | fill remaining free-text fields with a language model (see below) |
| `--model NAME` | model to use with `--llm` |
| `--quiet` | suppress progress output; warnings still go to stderr |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | draft written, no required field empty |
| 1 | draft written, required fields empty (warnings printed) |
| 2 | not enough text retrieved, or the text matches no datasheet |
| 3 | usage error |

Code `1` is the normal outcome, which makes the script easy to use in a loop:

```bash
while read doi; do
  python scrd_autofill.py "$doi" --out drafts/ --quiet || true
done < dois.txt
```

---

## Papers that feed more than one datasheet

This is the usual case, not the exception. Cell-free expression inside vesicles is both
an SCRD 2 and an SCRD 3; a reconstituted membrane protein is both an SCRD 1 and an
SCRD 3. By default every datasheet that fits is written as its own draft, and the drafts
are cross-linked through their `Related_datasheets` fields:

```
SCRD3_..._draft.json   id: SCRD3-Danelon2026-2026-10-08   links to SCRD2-Danelon2026-2026-10-08
SCRD2_..._draft.json   id: SCRD2-Danelon2026-2026-10-08   links to SCRD3-Danelon2026-2026-10-08
```

Use `--primary-only` for just the best fit, or `--scrd N` to force one.

---

## Units

Units are canonicalised before they are matched against the datasheet's own vocabulary,
so the many notations used in the literature all resolve to one spelling:

```
uM            -> µM                 cm/s           -> cm s^-1
mM-1 s-1      -> mM^-1 s^-1         nmol.min-1.mg-1 -> nmol min^-1 mg^-1
mg/mL         -> mg mL^-1           umol/min/mg     -> µmol min^-1 mg^-1
```

A unit that is real but absent from the datasheet's list is entered as `other`, with the
original spelling kept in the accompanying free-text field. Units are also checked
against the parameter they belong to, using the parameter-to-unit mapping declared in
the datasheet itself, so a K<sub>M</sub> reported in s<sup>-1</sup> raises a warning.

---

## Optional language-model pass

`--llm` asks a model to fill the free-text fields that pattern matching cannot reach
(preparation notes, reaction descriptions, internal solutions). It needs:

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python scrd_autofill.py 10.xxxx/yyyy --llm
```

The model is given only the empty fields, their help text and their permitted values,
and is instructed to fill a field only when the text states it and to return the source
sentence for each value. Anything outside a controlled vocabulary is rejected by the same
validator used for rule-based output.

Leave the flag off and the script is fully deterministic — the same input always gives
the same draft.

---

## Keeping up with datasheet revisions

The script reads the schema out of the datasheet HTML on every run. Field names,
requirement levels, vocabularies and unit lists always come from the datasheet version
you point it at. When the datasheets are updated, replace the HTML files; no change to
the script is needed.

---

## Known limitations

- Values that exist only inside a figure image or a graphical table cannot be read.
- Supplementary Information is not fetched automatically; pass it with `--pdf` or append
  it to your `--text` file.
- SCRD 2 drafts carry one measurement with one output, and the component tables are left
  for manual completion.
- Patterns were developed on English-language biochemistry and biophysics writing; check
  the first few papers from any unfamiliar journal style.

Roughly half of all required fields are filled automatically from a full methods
section. The rest are flagged, not silently skipped — which is the point.
