"""NG 이미지 꺼내기 (뷰어의 /img).

NG 리포트는 1,000쪽(수백 MB)이 넘기도 해서, 이미지마다 PDF 를 새로 열면 한 장에 0.7초씩 걸린다.
최근에 연 PDF 몇 개는 열어 둔 채로 쓰고, 한 PDF 는 한 번에 한 요청만 쓴다.
(pdf.py 와 따로 둔다. pdf.py 를 고치면 읽기 캐시가 모두 무효가 된다.)
"""

import io
import threading
from collections import OrderedDict

import pdfplumber

OPEN_DOCS_MAX = 4

_docs = OrderedDict()   # (경로, 수정 시각) → [PDF, Lock]
_docs_lock = threading.Lock()


def _doc(path, mtime):
    key = (str(path), mtime)
    with _docs_lock:
        if key in _docs:
            _docs.move_to_end(key)
            return _docs[key]
        entry = _docs[key] = [pdfplumber.open(path), threading.Lock()]
        while len(_docs) > OPEN_DOCS_MAX:
            _, (old, lock) = _docs.popitem(last=False)
            with lock:
                old.close()
        return entry


def ng_image(path, mtime, page_no, index):
    """NG 이미지 한 장 (bytes, content-type). PDF 안의 JPEG 는 그대로, 그 외는 PNG 로 그린다."""
    pdf, lock = _doc(path, mtime)
    with lock:
        page = pdf.pages[page_no - 1]
        try:
            im = page.images[index]
            stream = im["stream"]
            filt = stream.get("Filter")
            filters = filt if isinstance(filt, list) else [filt]
            if [getattr(f, "name", str(f)).lstrip("/") for f in filters] == ["DCTDecode"]:
                return stream.get_rawdata(), "image/jpeg"
            buf = io.BytesIO()
            page.crop((im["x0"], im["top"], im["x1"], im["bottom"])).to_image(resolution=200).original.save(buf, "PNG")
            return buf.getvalue(), "image/png"
        finally:
            page.close()  # 쪽 해석 결과를 붙잡고 있지 않도록
