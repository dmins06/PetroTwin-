# FX Source Audit — SBV USD/VND Central Reference Rate

**Audit date:** 2026-11-02

## Current conclusion

PetroTwin does not currently have a verified official source and schema for the State Bank of Vietnam (SBV) USD/VND central reference rate. The live FX collector is therefore disabled by configuration and fails closed. It never calls Vietcombank or uses Algolab as an automatic fallback.

## Historical sources

- `data/raw/usd_vnd_rates.csv` is labelled as SBV central reference rate and attributes the data through an Algolab page that identifies GSO/SBV as the source.
- Historical processed manifests contain both `sbv_reference_via_algolab` and `algolab_sbv_reference`, with Algolab URLs. Five run directories include this Algolab attribution in manifests/raw provenance.
- No repository evidence independently confirms the page's extraction method, publication provenance, contractual definition of the rate, or that every historical value is the official SBV central reference rate.
- Repository history contains a Vietcombank API and XML collector implementation, but the collector was corrected so production code contains no such integration.

## Verification status

| Source/pattern | Status | Repository evidence | Use permitted |
|---|---|---|---|
| Official SBV central-reference publication | Unverified | No verified official endpoint or machine-readable contract is configured | No live collection |
| Algolab page attributing GSO/SBV | Unverified secondary source | Historical raw and manifests only; attribution is not independent evidence | Historical audit only; exclude from live collection and new training assumptions until verified |
| Vietcombank API/XML transfer rate | Incorrect for this series | Previous implementation used commercial-bank transfer fields | Never |

Historical values and manifests were retained unchanged. The audit does not alter historical rate values, source IDs, or attribution, because those changes require new source evidence and the original snapshot should remain reproducible.

## Model risk

Treat all current historical FX observations attributed through Algolab as provenance-unverified and potentially misdefined. Using them as the official SBV central reference rate can introduce serious label leakage or target-feature mismatch when compared with a correctly sourced official series. Any model trained on these rows should report this caveat, and verification or replacement should precede production claims based on the FX feature.

## Re-enabling collection

Collection may be enabled only when maintainers provide:

1. an official HTTPS publication source operated or delegated by SBV;
2. the documented rate definition and unit;
3. a verified attribution/license statement;
4. a versioned response schema with observation date, VND/USD rate, unit, and rate type; and
5. tests against representative verified responses and failure cases.

## T14 impact

T14 must remain partially blocked for FX automation. Brent and domestic-fuel work is unaffected, but the FX cron entry is disabled. The full runner may still execute the collector, which will fail closed and prevent a false FX update from being written.
