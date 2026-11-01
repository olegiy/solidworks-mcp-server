"""Session-owned original files, with transactional replacement after validation."""

import hashlib
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from cadnext.mesh.contracts import MeshError, MeshLimits, input_scale

_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


@dataclass(frozen=True)
class MeshAsset:
    asset_id: str
    session_id: str
    sha256: str
    format: str
    units: str
    size_bytes: int
    state: str


class MeshAssetStore:
    def __init__(self, root: Path, *, limits: MeshLimits | None = None):
        self.root = Path(root).resolve()
        self.limits = limits or MeshLimits()
        self._assets: dict[str, MeshAsset] = {}
        self._current: dict[str, str] = {}
        self._pending: dict[str, str] = {}
        self._receiving: set[str] = set()
        self.references: dict[str, dict] = {}

    @staticmethod
    def _session(session_id):
        if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
            raise MeshError("invalid_session", "Некорректный идентификатор диалога.")

    def _asset(self, session_id, asset_id):
        self._session(session_id)
        asset = self._assets.get(asset_id)
        if asset is None or asset.session_id != session_id:
            raise MeshError("missing_asset", "Эталон сетки не найден в этом диалоге.")
        return asset

    def path_for(self, session_id, asset_id) -> Path:
        asset = self._asset(session_id, asset_id)
        return self.root / asset.session_id / f"{asset.asset_id}.{asset.format}"

    def current(self, session_id) -> MeshAsset | None:
        self._session(session_id)
        asset_id = self._current.get(session_id)
        return self._assets.get(asset_id) if asset_id else None

    def processing(self, session_id):
        return session_id in self._pending or session_id in self._receiving

    async def receive(self, session_id, chunks, *, format, units) -> MeshAsset:
        self._session(session_id)
        input_scale(format, units)
        if session_id in self._receiving or session_id in self._pending:
            raise MeshError("asset_busy", "Дождитесь обработки предыдущего файла сетки.")
        self._receiving.add(session_id)
        asset_id = uuid4().hex
        folder = self.root / session_id
        temporary = folder / f"{asset_id}.upload"
        final = folder / f"{asset_id}.{format}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if folder.is_symlink() or folder.resolve().parent != self.root:
                raise MeshError("invalid_storage", "Каталог эталона находится вне хранилища.")
            digest, size = hashlib.sha256(), 0
            with temporary.open("xb") as stream:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise MeshError("invalid_upload", "Некорректный поток загрузки сетки.")
                    size += len(chunk)
                    if size > self.limits.max_bytes:
                        raise MeshError("byte_limit", "Файл сетки превышает допустимый размер.")
                    digest.update(chunk)
                    stream.write(chunk)
                if not size:
                    raise MeshError("empty_upload", "Файл сетки пуст.")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, final)
            asset = MeshAsset(asset_id, session_id, digest.hexdigest(), format, units, size, "processing")
            self._assets[asset_id] = asset
            self._pending[session_id] = asset_id
            return asset
        except BaseException:
            temporary.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            self._remove_empty_folder(session_id)
            raise
        finally:
            self._receiving.discard(session_id)

    def accept(self, session_id, asset_id):
        asset = self._asset(session_id, asset_id)
        if self._pending.get(session_id) != asset_id:
            raise MeshError("stale_asset", "Обработка этого эталона уже завершена.")
        previous = self._current.get(session_id)
        if previous:
            self.path_for(session_id, previous).unlink(missing_ok=True)
            self._assets.pop(previous)
            self.references.pop(previous, None)
        self._assets[asset_id] = replace(asset, state="ready")
        self._current[session_id] = asset_id
        del self._pending[session_id]

    def reject(self, session_id, asset_id):
        self._asset(session_id, asset_id)
        if self._pending.get(session_id) != asset_id:
            raise MeshError("stale_asset", "Нельзя удалить принятый эталон как временную загрузку.")
        self.path_for(session_id, asset_id).unlink(missing_ok=True)
        del self._assets[asset_id]
        del self._pending[session_id]
        self._remove_empty_folder(session_id)

    def remove(self, session_id):
        self._session(session_id)
        for asset_id in (self._pending.pop(session_id, None), self._current.pop(session_id, None)):
            if asset_id is not None:
                self.path_for(session_id, asset_id).unlink(missing_ok=True)
                del self._assets[asset_id]
                self.references.pop(asset_id, None)
        self._remove_empty_folder(session_id)

    def _remove_empty_folder(self, session_id):
        try:
            (self.root / session_id).rmdir()
        except OSError:
            pass

    def import_ready(self, session_id, source, manifest):
        from cadnext.mesh.persistence import copy_verified, validate_manifest

        self._session(session_id)
        manifest = validate_manifest(manifest)
        reference = manifest["reference"]
        asset_id = uuid4().hex
        destination = self.root / session_id / f'{asset_id}.{reference["format"]}'
        size = copy_verified(source, destination, reference, expected_size=manifest["size_bytes"])
        previous = self.current(session_id)
        if previous is not None:
            self.remove(session_id)
        asset = MeshAsset(asset_id, session_id, reference["sha256"], reference["format"],
                          reference["units"], size, "ready")
        self._assets[asset_id] = asset
        self._current[session_id] = asset_id
        self.references[asset_id] = {**reference, "asset_id": asset_id}
        return asset

    def close(self):
        sessions = {asset.session_id for asset in self._assets.values()}
        for session in sessions:
            self.remove(session)
            try:
                (self.root / session).rmdir()
            except OSError:
                pass
        try:
            self.root.rmdir()
        except OSError:
            pass
