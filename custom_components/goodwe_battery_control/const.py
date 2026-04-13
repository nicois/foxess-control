"""Constants for the GoodWe Battery Control integration."""

from .smart_battery.const import (
    CONF_API_MIN_SOC as CONF_API_MIN_SOC,
)
from .smart_battery.const import (
    CONF_BATTERY_CAPACITY_KWH as CONF_BATTERY_CAPACITY_KWH,
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

DOMAIN = "goodwe_battery_control"

# Default inverter power for GoodWe ET/EH/BT/BH series (W)
DEFAULT_GOODWE_INVERTER_POWER = 5000
