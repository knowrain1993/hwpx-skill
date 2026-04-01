#!/usr/bin/env python3
"""
HWPX 양식 복제 도구 (Workflow F)

기존 HWPX 양식을 복사한 뒤 텍스트만 치환하여 새 문서를 생성한다.
원본의 테이블·이미지·스타일을 100% 유지하면서 내용만 교체한다.

3단계 치환:
  Phase 1 — 구문 수준(--map/--replace): 전체 XML에서 긴 문구를 먼저 치환
  Phase 2 — 키워드 수준(--keywords): <hp:t> 태그 내부에서만 남은 키워드를 치환
  Phase L — 장문 삽입(--long-map): lxml로 <hp:p> 복제하여 긴 텍스트를 문단 분할 삽입

사용법:
  분석:    python clone_form.py --analyze sample.hwpx
  복제:    python clone_form.py sample.hwpx output.hwpx --map map.json
  장문:    python clone_form.py sample.hwpx output.hwpx --long-map long.json
  키워드:  python clone_form.py sample.hwpx output.hwpx --map map.json --keywords kw.json
  오타검수: python clone_form.py --check-typos result.hwpx
  CLI:     python clone_form.py sample.hwpx output.hwpx --replace "원본=대체" "A=B"

Import:
  from clone_form import clone, analyze, extract_texts, check_typos
"""

import argparse
import copy
import json
import os
import re
import sys
import zipfile

from lxml import etree

# linesegarray: 한컴오피스가 저장한 "줄 배치 캐시".
# 텍스트를 치환하면 이 캐시가 무효화되어 글자가 겹쳐 보인다.
# 삭제하면 한컴오피스가 파일을 열 때 자동으로 줄 배치를 재계산한다.

# --- Legacy regex (폴백용) ---
_LINESEG_RE = re.compile(r"<(?:hp:)?linesegarray\b[^>]*>.*?</(?:hp:)?linesegarray>", re.DOTALL)


def _remove_linesegarray_regex(xml_text):
    """[Legacy] regex 기반 linesegarray 제거. lxml 실패 시 폴백."""
    return _LINESEG_RE.sub("", xml_text)


# --- lxml 기반 (primary) ---
def _remove_linesegarray_from_p(p_element):
    """lxml: 단일 <hp:p> 요소에서 linesegarray 자식을 제거한다."""
    for child in list(p_element):
        if etree.QName(child.tag).localname == "linesegarray":
            p_element.remove(child)


def _remove_all_linesegarray(root):
    """lxml: XML 트리 전체에서 모든 linesegarray 요소를 제거한다."""
    for elem in list(root.iter()):
        if etree.QName(elem.tag).localname == "linesegarray":
            parent = elem.getparent()
            if parent is not None:
                parent.remove(elem)


def _remove_linesegarray_lxml(xml_bytes):
    """lxml 파싱으로 linesegarray를 정확하게 제거한다.

    Args:
        xml_bytes: UTF-8 인코딩된 XML 바이트열 또는 문자열
    Returns:
        str: linesegarray가 제거된 XML 문자열
    """
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8")
    try:
        tree = etree.fromstring(xml_bytes)
        _remove_all_linesegarray(tree)
        # 원본 인코딩/선언을 최대한 보존하면서 직렬화
        result = etree.tostring(tree, encoding="unicode", xml_declaration=False)
        # XML 선언이 원본에 있었으면 복원
        if xml_bytes.lstrip().startswith(b"<?xml"):
            # 원본 선언 추출
            decl_end = xml_bytes.find(b"?>")
            if decl_end != -1:
                decl = xml_bytes[:decl_end + 2].decode("utf-8")
                result = decl + "\n" + result
        return result
    except etree.XMLSyntaxError:
        # lxml 파싱 실패 시 regex 폴백
        text = xml_bytes.decode("utf-8") if isinstance(xml_bytes, bytes) else xml_bytes
        return _remove_linesegarray_regex(text)


def _remove_linesegarray(xml_text):
    """치환된 XML에서 모든 linesegarray 요소를 제거한다.

    Primary: lxml 파싱 (정확한 요소 단위 삭제)
    Fallback: regex (lxml 파싱 실패 시)
    """
    return _remove_linesegarray_lxml(xml_text)


def extract_texts(hwpx_path):
    """HWPX에서 <hp:t> 태그의 텍스트를 모두 추출한다.

    Returns:
        list[str]: 고유 텍스트 목록 (등장 순서 유지)
    """
    texts = []
    seen = set()

    with zipfile.ZipFile(hwpx_path, "r") as zf:
        for name in zf.namelist():
            if name.startswith("Contents/") and name.endswith(".xml"):
                data = zf.read(name).decode("utf-8")
                for m in re.finditer(r"<hp:t>(.*?)</hp:t>", data, re.DOTALL):
                    # 인라인 XML 태그 제거하여 순수 텍스트 추출
                    raw = m.group(1)
                    clean = re.sub(r"<[^>]+>", "", raw).strip()
                    if clean and clean not in seen:
                        seen.add(clean)
                        texts.append(clean)
    return texts


def analyze(hwpx_path):
    """HWPX 양식을 분석하여 구조 요약과 텍스트 목록을 출력한다."""
    print(f"=== HWPX 양식 분석: {hwpx_path} ===\n")

    with zipfile.ZipFile(hwpx_path, "r") as zf:
        names = zf.namelist()
        print(f"ZIP 엔트리: {len(names)}개")

        # BinData 수
        bindata = [n for n in names if n.startswith("BinData/")]
        print(f"BinData (이미지 등): {len(bindata)}개")

        # section0.xml 분석
        if "Contents/section0.xml" in names:
            sec = zf.read("Contents/section0.xml").decode("utf-8")
            tables = len(re.findall(r"<hp:tbl ", sec))
            pics = len(re.findall(r"<hp:pic ", sec))
            paras = len(re.findall(r"<hp:p ", sec))
            runs = len(re.findall(r"<hp:run ", sec))
            print(f"문단: {paras}개, 런: {runs}개, 테이블: {tables}개, 이미지: {pics}개")
            print(f"section0.xml 크기: {len(sec):,} bytes")

    # 텍스트 추출
    texts = extract_texts(hwpx_path)
    print(f"\n고유 텍스트 조각: {len(texts)}개\n")
    for i, t in enumerate(texts, 1):
        display = t[:80] + "..." if len(t) > 80 else t
        print(f"  [{i:3d}] {display}")

    return texts


def auto_analyze(hwpx_path, output_json=None):
    """양식을 분석하고 치환 맵 템플릿을 JSON으로 출력한다.

    에이전트가 이 출력을 기반으로 치환 맵을 작성할 수 있도록
    원본 텍스트를 key로, 빈 문자열을 value로 하는 JSON을 생성한다.

    Args:
        hwpx_path: 분석할 .hwpx 파일
        output_json: 출력 JSON 경로 (None이면 stdout)

    Returns:
        dict: {structure: {...}, texts: [...], template: {...}}
    """
    structure = {}
    with zipfile.ZipFile(hwpx_path, "r") as zf:
        names = zf.namelist()
        bindata = [n for n in names if n.startswith("BinData/")]
        structure["zip_entries"] = len(names)
        structure["bindata_count"] = len(bindata)

        if "Contents/section0.xml" in names:
            sec = zf.read("Contents/section0.xml").decode("utf-8")
            structure["tables"] = len(re.findall(r"<hp:tbl ", sec))
            structure["images"] = len(re.findall(r"<hp:pic ", sec))
            structure["paragraphs"] = len(re.findall(r"<hp:p ", sec))
            structure["runs"] = len(re.findall(r"<hp:run ", sec))
            structure["section_size"] = len(sec)

    texts = extract_texts(hwpx_path)

    # 워크플로우 추천
    has_tables = structure.get("tables", 0) > 0
    has_images = structure.get("images", 0) > 0
    if has_tables or has_images:
        recommendation = "Workflow F (clone_form.py) — 테이블/이미지 포함, 양식 복제 필수"
    else:
        recommendation = "Workflow C 또는 F 가능 — 단순 텍스트 문서"

    # 치환 맵 템플릿 생성
    template = {}
    for t in texts:
        if len(t) > 1:  # 1글자 이하 건너뜀
            template[t] = ""

    result = {
        "source": hwpx_path,
        "structure": structure,
        "recommendation": recommendation,
        "text_count": len(texts),
        "template_map": template,
    }

    output = json.dumps(result, ensure_ascii=False, indent=2)

    if output_json:
        with open(output_json, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"자동 분석 완료: {output_json}")
        print(f"  구조: 테이블 {structure.get('tables', 0)}개, "
              f"이미지 {structure.get('images', 0)}개, "
              f"문단 {structure.get('paragraphs', 0)}개")
        print(f"  텍스트 조각: {len(texts)}개")
        print(f"  추천: {recommendation}")
    else:
        print(output)

    return result


# ---------------------------------------------------------------------------
# Phase L: lxml 기반 장문 삽입 (문단 분할)
# ---------------------------------------------------------------------------

def _find_p_containing_text(root, target_text):
    """<hp:t> 안에 target_text를 포함하는 <hp:p> 요소를 찾는다.

    Returns:
        list[Element]: 매칭된 <hp:p> 요소 목록
    """
    results = []
    target_stripped = target_text.strip()
    for elem in root.iter():
        if etree.QName(elem.tag).localname == "t" and elem.text:
            if target_stripped in elem.text.strip():
                # run -> p 순서로 부모 탐색
                run = elem.getparent()
                if run is not None:
                    p = run.getparent()
                    if p is not None and etree.QName(p.tag).localname == "p":
                        results.append((p, elem))
    return results


def _get_section_range(parent, title_p):
    """섹션 제목 <hp:p> 다음부터 그 다음 섹션 제목(또는 끝)까지의 인덱스 범위를 반환한다.

    다음 섹션 제목 판별: 텍스트가 숫자.숫자 패턴으로 시작하거나,
    '〈', '2.', '3.' 등 구조적 제목 패턴인 경우.
    """
    children = list(parent)
    title_idx = children.index(title_p)
    end_idx = len(children)

    for i in range(title_idx + 1, len(children)):
        child = children[i]
        for t in child.iter():
            if etree.QName(t.tag).localname == "t" and t.text:
                txt = t.text.strip()
                # 다음 섹션 제목 패턴
                if (re.match(r"^\d+\.\d+\s", txt) or
                        re.match(r"^[2-9]\.\s", txt) or
                        txt.startswith("〈")):
                    end_idx = i
                    return title_idx, end_idx
    return title_idx, end_idx


def _insert_long_text(root, target_text, paragraphs, after_section_title=None):
    """target_text를 포함하는 <hp:p>를 찾아 paragraphs 목록으로 교체한다.

    양식의 <hp:p> 스타일(paraPrIDRef, charPrIDRef)을 그대로 복제하므로
    자간·문단간격이 보존된다. 각 복제된 문단의 linesegarray는 자동 제거.

    Args:
        root: lxml Element (section XML root)
        target_text: 찾을 플레이스홀더 텍스트 (예: "ㆍ")
        paragraphs: 삽입할 텍스트 리스트 (각 항목이 하나의 <hp:p>가 됨)
        after_section_title: 이 섹션 제목 다음에 나오는 target만 매칭
                             (예: "1.1 추진 배경" → 이 제목 다음의 ㆍ만 교체)

    Returns:
        int: 삽입된 문단 수
    """
    if not paragraphs:
        return 0

    matches = _find_p_containing_text(root, target_text)

    if after_section_title:
        title_matches = _find_p_containing_text(root, after_section_title)
        if not title_matches:
            return 0
        title_p = title_matches[0][0]
        parent = title_p.getparent()
        if parent is None:
            return 0

        # 이 섹션의 범위를 정확히 계산
        range_start, range_end = _get_section_range(parent, title_p)

        children = list(parent)
        filtered = []
        for p_elem, t_elem in matches:
            if p_elem.getparent() is parent:
                try:
                    p_idx = children.index(p_elem)
                    if range_start < p_idx < range_end:
                        filtered.append((p_elem, t_elem))
                except ValueError:
                    pass
        matches = filtered

    if not matches:
        return 0

    # 사용할 매칭 수 = min(플레이스홀더 수, 삽입할 문단 수)
    use_count = min(len(matches), len(paragraphs))

    # 1:1 교체 — 각 플레이스홀더에 대응하는 문단 텍스트로 교체
    for i in range(use_count):
        p_elem, t_elem = matches[i]
        t_elem.text = "  " + paragraphs[i] + " "
        _remove_linesegarray_from_p(p_elem)

    # 문단이 플레이스홀더보다 많으면 → 마지막 매칭 <hp:p>를 복제하여 추가
    if len(paragraphs) > use_count:
        last_p, last_t = matches[use_count - 1]
        parent = last_p.getparent()
        last_idx = list(parent).index(last_p)

        for extra_i, txt in enumerate(paragraphs[use_count:], 1):
            new_p = copy.deepcopy(last_p)
            for t in new_p.iter():
                if etree.QName(t.tag).localname == "t":
                    t.text = "  " + txt + " "
                    break
            _remove_linesegarray_from_p(new_p)

            # 불릿+빈줄 패턴: 빈줄 다음에 삽입
            insert_at = last_idx + (extra_i * 2)
            children = list(parent)
            if insert_at > len(children):
                parent.append(new_p)
            else:
                parent.insert(insert_at, new_p)

            # 빈 간격 문단 복제 삽입
            spacer_src_idx = last_idx + 1
            children = list(parent)
            if spacer_src_idx < len(children):
                spacer_src = children[spacer_src_idx]
                has_text = any(
                    etree.QName(t.tag).localname == "t" and t.text and t.text.strip()
                    for t in spacer_src.iter()
                )
                if not has_text:
                    spacer = copy.deepcopy(spacer_src)
                    _remove_linesegarray_from_p(spacer)
                    spacer_at = insert_at + 1
                    children = list(parent)
                    if spacer_at > len(children):
                        parent.append(spacer)
                    else:
                        parent.insert(spacer_at, spacer)

    # 플레이스홀더가 문단보다 많으면 → 남은 플레이스홀더 + 빈줄 제거
    for p_elem, _ in matches[use_count:]:
        p_parent = p_elem.getparent()
        if p_parent is not None:
            p_idx = list(p_parent).index(p_elem)
            p_parent.remove(p_elem)
            # 바로 다음이 빈 문단이면 함께 제거
            children = list(p_parent)
            if p_idx < len(children):
                next_elem = children[p_idx]
                has_text = any(
                    etree.QName(t.tag).localname == "t" and t.text and t.text.strip()
                    for t in next_elem.iter()
                )
                if not has_text and etree.QName(next_elem.tag).localname == "p":
                    p_parent.remove(next_elem)

    return len(paragraphs)


def _apply_long_map(xml_bytes, long_map):
    """Phase L: 장문 치환 맵을 lxml로 적용한다.

    long_map 형식:
    {
        "section_title": {               # 섹션 제목 (위치 특정용)
            "placeholder": "ㆍ",          # 교체 대상 텍스트
            "paragraphs": ["문단1", "문단2", ...]  # 삽입할 텍스트 목록
        },
        ...
    }

    또는 간단 형식:
    {
        "section_title": ["문단1", "문단2", ...]  # placeholder 기본값 "ㆍ"
    }
    """
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8")

    # XML 선언 보존
    xml_decl = ""
    if xml_bytes.lstrip().startswith(b"<?xml"):
        decl_end = xml_bytes.find(b"?>")
        if decl_end != -1:
            xml_decl = xml_bytes[:decl_end + 2].decode("utf-8") + "\n"

    root = etree.fromstring(xml_bytes)
    total_inserted = 0

    for section_title, config in long_map.items():
        if isinstance(config, list):
            placeholder = "\u318d"  # ㆍ (기본값)
            paragraphs = config
        else:
            placeholder = config.get("placeholder", "\u318d")
            paragraphs = config.get("paragraphs", [])

        count = _insert_long_text(root, placeholder, paragraphs,
                                  after_section_title=section_title)
        if count > 0:
            print(f"  Phase L: [{section_title}] {count}개 문단 삽입")
        total_inserted += count

    # 전체 linesegarray 최종 정리
    _remove_all_linesegarray(root)

    result = etree.tostring(root, encoding="unicode", xml_declaration=False)
    return xml_decl + result, total_inserted


# ---------------------------------------------------------------------------
# 오타 검수
# ---------------------------------------------------------------------------

# 흔한 한국어 오타 패턴
_TYPO_PATTERNS = [
    # (regex, 설명, 수정제안)
    (r"되어[ ]?지", "이중피동 '되어지'", "되"),
    (r"할[ ]?수[ ]?있는[ ]?것[ ]?으로", "장황한 표현", "할 수 있음"),
    (r"됬", "'됬' → '됐'", "됐"),
    (r", ,", "쉼표 중복", ","),
    (r"\.{4,}", "마침표 과다", "..."),
    (r"  {2,}", "공백 과다 (3개 이상)", "  "),
    (r"([가-힣])\1{3,}", "같은 글자 4회 이상 반복", None),
    (r"임 \.", "'임 .' 띄어쓰기 오류", "임."),
    (r"함 \.", "'함 .' 띄어쓰기 오류", "함."),
    (r"있슴", "'있슴' → '있음'", "있음"),
    (r"없슴", "'없슴' → '없음'", "없음"),
    (r"몇몇의", "'몇몇의' → '몇몇'", "몇몇"),
]

# 공문서 금지 표현 (glossary.md 기준)
_FORBIDDEN_TERMS = {
    "장표": "슬라이드",
    "발주처": "발주기관",
    "산출물": "결과물",
    "성과지표": "핵심성과",
    "용역사": "수행사",
    "세컨브레인": "지식관리",
}


def check_typos(hwpx_path):
    """HWPX 결과물의 텍스트를 추출하여 오타·금지표현을 검수한다.

    Returns:
        dict: {issues: [...], forbidden: [...], total_issues: int}
    """
    texts = extract_texts(hwpx_path)
    full_text = " ".join(texts)

    issues = []
    forbidden = []

    # 오타 패턴 검사
    for pattern, desc, suggestion in _TYPO_PATTERNS:
        for m in re.finditer(pattern, full_text):
            context_start = max(0, m.start() - 15)
            context_end = min(len(full_text), m.end() + 15)
            context = full_text[context_start:context_end]
            issues.append({
                "type": "typo",
                "match": m.group(),
                "description": desc,
                "suggestion": suggestion,
                "context": f"...{context}...",
            })

    # 금지 표현 검사
    for old, new in _FORBIDDEN_TERMS.items():
        for m in re.finditer(re.escape(old), full_text):
            context_start = max(0, m.start() - 15)
            context_end = min(len(full_text), m.end() + 15)
            context = full_text[context_start:context_end]
            forbidden.append({
                "type": "forbidden",
                "match": old,
                "suggestion": new,
                "context": f"...{context}...",
            })

    total = len(issues) + len(forbidden)

    print(f"\n=== 오타 검수 ===")
    print(f"검사 텍스트: {len(texts)}개 조각, {len(full_text):,}자")
    if issues:
        print(f"\n오타/문체 이슈 {len(issues)}건:")
        for iss in issues:
            fix = f" → '{iss['suggestion']}'" if iss["suggestion"] else ""
            print(f"  - [{iss['description']}] '{iss['match']}'{fix}")
            print(f"    {iss['context']}")
    if forbidden:
        print(f"\n금지 표현 {len(forbidden)}건:")
        for fb in forbidden:
            print(f"  - '{fb['match']}' → '{fb['suggestion']}'")
            print(f"    {fb['context']}")
    if total == 0:
        print("  이슈 없음")

    return {"issues": issues, "forbidden": forbidden, "total_issues": total}


def _prepare_keywords(keywords):
    """키워드를 길이 내림차순으로 정렬한다 (긴 것이 먼저 매칭되도록)."""
    return sorted(keywords.items(), key=lambda x: len(x[0]), reverse=True)


def _apply_keywords_to_text(text, sorted_keywords):
    """순수 텍스트에 키워드 치환을 적용한다."""
    for old, new in sorted_keywords:
        if old in text:
            text = text.replace(old, new)
    return text


def _apply_keywords_in_xml(xml_text, sorted_keywords):
    """<hp:t> 태그 내부의 텍스트에만 키워드 치환을 적용한다.

    인라인 XML 요소(<hp:fwSpace/>, <hp:tab/> 등)가 키워드를
    분리하는 경우를 처리하기 위해 태그 경계에서 텍스트를 분할하여
    각 조각에 개별적으로 치환을 적용한다.
    """
    def replace_in_t(match):
        inner = match.group(1)
        # 인라인 XML 태그로 분할
        parts = re.split(r"(<[^>]+>)", inner)
        result = []
        for part in parts:
            if part.startswith("<"):
                # XML 태그는 그대로 유지
                result.append(part)
            else:
                # 텍스트 부분에만 키워드 치환 적용
                result.append(_apply_keywords_to_text(part, sorted_keywords))
        return "<hp:t>" + "".join(result) + "</hp:t>"

    return re.sub(r"<hp:t>(.*?)</hp:t>", replace_in_t, xml_text, flags=re.DOTALL)


def clone(src_path, dst_path, replacements=None, keywords=None,
          long_map=None, title=None, creator=None):
    """HWPX 양식을 복제하고 텍스트를 치환한다.

    Args:
        src_path: 원본 .hwpx 파일 경로
        dst_path: 출력 .hwpx 파일 경로
        replacements: Phase 1 구문 치환 dict (old → new)
        keywords: Phase 2 키워드 치환 dict (old → new), <hp:t> 내부에서만 적용
        long_map: Phase L 장문 삽입 dict (섹션제목 → 문단 리스트)
        title: 문서 제목 (메타데이터)
        creator: 작성자 (메타데이터)
    """
    replacements = replacements or {}
    sorted_keywords = _prepare_keywords(keywords) if keywords else []

    tmp_path = dst_path + ".tmp"

    with zipfile.ZipFile(src_path, "r") as zin:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)

                if item.filename.startswith("Contents/") and item.filename.endswith(".xml"):
                    text = data.decode("utf-8")

                    # Phase 1: 구문 수준 치환 (전체 XML)
                    for old, new in replacements.items():
                        text = text.replace(old, new)

                    # Phase 2: 키워드 수준 치환 (<hp:t> 내부만)
                    if sorted_keywords:
                        text = _apply_keywords_in_xml(text, sorted_keywords)

                    # Phase L: 장문 삽입 (lxml 문단 분할)
                    if long_map and item.filename.startswith("Contents/section"):
                        text, count = _apply_long_map(text, long_map)
                        if count > 0:
                            print(f"  Phase L 합계: {count}개 문단 삽입 ({item.filename})")

                    # Phase 3: linesegarray 제거 (텍스트 치환 후 줄 배치 캐시 무효화 방지)
                    if replacements or sorted_keywords:
                        text = _remove_linesegarray(text)

                    # 메타데이터 치환 (content.hpf의 제목/작성자)
                    if item.filename == "Contents/content.hpf":
                        if title:
                            text = re.sub(
                                r"(<dc:title>).*?(</dc:title>)",
                                rf"\1{title}\2",
                                text,
                            )
                        if creator:
                            text = re.sub(
                                r"(<dc:creator>).*?(</dc:creator>)",
                                rf"\1{creator}\2",
                                text,
                            )

                    data = text.encode("utf-8")

                # mimetype은 반드시 ZIP_STORED
                if item.filename == "mimetype":
                    zout.writestr(item, data, compress_type=zipfile.ZIP_STORED)
                else:
                    zout.writestr(item, data)

    os.replace(tmp_path, dst_path)


def validate_result(src_path, dst_path, replacements=None, keywords=None):
    """치환 결과를 검증하고 남은 원본 키워드를 보고한다.

    Returns:
        dict: {total_originals, replaced, remaining, remaining_texts, coverage_pct}
    """
    # 원본 텍스트 추출
    orig_texts = extract_texts(src_path)
    # 결과 텍스트 추출
    result_texts = extract_texts(dst_path)

    all_old_terms = set()
    if replacements:
        all_old_terms.update(replacements.keys())
    if keywords:
        all_old_terms.update(keywords.keys())

    # 결과에서 원본 키워드가 남아있는지 확인
    remaining = []
    result_full = " ".join(result_texts)
    for term in sorted(all_old_terms, key=len, reverse=True):
        if term in result_full:
            remaining.append(term)

    total = len(orig_texts)
    replaced = total - len(remaining)
    coverage = (1 - len(remaining) / max(total, 1)) * 100

    print(f"\n=== 치환 검증 ===")
    print(f"원본 텍스트 조각: {total}개")
    print(f"치환 완료: {replaced}개")
    print(f"미치환 키워드: {len(remaining)}개")
    print(f"커버리지: {coverage:.1f}%")

    if remaining:
        print(f"\n미치환 키워드:")
        for r in remaining[:20]:
            display = r[:60] + "..." if len(r) > 60 else r
            print(f"  - {display}")
        if len(remaining) > 20:
            print(f"  ... 외 {len(remaining) - 20}개")

    return {
        "total_originals": total,
        "replaced": replaced,
        "remaining": len(remaining),
        "remaining_texts": remaining,
        "coverage_pct": coverage,
    }


def main():
    parser = argparse.ArgumentParser(
        description="HWPX 양식 복제 도구 (Workflow F)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 양식 분석
  python clone_form.py --analyze sample.hwpx

  # JSON 맵으로 복제
  python clone_form.py sample.hwpx output.hwpx --map replacements.json

  # 키워드 폴백 추가
  python clone_form.py sample.hwpx output.hwpx --map map.json --keywords kw.json

  # CLI 직접 치환
  python clone_form.py sample.hwpx output.hwpx --replace "원본=대체" "A=B"
""",
    )
    parser.add_argument("source", help="원본 HWPX 파일")
    parser.add_argument("output", nargs="?", help="출력 HWPX 파일")
    parser.add_argument("--analyze", action="store_true", help="양식 분석 모드")
    parser.add_argument("--auto-analyze", metavar="JSON", help="자동 분석 + 치환 맵 템플릿 JSON 출력")
    parser.add_argument("--map", help="구문 치환 JSON 파일 (Phase 1)")
    parser.add_argument("--keywords", help="키워드 치환 JSON 파일 (Phase 2)")
    parser.add_argument("--long-map", help="장문 삽입 JSON 파일 (Phase L)")
    parser.add_argument("--replace", nargs="*", help="CLI 치환 쌍 (old=new)")
    parser.add_argument("--title", help="문서 제목 메타데이터")
    parser.add_argument("--creator", help="작성자 메타데이터")
    parser.add_argument("--validate", action="store_true", help="치환 후 검증 실행")
    parser.add_argument("--check-typos", action="store_true",
                        help="결과 HWPX 오타 검수 (출력 파일 또는 source 대상)")

    args = parser.parse_args()

    if not os.path.exists(args.source):
        print(f"Error: 파일을 찾을 수 없음: {args.source}")
        sys.exit(1)

    # 분석 모드
    if args.analyze:
        analyze(args.source)
        return

    # 자동 분석 모드
    if args.auto_analyze:
        auto_analyze(args.source, args.auto_analyze)
        return

    # 오타 검수 전용 모드 (출력 없이 source만 검수)
    if args.check_typos and not args.output:
        check_typos(args.source)
        return

    # 복제 모드
    if not args.output:
        print("Error: 출력 파일을 지정하세요.")
        sys.exit(1)

    # 치환 맵 구성
    replacements = {}
    if args.map:
        with open(args.map, "r", encoding="utf-8") as f:
            replacements = json.load(f)
        print(f"구문 치환 맵: {len(replacements)}개 항목 ({args.map})")

    if args.replace:
        for pair in args.replace:
            if "=" not in pair:
                print(f"Warning: 잘못된 치환 쌍 무시: {pair}")
                continue
            old, new = pair.split("=", 1)
            replacements[old] = new
        print(f"CLI 치환: {len(args.replace)}개 추가")

    keywords = None
    if args.keywords:
        with open(args.keywords, "r", encoding="utf-8") as f:
            keywords = json.load(f)
        print(f"키워드 폴백 맵: {len(keywords)}개 항목 ({args.keywords})")

    long_map = None
    if args.long_map:
        with open(args.long_map, "r", encoding="utf-8") as f:
            long_map = json.load(f)
        print(f"장문 삽입 맵: {len(long_map)}개 섹션 ({args.long_map})")

    # 복제 실행
    clone(args.source, args.output, replacements, keywords,
          long_map=long_map, title=args.title, creator=args.creator)
    print(f"복제 완료: {args.output}")

    # 검증
    if args.validate:
        validate_result(args.source, args.output, replacements, keywords)

    # 오타 검수
    if args.check_typos:
        check_typos(args.output)


if __name__ == "__main__":
    main()
