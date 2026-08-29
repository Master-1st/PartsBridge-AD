"""Conservative recovery for board pads moved away from their components."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import altium_monkey as altium


class PadRepairError(RuntimeError):
    """The current board does not match the narrowly supported repair pattern."""


@dataclass(frozen=True)
class PadCorrection:
    component: str
    footprint: str
    designator: str
    occurrence: int
    current_x: int
    current_y: int
    expected_x: int
    expected_y: int


def _rotate(x: float, y: float, degrees: float) -> tuple[float, float]:
    angle = float(degrees) % 360.0
    if math.isclose(angle, 0.0, abs_tol=1e-9):
        return x, y
    if math.isclose(angle, 90.0, abs_tol=1e-9):
        return -y, x
    if math.isclose(angle, 180.0, abs_tol=1e-9):
        return -x, -y
    if math.isclose(angle, 270.0, abs_tol=1e-9):
        return y, -x
    radians = math.radians(angle)
    return (
        x * math.cos(radians) - y * math.sin(radians),
        x * math.sin(radians) + y * math.cos(radians),
    )


def _component_transform(component: Any) -> tuple[float, float, float, bool]:
    return (
        float(component.get_x_mils()),
        float(component.get_y_mils()),
        float(component.get_rotation_degrees()),
        str(getattr(component, "layer", "TOP") or "TOP").strip().upper() == "BOTTOM",
    )


def _to_local(x: float, y: float, component: Any) -> tuple[float, float]:
    origin_x, origin_y, rotation, flipped = _component_transform(component)
    local_x, local_y = _rotate(x - origin_x, y - origin_y, -rotation)
    return (local_x, -local_y if flipped else local_y)


def _to_board(x: float, y: float, component: Any) -> tuple[float, float]:
    origin_x, origin_y, rotation, flipped = _component_transform(component)
    rotated_x, rotated_y = _rotate(x, -y if flipped else y, rotation)
    return origin_x + rotated_x, origin_y + rotated_y


def _pads_by_component(document: Any) -> dict[int, list[tuple[int, Any]]]:
    grouped: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for index, pad in enumerate(document.pads):
        component_index = getattr(pad, "component_index", None)
        if component_index is not None:
            grouped[int(component_index)].append((index, pad))
    return grouped


def _keyed_pads(pads: list[tuple[int, Any]]) -> dict[tuple[str, int], tuple[int, Any]]:
    occurrences: dict[str, int] = defaultdict(int)
    result: dict[tuple[str, int], tuple[int, Any]] = {}
    for index, pad in pads:
        designator = str(getattr(pad, "designator", "") or "")
        occurrence = occurrences[designator]
        occurrences[designator] += 1
        result[(designator, occurrence)] = (index, pad)
    return result


def _same_pad_shape(first: Any, second: Any) -> bool:
    fields = ("width", "height", "hole_size", "shape", "is_plated")
    return all(getattr(first, field, None) == getattr(second, field, None) for field in fields)


def plan_shifted_pad_repair(
    current: Any,
    reference: Any,
    *,
    shift_x_mils: float,
    shift_y_mils: float,
    tolerance_mils: float = 0.01,
) -> tuple[list[PadCorrection], dict[str, Any]]:
    """Plan an all-or-nothing repair from known-good component-local pad geometry."""
    if tolerance_mils <= 0:
        raise ValueError("tolerance_mils must be positive")

    current_groups = _pads_by_component(current)
    reference_groups = _pads_by_component(reference)
    reference_components = {
        str(component.unique_id): (index, component)
        for index, component in enumerate(reference.components)
    }
    corrections: list[PadCorrection] = []
    unexpected: list[str] = []
    compared = 0
    unchanged = 0
    unmatched_current: list[str] = []
    matched_reference_ids: set[str] = set()

    for current_index, current_component in enumerate(current.components):
        unique_id = str(current_component.unique_id)
        reference_entry = reference_components.get(unique_id)
        if reference_entry is None:
            unmatched_current.append(str(current_component.designator))
            continue
        matched_reference_ids.add(unique_id)
        reference_index, reference_component = reference_entry
        current_pads = _keyed_pads(current_groups.get(current_index, []))
        reference_pads = _keyed_pads(reference_groups.get(reference_index, []))
        if set(current_pads) != set(reference_pads):
            raise PadRepairError(
                f"pad inventory changed for {current_component.designator}: "
                f"current={sorted(current_pads)}, reference={sorted(reference_pads)}"
            )

        for key, (current_pad_index, current_pad) in current_pads.items():
            _, reference_pad = reference_pads[key]
            if not _same_pad_shape(current_pad, reference_pad):
                raise PadRepairError(
                    f"pad geometry changed for {current_component.designator}-{key[0]}"
                )
            local_x, local_y = _to_local(
                reference_pad.x / 10000.0,
                reference_pad.y / 10000.0,
                reference_component,
            )
            expected_x_mils, expected_y_mils = _to_board(
                local_x, local_y, current_component
            )
            expected_x = int(round(expected_x_mils * 10000.0))
            expected_y = int(round(expected_y_mils * 10000.0))
            delta_x = (int(current_pad.x) - expected_x) / 10000.0
            delta_y = (int(current_pad.y) - expected_y) / 10000.0
            compared += 1
            if math.isclose(delta_x, 0.0, abs_tol=tolerance_mils) and math.isclose(
                delta_y, 0.0, abs_tol=tolerance_mils
            ):
                unchanged += 1
                continue
            if math.isclose(
                delta_x, shift_x_mils, abs_tol=tolerance_mils
            ) and math.isclose(delta_y, shift_y_mils, abs_tol=tolerance_mils):
                corrections.append(
                    PadCorrection(
                        component=str(current_component.designator),
                        footprint=str(current_component.footprint),
                        designator=key[0],
                        occurrence=key[1],
                        current_x=int(current_pad.x),
                        current_y=int(current_pad.y),
                        expected_x=expected_x,
                        expected_y=expected_y,
                    )
                )
                continue
            unexpected.append(
                f"{current_component.designator}-{key[0]}[{key[1]}]: "
                f"delta=({delta_x:.4f},{delta_y:.4f})mil"
            )

    if unexpected:
        preview = "; ".join(unexpected[:12])
        raise PadRepairError(
            f"found {len(unexpected)} pad offsets outside the allowed pattern: {preview}"
        )

    unmatched_reference = [
        str(component.designator)
        for component in reference.components
        if str(component.unique_id) not in matched_reference_ids
    ]
    report = {
        "compared_pads": compared,
        "correction_count": len(corrections),
        "unchanged_count": unchanged,
        "shift_mils": [float(shift_x_mils), float(shift_y_mils)],
        "tolerance_mils": float(tolerance_mils),
        "unmatched_current_components": unmatched_current,
        "unmatched_reference_components": unmatched_reference,
    }
    return corrections, report


def apply_pad_corrections(document: Any, corrections: list[PadCorrection]) -> None:
    """Apply a validated plan without touching any other board primitive."""
    current_groups = _pads_by_component(document)
    by_component = {
        str(component.designator): _keyed_pads(current_groups.get(index, []))
        for index, component in enumerate(document.components)
    }
    for correction in corrections:
        try:
            _, pad = by_component[correction.component][
                (correction.designator, correction.occurrence)
            ]
        except KeyError as exc:
            raise PadRepairError(
                f"repair target disappeared: {correction.component}-{correction.designator}"
            ) from exc
        if int(pad.x) != correction.current_x or int(pad.y) != correction.current_y:
            raise PadRepairError(
                f"repair target changed before apply: "
                f"{correction.component}-{correction.designator}"
            )
        pad.x = correction.expected_x
        pad.y = correction.expected_y


def pad_snapshot(document: Any) -> list[tuple[Any, ...]]:
    """Return the semantic pad fields that must survive a save and reload."""
    result: list[tuple[Any, ...]] = []
    for pad in document.pads:
        result.append(
            (
                str(getattr(pad, "designator", "") or ""),
                getattr(pad, "component_index", None),
                getattr(pad, "net_index", None),
                int(pad.x),
                int(pad.y),
                int(pad.width),
                int(pad.height),
                int(getattr(pad, "hole_size", 0) or 0),
                int(getattr(pad, "shape", 0) or 0),
                round(float(getattr(pad, "rotation", 0.0) or 0.0), 9),
                bool(getattr(pad, "is_plated", False)),
            )
        )
    return result


def _model_record_count(data: bytes, stream_name: str) -> int:
    offset = 0
    count = 0
    while offset < len(data):
        if offset + 4 > len(data):
            raise PadRepairError(f"truncated model record length in {stream_name}")
        size = int.from_bytes(data[offset : offset + 4], "little")
        if size <= 0 or offset + 4 + size > len(data):
            raise PadRepairError(f"malformed model record in {stream_name} at {offset}")
        offset += 4 + size
        count += 1
    return count


def model_table_counts(path: Path | str) -> dict[str, tuple[int, int]]:
    """Validate native model table headers against their record counts."""
    result: dict[str, tuple[int, int]] = {}
    with altium.AltiumOleFile(path) as ole:
        for table in ("Models", "ModelsNoEmbed"):
            header_path = f"{table}/Header"
            data_path = f"{table}/Data"
            header_data = ole.openstream(header_path) if ole.exists(header_path) else b""
            model_data = ole.openstream(data_path) if ole.exists(data_path) else b""
            if header_data and len(header_data) < 4:
                raise PadRepairError(f"truncated model table header: {header_path}")
            declared = int.from_bytes(header_data[:4], "little") if header_data else 0
            actual = _model_record_count(model_data, data_path) if model_data else 0
            if declared != actual:
                raise PadRepairError(
                    f"model table count mismatch: {header_path} declares {declared}, "
                    f"but {data_path} contains {actual} records"
                )
            result[table] = (declared, actual)
    return result


def native_stream_inventory(path: Path | str) -> dict[str, tuple[int, str]]:
    """Return native OLE stream lengths and hashes without reserializing them."""
    result: dict[str, tuple[int, str]] = {}
    with altium.AltiumOleFile(path) as ole:
        for parts in ole.listdir(streams=True, storages=False):
            name = "/".join(parts)
            data = ole.openstream(parts)
            result[name] = (len(data), sha256(data).hexdigest())
    return result


def _validate_model_links(document: Any) -> None:
    model_ids = {
        str(model.id)
        for model in getattr(document, "models", [])
        if getattr(model, "id", None)
    }
    unresolved = [
        (index, str(body.model_id), int(getattr(body, "component_index", -1)))
        for index, body in enumerate(getattr(document, "component_bodies", []))
        if getattr(body, "model_id", None) and str(body.model_id) not in model_ids
    ]
    if unresolved:
        preview = "; ".join(
            f"body {index} / component {component}: {model_id}"
            for index, model_id, component in unresolved[:8]
        )
        raise PadRepairError(f"unresolved 3D model references: {preview}")


def write_pad_stream_only(
    source_path: Path | str,
    document: Any,
    output_path: Path | str,
) -> dict[str, Any]:
    """Write only ``Pads6/Data`` while preserving every other native stream."""
    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if source == output:
        raise PadRepairError("output must not overwrite the source PcbDoc")
    if output.exists():
        raise PadRepairError(f"output already exists: {output}")

    tables = model_table_counts(source)
    _validate_model_links(document)
    source_inventory = native_stream_inventory(source)
    if "Pads6/Data" not in source_inventory:
        raise PadRepairError("source PcbDoc has no Pads6/Data stream")
    repaired_pad_data = b"".join(pad.serialize_to_binary() for pad in document.pads)
    expected_size = source_inventory["Pads6/Data"][0]
    if len(repaired_pad_data) != expected_size:
        raise PadRepairError(
            f"Pads6/Data size changed: {expected_size} -> {len(repaired_pad_data)}"
        )

    try:
        with altium.AltiumOleFile(source) as ole:
            ole.modify_stream("Pads6/Data", repaired_pad_data)
            ole.write(output)

        output_inventory = native_stream_inventory(output)
        if set(output_inventory) != set(source_inventory):
            raise PadRepairError("native stream inventory changed during pad repair")
        changed_streams = sorted(
            name
            for name in source_inventory
            if source_inventory[name] != output_inventory[name]
        )
        if any(name != "Pads6/Data" for name in changed_streams):
            raise PadRepairError(
                "non-pad native streams changed during pad repair: "
                + ", ".join(changed_streams)
            )

        reloaded = altium.AltiumPcbDoc.from_file(output, verbose=False)
        if pad_snapshot(reloaded) != pad_snapshot(document):
            raise PadRepairError("reloaded pad records do not match the repair plan")
        output_tables = model_table_counts(output)
        if output_tables != tables:
            raise PadRepairError("model table counts changed during pad repair")
        _validate_model_links(reloaded)
    except Exception:
        output.unlink(missing_ok=True)
        raise

    return {
        "native_stream_count": len(output_inventory),
        "changed_native_streams": changed_streams,
        "model_table_counts": {
            name: {"declared": counts[0], "actual": counts[1]}
            for name, counts in output_tables.items()
        },
        "unresolved_model_references": 0,
    }


__all__ = [
    "PadCorrection",
    "PadRepairError",
    "apply_pad_corrections",
    "model_table_counts",
    "native_stream_inventory",
    "pad_snapshot",
    "plan_shifted_pad_repair",
    "write_pad_stream_only",
]
