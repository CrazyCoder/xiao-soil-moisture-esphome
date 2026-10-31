"""C++ helpers shared by the soil monitor's YAML packages.

Loading this component makes ESPHome copy its headers into the firmware and
include them from esphome.h, including when the source is a Git repository.
"""

import esphome.config_validation as cv

DEPENDENCIES = ["esp32"]

CONFIG_SCHEMA = cv.Schema({})


async def to_code(config):
    pass
