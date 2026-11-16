"""중량선별기(ITC81DB 계열) 데이터 폴더를 읽는다. 원본 파일은 읽기만 한다.

폴더 구조 (예: C:\\#Inbox\\WC)
    Data\\Inspection\\2026\\9\\I20260905-7.csv   검사 기록: 날짜-품목번호 하나가 한 '런'. 한 줄이 제품 하나의 계량 결과
        Date,Time,P_No,Name,Lot,UnderRange,PassRange,OverRange,TareRange,Weight,Grade(Pass/Under/Over/Double)
    Data\\History\\2026\\9\\H20260905.csv        이력(감사 추적): 하루 한 파일
        Date,Time,LoginID,Event,Detail,Before,After,Type(Operation/Parameter/Alarm)
    Data\\Others\\2026\\1\\S20260116_112836.csv  품목별 통계 한 줄 (머리글 없음, 열 의미 미확인)
    DataBackup\\*_Alarm.csv                     알람 백업: Date,Time,Raw Data,Alarm Name
    Header\\*_h.ini                             장비 정보 (SerialNumber 등)
    Encryption\\                                암호화된 사본 - 읽지 않는다

CSV 는 따옴표 없이 쉼표로만 나뉘어, 품명에 쉼표가 들어가면(예: 텔로셋40,5,12.5) 열이 늘어난다.
검사 기록은 품명 뒤의 열 개수가 고정이므로 남는 열을 품명으로 다시 합친다.
이력의 변경 전/후 값에 쉼표가 들어간 경우는 앞서 나온 값(품명 등)과 맞는 쪽으로 나누고, 못 맞추면 변경 전을 첫 칸으로 본다.
"""

import os
import re
import statistics
import threading
from datetime import datetime
from pathlib import Path

INSPECTION = re.compile(r"^I(\d{8})-(\d+)\.csv$", re.I)
HISTORY = re.compile(r"^H(\d{8})\.csv$", re.I)
STATS = re.compile(r"^S(\d{8})_(\d{6})\.csv$", re.I)
ALARM = re.compile(r"_Alarm\.csv$", re.I)
HEADER = re.compile(r"_h\.ini$", re.I)
SKIP_DIRS = {"encryption", "removefile", "run", "fonts", "systembackup", "systemfile1", "systemfile2"}

KIND_NAMES = {"inspection": "검사 기록", "history": "이력", "stats": "통계", "alarm": "알람 백업", "header": "장비 정보"}
I_HEAD = ["Date", "Time", "P_No", "Name", "Lot", "UnderRange", "PassRange", "OverRange", "TareRange", "Weight", "Grade"]
H_HEAD = ["Date", "Time", "LoginID", "Event", "Detail", "Before", "After", "Type"]
GRADES = ("Pass", "Under", "Over", "Double")

_cache = {}            # (경로, 크기, 수정 시각) → 파일 하나를 읽은 결과
_cache_lock = threading.Lock()


def classify(path):
    name = path.name
    if INSPECTION.match(name):
        return "inspection"
    if HISTORY.match(name):
        return "history"
    if STATS.match(name):
        return "stats"
    if ALARM.search(name):
        return "alarm"
    if HEADER.search(name):
        return "header"
    return None


def find_files(root):
    """[(Path, stat, kind)] - 폴더를 한 번 훑는다. 실행 파일·암호화 사본 폴더는 건너뛴다."""
    out, stack = [], [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() not in SKIP_DIRS:
                        stack.append(e.path)
                elif e.is_file():
                    kind = classify(Path(e.name))
                    if kind:
                        out.append((Path(e.path), e.stat(), kind))
            except OSError:
                continue
    out.sort(key=lambda x: (x[2], x[0].name.lower()))
    return out


def _lines(path):
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line:
                yield line


def _num(s):
    try:
        return float(s)
    except ValueError:
        return None


def inspection_rows(path):
    """검사 기록 파일의 행 [date, time, p_no, name, lot, under, pass, over, tare, weight, grade]. 머리글이 다르면 ValueError."""
    rows = []
    it = _lines(path)
    head = next(it, "")
    if head.split(",") != I_HEAD:
        raise ValueError("검사 기록 머리글이 아닙니다 (암호화되었거나 형식이 다름)")
    for line in it:
        f = line.split(",")
        if len(f) < 11:
            continue
        if f == I_HEAD:
            continue
        name = ",".join(f[3:len(f) - 7])   # 품명에 쉼표가 있으면 열이 늘어난다
        lot, under, pas, over, tare, weight, grade = f[len(f) - 7:]
        rows.append([f[0], f[1], f[2], name, lot, _num(under), _num(pas), _num(over), _num(tare), _num(weight), grade])
    return rows


def _summary(path, kind):
    """파일 하나를 읽어 요약한다. 검사 기록은 행을 다 들고 있지 않고 런 요약만 남긴다 (행은 run_detail 로 다시 읽는다)."""
    if kind == "inspection":
        rows = inspection_rows(path)
        weights = [r[9] for r in rows if r[9] is not None and r[10] in ("Pass", "Under", "Over")]
        counts = {g: 0 for g in GRADES}
        other = 0
        for r in rows:
            if r[10] in counts:
                counts[r[10]] += 1
            else:
                other += 1
        ranges = []
        for r in rows:   # 기준(Under/Pass/Over) 이 바뀐 지점
            key = (r[5], r[6], r[7])
            if not ranges or ranges[-1][1:] != key:
                ranges.append((r[1],) + key)
        m = INSPECTION.match(path.name)
        return {
            "rows": len(rows), "date": rows[0][0] if rows else f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}",
            "start": rows[0][1] if rows else "", "end": rows[-1][1] if rows else "",
            "p_no": rows[0][2] if rows else m.group(2),
            "names": sorted({r[3] for r in rows}), "lots": sorted({r[4] for r in rows}),
            "counts": counts, "other": other,
            "mean": statistics.fmean(weights) if weights else None,
            "sd": statistics.stdev(weights) if len(weights) > 1 else None,
            "min": min(weights) if weights else None, "max": max(weights) if weights else None,
            "ranges": ranges,
        }
    if kind == "history":
        it = _lines(path)
        if next(it, "").split(",") != H_HEAD:
            raise ValueError("이력 머리글이 아닙니다")
        return {"lines": [ln.split(",") for ln in it]}
    if kind == "stats":
        return {"lines": [ln.split(",") for ln in _lines(path)]}
    if kind == "alarm":
        it = _lines(path)
        next(it, "")
        return {"lines": [ln.split(",", 3) for ln in it]}
    if kind == "header":
        info = {}
        for ln in _lines(path):
            if "=" in ln:
                k, v = ln.split("=", 1)
                info[k.strip()] = v.strip()
        return {"info": info}
    raise ValueError(kind)


def _read(path, stat, kind):
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is None:
        hit = _summary(path, kind)
        with _cache_lock:
            _cache[key] = hit
    return hit


def _split_history(f, known):
    """이력 한 줄 → [date, time, user, event, detail, before, after, type, 추정 여부]."""
    if len(f) < 8:
        f = f + [""] * (8 - len(f))
    mid, guessed = f[5:-1], False
    if len(mid) == 2:
        before, after = mid
    else:   # 변경 전/후 값에 쉼표가 있다: 앞서 나온 값과 맞는 쪽으로 나눈다
        guessed = True
        before, after = mid[0], ",".join(mid[1:])
        for k in range(1, len(mid)):
            if ",".join(mid[:k]) in known:
                before, after = ",".join(mid[:k]), ",".join(mid[k:])
                break
    if after:
        known.add(after)
    return [f[0], f[1], f[2], f[3], f[4], before, after, f[-1], int(guessed)]


def scan(root):
    """root 아래 중량선별기 데이터를 읽어 화면용 dict 로."""
    root = Path(root)
    files, runs, history, stats, alarms, header = [], [], [], [], [], {}
    known = set()
    for path, st, kind in find_files(root):
        fi = len(files)
        rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        entry = [rel, kind, st.st_size, datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"), 0, "OK"]
        files.append(entry)
        try:
            s = _read(path, st, kind)
        except Exception as e:  # 읽지 못한 파일은 목록에 이유를 남기고 계속한다
            entry[5] = f"{type(e).__name__}: {e}"
            continue
        if kind == "inspection":
            c = s["counts"]
            ng = c["Under"] + c["Over"] + c["Double"]
            r = lambda v: None if v is None else round(v, 3)
            runs.append([fi, s["date"], s["start"], s["end"], s["p_no"], " / ".join(s["names"]), " / ".join(s["lots"]),
                         s["rows"], c["Pass"], c["Under"], c["Over"], c["Double"], s["other"],
                         round(100 * ng / s["rows"], 2) if s["rows"] else 0,
                         r(s["mean"]), r(s["sd"]), s["min"], s["max"], len(s["ranges"]) - 1 if s["ranges"] else 0,
                         list(s["ranges"][-1][1:]) if s["ranges"] else [],
                         list(s["ranges"][0][1:]) if s["ranges"] else []])   # 마지막 기준, 처음 기준
            entry[4] = s["rows"]
        elif kind == "history":
            for f in s["lines"]:
                history.append(_split_history(f, known) + [fi])
            entry[4] = len(s["lines"])
        elif kind == "stats":
            stats.extend([fi] + f for f in s["lines"])
            entry[4] = len(s["lines"])
        elif kind == "alarm":
            alarms.extend([fi] + f for f in s["lines"])
            entry[4] = len(s["lines"])
        elif kind == "header":
            header.update(s["info"])
            entry[4] = len(s["info"])
    runs.sort(key=lambda x: (x[1], x[2]))
    history.sort(key=lambda x: (x[0], x[1]))
    dates = [r[1] for r in runs] + [h[0] for h in history]
    return {
        "root": str(root), "header": header, "files": files, "runs": runs, "history": history, "stats": stats,
        "alarms": alarms, "kind_names": KIND_NAMES,
        "period": [min(dates), max(dates)] if dates else [],
    }


def run_detail(root, rel):
    """런 하나의 행: [time, weight, grade, under, pass, over, tare]. rel 은 scan 의 files[i][0]."""
    path = Path(root) / rel
    if INSPECTION.match(path.name) is None:
        raise ValueError("검사 기록 파일이 아닙니다")
    return [[r[1], r[9], r[10], r[5], r[6], r[7], r[8]] for r in inspection_rows(path)]
