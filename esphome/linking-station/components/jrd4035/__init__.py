"""ESPHome external component: M5Stack Unit UHF-RFID (U107, JRD-4035)."""

from esphome import automation
import esphome.codegen as cg
from esphome.components import uart
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_TRIGGER_ID

CODEOWNERS = ["@thermitehash"]
DEPENDENCIES = ["uart"]

CONF_TX_POWER = "tx_power"
CONF_REGION = "region"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_RSSI_WINDOW = "rssi_window"
CONF_ON_TAG = "on_tag"
CONF_ON_TIMEOUT = "on_timeout"

jrd4035_ns = cg.esphome_ns.namespace("jrd4035")
JRD4035Component = jrd4035_ns.class_(
    "JRD4035Component", cg.Component, uart.UARTDevice
)
TagTrigger = jrd4035_ns.class_(
    "TagTrigger", automation.Trigger.template(cg.std_string, cg.float_)
)
TimeoutTrigger = jrd4035_ns.class_("TimeoutTrigger", automation.Trigger.template())

REGIONS = {"CHINA_900": 0x01, "US": 0x02, "EUROPE": 0x03, "CHINA_800": 0x04, "KOREA": 0x06}

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(JRD4035Component),
            # Low by default so only the item in hand is read. The U107 is
            # specified up to 26 dBm.
            cv.Optional(CONF_TX_POWER, default=15.0): cv.float_range(min=5.0, max=26.0),
            cv.Optional(CONF_REGION, default="US"): cv.enum(REGIONS, upper=True),
            cv.Optional(
                CONF_SCAN_INTERVAL, default="100ms"
            ): cv.positive_time_period_milliseconds,
            cv.Optional(
                CONF_RSSI_WINDOW, default="1s"
            ): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_ON_TAG): automation.validate_automation(
                {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(TagTrigger)}
            ),
            cv.Optional(CONF_ON_TIMEOUT): automation.validate_automation(
                {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(TimeoutTrigger)}
            ),
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(uart.UART_DEVICE_SCHEMA)
)

FINAL_VALIDATE_SCHEMA = uart.final_validate_device_schema(
    "jrd4035", baud_rate=115200, require_tx=True, require_rx=True
)


async def to_code(config):
    """Generate the C++ for one U107."""
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await uart.register_uart_device(var, config)
    cg.add(var.set_tx_power(config[CONF_TX_POWER]))
    cg.add(var.set_region(config[CONF_REGION]))
    cg.add(var.set_scan_interval(config[CONF_SCAN_INTERVAL]))
    cg.add(var.set_rssi_window(config[CONF_RSSI_WINDOW]))
    for conf in config.get(CONF_ON_TAG, []):
        trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
        await automation.build_automation(
            trigger, [(cg.std_string, "epc"), (cg.float_, "rssi")], conf
        )
    for conf in config.get(CONF_ON_TIMEOUT, []):
        trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
        await automation.build_automation(trigger, [], conf)
