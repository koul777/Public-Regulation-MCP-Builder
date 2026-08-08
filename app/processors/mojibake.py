"""HWP 원본에서 글자가 깨진 채로 읽힌 구간을 찾아낸다.

한글이 깨진 것이 아니라, 원본 안의 비-텍스트 이진 데이터가 텍스트로 잘못 읽혀
한글 사이에 끼어든 것이다. 실제 보관 문서에서 관측한 형태는 두 가지다.

1. 영문 ASCII 조각이 한자로 읽힌 것.
   ``慤桥``의 UTF-16BE 바이트는 ``61 64 68 65`` = ``adhe``이고,
   ``漠杳``는 ``6F 20 67 73`` = ``o gs``다. 내부 태그·개체 이름 조각이다.

2. 배치 좌표 숫자가 글자로 읽힌 것.
   ``ྠ``는 U+0FA0 = 4000, ``Ā``는 256으로 HWP 내부 단위(1/7200인치) 좌표값이다.
   ``ྠ Ā ྠ Ā 신 청 인 : (서명)``처럼 서명란·표 양식에 몰려서 나온다.

품질 검사와 정규화가 같은 판정을 써야 해서 두 곳이 공유하는 모듈로 분리했다.
"""

from __future__ import annotations

import re


# 정규화 단계가 지운 깨진 글자 수를 ParsedDocument.metadata에 담아 두는 키.
# 지우고 나면 본문에는 흔적이 남지 않으므로, 품질 검사가 이 값을 읽어
# "지웠지만 원본은 손상돼 있었다"고 계속 경고한다.
MOJIBAKE_REMOVED_CHARS_KEY = "mojibake_removed_char_count"
MOJIBAKE_REMOVED_BLOCKS_KEY = "mojibake_removed_block_count"
# 지웠지만 내용 손실은 아닌 글자 수(내보내기 상용구 + 배치 좌표 누출).
# 손상 경고에는 쓰지 않고 관측용으로만 남긴다.
MOJIBAKE_CLEANED_CHARS_KEY = "mojibake_cleaned_char_count"

# HWP로 내보낼 때 제목 옆에 늘 따라붙는 조각. 보관 문서 26건을 전수 조사했더니
# ``漠杳``(o gs) 53회·26건, ``慤桥``(adhe) 49회·26건으로 예외 없이 모든 문서에 있었다.
# 반면 ``ྠ``·``Ā``·``敤敱`` 같은 나머지는 1~4건에서만 나온다.
#
# 지우는 것은 똑같이 지우되, '이 원본은 손상됐다'는 경고에서는 뺀다. 모든 문서에서
# 울리는 경고는 어느 문서가 실제로 상했는지 알려 주지 못해 아무도 읽지 않게 된다.
HWP_EXPORT_BOILERPLATE = ("慤桥", "漠杳")
_HWP_EXPORT_BOILERPLATE_PATTERN = re.compile("|".join(HWP_EXPORT_BOILERPLATE))

# 실제로 관측한 HWP 깨짐 사례. 아래 일반 규칙이 놓치는 비한자 조합을 위해 남겨 둔다.
HWP_ARTIFACT_PATTERN = re.compile(r"(捤獥|汤捯|氠瑢|湰灧|桤灧|灳瑣|湯慴|湯湷|†普)")

# BMP 사용자 지정 영역과 15·16면 보조 사용자 지정 영역(HWP 글머리표·양식 글리프).
PRIVATE_USE_PATTERN = re.compile(
    "[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]"
)

# UTF-16 바이트쌍이 그대로 한자로 읽힌 깨짐.
# 한 글자만 보면 정상 한자가 그대로 걸리므로 반드시 2글자 이상 연속을 요구한다.
_CJK_RUN_PATTERN = re.compile("[㐀-鿿豈-﫿]{2,}")

# 한국어 규정 본문에 나올 수 없는 문자 계열(라틴 확장·키릴·아랍·인도계·티베트 등).
# 그리스 문자와 한글 자모(U+1100–U+11FF), 단위·원문자 기호는 실제로 쓰이므로 제외한다.
_IMPOSSIBLE_SCRIPT_PATTERN = re.compile(
    "[Ā-ͯЀ-֏֐-ࣿऀ-࿿"
    "က-ჿሀ-᳿Ḁ-ỿ]"
)

# 한 글자짜리 한자는 정상 한자와 구별할 수 없어 단독으로는 세지 않지만,
# 이미 확정된 깨짐 구간에 공백 없이 붙어 있으면 같은 깨짐의 일부로 본다.
# 보관 문서에서 확인한 浫(mk)·浵(mu)가 이 경우다.
_CJK_IDEOGRAPH_PATTERN = re.compile("[㐀-鿿豈-﫿]")


# 한자 병기 표기. 정상 규정문은 "학위(學位)"처럼 한글 뒤 괄호·낫표 안에 한자를 넣는다.
_HANJA_ANNOTATION_BRACKETS = {"(": ")", "（": "）", "[": "]", "［": "］", "「": "」", "『": "』", "〔": "〕", "《": "》", "<": ">"}


def _is_utf16_ascii_pair(char: str) -> bool:
    """이 한자의 UTF-16BE 바이트 두 개가 모두 출력 가능한 ASCII인지."""
    code_point = ord(char)
    return 0x20 <= (code_point >> 8) <= 0x7E and 0x20 <= (code_point & 0xFF) <= 0x7E


def _is_utf16_lowercase_pair(char: str) -> bool:
    """이 한자의 UTF-16BE 바이트 두 개가 모두 소문자 또는 공백인지.

    이 조건이 정상 한자와 깨짐을 갈라 준다. 깨짐의 출처는 HWP 내부의 소문자
    영문 이름이라 ``慤桥``=``adhe``, ``漠杳``=``o gs``, ``潴景``=``otfo``처럼 나온다.
    반면 정상 한자의 코드값은 ASCII 전 범위에 흩어져 대문자나 기호가 섞인다.
    ``總則``=``~=RG``, ``學位``=``[xOM``, ``改正``=``e9kc``, ``本則``=``g,RG``.

    이 구분이 없으면 옛 규정문의 ``總則``·``細則``이나 기관명 ``韓國學中央研究院``이
    깨짐으로 몰려 통째로 지워진다.
    """
    code_point = ord(char)
    high, low = code_point >> 8, code_point & 0xFF
    return all(byte == 0x20 or 0x61 <= byte <= 0x7A for byte in (high, low))


def _is_hanja_annotation(text: str, start: int, end: int) -> bool:
    """이 한자 구간이 괄호·낫표로 묶인 한자 병기인지(=정상 표기인지)."""
    if start == 0 or end >= len(text):
        return False
    closing = _HANJA_ANNOTATION_BRACKETS.get(text[start - 1])
    return closing is not None and text[end] == closing


def _is_absorbable_neighbor(char: str) -> bool:
    """확정된 깨짐 구간 옆에 붙어 있으면 같은 깨짐으로 볼 문자인지.

    한글·숫자·ASCII·원문자는 UTF-16BE 바이트쌍이 ASCII가 아니거나 한자가 아니라서
    여기서 걸리지 않는다. 괄호도 마찬가지여서 한자 병기를 파고들지 않는다.
    """
    return bool(_CJK_IDEOGRAPH_PATTERN.fullmatch(char)) and _is_utf16_lowercase_pair(char)


def _absorb_adjacent_artifacts(value: str, flagged: bytearray) -> None:
    """확정된 깨짐 구간을 좌우로 넓혀 붙어 있는 한 글자짜리 깨짐을 흡수한다."""
    seeds = [index for index, marked in enumerate(flagged) if marked]
    for index in seeds:
        cursor = index - 1
        while cursor >= 0 and not flagged[cursor] and _is_absorbable_neighbor(value[cursor]):
            flagged[cursor] = 1
            cursor -= 1
        cursor = index + 1
        while cursor < len(value) and not flagged[cursor] and _is_absorbable_neighbor(value[cursor]):
            flagged[cursor] = 1
            cursor += 1


def mojibake_artifact_spans(text: str) -> list[tuple[int, int]]:
    """깨진 글자 구간을 겹치지 않게 돌려준다. 비어 있으면 깨짐 신호가 없다는 뜻이다."""
    value = str(text or "")
    if not value:
        return []
    flagged = bytearray(len(value))
    # 사용자 지정 영역(PUA)은 HWP 글머리표·양식 글리프로도 정상적으로 쓰이므로 여기서 세지 않는다.
    # 그쪽은 private_use_char_chunks 정보 지표로 따로 관찰한다.
    for pattern in (HWP_ARTIFACT_PATTERN, _IMPOSSIBLE_SCRIPT_PATTERN):
        for match in pattern.finditer(value):
            for index in range(match.start(), match.end()):
                flagged[index] = 1
    for run in _CJK_RUN_PATTERN.finditer(value):
        streak_start: int | None = None
        for index in range(run.start(), run.end() + 1):
            in_streak = index < run.end() and _is_utf16_lowercase_pair(value[index])
            if in_streak and streak_start is None:
                streak_start = index
            elif not in_streak and streak_start is not None:
                if index - streak_start >= 2 and not _is_hanja_annotation(value, streak_start, index):
                    for flagged_index in range(streak_start, index):
                        flagged[flagged_index] = 1
                streak_start = None
    _absorb_adjacent_artifacts(value, flagged)
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, marked in enumerate(flagged):
        if marked and start is None:
            start = index
        elif not marked and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(value)))
    return spans


def mojibake_damage_spans(text: str) -> list[tuple[int, int]]:
    """손상으로 셀 구간만. 모든 문서에 붙는 내보내기 상용구는 뺀다."""
    value = str(text or "")
    return [
        (start, end)
        for start, end in mojibake_artifact_spans(value)
        if _HWP_EXPORT_BOILERPLATE_PATTERN.sub("", value[start:end]).strip()
    ]


def mojibake_substitution_spans(text: str) -> list[tuple[int, int]]:
    """개체 이름이 본문 자리를 차지한 구간. 원래 있던 내용이 사라진 쪽이다.

    수식·필드 같은 내장 개체가 제 내용 대신 내부 이름으로 읽히면, 그 자리에 있던
    것이 통째로 없어진다. 실제로 성과급 세칙에서 ``기본연봉 敤敱 지급률``처럼
    곱셈 기호가 사라져, 지우고 나면 곱한다는 사실 자체가 남지 않았다.

    반면 배치 좌표가 새어 나온 삽입형(``ྠ Ā``)은 군더더기라 지우면 끝난다.
    둘을 같은 경고로 묶으면 어느 쪽이 사람 확인이 필요한지 알 수 없다.
    """
    value = str(text or "")
    return [
        (start, end)
        for start, end in mojibake_damage_spans(value)
        if _CJK_IDEOGRAPH_PATTERN.search(value[start:end])
        or HWP_ARTIFACT_PATTERN.search(value[start:end])
    ]


def mojibake_insertion_char_count(text: str) -> int:
    """지우면 해결되는 군더더기(배치 좌표 등) 글자 수."""
    value = str(text or "")
    substitution = set(mojibake_substitution_spans(value))
    return sum(
        end - start for start, end in mojibake_damage_spans(value) if (start, end) not in substitution
    )


def mojibake_artifact_char_count(text: str) -> int:
    """사람이 원문과 대조해야 하는 손상(=내용이 사라진 대체형) 글자 수."""
    return sum(end - start for start, end in mojibake_substitution_spans(text))


def has_mojibake_artifacts(text: str) -> bool:
    return bool(mojibake_substitution_spans(text))


def strip_mojibake_artifacts(text: str) -> tuple[str, int, int]:
    """깨진 구간을 지운 본문과 (내용 손실 글자 수, 그냥 지운 글자 수)를 돌려준다.

    구간을 빈 문자열로 지우면 앞뒤 낱말이 붙어버리므로 공백 한 칸으로 바꾼다.
    남는 공백은 호출한 쪽의 공백 정리 단계가 걷어낸다.

    지우는 것은 종류를 가리지 않고 똑같이 지운다. 다만 '사람이 원문과 대조해야 한다'는
    경고에 쓰이는 수는 개체 이름이 본문 자리를 차지한 대체형뿐이다. 내보내기 상용구와
    배치 좌표 누출까지 손상으로 세면 26건 전부가 걸려 어느 문서가 실제로 상했는지
    알 수 없다(실측: 26/26 → 1/26).
    """
    value = str(text or "")
    spans = mojibake_artifact_spans(value)
    if not spans:
        return value, 0, 0
    pieces: list[str] = []
    cursor = 0
    damaged = 0
    cleaned = 0
    for start, end in spans:
        pieces.append(value[cursor:start])
        pieces.append(" ")
        fragment = value[start:end]
        if not _HWP_EXPORT_BOILERPLATE_PATTERN.sub("", fragment).strip():
            cleaned += end - start
        elif _CJK_IDEOGRAPH_PATTERN.search(fragment) or HWP_ARTIFACT_PATTERN.search(fragment):
            # 개체 이름이 본문 자리를 차지한 대체형. 원래 있던 내용이 사라졌다.
            damaged += end - start
        else:
            cleaned += end - start
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces), damaged, cleaned
