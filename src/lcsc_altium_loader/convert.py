"""EasyEDA component conversion and native Altium library authoring."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import altium_monkey as altium
from easyeda2kicad import EasyedaFootprintImporter, EasyedaSymbolImporter

from . import __publisher__, __version__
from .ad_refresh import preflight_ad_write
from .client import EASYEDA_COMPONENT_URL, LCSC_DETAIL_URL, ClientError, LCSCClient
from .integrity import existing_components, native_inventory, preserved_inventory, verify_output
from .library_store import LIBRARY_NAMES, LibraryStore, output_metadata, write_json
from .models import ComponentResult

MM_TO_MIL = 39.37007874015748
SYMBOL_UNIT_TO_MIL = 10.0
FOOTPRINT_RAW_TO_MM = 0.254
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_PAIR_RE = re.compile(rf"({_NUMBER})[\s,]+({_NUMBER})")
_ARC_RE = re.compile(
    rf"M\s*({_NUMBER})[\s,]+({_NUMBER})\s*A\s*({_NUMBER})[\s,]+({_NUMBER})[\s,]+({_NUMBER})[\s,]+([01])[\s,]+([01])[\s,]+({_NUMBER})[\s,]+({_NUMBER})",
    re.IGNORECASE,
)


class ConversionError(RuntimeError):
    """Component data cannot be safely represented by the public authoring API."""

    def __init__(self, message: str, *, warnings: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.warnings = list(warnings)


class BatchCancelled(RuntimeError):
    """A batch was cancelled before publication; existing output is retained."""


def _clean(value: Any, fallback: str = "ITEM") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_.")
    return (text or fallback)[:90]


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _color(value: Any, default: int = 0) -> int:
    if not isinstance(value, str):
        return default
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if not match:
        return default
    rgb = match.group(1)
    return int(rgb, 16)


def _line_width(value: Any, default: int = 1) -> int:
    try:
        return max(0, min(3, int(float(value))))
    except (TypeError, ValueError):
        return default


def _symbol_counts(symbol: Any) -> dict[str, int]:
    sub_symbols = list(getattr(symbol, "sub_symbols", []) or [])
    units = sub_symbols or [symbol]
    return {
        "P": sum(len(getattr(unit, "pins", []) or []) for unit in units),
        "R": sum(len(getattr(unit, "rectangles", []) or []) for unit in units),
        "E": sum(len(getattr(unit, "ellipses", []) or []) for unit in units),
        "CIRCLE": sum(len(getattr(unit, "circles", []) or []) for unit in units),
        "ARC": sum(len(getattr(unit, "arcs", []) or []) for unit in units),
        "POLYLINE": sum(len(getattr(unit, "polylines", []) or []) for unit in units),
        "POLYGON": sum(len(getattr(unit, "polygons", []) or []) for unit in units),
        "PATH": sum(len(getattr(unit, "paths", []) or []) for unit in units),
        "TEXT": sum(len(getattr(unit, "texts", []) or []) for unit in units),
        "SUB_SYMBOL": len(sub_symbols),
    }


def _footprint_counts(footprint: Any) -> dict[str, int]:
    model_count = 1 if getattr(footprint, "model_3d", None) is not None else 0
    return {
        "PAD": len(getattr(footprint, "pads", []) or []),
        "TRACK": len(getattr(footprint, "tracks", []) or []),
        "ARC": len(getattr(footprint, "arcs", []) or []),
        "CIRCLE": len(getattr(footprint, "circles", []) or []),
        "SOLIDREGION": len(getattr(footprint, "solid_regions", []) or []),
        "SVGNODE": model_count,
        "HOLE": len(getattr(footprint, "holes", []) or []),
        "VIA": len(getattr(footprint, "vias", []) or []),
        "RECT": len(getattr(footprint, "rectangles", []) or []),
        "TEXT": len(getattr(footprint, "texts", []) or []),
    }


def _raw_pairs(text: str) -> list[tuple[float, float]]:
    return [(_number(x), _number(y)) for x, y in _PAIR_RE.findall(text or "")]


def _path_points(path: str) -> list[tuple[float, float]]:
    """Read the polygon subset used by EasyEDA SOLIDREGION paths."""
    if re.search(r"[CQSAHTVZ]", path or "", re.IGNORECASE):
        # M/L/Z are the only commands supported below.  Z is harmless.
        commands = re.findall(r"[A-Za-z]", path or "")
        if any(c.upper() not in {"M", "L", "Z"} for c in commands):
            return []
    return _raw_pairs(path)


def _symbol_path_tokens(path: str) -> list[str]:
    """Tokenize the absolute M/L/C/Q/Z subset emitted by EasyEDA symbols."""
    tokens = re.findall(rf"[A-Za-z]|{_NUMBER}", (path or "").replace(",", " "))
    unsupported = [token for token in tokens if token.isalpha() and token not in {"M", "L", "C", "Q", "Z"}]
    if unsupported:
        raise ConversionError(
            "unsupported symbol PATH command(s): " + ", ".join(sorted(set(unsupported)))
        )
    return tokens


def _svg_arc(path: str) -> tuple[tuple[float, float], float, float, float] | None:
    match = _ARC_RE.search(path or "")
    if not match:
        return None
    x1, y1, rx, ry, rotation, large, sweep, x2, y2 = [float(x) for x in match.groups()]
    if abs(rx - ry) > 1e-5 or abs(rotation) > 1e-5 or rx <= 0:
        return None
    radius = abs(rx)
    # Circular SVG endpoint-to-center conversion.  This is the W3C algorithm
    # with the rotation terms omitted because the supported case is circular.
    dx = (x1 - x2) / 2.0
    dy = (y1 - y2) / 2.0
    distance_sq = dx * dx + dy * dy
    if distance_sq <= 1e-24:
        return None
    if distance_sq > radius * radius:
        radius = math.sqrt(distance_sq)
    radicand = max(0.0, (radius * radius - distance_sq) / distance_sq)
    coefficient = (-1.0 if int(large) == int(sweep) else 1.0) * math.sqrt(radicand)
    cx = (x1 + x2) / 2.0 + coefficient * dy
    cy = (y1 + y2) / 2.0 - coefficient * dx
    start = math.atan2(y1 - cy, x1 - cx)
    end = math.atan2(y2 - cy, x2 - cx)
    delta = end - start
    if int(sweep) and delta < 0:
        delta += math.tau
    elif not int(sweep) and delta > 0:
        delta -= math.tau
    return (cx, cy), radius, math.degrees(start), math.degrees(start + delta)


def _raw_to_local_mils(value: float, origin: float) -> float:
    return (value - origin) * FOOTPRINT_RAW_TO_MM * MM_TO_MIL


def _footprint_layer(layer_id: Any, warnings: list[str]) -> int:
    layer = int(_number(layer_id, 3))
    mapping = {
        1: int(altium.PcbLayer.TOP),
        2: int(altium.PcbLayer.BOTTOM),
        3: int(altium.PcbLayer.TOP_OVERLAY),
        4: int(altium.PcbLayer.BOTTOM_OVERLAY),
        5: int(altium.PcbLayer.TOP_PASTE),
        6: int(altium.PcbLayer.BOTTOM_PASTE),
        7: int(altium.PcbLayer.TOP_SOLDER),
        8: int(altium.PcbLayer.BOTTOM_SOLDER),
        10: int(altium.PcbLayer.KEEPOUT),
        11: int(altium.PcbLayer.MULTI_LAYER),
        13: int(altium.PcbLayer.MECHANICAL_13),
        14: int(altium.PcbLayer.MECHANICAL_14),
        15: int(altium.PcbLayer.MECHANICAL_1),
        99: int(altium.PcbLayer.MECHANICAL_15),
        101: int(altium.PcbLayer.MECHANICAL_1),
    }
    if layer not in mapping:
        message = f"EasyEDA layer {layer} mapped to Mechanical 1"
        if message not in warnings:
            warnings.append(message)
        return int(altium.PcbLayer.MECHANICAL_1)
    if layer == 101:
        message = "EasyEDA layer 101 mapped to Mechanical 1"
        if message not in warnings:
            warnings.append(message)
    return mapping[layer]


def _raw_origin(footprint: Any) -> tuple[float, float]:
    bbox = footprint.bbox
    return (_number(getattr(bbox, "x_px", None), _number(getattr(bbox, "x", 0.0)) / FOOTPRINT_RAW_TO_MM), _number(getattr(bbox, "y_px", None), _number(getattr(bbox, "y", 0.0)) / FOOTPRINT_RAW_TO_MM))


def _symbol_xy(x: Any, y: Any, origin_x: float, origin_y: float) -> tuple[int, int]:
    return (
        round((_number(x) - origin_x) * SYMBOL_UNIT_TO_MIL),
        round(-(_number(y) - origin_y) * SYMBOL_UNIT_TO_MIL),
    )


def _import_symbol(data: dict[str, Any]) -> Any:
    """Keep raw pin paths and the authored origin, which KiCad import normalizes."""
    symbol = EasyedaSymbolImporter(data).get_symbol()
    head = data["dataStr"]["head"]
    for unit, source in zip([symbol, *symbol.sub_symbols], [data, *data.get("subparts", [])], strict=True):
        # Bounding-box centres can introduce off-grid electrical connection points.
        unit.bbox.x = _number(head.get("x"), unit.bbox.x)
        unit.bbox.y = _number(head.get("y"), unit.bbox.y)
        paths = {}
        for shape in source["dataStr"]["shape"]:
            if shape.startswith("P~"):
                fields = shape.split("^^")
                paths[fields[0].split("~")[7]] = fields[2].split("~")[0]
        for pin in unit.pins:
            # EeSymbolPinPath replaces v with h. Restore it before native authoring.
            pin.pin_path.path = paths[pin.settings.id]
    return symbol


def _import_footprint(data: dict[str, Any]) -> Any:
    """Retain native Y/N plated flags rather than the importer's generic bool cast."""
    footprint = EasyedaFootprintImporter(data).get_footprint()
    plated_by_id = {}
    for shape in data["packageDetail"]["dataStr"]["shape"]:
        if shape.startswith("PAD~"):
            fields = shape.split("~")
            flag = fields[15].strip().upper()
            if flag not in {"Y", "N"}:
                raise ConversionError(f"unrecognized source pad plated flag: {flag!r}")
            plated_by_id[fields[12]] = flag == "Y"
    for pad in footprint.pads:
        pad.is_plated = plated_by_id[pad.id]
    return footprint


def _symbol_pin_geometry(pin: Any, origin_x: float, origin_y: float) -> tuple[float, float, int, float]:
    """Return Altium body-end, outward orientation, and length from source endpoints."""
    dot = getattr(pin, "pin_dot", None)
    tip = (
        _number(getattr(dot, "dot_x", pin.settings.pos_x)),
        _number(getattr(dot, "dot_y", pin.settings.pos_y)),
    )
    match = re.fullmatch(
        rf"\s*(?:M\s*({_NUMBER})[\s,]+({_NUMBER})\s*)?([hv])\s*({_NUMBER})\s*",
        str(pin.pin_path.path),
    )
    if match is None:
        raise ConversionError(f"unsupported pin path: {pin.pin_path.path}")
    start = (float(match[1]), float(match[2])) if match[1] is not None else tip
    axis, delta = match[3], float(match[4])
    end = (start[0] + (delta if axis == "h" else 0), start[1] + (delta if axis == "v" else 0))
    if math.dist(tip, start) < 1e-6:
        body = end
    elif math.dist(tip, end) < 1e-6:
        body = start
    else:
        raise ConversionError("pin connection point does not match either path endpoint")
    length = math.dist(body, tip) * SYMBOL_UNIT_TO_MIL
    if length <= 0:
        raise ConversionError("pin has zero length")
    # Altium stores the body end and points OUT to its connection point.
    orientation = round(math.degrees(math.atan2(body[1] - tip[1], tip[0] - body[0])) / 90) % 4
    return ((body[0] - origin_x) * SYMBOL_UNIT_TO_MIL,
            -(body[1] - origin_y) * SYMBOL_UNIT_TO_MIL, orientation, length)


def _add_symbol_path(
    symbol: Any,
    path_data: Any,
    origin_x: float,
    origin_y: float,
    owner_part_id: int,
) -> int:
    tokens = _symbol_path_tokens(str(getattr(path_data, "paths", "")))
    color = _color(getattr(path_data, "stroke_color", ""))
    width = altium.LineWidth(_line_width(getattr(path_data, "stroke_width", 1)))
    filled = bool(getattr(path_data, "fill_color", False))
    current = (0.0, 0.0)
    first: tuple[float, float] | None = None
    points: list[tuple[int, int]] = []
    written = 0

    def flush(*, closed: bool = False) -> None:
        nonlocal written
        if len(points) < 2:
            points.clear()
            return
        if (closed or filled) and len(points) >= 3:
            if points[0] != points[-1]:
                points.append(points[0])
            symbol.add_polygon(
                list(points), color=color, line_width=width, is_solid=filled,
                owner_part_id=owner_part_id,
            )
        else:
            symbol.add_polyline(
                list(points), color=color, line_width=width,
                owner_part_id=owner_part_id,
            )
        written += 1
        points.clear()

    index = 0
    try:
        while index < len(tokens):
            command = tokens[index]
            if command in {"M", "L"}:
                if command == "M" and points:
                    flush()
                current = (float(tokens[index + 1]), float(tokens[index + 2]))
                if command == "M":
                    first = current
                points.append(_symbol_xy(*current, origin_x, origin_y))
                index += 3
            elif command == "C":
                control_1 = (float(tokens[index + 1]), float(tokens[index + 2]))
                control_2 = (float(tokens[index + 3]), float(tokens[index + 4]))
                end = (float(tokens[index + 5]), float(tokens[index + 6]))
                flush()
                symbol.add_bezier(
                    [
                        _symbol_xy(*current, origin_x, origin_y),
                        _symbol_xy(*control_1, origin_x, origin_y),
                        _symbol_xy(*control_2, origin_x, origin_y),
                        _symbol_xy(*end, origin_x, origin_y),
                    ],
                    color=color,
                    line_width=width,
                    owner_part_id=owner_part_id,
                )
                written += 1
                current = end
                points.append(_symbol_xy(*current, origin_x, origin_y))
                index += 7
            elif command == "Q":
                control = (float(tokens[index + 1]), float(tokens[index + 2]))
                end = (float(tokens[index + 3]), float(tokens[index + 4]))
                control_1 = (
                    current[0] + 2.0 * (control[0] - current[0]) / 3.0,
                    current[1] + 2.0 * (control[1] - current[1]) / 3.0,
                )
                control_2 = (
                    end[0] + 2.0 * (control[0] - end[0]) / 3.0,
                    end[1] + 2.0 * (control[1] - end[1]) / 3.0,
                )
                flush()
                symbol.add_bezier(
                    [
                        _symbol_xy(*current, origin_x, origin_y),
                        _symbol_xy(*control_1, origin_x, origin_y),
                        _symbol_xy(*control_2, origin_x, origin_y),
                        _symbol_xy(*end, origin_x, origin_y),
                    ],
                    color=color,
                    line_width=width,
                    owner_part_id=owner_part_id,
                )
                written += 1
                current = end
                points.append(_symbol_xy(*current, origin_x, origin_y))
                index += 5
            elif command == "Z":
                if first is not None and points:
                    points.append(_symbol_xy(*first, origin_x, origin_y))
                flush(closed=True)
                index += 1
            else:
                raise ValueError(f"unexpected token {command!r}")
    except (IndexError, TypeError, ValueError) as exc:
        raise ConversionError(f"malformed symbol PATH: {exc}") from exc
    flush()
    if not written:
        raise ConversionError("symbol PATH contains no drawable segments")
    return written


def _add_symbol_unit(
    symbol: Any,
    unit: Any,
    *,
    owner_part_id: int,
    force_passive: bool,
) -> dict[str, int]:
    origin_x = _number(getattr(unit.bbox, "x", 0.0))
    origin_y = _number(getattr(unit.bbox, "y", 0.0))
    written = {key: 0 for key in ("P", "R", "E", "CIRCLE", "ARC", "POLYLINE", "POLYGON", "PATH", "TEXT")}
    electrical_map = {
        0: altium.PinElectrical.PASSIVE,
        1: altium.PinElectrical.INPUT,
        2: altium.PinElectrical.OUTPUT,
        3: altium.PinElectrical.IO,
        4: altium.PinElectrical.POWER,
    }
    authored_pins = []
    for pin in getattr(unit, "pins", []) or []:
        settings = pin.settings
        pos_x, pos_y, rotation, length = _symbol_pin_geometry(pin, origin_x, origin_y)
        source_electrical = getattr(settings.type, "value", settings.type)
        electrical = altium.PinElectrical.PASSIVE if force_passive else electrical_map.get(source_electrical, altium.PinElectrical.PASSIVE)
        authored_pin = altium.make_sch_pin(
            designator=str(settings.spice_pin_number),
            name=str(getattr(pin.name, "text", "") or ""),
            location_mils=altium.SchPointMils(pos_x, pos_y),
            orientation=altium.Rotation90(rotation),
            electrical_type=electrical,
            length_mils=length,
            name_visible=bool(getattr(pin.name, "is_displayed", True)),
            designator_visible=True,
            owner_part_id=owner_part_id,
        )
        authored_pins.append(authored_pin)
        written["P"] += 1
    for rectangle in getattr(unit, "rectangles", []) or []:
        x1, y1 = _symbol_xy(rectangle.pos_x, rectangle.pos_y, origin_x, origin_y)
        x2, y2 = _symbol_xy(rectangle.pos_x + rectangle.width, rectangle.pos_y + rectangle.height, origin_x, origin_y)
        symbol.add_rectangle(
            min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2),
            color=_color(getattr(rectangle, "stroke_color", "")),
            line_width=altium.LineWidth(_line_width(getattr(rectangle, "stroke_width", 1))),
            is_solid=getattr(rectangle, "fill_color", "none") != "none",
            owner_part_id=owner_part_id,
        )
        written["R"] += 1
    for ellipse in getattr(unit, "ellipses", []) or []:
        cx, cy = _symbol_xy(ellipse.center_x, ellipse.center_y, origin_x, origin_y)
        symbol.add_ellipse(
            cx, cy,
            round(_number(ellipse.radius_x) * SYMBOL_UNIT_TO_MIL),
            round(_number(ellipse.radius_y) * SYMBOL_UNIT_TO_MIL),
            color=_color(getattr(ellipse, "stroke_color", "")),
            is_solid=bool(getattr(ellipse, "fill_color", False)),
            owner_part_id=owner_part_id,
        )
        written["E"] += 1
    for circle in getattr(unit, "circles", []) or []:
        cx, cy = _symbol_xy(circle.center_x, circle.center_y, origin_x, origin_y)
        radius = round(_number(circle.radius) * SYMBOL_UNIT_TO_MIL)
        symbol.add_ellipse(
            cx, cy, radius, radius,
            color=_color(getattr(circle, "stroke_color", "")),
            is_solid=bool(getattr(circle, "fill_color", False)),
            owner_part_id=owner_part_id,
        )
        written["CIRCLE"] += 1
    for arc in getattr(unit, "arcs", []) or []:
        path = list(getattr(arc, "path", []) or [])
        if len(path) != 2:
            raise ConversionError("malformed symbol ARC")
        move, curve = path
        required_move = ("start_x", "start_y")
        required_curve = (
            "radius_x", "radius_y", "x_axis_rotation", "flag_large_arc",
            "flag_sweep", "end_x", "end_y",
        )
        if not all(hasattr(move, field) for field in required_move) or not all(
            hasattr(curve, field) for field in required_curve
        ):
            raise ConversionError("unsupported symbol ARC path structure")
        path_text = (
            f"M {move.start_x} {move.start_y} A {curve.radius_x} {curve.radius_y} "
            f"{curve.x_axis_rotation} {int(bool(curve.flag_large_arc))} "
            f"{int(bool(curve.flag_sweep))} {curve.end_x} {curve.end_y}"
        )
        geometry = _svg_arc(path_text)
        if geometry is None:
            raise ConversionError("elliptical or rotated symbol ARC is not safely supported")
        (cx, cy), radius, start, end = geometry
        local_x, local_y = _symbol_xy(cx, cy, origin_x, origin_y)
        start, end = (-end, -start) if end > start else (-start, -end)
        symbol.add_arc(
            local_x,
            local_y,
            round(radius * SYMBOL_UNIT_TO_MIL),
            start_angle=start,
            end_angle=end,
            color=_color(getattr(arc, "stroke_color", "")),
            line_width=altium.LineWidth(_line_width(getattr(arc, "stroke_width", 1))),
            owner_part_id=owner_part_id,
        )
        written["ARC"] += 1
    for polyline in getattr(unit, "polylines", []) or []:
        vertices = [
            _symbol_xy(x, y, origin_x, origin_y)
            for x, y in _raw_pairs(str(getattr(polyline, "points", "")))
        ]
        if len(vertices) < 2:
            raise ConversionError("symbol POLYLINE has fewer than two points")
        symbol.add_polyline(
            vertices,
            color=_color(getattr(polyline, "stroke_color", "")),
            line_width=altium.LineWidth(_line_width(getattr(polyline, "stroke_width", 1))),
            owner_part_id=owner_part_id,
        )
        written["POLYLINE"] += 1
    for polygon in getattr(unit, "polygons", []) or []:
        vertices = [
            _symbol_xy(x, y, origin_x, origin_y)
            for x, y in _raw_pairs(str(getattr(polygon, "points", "")))
        ]
        if len(vertices) < 3:
            raise ConversionError("symbol POLYGON has fewer than three points")
        if vertices[0] != vertices[-1]:
            vertices.append(vertices[0])
        symbol.add_polygon(
            vertices,
            color=_color(getattr(polygon, "stroke_color", "")),
            line_width=altium.LineWidth(_line_width(getattr(polygon, "stroke_width", 1))),
            is_solid=bool(getattr(polygon, "fill_color", False)),
            owner_part_id=owner_part_id,
        )
        written["POLYGON"] += 1
    for path in getattr(unit, "paths", []) or []:
        written["PATH"] += _add_symbol_path(symbol, path, origin_x, origin_y, owner_part_id)
    for label in getattr(unit, "texts", []) or []:
        x, y = _symbol_xy(label.pos_x, label.pos_y, origin_x, origin_y)
        orientation = altium.TextOrientation(int(round(_number(getattr(label, "rotation", 0.0)) / 90.0)) % 4)
        symbol.add_label(
            str(getattr(label, "text", "") or ""), x, y,
            orientation=orientation,
            owner_part_id=owner_part_id,
        )
        written["TEXT"] += 1
    # Native object order is paint order. An opaque body added after pins
    # obscures names inside the symbol even when their visibility is enabled.
    for authored_pin in authored_pins:
        symbol.add_pin(authored_pin)
    return written


def _add_symbol(symbol_lib: Any, symbol_data: Any, name: str, footprint_name: str, component: dict[str, Any], warnings: list[str]) -> tuple[Any, dict[str, int]]:
    sub_symbols = list(getattr(symbol_data, "sub_symbols", []) or [])
    units = sub_symbols or [symbol_data]
    symbol = symbol_lib.add_symbol(name, description=str(component.get("description") or ""))
    if sub_symbols:
        symbol.set_part_count(len(units))
    prefix = str(getattr(symbol_data.info, "prefix", "U?") or "U?")
    symbol.add_designator(prefix, 0, 0)
    symbol.add_parameter("Comment", str(component.get("mpn") or component.get("name") or ""), is_hidden=False)
    symbol.add_parameter("LCSC", str(component.get("code") or ""), is_hidden=True)
    symbol.add_parameter("MPN", str(component.get("mpn") or ""), is_hidden=True)
    symbol.add_parameter("Manufacturer", str(component.get("manufacturer") or ""), is_hidden=True)
    symbol.add_parameter("Source URL", str((component.get("source_urls") or {}).get("lcsc") or ""), is_hidden=True)
    symbol.add_footprint(footprint_name, library_name="LCSC.PcbLib")
    prefix_key = re.sub(r"[^A-Za-z]", "", prefix).upper()
    total_pins = sum(len(getattr(unit, "pins", []) or []) for unit in units)
    force_passive = total_pins <= 2 or prefix_key in {
        "R", "C", "L", "D", "LED", "FB", "F", "FUSE", "SW", "J", "JP", "TP", "T", "Y", "XTAL"
    }
    written = {key: 0 for key in ("P", "R", "E", "CIRCLE", "ARC", "POLYLINE", "POLYGON", "PATH", "TEXT")}
    for index, unit in enumerate(units, start=1):
        unit_counts = _add_symbol_unit(
            symbol,
            unit,
            owner_part_id=index,
            force_passive=force_passive,
        )
        for key, count in unit_counts.items():
            written[key] += count
    return symbol, written


def _add_footprint(pcb_lib: Any, footprint_data: Any, name: str, component: dict[str, Any], warnings: list[str], *, step_data: bytes | None = None, three_d_requested: bool = False) -> tuple[Any, dict[str, int], dict[str, Any]]:
    counts = _footprint_counts(footprint_data)
    if counts["SVGNODE"] and "SVGNODE metadata retained only for optional 3D; graphic node skipped" not in warnings:
        warnings.append("SVGNODE metadata retained only for optional 3D; graphic node skipped")
    bbox_x, bbox_y = _raw_origin(footprint_data)
    bbox = footprint_data.bbox
    origin_mm = (_number(getattr(bbox, "x", 0.0)), _number(getattr(bbox, "y", 0.0)))
    footprint = pcb_lib.add_footprint(name, description=str(component.get("package") or ""))
    footprint.set_parameter("Comment", str(component.get("mpn") or component.get("name") or ""))
    footprint.set_parameter("LCSC", str(component.get("code") or ""))
    footprint.set_parameter("MPN", str(component.get("mpn") or ""))
    footprint.set_parameter("Manufacturer", str(component.get("manufacturer") or ""))
    footprint.set_parameter("Source URL", str((component.get("source_urls") or {}).get("lcsc") or ""))
    custom_pad_count = 0
    for pad in getattr(footprint_data, "pads", []) or []:
        px = (_number(pad.center_x) - origin_mm[0]) * MM_TO_MIL
        py = -(_number(pad.center_y) - origin_mm[1]) * MM_TO_MIL
        pad_rotation = -_number(getattr(pad, "rotation", 0.0))
        shape = str(getattr(pad, "shape", "RECT") or "RECT").upper()
        designator = str(getattr(pad, "number", "") or "")
        number_match = re.search(r"\(([^()]*)\)", designator)
        if number_match:
            designator = number_match.group(1)
        if shape == "RECT":
            al_shape = "RECTANGLE"
            kwargs: dict[str, Any] = {}
        elif shape in {"CIRCLE", "ROUND"}:
            al_shape = "CIRCLE"
            kwargs = {}
        elif shape in {"OVAL", "ELLIPSE", "ROUNDED_RECTANGLE"}:
            al_shape = "ROUNDED_RECTANGLE"
            kwargs = {"corner_radius_percent": 100}
        elif shape in {"POLYGON", "CUSTOM"}:
            if _number(getattr(pad, "hole_radius", 0.0)) > 0:
                raise ConversionError("custom through-hole pads are not safely supported", warnings=warnings)
            raw_points = _raw_pairs(str(getattr(pad, "points", "")))
            if len(raw_points) < 3:
                raise ConversionError("custom pad has fewer than three outline points", warnings=warnings)
            outline = [
                (
                    _raw_to_local_mils(x, bbox_x) - px,
                    -_raw_to_local_mils(y, bbox_y) - py,
                )
                for x, y in raw_points
            ]
            layer = _footprint_layer(getattr(pad, "layer_id", 1), warnings)
            footprint.add_custom_pad(
                designator=designator,
                position_mils=(px, py),
                outline_points_mils=outline,
                layer=layer,
                anchor_diameter_mils=1.0,
                anchor_shape=altium.PadShape.CIRCLE,
                anchor_rotation_degrees=0.0,
            )
            custom_pad_count += 1
            continue
        else:
            raise ConversionError(f"unsupported pad shape: {shape}", warnings=warnings)
        layer = _footprint_layer(getattr(pad, "layer_id", 1), warnings)
        hole_size = _number(getattr(pad, "hole_radius", 0.0)) * 2.0 * MM_TO_MIL
        hole_length = _number(getattr(pad, "hole_length", 0.0)) * MM_TO_MIL
        if hole_size > 0 and hole_length > hole_size:
            # Slot rotation in Altium is relative to the pad, whereas EasyEDA
            # stores the hole endpoints in absolute, already-rotated SVG space.
            hole_points = _raw_pairs(str(getattr(pad, "slot_outline", "")))
            slot_angle = pad_rotation
            if len(hole_points) >= 2:
                start, end = hole_points[:2]
                slot_angle = math.degrees(math.atan2(-(end[1] - start[1]), end[0] - start[0]))
            kwargs.update(
                slot_length_mils=hole_length,
                slot_rotation_degrees=slot_angle - pad_rotation,
                hole_shape=altium.PadHoleShape.SLOT,
            )
        footprint.add_pad(designator=designator, position_mils=(px, py), width_mils=max(0.01, _number(pad.width) * MM_TO_MIL), height_mils=max(0.01, _number(pad.height) * MM_TO_MIL), layer=layer, shape=al_shape, rotation_degrees=pad_rotation, hole_size_mils=hole_size, plated=bool(getattr(pad, "is_plated", False)), **kwargs)
    for hole in getattr(footprint_data, "holes", []) or []:
        diameter = _number(hole.radius) * 2.0 * MM_TO_MIL
        if diameter <= 0:
            raise ConversionError("footprint HOLE has a non-positive diameter", warnings=warnings)
        footprint.add_pad(
            designator="",
            position_mils=(
                (_number(hole.center_x) - origin_mm[0]) * MM_TO_MIL,
                -(_number(hole.center_y) - origin_mm[1]) * MM_TO_MIL,
            ),
            width_mils=diameter,
            height_mils=diameter,
            layer=altium.PcbLayer.MULTI_LAYER,
            shape=altium.PadShape.CIRCLE,
            hole_size_mils=diameter,
            plated=False,
        )
    for via in getattr(footprint_data, "vias", []) or []:
        diameter = _number(via.diameter) * MM_TO_MIL
        hole_size = _number(via.radius) * 2.0 * MM_TO_MIL
        if diameter <= 0 or hole_size <= 0:
            raise ConversionError("footprint VIA has invalid dimensions", warnings=warnings)
        footprint.add_via(
            position_mils=(
                (_number(via.center_x) - origin_mm[0]) * MM_TO_MIL,
                -(_number(via.center_y) - origin_mm[1]) * MM_TO_MIL,
            ),
            diameter_mils=diameter,
            hole_size_mils=hole_size,
        )
    for track in getattr(footprint_data, "tracks", []) or []:
        points = _raw_pairs(str(getattr(track, "points", "")))
        if len(points) < 2:
            warnings.append("skipped TRACK with fewer than two points")
            continue
        layer = _footprint_layer(getattr(track, "layer_id", 3), warnings)
        for start, end in zip(points, points[1:]):
            footprint.add_track((_raw_to_local_mils(start[0], bbox_x), -_raw_to_local_mils(start[1], bbox_y)), (_raw_to_local_mils(end[0], bbox_x), -_raw_to_local_mils(end[1], bbox_y)), width_mils=_number(getattr(track, "stroke_width", 0.2)) * MM_TO_MIL, layer=layer)
    for circle in getattr(footprint_data, "circles", []) or []:
        layer = _footprint_layer(getattr(circle, "layer_id", 101), warnings)
        cx = (_number(circle.cx) - origin_mm[0]) * MM_TO_MIL
        cy = -(_number(circle.cy) - origin_mm[1]) * MM_TO_MIL
        footprint.add_arc(center_mils=(cx, cy), radius_mils=_number(circle.radius) * MM_TO_MIL, start_angle_degrees=0, end_angle_degrees=360, width_mils=_number(circle.stroke_width) * MM_TO_MIL, layer=layer)
    for arc in getattr(footprint_data, "arcs", []) or []:
        geometry = _svg_arc(str(getattr(arc, "path", "")))
        if geometry is None:
            raise ConversionError("non-circular or malformed footprint ARC", warnings=warnings)
        (cx, cy), radius, start, end = geometry
        layer = _footprint_layer(getattr(arc, "layer_id", 3), warnings)
        start, end = (-end, -start) if end > start else (-start, -end)
        footprint.add_arc(center_mils=(_raw_to_local_mils(cx, bbox_x), -_raw_to_local_mils(cy, bbox_y)), radius_mils=radius * FOOTPRINT_RAW_TO_MM * MM_TO_MIL, start_angle_degrees=start, end_angle_degrees=end, width_mils=_number(getattr(arc, "stroke_width", 0.2)) * MM_TO_MIL, layer=layer)
    for rectangle in getattr(footprint_data, "rectangles", []) or []:
        layer = _footprint_layer(getattr(rectangle, "layer_id", 3), warnings)
        x1 = (_number(rectangle.x) - origin_mm[0]) * MM_TO_MIL
        y1 = -(_number(rectangle.y) - origin_mm[1]) * MM_TO_MIL
        x2 = x1 + _number(rectangle.width) * MM_TO_MIL
        y2 = y1 - _number(rectangle.height) * MM_TO_MIL
        width = max(0.01, _number(getattr(rectangle, "stroke_width", 0.2)) * MM_TO_MIL)
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
        for start, end in zip(corners, corners[1:]):
            footprint.add_track(start, end, width_mils=width, layer=layer)
    hidden_text_count = 0
    for label in getattr(footprint_data, "texts", []) or []:
        if not bool(getattr(label, "is_displayed", True)):
            hidden_text_count += 1
            continue
        layer = _footprint_layer(getattr(label, "layer_id", 3), warnings)
        if str(getattr(label, "type", "") or "").upper() == "N":
            layer = int(altium.PcbLayer.MECHANICAL_1)
        footprint.add_text(
            text=str(getattr(label, "text", "") or ""),
            position_mils=(
                (_number(label.center_x) - origin_mm[0]) * MM_TO_MIL,
                -(_number(label.center_y) - origin_mm[1]) * MM_TO_MIL,
            ),
            height_mils=max(1.0, _number(getattr(label, "font_size", 1.0)) * MM_TO_MIL),
            layer=layer,
            rotation_degrees=-_number(getattr(label, "rotation", 0.0)),
            stroke_width_mils=max(0.1, _number(getattr(label, "stroke_width", 0.1)) * MM_TO_MIL),
            is_mirrored=layer in {int(altium.PcbLayer.BOTTOM_OVERLAY), int(altium.PcbLayer.BOTTOM)},
        )
    if hidden_text_count:
        warnings.append(f"skipped hidden footprint TEXT x{hidden_text_count}")
    skipped_regions: dict[tuple[int, str], int] = {}
    for region in getattr(footprint_data, "solid_regions", []) or []:
        layer_id = int(_number(getattr(region, "layer_id", 0)))
        # EasyEDA 100/5 entries are paste/decoration duplicates; layer 99 is
        # the useful courtyard region.  Keep it as a real Altium region and
        # make every omission explicit in the manifest warnings.
        if layer_id != 99:
            key = (layer_id, str(getattr(region, "region_type", "unknown")))
            skipped_regions[key] = skipped_regions.get(key, 0) + 1
            continue
        points = _path_points(str(getattr(region, "path", "")))
        if len(points) < 3:
            raise ConversionError("malformed courtyard SOLIDREGION", warnings=warnings)
        local = [(_raw_to_local_mils(x, bbox_x), -_raw_to_local_mils(y, bbox_y)) for x, y in points]
        footprint.add_region(outline_points_mils=local, layer=int(altium.PcbLayer.MECHANICAL_15), kind=99)
    for (layer_id, region_type), count in sorted(skipped_regions.items()):
        warnings.append(f"skipped SOLIDREGION layer {layer_id} ({region_type}) x{count}")
    three_d: dict[str, Any] = {"requested": three_d_requested, "status": "not_requested", "uuid": ""}
    model_3d = getattr(footprint_data, "model_3d", None)
    if model_3d is not None:
        three_d["uuid"] = str(getattr(model_3d, "uuid", "") or "")
    if step_data is not None:
        try:
            model_name = f"{component['code']}.step"
            rotation = getattr(model_3d, "rotation", None)
            translation = getattr(model_3d, "translation", None)
            rotation_degrees = {
                f"rotation_{axis}_degrees": _number(getattr(rotation, axis, 0.0))
                for axis in ("x", "y", "z")
            }
            z_offset_mils = _number(getattr(translation, "z", 0.0)) * MM_TO_MIL
            # The native merge copies the body's pose into the model record.
            # Author both consistently so a merge preserves metadata exactly.
            model = pcb_lib.add_embedded_model(
                name=model_name, model_data=step_data,
                model_id=uuid.uuid5(uuid.NAMESPACE_URL, component["code"]),
                z_offset_mils=z_offset_mils, **rotation_degrees,
            )
            footprint.add_embedded_3d_model(
                model,
                location_mils=(
                    _number(getattr(translation, "x", 0.0)) * MM_TO_MIL,
                    _number(getattr(translation, "y", 0.0)) * MM_TO_MIL,
                ),
                standoff_height_mils=z_offset_mils, name=model_name,
                **rotation_degrees,
            )
            three_d["status"] = "embedded"
        except Exception as exc:  # library authoring API can reject malformed STEP payloads
            three_d["status"] = "failed"
            three_d["error"] = str(exc)
            warnings.append(f"3D STEP not embedded: {exc}")
    elif three_d_requested and model_3d is not None and str(getattr(model_3d, "uuid", "") or ""):
        three_d["status"] = "missing_or_not_downloaded"
    elif three_d_requested:
        three_d["status"] = "missing"
    if three_d_requested and three_d["status"] in {"missing", "missing_or_not_downloaded"}:
        warnings.append("3D STEP unavailable; symbol and footprint are retained without an embedded model.")
    return footprint, {
        "PAD": len(footprint.pads),
        "CUSTOM_PAD": custom_pad_count,
        "TRACK": len(footprint.tracks),
        "ARC": len(getattr(footprint_data, "arcs", []) or []),
        "CIRCLE": len(getattr(footprint_data, "circles", []) or []),
        "SOLIDREGION": len(footprint.regions),
        "HOLE": len(getattr(footprint_data, "holes", []) or []),
        "VIA": len(getattr(footprint_data, "vias", []) or []),
        "RECT": len(getattr(footprint_data, "rectangles", []) or []),
        "TEXT": len(getattr(footprint_data, "texts", []) or []) - hidden_text_count,
    }, three_d


def _validate_authored_pair(
    symbol: Any, footprint: Any, warnings: list[str]
) -> None:
    pins = list(getattr(symbol, "pins", []) or [])
    pads = list(getattr(footprint, "pads", []) or [])
    if not pins:
        raise ConversionError("authored symbol has no pins", warnings=warnings)
    if not pads:
        raise ConversionError("authored footprint has no pads", warnings=warnings)
    pin_numbers = [str(getattr(pin, "designator", "") or "").strip() for pin in pins]
    if any(not value for value in pin_numbers):
        raise ConversionError("symbol contains an unnumbered pin", warnings=warnings)
    if len(pin_numbers) != len(set(pin_numbers)):
        raise ConversionError("symbol contains duplicate pin designators", warnings=warnings)
    pad_numbers = [str(getattr(pad, "designator", "") or "").strip() for pad in pads]
    numbered_pads = [value for value in pad_numbers if value]
    unnumbered = len(pad_numbers) - len(numbered_pads)
    if unnumbered:
        warnings.append(f"footprint contains {unnumbered} unnumbered mechanical pad(s)")
    duplicate_pads = len(numbered_pads) - len(set(numbered_pads))
    if duplicate_pads:
        warnings.append(f"footprint contains {duplicate_pads} repeated pad designator(s)")
    missing_pads = sorted(set(pin_numbers) - set(numbered_pads))
    extra_pads = sorted(set(numbered_pads) - set(pin_numbers))
    if missing_pads or extra_pads:
        summary = []
        if missing_pads:
            summary.append("pins without pads=" + ",".join(missing_pads[:12]))
        if extra_pads:
            summary.append("pads without pins=" + ",".join(extra_pads[:12]))
        warnings.append("pin/pad designator mismatch: " + "; ".join(summary))


def _component_meta(code: str, detail: dict[str, Any], symbol_data: Any, footprint_data: Any) -> dict[str, Any]:
    info = getattr(symbol_data, "info", None)
    fp_info = getattr(footprint_data, "info", None)
    mpn = str(detail.get("productModel") or getattr(info, "mpn", "") or getattr(info, "name", "") or code)
    package = str(detail.get("encapStandard") or getattr(fp_info, "name", "") or "")
    manufacturer = str(detail.get("brandNameEn") or getattr(info, "manufacturer", "") or "")
    title = str(detail.get("title") or getattr(info, "name", "") or code)
    product_url = f"https://item.szlcsc.com/{detail.get('productId')}.html" if detail.get("productId") not in (None, "") else f"https://so.szlcsc.com/global.html?k={code}"
    global_product_url = f"https://www.lcsc.com/product-detail/{code}.html"
    prices = detail.get("productPriceList") or []
    first_price = prices[0] if prices and isinstance(prices[0], dict) else {}
    currency = {"$": "USD", "￥": "CNY", "¥": "CNY"}.get(str(first_price.get("currencySymbol") or detail.get("currencySymbol") or ""), str(first_price.get("currencySymbol") or detail.get("currencySymbol") or ""))
    return {
        "code": code,
        "mpn": mpn,
        "name": title,
        "description": str(detail.get("productIntroEn") or detail.get("productDescEn") or getattr(info, "description", "") or ""),
        "manufacturer": manufacturer,
        "package": package,
        "stock": detail.get("stockNumber", detail.get("stockSz", "")),
        "price": first_price.get("currencyPrice", first_price.get("productPrice", "")),
        "currency": currency,
        "price_source": "global",
        "source_urls": {
            "lcsc": product_url,
            "lcsc_global": global_product_url,
            "lcsc_global_api": LCSC_DETAIL_URL + "?productCode=" + code,
            "datasheet": str(detail.get("pdfUrl") or ""),
            "easyeda": EASYEDA_COMPONENT_URL.format(code=code),
        },
    }


def _fetch_component(client: LCSCClient, code: str, *, with_3d: bool = False) -> tuple[dict[str, Any], Any, Any, str, str, list[str], bytes | None]:
    code = code.strip().upper()
    detail = client.get_detail(code)
    data, component_transport = client.get_component_data_with_metadata(code)
    symbol_data = _import_symbol(data)
    footprint_data = _import_footprint(data)
    component = _component_meta(code, detail, symbol_data, footprint_data)
    package_detail = data.get("packageDetail") or {}
    component["source_metadata"] = {
        "fetched_at": component_transport["fetched_at"],
        "easyeda_transport": component_transport["transport"],
        "easyeda_cache_age_seconds": component_transport["cache_age_seconds"],
        "lcsc_detail_sha256": _json_hash(detail),
        "easyeda_component_sha256": _json_hash(data),
        "easyeda_component_uuid": str(data.get("uuid") or ""),
        "easyeda_package_uuid": str(
            (package_detail.get("uuid") or "") if isinstance(package_detail, dict) else ""
        ),
        "easyeda_verified": data.get("verify"),
    }
    symbol_name = f"{code}_{_clean(component['mpn'] or component['name'])}"
    footprint_name = f"{code}_{_clean(component['package'] or getattr(footprint_data.info, 'name', ''), 'PACKAGE')}"
    warnings: list[str] = []
    if data.get("verify") is not True:
        warnings.append("EasyEDA component source is not marked verified")
    # Fetching is kept in LCSCClient; a missing STEP model never blocks the
    # symbol/footprint result.
    step_data: bytes | None = None
    model_3d = getattr(footprint_data, "model_3d", None)
    model_uuid = str(getattr(model_3d, "uuid", "") or "") if model_3d is not None else ""
    if with_3d:
        if model_uuid:
            try:
                step_data = client.get_step_model(model_uuid)
            except ClientError as exc:
                warnings.append(f"3D STEP unavailable: {exc}")
        else:
            warnings.append("EasyEDA component has no 3D model UUID")
    return component, symbol_data, footprint_data, symbol_name, footprint_name, warnings, step_data


def convert_component(client: LCSCClient, code: str, *, with_3d: bool = False) -> ComponentResult:
    code = code.strip().upper()
    component, symbol_data, footprint_data, symbol_name, footprint_name, warnings, step_data = _fetch_component(client, code, with_3d=with_3d)
    # Authoring is done in-memory.  If any supported primitive unexpectedly
    # fails, the caller records the component as failed instead of publishing a
    # deceptively incomplete library item.
    sch = altium.AltiumSchLib()
    pcb = altium.AltiumPcbLib()
    _add_symbol(sch, symbol_data, symbol_name, footprint_name, component, warnings)
    _, written_footprint_counts, three_d = _add_footprint(pcb, footprint_data, footprint_name, component, warnings, step_data=step_data, three_d_requested=with_3d)
    # The actual footprint/symbol counts come from the authored objects.
    authored_symbol = sch.get_symbol(symbol_name)
    return ComponentResult(
        code=code,
        component=component,
        symbol_name=symbol_name,
        footprint_name=footprint_name,
        symbol_counts={"P": len(authored_symbol.pins), "R": len(authored_symbol.rectangles), "E": len(authored_symbol.ellipses)},
        footprint_counts=written_footprint_counts,
        warnings=warnings,
        raw_symbol_counts=_symbol_counts(symbol_data),
        raw_footprint_counts=_footprint_counts(footprint_data),
        three_d=three_d,
    )


def _manifest_component(result: ComponentResult) -> dict[str, Any]:
    value = dict(result.component)
    value.update({
        "origin": "generated",
        "status": "complete",
        "symbol_name": result.symbol_name,
        "footprint_name": result.footprint_name,
        "symbol": {"status": "complete", "raw_counts": result.raw_symbol_counts, "written_counts": result.symbol_counts},
        "footprint": {"status": "complete", "raw_counts": result.raw_footprint_counts, "written_counts": result.footprint_counts},
        "3d": result.three_d,
        "warnings": result.warnings,
        "manual_review_required": True,
        "manual_review_reasons": [
            "datasheet_pin_mapping_not_automatically_verified",
            "footprint_dimensions_layers_and_pin1_require_engineer_review",
            "3d_height_offset_and_rotation_require_review",
        ],
    })
    return value


def _notify_progress(
    callback: Callable[[int, int, str, str], None] | None,
    completed: int,
    total: int,
    code: str,
    status: str,
) -> None:
    if callback is not None:
        try:
            callback(completed, total, code, status)
        except Exception:
            # Progress reporting must never damage a library build.
            pass


def _native_inventory_recovery_hint(
    error: Exception,
    manifest: dict[str, Any],
    baseline: dict[str, Any],
) -> str:
    """Add a recovery hint only for a proven post-publish native-link change."""
    if "local symbol-footprint link is unresolved" not in str(error):
        return ""
    if manifest.get("schema_version") != 3 or manifest.get("published") is not True:
        return ""
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        return ""

    def metadata(value: Any) -> tuple[int, str] | None:
        if not isinstance(value, dict):
            return None
        size = value.get("size")
        sha256 = value.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return None
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
            return None
        return size, sha256.casefold()

    changed: list[str] = []
    for name in LIBRARY_NAMES:
        published = metadata(outputs.get(name))
        current = metadata(baseline.get(name))
        if published is None or current is None:
            return ""
        if published != current:
            changed.append(name)
    if not changed:
        return ""
    hint = (
        "；上次发布后的库校验值已变化："
        + "、".join(changed)
        + "；当前 SchLib/PcbLib 可能已不是同一次发布的配对文件。"
    )
    backup_directory = manifest.get("backup_directory")
    if manifest.get("retained_previous_libraries") is True and isinstance(backup_directory, str):
        backup = Path(backup_directory)
        if backup.is_absolute() and all((backup / name).is_file() for name in LIBRARY_NAMES):
            hint += f"可人工核对并成对恢复的发布前备份：{backup}。"
    return hint


def prepare_libraries(
    client: LCSCClient,
    codes: Iterable[str],
    output_dir: Path,
    *,
    with_3d: bool = False,
    progress: Callable[[int, int, str, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], int]:
    """Append new C codes to a persistent pair, retaining every native item."""
    unique_codes: list[str] = []
    for raw in codes:
        code = str(raw or "").strip().upper()
        if not code or code in unique_codes:
            continue
        if not re.fullmatch(r"C[0-9]+", code):
            raise ValueError(f"invalid LCSC code: {code}")
        unique_codes.append(code)
    if not unique_codes:
        raise ValueError("no LCSC codes supplied")

    preflight_ad_write()

    def check_cancelled() -> None:
        if cancelled is not None and cancelled():
            raise BatchCancelled("library generation cancelled before publication")

    with LibraryStore(output_dir) as store:
        assert store.stage is not None
        stage = store.stage
        successes: list[ComponentResult] = []
        previous_components: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        manifest: dict[str, Any] = {
            "schema_version": 3,
            "software": {
                "name": "PartsBridge AD", "version": __version__, "publisher": __publisher__,
            },
            "run_id": store.run_id,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": "append",
            "output_directory": str(store.output),
            "state_directory": str(store.state),
            "backup_directory": None,
            "status": "preparing",
            "published": False,
            "retained_previous_libraries": False,
            "requested_codes": unique_codes,
            "normalized_input_sha256": hashlib.sha256(
                ("\n".join(unique_codes) + "\n").encode("utf-8")
            ).hexdigest(),
            "data_source": "existing_native_libraries_and_public_lcsc_easyeda",
            "manual_review_required": True,
            "authoring_format_tested_with": ["Altium Designer 26.9.1"],
            "components": [],
            "added_codes": [],
            "added_count": 0,
            "skipped": skipped,
            "skipped_count": 0,
            "total_components": 0,
            "failures": failures,
            "outputs": {},
        }

        def record_run() -> None:
            try:
                write_json(store.state / "last-run.json", manifest)
            except OSError as exc:
                # A report failure must not turn an already committed pair into
                # a reported failed publication or hide the original exception.
                manifest["report_warning"] = str(exc)

        try:
            check_cancelled()
            previous_dir = store.snapshot()
            previous_manifest = store.read_manifest()
            try:
                previous_inventory = native_inventory(previous_dir)
            except Exception as exc:
                hint = _native_inventory_recovery_hint(exc, previous_manifest, store.baseline)
                raise ConversionError(
                    f"无法安全读取现有总库；已停止追加，未覆盖文件：{exc}{hint}"
                ) from exc
            previous_components = existing_components(previous_inventory, previous_manifest)
            manifest["components"] = previous_components
            manifest["native_inventory"] = previous_inventory
            manifest["total_components"] = len(previous_components)
            manifest["retained_previous_libraries"] = all(store.baseline.values())
            expected_inventory = {key: dict(value) for key, value in previous_inventory.items()}
            existing_codes = {
                item["code"] for category in ("symbols", "footprints")
                for item in previous_inventory[category].values() if item["code"]
            }
            symbol_names = {name.casefold() for name in previous_inventory["symbols"]}
            footprint_names = {name.casefold() for name in previous_inventory["footprints"]}
            symbol_paths = [previous_dir / "LCSC.SchLib"] if all(store.baseline.values()) else []
            footprint_paths = [previous_dir / "LCSC.PcbLib"] if all(store.baseline.values()) else []

            for index, code in enumerate(unique_codes):
                check_cancelled()
                if code in existing_codes:
                    skipped.append({"code": code, "reason": "already_in_native_library"})
                    manifest["skipped_count"] = len(skipped)
                    _notify_progress(progress, index + 1, len(unique_codes), code, "skipped")
                    continue
                _notify_progress(progress, index, len(unique_codes), code, "starting")
                try:
                    component, symbol_data, footprint_data, symbol_name, footprint_name, warnings, step_data = _fetch_component(client, code, with_3d=with_3d)
                    check_cancelled()
                    if str(component.get("code", "")).upper() != code:
                        raise ConversionError(f"source returned a different component code: {code}")
                    if symbol_name.casefold() in symbol_names:
                        raise ConversionError(f"symbol name conflict; existing item retained: {symbol_name}")
                    if footprint_name.casefold() in footprint_names:
                        raise ConversionError(f"footprint name conflict; existing item retained: {footprint_name}")
                    item_sch = altium.AltiumSchLib()
                    item_pcb = altium.AltiumPcbLib()
                    authored_symbol, written_symbol_counts = _add_symbol(
                        item_sch, symbol_data, symbol_name, footprint_name, component, warnings,
                    )
                    authored_footprint, written_footprint_counts, three_d = _add_footprint(
                        item_pcb, footprint_data, footprint_name, component, warnings,
                        step_data=step_data, three_d_requested=with_3d,
                    )
                    _validate_authored_pair(authored_symbol, authored_footprint, warnings)
                    item_dir = stage / f"item-{index:06d}"
                    item_dir.mkdir()
                    symbol_path, footprint_path = item_dir / "LCSC.SchLib", item_dir / "LCSC.PcbLib"
                    item_sch.save(symbol_path)
                    item_pcb.save(footprint_path)
                    item_inventory = native_inventory(item_dir)
                    for category in ("models", "fonts", "images"):
                        for name, value in item_inventory[category].items():
                            if name in expected_inventory[category] and expected_inventory[category][name] != value:
                                raise ConversionError(f"{category} identity conflict; existing item retained: {name}")
                    for category, values in item_inventory.items():
                        expected_inventory[category].update(values)
                    successes.append(ComponentResult(
                        code=code, component=component,
                        symbol_name=symbol_name, footprint_name=footprint_name,
                        symbol_counts=written_symbol_counts, footprint_counts=written_footprint_counts,
                        warnings=warnings,
                        raw_symbol_counts=_symbol_counts(symbol_data),
                        raw_footprint_counts=_footprint_counts(footprint_data),
                        three_d=three_d,
                    ))
                    symbol_names.add(symbol_name.casefold())
                    footprint_names.add(footprint_name.casefold())
                    symbol_paths.append(symbol_path)
                    footprint_paths.append(footprint_path)
                    _notify_progress(progress, index + 1, len(unique_codes), code, "complete")
                except BatchCancelled:
                    raise
                except Exception as exc:
                    failures.append({"code": code, "error": str(exc), "warnings": list(getattr(exc, "warnings", []))})
                    _notify_progress(progress, index + 1, len(unique_codes), code, "failed")

            check_cancelled()
            store.assert_unchanged()
            if not successes:
                manifest["status"] = "failed" if failures else "unchanged"
                manifest["published"] = not failures
                manifest["outputs"] = {name: value for name, value in store.baseline.items() if value is not None}
                if not failures:
                    verification = verify_output(store.output, manifest=manifest)
                    if not verification["ok"]:
                        raise ConversionError("existing library verification failed: " + "; ".join(verification["errors"]))
                    manifest["static_verification"] = {"status": "passed", "checks": verification["checks"]}
                    store.assert_unchanged()
                    check_cancelled()
                    # Only the external index changes; both native files remain byte-identical.
                    write_json(store.state / "manifest.json", manifest)
                record_run()
                return manifest, 1 if failures else 0

            _notify_progress(progress, len(unique_codes), len(unique_codes), "", "merging")
            altium.AltiumSchLib.merge(
                symbol_paths, stage / "LCSC.SchLib", handle_conflicts="error", verbose=False,
            )
            combined_pcb = altium.AltiumPcbLib.combine(footprint_paths, verbose=False)
            combined_pcb.save(stage / "LCSC.PcbLib")
            current_inventory = native_inventory(stage)
            preservation_errors = preserved_inventory(expected_inventory, current_inventory)
            if preservation_errors:
                raise ConversionError("native item preservation failed: " + "; ".join(preservation_errors))
            manifest.update({
                "status": "partial" if failures else "complete",
                "published": True,
                "components": previous_components + [_manifest_component(item) for item in successes],
                "native_inventory": current_inventory,
                "added_codes": [item.code for item in successes],
                "added_count": len(successes),
                "total_components": len(current_inventory["symbols"]),
                "outputs": {name: output_metadata(stage / name) for name in LIBRARY_NAMES},
            })
            verification = verify_output(stage, manifest=manifest)
            if not verification["ok"]:
                raise ConversionError("staged output validation failed: " + "; ".join(verification["errors"]))
            manifest["static_verification"] = {
                "status": "passed",
                "checks": verification["checks"] + ["previous_native_items_preserved"],
                "preserved_symbols": len(previous_inventory["symbols"]),
                "preserved_footprints": len(previous_inventory["footprints"]),
                "preserved_embedded_models": len(previous_inventory["models"]),
            }
            check_cancelled()
            store.publish(manifest)
        except Exception as exc:
            manifest.update({
                "status": "cancelled" if isinstance(exc, BatchCancelled) else "failed",
                "published": False,
                "error": str(exc),
                "prepared_codes_not_published": [item.code for item in successes],
                "components": previous_components,
                "added_codes": [],
                "added_count": 0,
                "total_components": len(previous_components),
            })
            if store.baseline:
                manifest["retained_previous_libraries"] = bool(
                    any(store.baseline.values()) and store.current_metadata() == store.baseline
                )
            record_run()
            raise
        record_run()
        _notify_progress(progress, len(unique_codes), len(unique_codes), "", "published")
        return manifest, 2 if failures else 0

__all__ = [
    "BatchCancelled",
    "ConversionError",
    "convert_component",
    "prepare_libraries",
]
