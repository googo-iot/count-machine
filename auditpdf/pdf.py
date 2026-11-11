"""계수기(COU-P004 / VIC55 / VIC110 계열) 리포트 PDF 한 건을 읽는다.

리포트 종류 (파일명 'REPORT-<종류>[배치]' 로 구분, scan.KINDS)
    AuditTrailLog  감사 추적       3열 표 TIME | LOG | USER
    Batch          배치 리포트     요약 2열 표 + 생산 결과 6열 표 (# | 시작 | 종료 | 양품 | 불량 | 불량 제품)
    BottleCountLog 병 계수 기록    5열 표 TIME | PASSED | REJECTED | PROD.COUNT | RESULT
    NGImageALL     NG 이미지       JPEG 이미지 + 오른쪽 글자 (시각 / [Ch n], NG 유형)

모든 리포트 1쪽 상단에 2열 메타데이터 표(REPORT TYPE … BATCH STOP TIME)가 있고, 마지막 쪽에 서명란이 있다.
표 머리글과 키는 영문(COU-P004, VIC110)과 한글(VIC55)로 갈린다. 두 표기를 모두 인식하고 영문 표기로 통일한다.

여러 행이 셀 하나로 합쳐져 나오는 표는 단어 좌표로 행을 복원한다. 감사 추적은 글자색(검정 아님)을 알람으로 본다.
"""

import re
import unicodedata
from dataclasses import asdict, dataclass, field

import pdfplumber

ROW_TOLERANCE = 3.0  # 같은 행으로 묶을 y좌표 허용 오차(pt)

META_KEYS = [
    "REPORT TYPE", "PRINTED DATE", "PRINTED USER", "PRINTED USER TITLE", "MACHINE ID",
    "PRODUCT NAME", "BATCH NUMBER", "RECIPE NAME", "BATCH START TIME", "BATCH STOP TIME",
]
SUMMARY_KEYS = [  # 배치 리포트 요약 표
    "BATCH START USER", "BATCH START USER TITLE", "DETAIL COMMENT", "TARGET PRODUCTION BOTTLE COUNT",
    "PASSED BOTTLE COUNT", "REJECTED BOTTLE COUNT", "NG-REJECTED PRODUCT COUNT",
]

# 공백을 빼고 대문자로 맞춘 형태로 비교한다 ('리포트 종류'/'리포트종류' 처럼 띄어쓰기가 리포트마다 다르다).
KEY_ALIASES = {
    "리포트종류": "REPORT TYPE",
    "인쇄한날짜": "PRINTED DATE",
    "인쇄한사용자": "PRINTED USER",
    "인쇄한사용자등급": "PRINTED USER TITLE",
    "장비고유번호": "MACHINE ID",
    "제품명": "PRODUCT NAME",
    "배치번호": "BATCH NUMBER",
    "레시피이름": "RECIPE NAME",
    "배치시작시간": "BATCH START TIME",
    "배치종료시간": "BATCH STOP TIME",
    "배치시작사용자": "BATCH START USER",
    "배치시작사용자등급": "BATCH START USER TITLE",
    "세부설명": "DETAIL COMMENT",
    "목표수량": "TARGET PRODUCTION BOTTLE COUNT",
    "양품[병]": "PASSED BOTTLE COUNT",
    "불량[병]": "REJECTED BOTTLE COUNT",
    "불량[제품]": "NG-REJECTED PRODUCT COUNT",
}

LOG_HEADERS = {("TIME", "LOG", "USER"), ("시간", "로그", "사용자")}
BOTTLE_HEADERS = {("TIME", "PASSED", "REJECTED", "PROD.COUNT", "RESULT"), ("시간", "양품", "불량", "제품수", "결과")}

TIME_PATTERN = re.compile(r"^\d{4}[/-]\d{2}[/-]\d{2}\s+\d{2}:\d{2}:\d{2}$")
TIME_SEARCH = re.compile(r"\d{4}[/-]\d{2}[/-]\d{2}\s+\d{2}:\d{2}:\d{2}")
CHANNEL = re.compile(r"\[\s*Ch\s*(\d+)\s*\]", re.I)
NG_TYPE = re.compile(r"(?:NG\s*Type|불량\s*유형)\s*:\s*(.*)", re.I | re.S)


@dataclass
class LogRow:
    time: str
    log: str
    user: str
    page: int
    line: int
    color: str = ""          # 검정이 아닌 글자색 (#RRGGBB). 있으면 알람.

    @property
    def alarm(self):
        return bool(self.color)


@dataclass
class PdfResult:
    rows: list = field(default_factory=list)      # 감사 추적 LogRow
    meta: dict = field(default_factory=dict)
    pages: int = 0
    bad_time: int = 0        # TIME 형식 이상
    no_user: int = 0         # USER 공란 (감사 추적)
    summary: dict = field(default_factory=dict)   # 배치 리포트 요약 (SUMMARY_KEYS)
    trials: list = field(default_factory=list)    # 배치 생산 결과 [#, 시작, 종료, 양품, 불량, 불량 제품]
    counts: list = field(default_factory=list)    # 병 계수 기록 [시간, 양품, 불량, 제품수, 결과, 쪽]
    ngs: list = field(default_factory=list)       # NG 이미지 [시간, 채널, 유형, 쪽, 쪽 안 이미지 번호]

    @property
    def items(self):
        """리포트의 본문 항목 수."""
        return len(self.rows) + len(self.trials) + len(self.counts) + len(self.ngs)

    def to_json(self):
        d = asdict(self)
        d["rows"] = [[r.time, r.log, r.user, r.page, r.line, r.color] for r in self.rows]
        return d

    @classmethod
    def from_json(cls, d):
        d = dict(d)
        d["rows"] = [LogRow(*r) for r in d["rows"]]
        return cls(**d)


def normalize_text(value):
    """PDF 폰트 인코딩 문제로 들어오는 제어문자(\\x01 등)를 공백으로 바꾸고 정리한다."""
    if value is None:
        return ""
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") and ch not in "\n\t" else ch
                   for ch in str(value))
    return re.sub(r"[ \t]+", " ", text.replace(" ", " ")).strip()


def color_to_hex(color):
    """pdfplumber 의 non_stroking_color 값을 #RRGGBB 로 바꾼다."""
    if color is None:
        return ""
    try:
        if isinstance(color, (int, float)):
            channels = [float(color)] * 3
        else:
            seq = list(color)
            if len(seq) == 1:
                channels = [float(seq[0])] * 3
            elif len(seq) == 3:
                channels = [float(c) for c in seq]
            elif len(seq) == 4:
                c, m, y, k = (float(v) for v in seq)
                channels = [(1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)]
            else:
                return ""
    except (TypeError, ValueError):
        return ""
    rgb = [max(0, min(255, int(round(v * 255 if v <= 1.0 else v)))) for v in channels]
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def is_black(hex_color):
    if not hex_color:
        return True
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return True
    return r < 40 and g < 40 and b < 40


def compact_upper(value):
    return re.sub(r"\s+", "", normalize_text(value)).upper()


def canonical_key(raw):
    """2열 표의 키를 영문 표기로. 한글은 KEY_ALIASES 로, 나머지는 대문자 원문."""
    return KEY_ALIASES.get(compact_upper(raw)) or normalize_text(raw).upper()


def header_of(values, headers):
    n = len(next(iter(headers)))
    return len(values) >= n and tuple(compact_upper(v) for v in values[:n]) in headers


def is_log_header(values):
    return header_of(values, LOG_HEADERS)


def first_row(table):
    extracted = table.extract()
    return [normalize_text(c) for c in extracted[0]] if extracted else []


def classify_tables(page):
    """페이지의 표를 (메타데이터/요약 표, 로그 표 목록, 기타 표 목록) 으로 나눈다. 감사 추적과 진단용."""
    meta, logs, others = None, [], []
    for table in page.find_tables():
        head = first_row(table)
        if len(head) == 3 and is_log_header(head):
            logs.append(table)
        elif len(head) == 2 and canonical_key(head[0]) in META_KEYS:
            meta = table
        else:
            others.append(table)
    return meta, logs, others


def parse_kv_table(table):
    out = {}
    for row in table.extract() or []:
        if len(row) >= 2 and (key := canonical_key(row[0])):
            out[key] = normalize_text(row[1])
    return out


def _column(word, bounds):
    """단어 중심 x좌표가 속한 열. 경계를 벗어나면 가장 가까운 열."""
    center = (float(word["x0"]) + float(word["x1"])) / 2.0
    for i, (left, right) in enumerate(bounds):
        if left - 1.0 <= center <= right + 1.0:
            return i
    dist = [min(abs(center - left), abs(center - right)) for left, right in bounds]
    return dist.index(min(dist))


def grid_lines(page, table, header_row=0, words=None):
    """표의 머리글(header_row) 아래를 눈에 보이는 줄 단위로 [(열별 글자, 글자색)] 로 복원한다."""
    # 열 경계는 머리글 행의 칸에서 잡는다 (위에 합쳐진 제목 칸이 있으면 table.columns 의 첫 열이 표 전체가 된다)
    head_cells = [c for c in table.rows[header_row].cells if c] if len(table.rows) > header_row else []
    bounds = sorted((float(c[0]), float(c[2])) for c in head_cells) or \
        sorted((float(c.bbox[0]), float(c.bbox[2])) for c in table.columns)
    if not bounds:
        return []
    tx0, ttop, tx1, tbottom = table.bbox
    y_start = float(table.rows[header_row].bbox[3]) if len(table.rows) > header_row else float(ttop)
    if words is None:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False, extra_attrs=["non_stroking_color"])
    inside = [w for w in words
              if float(w["x0"]) >= tx0 - 1.0 and float(w["x1"]) <= tx1 + 1.0
              and float(w["top"]) >= y_start - 0.5 and float(w["bottom"]) <= tbottom + 1.0]

    lines = []
    for w in sorted(inside, key=lambda w: (float(w["top"]), float(w["x0"]))):
        if lines and abs(float(w["top"]) - lines[-1][0]) <= ROW_TOLERANCE:
            lines[-1][1].append(w)
        else:
            lines.append((float(w["top"]), [w]))

    out = []
    for _, line in lines:
        buckets = [[] for _ in bounds]
        for w in sorted(line, key=lambda w: float(w["x0"])):
            buckets[_column(w, bounds)].append(w)
        texts = [normalize_text(" ".join(w["text"] for w in b)) for b in buckets]
        if not any(texts):
            continue
        colors = [color_to_hex(w.get("non_stroking_color")) for w in line]
        out.append((texts, next((c for c in colors if c and not is_black(c)), "")))
    return out


def log_rows(page, table, words=None):
    """한 페이지 로그 표의 [(time, log, user, color)]. TIME 없이 LOG 만 있는 줄은 앞 행의 줄바꿈."""
    rows = []
    for texts, color in grid_lines(page, table, 0, words):
        if len(texts) < 3:
            return []
        time_text, log_text, user_text = texts[:3]
        if (compact_upper(time_text), compact_upper(log_text)) in {h[:2] for h in LOG_HEADERS}:
            continue  # 페이지마다 반복되는 머리글 (USER 열이 비어도 인정)
        if not time_text and log_text and rows:
            t, l, u, c = rows[-1]
            rows[-1] = (t, normalize_text(l + " " + log_text), u, c or color)
            continue
        rows.append((time_text, log_text, user_text, color))
    return rows


# --------------------------------------------------------------------------
# 리포트별 본문
# --------------------------------------------------------------------------

def _audit(result, pno, page, tables, words):
    for table in tables:
        if len(first_row(table)) == 3 and is_log_header(first_row(table)):
            for line_no, (t, l, u, c) in enumerate(log_rows(page, table, words), start=1):
                result.bad_time += bool(t and not TIME_PATTERN.match(t))
                result.no_user += not u
                result.rows.append(LogRow(t, l, u, pno, line_no, c))


def _batch(result, pno, page, tables, words):
    for table in tables:
        rows = [[normalize_text(c) for c in r] for r in table.extract() or []]
        head = next((i for i, r in enumerate(rows) if r and r[0] == "#"), None)
        if head is None or len(rows[head]) < 6:
            continue
        for texts, _ in grid_lines(page, table, head, words):
            if texts[0].isdigit():
                result.trials.append(texts[:6])
            elif result.trials:  # 줄바꿈된 셀
                result.trials[-1] = [normalize_text(a + " " + b) for a, b in zip(result.trials[-1], texts[:6])]


def _bottle(result, pno, page, tables, words):
    for table in tables:
        if not header_of(first_row(table), BOTTLE_HEADERS):
            continue
        for texts, _ in grid_lines(page, table, 0, words):
            if header_of(texts, BOTTLE_HEADERS):
                continue
            if not texts[0] and result.counts:  # 결과 글자가 줄바꿈됨
                result.counts[-1][4] = normalize_text(result.counts[-1][4] + " " + texts[4])
                continue
            result.bad_time += not TIME_PATTERN.match(texts[0])
            result.counts.append(texts[:5] + [pno])


def _ng(result, pno, page, tables, words):
    """이미지마다 오른쪽(다음 이미지 전까지)에 있는 글자에서 시각, 채널, NG 유형을 읽는다."""
    images = page.images
    for n, im in enumerate(images):
        right = min([o["x0"] for o in images if o["x0"] > im["x1"] and o["top"] < im["bottom"] and o["bottom"] > im["top"]]
                    + [page.width])
        near = sorted((w for w in words if im["x1"] <= float(w["x0"]) < right
                       and im["top"] - 2 <= float(w["top"]) <= im["bottom"] + 2),
                      key=lambda w: (round(float(w["top"])), float(w["x0"])))
        text = normalize_text(" ".join(w["text"] for w in near))
        t = TIME_SEARCH.search(text)
        if not t:
            continue  # 머리글 로고 등
        ch = CHANNEL.search(text)
        ng = NG_TYPE.search(text)
        result.ngs.append([t.group(0), ch.group(1) if ch else "", normalize_text(ng.group(1)) if ng else "", pno, n])


BODY = {"audit": _audit, "batch": _batch, "bottle": _bottle, "ng": _ng}


def read_pdf(path, kind="audit"):
    result = PdfResult()
    body = BODY[kind]
    with pdfplumber.open(path) as pdf:
        result.pages = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            for table in tables:  # 메타데이터 / 배치 요약 2열 표
                head = first_row(table)
                if len(head) == 2 and canonical_key(head[0]) in META_KEYS + SUMMARY_KEYS:
                    for k, v in parse_kv_table(table).items():
                        if k in SUMMARY_KEYS:
                            result.summary.setdefault(k, v)
                        else:
                            result.meta.setdefault(k, v)
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False,
                                       extra_attrs=["non_stroking_color"])
            body(result, pno, page, tables, words)
    result.meta = {k: result.meta.get(k, "") for k in META_KEYS}
    return result


def ng_image(path, page_no, index):
    """NG 이미지 한 장 (bytes, content-type). PDF 안의 JPEG 는 그대로, 그 외는 PNG 로 그린다."""
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[page_no - 1]
        im = page.images[index]
        stream = im["stream"]
        filt = stream.get("Filter")
        filters = filt if isinstance(filt, list) else [filt]
        if [getattr(f, "name", str(f)).lstrip("/") for f in filters] == ["DCTDecode"]:
            return stream.get_rawdata(), "image/jpeg"
        import io
        buf = io.BytesIO()
        page.crop((im["x0"], im["top"], im["x1"], im["bottom"])).to_image(resolution=200).original.save(buf, "PNG")
        return buf.getvalue(), "image/png"
