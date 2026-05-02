---
name: karyotype-databases
description: >
  Use when working with karyotype, chromosome number, sex-system data, or any of
  the lab's curated species databases (Coleoptera, Diptera, Amphibia, Mammalia,
  Polyneoptera, Drosophila karyotypes; Tree of Sex; CURES; Epistasis; Tau).
  Covers how to resolve resource keys via require_data_resource, what each
  database contains, and safe read/write patterns.
---

# Karyotype & Species Databases

## Overview

The Blackmon Lab maintains a suite of curated comparative genomics databases, all
publicly hosted at `coleoguy.github.io/data/`. Every database is registered in
`data/known_sheets.json` and accessed via the `require_data_resource(key)` tool.

**Hard rule: call `require_data_resource(key)` before writing any R or Python code
that reads a lab database.** The tool either returns `OK|<path>` (local CSV/JSON)
or `OK|<sheet_id>` (Google Sheet). Use the returned string verbatim in the
generated code. If it returns `ERROR|...`, STOP — do not emit analysis code.
Tell Heath what is missing and ask him to supply the path or Sheet ID.

---

## The require_data_resource Protocol

```python
# Example call
result = require_data_resource("coleoptera_karyotypes")
# Returns one of:
#   "OK|/Users/blackmon/Desktop/GitHub/coleoguy.github.io/data/karyotypes-coleoptera.csv"
#   "ERROR|key 'coleoptera_karyotypes' not in known_sheets.json. Available keys: ..."
#   "ERROR|resource 'tree_of_sex' is registered but not yet configured ..."
```

On `OK|<path>`:
- For local CSV: use the absolute path directly in `read.csv(<path>)` or `pd.read_csv("<path>")`
- For Google Sheet: pass the ID to the Sheets API or `read_sheet(sheet_id, range)`

On `ERROR|...`:
- Do NOT write any analysis code
- Report the error to Heath verbatim
- Ask whether he wants to supply a corrected path/ID or create a fresh registration

---

## Database Catalog

### Karyotype Databases (insect/arthropod focus)

**`coleoptera_karyotypes`** — Flagship database
- Rows: ~4,958 | Cols: 16
- Path suffix: `data/karyotypes-coleoptera.csv`
- Key columns: `Order`, `Suborder`, `Family`, `Genus`, `species`,
  `Reproductive.mode`, `B.chromosomes`, `Ploidy.level`,
  `Sex.chromosome.system`, `Meioformula`, `Diploid.number`, `Notes`, `Citation`
- Notes: Header contains 2 blank trailing columns (BOM artifact). The column
  `Sex.Chromosome.System` (duplicate spelling) appears as col 14. Always check
  `colnames()` / `df.columns` before joining.
- Companion: `coleoptera_karyotype_citations` (~251 rows, single-column `Citation`)

**`diptera_karyotypes`**
- Rows: ~3,474 | Cols: 17
- Key columns: `Family`, `Subfamily`, `Tribe`, `Genus`, `Species`,
  arm-type counts (`Submetacentric`, `Metacentric`, `Subtelocentric`,
  `Telocentric`, `Subacrocentric`, `Acrocentric`, `Dot`),
  `Sex`, `HaploidNum`, `SCS` (sex chromosome system), `Notes`, `Reference`

**`drosophila_karyotypes`** — Drosophilidae only
- Rows: ~1,246 | Cols: 14
- Key columns: `Genus`, `Subgenus`, `Species`, `R` (rod), `V` (V-shaped),
  `J` (J-shaped), `D` (dot), `HaploidNum`, `Arms`, `X`, `Y`, `Sex`,
  `Notes`, `Ref`
- Use for arm-morphology analyses within Drosophilidae

**`polyneoptera_karyotypes`** — Roaches, termites, grasshoppers, crickets
- Rows: ~822 | Cols: 11
- Key columns: `Order`, `Family`, `Genus`, `Species`,
  `femalediploidnumber`, `malediploidnumber`, `haploidnumber`,
  `reproductivemode`, `sexchromosome`, `notes`, `citation`
- Note: separate female/male 2n columns — useful for sex-biased ploidy analyses

### Karyotype Databases (vertebrate/other)

**`amphibia_karyotypes`**
- Rows: ~2,123 | Cols: 13
- Key columns: `Order`, `Family`, `Genus`, `Species`, `Diploid.number`,
  `Fundamental.number`, `Sex.chromosome`, `Ploidy`, `Max.Bs`,
  `Microchromosomes`, `Notes`, `Original.listing`, `Citation`
- Note: `Fundamental.number` (FN) = total chromosome arms; relevant for
  Robertsonian fusion/fission analysis

**`mammalia_karyotypes`**
- Rows: ~1,439 | Cols: 12
- Key columns: `Order`, `Family`, `Genus`, `Species`, `Binomial`,
  `Female2n`, `Male2n`, `B chromosomes`, `Sex chromosome system`,
  `Notes-sex.chrom`, `Notes-diploid.num`, `Source`
- Note: separate `Female2n`/`Male2n` — essential for detecting male-heterogamety
  vs. female-heterogamety in mammals

### Tree of Sex

**`tree_of_sex`** (top-level key) — Do NOT use this key directly.
- It is registered as `kind: unknown` and will return `ERROR|...`
- Use the three sub-keys instead:

**`tree_of_sex_vertebrates`**
- Rows: 2,063 | Cols: 40
- Key columns (selected): karyotype system (`ZO`, `ZW`, `XY`, `XO`,
  `WO`, `homomorphic`, `complex XY`, `complex ZW`), genotypic sex
  determination, haplodiploidy, environmental sex determination (TSD),
  chromosome number
- Warning: CR line endings — in R use `read.csv(path)` (handles CR/LF);
  in Python use `open(path, newline='')` before passing to `csv.reader`

**`tree_of_sex_invertebrates`**
- Rows: 14,146 | Cols: 20
- Key columns: `Kingdom`, `Higher.taxonomic.group`, `Order`, `Family`,
  `Genus`, `species`, `Sexual.System`, `Karyotype`, `Genotypic`,
  `Haplodiploidy`, `Predicted.ploidy`, `Chromosome.number` (female 2N / male 2N),
  `entry.name`, `cite.key`
- Same CR line-ending caveat as vertebrates

**`tree_of_sex_plants`**
- Rows: 25,117 | Cols: 40 — largest TOS subset
- Key columns: sexual system (`hermaphrodite`, `monoecy`, `dioecy`,
  `gynodioecy`, etc.), selfing status (`self incompatible / compatible`),
  growth form, hybrid status, ThePlantList v1.1 accepted-name mapping,
  chromosome number
- Same CR line-ending caveat

### CURES Karyotype Database

**`cures_karyotype_database`** — Broadest cross-clade database
- Rows: ~63,542 | Cols: 4
- Key columns: `clade`, `species`, `haploid_number`, `citation`
- Use for broad-scale chromosome number analyses spanning multiple eukaryotic
  clades; the 63k figure represents the dataset underlying the chromosomal
  stasis preprint
- Companion JSON: `cures_karyotype_data_json` (same records, JSON format with
  top-level keys `clades`, `sources`, `records`, `total`) — use CSV for R/pandas,
  JSON for web display

### Epistasis Database

**`epistasis_database`**
- Rows: ~1,606 | Cols: 14
- Key columns: `add` (additive effect), `dom` (dominance), `epi` (epistasis),
  `file`, `refile`, `class`, `kingdom`, `domestication`, `trait`, `species`,
  `SCS` (sex chromosome system), `divergence`, `weighted`, `method`
- Use for cross-species epistasis effect-size meta-analyses
- Companion: `epistasis_database_citations` (~128 rows, single column `Citation`)

### Tau (Circadian Period) Database

**`tau_database`** — Local JSON format
- Rows: ~1,960 | Cols: 22
- Key columns: `kingdom`, `phylum`, `class`, `order`, `family`, `genus`,
  `species`, `tau_mean_hours`, `tau_sd_hours`, `sample_size`, `sex`,
  `age_class`, `genotype`, `strain`, `light_condition`,
  `temperature_celsius`, `measurement_method`, `tissue_or_output`,
  `first_author`, `paper_year`, `doi`, `source`
- In R, load with `jsonlite::fromJSON(path)` or `rjson::fromJSON(file=path)`

---

## Database Health Checks

The 6 karyotype databases + Tree of Sex + Epistasis run a consistency check
every Saturday at 3am. Flagged rows surface in a Sunday briefing accessible
via `list_database_flags(sheet_name="coleoptera_karyotypes")` (or other sheet
name). If Heath asks "is the Coleoptera DB clean?", call
`trigger_database_health_check` immediately for fresh results rather than
relying on the cached Sunday briefing.

---

## Safe Write Pattern for Curated Databases

These databases represent years of curation. Every write is peer-review caliber:

1. `require_data_resource(key)` — get path
2. `read_sheet(sheet_id, range)` — see current values (for Google Sheet resources)
3. Show Heath the diff
4. Wait for explicit approval
5. `append_rows_to_sheet` OR `update_sheet_cells` (with two-step confirmation)

Never bulk-update without reading first. A single bad batch can corrupt thousands
of records. For local CSV resources, treat any programmatic edit with the same
caution — write to a staging copy first, diff, confirm.

---

## Cross-Database Joins

When joining across databases (e.g. Coleoptera karyotypes to Tree of Sex), use
`find_synonyms_for_species(name)` first to catch taxonomic name variants before
the join. Always report the match rate to Heath before proceeding with analysis —
low match rates (< 70%) usually signal a taxonomic scope mismatch or synonym
problem, not a coding error.
