"""검색 조건: 사용자가 입력창 아래에서 고르는 지역·신청 주체·접수 상태.

질문에 일일이 적지 않아도 되게 계정에 저장해 두고, 지원사업 공고를 찾는 질문마다 모델에 함께 알려 준다. 무엇이 적용됐는지
실행 전에 화면에서 보이고, 답에도 적는다. 아무것도 고르지 않았으면 모델에 아무 말도 덧붙이지 않는다(전과 똑같이 동작한다).
"""
from __future__ import annotations

from typing import Any

REGIONS = ('서울', '부산', '대구', '인천', '광주', '대전', '울산', '세종', '경기', '강원', '충북', '충남', '전북', '전남', '경북', '경남', '제주')
APPLICANTS = ('예비창업자', '개인사업자', '법인 중소기업', '중견기업', '대학·연구기관')
CHOICES = {'regions': list(REGIONS), 'applicants': list(APPLICANTS)}


def clean(raw: Any) -> dict[str, Any]:
    """받은 값을 고를 수 있는 것으로만 남긴다. region·applicant 가 빈 글이면 고르지 않은 것, closed 는 마감된 공고까지 볼지."""
    raw = raw if isinstance(raw, dict) else {}
    return {'region': raw.get('region') if raw.get('region') in REGIONS else '',
            'applicant': raw.get('applicant') if raw.get('applicant') in APPLICANTS else '',
            'closed': raw.get('closed') is True}


def label(conditions: dict[str, Any]) -> str:
    """화면과 답에 적는 한 줄. 예: 강원 · 개인사업자 · 접수 중"""
    return ' · '.join(part for part in (conditions['region'], conditions['applicant'], '마감 포함' if conditions['closed'] else '접수 중') if part)


def note(raw: Any) -> str:
    """모델에 알려 주는 글. 고른 것이 없으면 빈 글."""
    conditions = clean(raw)
    if not (conditions['region'] or conditions['applicant'] or conditions['closed']):
        return ''
    lines = []
    if conditions['region']:
        lines.append(f'- 지역: {conditions["region"]}. 공고를 찾을 때 region 에 넣습니다(그 지역 소재 기업이 대상인 공고). 전국 대상 공고는 사용자가 원할 때 따로 찾습니다.')
    if conditions['applicant']:
        lines.append(f'- 신청 주체: {conditions["applicant"]}. 공고를 표로 정리할 때 "신청 주체(공고문)" 칸에 공고문이 정한 신청 주체를 그 표기대로 적고, '
                     f'"내 조건 기준" 칸에 {conditions["applicant"]}가 신청할 수 있는지를 가능 / 불가 / 확인 필요 가운데 하나로 적습니다. '
                     '불가와 확인 필요에는 이유를 한마디 붙입니다(예: "불가 — 예비창업자 전용", "확인 필요 — 제조업 영위 조건"). '
                     '판단은 공고문의 자격 대목(get_history_requirements)에 근거하고, 근거가 없으면 확인 필요로 둡니다. 불가인 공고도 표에서 빼지 않습니다.')
    lines.append('- 접수 상태: ' + ('마감된 공고까지 포함해 찾습니다(공고 이력 도구).' if conditions['closed'] else '지금 접수 중인 공고만 찾습니다.'))
    return ('\n\n사용자가 화면에서 고른 검색 조건입니다. 지원사업 공고를 찾는 질문에 적용합니다. 질문에 다른 지역·대상·기간이 적혀 있으면 질문이 우선이고, '
            '국가R&D 과제·성과를 묻는 질문과 건수·추이만 묻는 질문에는 적용하지 않습니다.\n' + '\n'.join(lines)
            + f'\n조건을 적용했으면 답의 첫 문장 끝에 "(조건: {label(conditions)})"를 적습니다.')
