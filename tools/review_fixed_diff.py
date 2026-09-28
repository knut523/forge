"""Run forge's upgraded review over a locally-built fix diff (no GitHub).
The diff files are passed as JSON [{path, diff}] on argv[1]."""
import json, sys
from forge.config.store import ConfigStore
from forge.runs import pr_review as PR

diffs = json.load(open(sys.argv[1]))
GOAL = (
    "Fix the Austrian house-number splitter (calc-api #54) and its prefill DTO wiring (#55).\n\n"
    "## Acceptance\n"
    "- [ ] the splitter declines (does not emit a confident split) when input has residual text "
    "between keywords or unresolved slash structure, instead of silently mis-splitting\n"
    "- [ ] the prefill DTO exposes the preserved raw house-number value\n"
    "- [ ] when the split is not confident the DTO falls back to the raw value in Hausnummer, "
    "not a wrong split\n"
    "- [ ] existing prefill and splitter tests still pass\n"
)
cfg = ConfigStore()
try:
    out = PR.review_built(cfg, "olaf-calc-api", GOAL, diffs)
    print("verdict:", out.get("verdict"))
    print("acceptance:", out.get("acceptance", {}).get("source"),
          "->", len(out.get("acceptance", {}).get("criteria", [])), "criteria")
    print("seeds:", out.get("seeds"), "| models:", out.get("models"), "verifier:", out.get("verifier"))
    fs = out.get("findings", [])
    print("findings:", len(fs))
    for f in fs[:12]:
        print(f"  [{f.get('severity')}/{f.get('angle','')}] {f.get('file')}: {str(f.get('detail'))[:150]}")
    print("\nsummary:", str(out.get("summary"))[:600])
finally:
    cfg.close()
