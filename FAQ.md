# Frequently Asked Questions

## Can I run back-to-back sessions?

Yes, but only one charge session and one discharge session can be active at a time. Starting a new smart charge cancels any existing smart charge session (and vice versa for discharge). Starting a smart discharge also cancels any active smart charge, and vice versa.

To schedule multiple sessions throughout the day, use separate HA automations that call the services at the right times. For example, one automation for off-peak charging at 00:30–04:30 and another for afternoon discharge at 16:00–19:00.

## Does the integration work without web portal credentials?

Yes. Web portal credentials (username + password) are optional and only needed for two features:

- **WebSocket real-time data**: ~5-second power updates during sessions (vs 5-minute REST polling)
- **BMS battery temperature**: the actual cell temperature from the battery management system

All other features — smart charge, smart discharge, force operations, sensors, dashboard cards — work with just the API key and device serial number.

## What happens if Home Assistant restarts mid-session?

Sessions are persisted to disk and automatically recovered on restart. When HA starts up, the integration checks if any saved sessions are still within their time window and have a matching schedule on the inverter. If so, the session resumes from its current state (re-reading SoC and recalculating power).

If the session window has expired during the restart, the session is discarded and the inverter is left in whatever mode it was in — typically self-use, since the schedule's safe horizon will have expired.

## What is the FoxESS API quota?

The FoxESS Cloud API allows approximately 1440 requests per day (1 per minute). The integration enforces a minimum 5-second interval between requests. At the default 300-second polling interval, you'll use ~288 requests/day — well within the quota.

Running additional integrations against the same FoxESS account (e.g. foxess-ha) doubles your API usage. If you lower the polling interval, check that your total request rate stays within the quota. Rate-limited requests return errno 40400 and are retried with exponential backoff.

## What is the difference between entity mode and cloud mode?

**Cloud mode** (default) communicates with the inverter through the FoxESS Cloud API. Commands go over the internet to FoxESS servers and back to your inverter. Latency is higher (~5 seconds per command) but no local hardware is needed beyond HA.

**Entity mode** controls the inverter through local entities provided by the [foxess_modbus](https://github.com/nathanmarlor/foxess_modbus) integration. Commands are sent directly to the inverter over your local network via Modbus, which is faster and doesn't depend on cloud availability. To use entity mode, install foxess_modbus first, then map its entities in the FoxESS Control options flow.

Both modes use the same smart battery algorithms — only the transport layer differs.

## How do I determine my battery capacity?

Battery Capacity (in the integration options) is the **usable** capacity of your battery in kWh. The algorithms use this to calculate charge/discharge power targets.

To find your usable capacity:

1. **Check the inverter specs**: FoxESS batteries are typically labelled by nominal capacity (e.g. "HV2600" = 2.56 kWh per module). Multiply by the number of modules.
2. **Measure empirically**: Fully charge the battery, then discharge to the minimum SoC. The total energy discharged (shown in cumulative energy counters) is your usable capacity.

Note that usable capacity is slightly less than nominal capacity and degrades over time.

## What does Smart Headroom do?

Smart Headroom (0–25%, default 0%) adds a buffer to the charge power calculation. When set to 0%, the algorithm calculates exactly the power needed to reach the target SoC by the end of the window. A higher headroom value makes charging start earlier and/or at a higher power to finish ahead of schedule.

This is useful if:
- Your solar forecast is uncertain and you want to ensure the battery is charged even if the sun doesn't cooperate
- You want charging to complete before the end of the cheap rate window to allow time for other loads
- Your battery's charge taper at high SoC is steeper than expected

## How does the discharge safety floor work?

During forced discharge, the integration tracks peak household consumption using an exponential moving average (~4 minute half-life). Discharge power is floored at 1.5x this peak to ensure the battery can absorb load spikes between data updates without importing from the grid.

For example, if your house has been consuming 800W, the discharge floor is 1200W. Even if the pacing algorithm would set a lower power target, the actual discharge power won't drop below the floor. This prevents grid import during inter-poll load spikes.
