import os
import time
import json
import requests
from datetime import datetime
import pandas as pd
import akshare as ak
import mplfinance as mpf
from openai import OpenAI
import numpy as np
import markdown
from xhtml2pdf import pisa

# ==========================================
# 1. 数据获取模块
# ==========================================

def fetch_a_share_minute(symbol: str) -> pd.DataFrame:
    """获取A股1分钟K线 (使用东方财富接口)"""
    symbol_code = ''.join(filter(str.isdigit, symbol))
    print(f"   -> 正在获取 {symbol_code} 数据...")

    try:
        df = ak.stock_zh_a_hist_min_em(
            symbol=symbol_code, 
            period="1", 
            adjust="qfq"
        )
    except Exception as e:
        print(f"   [Error] 接口报错: {e}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    rename_map = {
        "时间": "date", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "volume"
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    
    df["date"] = pd.to_datetime(df["date"])
    cols = ["open", "high", "low", "close", "volume"]
    df[cols] = df[cols].astype(float)
    
    # === Open=0 修复逻辑 ===
    if (df["open"] == 0).any():
        print(f"   [清洗] 修复 Open=0 数据...")
        df["open"] = df["open"].replace(0, np.nan)
        # 用上一行的收盘价填充
        df["open"] = df["open"].fillna(df["close"].shift(1))
        # 如果第一行也是0，用当行收盘价填充
        df["open"] = df["open"].fillna(df["close"])

    bars_count = int(os.getenv("BARS_COUNT", 600))
    df = df.sort_values("date").tail(bars_count).reset_index(drop=True)
    return df

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma50"] = df["close"].rolling(50).mean()
    df["ma200"] = df["close"].rolling(200).mean()
    return df

# ==========================================
# 2. 本地绘图模块 (专业版)
# ==========================================

def generate_local_chart(symbol: str, df: pd.DataFrame, save_path: str):
    if df.empty: return

    plot_df = df.copy()
    plot_df.set_index("date", inplace=True)

    # 威科夫风格配色：涨红跌绿
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

    try:
        mpf.plot(
            plot_df, type='candle', style=s, addplot=apds, volume=True,
            title=f"Wyckoff Setup: {symbol}",
            savefig=dict(fname=save_path, dpi=150, bbox_inches='tight'),
            warn_too_much_data=2000
        )
        print(f"   [OK] 图表已保存")
    except Exception as e:
        print(f"   [Error] 绘图失败: {e}")

# ==========================================
# 3. AI 分析模块 (Gemini -> OpenAI)
# ==========================================

def get_prompt_content(symbol, df):
    """读取并填充 Prompt 模板"""
    prompt_template = os.getenv("WYCKOFF_PROMPT_TEMPLATE")
    
    # 本地回退逻辑
    if not prompt_template and os.path.exists("prompt_secret.txt"):
        try:
            with open("prompt_secret.txt", "r", encoding="utf-8") as f:
                prompt_template = f.read()
        except: pass

    if not prompt_template:
        return None

    csv_data = df.to_csv(index=False)
    latest = df.iloc[-1]
    
    return prompt_template.replace("{symbol}", symbol) \
                          .replace("{latest_time}", str(latest["date"])) \
                          .replace("{latest_price}", str(latest["close"])) \
                          .replace("{csv_data}", csv_data)

def call_gemini_http(prompt: str) -> str:
    """使用 HTTP POST 直接调用 Gemini API"""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found")

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
    
    if resp.status_code != 200:
        raise Exception(f"Gemini API Error {resp.status_code}: {resp.text}")
    
    result = resp.json()
    try:
        return result['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError):
        raise Exception(f"解析 Gemini 响应失败: {result}")

def call_openai_official(prompt: str) -> str:
    """调用官方 OpenAI API"""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not found")
        
    model_name = os.getenv("AI_MODEL", "gpt-4o")
    print(f"   >>> OpenAI ({model_name})...")
    
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model_name, 
        messages=[
            {"role": "system", "content": "You are Richard D. Wyckoff."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2 
    )
    return resp.choices[0].message.content

def ai_analyze(symbol, df):
    prompt = get_prompt_content(symbol, df)
    
    if not prompt:
        return "Error: No Prompt Template Found"

    # 1. 尝试 Gemini
    try:
        return call_gemini_http(prompt)
    except Exception as e:
        print(f"   [Warn] Gemini 失败: {e}")
        print("   >>> 切换至 OpenAI...")

    # 2. 尝试 OpenAI (Fallback)
    try:
        return call_openai_official(prompt)
    except Exception as e:
        error_msg = f"# 分析失败\n\nGemini 和 OpenAI 均无法响应。\n最后错误: `{e}`"
        print(f"   [Error] 所有 AI 通道均失败: {e}")
        return error_msg

# ==========================================
# 4. PDF 生成模块 (核心新增)
# ==========================================

def generate_pdf_report(symbol, chart_path, report_text, pdf_path):
    
    # 1. 转换 Markdown 为 HTML
    html_content = markdown.markdown(report_text)
    
    # 2. 获取图片的绝对路径 (PDF引擎需要)
    abs_chart_path = os.path.abspath(chart_path)
    
    # 3. 指定字体路径 (兼容 Linux/Windows)
    font_path = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc" # Linux GitHub Actions 路径
    if not os.path.exists(font_path):
        font_path = "msyh.ttc" # 本地测试回退
    
    # 4. 构建完整的 HTML 模板
    full_html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @font-face {{
                font-family: "MyChineseFont";
                src: url("{font_path}");
            }}
            @page {{
                size: A4;
                margin: 1cm;
            }}
            body {{
                font-family: "MyChineseFont", sans-serif;
                font-size: 12px;
                line-height: 1.5;
            }}
            h1, h2, h3, p, div {{ 
                font-family: "MyChineseFont", sans-serif; 
                color: #2c3e50;
            }}
            img {{ width: 100%; height: auto; margin-bottom: 20px; }}
            .header {{ text-align: center; margin-bottom: 20px; color: #7f8c8d; font-size: 10px; }}
            pre {{ background: #f4f4f4; padding: 10px; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <div class="header">Wyckoff Quantitative Analysis Report | Generated by AI Agent</div>
        
        <img src="{abs_chart_path}" />
        <hr/>
        
        {html_content}
        
        <div style="text-align:right; color:#bdc3c7; font-size:8px;">
            Target: {symbol} | Data Source: EastMoney
        </div>
    </body>
    </html>
    """
    
    # 5. 生成 PDF
    try:
        with open(pdf_path, "wb") as pdf_file:
            pisa.CreatePDF(full_html, dest=pdf_file)
        print(f"   [OK] PDF Generated: {pdf_path}")
    except Exception as e:
        print(f"   [Error] PDF 生成失败: {e}")

# ==========================================
# 5. 主程序 (支持多股循环)
# ==========================================

def process_one_stock(symbol: str):
    """处理单个股票的完整流程"""
    print(f"\n{'='*40}")
    print(f"🚀 开始分析: {symbol}")
    print(f"{'='*40}")

    # 1. 获取数据
    df = fetch_a_share_minute(symbol)
    if df.empty:
        print(f"   [Skip] 数据为空，跳过 {symbol}")
        return
    df = add_indicators(df)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 2. 保存中间文件
    csv_path = f"data/{symbol}_1min_{ts}.csv"
    chart_path = f"reports/{symbol}_chart_{ts}.png"
    pdf_path = f"reports/{symbol}_report_{ts}.pdf"
    
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    generate_local_chart(symbol, df, chart_path)

    # 3. AI 分析
    report_text = ai_analyze(symbol, df)
    
    # 4. 生成 PDF
    generate_pdf_report(symbol, chart_path, report_text, pdf_path)
    
    # 5. (可选) 保存 Markdown 用于调试
    md_path = f"reports/{symbol}_report_{ts}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    
    print(f"✅ {symbol} 处理完成")

def main():
    # 确保目录存在
    os.makedirs("data", exist_ok=True)
    os.makedirs("reports", exist_ok=True)

    # === 优先读取 stock_list.txt ===
    symbols = []
    
    # 1. 尝试读取文件
    if os.path.exists("stock_list.txt"):
        print("📂 发现 stock_list.txt，正在读取持仓列表...")
        try:
            with open("stock_list.txt", "r", encoding="utf-8") as f:
                symbols = [line.strip() for line in f.readlines() if line.strip() and not line.startswith("#")]
        except Exception as e:
            print(f"   [Error] 读取文件失败: {e}")

    # 2. 回退到环境变量
    if not symbols:
        print("⚠️ 未找到文件或为空，尝试读取环境变量 SYMBOLS...")
        symbols_env = os.getenv("SYMBOLS", "600970")
        symbols = [s.strip() for s in symbols_env.split(",") if s.strip()]

    symbols = list(set(symbols)) # 去重
    print(f"📋 最终待处理股票列表 ({len(symbols)}只): {symbols}")

    if not symbols:
        print("❌ 没有找到任何股票代码，程序结束。")
        return

    # 循环处理
    for i, symbol in enumerate(symbols):
        try:
            process_one_stock(symbol)
        except Exception as e:
            print(f"❌ {symbol} 发生严重错误: {e}")
        
        # 除非是最后一个，否则休息一下，防止接口封禁
        if i < len(symbols) - 1:
            wait_sec = 10
            print(f"⏳ 休息 {wait_sec} 秒...")
            time.sleep(wait_sec)

if __name__ == "__main__":
    main()
