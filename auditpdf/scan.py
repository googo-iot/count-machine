"""PDF 루트 폴더를 훑어 리포트 PDF 를 모두 읽는다. 뷰어, 리포트, Excel 은 이 결과만 쓴다.

파일명에 리포트 종류 단어(KINDS)가 앞뒤로 다른 영문자 없이 들어간 PDF 만 읽는다.
예) 20261007_110858_VIC55-REPORT-Batch[CNZ2F015A]__[니자티딘캡슐 150mg 30C].pdf → 배치 리포트

원본 PDF 는 읽기만 한다. 읽은 결과는 메모리와 디스크(%APPDATA%\\audit-pdf-viewer\\cache)에 두고,
경로·크기·수정 시각이 같은 파일은 다시 열지 않는다. 그래서 프로그램을 다시 켜도 새 파일이나 바뀐 파일만 읽는다.
pdf.py 를 고치면 해석 결과가 달라질 수 있으므로 그 파일 내용의 해시(PARSER_VERSION)가 다른 캐시는 쓰지 않는다.
"""

import datetime
import gc
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import pdf as _pdf
from .config import CONFIG_PATH
from .pdf import PdfResult, read_pdf

# 파일명 단어 → (종류, 화면 이름). 대소문자 무시.
KINDS = {
    "AuditTrailLog": ("audit", "감사 추적"),
    "Batch": ("batch", "배치 리포트"),
    "BottleCountLog": ("bottle", "병 계수 기록"),
    "NGImageALL": ("ng", "NG 이미지"),
}
KIND_NAMES = {kind: name for kind, name in KINDS.values()}
FILENAME_PATTERN = re.compile(r"(?<![A-Za-z])(" + "|".join(KINDS) + r")(?![A-Za-z])", re.I)
FILENAME_KEYWORD = ", ".join(KINDS)   # 안내 문구용

CACHE_DIR = CONFIG_PATH.parent / "cache"
PARSER_VERSION = hashlib.sha1(Path(_pdf.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]  # 줄바꿈 문자는 무시

_cache = {}
_cache_lock = threading.Lock()


class Cancelled(Exception):
    pass


@dataclass
class FileInfo:
    seq: int
    folder: str
    name: str
    path: str
    kind: str                # audit / batch / bottle / ng
    status: str              # OK / 내용 없음 / 중복 / 예외 내용
    pages: int
    rows: int                # 본문 항목 수 (로그 행, 생산 결과, 계수 행, NG 이미지)
    alarms: int
    bad_time: int
    no_user: int
    size: int
    mtime: str
    sha256: str
    meta: dict
    data: PdfResult = field(default=None, repr=False)
    dup_of: int = 0          # 앞서 읽은 같은 내용(SHA-256) 파일의 seq. 0 이면 원본

    @property
    def failed(self):
        return self.status not in ("OK", "내용 없음", "중복")


@dataclass
class Scan:
    root: Path
    files: list = field(default_factory=list)      # FileInfo

    def of(self, kind):
        return [f for f in self.files if f.kind == kind and f.data and not f.dup_of]

    @property
    def records(self):
        """감사 추적 [(FileInfo, LogRow)]."""
        return [(f, r) for f in self.of("audit") for r in f.data.rows]


def kind_of(name):
    """파일명의 리포트 종류. 대상이 아니면 None."""
    m = FILENAME_PATTERN.search(name)
    if not m:
        return None
    word = m.group(1).lower()
    return next(kind for key, (kind, _) in KINDS.items() if key.lower() == word)


def find_pdfs(root):
    return sorted((p for p in Path(root).rglob("*.pdf") if kind_of(p.name) and p.is_file()),
                  key=lambda p: (str(p.parent).lower(), p.name.lower()))


def rel_folder(path, root):
    try:
        rel = path.parent.relative_to(root)
    except ValueError:
        return path.parent.name
    return str(rel) if str(rel) != "." else Path(root).name


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _try_sha256(path):
    try:
        return sha256_of(path)
    except OSError:
        return ""


def _cache_file(path):
    return CACHE_DIR / (hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest() + ".json")


def _load_disk(path, stat, kind):
    """디스크 캐시에서 (PdfResult, sha256). 없거나 파일/해석기가 바뀌었으면 None."""
    try:
        c = json.loads(_cache_file(path).read_text(encoding="utf-8"))
        if (c["parser"], c["path"], c["size"], c["mtime_ns"], c["kind"]) != \
                (PARSER_VERSION, str(path), stat.st_size, stat.st_mtime_ns, kind):
            return None
        return PdfResult.from_json(c["result"]), c["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_disk(path, stat, kind, result, digest):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_file(path).with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "parser": PARSER_VERSION, "path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "kind": kind, "sha256": digest, "result": result.to_json(),
        }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_cache_file(path))
    except OSError:
        pass  # 캐시를 못 써도 결과에는 영향이 없다


def _read_cached(path, stat, kind):
    """메모리 → 디스크 캐시 → PDF 순서로 찾는다. 경로·크기·수정 시각이 같으면 같은 파일로 본다."""
    key = (str(path), stat.st_size, stat.st_mtime_ns, kind)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is None:
        hit = _load_disk(path, stat, kind)
        if hit is None:
            hit = (read_pdf(path, kind), sha256_of(path))
            _save_disk(path, stat, kind, *hit)
            # PDF 해석 객체는 순환 참조가 많아 자동 GC 를 기다리면 수백 건 읽는 동안 GB 단위로 쌓인다
            gc.collect()
        with _cache_lock:
            _cache[key] = hit
    return hit


def scan(root, progress=None, cancel=None):
    """root 아래 리포트 PDF 를 모두 읽는다.

    progress(done, total, name) 는 파일을 시작할 때마다 부른다. cancel 이 set 되면 다음 파일에서 멈춘다.
    """
    root = Path(root).resolve()
    pdfs = find_pdfs(root)
    result = Scan(root)
    for seq, path in enumerate(pdfs, start=1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if progress:
            progress(seq - 1, len(pdfs), path.name)
        kind = kind_of(path.name)
        stat = path.stat()
        try:
            pdf, digest = _read_cached(path, stat, kind)
            status = "OK" if pdf.items else "내용 없음"
        except Exception as e:  # 손상된 PDF 등: 파일 목록에 사유를 남기고 계속한다
            pdf, digest, status = None, "", f"{type(e).__name__}: {e}"
        result.files.append(FileInfo(
            seq=seq, folder=rel_folder(path, root), name=path.name, path=str(path), kind=kind, status=status,
            pages=pdf.pages if pdf else 0, rows=pdf.items if pdf else 0,
            alarms=sum(r.alarm for r in pdf.rows) if pdf else 0,
            bad_time=pdf.bad_time if pdf else 0, no_user=pdf.no_user if pdf else 0,
            size=stat.st_size, mtime=datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            sha256=digest or _try_sha256(path),
            meta={k: (pdf.meta.get(k, "") if pdf else "") for k in _pdf.META_KEYS}, data=pdf,
        ))
    mark_duplicates(result.files)
    if progress:
        progress(len(pdfs), len(pdfs), "")
    return result


def mark_duplicates(files):
    """분기별 백업처럼 같은 PDF 가 다른 폴더에 또 있으면 처음 것만 쓰고 나머지는 '중복' 으로 둔다.

    내용(SHA-256)이 같아야 중복이다. 이름만 같고 내용이 다르면 둘 다 쓴다.
    """
    seen = {}
    for f in files:
        if f.data is None or not f.sha256:
            continue
        if f.sha256 in seen:
            f.dup_of, f.status = seen[f.sha256], "중복"
        else:
            seen[f.sha256] = f.seq
