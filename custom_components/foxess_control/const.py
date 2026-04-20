"""Constants for the FoxESS Control integration.

Brand-agnostic constants are re-exported from ``smart_battery.const``
so existing imports within foxess_control continue to work unchanged.
"""

# Re-export shared constants — these are used by __init__.py, config_flow.py,
# coordinator.py, sensor.py, and tests via ``from .const import ...``.
from .smart_battery.const import (
    CONF_API_MIN_SOC as CONF_API_MIN_SOC,
)
from .smart_battery.const import (
    CONF_BATTERY_CAPACITY_KWH as CONF_BATTERY_CAPACITY_KWH,
)
from .smart_battery.const import (
    CONF_BMS_POLLING_INTERVAL as CONF_BMS_POLLING_INTERVAL,
)
from .smart_battery.const import (
    CONF_CHARGE_POWER_ENTITY as CONF_CHARGE_POWER_ENTITY,
)
from .smart_battery.const import (
    CONF_DISCHARGE_POWER_ENTITY as CONF_DISCHARGE_POWER_ENTITY,
)
from .smart_battery.const import (
    CONF_FEEDIN_ENERGY_ENTITY as CONF_FEEDIN_ENERGY_ENTITY,
)
from .smart_battery.const import (
    CONF_INVERTER_POWER as CONF_INVERTER_POWER,
)
from .smart_battery.const import (
    CONF_LOADS_POWER_ENTITY as CONF_LOADS_POWER_ENTITY,
)
from .smart_battery.const import (
    CONF_MIN_POWER_CHANGE as CONF_MIN_POWER_CHANGE,
)
from .smart_battery.const import (
    CONF_MIN_SOC_ENTITY as CONF_MIN_SOC_ENTITY,
)
from .smart_battery.const import (
    CONF_MIN_SOC_ON_GRID as CONF_MIN_SOC_ON_GRID,
)
from .smart_battery.const import (
    CONF_POLLING_INTERVAL as CONF_POLLING_INTERVAL,
)
from .smart_battery.const import (
    CONF_PV_POWER_ENTITY as CONF_PV_POWER_ENTITY,
)
from .smart_battery.const import (
    CONF_SMART_HEADROOM as CONF_SMART_HEADROOM,
)
from .smart_battery.const import (
    CONF_SOC_ENTITY as CONF_SOC_ENTITY,
)
from .smart_battery.const import (
    CONF_WORK_MODE_ENTITY as CONF_WORK_MODE_ENTITY,
)
from .smart_battery.const import (
    DEFAULT_API_MIN_SOC as DEFAULT_API_MIN_SOC,
)
from .smart_battery.const import (
    DEFAULT_BMS_POLLING_INTERVAL as DEFAULT_BMS_POLLING_INTERVAL,
)
from .smart_battery.const import (
    DEFAULT_ENTITY_POLLING_INTERVAL as DEFAULT_ENTITY_POLLING_INTERVAL,
)
from .smart_battery.const import (
    DEFAULT_INVERTER_POWER as DEFAULT_INVERTER_POWER,
)
from .smart_battery.const import (
    DEFAULT_MIN_POWER_CHANGE as DEFAULT_MIN_POWER_CHANGE,
)
from .smart_battery.const import (
    DEFAULT_MIN_SOC_ON_GRID as DEFAULT_MIN_SOC_ON_GRID,
)
from .smart_battery.const import (
    DEFAULT_POLLING_INTERVAL as DEFAULT_POLLING_INTERVAL,
)
from .smart_battery.const import (
    DEFAULT_SMART_HEADROOM as DEFAULT_SMART_HEADROOM,
)
from .smart_battery.const import (
    MAX_OVERRIDE_HOURS as MAX_OVERRIDE_HOURS,
)
from .smart_battery.const import (
    PLATFORMS as PLATFORMS,
)

# --- FoxESS-specific constants ---
DOMAIN = "foxess_control"
CONF_API_KEY = "api_key"
CONF_DEVICE_SERIAL = "device_serial"

# Web portal credentials (optional, for WebSocket real-time data)
CONF_WEB_USERNAME = "web_username"
CONF_WEB_PASSWORD = "web_password"
CONF_WS_ALL_SESSIONS = "ws_all_sessions"  # legacy boolean; kept for migration

CONF_WS_MODE = "ws_mode"
WS_MODE_AUTO = "auto"
WS_MODE_SMART_SESSIONS = "smart_sessions"
WS_MODE_ALWAYS = "always"

POLLED_VARIABLES = [
    "SoC",
    "batChargePower",
    "batDischargePower",
    "loadsPower",
    "pvPower",
    "batTemperature",
    "gridConsumptionPower",
    "feedinPower",
    "generationPower",
    "batVolt",
    "batCurrent",
    "pv1Power",
    "pv2Power",
    "ambientTemperation",
    "invTemperation",
    # Cumulative energy counters (lifetime kWh)
    "feedin",
    "gridConsumption",
    "generation",
    "chargeEnergyToTal",
    "dischargeEnergyToTal",
    "loads",
    "energyThroughput",
    # Grid connection
    "meterPower",
    "RVolt",
    "RCurrent",
    "RFreq",
    # EPS / backup
    "epsPower",
]
