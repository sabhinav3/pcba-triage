# PCBA triage benchmark — rp2040_easyduino — sample

Created 2026-10-31T20:52:43+00:00 · fault seed 2026 · 30 injected faults (6 classes × 5) · extra-probe budget K=4 · prompt v1 · model `gpt-5-mini`

**Agent not evaluated in this run** (OPENAI_API_KEY not set; 0/30 cached). Rules vs static prior only.

## Summary

| method | n | top-1 net | 95% CI (Wilson) | top-3 net | 95% CI | class acc | refdes acc | mean extra probes | mean bench s | mean wall ms | LLM cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| none | 30 | 5/30 (17%) | [7–34%] | 9/30 (30%) | [17–48%] | 17% | 3% | 0.0 | 345.0 | 0.209 | $0 |
| rules | 30 | 29/30 (97%) | [83–99%] | 29/30 (97%) | [83–99%] | 100% | 97% | 1.933 | 383.667 | 0.243 | $0 |
| agent | 0 | skipped: OPENAI_API_KEY not set; 0/30 cached |  |  |  |  |  |  |  |  |  |

n=30 is small: a 95% Wilson interval at 50% spans roughly ±17 points, so differences under ~25 points between methods are not distinguishable by the intervals alone. The paired comparison below uses the same cases for both methods.

### Paired comparison (same cases)

| A vs B | metric | A-only hits | B-only hits | exact two-sided p |
|---|---|---|---|---|
| rules vs none | top1_net | 24 | 0 | <0.0001 |
| rules vs none | top3_net | 20 | 0 | <0.0001 |

## Per-class accuracy (top-1 net / top-3 net / class, out of 5)

| class | none | rules |
|---|---|---|
| RAIL_SHORT | 2/4/5 of 5 | 5/5/5 of 5 |
| RAIL_OPEN | 3/5/0 of 5 | 5/5/5 of 5 |
| WRONG_VALUE | 0/0/0 of 5 | 5/5/5 of 5 |
| CLOCK_FAIL | 0/0/0 of 5 | 5/5/5 of 5 |
| PIN_BRIDGE | 0/0/0 of 5 | 4/4/5 of 5 |
| SIGNAL_OPEN | 0/0/0 of 5 | 5/5/5 of 5 |

## Class confusion (rows = injected class, columns = top-1 predicted class)

**none**

| truth \ pred | R_SHORT | R_OPEN | WRONG_V | CLOCK | BRIDGE | SIG_OPEN | (none) |
|---|---|---|---|---|---|---|---|
| R_SHORT | 5 | · | · | · | · | · | · |
| R_OPEN | 5 | · | · | · | · | · | · |
| WRONG_V | 5 | · | · | · | · | · | · |
| CLOCK | 5 | · | · | · | · | · | · |
| BRIDGE | 5 | · | · | · | · | · | · |
| SIG_OPEN | 5 | · | · | · | · | · | · |

**rules**

| truth \ pred | R_SHORT | R_OPEN | WRONG_V | CLOCK | BRIDGE | SIG_OPEN | (none) |
|---|---|---|---|---|---|---|---|
| R_SHORT | 5 | · | · | · | · | · | · |
| R_OPEN | · | 5 | · | · | · | · | · |
| WRONG_V | · | · | 5 | · | · | · | · |
| CLOCK | · | · | · | 5 | · | · | · |
| BRIDGE | · | · | · | · | 5 | · | · |
| SIG_OPEN | · | · | · | · | · | 5 | · |

## Per-case results

| case | injected | truth nets | none top-1 (probes) | rules top-1 (probes) |
|---|---|---|---|---|
| c01 | WRONG_VALUE R4 27→2700 | /USB_DP, /USB_R_DP | ❌ R_SHORT +3.3V (0) | ✅ WRONG_V /USB_DP,/USB_R_DP (4) |
| c02 | WRONG_VALUE R5 27→2700 | /USB_DN, /USB_R_DN | ❌ R_SHORT +3.3V (0) | ✅ WRONG_V /USB_DN,/USB_R_DN (4) |
| c03 | SIGNAL_OPEN U2  | /QSPI_SD2 | ❌ R_SHORT +3.3V (0) | ✅ SIG_OPEN /QSPI_SD2 (4) |
| c04 | RAIL_SHORT C13  | VCC | ❌ R_SHORT +3.3V (0) | ✅ R_SHORT VCC (0) |
| c05 | PIN_BRIDGE U2  | /USB_R_DN, /USB_R_DP | ❌ R_SHORT +3.3V (0) | ✅ BRIDGE /USB_R_DP,/USB_R_DN (4) |
| c06 | RAIL_OPEN U1 out_open | VCC, +3.3V | ✅ R_SHORT +3.3V (0) | ✅ R_OPEN VCC,+3.3V (0) |
| c07 | RAIL_SHORT C2  | +1V1 | 🟡 R_SHORT +3.3V (0) | ✅ R_SHORT +1V1 (0) |
| c08 | RAIL_OPEN D1 missing | VBUS, VCC | 🟡 R_SHORT +3.3V (0) | ✅ R_OPEN VBUS,VCC (0) |
| c09 | SIGNAL_OPEN U2  | /QSPI_SCLK | ❌ R_SHORT +3.3V (0) | ✅ SIG_OPEN /QSPI_SCLK (4) |
| c10 | SIGNAL_OPEN U2  | /GPIO25 | ❌ R_SHORT +3.3V (0) | ✅ SIG_OPEN /GPIO25 (1) |
| c11 | PIN_BRIDGE U2  | /GPIO23, /GPIO24 | ❌ R_SHORT +3.3V (0) | ✅ BRIDGE /GPIO24 (4) |
| c12 | CLOCK_FAIL Y1 missing | /Xin, /XoutR | ❌ R_SHORT +3.3V (0) | ✅ CLOCK /Xin,/XoutR (3) |
| c13 | RAIL_OPEN U2 out_open | +3.3V, +1V1 | ✅ R_SHORT +3.3V (0) | ✅ R_OPEN +3.3V,+1V1 (0) |
| c14 | WRONG_VALUE C16 2.7e-11→2.7e-10 | /Xin | ❌ R_SHORT +3.3V (0) | ✅ WRONG_V /Xin,/XoutR (0) |
| c15 | CLOCK_FAIL Y1 cracked | /Xin, /XoutR | ❌ R_SHORT +3.3V (0) | ✅ CLOCK /Xin,/XoutR (3) |
| c16 | PIN_BRIDGE U2  | /GPIO15 | ❌ R_SHORT +3.3V (0) | ❌ BRIDGE /GPIO24 (4) |
| c17 | PIN_BRIDGE U2  | /Xin, /Xout | ❌ R_SHORT +3.3V (0) | ✅ BRIDGE /Xin,/Xout (3) |
| c18 | SIGNAL_OPEN U2  | /Xin | ❌ R_SHORT +3.3V (0) | ✅ SIG_OPEN /Xin (3) |
| c19 | RAIL_OPEN U1 missing | VCC, +3.3V | ✅ R_SHORT +3.3V (0) | ✅ R_OPEN VCC,+3.3V (0) |
| c20 | WRONG_VALUE R14 1000→10000 | /GPIO25, /LED_BUILT_IN1 | ❌ R_SHORT +3.3V (0) | ✅ WRONG_V /GPIO25,/LED_BUILT_IN1 (0) |
| c21 | CLOCK_FAIL Y1 missing | /Xin, /XoutR | ❌ R_SHORT +3.3V (0) | ✅ CLOCK /Xin,/XoutR (3) |
| c22 | CLOCK_FAIL Y1 no_start | /Xin, /XoutR | ❌ R_SHORT +3.3V (0) | ✅ CLOCK /Xin,/XoutR (3) |
| c23 | WRONG_VALUE C17 2.7e-11→2.7e-10 | /XoutR | ❌ R_SHORT +3.3V (0) | ✅ WRONG_V /Xin,/XoutR (0) |
| c24 | CLOCK_FAIL Y1 no_start | /Xin, /XoutR | ❌ R_SHORT +3.3V (0) | ✅ CLOCK /Xin,/XoutR (3) |
| c25 | PIN_BRIDGE U2  | /QSPI_SD1, /QSPI_SS | ❌ R_SHORT +3.3V (0) | ✅ BRIDGE /QSPI_SD1 (4) |
| c26 | RAIL_SHORT C14  | +3.3V | ✅ R_SHORT +3.3V (0) | ✅ R_SHORT +3.3V (0) |
| c27 | RAIL_SHORT C4  | +3.3V | ✅ R_SHORT +3.3V (0) | ✅ R_SHORT +3.3V (0) |
| c28 | RAIL_SHORT C3  | +1V1 | 🟡 R_SHORT +3.3V (0) | ✅ R_SHORT +1V1 (0) |
| c29 | RAIL_OPEN D1 open | VBUS, VCC | 🟡 R_SHORT +3.3V (0) | ✅ R_OPEN VBUS,VCC (0) |
| c30 | SIGNAL_OPEN U2  | /USB_R_DP | ❌ R_SHORT +3.3V (0) | ✅ SIG_OPEN /USB_R_DP (4) |

✅ top-1 net hit · 🟡 hit in top-3 · ❌ miss

## Method and scoring

- Faults are injected into a behavioral model of the board (power tree, current-limited supply, DMM readings, crystal, boot chain, UART, LED, USB). It is not SPICE. Analog readings carry seeded ~1% Gaussian noise (5 ppm on frequency).
- Every method sees the same standard panel (S1–S5, S7, S8) and may take up to K extra probes, enforced in code. Bench time per probe: v 15 s, r_gnd/r_pair 20 s, freq 45 s, logic 10 s, i_supply 10 s. "mean bench s" includes the fixed panel time, which is identical for every method.
- A hypothesis hits if its nets overlap the injected fault's nets. GND is excluded from both sides. Each hypothesis is capped at 3 nets and 3 refdes so listing many nets cannot buy hits. top-k = any hit in the first k hypotheses.
- Class accuracy = top-1 class matches. Refdes accuracy = top-1 refdes overlap; it is low by construction for rail shorts (one rail has many decoupling caps and the panel cannot localize which).
- `none` ignores all measurements (static prior). `rules` is a deterministic decision tree with a fixed probe order per branch. `agent` is an LLM with netlist tools and the same probe tool; its results are cached on disk by hash(case, method, model, prompt version).
