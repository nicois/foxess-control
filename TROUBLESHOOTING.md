# Troubleshooting

Common issues and how to diagnose them. For each problem, work through the checks in order.

## Session won't start

**Symptom:** You call `smart_charge` or `smart_discharge` but nothing happens — no active session appears on the card or in the smart operations sensor.

1. **Is battery capacity configured?** Check Options > Battery Capacity. The algorithms need this to calculate power. A value of 0 or blank prevents sessions from starting.
2. **Is SoC already at the target?** A charge session won't start if the battery is already at or above the target SoC. A discharge session won't start if SoC is at or below min SoC.
3. **Is the time window valid?** Windows must not cross midnight (e.g. 23:00–01:00 is invalid). Use two separate sessions for overnight windows.
4. **Is there a schedule conflict?** In cloud mode, if the inverter has a non-managed work mode (e.g. Backup) in its schedule, the integration refuses to modify it. Check the HA Repairs panel for an "unmanaged work mode" issue.
5. **Is the integration loaded?** Check Settings > Devices & Services > FoxESS Control. If it shows as "not loaded", check the HA logs for setup errors.

## Grid import during discharge

**Symptom:** The house imports from the grid even though the battery is discharging.

1. **Is house load exceeding discharge power?** The discharge power is capped to prevent grid export overshoot. If household consumption spikes above the discharge rate, the shortfall comes from the grid.
2. **Is the safety floor active?** Discharge power is floored at 1.5x peak observed consumption to absorb load spikes between data updates. If the floor is higher than the paced target, the algorithm will use the floor value. This is working as designed — it prevents grid import from load spikes that occur between polls.
3. **Are you using WebSocket mode?** With only REST polling (default 5-minute interval), the integration can't react to load spikes between polls. Enable WebSocket mode (Options > WebSocket Mode > "All smart sessions" or "Always") for ~5-second data updates and faster power adjustments.
4. **Is the feed-in limit set very low?** A low feed-in energy limit forces the algorithm to pace discharge slowly, which may not keep up with household consumption.

## WebSocket not connecting

**Symptom:** The data source badge stays on "API" even though you expect WebSocket data, or `sensor.foxess_data_freshness` shows `api` as the source.

1. **Are web portal credentials configured?** WebSocket requires the optional web credentials (username + password) from the FoxESS Cloud web portal. Check your integration configuration — if the reconfigure step for web credentials was skipped, WS is not available.
2. **Are you in entity mode?** WebSocket is cloud-mode only. If you're using entity mode (foxess_modbus), real-time data comes from local Modbus polling instead.
3. **Check ws_mode setting:** Options > WebSocket Mode must be set to "Auto", "All smart sessions", or "Always". In "Auto" mode, WS only connects during paced forced discharge sessions.
4. **Is the token valid?** The web session token expires after ~12 hours and is automatically refreshed. If the refresh fails (wrong password, FoxESS portal down), WS won't connect. Check HA logs for `web_session` errors.
5. **Network/firewall:** WebSocket connects to the FoxESS Cloud, not your inverter directly. Ensure outbound WebSocket connections to `*.foxesscloud.com` are allowed.

## Session aborted early

**Symptom:** A smart charge or discharge session stops before the window ends or the target is reached.

1. **Check the Repairs panel:** Go to Settings > System > Repairs. Session aborts create a repair issue with the reason (e.g. "repeated errors", "unmanaged work mode").
2. **Consecutive API errors:** After 3 consecutive failures communicating with the inverter API, the session opens a circuit breaker (holding position). If the API doesn't recover within 5 more ticks, the session aborts and reverts to self-use. Check HA logs for "transient error" or "circuit breaker" messages.
3. **SoC unavailable:** If the battery SoC sensor is unavailable for 3 consecutive checks (~15 minutes), the session aborts. This usually indicates a communication problem with the inverter.
4. **Unmanaged work mode detected:** If someone changes the inverter work mode externally (e.g. via the FoxESS app) to a mode the integration doesn't manage (e.g. Backup), the session aborts to avoid conflicts.
5. **End-of-discharge guard:** Discharge sessions suspend ~10 minutes before the window end if the paced power would drop below household consumption. This prevents grid import at the tail of the session.

## Stale data / data not updating

**Symptom:** Sensor values seem frozen, the data freshness badge shows a stale age, or dashboard values don't change.

1. **Check data_source:** Look at `sensor.foxess_data_freshness` — it shows the active source (`ws`, `api`, or `modbus`) and `age_seconds`. If `age_seconds` is growing, data isn't being refreshed.
2. **Check polling interval:** The default REST polling interval is 300 seconds (5 minutes). Values only update on each poll cycle. If you need faster updates, enable WebSocket mode or reduce the polling interval (at the cost of more API quota usage).
3. **API quota:** The FoxESS Cloud allows approximately 1440 requests per day (1 per minute). If you're running multiple integrations against the same account or have reduced the polling interval aggressively, you may be hitting rate limits. Check HA logs for "rate limit" or errno 40400 messages.
4. **Inverter offline:** If the inverter itself is offline (e.g. powered down overnight for hybrid systems without EPS), the API returns stale data. This is normal — values will refresh when the inverter comes back online.
5. **HA restart:** After an HA restart, the first data update may take up to one polling interval. Check that the integration loaded successfully in Settings > Devices & Services.
