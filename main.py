import os
import time
import requests
from datetime import datetime, timedelta, timezone
import pandas as pd
import akshare as ak
import baostock as bs  # ✅ 引入 BaoStock
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
# 0) Gemini 稳定性增强：429 退避重试
# ==========================================

class GeminiQuotaExceeded(Exception):
    """按天/按项目配额耗尽：等待无效，应切 OpenAI。"""
    pass

class GeminiRateLimited(Exception):
    """短期速率限制：可退避重试。"""
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
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: raise ValueError("GEMINI_API_KEY missing")

    model_name = os.getenv("GEMINI_MODEL") or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    session = requests.Session()
    headers = {"Content-Type": "application/json"}
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

    max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "8"))
    base_sleep = float(os.getenv("GEMINI_BASE_SLEEP", "2.5"))
    timeout_s = int(os.getenv("GEMINI_TIMEOUT", "300"))
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

            if resp.status_code == 429:
                if _is_quota_exhausted(resp):
                    raise GeminiQuotaExceeded(resp.text[:200])
                
                retry_s = _extract_retry_seconds(resp)
                if retry_s <= 0:
                    retry_s = int(base_sleep * (2 ** (attempt - 1)) + random.random() * 2)

                if attempt == max_retries:
                    raise GeminiRateLimited(resp.text[:200])

                print(f"   ⚠️ Gemini 429限流，等待 {retry_s}s ({attempt}/{max_retries})", flush=True)
                time.sleep(retry_s)
                continue

            if resp.status_code == 503:
                retry_s = int(base_sleep * (2 ** (attempt - 1)) + random.random() * 2)
                print(f"   ⚠️ Gemini 503过载，等待 {retry_s}s", flush=True)
                time.sleep(retry_s)
                continue

            raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")

        except GeminiQuotaExceeded: raise
        except Exception as e:
            last_err = e
            if attempt == max_retries: raise
            retry_s = int(base_sleep * (2 ** (attempt - 1)) + random.random())
            print(f"   ⚠️ Gemini 异常: {str(e)[:100]}... 等待 {retry_s}s", flush=True)
            time.sleep(retry_s)

    raise last_err or Exception("Gemini Unknown Failure")


# ==========================================
# 1. 数据获取模块 (BaoStock历史 + AkShare实时 + 自动对齐)
# ==========================================


def _get_baostock_code(symbol: str) -> str:
    if symbol.startswith("6"): return f"sh.{symbol}"
    if symbol.startswith("0") or symbol.startswith("3"): return f"sz.{symbol}"
    if symbol.startswith("8") or symbol.startswith("4"): return f"bj.{symbol}"
    return f"sz.{symbol}"

def fetch_stock_data_dynamic(symbol: str, timeframe_str: str, bar_count_str: str) -> dict:
    clean_digits = ''.join(filter(str.isdigit, str(symbol)))
    symbol_code = clean_digits.zfill(6)
    
    # 1. 参数解析 (支持自定义)
    try: tf_min = int(timeframe_str)
    except: tf_min = 5
    
    try: limit = int(bar_count_str)
    except: limit = 500

    if tf_min not in [5, 15, 30, 60]:
        print(f"   ⚠️ 周期 {tf_min} 非标准(BaoStock仅支持5/15/30/60)，调整为 60", flush=True)
        tf_min = 60
    
    # 计算需要的历史天数
    total_minutes = limit * tf_min
    days_back = int((total_minutes / 240) * 2.5) + 30 
    start_date_str = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    print(f"   🔍 获取 {symbol_code}: 周期={tf_min}m, 目标={limit}根 (BaoStock+AkShare)", flush=True)

    # === A. BaoStock 历史 ===
    df_bs = pd.DataFrame()
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
                    df_bs["time"] = pd.to_datetime(df_bs["time"], format="%Y%m%d%H%M%S000", errors='coerce')
                    df_bs = df_bs.rename(columns={"time": "date"})
                    cols = ["open", "high", "low", "close", "volume"]
                    df_bs[cols] = df_bs[cols].apply(pd.to_numeric, errors='coerce')
                    df_bs = df_bs.dropna(subset=['close'])
        bs.logout()
    except Exception as e:
        print(f"   [BaoStock] 异常: {e}", flush=True)

    # === B. AkShare 实时补全 ===
    df_ak = pd.DataFrame()
    try:
        ak_start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        df_ak = ak.stock_zh_a_hist_min_em(symbol=symbol_code, period=str(tf_min), start_date=ak_start, adjust="qfq")
        if not df_ak.empty:
            rename_map = {"时间": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume"}
            df_ak = df_ak.rename(columns={k: v for k, v in rename_map.items() if k in df_ak.columns})
            df_ak["date"] = pd.to_datetime(df_ak["date"])
            cols = ["open", "high", "low", "close", "volume"]
            df_ak[cols] = df_ak[cols].astype(float)
            if "open" in df_ak.columns:
                df_ak["open"] = df_ak["open"].replace(0, np.nan)
                df_ak["open"] = df_ak["open"].fillna(df_ak["close"].shift(1)).fillna(df_ak["close"])
    except Exception as e:
        print(f"   [AkShare] 异常: {e}", flush=True)

    # === C. 合并与清洗 ===
    if df_bs.empty and df_ak.empty:
        return {"df": pd.DataFrame(), "period": f"{tf_min}m"}
    
    # 自动对齐单位 (手 vs 股)
    if not df_bs.empty and not df_ak.empty:
        mean_bs = df_bs['volume'].mean()
        mean_ak = df_ak['volume'].mean()
        if mean_bs > 0 and mean_ak > 0:
            ratio = mean_bs / mean_ak
            if 80 < ratio < 120:
                print(f"   ⚖️ 修正 AkShare 单位 (x100)", flush=True)
                df_ak['volume'] = df_ak['volume'] * 100
            elif 0.008 < ratio < 0.012:
                print(f"   ⚖️ 修正 BaoStock 单位 (x100)", flush=True)
                df_bs['volume'] = df_bs['volume'] * 100

    df_final = pd.concat([df_bs, df_ak], axis=0)
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
        print(f"   [Error] 绘图失败: {e}", flush=True)


# ==========================================
# 3. AI 分析模块
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
    if not api_key: raise ValueError("OpenAI Key missing")
    model_name = os.getenv("AI_MODEL", "gpt-4o")
    client = OpenAI(api_key=api_key)
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
    except GeminiQuotaExceeded as qe:
        print(f"   ⚠️ [{symbol}] Gemini 配额耗尽 -> OpenAI", flush=True)
        try: return call_openai_official(prompt)
        except Exception as e2: return f"Analysis Failed. OpenAI Err: {e2}"
    except GeminiRateLimited as rl:
        print(f"   ⚠️ [{symbol}] Gemini 限流重试失败 -> OpenAI", flush=True)
        try: return call_openai_official(prompt)
        except Exception as e2: return f"Analysis Failed. OpenAI Err: {e2}"
    except Exception as e:
        print(f"   ⚠️ [{symbol}] Gemini 异常 -> OpenAI: {str(e)[:100]}", flush=True)
        try: return call_openai_official(prompt)
        except Exception as e2: return f"Analysis Failed. OpenAI Err: {e2}"


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
# 5. 主程序 (串行 + 强制休息)
# ==========================================

def process_one_stock(symbol: str, position_info: dict):
    if position_info is None: position_info = {}
    clean_digits = ''.join(filter(str.isdigit, str(symbol)))
    clean_symbol = clean_digits.zfill(6)

    # 🟢 获取自定义配置
    tf_str = position_info.get("timeframe", "5")
    bars_str = position_info.get("bars", "500")

    print(f"🚀 [{clean_symbol}] 开始分析 (TF:{tf_str}m, Bars:{bars_str})...", flush=True)

    # 🟢 调用双源数据获取
    data_res = fetch_stock_data_dynamic(clean_symbol, tf_str, bars_str)
    
    df = data_res["df"]
    period = data_res["period"]

    if df.empty:
        print(f"   ⚠️ [{clean_symbol}] 数据为空，跳过", flush=True)
        return None

    df = add_indicators(df)

    beijing_tz = timezone(timedelta(hours=8))
    ts = datetime.now(beijing_tz).strftime("%Y%m%d_%H%M%S")

    csv_path = f"data/{clean_symbol}_{period}_{ts}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

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
            if pdf_path:
                generated_pdfs.append(pdf_path)
        except Exception as e:
            print(f"❌ [{symbol}] 处理发生异常: {e}", flush=True)

        if i < len(items) - 1:
            print("⏳ 强制冷却 60秒...", flush=True)
            time.sleep(60)

    if generated_pdfs:
        print(f"\n📝 生成推送清单 ({len(generated_pdfs)}):", flush=True)
        with open("push_list.txt", "w", encoding="utf-8") as f:
            for pdf in generated_pdfs:
                print(f"   -> {pdf}")
                f.write(f"{pdf}\n")
    else:
        print("\n⚠️ 无报告生成", flush=True)

if __name__ == "__main__":
    main()
