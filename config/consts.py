# Map YAML type strings to Python casting functions
from typing import Dict


YAML_TYPE_MAP = {
    "float64": float,
    "float32": float,
    "int32": int,
    "uint8": int,
    "string": str
}

# Maps unit strings from motor_protocol.yaml → output key suffix
_UNIT_SUFFIX_MAP: Dict[str, str] = {
    "rad/s": "_rad_s",
    "A":     "_a",
    "V":     "_v",
    "Nm":    "_nm",
    "ms":    "_ms",
    "rpm":   "_rpm",
}

# Maps YAML byte_order strings → struct prefix character
_BYTE_ORDER_PREFIX: Dict[str, str] = {
    "little_endian": "<",
    "big_endian":    ">",
    "network":       "!",
}