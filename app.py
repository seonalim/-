"""
대손충당금 설정 적정성 비교 웹툴 (Streamlit)
==========================================

동종업종 기업들의 대손충당금 관련 지표를 DART(전자공시시스템) Open API로 실시간 수집하여
비교하고, 이상치(동종평균과 크게 차이나는 기업)를 z-score로 찾아내는 웹 도구입니다.

계정 표시 관행이 기업마다 달라서, 두 가지 방식을 자동으로 시도합니다.
  A) 일반기업형: 대손충당금(잔액) / 매출채권(잔액)  ← 재무상태표에 별도 계정으로 잡히는 경우
  B) 금융회사형: 신용손실충당금 전입액(당기 손익) / 대출채권(잔액)  ← 은행/금융지주처럼
     충당금 "잔액"은 순액표시라 안 잡히지만, 당기 신규 적립액은 손익계산서에 잡히는 경우

배포 방법 (요약)
----------------
1. github.com 에서 새 저장소(repository)를 만들고 이 폴더의 app.py, requirements.txt 를
   웹 화면에서 그대로 드래그 앤 드롭으로 업로드합니다 (git 명령어 필요 없음).
2. share.streamlit.io 에서 GitHub 계정으로 로그인 → "New app" → 방금 만든 저장소 선택 → Deploy.
3. 앱 설정(Settings) → Secrets 에 아래처럼 입력합니다.
       DART_API_KEY = "발급받은_키"
4. 몇 분 뒤 https://내앱이름.streamlit.app 형태의 실제 URL이 생깁니다.
"""

import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
import pandas as pd
import streamlit as st
import altair as alt

BASE_URL = "https://opendart.fss.or.kr/api"

st.set_page_config(page_title="대손충당금 설정 적정성 비교", layout="wide")

# ----------------------------------------------------------------------
# 사전 정의된 동종업종 그룹 (필요시 자유롭게 커스텀 입력도 가능)
# ----------------------------------------------------------------------
PRESET_GROUPS = {
    "5대 금융지주": ["KB금융지주", "신한지주", "하나금융지주", "우리금융지주", "농협금융지주"],
    "편의점/유통 3사": ["GS리테일", "BGF리테일", "이마트"],
    "직접 입력": [],
}


def get_api_key():
    key = st.secrets.get("DART_API_KEY", "") if hasattr(st, "secrets") else ""
    if not key:
        key = st.sidebar.text_input("DART API 키 (Secrets에 등록 안 했다면 여기 직접 입력)", type="password")
    return key


@st.cache_data(ttl=60 * 60 * 24, show_spinner="기업 코드 목록 불러오는 중...")
def load_corp_code_map(api_key: str) -> dict:
    resp = requests.get(f"{BASE_URL}/corpCode.xml", params={"crtfc_key": api_key}, timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml_bytes)
    name_to_code = {}
    for item in root.findall("list"):
        corp_name = (item.findtext("corp_name") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        if corp_name and corp_code and stock_code:
            name_to_code[corp_name] = corp_code
    return name_to_code


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def fetch_full_financials(api_key: str, corp_code: str, bsns_year: str, reprt_code: str, fs_div: str) -> list:
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
        "fs_div": fs_div,
    }
    resp = requests.get(f"{BASE_URL}/fnlttSinglAcntAll.json", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "000":
        return []
    return data.get("list", [])


def parse_amount(raw):
    if raw is None:
        return None
    cleaned = str(raw).replace(",", "").strip()
    if cleaned in ("", "-"):
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def sum_matching(accounts, sj_div, patterns):
    total, matched_names = 0, []
    for a in accounts:
        if a.get("sj_div") != sj_div:
            continue
        name = a.get("account_nm", "")
        if any(p in name for p in patterns):
            amt = parse_amount(a.get("thstrm_amount"))
            if amt:
                total += abs(amt)
                matched_names.append(name)
    return (total, matched_names) if matched_names else (None, [])


def extract_ratio(company: str, accounts: list) -> dict:
    result = {"company": company, "method": None, "base_amount": None,
              "provision_amount": None, "ratio": None, "note": ""}
    if not accounts:
        result["note"] = "재무제표 데이터 없음"
        return result

    # A) 일반기업형: 매출채권 대비 대손충당금(잔액)
    receivable, _ = sum_matching(accounts, "BS", ["매출채권"])
    allowance, _ = sum_matching(accounts, "BS", ["대손충당금"])
    if receivable and allowance:
        result.update(method="A. 대손충당금(잔액)/매출채권(잔액)", base_amount=receivable,
                       provision_amount=allowance, ratio=round(allowance / receivable * 100, 3),
                       note="정상 산출")
        return result

    # B) 금융회사형: 대출채권 잔액 대비 신용손실충당금 당기 신규 적립액
    loans, _ = sum_matching(accounts, "BS", ["대출채권"])
    provision, _ = sum_matching(accounts, "CIS", ["신용손실충당금", "신용손실에대한손상차손", "대손상각", "손상차손"])
    if loans and provision:
        result.update(method="B. 신용손실충당금 신규적립액/대출채권(잔액) [금융회사형]",
                       base_amount=loans, provision_amount=provision,
                       ratio=round(provision / loans * 100, 3), note="정상 산출 (은행/금융지주 방식)")
        return result

    if receivable and not allowance:
        result.update(base_amount=receivable,
                      note="매출채권은 찾았으나 대손충당금 별도 계정 미발견 (주석 원문 확인 필요)")
        return result
    if loans and not provision:
        result.update(base_amount=loans,
                      note="대출채권은 찾았으나 신용손실충당금 항목 미발견 (주석 원문 확인 필요)")
        return result

    result["note"] = "매출채권/대출채권 계정 자체를 찾지 못함 (업종 특성상 해당 없음 가능)"
    return result


def flag_outliers(df: pd.DataFrame, z_threshold=1.5) -> pd.DataFrame:
    valid = df["ratio"].notna()
    if valid.sum() < 2:
        df["peer_mean"] = None
        df["z_score"] = None
        df["flag"] = ""
        return df
    mean = df.loc[valid, "ratio"].mean()
    std = df.loc[valid, "ratio"].std(ddof=0)
    df["peer_mean"] = round(mean, 3)
    df["z_score"] = df["ratio"].apply(lambda r: round((r - mean) / std, 2) if pd.notna(r) and std else None)
    df["flag"] = df["z_score"].apply(
        lambda z: "⚠ 이상치 후보" if z is not None and abs(z) >= z_threshold
        else ("△ 근접" if z is not None and abs(z) >= 1.0 else "")
    )
    return df


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
st.title("대손충당금 설정 적정성 비교 웹툴")
st.caption("동종업종 기업의 대손충당금 관련 지표를 DART Open API로 실시간 수집해 비교하고, 이상치를 자동으로 찾아줍니다.")

api_key = get_api_key()

col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    group = st.selectbox("동종업종 그룹 선택", list(PRESET_GROUPS.keys()))
    default_companies = ", ".join(PRESET_GROUPS[group])
    companies_input = st.text_area("비교할 회사명 (쉼표로 구분, 자유롭게 수정 가능)", value=default_companies, height=80)
with col2:
    year = st.selectbox("사업연도", ["2024", "2023", "2022", "2021"], index=1)
with col3:
    fs_div = st.selectbox("재무제표 기준", ["CFS(연결)", "OFS(별도)"])
    fs_div_code = "CFS" if fs_div.startswith("CFS") else "OFS"

run = st.button("분석 실행", type="primary", use_container_width=False)

if run:
    if not api_key:
        st.error("DART API 키가 필요합니다. 왼쪽 사이드바에 입력하거나 Secrets에 DART_API_KEY로 등록해주세요.")
        st.stop()

    companies = [c.strip() for c in companies_input.split(",") if c.strip()]
    if not companies:
        st.warning("비교할 회사명을 최소 2개 이상 입력해주세요.")
        st.stop()

    try:
        corp_map = load_corp_code_map(api_key)
    except Exception as e:
        st.error(f"기업 코드 목록을 불러오지 못했습니다: {e}")
        st.stop()

    rows = []
    progress = st.progress(0.0, text="데이터 수집 중...")
    for i, company in enumerate(companies):
        corp_code = corp_map.get(company)
        if not corp_code:
            rows.append({"company": company, "method": None, "base_amount": None,
                         "provision_amount": None, "ratio": None,
                         "note": "기업명을 DART에서 찾지 못함 (정확한 공식 명칭인지 확인해주세요)"})
        else:
            accounts = fetch_full_financials(api_key, corp_code, year, "11011", fs_div_code)
            rows.append(extract_ratio(company, accounts))
        progress.progress((i + 1) / len(companies), text=f"{company} 처리 완료")
    progress.empty()

    df = pd.DataFrame(rows)
    df = flag_outliers(df)

    st.subheader("비교 결과")
    valid_df = df[df["ratio"].notna()].sort_values("ratio", ascending=False)
    invalid_df = df[df["ratio"].isna()]

    if not valid_df.empty:
        chart_df = valid_df.copy()
        chart_df["색상"] = chart_df["flag"].apply(
            lambda f: "이상치 후보" if f == "⚠ 이상치 후보" else ("근접" if f == "△ 근접" else "정상")
        )
        chart = alt.Chart(chart_df).mark_bar().encode(
            x=alt.X("company:N", sort="-y", title=None),
            y=alt.Y("ratio:Q", title="비율 (%)"),
            color=alt.Color("색상:N",
                             scale=alt.Scale(domain=["정상", "근접", "이상치 후보"],
                                             range=["#2a78d6", "#fab219", "#ec835a"]),
                             legend=alt.Legend(title=None)),
            tooltip=["company", "method", "ratio", "peer_mean", "z_score", "note"]
        ).properties(height=380)
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("자동 산출 가능한 회사가 없습니다. 아래 표에서 사유를 확인해주세요.")

    st.dataframe(
        df[["company", "method", "base_amount", "provision_amount", "ratio", "peer_mean", "z_score", "flag", "note"]],
        use_container_width=True,
        hide_index=True,
    )

    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("결과 CSV 다운로드", csv, file_name=f"bad_debt_ratio_{year}.csv", mime="text/csv")

    st.caption(
        "⚠ 이 지표는 재무제표에 대손충당금 잔액이 별도 계정으로 태깅되지 않은 기업이 많아, "
        "가능한 경우 방식A(잔액/잔액), 불가능하면 방식B(당기 신규 적립액/대출채권, 은행형)를 자동으로 적용합니다. "
        "z-score는 동일 실행에 포함된 회사들 사이에서만 계산되며, 절대 결론이 아니라 추가 검토가 필요한 대상을 "
        "찾기 위한 1차 스크리닝 결과입니다."
    )
else:
    st.info("왼쪽에서 비교할 회사와 연도를 선택한 뒤 '분석 실행' 버튼을 눌러주세요.")
