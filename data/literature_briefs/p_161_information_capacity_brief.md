# p_161 Information Capacity of Finite Populations — Literature Brief

**Question:** Given N births per generation and N_e individuals contributing to the next generation, in a CONSTANT environment, what is the maximum amount of heritable information a population can maintain with fidelity?

**Date:** 2026-04-25 | **For:** Researcher
**Status:** New theoretical project added 2026-04-25. This brief is a survey of whether the question has been answered.

---

## TL;DR — partial answers exist, no unified bound

The question sits at the intersection of three literatures that have not been fully unified:

1. **Mutation-selection-drift balance (MSDB).** Classical and recent. Yields per-locus equilibrium distributions but not closed-form bounds on the total number of loci that can be simultaneously maintained.
2. **Error-threshold theory (Eigen quasispecies).** Gives a sharp bound on per-base mutation rate above which information is lost (μ × L < 1, where L is genome length). Built for non-recombining replicators; the extension to sexual finite populations is incomplete.
3. **Substitution-load / cost-of-natural-selection (Haldane, Kimura).** Bounds the rate at which beneficial substitutions can be fixed without driving the population extinct. Adjacent but distinct from "how much info can be MAINTAINED."

**No published paper, to my knowledge, derives a unified expression for L_max(N, N_e, U, s) under stabilizing selection in a constant environment.** This is the conjectured form the project's hypothesis sketches: L_max ~ N_e·s / U for additive selection in the SSWM regime. Whether this conjecture is correct or has counter-examples is exactly what the literature review needs to settle.

---

## Six papers to read first (in order)

### 1. Berg, Li, Riall, Hayward, Sella 2025 — *Mutation-selection-drift balance models of complex diseases* (PMC12693578)

**The single most directly relevant paper.** Models how MSDB shapes the prevalence and genetic architecture of complex disease. Derives explicit equilibrium properties for polygenic traits under continuous mutation, selection, and drift. The disease-prevalence framing is incidental — the underlying model is exactly the one the p_161 question requires. **First task:** read this and see whether their analytical results imply a finite L_max or whether L_max is unbounded for finite N_e under their assumptions.

### 2. Hayward & Sella 2022 — *Polygenic adaptation after a sudden change in environment* (PMC9683794)

Same paper that's central to p_030. Here the relevant content is the equilibrium distribution PRIOR to the environmental shift — the maintained polygenic architecture under stabilizing selection. Equation 6+ in the paper gives the genetic-variance equilibrium as a function of N_e, U, and the selection function. This is the closest existing form of the L_max question.

### 3. Negm & Veller 2026 — *Effect of long-range LD on allele-frequency dynamics under stabilizing selection* (PMC12991805)

Adds the LD-among-loci dimension. Shows that as L grows (more loci of small effect), opposite-effect-allele LD becomes substantial and the equilibrium variance equation must be modified. **This is where the L_max question may bite hardest:** as L increases, LD between negatively-correlated alleles erodes the per-locus heterozygosity. Mathematically, the L_max bound may emerge naturally from this LD-erosion machinery, but Negm & Veller don't frame it that way.

### 4. Schuster 2018 — *Molecular evolution between chemistry and biology* (PMC5982545)

The Eigen-tradition entry point. Reviews error-threshold theory in self-replicators. Derives μ_crit·L = ln(σ) where σ is the selective advantage and L is genome length. **The classical result the project's hypothesis is implicitly extending.** Eigen's framework assumes asexual replicators in infinite populations; the extension to sexual finite populations is exactly the gap.

### 5. Höllinger, Wölfl, Hermisson 2023 — *A theory of oligogenic adaptation of a quantitative trait* (PMC10550320)

Same paper that's central to p_030. Here it's relevant because Θ_bg = 4·N_e·μ·L is a candidate parameterization of "information capacity per generation." When Θ_bg ≫ 1, segregating polygenic variation is high; when Θ_bg ≪ 1, it's not. The L at which Θ_bg crosses unity is a candidate transition where the maintenance-vs-loss trade-off flips. Worth examining whether L* defined this way matches the Hayward-Sella equilibrium-variance formula's implicit L_max.

### 6. Solé, Kempes, Corominas-Murtra, et al. 2024 — *Fundamental constraints to the logic of living systems* (PMC11503024)

Philosophical entry point — useful framing but not a closed-form bound. Discusses information-theoretic limits on living systems at multiple scales. Read for the citation graph (it points at the right primary literature) and for the framing language if the project ever turns into a perspective piece.

---

## Two papers I checked that are LESS relevant than they look

- **Eigen 1971 (original quasispecies).** Foundational but not specific enough; Schuster 2018 covers the framework with later refinements.
- **Felsenstein 1974 (recombination + info maintenance).** The seminal paper on why recombination evolves under finite-N + multi-locus selection. The analytical framework treats the information-loss/Hill-Robertson question but does NOT provide a closed-form L_max. It's the right ancestor; Hayward & Sella 2022 is its modern descendant.

---

## What a 1-month theoretical literature project would look like

1. **Week 1:** Read papers 1–3 above. For each, write down the per-locus equilibrium variance formula and the assumed limit on L.
2. **Week 2:** Identify whether any of them lets L → ∞ at finite N_e while preserving a non-trivial polygenic equilibrium. If yes, the conjecture (L_max ~ N_e·s/U) is wrong as stated. If no, the conjecture is at least consistent with the existing framework.
3. **Week 3:** Try to derive the L_max bound directly from the Hayward-Sella variance formula by asking when per-locus heterozygosity falls below the drift threshold (h ~ 1/N_e).
4. **Week 4:** Assess whether the result is novel. Two outcomes:
   - **Already in the literature, hidden in a different framing.** Project closes; brief note for Heath's records on where the bound lives.
   - **Genuinely missing.** Project becomes a Genetics or PNAS short paper. Possible co-author candidates: Sella (Columbia), Hermisson (Vienna), Veller (Cornell). Heath's existing lab work on Coleoptera karyotype distributions provides empirical anchoring.

**Estimated effort:** 1 month of theoretical reading + 2 months derivation if the gap is real. NAS-relevance: medium (review-paper or methods-paper category, but theoretically deep enough to attract citations from beyond Heath's usual community).

---

## How this differs from p_030

- **p_030 asks:** GIVEN a polygenic architecture, what karyotype maximizes adaptation rate?
- **p_161 asks:** WHAT polygenic architecture is even sustainable in the first place, given finite N and N_e?

p_161 is upstream of p_030. The L_max bound from p_161 sets the ceiling on the L axis Mallory's chromosome-number simulations should explore. The two projects share the Hayward-Sella + Höllinger + Negm-Veller scaffolding, so reading the literature once buys both.
