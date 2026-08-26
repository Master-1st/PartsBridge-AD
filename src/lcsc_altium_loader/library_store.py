"""Library-only output, external maintenance state and recoverable publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

LIBRARY_NAMES = ("LCSC.SchLib", "LCSC.PcbLib")


def app_data_dir() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache"))
    return local / "PartsBridge-AD"


def default_output_dir() -> Path:
    try:
        value = json.loads((app_data_dir() / "preferences.json").read_text(encoding="utf-8"))
        directory = Path(value["library_directory"])
        if directory.is_absolute():
            return directory
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return Path("G:/dontdel/AD/_Lib")


def remember_output_dir(directory: Path) -> None:
    write_json(app_data_dir() / "preferences.json", {"library_directory": str(Path(directory).resolve())})


def state_directory(directory: Path) -> Path:
    canonical = os.path.normcase(str(Path(directory).resolve()))
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return app_data_dir() / "library-state" / key


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output_metadata(path: Path) -> dict[str, Any]:
    return {"size": path.stat().st_size, "sha256": sha256_file(path)}


def _metadata_or_none(path: Path) -> dict[str, Any] | None:
    return output_metadata(path) if path.exists() else None


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class LibraryStore:
    """Hold a process lock from snapshot through validation and publication.

    Two native files cannot be replaced atomically together. A write-ahead
    journal permits recovery only while all targets still match known hashes;
    otherwise recovery stops without overwriting an external editor's changes.
    """

    def __init__(self, directory: Path) -> None:
        self.output = Path(directory).resolve()
        self.state = state_directory(self.output)
        self.run_id = uuid.uuid4().hex
        self.stage: Path | None = None
        self.baseline: dict[str, Any] = {}
        self._lock: Any = None

    def __enter__(self) -> LibraryStore:
        if self.state.is_relative_to(self.output):
            raise ValueError("总库目录不能包含应用维护目录，请选择独立的库文件夹。")
        self.output.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        self._lock = (self.state / "library.lock").open("a+b")
        try:
            if self._lock.seek(0, os.SEEK_END) == 0:
                self._lock.write(b"\0")
                self._lock.flush()
            self._lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock.close()
            self._lock = None
            raise RuntimeError("此总库正在被另一个生成任务使用，请等待该任务结束。") from exc
        try:
            self.recover_pending()
            # Same volume as the destination, but never inside the library folder.
            self.stage = Path(tempfile.mkdtemp(prefix=".partsbridge-stage-", dir=self.output.parent))
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args: Any) -> None:
        try:
            if self.stage is not None and not (self.state / "transaction.json").exists():
                shutil.rmtree(self.stage, ignore_errors=True)
                (self.state / f".manifest-{self.run_id}.json").unlink(missing_ok=True)
        finally:
            if self._lock is not None:
                self._lock.close()  # OS releases the lock even after a process crash.
                self._lock = None

    def current_metadata(self) -> dict[str, Any]:
        return {name: _metadata_or_none(self.output / name) for name in LIBRARY_NAMES}

    def snapshot(self) -> Path:
        self.baseline = self.current_metadata()
        existing = [value is not None for value in self.baseline.values()]
        if any(existing) and not all(existing):
            raise RuntimeError("现有总库缺少 SchLib 或 PcbLib；为防止丢失历史，已停止追加。请先恢复完整库对。")
        assert self.stage is not None
        previous = self.stage / "previous"
        previous.mkdir()
        for name in LIBRARY_NAMES:
            if self.baseline[name] is not None:
                shutil.copy2(self.output / name, previous / name)
                if output_metadata(previous / name) != self.baseline[name]:
                    raise RuntimeError("复制基线时总库发生变化；未发布，请保存/关闭库后再试。")
        self.assert_unchanged()
        return previous

    def assert_unchanged(self) -> None:
        if self.current_metadata() != self.baseline:
            raise RuntimeError("总库在生成期间被其他程序修改；本次未覆盖，请保存/关闭库后重新追加。")

    def read_manifest(self) -> dict[str, Any]:
        try:
            value = json.loads((self.state / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != 3:
                return {}
            inventory = value.get("native_inventory")
            components = value.get("components")
            if not isinstance(inventory, dict) or not isinstance(components, list):
                return {}
            if not all(isinstance(inventory.get(key), dict) for key in ("symbols", "footprints", "models", "fonts", "images")):
                return {}
            return value
        except (OSError, ValueError, TypeError):
            return {}

    def _targets(self) -> dict[str, Path]:
        return {**{name: self.output / name for name in LIBRARY_NAMES}, "manifest.json": self.state / "manifest.json"}

    def recover_pending(self, *, commit_if_complete: bool = True) -> None:
        journal_path = self.state / "transaction.json"
        if not journal_path.exists():
            return
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            run_id = journal["run_id"]
            if not re.fullmatch(r"[0-9a-f]{32}", run_id) or journal["output"] != str(self.output):
                raise ValueError("transaction identity mismatch")
            records = journal["files"]
            targets = self._targets()
            if set(records) != set(targets):
                raise ValueError("transaction file set mismatch")
            for record in records.values():
                for key in ("before", "after"):
                    value = record[key]
                    if value is not None and (
                        not isinstance(value.get("size"), int)
                        or not re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))
                    ):
                        raise ValueError("invalid transaction hash")
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise RuntimeError(f"维护事务记录损坏，未改动总库。请检查 {journal_path}") from exc

        backup = self.state / "backups" / run_id
        actual = {name: _metadata_or_none(path) for name, path in targets.items()}
        unknown = [name for name in targets if actual[name] not in (records[name]["before"], records[name]["after"])]
        if unknown:
            raise RuntimeError(
                "上次保存中断且文件随后发生变化，已停止自动恢复以保留你的修改："
                + ", ".join(unknown) + f"。备份：{backup}；事务记录：{journal_path}"
            )
        complete = all(actual[name] == records[name]["after"] for name in targets)
        if not (complete and commit_if_complete):
            # Validate every backup before touching any target.
            for name in targets:
                before = records[name]["before"]
                if before is not None and _metadata_or_none(backup / name) != before:
                    raise RuntimeError(f"恢复备份不完整，未改动总库：{backup / name}")
            for name, target in reversed(list(targets.items())):
                before = records[name]["before"]
                if actual[name] == before:
                    continue
                if _metadata_or_none(target) != actual[name]:
                    raise RuntimeError(f"恢复期间文件发生变化，停止恢复：{target}；备份：{backup}")
                if before is None:
                    target.unlink(missing_ok=True)
                else:
                    temporary_root = self.output.parent if name in LIBRARY_NAMES else self.state
                    fd, filename = tempfile.mkstemp(prefix=".partsbridge-restore-", dir=temporary_root)
                    os.close(fd)
                    temporary = Path(filename)
                    try:
                        shutil.copy2(backup / name, temporary)
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
            if any(_metadata_or_none(path) != records[name]["before"] for name, path in targets.items()):
                raise RuntimeError(f"恢复校验失败；请保留备份并检查：{backup}")
        journal_path.unlink()
        (self.state / f".manifest-{run_id}.json").unlink(missing_ok=True)

    def publish(self, manifest: dict[str, Any]) -> None:
        assert self.stage is not None
        self.assert_unchanged()
        backup = self.state / "backups" / self.run_id
        backup.mkdir(parents=True)
        manifest["backup_directory"] = str(backup)
        candidate = self.state / f".manifest-{self.run_id}.json"
        write_json(candidate, manifest)
        targets = self._targets()
        sources = {**{name: self.stage / name for name in LIBRARY_NAMES}, "manifest.json": candidate}
        records: dict[str, Any] = {}
        for name, target in targets.items():
            before = self.baseline[name] if name in LIBRARY_NAMES else _metadata_or_none(target)
            if before is not None:
                original = self.stage / "previous" / name if name in LIBRARY_NAMES else target
                shutil.copy2(original, backup / name)
                if output_metadata(backup / name) != before:
                    raise RuntimeError(f"发布前备份校验失败，未覆盖旧库：{backup / name}")
                with (backup / name).open("r+b") as handle:
                    os.fsync(handle.fileno())
            records[name] = {"before": before, "after": output_metadata(sources[name])}
        self.assert_unchanged()
        write_json(self.state / "transaction.json", {"run_id": self.run_id, "output": str(self.output), "files": records})
        try:
            for name, target in targets.items():
                if _metadata_or_none(target) != records[name]["before"]:
                    raise RuntimeError(f"发布时检测到外部修改，停止覆盖：{target}")
                os.replace(sources[name], target)
            if any(_metadata_or_none(path) != records[name]["after"] for name, path in targets.items()):
                raise RuntimeError("发布后的文件校验失败")
        except Exception as exc:
            try:
                self.recover_pending(commit_if_complete=False)
            except Exception as recovery_exc:
                raise RuntimeError(f"保存失败：{exc}；恢复未完成：{recovery_exc}") from exc
            raise
        (self.state / "transaction.json").unlink()
        candidate.unlink(missing_ok=True)


__all__ = ["LIBRARY_NAMES", "LibraryStore", "default_output_dir", "remember_output_dir", "state_directory", "sha256_file", "output_metadata", "write_json"]
