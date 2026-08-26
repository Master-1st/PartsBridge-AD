"""Static integrity and native-library round-trip verification."""

from __future__ import annotations

import hashlib
import json
import re
import zlib
from pathlib import Path
from typing import Any

import altium_monkey as altium

from .library_store import output_metadata, sha256_file, state_directory


def _json_hash(value: Any) -> str:
    def native_bytes(item: Any) -> dict[str, str]:
        if isinstance(item, (bytes, bytearray)):
            return {"native_bytes_hex": item.hex()}
        raise TypeError(f"unsupported native metadata type: {type(item).__name__}")

    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=native_bytes).encode("utf-8")).hexdigest()


def _binary_hash(values: Any) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(len(value).to_bytes(8, "little"))
        digest.update(value)
    return digest.hexdigest()


def _component_code(parameters: dict[str, str], name: str) -> str:
    values = {str(value).strip().upper() for key, value in parameters.items()
              if key.casefold() in {"lcsc", "lcsc part #", "lcsc part number"} and str(value).strip()}
    if len(values) > 1:
        raise ValueError(f"conflicting LCSC parameters: {name}")
    if values:
        value = next(iter(values))
        if not re.fullmatch(r"C[0-9]+", value):
            raise ValueError(f"invalid LCSC parameter: {name}")
        return value
    match = re.match(r"^(C[0-9]+)_", name, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def native_inventory(directory: Path) -> dict[str, Any]:
    """Read the actual native pair, including untracked items and embedded data."""
    inventory: dict[str, Any] = {key: {} for key in ("symbols", "footprints", "models", "fonts", "images")}
    sch_path, pcb_path = Path(directory) / "LCSC.SchLib", Path(directory) / "LCSC.PcbLib"
    if not sch_path.exists() and not pcb_path.exists():
        return inventory
    sch = altium.AltiumSchLib(sch_path)
    pcb = altium.AltiumPcbLib.from_file(pcb_path)
    for category, items in (("symbols", sch.symbols), ("footprints", pcb.footprints)):
        names = [item.name.casefold() for item in items]
        if len(names) != len(set(names)):
            raise ValueError(f"case-insensitive name collision in existing {category}")
    footprint_names = {item.name.casefold() for item in pcb.footprints}
    for symbol in sch.symbols:
        for link in symbol.implementations:
            if link.model_type.upper() != "PCBLIB":
                continue
            fields = {key.casefold(): str(value) for key, value in link.serialize_to_record().items()}
            reference = fields.get("modeldatafile0", fields.get("modeldatafileentity0", ""))
            if reference.replace("\\", "/").split("/")[-1].casefold() == "lcsc.pcblib":
                if link.model_name.casefold() not in footprint_names:
                    raise ValueError(f"local symbol-footprint link is unresolved: {symbol.name} / {link.model_name}")
    if sch.font_manager is not None:
        inventory["fonts"] = {str(key): _json_hash(value) for key, value in sch.font_manager.fonts.items()}
    inventory["images"] = {name: hashlib.sha256(data).hexdigest() for name, data in sch.embedded_images.items()}
    for symbol in sch.symbols:
        parameters = {parameter.name: parameter.text for parameter in symbol.parameters}
        inventory["symbols"][symbol.name] = {
            "sha256": _json_hash({
                "name": symbol.name,
                "original_name": symbol.original_name,
                "description": symbol.description,
                "part_count": symbol.part_count,
                "component": symbol.component_record,
                "objects": [item.serialize_to_record() for item in symbol.objects],
                "raw_records": symbol.raw_records,
                "extra_streams": {key: hashlib.sha256(data).hexdigest() for key, data in symbol._original_streams.items()},
            }),
            "code": _component_code(parameters, symbol.name),
            "mpn": parameters.get("MPN", parameters.get("Manufacturer_Part_Number", "")),
            "description": symbol.description,
            "pin_count": len(symbol.pins),
            "part_count": symbol.part_count,
            "footprint_names": [item.model_name for item in symbol.implementations if item.model_type.upper() == "PCBLIB"],
        }
    for footprint in pcb.footprints:
        inventory["footprints"][footprint.name] = {
            "sha256": _json_hash({
                "parameters": footprint.parameters,
                "primitives": _binary_hash(item.serialize_to_binary() for item in footprint.primitives),
            }),
            "code": _component_code(footprint.parameters, footprint.name),
            "pad_count": len(footprint.pads),
            "embedded_model_ids": [body.model_id for body in footprint.component_bodies if body.model_is_embedded],
        }
    for model, compressed in pcb.get_embedded_model_entries():
        record = {
            "metadata_sha256": hashlib.sha256(model.serialize_to_binary()).hexdigest(),
            "payload_sha256": hashlib.sha256(zlib.decompress(compressed)).hexdigest(),
        }
        if model.id in inventory["models"] and inventory["models"][model.id] != record:
            raise ValueError(f"conflicting embedded model ID: {model.id}")
        inventory["models"][model.id] = record
    for name, record in inventory["footprints"].items():
        for model_id in record["embedded_model_ids"]:
            if model_id not in inventory["models"]:
                raise ValueError(f"embedded model payload missing: {name} / {model_id}")
    return inventory


def preserved_inventory(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Every previous item must still be present and byte/record equivalent."""
    errors = []
    for category, items in previous.items():
        for name, value in items.items():
            if current.get(category, {}).get(name) != value:
                errors.append(f"{category} changed or missing: {name}")
    return errors


def existing_components(inventory: dict[str, Any], previous: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep trustworthy provenance; otherwise index the native files themselves."""
    prior_inventory = previous.get("native_inventory") or {}
    prior_components = {item.get("symbol_name"): item for item in previous.get("components", []) if isinstance(item, dict)}
    result = []
    for name, value in inventory["symbols"].items():
        footprints = [item for item in value["footprint_names"] if item in inventory["footprints"]]
        unchanged = prior_inventory.get("symbols", {}).get(name) == value and all(
            prior_inventory.get("footprints", {}).get(fp) == inventory["footprints"][fp] for fp in footprints
        )
        if unchanged and name in prior_components:
            result.append(prior_components[name])
        else:
            model_ids = {model_id for fp in footprints for model_id in inventory["footprints"][fp]["embedded_model_ids"]}
            result.append({
                "code": value["code"], "mpn": value["mpn"], "name": name,
                "origin": "existing_native_library", "status": "retained",
                "symbol_name": name, "footprint_name": footprints[0] if len(footprints) == 1 else None,
                "footprint_names": footprints, "pin_count": value["pin_count"],
                "3d": {"status": "embedded" if model_ids else "not_embedded", "model_ids": sorted(model_ids)},
                "manual_review_required": True,
                "warnings": ["Existing native component preserved; source engineering validation was not repeated."],
            })
    return result


def verify_output(output_dir: Path, *, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    directory = Path(output_dir)
    errors: list[str] = []
    checks: list[str] = []
    manifest_path = state_directory(directory) / "manifest.json"
    if manifest is None:
        if (manifest_path.parent / "transaction.json").exists():
            return {"ok": False, "output": str(directory.resolve()), "checks": [], "errors": ["unfinished library transaction; run prepare to recover before verification"]}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "output": str(directory.resolve()),
                "checks": checks,
                "errors": [f"manifest unreadable: {exc}"],
            }
    if not isinstance(manifest, dict):
        return {"ok": False, "output": str(directory.resolve()), "checks": [], "errors": ["manifest must be an object"]}
    if manifest.get("schema_version") != 3:
        errors.append("unsupported manifest schema_version")
    else:
        checks.append("manifest_schema")
    if not manifest.get("published"):
        errors.append("manifest does not describe a published library set")
    else:
        checks.append("published_state")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        errors.append("manifest outputs missing")
        outputs = {}
    for name in ("LCSC.SchLib", "LCSC.PcbLib"):
        path = directory / name
        expected = outputs.get(name)
        if not isinstance(expected, dict):
            errors.append(f"missing output metadata: {name}")
            continue
        if not path.is_file():
            errors.append(f"output missing: {name}")
            continue
        actual_size = path.stat().st_size
        actual_hash = sha256_file(path)
        if actual_size != expected.get("size"):
            errors.append(f"size mismatch: {name}")
        elif actual_hash != expected.get("sha256"):
            errors.append(f"SHA-256 mismatch: {name}")
        else:
            checks.append(f"hash:{name}")

    if errors:
        return {
            "ok": False,
            "output": str(directory.resolve()),
            "checks": checks,
            "errors": errors,
        }

    try:
        inventory = native_inventory(directory)
        sch = altium.AltiumSchLib(directory / "LCSC.SchLib")
        pcb = altium.AltiumPcbLib.from_file(directory / "LCSC.PcbLib")
    except Exception as exc:
        errors.append(f"native library round-trip failed: {exc}")
    else:
        symbols = {symbol.name: symbol for symbol in sch.symbols}
        footprints = {footprint.name: footprint for footprint in pcb.footprints}
        expected = manifest.get("native_inventory") or {}
        if inventory != expected:
            errors.append("native inventory mismatch (symbols, footprints, fonts, images or embedded 3D)")
        else:
            checks.append("native_inventory_and_embedded_models")
        expected_symbols = set(expected.get("symbols", {}))
        expected_footprints = set(expected.get("footprints", {}))
        if set(symbols) != expected_symbols:
            errors.append("symbol names do not match manifest")
        else:
            checks.append("symbol_names")
        if set(footprints) != expected_footprints:
            errors.append("footprint names do not match manifest")
        else:
            checks.append("footprint_names")
        for item in manifest.get("components", []):
            if item.get("origin") == "existing_native_library":
                continue
            symbol_name = str(item.get("symbol_name"))
            footprint_name = str(item.get("footprint_name"))
            symbol = symbols.get(symbol_name)
            footprint = footprints.get(footprint_name)
            if symbol is None or footprint is None:
                continue
            expected_pins = (item.get("symbol") or {}).get("written_counts", {}).get("P")
            expected_pads = (item.get("footprint") or {}).get("written_counts", {}).get("PAD")
            symbol_written = (item.get("symbol") or {}).get("written_counts", {})
            symbol_raw = (item.get("symbol") or {}).get("raw_counts", {})
            footprint_written = (item.get("footprint") or {}).get("written_counts", {})
            if len(symbol.pins) != expected_pins:
                errors.append(f"pin count mismatch: {symbol_name}")
            if len(footprint.pads) != expected_pads:
                errors.append(f"pad count mismatch: {footprint_name}")
            expected_parts = int(symbol_raw.get("SUB_SYMBOL") or 1)
            if symbol.part_count != expected_parts:
                errors.append(f"part count mismatch: {symbol_name}")
            if len(symbol.rectangles) != symbol_written.get("R"):
                errors.append(f"rectangle count mismatch: {symbol_name}")
            expected_ellipses = int(symbol_written.get("E") or 0) + int(
                symbol_written.get("CIRCLE") or 0
            )
            if len(symbol.ellipses) != expected_ellipses:
                errors.append(f"ellipse count mismatch: {symbol_name}")
            if len(symbol.labels) != symbol_written.get("TEXT"):
                errors.append(f"label count mismatch: {symbol_name}")
            if len(footprint.tracks) != footprint_written.get("TRACK"):
                errors.append(f"track count mismatch: {footprint_name}")
            expected_arcs = int(footprint_written.get("ARC") or 0) + int(
                footprint_written.get("CIRCLE") or 0
            )
            if len(footprint.arcs) != expected_arcs:
                errors.append(f"arc count mismatch: {footprint_name}")
            if len(footprint.regions) != footprint_written.get("SOLIDREGION"):
                errors.append(f"region count mismatch: {footprint_name}")
            if len(footprint.vias) != int(footprint_written.get("VIA") or 0):
                errors.append(f"via count mismatch: {footprint_name}")
            if len(footprint.texts) != int(footprint_written.get("TEXT") or 0):
                errors.append(f"text count mismatch: {footprint_name}")
            expected_custom = footprint_written.get("CUSTOM_PAD")
            if expected_custom is not None:
                actual_custom = len(
                    [pad for pad in footprint.pads if pad.custom_shape is not None]
                )
                if actual_custom != expected_custom:
                    errors.append(f"custom pad count mismatch: {footprint_name}")
            links = [
                implementation
                for implementation in symbol.implementations
                if implementation.model_name == footprint_name
                and implementation.datafile_entity == "LCSC.PcbLib"
            ]
            if len(links) != 1:
                errors.append(f"symbol-footprint link mismatch: {symbol_name}")
        if not any("count mismatch" in error for error in errors):
            checks.append("primitive_counts")
        if not any("link mismatch" in error for error in errors):
            checks.append("symbol_footprint_links")

    return {
        "ok": not errors,
        "output": str(directory.resolve()),
        "checks": checks,
        "errors": errors,
        "components": len(manifest.get("components", [])),
    }


__all__ = ["output_metadata", "sha256_file", "native_inventory", "preserved_inventory", "existing_components", "verify_output"]
