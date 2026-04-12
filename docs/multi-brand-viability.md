# Multi-Brand Inverter Support: Viability Assessment

## Executive Summary

No other inverter brand's HA integration offers anything equivalent to foxess_control's smart charge/discharge algorithms — deferred paced charging, consumption-aware power adjustment, paced discharge with feed-in limits, suspension logic, and session recovery. The closest alternative is Predbat, which takes a different approach (48-hour price/forecast optimization vs foxess_control's operational "reach X% by time Y" model).

The foxess_control codebase is already partially abstracted via entity mode, which decouples smart algorithms from the FoxESS cloud API. Extracting the generic logic into a shared library is feasible — roughly 80-90% of the code (all smart algorithms, session management, sensors, cards) is brand-agnostic.

## Architecture for Multi-Brand Support

### What's brand-specific (10-20% of codebase)

- API client (auth, endpoints, error handling)
- Inverter class (schedule read/write, SoC query, mode enumeration)
- API quirks (placeholder groups, validation bugs, rate limits)
- Work mode enum values and entity mappings

### What's generic and reusable

- `_calculate_charge_power`, `_calculate_discharge_power` — pacing algorithms
- `_calculate_deferred_start` — consumption-aware deferral
- `_should_suspend_discharge` — consumption-based suspension
- Session persistence and recovery (`_save_session`, `_clear_stored_session`)
- All sensors (smart operations, forecast, binary sensors, debug log)
- Both Lovelace cards (overview, smart operations)
- Service handlers and schemas (force charge/discharge, smart charge/discharge)

### Suggested approach

A shared Python package (`smart_battery_core` or similar) containing the generic algorithms, sensor base classes, and card JS. Each brand integration imports this and provides:

1. A brand-specific `InverterAdapter` implementing a common interface:
   - `async get_soc() -> float`
   - `async set_mode(mode, power_w, min_soc, duration)`
   - `async clear_mode()`
   - `async get_battery_status() -> dict`
2. A config flow mapping brand-specific entities (like foxess_control's entity mode already does)
3. Brand-specific sensor mappings for coordinator data

Entity mode (local Modbus) is the easiest path for most brands — it only requires mapping mode names and entity IDs. Cloud API support can be added later per brand.

## Brand Assessment

### Tier 1: High viability, large feature gap

| Brand | HA Users | Control Path | Native Smart Features | Feature Gap | Notes |
|---|---|---|---|---|---|
| **Huawei** | Very high | Modbus TCP (installer creds) | Static TOU via app | Large | Fastest-growing brand. `huawei_solar` HACS integration provides raw controls (mode, power, SoC) but zero smart algorithms. Requires installer account (`00000a`). |
| **Sungrow** | High | Modbus TCP (immature) | Static TOU via app | Very large | World's largest inverter manufacturer. HA control integration barely exists — SunGather is monitoring only. Huge opportunity but requires building Modbus write support from scratch. |
| **SolaX** | Moderate-high | Modbus TCP | Static modes via app | Large | Well-documented Modbus registers. `solax-modbus` HACS integration exists but write support is sparse. |

| **GoodWe** | Moderate-high | UDP 8899 / Modbus TCP | Static eco mode scheduling | Large | Well-maintained `goodwe` Python library (PyPI, v0.4.10). HACS integration (`mletenay/home-assistant-goodwe-inverter`, 204 stars) provides full control: mode switching, eco charge/discharge, SoC targets, power limits, 4 time-slot scheduling. Official HA integration is monitoring-only. No smart algorithms exist. Predbat partially supported via template inverter. Very similar control model to FoxESS (schedule groups, mode enum, power/SoC params). |

### Tier 2: Good viability, moderate gap

| Brand | HA Users | Control Path | Native Smart Features | Feature Gap | Notes |
|---|---|---|---|---|---|
| **GivEnergy** | Very high (UK) | Modbus TCP + Cloud | Predbat (different approach) | Small-moderate | Best HA ecosystem of any brand. `givenergy-local` provides excellent raw controls. Predbat was built for GivEnergy. The gap is specifically foxess_control-style paced operations — Predbat optimises for price, not "reach SoC by time T". |
| **Lux Power** | Moderate | Modbus TCP (port 8000) | Basic scheduling | Moderate | Good raw controls via HACS integration (power limits, SoC targets, forced modes, time windows). No smart layer. Also sold as EG4 in US market. |
| **Alpha ESS** | Low-moderate | Modbus TCP | Basic TOU | Moderate | Decent Modbus integration with force charge/discharge, SoC cutoffs, scheduling. Small user base limits impact. |
| **Growatt** | Moderate-high | Cloud API | Basic TOU | Moderate-large | Official HA integration has some cloud write support (charge/discharge power, SoC, TOU schedules) but only for certain models. Cloud-only control adds latency. |

### Tier 3: Low viability

| Brand | HA Users | Control Path | Native Smart Features | Feature Gap | Notes |
|---|---|---|---|---|---|
| **Fronius** | Moderate-high | Modbus TCP (manual) | Native TOU optimization | Large | Official HA integration is read-only. GEN24 has writable Modbus registers but no dedicated integration. Fronius's own portal offers decent native TOU — reducing the gap for users who don't need HA control. |
| **SolarEdge** | Very high | Modbus TCP only | StorEdge TOU profiles | Large | Cloud API is monitoring-only. Modbus write requires specific configuration. SolarEdge's native TOU and self-consumption profiles cover basic use cases. Large install base but harder to reach programmatically. |
| **Enphase** | Very high | **Blocked** | Good native automation | N/A | Enphase deliberately removed local API control in firmware 8.2.4225. No path to third-party battery control. Not viable. |

## Recommendations

### Priority 1: Huawei

- Largest addressable audience with the biggest feature gap
- `huawei_solar` already provides the raw control primitives via Modbus
- Entity mode in foxess_control is a near-perfect fit — just map Huawei's mode names and entities
- Installer credentials are widely known, reducing the barrier
- Single Modbus connection limitation is a constraint but manageable

### Priority 2: GivEnergy

- Excellent raw controls already available
- Large UK user base accustomed to HA battery automation
- Predbat is the incumbent but serves a different use case (price optimization vs operational pacing)
- The "smart charge to reach target SoC by deadline" pattern fills a gap Predbat doesn't

### Priority 3: Sungrow

- Massive install base globally with almost no HA control options
- Would require building Modbus write support (higher effort)
- Highest impact per-user since the current state is so poor
- Could partner with SunGather maintainers

### Not recommended

- **Enphase**: No control path exists. Wait for firmware changes.
- **SolarEdge**: Possible but the Modbus path is complex and the native TOU profiles reduce demand.
- **Fronius**: GEN24 Modbus is viable but the user base that needs this over native features is small.

## Effort Estimate

| Phase | Work | Scope |
|---|---|---|
| Extract shared library | Factor out generic algorithms, sensors, cards into a reusable package | 1 integration (foxess_control refactored) |
| First new brand (Huawei) | Implement `InverterAdapter` for Huawei entity mode, map modes/entities, test | Validates the abstraction |
| Each subsequent brand | Adapter + entity mapping + brand-specific tests | Incremental |

The entity mode abstraction in foxess_control already proves the concept — it controls the inverter through HA entities without touching the FoxESS API. Each new brand essentially needs a new entity mapping and mode enum, plus any brand-specific quirks.
