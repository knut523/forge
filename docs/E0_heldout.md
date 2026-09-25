# E₀ — held-out benchmark set (parity plan, P0b)

Held-out Christoph-reviewed **deep infra** PRs, NOT used for lens-mining or prompt tuning
(distinct from the tuning PRs #190/#181/#175). Ground truth = Christoph's (bizarrochris)
blocking findings, extracted by the benchmark judge from the pr_history index.

| PR | Christoph comments | chars | why |
|----|----|----|----|
| olaf-admin#72  | 23 | 40717 | richest review in the corpus |
| olaf-admin#137 | 8  | 31826 | infra, long review bodies |
| olaf-admin#129 | 8  | 27221 | infra, long review bodies |
| olaf-admin#130 | 4  | 15474 | infra |
| olaf-admin#85  | 4  | 16457 | infra |

Run: `FORGE_E0="olaf-admin:72,olaf-admin:137,olaf-admin:129,olaf-admin:130,olaf-admin:85" \
      python3 tools/benchmark_fortress.py`
Parity target (proposed): forge matches ≥80% of Christoph's blocking findings across E₀.
