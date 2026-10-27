import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import uart
from esphome.const import CONF_ID

slip_ns = cg.esphome_ns.namespace("slip")
SlipComponent = slip_ns.class_("SlipComponent", cg.Component, uart.UARTDevice)

DEPENDENCIES = ["uart"]

CONF_LOCAL_IP = "local_ip"
CONF_GATEWAY = "gateway"
CONF_SUBNET = "subnet"
CONF_DIAGNOSTICS = "diagnostics"

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(SlipComponent),
            cv.Optional(CONF_LOCAL_IP, default="192.168.254.2"): cv.ipv4address,
            cv.Optional(CONF_GATEWAY, default="192.168.254.1"): cv.ipv4address,
            cv.Optional(CONF_SUBNET, default="255.255.255.0"): cv.ipv4address,
            cv.Optional(CONF_DIAGNOSTICS, default=False): cv.boolean,
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(uart.UART_DEVICE_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await uart.register_uart_device(var, config)

    cg.add(var.set_local_ip(str(config[CONF_LOCAL_IP])))
    cg.add(var.set_gateway(str(config[CONF_GATEWAY])))
    cg.add(var.set_subnet(str(config[CONF_SUBNET])))
    cg.add(var.set_diagnostics(config[CONF_DIAGNOSTICS]))
