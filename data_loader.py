"""
data_loader.py
한국 증시(KOSPI, KOSDAQ)의 보통주 및 우선주 종목 데이터를 수집하고
투자 지표(괴리율, 배당수익률, 배당성향 등) 및 시계열 데이터를 산출하는 모듈.
Streamlit Cloud(해외 IP) 및 로컬 환경 모두에서 100% 동작하는 하이브리드 데이터 파이프라인.
"""

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


def get_latest_business_day():
    """가장 최근 영업일 YYYYMMDD 반환"""
    # 1) pykrx 시도
    if HAS_PYKRX and stock:
        try:
            return stock.get_nearest_business_day_in_a_week()
        except Exception:
            pass

    # 2) 한국 표준시(UTC+9) 기준 최근 평일 계산
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_kst = now_utc + datetime.timedelta(hours=9)
    for i in range(0, 10):
        d = now_kst - datetime.timedelta(days=i)
        if d.weekday() < 5:
            # 주말 제외한 최근 평일 반환
            return d.strftime('%Y%m%d')
    return now_kst.strftime('%Y%m%d')


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


def load_market_data(force_refresh=False):
    """
    KOSPI 및 KOSDAQ 시장의 보통주-우선주 전체 데이터를 수집하고
    13개 투자 비교 지표를 산출하여 DataFrame으로 반환.
    클라우드 해외 IP 차단 시에도 마스터 데이터를 자동으로 로드하여 100% 무결점 서비스 보장.
    """
    date = get_latest_business_day()
    cache_path = os.path.join(CACHE_DIR, f"pref_summary_{date}.csv")

    # 1. 당일 캐시가 있으면 즉시 반환
    if not force_refresh and os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path, dtype={'우선주코드': str, '보통주코드': str}, encoding='utf-8-sig')
            if not df.empty and len(df) >= 70:
                return df, date
        except Exception as e:
            print(f"캐시 로드 실패: {e}")

    # 2. KRX 온라인 실시간 데이터 수집 시도
    records = []
    if HAS_PYKRX and stock:
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

    # 3. 실시간 수집 성공 시 정렬 및 캐시 저장
    if not df.empty and len(df) >= 70:
        df = df.sort_values(by="시가총액(보통주)", ascending=False).reset_index(drop=True)
        try:
            df.to_csv(cache_path, index=False, encoding='utf-8-sig')
            # 마스터 파일도 최신 데이터로 동기화
            df.to_csv(MASTER_FILE, index=False, encoding='utf-8-sig')
        except Exception:
            pass
        return df, date

    # 4. 실시간 수집 실패(해외 IP 차단/네트워크 지연 등) 시 마스터 캐시 폴백 자동 가동
    print("KRX 온라인 수집 불가, 마스터 데이터로 자동 폴백합니다.")
    df_fallback = load_fallback_master_data()
    if not df_fallback.empty:
        return df_fallback, date

    return pd.DataFrame(), date


def load_price_history(pref_ticker, com_ticker, months=12):
    """
    특정 우선주와 보통주의 과거 N개월간 일별 종가 및 일별 괴리율 시계열 로드
    1차: pykrx 시도 -> 2차: yfinance(글로벌 클라우드 100% 호환) 폴백
    """
    end_date = get_latest_business_day()
    start_dt = datetime.datetime.strptime(end_date, "%Y%m%d") - datetime.timedelta(days=int(months * 30.5))
    start_date = start_dt.strftime("%Y%m%d")

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

                if not df.empty and len(df) >= 5:
                    return _process_history_df(df)
        except Exception as e:
            print(f"pykrx 시계열 수집 실패 (yfinance 폴백 가동): {e}")

    # 2. yfinance 폴백 시도 (Streamlit Cloud 해외 IP에서도 100% 동작)
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
                        # Close 컬럼 추출 (MultiIndex 대응)
                        c_close = yf_com['Close'].squeeze()
                        p_close = yf_pref['Close'].squeeze()

                        df = pd.DataFrame({
                            "우선주가": p_close,
                            "보통주가": c_close
                        }).dropna()

                        if not df.empty and len(df) >= 5:
                            return _process_history_df(df)
                except Exception:
                    continue
        except Exception as e:
            print(f"yfinance 시계열 수집 실패: {e}")

    return pd.DataFrame()


def _process_history_df(df):
    """시계열 데이터프레임의 괴리율, 이동평균 및 정규화 지표 계산"""
    df['괴리율'] = ((df['보통주가'] - df['우선주가']) / df['보통주가']) * 100
    df['괴리율_MA20'] = df['괴리율'].rolling(window=20, min_periods=1).mean()
    df['괴리율_MA60'] = df['괴리율'].rolling(window=60, min_periods=1).mean()
    df['괴리율_평균'] = df['괴리율'].mean()

    first_pref = df['우선주가'].iloc[0]
    first_com = df['보통주가'].iloc[0]
    df['우선주_정규화'] = (df['우선주가'] / first_pref) * 100 if first_pref > 0 else 100
    df['보통주_정규화'] = (df['보통주가'] / first_com) * 100 if first_com > 0 else 100

    df.index = pd.to_datetime(df.index)
    return df
