"""Bounded streaming uploads of one browser-selected SolidWorks folder.

The portal accepts ``POST /api/runs`` as ``multipart/form-data`` with repeated parts named
``files``; each part's filename is the browser's ``webkitRelativePath`` with ``/`` separators
(the selected folder is the first segment). Authentication and CSRF are enforced by the caller
before this module reads a single byte of the body, and nothing here trusts client digests.

Admission rules (any violation rejects the whole upload and removes staging):

* one shared top folder for every file; portable member names through the shared
  ``artifact_path_parts`` admission (rejects ``..``, absolute, backslash, control characters,
  Windows-forbidden characters, reserved device names, trailing dot/space, ``.git``);
* explicit duplicate and casefold-alias rejection including file/directory collisions, so the
  upload cannot alias differently on a case-insensitive Windows extraction;
* SolidWorks lock transients (``~$*``) are rejected rather than silently dropped;
* an optional ``main_assembly`` text part selects the delivered assembly: a canonical POSIX
  path inside the selected top folder (top folder excluded), case-exact against the admitted
  files, ``.sldasm`` only; absent keeps the current native marker / unique-root discovery;
* 4096 files / 2 GiB aggregate / 512 MiB per file / path and component length caps, enforced
  incrementally while streaming (never after buffering the whole body).

After the stream ends, the staged folder passes the pipeline's own ``describe_handoff`` (the
same admission the DAG resolves later), so the canonical ``handoff_sha256`` is computed
server-side from the admitted bytes and matches the filesystem-path import for identical
bytes. Finalization is one atomic rename of ``<intake>/.staging/<run_id>`` to
``<intake>/<run_id>`` followed by 0444 files / 0555 directories; finalized evidence is never
deleted or overwritten, and staging is removed on every failure path.

Deployment prerequisites: ``portal.upload_root`` must be the dedicated Linux intake directory
``SOLIDWORKS_HANDOFF_ROOT`` that the installer provisions and registers inside the
``solidworks_windows`` connection's ``handoff_roots`` allowlist, and it must stay disjoint
from the Windows endpoint ``package_root`` and any pipeline store/import directory.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from python_multipart.exceptions import FormParserError, MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from ..io import PipelineError, artifact_path_parts
from ..sources.solidworks.handoff import describe_handoff

UPLOAD_FIELD = "files"
MAIN_ASSEMBLY_FIELD = "main_assembly"
MAX_MAIN_ASSEMBLY_BYTES = 1024
MAX_FILES = 4096
MAX_TOTAL_BYTES = 2 * 1024**3
MAX_FILE_BYTES = 512 * 1024**2
MAX_RELATIVE_PATH = 1024
MAX_COMPONENT = 255
#: Multipart framing allowance so a file sum exactly at the aggregate limit still passes the
#: pre-read Content-Length check (the streamed byte counter is the authoritative bound).
BODY_OVERHEAD_BYTES = MAX_FILES * 1024 + 65536
MAX_BODY_BYTES = MAX_TOTAL_BYTES + BODY_OVERHEAD_BYTES
DISK_RESERVE_FLOOR = 2 * 1024**3
DISK_RESERVE_FRACTION = 0.05
DISK_RECHECK_BYTES = 128 * 1024**2
STAGING_DIRNAME = ".staging"
TRANSIENT_PREFIX = "~$"
_READ_CHUNK = 1 << 20

log = logging.getLogger(__name__)


class UploadRejected(Exception):
    """One bounded, user-facing upload rejection."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class UploadReceipt:
    folder: str
    files: int
    bytes: int
    handoff_sha256: str
    main_assembly: str | None = None


class UploadGate:
    """Bound upload concurrency: ``concurrency`` portal-wide plus one per principal."""

    def __init__(self, concurrency: int = 2) -> None:
        self._portal = threading.BoundedSemaphore(max(1, concurrency))
        self._principals: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    @contextmanager
    def slot(self, principal: str) -> Iterator[None]:
        if not self._portal.acquire(blocking=False):
            raise UploadRejected(429, "上传通道繁忙，请稍后重试")
        with self._lock:
            per_principal = self._principals.setdefault(principal, threading.BoundedSemaphore(1))
        if not per_principal.acquire(blocking=False):
            self._portal.release()
            raise UploadRejected(429, "已有一个上传正在进行，请等待完成")
        try:
            yield
        finally:
            per_principal.release()
            self._portal.release()


def admit_member(filename: object) -> tuple[str, tuple[str, ...]]:
    """Admit one uploaded member name; returns the NFC relative path and its parts."""
    if not isinstance(filename, str) or not filename:
        raise UploadRejected(400, "上传文件缺少名称")
    if "\\" in filename:
        raise UploadRejected(400, "文件路径不能包含反斜杠")
    name = unicodedata.normalize("NFC", filename)
    segments = name.split("/")
    if any(segment == "" for segment in segments):
        raise UploadRejected(400, "文件路径包含空路径段")
    if len(segments) < 2:
        raise UploadRejected(400, "请选择整个工程文件夹，而不是单个文件")
    for segment in segments:
        if segment in {".", ".."}:
            raise UploadRejected(400, "文件路径不能包含 . 或 .. 路径段")
        if len(segment) > MAX_COMPONENT:
            raise UploadRejected(400, "文件名超过 255 个字符")
        if segment.startswith(TRANSIENT_PREFIX):
            raise UploadRejected(400, "上传包含 SolidWorks 临时锁文件（~$ 开头）；请关闭 SolidWorks 或删除后重试")
    if len(name) > MAX_RELATIVE_PATH:
        raise UploadRejected(400, "文件相对路径超过 1024 个字符")
    try:
        parts = artifact_path_parts(name)
    except PipelineError as error:
        raise UploadRejected(400, f"不符合平台文件命名规则：{str(error)[:200]}") from error
    if parts != tuple(segments):
        raise UploadRejected(400, "文件路径规范化结果不一致")
    return name, parts


def receive_folder(
    environ: dict,
    *,
    intake_root: Path,
    run_id: str,
    principal: str,
    gate: UploadGate,
) -> UploadReceipt:
    """Consume one multipart folder upload into the intake root; atomic on success."""
    root = Path(intake_root)
    final = root / run_id
    if final.exists():
        raise UploadRejected(409, "该运行的上传目录已存在")
    kind, options = parse_options_header(str(environ.get("CONTENT_TYPE") or ""))
    if kind.lower() != b"multipart/form-data":
        raise UploadRejected(400, "请通过页面上的文件夹按钮上传工程文件夹")
    boundary = options.get(b"boundary")
    if not boundary:
        raise UploadRejected(400, "上传请求缺少 multipart 边界")
    declared_raw = environ.get("CONTENT_LENGTH")
    if not declared_raw:
        raise UploadRejected(411, "上传请求缺少长度声明，无法安全校验")
    try:
        declared = int(str(declared_raw))
    except ValueError as error:
        raise UploadRejected(400, "上传长度声明不是整数") from error
    if declared <= 0:
        raise UploadRejected(400, "上传内容为空")
    if declared > MAX_BODY_BYTES:
        raise UploadRejected(413, "上传超过 2 GiB 或 4096 个文件的上限")
    staging = root / STAGING_DIRNAME / run_id
    with gate.slot(principal):
        _require_disk(root, declared)
        try:
            staging.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            if error.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise UploadRejected(507, "服务器存储空间不足，请稍后重试") from error
            raise UploadRejected(500, "无法创建上传暂存目录") from error
        try:
            receiver = _Receiver(staging, root)
            try:
                _parse_body(environ["wsgi.input"], boundary, receiver, declared)
                receipt = receiver.finish()
            except BaseException:
                receiver.close()
                raise
            os.rename(staging, final)
        except BaseException:
            _remove_tree(staging)
            raise
        try:
            _seal_tree(final)
        except OSError:  # best effort: the finalized folder is retained either way
            log.warning("uploaded folder could not be sealed read-only: %s", final)
    return receipt


def _require_disk(intake_root: Path, declared: int) -> None:
    usage = shutil.disk_usage(intake_root)
    reserve = max(DISK_RESERVE_FLOOR, int(usage.total * DISK_RESERVE_FRACTION))
    if usage.free - declared - reserve < 0:
        raise UploadRejected(507, "服务器存储空间不足，请稍后重试")


def _parse_body(stream: Any, boundary: bytes | str, receiver: _Receiver, declared: int) -> None:
    parser = MultipartParser(boundary, receiver.callbacks(), max_size=MAX_BODY_BYTES + 1)
    remaining = declared
    while remaining > 0:
        chunk = stream.read(min(_READ_CHUNK, remaining))
        if not chunk:
            raise UploadRejected(400, "上传中断，数据不完整")
        remaining -= len(chunk)
        try:
            parser.write(chunk)
        except (MultipartParseError, FormParserError) as error:
            raise UploadRejected(400, "上传内容无法解析为 multipart 数据") from error
    try:
        parser.finalize()
    except (MultipartParseError, FormParserError) as error:
        raise UploadRejected(400, "上传内容不完整") from error
    if not receiver.ended:
        raise UploadRejected(400, "上传内容不完整（缺少结束边界）")


class _Receiver:
    """Streaming multipart callback state machine; one instance per upload."""

    def __init__(self, staging: Path, intake_root: Path) -> None:
        self.staging = staging
        self.intake_root = intake_root
        self.top: str | None = None
        self.files: dict[str, str] = {}
        self.directories: dict[str, str] = {}
        self.count = 0
        self.total = 0
        self._last_disk_check = 0
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._disposition = b""
        self._handle: int | None = None
        self._part_bytes = 0
        self.ended = False
        self._text_field: str | None = None
        self._text_data = bytearray()
        self._assembly_seen = False
        self._assembly: str | None = None

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
            "on_end": self._on_end,
        }

    def _on_part_begin(self) -> None:
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._disposition = b""
        self._part_bytes = 0
        self._text_field = None
        self._text_data = bytearray()

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field.extend(bytes(data[start:end]))

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value.extend(bytes(data[start:end]))

    def _on_header_end(self) -> None:
        if bytes(self._header_field).strip().lower() == b"content-disposition":
            self._disposition = bytes(self._header_value)
        self._header_field = bytearray()
        self._header_value = bytearray()

    def _on_headers_finished(self) -> None:
        if self._handle is not None:
            raise UploadRejected(400, "上传分片未正确闭合")
        disposition, params = parse_options_header(self._disposition)
        if disposition.lower() != b"form-data":
            raise UploadRejected(400, "上传分片缺少表单描述")
        field = params.get(b"name")
        if field == MAIN_ASSEMBLY_FIELD.encode("ascii"):
            if params.get(b"filename"):
                raise UploadRejected(400, "主装配选择不能作为文件上传")
            if self._assembly_seen:
                raise UploadRejected(400, "上传包含重复的主装配选择")
            self._assembly_seen = True
            self._text_field = MAIN_ASSEMBLY_FIELD
            return
        if field != UPLOAD_FIELD.encode("ascii"):
            raise UploadRejected(400, "只接受名为 files 的文件部分与 main_assembly 选择")
        raw_name = params.get(b"filename")
        if not raw_name:
            raise UploadRejected(400, "上传分片缺少文件名")
        try:
            filename = raw_name.decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise UploadRejected(400, "文件名不是有效的 UTF-8 文本") from error
        name, parts = admit_member(filename)
        top = parts[0]
        if self.top is None:
            self.top = top
        elif top != self.top:
            if top.casefold() == self.top.casefold():
                raise UploadRejected(400, "文件夹名称大小写不一致")
            raise UploadRejected(400, "一次只能上传一个工程文件夹")
        key = name.casefold()
        if key in self.files:
            raise UploadRejected(400, "上传包含重复文件路径")
        if key in self.directories:
            raise UploadRejected(400, "上传包含文件与目录同名冲突")
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            prefix_key = prefix.casefold()
            if prefix_key in self.files:
                raise UploadRejected(400, "上传包含文件与目录同名冲突")
            prior = self.directories.get(prefix_key)
            if prior is None:
                self.directories[prefix_key] = prefix
            elif prior != prefix:
                raise UploadRejected(400, "目录名称大小写不一致")
        if self.count >= MAX_FILES:
            raise UploadRejected(413, "上传文件数量超过 4096 个上限")
        target = self.staging.joinpath(*parts)
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as error:
            raise UploadRejected(400, "上传包含重复文件路径") from error
        except OSError as error:
            if error.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise UploadRejected(507, "服务器存储空间不足，请稍后重试") from error
            raise UploadRejected(500, "无法写入上传文件") from error
        self.files[key] = name
        self.count += 1

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._text_field is not None:
            self._text_data.extend(bytes(data[start:end]))
            if len(self._text_data) > MAX_MAIN_ASSEMBLY_BYTES:
                raise UploadRejected(400, "主装配选择过长")
            return
        if self._handle is None:
            raise UploadRejected(400, "上传分片数据出现在文件打开之前")
        chunk = bytes(data[start:end])
        self._part_bytes += len(chunk)
        self.total += len(chunk)
        if self._part_bytes > MAX_FILE_BYTES:
            raise UploadRejected(413, "单个文件超过 512 MiB 上限")
        if self.total > MAX_TOTAL_BYTES:
            raise UploadRejected(413, "上传总大小超过 2 GiB 上限")
        if self.total - self._last_disk_check >= DISK_RECHECK_BYTES:
            self._last_disk_check = self.total
            usage = shutil.disk_usage(self.intake_root)
            reserve = max(DISK_RESERVE_FLOOR, int(usage.total * DISK_RESERVE_FRACTION))
            if usage.free - reserve < 0:
                raise UploadRejected(507, "服务器存储空间不足，请稍后重试")
        view = memoryview(chunk)
        written = 0
        try:
            while written < len(view):
                written += os.write(self._handle, view[written:])
        except OSError as error:
            if error.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise UploadRejected(507, "服务器存储空间不足，请稍后重试") from error
            raise UploadRejected(500, "写入上传文件失败") from error

    def _on_part_end(self) -> None:
        if self._text_field is not None:
            self._finish_text()
            return
        self.close()

    def _on_end(self) -> None:
        self._on_part_end()
        self.ended = True

    def _finish_text(self) -> None:
        raw = bytes(self._text_data)
        try:
            value = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise UploadRejected(400, "主装配选择不是有效的 UTF-8 文本") from error
        self._assembly = value
        self._text_field = None
        self._text_data = bytearray()

    def _validate_main_assembly(self) -> str | None:
        """One canonical, case-exact selection inside the uploaded top folder."""
        raw = self._assembly
        if raw is None:
            return None
        value = unicodedata.normalize("NFC", raw)
        if value != raw.strip():
            raise UploadRejected(400, "主装配路径不能包含首尾空白")
        if not value:
            raise UploadRejected(400, "主装配选择为空")
        try:
            parts = artifact_path_parts(value)
        except PipelineError as error:
            raise UploadRejected(400, f"主装配路径不符合平台文件命名规则：{str(error)[:200]}") from error
        if parts != tuple(value.split("/")):
            raise UploadRejected(400, "主装配路径规范化结果不一致")
        if Path(value).suffix.casefold() != ".sldasm":
            raise UploadRejected(400, "主装配必须是 .SLDASM 文件")
        prefix = f"{self.top}/"
        relative = {name[len(prefix):]: name for name in self.files.values() if name.startswith(prefix)}
        if value in relative:
            return value
        if any(key.casefold() == value.casefold() for key in relative):
            raise UploadRejected(400, "主装配路径的大小写与上传文件不一致，请从列表中选择")
        raise UploadRejected(400, "主装配不在所选工程文件夹中")

    def finish(self) -> UploadReceipt:
        self.close()
        if self.top is None or self.count == 0:
            raise UploadRejected(400, "未收到任何文件，请选择工程文件夹后重试")
        source = self.staging / self.top
        try:
            identity = describe_handoff(source)
        except PipelineError as error:
            raise UploadRejected(400, f"工程文件夹未通过平台校验：{str(error)[:200]}") from error
        return UploadReceipt(
            folder=self.top,
            files=self.count,
            bytes=self.total,
            handoff_sha256=str(identity["handoff_sha256"]),
            main_assembly=self._validate_main_assembly(),
        )

    def close(self) -> None:
        """Idempotent: release the part file handle on every exit path (no fd leaks)."""
        if self._handle is not None:
            try:
                os.close(self._handle)
            finally:
                self._handle = None


def _seal_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False):
        for name in files:
            os.chmod(os.path.join(current, name), 0o444)
        for name in directories:
            os.chmod(os.path.join(current, name), 0o555)
    os.chmod(root, 0o555)


def _remove_tree(root: Path) -> None:
    """Remove one staging tree even after sealing; never used on finalized uploads."""
    if not root.exists():
        return
    for current, _directories, files in os.walk(root):
        os.chmod(current, 0o700)
        for name in files:
            try:
                os.chmod(os.path.join(current, name), 0o600)
            except OSError:
                continue
    shutil.rmtree(root, ignore_errors=True)
