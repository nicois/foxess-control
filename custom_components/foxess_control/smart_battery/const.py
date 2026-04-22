"""Shared constants for smart battery control integrations.

Brand-specific constants (API keys, device serials, polled variable lists)
remain in each brand's own ``const.py``.
"""

from homeassistant.const import Platform

# --- Battery & algorithm configuration ---
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_MIN_POWER_CHANGE = "min_power_change"
CONF_MIN_SOC_ON_GRID = "min_soc_on_grid"
CONF_SMART_HEADROOM = "charge_headroom"
CONF_POLLING_INTERVAL = "polling_interval"
CONF_BMS_POLLING_INTERVAL = "bms_polling_interval"
CONF_API_MIN_SOC = "api_min_soc"
CONF_INVERTER_POWER = "inverter_power"
CONF_GRID_EXPORT_LIMIT = "grid_export_limit"

DEFAULT_MIN_POWER_CHANGE = 500
DEFAULT_MIN_SOC_ON_GRID = 15
DEFAULT_SMART_HEADROOM = 10  # percent
DEFAULT_POLLING_INTERVAL = 300  # seconds
DEFAULT_BMS_POLLING_INTERVAL = 300  # seconds
DEFAULT_ENTITY_POLLING_INTERVAL = 30  # seconds — entity mode updates are fast
DEFAULT_API_MIN_SOC = 11
DEFAULT_INVERTER_POWER = 12000  # watts
DEFAULT_GRID_EXPORT_LIMIT = 5000  # watts — net export cap

MAX_OVERRIDE_HOURS = 4

# --- Entity-mode configuration (local Modbus interop) ---
CONF_WORK_MODE_ENTITY = "work_mode_entity"
CONF_CHARGE_POWER_ENTITY = "charge_power_entity"
CONF_DISCHARGE_POWER_ENTITY = "discharge_power_entity"
CONF_MIN_SOC_ENTITY = "min_soc_entity"
CONF_SOC_ENTITY = "soc_entity"
CONF_LOADS_POWER_ENTITY = "loads_power_entity"
CONF_PV_POWER_ENTITY = "pv_power_entity"
CONF_FEEDIN_ENERGY_ENTITY = "feedin_energy_entity"
CONF_BAT_CHARGE_POWER_ENTITY = "bat_charge_power_entity"
CONF_BAT_DISCHARGE_POWER_ENTITY = "bat_discharge_power_entity"
CONF_GRID_CONSUMPTION_POWER_ENTITY = "grid_consumption_power_entity"
CONF_FEEDIN_POWER_ENTITY = "feedin_power_entity"

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# --- Service names ---
SERVICE_CLEAR_OVERRIDES = "clear_overrides"
SERVICE_FEEDIN = "feedin"
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_SMART_CHARGE = "smart_charge"
SERVICE_SMART_DISCHARGE = "smart_discharge"

# --- Smart session timing ---
SMART_CHARGE_ADJUST_SECONDS = 300  # 5 minutes
SMART_DISCHARGE_CHECK_SECONDS = 60  # 1 minute

# Cancel a smart session if the SoC entity is unavailable for this many
# consecutive periodic checks (3 x 5 min = 15 minutes).
MAX_SOC_UNAVAILABLE_COUNT = 3

# Cancel a smart session after this many consecutive adapter errors
# (e.g. API timeout, device offline).  With 5-min charge ticks this
# gives ~15 min of tolerance; with 1-min discharge ticks, ~3 min.
MAX_CONSECUTIVE_ADAPTER_ERRORS = 3

# After the circuit breaker opens (MAX_CONSECUTIVE_ADAPTER_ERRORS reached),
# hold position for this many additional ticks before aborting.  With 5-min
# charge ticks this gives ~25 min; with 1-min discharge ticks, ~5 min.
CIRCUIT_BREAKER_TICKS_BEFORE_ABORT = 5

STORAGE_VERSION = 1
