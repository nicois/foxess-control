"""Shared constants for smart battery control integrations.

Brand-specific constants (API keys, device serials, polled variable lists)
remain in each brand's own ``const.py``.
"""

# --- Battery & algorithm configuration ---
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_MIN_POWER_CHANGE = "min_power_change"
CONF_MIN_SOC_ON_GRID = "min_soc_on_grid"
CONF_SMART_HEADROOM = "charge_headroom"
CONF_POLLING_INTERVAL = "polling_interval"
CONF_API_MIN_SOC = "api_min_soc"
CONF_INVERTER_POWER = "inverter_power"

DEFAULT_MIN_POWER_CHANGE = 500
DEFAULT_MIN_SOC_ON_GRID = 15
DEFAULT_SMART_HEADROOM = 10  # percent
DEFAULT_POLLING_INTERVAL = 300  # seconds
DEFAULT_ENTITY_POLLING_INTERVAL = 30  # seconds — entity mode updates are fast
DEFAULT_API_MIN_SOC = 11
DEFAULT_INVERTER_POWER = 12000  # watts

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

PLATFORMS: list[str] = ["binary_sensor", "sensor"]

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

STORAGE_VERSION = 1
