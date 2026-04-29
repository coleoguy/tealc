# p_030 Chromosome Selection — Literature Brief

**Question:** Has anyone derived an optimal chromosome number as a function of trait polygenicity?

**Date:** 2026-04-25 | **For:** Mallory Murphy (primary author), Heath Blackmon
**Status:** Project paused until next semester per Mallory's note. This brief is the entry-point literature for when work resumes.

---

## TL;DR — the gap is real

The continuous-recombination-rate-optimum literature is mature. The discrete-chromosome-number literature is descriptive (modal counts, transition rates). **No paper directly derives k* (optimal chromosome number) as a function of L (number of causal loci) under a quantitative-genetic model with the obligate-chiasma constraint.** That's the opening for Mallory's project.

The closest existing pieces stop one inferential step short of the question. Each of them either fixes the karyotype and varies recombination rate, or fixes the trait architecture and varies population parameters. The synthesis that combines (i) discrete chromosome count → minimum recombination via obligate chiasma, (ii) variable Θ_bg / L architecture, and (iii) adaptation rate as the response variable is missing.

---

## Five papers to read first (in order)

### 1. Höllinger, Wölfl, Hermisson 2023 — *A theory of oligogenic adaptation of a quantitative trait* (PCI Evol Biol; PMC10550320)

The single most useful paper for framing this project. Models an additive QT under Gaussian stabilizing selection adapting to a new optimum. Derives joint allele-frequency distributions analytically using Yule branching processes. **Key result:** a single composite parameter — the population-scaled background mutation rate Θ_bg = 4·N_e·μ·L — predicts whether adaptation is sweep-like (Θ_bg ≪ 1), oligogenic (Θ_bg ≈ 1), or subtle polygenic (Θ_bg ≫ 1). Selection strength, locus number, and linkage are minor predictors. **Implication for p_030:** chromosome number enters only through linkage, which Höllinger shows is a minor factor. So the obligate-chiasma argument needs to engage with this — either by showing it operates through a different channel (e.g., LD-breakdown rate as a function of pheno-time), or by identifying parameter regimes where Höllinger's result breaks down.

### 2. Hayward & Sella 2022 — *Polygenic adaptation after a sudden change in environment* (PMC9683794)

The canonical recent analytical treatment. Models how mean phenotype changes over time under polygenic adaptation to a fitness-optimum shift. Derives explicit formulas for the rate of phenotypic adaptation as a function of mutation rate, effective population size, and trait architecture. Mallory's chromosome-number model needs to compare its rate-of-adaptation predictions against Hayward & Sella's baseline.

### 3. Negm & Veller 2026 — *The effect of long-range linkage disequilibrium on allele-frequency dynamics under stabilizing selection* (PMC12991805)

Newest piece (PLoS Genet 2026). Shows that stabilizing selection on a polygenic trait generates correlations (long-range LD) between opposite-effect alleles throughout the genome, and erodes heterozygosity. **This is the place where chromosome number could plausibly enter** — long-range LD is what obligate chiasmata break up. Veller's lab is the right collaborative target if this project ever needs co-author firepower.

### 4. Yeaman 2022 — *Evolution of polygenic traits under global vs local adaptation* (G3 / Genetics, PMC8733419)

Frames the genotypic-redundancy concept clearly. Local adaptation favors "concentrated" architectures (fewer, larger-effect, tightly linked alleles); global adaptation favors diffuse architectures. Useful for setting up the comparison: **Mallory's question can be reframed as "what karyotype minimizes the cost of redundancy in adaptation under fluctuating selection?"** That framing connects to the project's existing hypothesis (env-fluctuation modulation).

### 5. Dapper & Payseur 2017 — *Connecting theory and data to understand recombination rate evolution* (Phil Trans B)

The best entry point to the recombination-rate-optimum literature. Catalogs what's been shown about r* (optimal recombination rate per genome) under varied selection regimes. Use as the literature root: every paper on r* that matters cites this review.

---

## Existing lab-internal context

Heath's wiki topic `chromosome_number_optima` summarizes Blackmon et al. 2024 (J Heredity): Polyphaga modal autosome count = 9 (29% of records); Adephaga bimodal at 11 and 18. **Empirical pattern is real and well-described; the theoretical question of why these specific numbers is open.**

The project's `current_hypothesis` (paraphrased): more chromosomes → more obligate crossovers → faster LD breakdown → faster reassembly of favorable multi-locus genotypes → fitness advantage in fluctuating environments, modulated by epistasis level. **Critical gap to address:** Höllinger 2023 says linkage is a minor factor for adaptation rate. Why would the obligate-chiasma minimum-r mechanism produce a stronger signal? Two candidate answers worth simulating:

1. **Fluctuating environments make linkage more important.** Höllinger assumes a single optimum shift; fluctuating selection may amplify the cost of LD persistence.
2. **Epistasis makes linkage more important.** Höllinger assumes additive trait architecture. Heath's lab work on epistatic variance (and the project's `epistasis` keyword) suggests Mallory's model needs to vary epistasis level explicitly.

Either way, the experimental design has to include a Höllinger-style additive baseline so the chromosome-number effect can be measured against the appropriate null.

---

## Suggested next concrete actions when project resumes

1. Re-read the previous lab code Mallory mentioned and identify what exactly was being varied (chromosome number? recombination rate? both?).
2. Re-implement in SLiM 4 with three parameter axes: chromosome number (k), epistasis level (additive → strong epistasis), environmental fluctuation period (constant → fast oscillation).
3. Compare adaptation rate (sensu Hayward & Sella) under each (k, ε, T) combination.
4. Diagnostic: does the optimal k depend on ε and T? If yes → lab has a result. If no → the additive Θ_bg dominance result of Höllinger holds and the project becomes a methodologically careful null finding worth publishing as a note.

**Estimated effort:** 1 PhD-semester of Mallory's time once she resumes. The literature scaffolding above should save 4–6 weeks of orientation.
