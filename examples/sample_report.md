# PowerTrace Correlation Report

**Generated:** `2026-06-10T01:18:42Z`

## Incident: llama-inference-api

- **Window:** 14:32:01 – 14:33:48 (107s)
- **Confidence:** 89%

### Causal Chain

| Time | Severity | Type | Device | Confidence | Lag | Indirect? |
|------|----------|------|--------|------------|-----|-----------|
| 14:32:01 | CRITICAL | voltage_sag | `PDU-B-rack-14` | — | 10.0s | no |
| 14:32:02 | CRITICAL | psu_failover | `server-rack-14-node-3` | 89% | 1.0s | no
|
| 14:32:03 | MEDIUM | cpu_power_cap | `server-rack-14-node-3` | 87% | 1.0s | no 
|
| 14:32:04 | HIGH | thermal_throttle | `GPU-0` | 89% | 1.0s | no |
| 14:32:07 | HIGH | power_cap_applied | `GPU-0` | 89% | 3.0s | no |
| 14:32:11 | IMPACT | trace_degradation | `llama-inference-api` | 89% | 0.0s | 
no |

### Impact

- **P99:** 528ms → 2250ms (+326%)
- **Error rate:** 0.1% → 5.1%
- **Throughput:** 141 → 55 rps (-57%)
- **Estimated cost impact:** `null` (pass `--gpu-rate USD_PER_HR` to compute)


---
**Summary:** 1 incident(s) | 5 events processed | 0 unmapped device(s) | 0 
warning(s)

