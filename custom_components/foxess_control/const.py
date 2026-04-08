"""Constants for the FoxESS Control integration."""

DOMAIN = "foxess_control"
CONF_API_KEY = "api_key"
CONF_DEVICE_SERIAL = "device_serial"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_MIN_POWER_CHANGE = "min_power_change"
CONF_MIN_SOC_ON_GRID = "min_soc_on_grid"
DEFAULT_MIN_POWER_CHANGE = 500
DEFAULT_MIN_SOC_ON_GRID = 15
CONF_API_MIN_SOC = "api_min_soc"
DEFAULT_API_MIN_SOC = 11
CONF_POLLING_INTERVAL = "polling_interval"
DEFAULT_POLLING_INTERVAL = 300  # seconds
MAX_OVERRIDE_HOURS = 4
PLATFORMS: list[str] = ["binary_sensor", "sensor"]

POLLED_VARIABLES = [
    "SoC",
    "batChargePower",
    "batDischargePower",
    "loadsPower",
    "pvPower",
    "ResidualEnergy",
    "batTemperature",
]
