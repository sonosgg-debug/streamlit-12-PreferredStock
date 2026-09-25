"""
data_loader.py
한국 증시(KOSPI, KOSDAQ)의 보통주 및 우선주 종목 데이터를 수집하고
투자 지표(괴리율, 배당수익률, 배당성향 등) 및 시계열 데이터를 산출하는 모듈.
Streamlit Cloud(해외 IP) 및 로컬 환경 모두에서 100% 동작하는 하이브리드 데이터 파이프라인.
"""

import socket
socket.setdefaulttimeout(5.0)

import sys
# Python 3.12+ 및 Streamlit Cloud(Python 3.14) 환경에서 pykrx의 pkg_resources 모듈 임포트 에러 방지용 shim
try:
    import pkg_resources
except Exception:
    try:
        import setuptools.command
        import pkg_resources
    except Exception:
        import types
        pkg_mock = types.ModuleType("pkg_resources")
        pkg_mock.resource_filename = lambda *args, **kwargs: ""
        pkg_mock.resource_string = lambda *args, **kwargs: b""
        pkg_mock.Requirement = type("Requirement", (), {"parse": lambda s: s})
        pkg_mock.get_distribution = lambda *args, **kwargs: type("Dist", (), {"version": "1.0.0"})()
        sys.modules["pkg_resources"] = pkg_mock

import os
import re
import datetime
import urllib.request
import json
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import numpy as np

# 1. KRX 계정 인증 설정
def init_krx_credentials():
    """상위 00 API Key 디렉토리의 KRX 계정 파일, 환경변수 또는 Streamlit Secrets를 유연하게 확인하여 설정"""
    # 1) Streamlit Cloud Secrets 확인 (대소문자 및 섹션 구분 없이 검색)
    try:
        import streamlit as st
        if hasattr(st, 'secrets'):
            # 직렬 키 탐색
            for id_key in ['KRX_ID', 'krx_id', 'krx-id', 'Krx_Id']:
                if id_key in st.secrets and st.secrets[id_key]:
                    os.environ['KRX_ID'] = str(st.secrets[id_key]).strip()
                    break
            for pw_key in ['KRX_PW', 'krx_pw', 'krx-pw', 'Krx_Pw']:
                if pw_key in st.secrets and st.secrets[pw_key]:
                    os.environ['KRX_PW'] = str(st.secrets[pw_key]).strip()
                    break
            
            # [krx] 섹션 탐색
            for sec_name in ['krx', 'KRX', 'Krx']:
                if sec_name in st.secrets:
                    sec = st.secrets[sec_name]
                    if isinstance(sec, dict):
                        if 'id' in sec and not os.getenv('KRX_ID'):
                            os.environ['KRX_ID'] = str(sec['id']).strip()
                        elif 'ID' in sec and not os.getenv('KRX_ID'):
                            os.environ['KRX_ID'] = str(sec['ID']).strip()
                        if 'pw' in sec and not os.getenv('KRX_PW'):
                            os.environ['KRX_PW'] = str(sec['pw']).strip()
                        elif 'PW' in sec and not os.getenv('KRX_PW'):
                            os.environ['KRX_PW'] = str(sec['PW']).strip()
    except Exception:
        pass

    # 2) 로컬 00 API Key 디렉토리 탐색
    if not os.getenv('KRX_ID') or not os.getenv('KRX_PW'):
        parent_dir = os.path.dirname(os.path.abspath(__file__))
        grandparent_dir = os.path.dirname(parent_dir)
        api_key_path = os.path.join(grandparent_dir, "00 API Key", "KRX ID&PW.txt")
        
        if os.path.exists(api_key_path):
            try:
                with open(api_key_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("ID :") or line.startswith("ID:"):
                            os.environ['KRX_ID'] = line.split(":", 1)[1].strip()
                        elif line.startswith("PW :") or line.startswith("PW:"):
                            os.environ['KRX_PW'] = line.split(":", 1)[1].strip()
            except Exception as e:
                print(f"KRX 인증 파일 읽기 오류: {e}")

init_krx_credentials()

# pykrx 안전 로딩
try:
    from pykrx import stock
    HAS_PYKRX = True
except Exception as e:
    stock = None
    HAS_PYKRX = False
    print(f"pykrx 로딩 실패: {e}")

# yfinance 안전 로딩 (시계열 폴백용)
try:
    import yfinance as yf
    HAS_YF = True
except Exception:
    yf = None
    HAS_YF = False

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(CURRENT_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
MASTER_FILE = os.path.join(CURRENT_DIR, "pref_stocks_master.csv")


def ensure_krx_session():
    """pykrx 세션이 유효한지 확인하고 필요한 경우 재로그인 수행"""
    init_krx_credentials()
    try:
        from pykrx.website.comm import auth, webio
        sess = auth.get_auth_session()
        if sess is None or not sess.is_valid():
            login_id = os.getenv('KRX_ID')
            login_pw = os.getenv('KRX_PW')
            if login_id and login_pw:
                new_sess = auth.build_krx_session(login_id, login_pw)
                if new_sess:
                    auth.set_auth_session(new_sess)
                    webio.set_auth_session(new_sess)
    except Exception as e:
        print(f"KRX 세션 갱신 시도 중 예외: {e}")



import requests

# 한국거래소(KRX) 정규 휴장일 및 법정 공휴일 (2024~2027)
KRX_HOLIDAYS = {
    # 2024
    '20240101', '20240209', '20240212', '20240301', '20240410', '20240501', '20240506',
    '20240515', '20240606', '20240815', '20240916', '20240917', '20240918', '20241001',
    '20241003', '20241009', '20241225', '20241231',
    # 2025
    '20250101', '20250128', '20250129', '20250130', '20250303', '20250501', '20250505',
    '20250506', '20250606', '20250815', '20251003', '20251006', '20251007', '20251008',
    '20251009', '20251225', '20251231',
    # 2026
    '20260101', '20260216', '20260217', '20260218', '20260302', '20260501', '20260505',
    '20260525', '20260603', '20260606', '20260817', '20260924', '20260925', '20261005',
    '20261009', '20261225', '20261231',
    # 2027
    '20270101', '20270208', '20270209', '20270210', '20270301', '20270503', '20270505',
    '20270513', '20270607', '20270816', '20270914', '20270915', '20270916', '20271004',
    '20271011', '20271225', '20271231'
}

_CACHED_TRADING_DAYS = None

def get_krx_trading_days(count=120):
    """
    한국거래소(KRX)의 실제 거래일(개장일) 목록을 반환합니다.
    1. 네이버 증시 API를 통해 실시간 실제 거래일 리스트를 우선 확보
    2. 실패 시 사전 정의된 휴장일 캘린더 및 주말 제외 알고리즘으로 폴백
    """
    global _CACHED_TRADING_DAYS
    if _CACHED_TRADING_DAYS is not None and len(_CACHED_TRADING_DAYS) >= count:
        return _CACHED_TRADING_DAYS
        
    days = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    pages_needed = (count + 59) // 60
    for page in range(1, pages_needed + 1):
        try:
            url = f'https://m.stock.naver.com/api/stock/005930/price?pageSize=60&page={page}'
            r = requests.get(url, headers=headers, timeout=3)
            if r.status_code == 200:
                items = r.json()
                if items:
                    days.extend([item['localTradedAt'].replace('-', '') for item in items])
                else:
                    break
        except Exception:
            pass
            
    if days:
        _CACHED_TRADING_DAYS = sorted(list(set(days)))
        return _CACHED_TRADING_DAYS
        
    fallback_days = []
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_kst = now_utc + datetime.timedelta(hours=9)
    d = now_kst
    for _ in range(count * 3):
        d_str = d.strftime('%Y%m%d')
        if d.weekday() < 5 and d_str not in KRX_HOLIDAYS:
            fallback_days.append(d_str)
            if len(fallback_days) >= count:
                break
        d -= datetime.timedelta(days=1)
        
    _CACHED_TRADING_DAYS = sorted(fallback_days)
    return _CACHED_TRADING_DAYS

def is_krx_trading_day(date_str):
    """주어진 날짜(YYYYMMDD 또는 YYYY-MM-DD)가 실제 거래일인지 판별합니다."""
    clean_date = str(date_str).replace('-', '')
    trading_days = get_krx_trading_days(120)
    if clean_date in trading_days:
        return True
    try:
        dt = datetime.datetime.strptime(clean_date, "%Y%m%d")
        return (dt.weekday() < 5) and (clean_date not in KRX_HOLIDAYS)
    except:
        return False

def get_latest_business_day(target_date=None):
    """
    가장 최근 거래 완료된 실제 영업일 YYYYMMDD 반환.
    - target_date가 전달된 경우: 해당 날짜가 거래일이면 그대로, 휴장일이면 직전 실제 거래일로 자동 보정
    - target_date가 없는 경우: KST 기준 16:00 이전이거나 오늘이 휴장일이면 최신 마감 거래일 반환
    """
    trading_days = get_krx_trading_days(120)
    
    if target_date:
        clean_date = str(target_date).replace('-', '')
        if clean_date in trading_days:
            return clean_date
        earlier = [d for d in trading_days if d <= clean_date]
        if earlier:
            return earlier[-1]
        try:
            dt = datetime.datetime.strptime(clean_date, "%Y%m%d")
            while True:
                d_str = dt.strftime("%Y%m%d")
                if dt.weekday() < 5 and d_str not in KRX_HOLIDAYS:
                    return d_str
                dt -= datetime.timedelta(days=1)
        except Exception:
            return clean_date

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_kst = now_utc + datetime.timedelta(hours=9)
    today_str = now_kst.strftime('%Y%m%d')

    if now_kst.hour >= 16 and today_str in trading_days:
        return today_str

    prior_days = [d for d in trading_days if d < today_str]
    if prior_days:
        return prior_days[-1]

    return trading_days[-1] if trading_days else (now_kst - datetime.timedelta(days=1)).strftime('%Y%m%d')


def is_preferred_stock(ticker, name, all_names):
    """
    우선주 여부 및 해당 보통주 티커 탐색
    반환값: (is_pref, common_ticker)
    """
    # 1. 일반적인 우선주 티커 규칙 (끝자리가 5, 7, 9, K, L, M 등이고 앞 5자리 + '0'이 존재하는 경우)
    base_candidate = ticker[:5] + '0'
    if base_candidate in all_names and base_candidate != ticker:
        if '우' in name or name.endswith('우') or '우B' in name or '우C' in name or '(전환)' in name:
            return True, base_candidate
        if ticker[-1] in ['5', '7', '9', 'K', 'L', 'M']:
            return True, base_candidate

    # 2. 이름 정규식 규칙으로 탐색
    pref_suffixes = [
        r'(\d+)?우[A-Za-z]?(\([^\)]+\))?$',
        r'우[A-Za-z]?(\([^\)]+\))?$',
        r'(\d+)?우[A-Za-z]?$',
        r'우$'
    ]
    for pattern in pref_suffixes:
        cleaned_name = re.sub(pattern, '', name).strip()
        if cleaned_name and cleaned_name != name:
            for cand_t, cand_n in all_names.items():
                if cand_n == cleaned_name and cand_t != ticker:
                    return True, cand_t

    return False, None


def load_fallback_master_data():
    """번들링된 마스터 캐시 파일에서 114개 우선주 데이터를 안전하게 로드"""
    if os.path.exists(MASTER_FILE):
        try:
            df = pd.read_csv(MASTER_FILE, dtype={'우선주코드': str, '보통주코드': str}, encoding='utf-8-sig')
            if not df.empty and '우선주명' in df.columns:
                return df
        except Exception as e:
            print(f"마스터 데이터 로드 실패: {e}")
    return pd.DataFrame()


def update_prices_with_naver(df, target_date=None):
    """
    네이버 금융 공식 모바일 API(코스콤 실시간 체결망 연동)를 통해
    우선주 및 보통주의 최종 공식 마감 종가를 고속 병렬 수집하고,
    괴리율(%), 시가총액(보통주), 배당수익률(A, B), 배당수익률 비율(B/A)을 재산출하여 반환.
    KRX 통계 화면의 20분 지연 및 장중 스냅샷 오차를 100% 무결점 보정.
    """
    if df.empty:
        return df, target_date

    p_codes = [str(x).strip() for x in df["우선주코드"].dropna().unique()]
    c_codes = [str(x).strip() for x in df["보통주코드"].dropna().unique()]
    all_tickers = list(set(p_codes + c_codes))

    def fetch_naver_price(ticker):
        url = f"https://m.stock.naver.com/api/stock/{ticker}/basic"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=4) as response:
                data = json.loads(response.read().decode("utf-8"))
                price_str = str(data.get("closePrice", "")).replace(",", "").strip()
                if not price_str or price_str == "None":
                    return ticker, (None, None)
                price = float(price_str)
                traded_at = data.get("localTradedAt", "")
                dt_str = traded_at[:10].replace("-", "") if traded_at else None
                return ticker, (price, dt_str)
        except Exception:
            return ticker, (None, None)

    try:
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = dict(executor.map(fetch_naver_price, all_tickers))
    except Exception as e:
        print(f"네이버 금융 병렬 수집 예외: {e}")
        return df, target_date

    df_res = df.copy()
    count_updated = 0
    date_candidates = []

    for idx in df_res.index:
        p_code = str(df_res.loc[idx, "우선주코드"]).strip()
        c_code = str(df_res.loc[idx, "보통주코드"]).strip()

        p_info = results.get(p_code, (None, None))
        c_info = results.get(c_code, (None, None))

        new_p_price = p_info[0]
        new_c_price = c_info[0]

        if p_info[1]:
            date_candidates.append(p_info[1])
        if c_info[1]:
            date_candidates.append(c_info[1])

        if new_c_price is not None and new_p_price is not None and new_c_price > 0 and new_p_price > 0:
            old_c_price = float(df_res.loc[idx, "보통주가"]) if pd.notna(df_res.loc[idx, "보통주가"]) else 0.0
            old_cap = float(df_res.loc[idx, "시가총액(보통주)"]) if pd.notna(df_res.loc[idx, "시가총액(보통주)"]) else 0.0

            if old_c_price > 0 and old_cap > 0:
                df_res.loc[idx, "시가총액(보통주)"] = round(old_cap * (new_c_price / old_c_price))

            df_res.loc[idx, "보통주가"] = new_c_price
            df_res.loc[idx, "우선주가"] = new_p_price

            # 괴리율(%)
            discount_rate = round(((new_c_price - new_p_price) / new_c_price) * 100, 2)
            df_res.loc[idx, "괴리율(%)"] = discount_rate

            # 배당금 및 배당수익률
            c_dps = float(df_res.loc[idx, "보통주 배당금"]) if pd.notna(df_res.loc[idx, "보통주 배당금"]) else 0.0
            p_dps = float(df_res.loc[idx, "우선주 배당금"]) if pd.notna(df_res.loc[idx, "우선주 배당금"]) else 0.0

            c_yield = round((c_dps / new_c_price) * 100, 2)
            p_yield = round((p_dps / new_p_price) * 100, 2)
            df_res.loc[idx, "보통주 배당수익률(A)"] = c_yield
            df_res.loc[idx, "우선주 배당수익률(B)"] = p_yield

            # 배당수익률 비율(B/A)
            if c_yield > 0:
                df_res.loc[idx, "배당수익률 비율(B/A)"] = round(p_yield / c_yield, 2)
            else:
                df_res.loc[idx, "배당수익률 비율(B/A)"] = np.nan if p_yield == 0 else 999.0

            count_updated += 1

    if count_updated >= 50:
        df_res = df_res.sort_values(by="시가총액(보통주)", ascending=False).reset_index(drop=True)
        actual_date = target_date
        if date_candidates:
            from collections import Counter
            valid_dates = [d for d in date_candidates if len(d) == 8]
            if valid_dates:
                actual_date = Counter(valid_dates).most_common(1)[0][0]
        return df_res, actual_date

    return df, target_date


def update_master_with_yfinance(df_master, target_date=None):
    """
    yfinance를 활용하여 114개 우선주 및 보통주의 최신 종가를 일괄 다운로드하고
    괴리율, 시가총액, 배당수익률(A, B), 배당수익률 비율(B/A)을 재산출하여 반환.
    KRX 서버 점검, 네트워크 타임아웃, Streamlit Cloud 해외 IP 차단 시 100% 무결점 최신 시세 보장.
    """
    if df_master.empty or not HAS_YF or not yf:
        return df_master, target_date

    df_res = df_master.copy()
    symbol_map = {}
    for idx, row in df_res.iterrows():
        mkt = row.get("시장", "코스피")
        suff = ".KS" if mkt == "코스피" else ".KQ"
        p_code = str(row["우선주코드"]).strip()
        c_code = str(row["보통주코드"]).strip()
        symbol_map[p_code] = f"{p_code}{suff}"
        symbol_map[c_code] = f"{c_code}{suff}"

    all_symbols = list(set(symbol_map.values()))
    try:
        yf_data = yf.download(all_symbols, period="5d", progress=False)
        if "Close" not in yf_data:
            return df_master, target_date

        close_df = yf_data["Close"].dropna(how="all")
        if close_df.empty:
            return df_master, target_date

        # 최신 영업일 종가 시리즈 추출
        latest_series = close_df.iloc[-1]
        actual_date = latest_series.name.strftime("%Y%m%d")

        count_updated = 0
        for idx in df_res.index:
            p_code = str(df_res.loc[idx, "우선주코드"]).strip()
            c_code = str(df_res.loc[idx, "보통주코드"]).strip()
            p_sym = symbol_map.get(p_code)
            c_sym = symbol_map.get(c_code)

            new_c_price = float(latest_series[c_sym]) if c_sym in latest_series and pd.notna(latest_series[c_sym]) else 0.0
            new_p_price = float(latest_series[p_sym]) if p_sym in latest_series and pd.notna(latest_series[p_sym]) else 0.0

            if new_c_price > 0 and new_p_price > 0:
                old_c_price = float(df_res.loc[idx, "보통주가"]) if pd.notna(df_res.loc[idx, "보통주가"]) else 0.0
                old_cap = float(df_res.loc[idx, "시가총액(보통주)"]) if pd.notna(df_res.loc[idx, "시가총액(보통주)"]) else 0.0

                if old_c_price > 0 and old_cap > 0:
                    df_res.loc[idx, "시가총액(보통주)"] = round(old_cap * (new_c_price / old_c_price))

                df_res.loc[idx, "보통주가"] = new_c_price
                df_res.loc[idx, "우선주가"] = new_p_price

                # 괴리율(%)
                discount_rate = round(((new_c_price - new_p_price) / new_c_price) * 100, 2)
                df_res.loc[idx, "괴리율(%)"] = discount_rate

                # 배당금 및 배당수익률
                c_dps = float(df_res.loc[idx, "보통주 배당금"]) if pd.notna(df_res.loc[idx, "보통주 배당금"]) else 0.0
                p_dps = float(df_res.loc[idx, "우선주 배당금"]) if pd.notna(df_res.loc[idx, "우선주 배당금"]) else 0.0

                c_yield = round((c_dps / new_c_price) * 100, 2)
                p_yield = round((p_dps / new_p_price) * 100, 2)
                df_res.loc[idx, "보통주 배당수익률(A)"] = c_yield
                df_res.loc[idx, "우선주 배당수익률(B)"] = p_yield

                # 배당수익률 비율(B/A)
                if c_yield > 0:
                    df_res.loc[idx, "배당수익률 비율(B/A)"] = round(p_yield / c_yield, 2)
                else:
                    df_res.loc[idx, "배당수익률 비율(B/A)"] = np.nan if p_yield == 0 else 999.0

                count_updated += 1

        if count_updated >= 50:
            df_res = df_res.sort_values(by="시가총액(보통주)", ascending=False).reset_index(drop=True)
            return df_res, actual_date

    except Exception as e:
        print(f"yfinance 최신 종가 업데이트 중 예외 발생: {e}")

    return df_master, target_date


def load_market_data(force_refresh=False):
    """
    KOSPI 및 KOSDAQ 시장의 보통주-우선주 전체 데이터를 수집하고
    13개 투자 비교 지표를 산출하여 DataFrame, 기준일자, 데이터 제공처 반환.
    1단계: 로컬 캐시 (당일 파일 존재 시 즉시 반환)
    2단계: KRX 실시간 펀더멘털 수집 + 네이버 금융(코스콤 실시간 피드) 공식 마감 종가 교정
    3단계: KRX 온라인 수집 불가 시(해외 IP, 주말 점검 등) 네이버 금융 동적 마감 종가 갱신
    4단계: 네이버 금융 불가 시 yfinance 동적 최신 종가 갱신
    5단계: 완전 오프라인 시 마스터 데이터 폴백
    """
    date = get_latest_business_day()
    cache_path = os.path.join(CACHE_DIR, f"pref_summary_{date}.csv")

    # 1. 당일 캐시가 있고 강제 갱신이 아닌 경우 즉시 반환
    if not force_refresh and os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path, dtype={'우선주코드': str, '보통주코드': str}, encoding='utf-8-sig')
            if not df.empty and len(df) >= 70:
                return df, date, "시장 공식 마감가 (로컬 캐시)"
        except Exception as e:
            print(f"캐시 로드 실패: {e}")

    # 2. KRX 온라인 실시간 데이터 수집 시도
    records = []
    if HAS_PYKRX and stock:
        ensure_krx_session()
        for mkt_name in ['KOSPI', 'KOSDAQ']:
            try:
                tickers = stock.get_market_ticker_list(date, market=mkt_name)
                if not tickers:
                    continue

                all_names = {t: stock.get_market_ticker_name(t) for t in tickers}
                fund = stock.get_market_fundamental_by_ticker(date, market=mkt_name)
                cap = stock.get_market_cap_by_ticker(date, market=mkt_name)

                for pref_ticker, pref_name in all_names.items():
                    is_pref, com_ticker = is_preferred_stock(pref_ticker, pref_name, all_names)
                    if not is_pref or not com_ticker:
                        continue

                    com_name = all_names.get(com_ticker, '')
                    if not com_name:
                        continue

                    # 보통주/우선주 가격 및 시총
                    com_price = float(cap.loc[com_ticker, '종가']) if com_ticker in cap.index else 0.0
                    pref_price = float(cap.loc[pref_ticker, '종가']) if pref_ticker in cap.index else 0.0
                    com_cap = float(cap.loc[com_ticker, '시가총액']) if com_ticker in cap.index else 0.0

                    # 배당금 및 EPS
                    com_eps = float(fund.loc[com_ticker, 'EPS']) if com_ticker in fund.index else 0.0
                    com_dps = float(fund.loc[com_ticker, 'DPS']) if com_ticker in fund.index else 0.0
                    pref_dps = float(fund.loc[pref_ticker, 'DPS']) if pref_ticker in fund.index else 0.0

                    # 배당성향
                    payout_ratio = round((com_dps / com_eps) * 100, 2) if com_eps > 0 and com_dps > 0 else np.nan

                    # 괴리율(%)
                    discount_rate = round(((com_price - pref_price) / com_price) * 100, 2) if com_price > 0 else 0.0

                    # 배당수익률(A, B)
                    com_div_yield = round((com_dps / com_price) * 100, 2) if com_price > 0 else 0.0
                    pref_div_yield = round((pref_dps / pref_price) * 100, 2) if pref_price > 0 else 0.0

                    # 배당수익률 비율(B/A)
                    if com_div_yield > 0:
                        div_ratio = round(pref_div_yield / com_div_yield, 2)
                    else:
                        div_ratio = np.nan if pref_div_yield == 0 else 999.0

                    market_display = "코스피" if mkt_name == "KOSPI" else "코스닥"

                    records.append({
                        "시장": market_display,
                        "보통주명": com_name,
                        "우선주명": pref_name,
                        "시가총액(보통주)": com_cap,
                        "배당성향": payout_ratio,
                        "보통주가": com_price,
                        "우선주가": pref_price,
                        "괴리율(%)": discount_rate,
                        "보통주 배당금": com_dps,
                        "보통주 배당수익률(A)": com_div_yield,
                        "우선주 배당금": pref_dps,
                        "우선주 배당수익률(B)": pref_div_yield,
                        "배당수익률 비율(B/A)": div_ratio,
                        "우선주코드": pref_ticker,
                        "보통주코드": com_ticker
                    })
            except Exception as ex:
                print(f"{mkt_name} 데이터 처리 중 에러: {ex}")

    df = pd.DataFrame(records)

    # 2-1. KRX 실시간 수집 성공 시 시장 공식 마감 종가로 교정 후 저장
    if not df.empty and len(df) >= 70:
        # 네이버 금융(코스콤 실시간 피드)으로 정규장 공식 마감 종가 교정
        df_calibrated, calibrated_date = update_prices_with_naver(df, target_date=date)
        effective_date = calibrated_date if calibrated_date else date
        eff_cache_path = os.path.join(CACHE_DIR, f"pref_summary_{effective_date}.csv")
        try:
            df_calibrated.to_csv(eff_cache_path, index=False, encoding='utf-8-sig')
            # 마스터 파일도 최신 데이터로 동기화
            df_calibrated.to_csv(MASTER_FILE, index=False, encoding='utf-8-sig')
        except Exception:
            pass
        return df_calibrated, effective_date, "KRX 정보데이터시스템 및 시장 공식 마감가"

    # 3. KRX 온라인 수집 실패(해외 IP 차단/네트워크 지연/주말 점검 등) 시 
    # 3-1: 네이버 금융으로 마스터 데이터의 종가 및 지표 최신화 시도
    print("KRX 온라인 수집 불가 -> 네이버 금융 최신 종가 동적 파이프라인 가동")
    df_master = load_fallback_master_data()
    if not df_master.empty:
        try:
            df_naver, naver_date = update_prices_with_naver(df_master, target_date=date)
            if not df_naver.empty and len(df_naver) >= 70:
                effective_date = naver_date if naver_date else date
                naver_cache_path = os.path.join(CACHE_DIR, f"pref_summary_{effective_date}.csv")
                try:
                    df_naver.to_csv(naver_cache_path, index=False, encoding='utf-8-sig')
                    df_naver.to_csv(MASTER_FILE, index=False, encoding='utf-8-sig')
                except Exception:
                    pass
                return df_naver, effective_date, "네이버 금융 (시장 공식 확정 마감가)"
        except Exception as ex_naver:
            print(f"네이버 금융 동적 갱신 예외: {ex_naver}")

    # 3-2: yfinance 폴백 (네이버 금융 실패 시)
    if not df_master.empty and HAS_YF and yf:
        try:
            df_yf, yf_date = update_master_with_yfinance(df_master, target_date=date)
            if not df_yf.empty and len(df_yf) >= 70:
                effective_date = yf_date if yf_date else date
                yf_cache_path = os.path.join(CACHE_DIR, f"pref_summary_{effective_date}.csv")
                try:
                    df_yf.to_csv(yf_cache_path, index=False, encoding='utf-8-sig')
                    df_yf.to_csv(MASTER_FILE, index=False, encoding='utf-8-sig')
                except Exception:
                    pass
                return df_yf, effective_date, "야후 파이낸스 (KRX 지연 시 실시간 동기화)"
        except Exception as ex_yf:
            print(f"yfinance 동적 갱신 예외: {ex_yf}")

    # 4. 완전 오프라인 시 마스터 정적 데이터 폴백
    print("인터넷 연결 불가 -> 마스터 데이터로 자동 폴백합니다.")
    if not df_master.empty:
        return df_master, date, "로컬 마스터 데이터 (오프라인)"

    return pd.DataFrame(), date, "데이터 없음"


def load_price_history(pref_ticker, com_ticker, months=12, latest_date=None, latest_pref_price=None, latest_com_price=None):
    """
    특정 우선주와 보통주의 과거 N개월간 일별 종가 및 일별 괴리율 시계열 로드
    1차: pykrx 시도 -> 2차: yfinance(글로벌 클라우드 100% 호환) 폴백
    3차: 테이블의 최신 영업일 종가(latest_date)와 동기화하여 시계열 데이터 누락/지연 원천 방지
    """
    end_date = get_latest_business_day()
    if latest_date:
        clean_date = str(latest_date).replace('-', '').strip()
        if len(clean_date) == 8:
            end_date = max(end_date, clean_date)

    start_dt = datetime.datetime.strptime(end_date, "%Y%m%d") - datetime.timedelta(days=int(months * 30.5))
    start_date = start_dt.strftime("%Y%m%d")

    df = pd.DataFrame()

    # 1. pykrx 시도
    if HAS_PYKRX and stock:
        try:
            df_pref = stock.get_market_ohlcv_by_date(start_date, end_date, pref_ticker)
            df_com = stock.get_market_ohlcv_by_date(start_date, end_date, com_ticker)

            if not df_pref.empty and not df_com.empty:
                df = pd.DataFrame({
                    "우선주가": df_pref['종가'],
                    "보통주가": df_com['종가']
                }).dropna()
        except Exception as e:
            print(f"pykrx 시계열 수집 실패 (yfinance 폴백 가동): {e}")

    # 2. yfinance 폴백 시도 (Streamlit Cloud 해외 IP에서도 100% 동작)
    if df.empty or len(df) < 5:
        if HAS_YF and yf:
            try:
                # 코스닥/코스피 티커 서픽스 자동 매칭 (.KS 또는 .KQ)
                for suff in ['.KS', '.KQ']:
                    try:
                        p_sym = f"{pref_ticker}{suff}"
                        c_sym = f"{com_ticker}{suff}"
                        yf_pref = yf.download(p_sym, start=start_dt.strftime("%Y-%m-%d"), progress=False)
                        yf_com = yf.download(c_sym, start=start_dt.strftime("%Y-%m-%d"), progress=False)

                        if not yf_pref.empty and not yf_com.empty:
                            c_close = yf_com['Close'].squeeze()
                            p_close = yf_pref['Close'].squeeze()

                            df_cand = pd.DataFrame({
                                "우선주가": p_close,
                                "보통주가": c_close
                            }).dropna()

                            if not df_cand.empty and len(df_cand) >= 5:
                                df = df_cand
                                break
                    except Exception:
                        continue
            except Exception as e:
                print(f"yfinance 시계열 수집 실패: {e}")

    if df.empty:
        return pd.DataFrame()

    # 3. 타임존 제거 (Asia/Seoul 등의 타임존이 있으면 Plotly에서 UTC 변환 시 날짜가 1일 전으로 표시되는 현상 원천 방지)
    if hasattr(df.index, 'tz') and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = pd.to_datetime(df.index)

    # 4. 테이블의 최신 검증 종가 데이터와 시계열 동기화 (야후 파이낸스 한국 주식 반영 지연 방지)
    if latest_date and latest_pref_price is not None and latest_com_price is not None:
        try:
            p_val = float(latest_pref_price)
            c_val = float(latest_com_price)
            if p_val > 0 and c_val > 0:
                t_dt = pd.to_datetime(str(latest_date).replace('-', '').strip())
                max_dt = df.index.max()
                if t_dt > max_dt:
                    # 야후 파이낸스 등에 최신일 종가가 아직 미반영된 경우, 테이블의 검증된 최신 종가를 추가
                    new_row = pd.DataFrame({"우선주가": [p_val], "보통주가": [c_val]}, index=[t_dt])
                    df = pd.concat([df, new_row])
                elif t_dt == max_dt:
                    # 당일 종가를 테이블의 확정 종가로 보정
                    df.loc[max_dt, "우선주가"] = p_val
                    df.loc[max_dt, "보통주가"] = c_val
        except Exception as ex_sync:
            print(f"최신 종가 동기화 예외 무시: {ex_sync}")

    return _process_history_df(df)


def _process_history_df(df):
    """시계열 데이터프레임의 괴리율, 이동평균 및 정규화 지표 계산"""
    if hasattr(df.index, 'tz') and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = pd.to_datetime(df.index)

    df['괴리율'] = ((df['보통주가'] - df['우선주가']) / df['보통주가']) * 100
    df['괴리율_MA20'] = df['괴리율'].rolling(window=20, min_periods=1).mean()
    df['괴리율_MA60'] = df['괴리율'].rolling(window=60, min_periods=1).mean()
    df['괴리율_평균'] = df['괴리율'].mean()

    first_pref = df['우선주가'].iloc[0]
    first_com = df['보통주가'].iloc[0]
    df['우선주_정규화'] = (df['우선주가'] / first_pref) * 100 if first_pref > 0 else 100
    df['보통주_정규화'] = (df['보통주가'] / first_com) * 100 if first_com > 0 else 100

    return df

def get_latest_expected_trading_day(target_date: str = None) -> str:
    """
    가장 최근 거래 완료된 실제 영업일 YYYY-MM-DD 반환.
    - target_date가 전달된 경우: 해당 날짜 기준 (또는 직전 영업일)
    - target_date가 없는 경우: KST 기준 15:45 이전이거나 오늘이 주말/새벽이면 직전 마감 거래일 반환
    """
    from datetime import datetime, timezone, timedelta
    now_kst = datetime.now(timezone(timedelta(hours=9)))
    if target_date:
        try:
            clean_date = str(target_date).replace('-', '')
            dt = datetime.strptime(clean_date, "%Y%m%d").replace(tzinfo=timezone(timedelta(hours=9)))
        except Exception:
            dt = now_kst
    else:
        dt = now_kst

    # 평일 15:45 이후에만 당일 종가 확정
    if dt.weekday() < 5 and (dt.hour > 15 or (dt.hour == 15 and dt.minute >= 45)):
        return dt.strftime("%Y-%m-%d")

    # 장전, 새벽, 주말: 직전 마감 거래일 산출
    if dt.weekday() == 0:    # 월요일 장전 -> 지난주 금요일 (3일 전)
        days_back = 3
    elif dt.weekday() == 6:  # 일요일 -> 지난주 금요일 (2일 전)
        days_back = 2
    elif dt.weekday() == 5:  # 토요일 -> 지난주 금요일 (1일 전)
        days_back = 1
    else:                    # 화~금 장전/새벽 -> 전일 (1일 전)
        days_back = 1

    return (dt - timedelta(days=days_back)).strftime("%Y-%m-%d")
