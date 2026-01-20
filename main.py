import os
import time
import requests
from datetime import datetime, timedelta, timezone
import pandas as pd
import akshare as ak
import mplfinance as mpf
from openai import OpenAI
import numpy as np
import markdown
from xhtml2pdf import pisa
from sheet_manager import SheetManager 

# ==========================================
# 1. 数据获取模块 (保持 zfill 补全逻辑)
# ==========================================

def fetch_stock_data_dynamic(symbol: str, buy_date_str: str) -> dict:
    # 强制补全6位
    clean_digits = ''.join(filter(str.isdigit, str(symbol)))
    symbol_code = clean_digits.zfill(6)
    
    print(f"   -> 正在分析 {symbol_code} (买入日期: {buy_date_str})...")

    # 计算时间窗口
    try:
        if buy_date_str and str(buy_date_str) != 'nan' and len(str(buy_date_str)) >= 10:
            buy_dt = datetime.strptime(str(buy_date_str)[:10], "%Y-%m-%d")
            start_dt = buy_dt - timedelta(days=15) 
            start_date_em = start_dt.strftime("%Y%m%d")
        else:
            start_date_em = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
    except:
        start_date_em = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")

    # 获取数据 (5min)
    try:
        df = ak.stock_zh_a_hist_min_em(symbol=symbol_code, period="5", start_date=start_date_em, adjust="qfq")
    except Exception as e:
        print(f"   [Error] 5min接口报错: {e}")
        return {"df": pd.DataFrame(), "period": "5m"}

    if df.empty:
        return {"df": pd.DataFrame(), "period": "5m"}

    # 策略切换 (15min)
    current_period = "5m"
    if len(df) > 960:
        print(f"   [策略] 数据量大，切换至 15min...")
        try:
            df_15 = ak.stock_zh_a_hist_min_em(symbol=symbol_code, period="15", adjust="qfq")
            rename_map = {"时间": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume"}
            df_15 = df_15.rename(columns={k: v for k, v in rename_map.items() if k in df_15.columns})
            df = df_15.tail(960).reset_index(drop=True) 
            current_period = "15m"
        except:
            df = df.tail(960)

    # 清洗
    rename_map = {"时间": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume"}
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    
    if "date" in df.columns: df["date"] = pd.to_datetime(df["date"])
    cols = ["open", "high", "low", "close", "volume"]
    valid_cols = [c for c in cols if c in df.columns]
    df[valid_cols] = df[valid_cols].astype(float)

    if "open" in df.columns and (df["open"] == 0).any():
        df["open"] = df["open"].replace(0, np.nan)
        if "close" in df.columns:
            df["open"] = df["open"].fillna(df["close"].shift(1)).fillna(df["close"])

    return {"df": df, "period": current_period}

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
        mpf.plot(plot_df, type='candle', style=s, addplot=apds, volume=True, title=f"Wyckoff: {symbol} ({period})", savefig=dict(fname=save_path, dpi=150, bbox_inches='tight'), warn_too_much_data=2000)
    except Exception as e:
        print(f"   [Error] 绘图失败: {e}")

# ==========================================
# 3. AI 分析模块 (纯文本注入 + 强制报错逻辑)
# ==========================================

def get_prompt_content(symbol, df, position_info):
    # 1. 读取 Prompt 模板
    prompt_template = os.getenv("WYCKOFF_PROMPT_TEMPLATE")
    if not prompt_template and os.path.exists("prompt_secret.txt"):
        try:
            with open("prompt_secret.txt", "r", encoding="utf-8") as f:
                prompt_template = f.read()
        except: pass
    if not prompt_template: return None

    # 2. 准备基础数据
    csv_data = df.to_csv(index=False)
    latest = df.iloc[-1]

    # 3. 替换模板中的基础变量
    base_prompt = prompt_template.replace("{symbol}", symbol) \
                          .replace("{latest_time}", str(latest["date"])) \
                          .replace("{latest_price}", str(latest["close"])) \
                          .replace("{csv_data}", csv_data)
    
    # 4. === 核心修改：纯文本注入持仓信息 ===
    # 不做任何计算，直接把字典里的数据拼成字符串，贴在最后
    buy_date = position_info.get('date', 'N/A')
    buy_price = position_info.get('price', 'N/A')
    qty = position_info.get('qty', 'N/A')

    position_text = (
        f"\n\n[USER POSITION DATA]\n"
        f"Symbol: {symbol}\n"
        f"Buy Date: {buy_date}\n"
        f"Cost Price: {buy_price}\n"
        f"Quantity: {qty}\n"
        f"(Note: Please analyze the current trend based on this position cost.)"
    )
    
    return base_prompt + position_text

def call_gemini_http(prompt: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: raise ValueError("GEMINI_API_KEY missing")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp") 
    
    print(f"   >>> Gemini ({model_name})...")
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    headers = {'Content-Type': 'application/json'}
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "system_instruction": {"parts": [{"text": "You are Richard D. Wyckoff."}]},
        "generationConfig": {"temperature": 0.2}
    }
    
    resp = requests.post(url, headers=headers, json=data, timeout=30)
    
    if resp.status_code != 200: 
        raise Exception(f"Gemini API Error {resp.status_code}: {resp.text}")
    
    try:
        result = resp.json()
        content = result.get('candidates', [])[0].get('content', {})
        text = content.get('parts', [])[0].get('text', '')
        if not text: raise ValueError("Empty text")
        return text
    except Exception as e:
        print(f"   [Debug] Gemini 解析失败，原始响应: {resp.text[:100]}...")
        raise e 

def call_openai_official(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key: raise ValueError("OpenAI Key missing")
    model_name = os.getenv("AI_MODEL", "gpt-4o")
    print(f"   >>> 🔄 Switching to OpenAI ({model_name})...")
    
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
    except Exception as e: 
        print(f"   ⚠️ Gemini Error: {e}")
        try: return call_openai_official(prompt)
        except Exception as e2: return f"Analysis Failed: {e2}"

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
        with open(pdf_path, "wb") as pdf_file:
            pisa.CreatePDF(full_html, dest=pdf_file)
        return True
    except: return False

# ==========================================
# 5. 主程序
# ==========================================

def process_one_stock(symbol: str, position_info: dict, generated_files: list):
    clean_digits = ''.join(filter(str.isdigit, str(symbol)))
    clean_symbol = clean_digits.zfill(6)

    print(f"\n{'='*40}\n🚀 开始分析: {clean_symbol}\n{'='*40}")

    data_res = fetch_stock_data_dynamic(clean_symbol, position_info.get('date'))
    df = data_res["df"]
    period = data_res["period"]
    
    if df.empty:
        print(f"   [Skip] 数据为空")
        return
    df = add_indicators(df)

    beijing_tz = timezone(timedelta(hours=8))
    ts = datetime.now(beijing_tz).strftime("%Y%m%d_%H%M%S")
    
    chart_path = f"reports/{clean_symbol}_chart_{ts}.png"
    pdf_path = f"reports/{clean_symbol}_report_{period}_{ts}.pdf"
    
    generate_local_chart(clean_symbol, df, chart_path, period)
    report_text = ai_analyze(clean_symbol, df, position_info)
    
    if generate_pdf_report(clean_symbol, chart_path, report_text, pdf_path):
        generated_files.append(pdf_path)
    
    print(f"✅ {clean_symbol} 处理完成")

def main():
    os.makedirs("data", exist_ok=True)
    os.makedirs("reports", exist_ok=True)

    print("☁️ 正在连接 Google Sheets...")
    try:
        sm = SheetManager()
        stocks_dict = sm.get_all_stocks()
        print(f"📋 获取 {len(stocks_dict)} 个任务")
    except Exception as e:
        print(f"❌ Sheet 连接失败: {e}")
        return

    generated_pdfs = []
    for i, (symbol, info) in enumerate(stocks_dict.items()):
        try: process_one_stock(symbol, info, generated_pdfs)
        except Exception as e: print(f"❌ {symbol} 错误: {e}")
        if i < len(stocks_dict) - 1: time.sleep(5)

    if generated_pdfs:
        print(f"\n📝 生成推送清单 ({len(generated_pdfs)}):")
        with open("push_list.txt", "w", encoding="utf-8") as f:
            for pdf in generated_pdfs:
                print(f"   -> {pdf}")
                f.write(f"{pdf}\n")
    else:
        print("\n⚠️ 无报告生成")

if __name__ == "__main__":
    main()
