# Rubric: Comparative Genomics

## What counts as on-topic

Content is on-topic if it uses sequence-level data from two or more species to infer evolutionary processes, functional constraints, or genomic architecture differences. This includes synteny analysis, rate-of-evolution comparisons, gene family expansion/contraction, repetitive element dynamics, and genome size variation. Population-genomic studies of a single species without a cross-species inference are borderline; purely clinical or medical genomics is off-topic.

## Standards for a testable hypothesis

A well-formed hypothesis names the genomic feature being compared (e.g., repeat content, gene family size, dN/dS ratio for a gene class), the taxa being compared, and the expected pattern under a stated evolutionary model. The hypothesis should be falsifiable with available or obtainable genome assemblies and standard bioinformatic pipelines. Claims that require genome data that do not yet exist for the relevant clade should be flagged as preliminary.

## What "grounding" means in this domain

Grounding requires citation of the specific genome assembly or database version used or proposed, and of the computational method with the original methods paper (not just a downstream application paper). When invoking selection as a mechanism, the output must cite empirical or theoretical work linking the genomic pattern to the specific selective regime, not just a general evolutionary genetics textbook.

## Red flags

- Treating assembly quality differences across species as biological signal (e.g., interpreting gap-rich regions as genuine gene-poor regions)
- Conflating synteny with orthology, or using gene collinearity as a proxy for functional conservation without evidence
- Reporting dN/dS > 1 as evidence of positive selection without acknowledging saturation or codon usage bias issues
- Ignoring genome size or ploidy when comparing repeat content in raw base-pair terms
- Drawing conclusions from single-copy BUSCO completeness scores without noting what those scores do and do not measure
