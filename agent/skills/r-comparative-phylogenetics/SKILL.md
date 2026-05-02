---
name: r-comparative-phylogenetics
description: >
  Use when writing R code for comparative phylogenetic analysis (BiSSE/MuSSE,
  ancestral state reconstruction, BAMM, diversitree, sex-chromosome turnover,
  dysploidy rate analysis). Covers the standard library set, code templates,
  the data-resource preflight rule, working-directory conventions, and
  sandbox restrictions.
---

# R Comparative Phylogenetics

## Execution Environment

R code runs via the `run_r_script` tool, which:
- Locates `Rscript` at `/opt/homebrew/bin/Rscript` (fallback: `which Rscript`)
- Creates a timestamped working directory under `data/r_runs/YYYYMMDD_HHMMSS/`
  unless `working_dir` is specified explicitly
- Prepends `agent/r_runtime/preamble.R` to every script
- Saves the full script as `working_dir/script.R` for reproducibility
- Returns JSON: `stdout`, `stderr`, `exit_code`, `working_dir`, `plot_paths`,
  `created_files`

Always tell Heath the `working_dir` path so he can inspect plots and outputs
directly. For long analyses, write intermediate results to disk rather than
holding everything in the R session.

### Preamble (auto-prepended)

```r
options(warn = 1, stringsAsFactors = FALSE)
set.seed(42)
suppressPackageStartupMessages({
  # Loaded by default; per-run libraries appended after
})
```

`stringsAsFactors = FALSE` is always active — do not fight it. `set.seed(42)` is
set globally; override inside your script if a different seed is needed for a
specific analysis.

---

## Mandatory Preflight: Data Resource Resolution

Before emitting any R code that reads a lab database, call
`require_data_resource(key)`. Use the returned `OK|<path>` string verbatim.

```r
# After require_data_resource("coleoptera_karyotypes") returns
# "OK|/Users/blackmon/Desktop/GitHub/coleoguy.github.io/data/karyotypes-coleoptera.csv"

dat <- read.csv("/Users/blackmon/Desktop/GitHub/coleoguy.github.io/data/karyotypes-coleoptera.csv",
                header = TRUE)
```

If it returns `ERROR|...`, stop and report to Heath. Do not write analysis code
against an unresolved resource.

---

## Standard Library Set

Load via the `libraries` parameter of `run_r_script` (comma-separated).

| Package | Purpose |
|---------|---------|
| `ape` | Tree I/O (read.tree, read.nexus), root/prune/drop.tip, `ace()` for ancestral state |
| `phytools` | `make.simmap()`, `stochastic.character.mapping()`, `fastAnc()`, `contMap()`, `plotSimmap()` |
| `geiger` | `fitContinuous()`, `fitDiscrete()`, `treedata()` for name-matching |
| `diversitree` | BiSSE/MuSSE/QuaSSE model fitting and MCMC; `make.bisse()`, `find.mle()`, `mcmc()` |
| `tidyverse` | Data manipulation; prefer `dplyr`/`tidyr` over base for clarity |
| `ggplot2` | Primary plotting; ggtree for phylogenetic plots |
| `patchwork` | Multi-panel figure assembly (`p1 + p2 + p3`) |
| `BAMM` / `BAMMtools` | Diversification rate analysis; use `BAMMtools` for post-processing |
| `nlme` / `caper` | PGLS regression (`pgls()` in caper) |
| `corHMM` | Multistate discrete character models; hidden-rates extension |

---

## Code Templates

### BiSSE Analysis

```r
library(ape)
library(diversitree)

# Load tree and data (paths from require_data_resource)
tree <- read.tree("/path/to/tree.nwk")
dat  <- read.csv("/path/to/data.csv", header = TRUE)

# Match tips — critical step
rownames(dat) <- dat$species
td <- treedata(tree, dat, sort = TRUE, warnings = TRUE)
tree_m <- td$phy
dat_m  <- td$data

# Encode binary state (0/1); must be named vector
states <- setNames(as.integer(dat_m[, "has_xy"]), rownames(dat_m))

# Build model; supply sampling fraction if incomplete
lik <- make.bisse(tree_m, states, sampling.f = c(0.5, 0.5))

# MLE starting point
p0 <- starting.point.bisse(tree_m)
fit_full <- find.mle(lik, p0)

# Constrained model (equal speciation rates)
lik_eq_lambda <- constrain(lik, lambda1 ~ lambda0)
fit_eq_lambda  <- find.mle(lik_eq_lambda, p0[-2])

# LRT
anova(fit_full, equal.lambda = fit_eq_lambda)

# MCMC (short run — extend for publication)
samples <- mcmc(lik, coef(fit_full), nsteps = 10000, w = 0.1,
                lower = 0, upper = Inf, print.every = 1000)
saveRDS(samples, file.path(getwd(), "bisse_mcmc_samples.rds"))
```

### MuSSE (Multiple States)

```r
library(diversitree)

# States must be integers 1, 2, 3, ...
states <- setNames(dat_m[, "sex_system_int"], rownames(dat_m))

lik <- make.musse(tree_m, states, k = 3)  # k = number of states
p0  <- starting.point.musse(tree_m, k = 3)
fit <- find.mle(lik, p0)

# Constrain net diversification to be equal across states for null
lik_null <- constrain(lik, lambda2 ~ lambda1, lambda3 ~ lambda1,
                           mu2 ~ mu1, mu3 ~ mu1)
fit_null <- find.mle(lik_null, p0[!grepl("lambda[23]|mu[23]", names(p0))])

anova(fit, null = fit_null)
```

### Ancestral State Reconstruction (Discrete)

```r
library(ape)
library(phytools)

# Maximum parsimony
mp_anc <- ape::ancestral.pars(tree_m, states, type = "MPR")

# ML via ace (ape)
ml_anc <- ace(states, tree_m, type = "discrete", model = "ARD")
round(ml_anc$lik.anc, 3)  # posterior-like probabilities at each node

# Stochastic character mapping (100 maps)
sm <- make.simmap(tree_m, states, model = "ARD", nsim = 100, pi = "estimated")
pd <- describe.simmap(sm, plot = FALSE)

pdf(file.path(getwd(), "simmap_100reps.pdf"), width = 10, height = 14)
plotSimmap(sm[[1]], pts = FALSE, lwd = 1.5)
dev.off()
```

### Ancestral State Reconstruction (Continuous)

```r
library(phytools)

trait <- setNames(log(dat_m[, "diploid_number"]), rownames(dat_m))

# Fast ancestral value estimation (Brownian motion)
anc_vals <- fastAnc(tree_m, trait, vars = TRUE, CI = TRUE)

# Visualize on tree
pdf(file.path(getwd(), "contMap_diploid_number.pdf"), width = 8, height = 12)
obj <- contMap(tree_m, trait, plot = FALSE)
obj <- setMap(obj, colors = c("blue", "yellow", "red"))
plot(obj, fsize = 0.4, legend = 0.7 * max(nodeHeights(tree_m)))
dev.off()
```

### Dysploidy Rate Analysis (chromosome number evolution)

```r
library(ape)
library(phytools)
# chromePlus / ChromEvol-style transitions encoded as hidden states via corHMM

library(corHMM)

# Chromosome numbers as ordered states; large ranges need state compression
dat_m$n_state <- as.integer(dat_m$haploid_number)

rate_mat <- getStateMat4Dat(dat_m["n_state"])$rate.mat
# Allow only +1 / -1 transitions (dysploidy model)
rate_mat_constrained <- ParEqual(rate_mat, list(c(1,2), c(2,3)))  # adjust indices

fit_chromo <- corHMM(tree_m, dat_m[, c("species", "n_state")],
                     rate.cat = 1, rate.mat = rate_mat_constrained,
                     node.states = "marginal")

saveRDS(fit_chromo, file.path(getwd(), "corhmm_dysploidy.rds"))
```

### PGLS Regression

```r
library(ape)
library(caper)

comp_data <- comparative.data(tree_m, dat_m, names.col = "species",
                               vcv = TRUE, na.omit = FALSE)

fit_pgls <- pgls(log(diploid_number) ~ log(body_mass),
                 data = comp_data, lambda = "ML")
summary(fit_pgls)

# Save PGLS diagnostics plot
pdf(file.path(getwd(), "pgls_diagnostics.pdf"))
par(mfrow = c(2, 2))
plot(fit_pgls)
dev.off()
```

### BAMM Diversification

```r
library(BAMMtools)

# BAMM runs externally; post-processing in R
edata <- getEventData(tree_m,
                      eventdata = file.path(getwd(), "event_data.txt"),
                      burnin = 0.1)

# Rate-through-time
pdf(file.path(getwd(), "bamm_rate_thru_time.pdf"))
plotRateThroughTime(edata, ratetype = "speciation")
dev.off()

# Credible shift set
css <- credibleShiftSet(edata, expectedNumberOfShifts = 1,
                        threshold = 5, set.limit = 0.95)
plot(css)
```

---

## Working Directory Conventions

- Never hardcode absolute paths in analysis code except for the data file
  path resolved by `require_data_resource`.
- Use `getwd()` for all output paths so the script is portable across runs.
- Save at minimum: the script itself (`script.R` — done automatically), any
  model fit objects as `.rds`, and all plots as PDF (preferred) or PNG.
- For large trees or MCMC chains, write checkpoints to `working_dir` rather
  than holding in memory.

---

## Sandbox Restrictions

- **Never use `system()`, `shell()`, or `processx` to shell out from R.**
  Use R packages or the Python/Bash tool for OS-level operations.
- **Never call `unlink()` or `file.remove()` on files outside `working_dir`.**
- Do not install packages at runtime (`install.packages()`). If a package is
  missing, report it to Heath and ask him to run `setup_r.sh`.
- The timeout is 300 seconds by default. For long MCMC runs, ask Heath whether
  to split into a short diagnostic run first before committing to the full chain.

---

## Tree Ingestion Patterns

```r
# Newick
tree <- read.tree("path/to/tree.nwk")

# NEXUS (MrBayes / BEAST output)
tree <- read.nexus("path/to/tree.nex")

# Drop outgroups; re-root
tree <- drop.tip(tree, c("Outgroup1", "Outgroup2"))
tree <- root(tree, outgroup = "RootTaxon", resolve.root = TRUE)

# Ultrametricize if needed (Grafen scaling — crude; prefer time-calibrated)
tree_u <- compute.brlen(tree, method = "Grafen", power = 1)

# Name-match tip labels to data frame
td <- treedata(tree, dat, sort = TRUE, warnings = TRUE)
# Always report: nrow(td$data) vs. length(td$phy$tip.label)
```

---

## Common Pitfalls

1. **Factor columns**: `stringsAsFactors = FALSE` is set globally but older
   data files may use `factor()`. Always `as.character()` or `as.integer()`
   before passing to diversitree.
2. **Zero branch lengths**: BAMM and some phytools functions fail on zero-length
   branches. Check `min(tree$edge.length)` and add a small constant if needed
   (`tree$edge.length[tree$edge.length == 0] <- 1e-6`).
3. **Incomplete sampling**: BiSSE/MuSSE are sensitive to sampling fraction.
   Always specify `sampling.f` if the tree covers < 90% of the clade.
4. **State encoding**: diversitree expects states as `0/1` (BiSSE) or `1/2/3...`
   (MuSSE). A state vector named `c(0,1)` passed as `c(TRUE, FALSE)` will silently
   fail. Always cast: `as.integer(states)`.
