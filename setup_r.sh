#!/bin/bash
RSCRIPT=$(command -v Rscript || echo /opt/homebrew/bin/Rscript)
if [ ! -x "$RSCRIPT" ]; then
  echo "Rscript not found. brew install r"
  exit 1
fi
"$RSCRIPT" -e 'install.packages(c("ape","phytools","geiger","diversitree","tidyverse","ggplot2","patchwork"), repos="https://cloud.r-project.org")'
