"""ESPHome 2026.6.5 rf_bridge fork with validated B1 callbacks."""

from __future__ import annotations

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.components import uart
from esphome.const import (
    CONF_CODE,
    CONF_DURATION,
    CONF_HIGH,
    CONF_ID,
    CONF_LENGTH,
    CONF_LOW,
    CONF_PROTOCOL,
    CONF_RAW,
    CONF_SYNC,
)

DEPENDENCIES = ["uart"]
CODEOWNERS = ["@jesserockz"]

rf_bridge_ns = cg.esphome_ns.namespace("rf_bridge")
RFBridgeComponent = rf_bridge_ns.class_("RFBridgeComponent", cg.Component, uart.UARTDevice)

RFBridgeData = rf_bridge_ns.struct("RFBridgeData")
RFBridgeAdvancedData = rf_bridge_ns.struct("RFBridgeAdvancedData")

RFBridgeSendCodeAction = rf_bridge_ns.class_("RFBridgeSendCodeAction", automation.Action)
RFBridgeSendAdvancedCodeAction = rf_bridge_ns.class_(
    "RFBridgeSendAdvancedCodeAction", automation.Action
)

RFBridgeLearnAction = rf_bridge_ns.class_("RFBridgeLearnAction", automation.Action)

RFBridgeStartAdvancedSniffingAction = rf_bridge_ns.class_(
    "RFBridgeStartAdvancedSniffingAction", automation.Action
)
RFBridgeStopAdvancedSniffingAction = rf_bridge_ns.class_(
    "RFBridgeStopAdvancedSniffingAction", automation.Action
)

RFBridgeStartBucketSniffingAction = rf_bridge_ns.class_(
    "RFBridgeStartBucketSniffingAction", automation.Action
)

RFBridgeBeepAction = rf_bridge_ns.class_("RFBridgeBeepAction", automation.Action)

RFBridgeSendRawAction = rf_bridge_ns.class_("RFBridgeSendRawAction", automation.Action)

CONF_ON_CODE_RECEIVED = "on_code_received"
CONF_ON_ADVANCED_CODE_RECEIVED = "on_advanced_code_received"
CONF_ON_BUCKET_RECEIVED = "on_bucket_received"

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(RFBridgeComponent),
            cv.Optional(CONF_ON_CODE_RECEIVED): automation.validate_automation({}),
            cv.Optional(CONF_ON_ADVANCED_CODE_RECEIVED): automation.validate_automation({}),
            cv.Optional(CONF_ON_BUCKET_RECEIVED): automation.validate_automation({}),
        }
    )
    .extend(uart.UART_DEVICE_SCHEMA)
    .extend(cv.COMPONENT_SCHEMA)
)


_CALLBACK_AUTOMATIONS = (
    automation.CallbackAutomation(
        CONF_ON_CODE_RECEIVED,
        "add_on_code_received_callback",
        [(RFBridgeData, "data")],
    ),
    automation.CallbackAutomation(
        CONF_ON_ADVANCED_CODE_RECEIVED,
        "add_on_advanced_code_received_callback",
        [(RFBridgeAdvancedData, "data")],
    ),
    automation.CallbackAutomation(
        CONF_ON_BUCKET_RECEIVED,
        "add_on_bucket_received_callback",
        [(cg.std_string, "data")],
    ),
)


async def to_code(config: dict[str, object]) -> None:
    """Register the RF bridge and its receive automations."""
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await uart.register_uart_device(var, config)

    await automation.build_callback_automations(var, config, _CALLBACK_AUTOMATIONS)


RFBRIDGE_SEND_CODE_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(RFBridgeComponent),
        cv.Required(CONF_SYNC): cv.templatable(cv.hex_uint16_t),
        cv.Required(CONF_LOW): cv.templatable(cv.hex_uint16_t),
        cv.Required(CONF_HIGH): cv.templatable(cv.hex_uint16_t),
        cv.Required(CONF_CODE): cv.templatable(cv.hex_uint32_t),
    }
)


@automation.register_action(
    "rf_bridge.send_code",
    RFBridgeSendCodeAction,
    RFBRIDGE_SEND_CODE_SCHEMA,
    synchronous=True,
)
async def rf_bridge_send_code_to_code(
    config: dict[str, object],
    action_id: object,
    template_args: object,
    args: object,
) -> object:
    """Build the standard-code transmit action."""
    paren = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_args, paren)
    template_ = await cg.templatable(config[CONF_SYNC], args, cg.uint16)
    cg.add(var.set_sync(template_))
    template_ = await cg.templatable(config[CONF_LOW], args, cg.uint16)
    cg.add(var.set_low(template_))
    template_ = await cg.templatable(config[CONF_HIGH], args, cg.uint16)
    cg.add(var.set_high(template_))
    template_ = await cg.templatable(config[CONF_CODE], args, cg.uint32)
    cg.add(var.set_code(template_))
    return var


RFBRIDGE_ID_SCHEMA = cv.Schema({cv.GenerateID(): cv.use_id(RFBridgeComponent)})


@automation.register_action(
    "rf_bridge.learn", RFBridgeLearnAction, RFBRIDGE_ID_SCHEMA, synchronous=True
)
async def rf_bridge_learnx_to_code(
    config: dict[str, object],
    action_id: object,
    template_args: object,
    _args: object,
) -> object:
    """Build the stock learn action."""
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_args, paren)


@automation.register_action(
    "rf_bridge.start_advanced_sniffing",
    RFBridgeStartAdvancedSniffingAction,
    RFBRIDGE_ID_SCHEMA,
    synchronous=True,
)
async def rf_bridge_start_advanced_sniffing_to_code(
    config: dict[str, object],
    action_id: object,
    template_args: object,
    _args: object,
) -> object:
    """Build the advanced-sniff start action."""
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_args, paren)


@automation.register_action(
    "rf_bridge.stop_advanced_sniffing",
    RFBridgeStopAdvancedSniffingAction,
    RFBRIDGE_ID_SCHEMA,
    synchronous=True,
)
async def rf_bridge_stop_advanced_sniffing_to_code(
    config: dict[str, object],
    action_id: object,
    template_args: object,
    _args: object,
) -> object:
    """Build the advanced-sniff stop action."""
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_args, paren)


@automation.register_action(
    "rf_bridge.start_bucket_sniffing",
    RFBridgeStartBucketSniffingAction,
    RFBRIDGE_ID_SCHEMA,
    synchronous=True,
)
async def rf_bridge_start_bucket_sniffing_to_code(
    config: dict[str, object],
    action_id: object,
    template_args: object,
    _args: object,
) -> object:
    """Build the bucket-sniff start action."""
    paren = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_args, paren)


RFBRIDGE_SEND_ADVANCED_CODE_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(RFBridgeComponent),
        cv.Required(CONF_LENGTH): cv.templatable(cv.hex_uint8_t),
        cv.Required(CONF_PROTOCOL): cv.templatable(cv.hex_uint8_t),
        cv.Required(CONF_CODE): cv.templatable(cv.string),
    }
)


@automation.register_action(
    "rf_bridge.send_advanced_code",
    RFBridgeSendAdvancedCodeAction,
    RFBRIDGE_SEND_ADVANCED_CODE_SCHEMA,
    synchronous=True,
)
async def rf_bridge_send_advanced_code_to_code(
    config: dict[str, object],
    action_id: object,
    template_args: object,
    args: object,
) -> object:
    """Build the advanced-code transmit action."""
    paren = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_args, paren)
    template_ = await cg.templatable(config[CONF_LENGTH], args, cg.uint8)
    cg.add(var.set_length(template_))
    template_ = await cg.templatable(config[CONF_PROTOCOL], args, cg.uint8)
    cg.add(var.set_protocol(template_))
    template_ = await cg.templatable(config[CONF_CODE], args, cg.std_string)
    cg.add(var.set_code(template_))
    return var


RFBRIDGE_SEND_RAW_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(RFBridgeComponent),
        cv.Required(CONF_RAW): cv.templatable(cv.string),
    }
)


@automation.register_action(
    "rf_bridge.send_raw",
    RFBridgeSendRawAction,
    RFBRIDGE_SEND_RAW_SCHEMA,
    synchronous=True,
)
async def rf_bridge_send_raw_to_code(
    config: dict[str, object],
    action_id: object,
    template_args: object,
    args: object,
) -> object:
    """Build the raw-code transmit action."""
    paren = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_args, paren)
    template_ = await cg.templatable(config[CONF_RAW], args, cg.std_string)
    cg.add(var.set_raw(template_))
    return var


RFBRIDGE_BEEP_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(RFBridgeComponent),
        cv.Required(CONF_DURATION): cv.templatable(cv.uint16_t),
    }
)


@automation.register_action(
    "rf_bridge.beep", RFBridgeBeepAction, RFBRIDGE_BEEP_SCHEMA, synchronous=True
)
async def rf_bridge_beep_to_code(
    config: dict[str, object],
    action_id: object,
    template_args: object,
    args: object,
) -> object:
    """Build the buzzer action."""
    paren = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_args, paren)
    template_ = await cg.templatable(config[CONF_DURATION], args, cg.uint16)
    cg.add(var.set_duration(template_))
    return var
