#!/usr/bin/env python3
"""
HWPX 후처리: 오타 검수 + 줄간격 정합성 검증/보정

clone_form.py로 양식을 채운 뒤, 최종 품질을 보장하는 후처리 스크립트.

사용법:
  # 오타 검수만
  python postprocess.py --typos result.hwpx

  # 줄간격 검증만
  python postprocess.py --spacing result.hwpx --source original.hwpx

  # 전체 (오타 + 줄간격 + 자동 보정)
  python postprocess.py --all result.hwpx --source original.hwpx

  # 줄간격 자동 보정 적용
  python postprocess.py --spacing result.hwpx --source original.hwpx --fix
"""
import argparse
import os
import re
import sys
import zipfile
from pathlib import Path

from lxml import etree

# clone_form.py에서 기존 기능 import
sys.path.insert(0, str(Path(__file__).parent))
from clone_form import check_typos, extract_texts


# ─────────────────────────────────────────────────────────
# 1. 줄간격 분석
# ─────────────────────────────────────────────────────────

def _parse_spacing_from_header(header_bytes):
    """header.xml에서 paraPr별 lineSpacing과 spacing 정보를 추출한다.

    Returns:
        dict: {paraPr_id: {lineSpacing_type, lineSpacing_value, before, after}}
    """
    root = etree.fromstring(header_bytes)
    result = {}

    for elem in root.iter():
        if etree.QName(elem.tag).localname == "paraPr":
            pr_id = elem.get("id")
            if pr_id is None:
                continue
            info = {}

            for child in elem.iter():
                local = etree.QName(child.tag).localname
                if local == "lineSpacing":
                    info["lineSpacing_type"] = child.get("type", "")
                    info["lineSpacing_value"] = child.get("value", "")
                elif local == "spacing":
                    info["before"] = child.get("before", "0")
                    info["after"] = child.get("after", "0")

            result[pr_id] = info

    return result


def _is_inside_table(elem):
    """요소가 테이블 셀(subList) 내부에 있는지 판별한다."""
    parent = elem.getparent()
    while parent is not None:
        local = etree.QName(parent.tag).localname
        if local in ("tc", "subList"):
            return True
        if local == "sec":
            return False
        parent = parent.getparent()
    return False


def _analyze_section_spacing(section_bytes, header_info):
    """section0.xml의 **본문 영역** 문단 간격 패턴을 분석한다.

    테이블 셀 내부의 문단은 제외한다 (셀은 자체 간격 규칙이 있음).

    Returns:
        list[dict]: 각 문단의 {index, paraPrIDRef, lineSpacing, has_text, text_preview, is_spacer, in_table}
    """
    root = etree.fromstring(section_bytes)
    paragraphs = []

    for i, elem in enumerate(root.iter()):
        if etree.QName(elem.tag).localname != "p":
            continue

        in_table = _is_inside_table(elem)
        pr_ref = elem.get("paraPrIDRef", "0")

        # 텍스트 추출
        texts = []
        for t in elem.iter():
            if etree.QName(t.tag).localname == "t" and t.text:
                clean = t.text.strip()
                if clean:
                    texts.append(clean)

        has_text = len(texts) > 0
        text_preview = " ".join(texts)[:60] if texts else ""

        # header에서 간격 정보 조회
        spacing_info = header_info.get(pr_ref, {})

        paragraphs.append({
            "index": i,
            "paraPrIDRef": pr_ref,
            "lineSpacing_type": spacing_info.get("lineSpacing_type", "?"),
            "lineSpacing_value": spacing_info.get("lineSpacing_value", "?"),
            "before": spacing_info.get("before", "0"),
            "after": spacing_info.get("after", "0"),
            "has_text": has_text,
            "text_preview": text_preview,
            "is_spacer": not has_text,
            "in_table": in_table,
        })

    return paragraphs


def check_spacing(hwpx_path, source_path=None):
    """HWPX 문서의 줄간격 정합성을 검증한다.

    검증 항목:
    1. 내용 문단의 lineSpacing 일관성 (같은 섹션 내)
    2. 내용 문단 사이 빈줄(스페이서) 존재 여부
    3. (원본 비교 시) 원본과 간격 속성 일치 여부

    Returns:
        dict: {issues: [...], summary: {...}}
    """
    with zipfile.ZipFile(hwpx_path, "r") as zf:
        sec_data = zf.read("Contents/section0.xml")
        header_data = zf.read("Contents/header.xml")

    header_info = _parse_spacing_from_header(header_data)
    paras = _analyze_section_spacing(sec_data, header_info)

    issues = []

    # 본문 문단만 필터 (테이블 셀 내부 제외)
    body_paras = [p for p in paras if not p["in_table"]]

    # 레이아웃 요소 패턴 (빈줄 없는 것이 정상)
    _LAYOUT_PATTERNS = re.compile(
        r"^(CHAPTER|■|0[1-9]$|\d\.\s|추진 계획|세부 계획|사업 관리|"
        r"업체 일반|배경 및|사업 개요|∙ 예시|예시 이미지|"
        r"Andone|AI Startup|수행 범위|수행목표|추진 일정|"
        r"참여 인력|사업 관리계획|계획 개요)"
    )

    # 1. 본문 내용 문단 사이 빈줄 검증 (레이아웃 요소 제외)
    missing_spacers = 0
    consecutive_content = 0
    for i, p in enumerate(body_paras):
        if p["has_text"]:
            # 레이아웃 요소는 연속 카운트에서 제외
            if _LAYOUT_PATTERNS.search(p["text_preview"]):
                consecutive_content = 0
                continue
            consecutive_content += 1
            if consecutive_content > 1:
                missing_spacers += 1
                issues.append({
                    "type": "missing_spacer",
                    "index": p["index"],
                    "text": p["text_preview"],
                    "message": f"문단 [{p['index']}] 직전에 빈줄 없음",
                })
        else:
            consecutive_content = 0

    # 2. 본문 lineSpacing 일관성 검사
    content_spacings = {}
    for p in body_paras:
        if p["has_text"] and p["lineSpacing_value"] != "?":
            key = f"{p['lineSpacing_type']}:{p['lineSpacing_value']}"
            content_spacings[key] = content_spacings.get(key, 0) + 1

    dominant_spacing = max(content_spacings, key=content_spacings.get) if content_spacings else None
    inconsistent = 0
    for p in body_paras:
        if p["has_text"] and p["lineSpacing_value"] != "?":
            # 레이아웃 요소는 의도적으로 다른 간격을 쓰므로 제외
            if _LAYOUT_PATTERNS.search(p["text_preview"]):
                continue
            key = f"{p['lineSpacing_type']}:{p['lineSpacing_value']}"
            if key != dominant_spacing:
                inconsistent += 1
                issues.append({
                    "type": "inconsistent_spacing",
                    "index": p["index"],
                    "expected": dominant_spacing,
                    "actual": key,
                    "text": p["text_preview"],
                })

    # 3. 원본 비교
    source_diff = 0
    if source_path:
        with zipfile.ZipFile(source_path, "r") as zf:
            src_header = zf.read("Contents/header.xml")
        src_info = _parse_spacing_from_header(src_header)
        for pr_id, info in header_info.items():
            src = src_info.get(pr_id)
            if src and info.get("lineSpacing_value") != src.get("lineSpacing_value"):
                source_diff += 1

    body_content = sum(1 for p in body_paras if p["has_text"])
    body_spacer = sum(1 for p in body_paras if p["is_spacer"])
    table_paras = sum(1 for p in paras if p["in_table"])

    summary = {
        "total_paragraphs": len(paras),
        "body_content": body_content,
        "body_spacer": body_spacer,
        "table_paragraphs": table_paras,
        "dominant_spacing": dominant_spacing,
        "missing_spacers": missing_spacers,
        "inconsistent_spacing": inconsistent,
        "source_diff": source_diff,
        "total_issues": len(issues),
    }

    # 출력
    print(f"\n=== 줄간격 검증 ===")
    print(f"전체 문단: {len(paras)}개 (본문 내용: {body_content}, "
          f"본문 빈줄: {body_spacer}, 테이블 셀: {table_paras})")
    print(f"본문 기본 줄간격: {dominant_spacing}")
    if missing_spacers:
        print(f"⚠️ 본문 빈줄 누락: {missing_spacers}곳")
        for iss in issues[:10]:  # 상위 10건만 표시
            if iss["type"] == "missing_spacer":
                print(f"   → [{iss['index']}] \"{iss['text'][:50]}\"")
        if missing_spacers > 10:
            print(f"   ... 외 {missing_spacers - 10}건")
    if inconsistent:
        print(f"⚠️ 본문 줄간격 불일치: {inconsistent}곳")
        for iss in issues[:5]:
            if iss["type"] == "inconsistent_spacing":
                print(f"   → [{iss['index']}] {iss['actual']} (기대: {iss['expected']})")
        if inconsistent > 5:
            print(f"   ... 외 {inconsistent - 5}건")
    if source_diff:
        print(f"⚠️ 원본 대비 간격 속성 차이: {source_diff}개 paraPr")
    if not issues:
        print("✅ 줄간격 정합성 이상 없음")

    return {"issues": issues, "summary": summary}


# ─────────────────────────────────────────────────────────
# 2. 줄간격 보정
# ─────────────────────────────────────────────────────────

def fix_spacing(hwpx_path, source_path=None):
    """수정된 문단의 줄간격을 원본 양식 기준으로 보정한다.

    보정 내용:
    1. 연속 내용 문단 사이에 빈줄(스페이서) 삽입
    2. Phase L로 삽입된 문단의 paraPrIDRef가 원본과 다르면 교정

    Returns:
        int: 보정된 항목 수
    """
    with zipfile.ZipFile(hwpx_path, "r") as zf:
        sec_data = zf.read("Contents/section0.xml")
        all_files = {item.filename: zf.read(item.filename) for item in zf.infolist()}

    root = etree.fromstring(sec_data)
    fixes = 0

    # 내용 문단 사이 빈줄 삽입
    # 순회하면서 연속 내용 문단을 찾고 사이에 빈줄 삽입
    sections = [root]  # 최상위 + subList 내부
    for sublist in root.iter():
        if etree.QName(sublist.tag).localname == "subList":
            sections.append(sublist)

    for parent in sections:
        children = list(parent)
        p_elements = []
        for child in children:
            if etree.QName(child.tag).localname == "p":
                p_elements.append(child)

        if len(p_elements) < 2:
            continue

        insertions = []  # (insert_after_index, spacer_element)

        for i in range(len(p_elements) - 1):
            curr = p_elements[i]
            next_p = p_elements[i + 1]

            curr_has_text = any(
                etree.QName(t.tag).localname == "t" and t.text and t.text.strip()
                for t in curr.iter()
            )
            next_has_text = any(
                etree.QName(t.tag).localname == "t" and t.text and t.text.strip()
                for t in next_p.iter()
            )

            if curr_has_text and next_has_text:
                # 사이에 빈줄이 없음 → 빈줄 삽입 필요
                # 현재 문단 직후의 요소가 빈줄인지 확인
                curr_idx = list(parent).index(curr)
                next_idx = list(parent).index(next_p)
                if next_idx == curr_idx + 1:
                    # 바로 붙어있음 → 빈줄 삽입
                    spacer = _make_spacer_element(curr)
                    insertions.append((curr, spacer))

        # 역순으로 삽입 (인덱스 밀림 방지)
        for after_elem, spacer in reversed(insertions):
            after_idx = list(parent).index(after_elem)
            parent.insert(after_idx + 1, spacer)
            fixes += 1

    if fixes > 0:
        # 저장
        result = etree.tostring(root, encoding="unicode", xml_declaration=False)
        # XML 선언 복원
        if sec_data.lstrip().startswith(b"<?xml"):
            decl_end = sec_data.find(b"?>")
            if decl_end != -1:
                decl = sec_data[:decl_end + 2].decode("utf-8")
                result = decl + "\n" + result

        all_files["Contents/section0.xml"] = result.encode("utf-8")

        # ZIP 재패키징
        tmp = str(hwpx_path) + ".fix_tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for name, data in all_files.items():
                if name == "mimetype":
                    zout.writestr(name, data, compress_type=zipfile.ZIP_STORED)
                else:
                    zout.writestr(name, data)
        os.replace(tmp, str(hwpx_path))

    print(f"\n=== 줄간격 보정 ===")
    print(f"빈줄 삽입: {fixes}곳")
    if fixes == 0:
        print("✅ 보정 불필요")

    return fixes


def _make_spacer_element(ref_p):
    """참조 문단 스타일을 기반으로 빈줄(스페이서) 요소를 생성한다."""
    import copy

    spacer = copy.deepcopy(ref_p)

    # 모든 텍스트를 비우기
    for t in spacer.iter():
        if etree.QName(t.tag).localname == "t":
            t.text = None
            # 자식 요소도 제거 (인라인 태그)
            for child in list(t):
                t.remove(child)

    # linesegarray 제거
    for child in list(spacer):
        if etree.QName(child.tag).localname == "linesegarray":
            spacer.remove(child)

    # run을 하나만 남기기 (빈 텍스트)
    runs = [r for r in spacer if etree.QName(r.tag).localname == "run"]
    for r in runs[1:]:
        spacer.remove(r)

    return spacer


# ─────────────────────────────────────────────────────────
# 3. 통합 후처리
# ─────────────────────────────────────────────────────────

def postprocess(hwpx_path, source_path=None, fix=False):
    """오타 검수 + 줄간격 검증(+보정)을 한 번에 실행한다.

    Returns:
        dict: {typos: {...}, spacing: {...}, fixes: int}
    """
    print("=" * 55)
    print(f"  HWPX 후처리: {Path(hwpx_path).name}")
    print("=" * 55)

    # 1. 오타 검수
    typo_result = check_typos(hwpx_path)

    # 2. 줄간격 검증
    spacing_result = check_spacing(hwpx_path, source_path)

    # 3. 자동 보정
    fix_count = 0
    if fix and spacing_result["summary"]["missing_spacers"] > 0:
        fix_count = fix_spacing(hwpx_path, source_path)

    # 종합
    total = typo_result["total_issues"] + spacing_result["summary"]["total_issues"]
    print(f"\n{'=' * 55}")
    print(f"  종합: 이슈 {total}건" + (f" (보정 {fix_count}건 적용)" if fix_count else ""))
    if total == 0:
        print("  ✅ 모든 검증 통과")
    print("=" * 55)

    return {
        "typos": typo_result,
        "spacing": spacing_result,
        "fixes": fix_count,
    }


def main():
    parser = argparse.ArgumentParser(description="HWPX 후처리 (오타 + 줄간격)")
    parser.add_argument("hwpx", help="검사할 .hwpx 파일")
    parser.add_argument("--source", help="원본 양식 (비교용)")
    parser.add_argument("--typos", action="store_true", help="오타 검수만")
    parser.add_argument("--spacing", action="store_true", help="줄간격 검증만")
    parser.add_argument("--fix", action="store_true", help="줄간격 자동 보정 적용")
    parser.add_argument("--all", action="store_true", help="전체 후처리")

    args = parser.parse_args()

    if args.all or (not args.typos and not args.spacing):
        postprocess(args.hwpx, args.source, args.fix)
    else:
        if args.typos:
            check_typos(args.hwpx)
        if args.spacing:
            result = check_spacing(args.hwpx, args.source)
            if args.fix and result["summary"]["missing_spacers"] > 0:
                fix_spacing(args.hwpx, args.source)


if __name__ == "__main__":
    main()
