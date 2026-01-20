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
# 引入 Google Sheets 管理模块
from sheet_manager import SheetManager 

# ==========================================
# 1. 数据获取模块 (智能策略版)
# ==========================================

def fetch_stock_data_dynamic(symbol: str, buy_date_str: str) -> dict:
    """
    智能获取数据策略：
    1. 计算 start_date = buy_date - 15天 (覆盖买入前后的走势)
    2. 尝试获取 5分钟 K线
    3. 如果数据行数 > 960，则改抓最近 960 根 15分钟 K线
    """
    symbol_code = ''.join(filter(str.isdigit, symbol))
    print(f"   -> 正在分析 {symbol_code} (买入日期: {buy_date_str})...")

    # 1. 计算开始时间 (近似倒推15个自然日)
    try:
        if buy_date_str and buy_date_str != 'Unknown':
            buy_dt = datetime.strptime(buy_date_str, "%Y-%m-%d")
            start_dt = buy_dt - timedelta(days=15) 
            start_date_em = start_dt.strftime("%Y%m%d")
        else:
            # 如果没有买入日期，默认拉取最近15天
            start_date_em = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
    except Exception as e:
        print(f"   [Warn] 日期解析失败 ({buy_date_str}), 使用默认窗口: {e}")
        start_date_em = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")

    # 2. 尝试拉取 5分钟 K线 (指定开始时间)
    try:
        df = ak.stock_zh_a_hist_min_em(
            symbol=symbol_code, 
            period="5", 
            start_date=start_date_em,
            adjust="qfq"
        )
    except Exception as e:
        print(f"   [Error] 5min接口报错: {e}")
        return {"df": pd.DataFrame(), "period": "5m"}

    if df.empty:
        return {"df": pd.DataFrame(), "period": "5m"}

    # 3. 判断是否超过 960 根 (策略切换)
    current_period = "5m"
    if len(df) > 960:
        print(f"   [策略] 5分钟数据({len(df)}根)过长，切换至 15分钟 K线 (最近960根)...")
        try:
            # 15分钟线，不限制开始时间，直接拉取，然后截取
            df_15 = ak.stock_zh_a_hist_min_em(symbol=symbol_code, period="15", adjust="qfq")
            # 重命名列以确保统一
            rename_map = {"时间": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume"}
            df_15 = df_15.rename(columns={k: v for k, v in rename_map.items() if k in df_15.columns})
            
            df = df_15.tail(960).reset_index(drop=True) # 只取最近960根
            current_period = "15m"
        except Exception as e:
            print(f"   [Warn] 15min接口失败，回退5min截断: {e}")
            df = df.tail(960) # 还是用5min，但截断

    # 4. 数据清洗与重命名 (确保df结构正确)
    rename_map = {
        "时间": "date", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "volume"
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    df["date"] = pd.to_datetime(df["date"])
    cols = ["open", "high", "low", "close", "volume"]
    df[cols] = df[cols].astype(float)

    # 修复 Open=0
    if (df["open"] == 0).any():
        print(f"   [清洗] 修复 Open=0 数据...")
        df["open"] = df["open"].replace(0, np.nan)
        df["open"] = df["open"].fillna(df["close"].shift(1))
        df["open"] = df["open"].fillna(df["close"])

    return {"df": df, "period": current_period}

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma50"] = df["close"].rolling(50).mean()
    df["ma200"] = df["close"].rolling(200).mean()
    return df

# ==========================================
# 2. 绘图模块
# ==========================================

def generate_local_chart(symbol: str, df: pd.DataFrame, save_path: str, period: str):
    if df.empty: return

    plot_df = df.copy()
    plot_df.set_index("date", inplace=True)

    mc = mpf.make_marketcolors(
        up='#ff3333', down='#00b060', 
        edge='inherit', wick='inherit', 
        volume={'up': '#ff3333', 'down': '#00b060'},
        inherit=True
    )
    s = mpf.make_mpf_style(
        base_mpf_style='yahoo', 
        marketcolors=mc, 
        gridstyle=':', 
        y_on_right=True
    )

    apds = []
    if 'ma50' in plot_df.columns:
        apds.append(mpf.make_addplot(plot_df['ma50'], color='#ff9900', width=1.5))
    if 'ma200' in plot_df.columns:
        apds.append(mpf.make_addplot(plot_df['ma200'], color='#2196f3', width=2.0))

    title_text = f"Wyckoff Setup: {symbol} ({period})"
    
    try:
        mpf.plot(
            plot_df, type='candle', style=s, addplot=apds, volume=True,
            title=title_text,
            savefig=dict(fname=save_path, dpi=150, bbox_inches='tight'),
            warn_too_much_data=2000
        )
        print(f"   [OK] 图表已保存")
    except Exception as e:
        print(f"   [Error] 绘图失败: {e}")

# ==========================================
# 3. AI 分析模块 (持仓感知版)
# ==========================================

def get_prompt_content(symbol, df, position_info):
    """
    position_info: {'date': '...', 'qty': '...', 'price': '...'}
    """
    prompt_template = os.getenv("WYCKOFF_PROMPT_TEMPLATE")
    if not prompt_template and os.path.exists("prompt_secret.txt"):
        try:
            with open("prompt_secret.txt", "r", encoding="utf-8") as f:
                prompt_template = f.read()
        except: pass
    if not prompt_template: return None

    csv_data = df.to_csv(index=False)
    latest = df.iloc[-1]
    current_price = float(latest["close"])
    
    # === 新增：计算持仓盈亏并注入 Prompt ===
    try:
        buy_price = float(position_info.get('price', 0))
        buy_date = position_info.get('date', 'Unknown')
    except:
        buy_price = 0
    
    position_context = ""
    if buy_price > 0:
        pnl_pct = ((current_price - buy_price) / buy_price) * 100
        sign = "+" if pnl_pct >= 0 else ""
        position_context = (
            f"\n\n[USER POSITION INFO]\n"
            f"- Buy Date: {buy_date}\n"
            f"- Buy Price: {buy_price}\n"
            f"- Current PnL: {sign}{pnl_pct:.2f}%\n"
            f"IMPORTANT: The user currently holds this position. "
            f"Please give specific advice based on the profit/loss status (e.g., set stop loss, take profit, or hold)."
        )
    else:
        position_context = "\n\n[USER POSITION INFO]\nUser is watching this stock but has NO open position yet."

    # 替换模板变量
    final_prompt = prompt_template.replace("{symbol}", symbol) \
                          .replace("{latest_time}", str(latest["date"])) \
                          .replace("{latest_price}", str(latest["close"])) \
                          .replace("{csv_data}", csv_data)
    
    # 将持仓信息附加到最后
    return final_prompt + position_context

def call_gemini_http(prompt: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: raise ValueError("GEMINI_API_KEY missing")
    model_name = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
    print(f"   >>> Gemini ({model_name})...")
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    headers = {'Content-Type': 'application/json'}
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "system_instruction": {"parts": [{"text": "You are Richard D. Wyckoff. You follow strict Wyckoff logic."}]},
        "generationConfig": {"temperature": 0.2}
    }
    resp = requests.post(url, headers=headers, json=data)
    if resp.status_code != 200: raise Exception(f"Gemini API Error {resp.status_code}: {resp.text}")
    
    # 安全获取响应内容
    try:
        return resp.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        raise Exception(f"Gemini 响应解析失败: {resp.text}")

def call_openai_official(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key: raise ValueError("OPENAI_API_KEY missing")
    model_name = os.getenv("AI_MODEL", "gpt-4o")
    print(f"   >>> OpenAI ({model_name})...")
    
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
    
    try: return call_gemini_http(prompt)
    except Exception as e: 
        print(f"   [Warn] Gemini 失败: {e} -> 切换 OpenAI")
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
            pre {{ background: #f4f4f4; padding: 10px; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <div class="header">Wyckoff Quantitative Analysis Report | Generated by AI Agent</div>
        <img src="{abs_chart_path}" />
        <hr/>
        {html_content}
        <div style="text-align:right; color:#bdc3c7; font-size:8px;">Target: {symbol} | Data: EastMoney</div>
    </body>
    </html>
    """
    try:
        with open(pdf_path, "wb") as pdf_file:
            pisa.CreatePDF(full_html, dest=pdf_file)
        print(f"   [OK] PDF Generated: {pdf_path}")
        return True
    except Exception as e:
        print(f"   [Error] PDF 生成失败: {e}")
        return False

# ==========================================
# 5. 主程序
# ==========================================

def process_one_stock(symbol: str, position_info: dict, generated_files: list):
    """
    symbol: 股票代码
    position_info: {'date': '2025-01-01', 'qty': '100', 'price': '10.5'}
    """
    print(f"\n{'='*40}")
    print(f"🚀 开始分析: {symbol}")
    print(f"{'='*40}")

    # 1. 动态拉取数据 (5m 或 15m) - 传入买入日期
    data_res = fetch_stock_data_dynamic(symbol, position_info.get('date'))
    df = data_res["df"]
    period = data_res["period"]
    
    if df.empty:
        print(f"   [Skip] 数据为空，跳过 {symbol}")
        return
    df = add_indicators(df)

    # 2. 生成文件名 (北京时间) + 增加周期标识
    beijing_tz = timezone(timedelta(hours=8))
    ts = datetime.now(beijing_tz).strftime("%Y%m%d_%H%M%S")
    
    # 文件名增加 _{period}_ 标识
    csv_path = f"data/{symbol}_{period}_{ts}.csv"
    chart_path = f"reports/{symbol}_chart_{ts}.png"
    pdf_path = f"reports/{symbol}_report_{period}_{ts}.pdf"
    
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    
    # 画图时传入 period 标题
    generate_local_chart(symbol, df, chart_path, period)
    
    # 3. AI 分析 (传入持仓信息)
    report_text = ai_analyze(symbol, df, position_info)
    
    # 4. 生成 PDF
    if generate_pdf_report(symbol, chart_path, report_text, pdf_path):
        generated_files.append(pdf_path)
    
    # 调试用 MD (修复了这里的闭合问题)
    md_path = f"reports/{symbol}_report_{ts}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    
    print(f"✅ {symbol} 处理完成")

def main():
    os.makedirs("data", exist_ok=True)
    os.makedirs("reports", exist_ok=True)

    print("☁️ 正在连接 Google Sheets 获取持仓列表...")
    
    try:
        sm = SheetManager()
        stocks_dict = sm.get_all_stocks()
        print(f"📋 成功获取 {len(stocks_dict)} 只股票任务")
    except Exception as e:
        print(f"❌ Google Sheets 连接失败: {e}")
        return

    if not stocks_dict:
        print("⚠️ 列表为空，结束。")
        return

    # 2. 循环处理
    generated_pdfs = []
    
    for i, (symbol, info) in enumerate(stocks_dict.items()):
        try:
            process_one_stock(symbol, info, generated_pdfs)
        except Exception as e:
            print(f"❌ {symbol} 错误: {e}")
        
        # 防止接口限流
        if i < len(stocks_dict) - 1:
            time.sleep(10)

    # 3. 生成推送清单
    if generated_pdfs:
        print(f"\n📝 生成推送清单 ({len(generated_pdfs)} 个文件):")
        with open("push_list.txt", "w", encoding="utf-8") as f:
            for pdf in generated_pdfs:
                print(f"   -> {pdf}")
                f.write(f"{pdf}\n")
    else:
        print("\n⚠️ 本次没有生成任何 PDF，不创建 push_list.txt")

if __name__ == "__main__":
    main()
