# Contributing to Nawa Edge

Thank you for helping make the sovereign edge better. Nawa Edge is deliberately a
**single file with zero dependencies** — that constraint is the product. Contributions
that respect it are very welcome.

## Ground rules

- **Zero dependencies is non-negotiable.** PRs that add a third-party import to
  `nawa_edge.py` will be declined, however good the library. Optional integrations
  belong in `examples/` as separate scripts.
- **No network calls, ever.** The dashboard binds to 127.0.0.1 only. Anything that
  opens an outbound connection breaks the product's core promise and will be declined.
- **Bilingual parity.** User-facing strings live in the `I18N` dict — add both English
  and Arabic (we'll help polish the Arabic if yours is rusty).
- **Python 3.9 compatibility.** No syntax or stdlib features newer than 3.9.

## How to contribute

1. **Bug reports** — open an issue with your OS, Python version, a minimal CSV that
   reproduces the problem, and what you expected. Never attach real plant data.
2. **False alarm / missed fault reports** — the most valuable contribution of all.
   Describe the physical situation (sensor type, climate, cadence) and, if possible,
   an anonymized/synthetic CSV that reproduces it.
3. **Pull requests** — run the check below, keep the diff focused, and explain the
   engineering reasoning in the description.

## Testing your change

```bash
python nawa_edge.py --no-serve --out /tmp/test_out
python nawa_edge.py verify /tmp/test_out/nawa_edge_seals.jsonl
```

The demo must still detect its three faults with zero false alarms, and the seal
chain must verify.

## Licensing of contributions

By submitting a pull request you agree that your contribution is licensed under the
repository's MIT License. The Nawa names and logo remain trademarks of Nawa Advanced
Technologies Ltd (see TRADEMARKS.md).

## Questions

Open a GitHub issue, or email info@nawacore.ai.
