"""Constants for the FoxESS Control integration."""

DOMAIN = "foxess_control"
CONF_API_KEY = "api_key"
CONF_DEVICE_SERIAL = "device_serial"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_MIN_POWER_CHANGE = "min_power_change"
CONF_MIN_SOC_ON_GRID = "min_soc_on_grid"
DEFAULT_MIN_POWER_CHANGE = 500
DEFAULT_MIN_SOC_ON_GRID = 15
CONF_API_MIN_SOC = "api_min_soc"
DEFAULT_API_MIN_SOC = 11
CONF_POLLING_INTERVAL = "polling_interval"
DEFAULT_POLLING_INTERVAL = 300  # seconds
CONF_SMART_HEADROOM = "charge_headroom"
DEFAULT_SMART_HEADROOM = 10  # percent
DEFAULT_ENTITY_POLLING_INTERVAL = 30  # seconds — entity mode updates are fast
MAX_OVERRIDE_HOURS = 4
CONF_INVERTER_POWER = "inverter_power"
DEFAULT_INVERTER_POWER = 12000  # watts

# Entity-mode configuration (optional foxess_modbus interop)
CONF_WORK_MODE_ENTITY = "work_mode_entity"
CONF_CHARGE_POWER_ENTITY = "charge_power_entity"
CONF_DISCHARGE_POWER_ENTITY = "discharge_power_entity"
CONF_MIN_SOC_ENTITY = "min_soc_entity"
CONF_SOC_ENTITY = "soc_entity"
CONF_LOADS_POWER_ENTITY = "loads_power_entity"
CONF_PV_POWER_ENTITY = "pv_power_entity"
CONF_FEEDIN_ENERGY_ENTITY = "feedin_energy_entity"
PLATFORMS: list[str] = ["binary_sensor", "sensor"]

POLLED_VARIABLES = [
    "SoC",
    "batChargePower",
    "batDischargePower",
    "loadsPower",
    "pvPower",
    "ResidualEnergy",
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
