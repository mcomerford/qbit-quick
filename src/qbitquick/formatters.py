import json
import os
from enum import Enum
from typing import Any


class OutputFormat(str, Enum):
    json = "json"
    plain = "plain"


# noinspection PyShadowingBuiltins
def format_torrent_info(torrents_info: list[dict[str, Any]], include_field_names: bool, format: OutputFormat) -> str:
    if format == OutputFormat.plain:
        lines = []

        if include_field_names and torrents_info:
            header_keys = list(torrents_info[0].keys())
            lines.append(",".join(header_keys))

        for row in torrents_info:
            values = row.values() if include_field_names else list(row.values())
            lines.append(",".join(str(v) for v in values))

        return os.linesep.join(lines)
    else:
        if not include_field_names:
            torrent_info_values = [list(row.values()) for row in torrents_info]
            return json.dumps(torrent_info_values, indent=2)
        return json.dumps(torrents_info, indent=2)
