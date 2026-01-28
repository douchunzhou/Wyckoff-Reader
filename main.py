import os
import time
import requests
from datetime import datetime, timedelta, timezone
import pandas as pd
import akshare as ak
import baostock as bs
import mplfinance as mpf
from openai import OpenAI
import numpy as np
import markdown
from xhtml2pdf import pisa
from sheet_manager import SheetManager

import json
import random
import re
from typing import Optional

# ==========================================
# 0) Gemini 稳定性增强：429 退避 + 致命错误熔断 + 防断连
# ==========================================

class GeminiQuotaExceeded(Exception):
    """按天/按项目配额耗尽：等待无效，应切 OpenAI。"""
    pass

class GeminiRateLimited(Exception):
    """短期速率限制：可退避重试。"""
    pass

class GeminiFatalError(Exception):
    """致命错误（如 API Key 无效）：绝对不可重试。"""
    pass

def _extract_retry_seconds(resp: requests.Response) -> int:
    ra = resp.headers.get("Retry-After")
    if ra:
        try: return max(1, int(float(ra)))
        except: pass
    text = resp.text or ""
    m = re.search(r"retry in\s+([\d\.]+)\s*s", text, re.IGNORECASE)
    if m: return max(1, int(float(m.group(1))))
    try:
        msg = ((resp.json().get("error", {}) or {}).get("message", "") or "")
        m2 = re.search(r"retry in\s+([\d\.]+)\s*s", msg, re.IGNORECASE)
        if m2: return max(1, int(float(m2.group(1))))
    except: pass
    return 0

def _is_quota_exhausted(resp: requests.Response) -> bool:
    text = (resp.text or "").lower()
    if ("quota exceeded" in text) or ("exceeded your current quota" in text): return True
    if ("free_tier" in text) and ("limit" in text): return True
    try:
        msg = (((resp.json().get("error", {}) or {}).get("message", "")) or "").lower()
        if ("quota exceeded" in msg) or ("exceeded your current quota" in msg): return True
    except: pass
    return False

def call_gemini_http(prompt: str) -> str:
    """第一优先级：Google 官方 API"""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: raise ValueError("GEMINI_API_KEY missing")

    model_name = os.getenv("GEMINI_MODEL") or "gemini-3-flash-preview"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    session = requests.Session()
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Connection": "close"
    }

    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "system_instruction": {"parts": [{"text": "You are Richard D. Wyckoff."}]},
        "generationConfig": {"temperature": 0.2},
        "safetySettings": safety_settings,
    }

    max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "1"))
    base_sleep = float(os.getenv("GEMINI_BASE_SLEEP", "3.0"))
    timeout_s = int(os.getenv("GEMINI_TIMEOUT", "120"))

    last_err: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.post(url, headers=headers, json=data, timeout=timeout_s)

            if resp.status_code == 200:
                result = resp.json()
                try:
                    return result["candidates"][0]["content"]["parts"][0]["text"]
                except:
                    raise ValueError(f"Invalid response: {str(result)[:200]}")
            
            if resp.status_code == 400:
                raise GeminiFatalError(f"Gemini Key/Params Error (400): {resp.text[:200]}")

            if resp.status_code == 429:
                if _is_quota_exhausted(resp):
                    raise GeminiQuotaExceeded(resp.text[:200])
                
                retry_s = _extract_retry_seconds(resp)
                if retry_s <= 0:
                    retry_s = int(base_sleep * (2 ** (attempt - 1)) + random.random())

                if attempt == max_retries:
                    raise GeminiRateLimited(resp.text[:200])

                print(f"    ⚠️ Gemini 429限流，等待 {retry_s}s ({attempt}/{max_retries})", flush=True)
                time.sleep(retry_s)
                continue

            if resp.status_code == 503:
                retry_s = int(base_sleep * (2 ** (attempt - 1)) + random.random())
                print(f"    ⚠️ Gemini 503过载，等待 {retry_s}s ({attempt}/{max_retries})", flush=True)
                time.sleep(retry_s)
                continue

            raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")

        except GeminiFatalError: raise 
        except GeminiQuotaExceeded: raise 
        except Exception as e:
            last_err = e
            if attempt == max_retries: raise
            retry_s = int(base_sleep * (2 ** (attempt - 1)) + random.random())
            print(f"    ⚠️ Gemini 异常: {str(e)[:100]}... 等待 {retry_s}s ({attempt}/{max_retries})", flush=True)
            time.sleep(retry_s)

    raise last_err or Exception("Gemini Unknown Failure")


# ==========================================
# 1. 数据获取模块 (稳定性增强：AkShare 重试 + 延迟)
# ==========================================

def _get_baostock_code(symbol: str) -> str:
    if symbol.startswith("6"): return f"sh.{symbol}"
    if symbol.startswith("0") or symbol.startswith("3"): return f"sz.{symbol}"
    if symbol.startswith("8") or symbol.startswith("4"): return f"bj.{symbol}"
    return f"sz.{symbol}"

def _detect_and_fix_volume_units(df_bs: pd.DataFrame, df_ak: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df_bs.empty and not df_ak.empty:
        v = df_ak["volume"].dropna()
        if len(v) < 50:
            df_ak = df_ak.copy()
            df_ak["volume"] *= 100
            return df_bs, df_ak
        mod100 = float((v % 100 == 0).mean())
        if mod100 > 0.9:
            return df_bs, df_ak
        df_ak = df_ak.copy()
        df_ak["volume"] *= 100
        return df_bs, df_ak

    if df_bs.empty or df_ak.empty:
        return df_bs, df_ak

    a = df_bs[["date", "volume"]].dropna()
    b = df_ak[["date", "volume"]].dropna()
    m = a.merge(b, on="date", how="inner", suffixes=("_bs", "_ak"))
    m = m[(m["volume_bs"] > 0) & (m["volume_ak"] > 0)]

    if len(m) < 10: 
        return df_bs, df_ak

    m = m.tail(200) 
    ratio_med = float((m["volume_bs"] / m["volume_ak"]).median())

    def _in(r, center, tol=0.25):
        return (center*(1-tol)) <= r <= (center*(1+tol))

    df_ak = df_ak.copy() 
    df_bs = df_bs.copy()

    if _in(ratio_med, 1000):
        df_ak["volume"] *= 1000
    elif _in(ratio_med, 100):
        df_ak["volume"] *= 100
    elif _in(ratio_med, 0.001):
        df_bs["volume"] *= 1000
    elif _in(ratio_med, 0.01):
        df_bs["volume"] *= 100
    return df_bs, df_ak

def fetch_stock_data_dynamic(symbol: str, timeframe_str: str, bar_count_str: str) -> dict:
    clean_digits = ''.join(filter(str.isdigit, str(symbol)))
    symbol_code = clean_digits.zfill(6)
    
    try: tf_min = int(timeframe_str)
    except: tf_min = 5
    
    try: limit = int(bar_count_str)
    except: limit = 500

    if tf_min not in [1, 5, 15, 30, 60]:
        tf_min = 60
    
    total_minutes = limit * tf_min
    days_back = int((total_minutes / 240) * 2.5) + 10 
    
    start_date_dt = datetime.now() - timedelta(days=days_back)
    start_date_str = start_date_dt.strftime("%Y-%m-%d")
    start_date_ak_str = start_date_dt.strftime("%Y%m%d")
    
    source_msg = "AkShare Only" if tf_min == 1 else "BaoStock+AkShare"
    print(f"    🔍 获取 {symbol_code}: 周期={tf_min}m, 目标={limit}根 ({source_msg})", flush=True)

    # === A. BaoStock 历史 ===
    df_bs = pd.DataFrame()
    if tf_min >= 5:
        try:
            bs_code = _get_baostock_code(symbol_code)
            lg = bs.login()
            if lg.error_code == '0':
                rs = bs.query_history_k_data_plus(
                    bs_code, "date,time,open,high,low,close,volume",
                    start_date=start_date_str, end_date=datetime.now().strftime("%Y-%m-%d"),
                    frequency=str(tf_min), adjustflag="3"
                )
                if rs.error_code == '0':
                    data_list = []
                    while rs.next(): data_list.append(rs.get_row_data())
                    df_bs = pd.DataFrame(data_list, columns=rs.fields)
                    
                    if not df_bs.empty:
                        df_bs["date"] = pd.to_datetime(df_bs["time"], format="%Y%m%d%H%M%S000", errors="coerce")
                        df_bs = df_bs.drop(columns=["time"], errors="ignore")
                        cols = ["open", "high", "low", "close", "volume"]
                        for c in cols: df_bs[c] = pd.to_numeric(df_bs[c], errors="coerce")
                        df_bs = df_bs.dropna(subset=["date", "close"])
                        df_bs = df_bs[["date", "open", "high", "low", "close", "volume"]]
            bs.logout()
        except Exception as e:
            print(f"    [BaoStock] 异常: {e}", flush=True)

    # === B. AkShare 数据 (增加稳定性重试逻辑) ===
    ak_fetch_start = start_date_ak_str if tf_min == 1 else (datetime.now() - timedelta(days=20)).strftime("%Y%m%d")
    df_ak = pd.DataFrame()
    
    max_ak_retries = 3
    for ak_attempt in range(1, max_ak_retries + 1):
        try:
            # 1. 模拟人工：请求前随机微调 1-3 秒
            time.sleep(random.uniform(1.0, 3.0))
            
            df_temp = ak.stock_zh_a_hist_min_em(symbol=symbol_code, period=str(tf_min), start_date=ak_fetch_start, adjust="qfq")
            
            if not df_temp.empty:
                df_ak = df_temp
                break # 成功则退出重试
        except Exception as e:
            err_msg = str(e)
            if "RemoteDisconnected" in err_msg or "Connection aborted" in err_msg:
                wait_s = ak_attempt * 5 + random.random()
                print(f"    ⚠️ AkShare 连接中断 ({ak_attempt}/{max_ak_retries})，等待 {wait_s:.1f}s 重试...", flush=True)
                time.sleep(wait_s)
            else:
                print(f"    [AkShare] 未知异常: {err_msg}", flush=True)
                break

    if not df_ak.empty:
        rename_map = {
            "时间": "date", "开盘": "open", "最高": "high", "最低": "low", 
            "收盘": "close", "成交量": "volume"
        }
        df_ak = df_ak.rename(columns={k: v for k, v in rename_map.items() if k in df_ak.columns})
        df_ak["date"] = pd.to_datetime(df_ak["date"], errors="coerce")
        cols = ["open", "high", "low", "close", "volume"]
        for c in cols: df_ak[c] = pd.to_numeric(df_ak[c], errors="coerce")
        df_ak["open"] = df_ak["open"].replace(0, np.nan)
        df_ak["open"] = df_ak["open"].fillna(df_ak["close"].shift(1)).fillna(df_ak["close"])
        df_ak = df_ak.dropna(subset=["date", "close"])
        df_ak = df_ak[["date", "open", "high", "low", "close", "volume"]]

    # === C. 合并与单位修正 ===
    if df_bs.empty and df_ak.empty:
        return {"df": pd.DataFrame(), "period": f"{tf_min}m"}
    
    df_bs, df_ak = _detect_and_fix_volume_units(df_bs, df_ak)
    df_final = pd.concat([df_bs, df_ak], axis=0, ignore_index=True)
    df_final = df_final.drop_duplicates(subset=['date'], keep='last')
    df_final = df_final.sort_values(by='date').reset_index(drop=True)
    
    if len(df_final) > limit:
        df_final = df_final.tail(limit).reset_index(drop=True)
    return {"df": df_final, "period": f"{tf_min}m"}

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "close" in df.columns:
        df["ma50"] = df["close"].rolling(50).mean()
        df["ma200"] = df["close"].rolling(200).mean()
    return df

# ==========================================
# 2. 绘图模块
# ==========================================

def generate_local_chart(symbol: str, df: pd.DataFrame, save_path: str, period: str):
    if df.empty: return
    plot_df = df.copy()
    if "date" in plot_df.columns: plot_df.set_index("date", inplace=True)

    mc = mpf.make_marketcolors(up='#ff3333', down='#00b060', edge='inherit', wick='inherit', volume={'up': '#ff3333', 'down': '#00b060'}, inherit=True)
    s = mpf.make_mpf_style(base_mpf_style='yahoo', marketcolors=mc, gridstyle=':', y_on_right=True)
    apds = []
    if 'ma50' in plot_df.columns: apds.append(mpf.make_addplot(plot_df['ma50'], color='#ff9900', width=1.5))
    if 'ma200' in plot_df.columns: apds.append(mpf.make_addplot(plot_df['ma200'], color='#2196f3', width=2.0))

    try:
        mpf.plot(plot_df, type='candle', style=s, addplot=apds, volume=True, 
                 title=f"Wyckoff: {symbol} ({period} | {len(plot_df)} bars)", 
                 savefig=dict(fname=save_path, dpi=150, bbox_inches='tight'), 
                 warn_too_much_data=2000)
    except Exception as e:
        print(f"    [Error] 绘图失败: {e}", flush=True)


# ==========================================
# 3. AI 分析模块 (三级兜底)
# ==========================================

_PROMPT_CACHE = None

def get_prompt_content(symbol, df, position_info):
    global _PROMPT_CACHE
    if _PROMPT_CACHE is None:
        prompt_template = os.getenv("WYCKOFF_PROMPT_TEMPLATE")
        if not prompt_template and os.path.exists("prompt_secret.txt"):
            try:
                with open("prompt_secret.txt", "r", encoding="utf-8") as f: prompt_template = f.read()
            except: prompt_template = None
        _PROMPT_CACHE = prompt_template

    if not _PROMPT_CACHE: return None
    csv_data = df.to_csv(index=False)
    latest = df.iloc[-1]
    period_str = position_info.get('timeframe', '5') + "m"
    
    base_prompt = (_PROMPT_CACHE
        .replace("{symbol}", symbol)
        .replace("{latest_time}", str(latest["date"]))
        .replace("{latest_price}", str(latest["close"]))
        .replace("{csv_data}", csv_data)
    )

    def safe_get(key):
        val = position_info.get(key)
        return 'N/A' if val is None or str(val).lower() == 'nan' or str(val).strip() == '' else val

    buy_date = safe_get('date')
    buy_price = safe_get('price')
    qty = safe_get('qty')

    position_text = (
        f"\n\n[USER POSITION DATA]\n"
        f"Symbol: {symbol}\n"
        f"Timeframe: {period_str}\n" 
        f"Buy Date: {buy_date}\n"
        f"Cost Price: {buy_price}\n"
        f"Quantity: {qty}\n"
        f"(Note: Please analyze the current trend based on this position data and timeframe.)"
    )
    return base_prompt + position_text

def call_openai_official(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key: raise ValueError("OPENAI_API_KEY missing")
    model_name = os.getenv("AI_MODEL", "gpt-4o")
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "system", "content": "You are Richard D. Wyckoff."}, {"role": "user", "content": prompt}],
        temperature=0.2
    )
    return resp.choices[0].message.content

def call_custom_api(prompt: str) -> str:
    api_key = os.getenv("CUSTOM_API_KEY") 
    if not api_key: raise ValueError("CUSTOM_API_KEY missing")
    base_url = "https://api2.qiandao.mom/v1"
    model_name = "DeepSeek-V3.2-a"
    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "system", "content": "You are Richard D. Wyckoff."}, {"role": "user", "content": prompt}],
        temperature=0.2
    )
    return resp.choices[0].message.content

def ai_analyze(symbol, df, position_info):
    prompt = get_prompt_content(symbol, df, position_info)
    if not prompt: return "Error: No Prompt"
    try:
        return call_gemini_http(prompt)
    except Exception as e1:
        print(f"    ⚠️ Gemini Official 失败: {str(e1)[:100]} -> 切 Custom API", flush=True)
        try:
            return call_custom_api(prompt)
        except Exception as e2:
            print(f"    ⚠️ Custom API 失败: {str(e2)[:100]} -> 切 OpenAI", flush=True)
            try:
                return call_openai_official(prompt)
            except Exception as e3:
                return f"Analysis Failed. All APIs down. Error: {e3}"

# ==========================================
# 4. PDF 生成模块
# ==========================================

def generate_pdf_report(symbol, chart_path, report_text, pdf_path):
    html_content = markdown.markdown(report_text)
    abs_chart_path = os.path.abspath(chart_path)
    font_path = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
    if not os.path.exists(font_path): font_path = "msyh.ttc"

    full_html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @font-face {{ font-family: "MyChineseFont"; src: url("{font_path}"); }}
            @page {{ size: A4; margin: 1cm; }}
            body {{ font-family: "MyChineseFont", sans-serif; font-size: 12px; line-height: 1.5; }}
            h1, h2, h3, p, div {{ font-family: "MyChineseFont", sans-serif; color: #2c3e50; }}
            img {{ width: 18cm; margin-bottom: 20px; }}
            .header {{ text-align: center; margin-bottom: 20px; color: #7f8c8d; font-size: 10px; }}
        </style>
    </head>
    <body>
        <div class="header">Wyckoff Quantitative Analysis | {symbol}</div>
        <img src="{abs_chart_path}" />
        <hr/>
        {html_content}
    </body>
    </html>
    """
    try:
        with open(pdf_path, "wb") as pdf_file: pisa.CreatePDF(full_html, dest=pdf_file)
        return True
    except: return False

# ==========================================
# 5. 主程序 (串行 + 30s 休息)
# ==========================================

def process_one_stock(symbol: str, position_info: dict):
    if position_info is None: position_info = {}
    clean_digits = ''.join(filter(str.isdigit, str(symbol)))
    clean_symbol = clean_digits.zfill(6)
    tf_str = position_info.get("timeframe", "5")
    bars_str = position_info.get("bars", "500")

    print(f"🚀 [{clean_symbol}] 开始分析 (TF:{tf_str}m, Bars:{bars_str})...", flush=True)
    data_res = fetch_stock_data_dynamic(clean_symbol, tf_str, bars_str)
    df = data_res["df"]
    period = data_res["period"]

    if df.empty:
        print(f"    ⚠️ [{clean_symbol}] 数据为空，跳过", flush=True)
        return None

    df = add_indicators(df)
    beijing_tz = timezone(timedelta(hours=8))
    ts = datetime.now(beijing_tz).strftime("%Y%m%d_%H%M%S")

    chart_path = f"reports/{clean_symbol}_chart_{ts}.png"
    pdf_path = f"reports/{clean_symbol}_report_{period}_{ts}.pdf"

    generate_local_chart(clean_symbol, df, chart_path, period)
    report_text = ai_analyze(clean_symbol, df, position_info)

    if generate_pdf_report(clean_symbol, chart_path, report_text, pdf_path):
        print(f"✅ [{clean_symbol}] 报告生成完毕", flush=True)
        return pdf_path
    return None

def main():
    os.makedirs("data", exist_ok=True)
    os.makedirs("reports", exist_ok=True)
    print("☁️ 正在连接 Google Sheets...", flush=True)
    try:
        sm = SheetManager()
        stocks_dict = sm.get_all_stocks()
        print(f"📋 获取 {len(stocks_dict)} 个任务", flush=True)
    except Exception as e:
        print(f"❌ Sheet 连接失败: {e}", flush=True)
        return

    generated_pdfs = []
    items = list(stocks_dict.items())
    for i, (symbol, info) in enumerate(items):
        try:
            pdf_path = process_one_stock(symbol, info)
            if pdf_path: generated_pdfs.append(pdf_path)
        except Exception as e:
            print(f"❌ [{symbol}] 处理发生异常: {e}", flush=True)

        if i < len(items) - 1:
            print("⏳ 强制冷却 30秒...", flush=True)
            time.sleep(30)

    if generated_pdfs:
        with open("push_list.txt", "w", encoding="utf-8") as f:
            for pdf in generated_pdfs: f.write(f"{pdf}\n")
    else:
        print("\n⚠️ 无报告生成", flush=True)

if __name__ == "__main__":
    main()




