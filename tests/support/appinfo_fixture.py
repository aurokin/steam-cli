"""Write a synthetic binary ``appinfo.vdf`` (format v28 or v29) for platform tests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import struct

MAGIC_V28 = 0x07564428
MAGIC_V29 = 0x07564429

# A depot is its ``config.oslist`` (``None`` for unrestricted) or a mapping
# with optional ``oslist``, ``osarch``, ``language``, ``dlcappid``,
# ``depotfromapp``, ``optional`` and ``manifests`` (``None`` for unreleased)
# keys.
DepotSpec = str | None | Mapping[str, object]
# AppID -> (common.oslist or None, {depot ID: depot spec})
AppSpec = tuple[str | None, Mapping[int | str, DepotSpec]]

# Spacewar-shaped defaults: a native-capable app with an unrestricted depot.
NATIVE_480: dict[int, AppSpec] = {480: ("windows,macos,linux", {481: None})}


def _tree(appid: int, spec: AppSpec) -> dict[str, object]:
    app_oslist, depots = spec
    common: dict[str, object] = {"name": f"Synthetic {appid}"}
    if app_oslist is not None:
        common["oslist"] = app_oslist
    depot_tree: dict[str, object] = {"branches": {"public": {"buildid": "1"}}}
    for depot, spec in depots.items():
        fields = {"oslist": spec} if isinstance(spec, str) else dict(spec or {})
        node: dict[str, object] = {}
        for node_key in ("dlcappid", "depotfromapp", "optional"):
            if node_key in fields:
                node[node_key] = fields.pop(node_key)
        # Released on the public branch unless the spec says otherwise.
        manifests = fields.pop("manifests", {"public": {"gid": "1"}})
        if manifests is not None and "depotfromapp" not in node:
            node["manifests"] = manifests
        if fields:
            node["config"] = fields
        depot_tree[str(depot)] = node
    return {"appinfo": {"appid": appid, "common": common, "depots": depot_tree}}


def _encode(tree: Mapping[str, object], strings: dict[str, int] | None) -> bytes:
    def key(name: str) -> bytes:
        if strings is None:
            return name.encode() + b"\x00"
        return struct.pack("<I", strings.setdefault(name, len(strings)))

    out = bytearray()
    for name, value in tree.items():
        if isinstance(value, Mapping):
            out += b"\x00" + key(name) + _encode(value, strings)
        elif isinstance(value, int):
            out += b"\x02" + key(name) + struct.pack("<i", value)
        else:
            out += b"\x01" + key(name) + str(value).encode() + b"\x00"
    return bytes(out + b"\x08")


def appinfo_bytes(apps: Mapping[int, AppSpec], *, version: int = 29) -> bytes:
    """v29 indexes keys into a trailing string table; v28 inlines them."""

    strings: dict[str, int] | None = {} if version == 29 else None
    body = bytearray()
    for appid, spec in apps.items():
        kv = _encode(_tree(appid, spec), strings)
        header = struct.pack("<IIQ20sI20s", 2, 0, 0, b"\0" * 20, 1, b"\0" * 20)
        body += struct.pack("<II", appid, len(header) + len(kv)) + header + kv
    body += struct.pack("<I", 0)
    if strings is None:
        return struct.pack("<II", MAGIC_V28, 1) + bytes(body)
    table_offset = 16 + len(body)
    table = struct.pack("<I", len(strings)) + b"".join(
        name.encode() + b"\x00" for name, _ in sorted(strings.items(), key=lambda item: item[1])
    )
    return struct.pack("<IIq", MAGIC_V29, 1, table_offset) + bytes(body) + table


def write_appinfo(
    steam_root: Path, apps: Mapping[int, AppSpec] = NATIVE_480, *, version: int = 29,
) -> Path:
    path = steam_root / "appcache" / "appinfo.vdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(appinfo_bytes(apps, version=version))
    return path
