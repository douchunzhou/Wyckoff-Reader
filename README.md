# 📈 Wyckoff AI Stock Analysis Bot

[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)
[![GitHub Actions](https://img.shields.io/badge/Actions-Daily%20Report-green.svg)](https://github.com/features/actions)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **全自动威科夫量化分析机器人**
>
> 结合传统技术指标与 LLM（大语言模型）的逻辑推理能力，自动拉取 K 线数据、绘制图表、生成威科夫分析报告，并通过 Telegram 推送。

---

## ✨ 核心特性 (Key Features)

### 1. 🧬 双源混合数据引擎 (Hybrid Data Engine)
为了解决单一数据源历史数据不足的问题，构建了强大的混合获取机制：
* **BaoStock (历史)**：负责拉取长周期的历史 K 线（底仓数据）。
* **AkShare (实时)**：负责补全最近期的实时数据。
* **智能清洗**：自动对齐不同数据源的时间戳格式，并智能修复“手/股”成交量单位差异（100x 修正）。
* **1分钟级支持**：针对超短线（1m）自动切换全量 AkShare 模式。

### 2. 🛡️ 三级 AI 熔断兜底 (Triple-Tier AI Fallback)
拒绝 `429` (限流) 和 `503` (过载)，确保报告 100% 产出。系统按以下优先级自动切换：
1.  **Primary**: Google Official Gemini API (`gemini-3-flash-preview`)
2.  **Secondary**: Custom Relay API (Qiandao `gemini-3-pro-preview-h`)
3.  **Fallback**: OpenAI / DeepSeek (`gpt-4o` 兼容接口)

### 3. 🎯 “千股千策” 动态配置
无需修改代码，直接在 Google Sheet 中定义每只股票的分析策略：
* 支持 **自定义周期**：`1m`, `5m`, `15m`, `30m`, `60m`。
* 支持 **自定义长度**：任意指定分析的 K 线根数（如 500, 1000, 2000）。

### 4. 🚀 高可用架构
* **防断连**：HTTP 连接强制伪装 UA 并禁用 Keep-Alive，防止 `RemoteDisconnected`。
* **自动化**：基于 GitHub Actions 定时运行，无需本地服务器。
* **推送**：分析完成后自动生成 PDF 并推送到 Telegram 群组。

---

## 🛠️ 配置指南 (Configuration)

### 1. Google Sheets 设置 (核心)
请在您的 Google Sheet 中设置以下列结构。**程序现在支持读取 E 列和 F 列进行个性化配置。**

| 列号 | 标题 (Header) | 说明 (Description) | 示例 (Example) |
| :--- | :--- | :--- | :--- |
| **A** | **Symbol** | 股票代码 (支持 A 股) | `600519` |
| **B** | Date | 建仓日期 (可选) | `2023-01-01` |
| **C** | Price | 建仓价格 (可选) | `1500.00` |
| **D** | Qty | 持仓数量 (可选) | `100` |
| **E** | **Timeframe** | **[新增] 分析周期 (分钟)** | `5`, `15`, `30`, `60`, `1` |
| **F** | **Bars** | **[新增] K 线抓取数量** | `500`, `1000` |

> 💡 **提示**：如果 E、F 列留空，程序将默认使用 `5m` 和 `500` 根 K 线。

### 2. GitHub Secrets 设置
前往仓库 `Settings` -> `Secrets and variables` -> `Actions`，添加以下环境变量：

#### 🤖 AI 模型相关
* `GEMINI_API_KEY`: Google Gemini 官方 API Key。
* `CUSTOM_API_KEY`: **[新增]** 第三方中转 API Key (第二优先级)。
* `OPENAI_API_KEY`: OpenAI 或 DeepSeek API Key (最终兜底)。

#### 📊 基础设施相关
* `GCP_SA_KEY`: Google Service Account JSON (用于读取表格)。
* `SHEET_NAME`: Google Sheet 的文件名或 ID。
* `TG_BOT_TOKEN`: Telegram Bot Token。
* `TG_CHAT_ID`: 接收报告的 Chat ID。

#### 📝 提示词
* `WYCKOFF_PROMPT_TEMPLATE`: 你的 AI 分析提示词模板。

---

## 📦 本地运行 (Local Development)

如果您想在本地测试代码：

1.  **克隆仓库**
    ```bash
    git clone [https://github.com/your-username/your-repo.git](https://github.com/your-username/your-repo.git)
    cd your-repo
    ```

2.  **安装依赖**
    ```bash
    pip install -r requirements.txt
    ```
    *注意：必须包含 `baostock`, `akshare`, `pandas`, `mplfinance` 等库。*

3.  **配置环境变量**
    建议创建一个 `.env` 文件或直接在终端 export 你的 API Keys。

4.  **运行脚本**
    ```bash
    # 运行主程序
    python main.py
    
    # 仅测试数据获取 (不消耗 Token)
    python test_data.py
    ```

---

## 🔄 工作流逻辑 (Workflow)

```mermaid
graph TD
    A[GitHub Actions Trigger] --> B{"读取 Google Sheet"};
    B --> C[遍历股票列表];
    C --> D{"周期 >= 5m?"};
    D -- Yes --> E[BaoStock 拉取历史数据];
    D -- No --> F[跳过 BaoStock];
    E --> G[AkShare 拉取实时数据];
    F --> G;
    G --> H["数据清洗 & 单位对齐"];
    H --> I["生成 K 线图 (mplfinance)"];
    I --> J{"AI 分析 (Triple Fallback)"};
    J -- Try 1 --> K[Gemini Official];
    K -- Fail --> L[Custom API];
    L -- Fail --> M["OpenAI / DeepSeek"];
    M --> N[生成 PDF 报告];
    K --> N;
    L --> N;
    N --> O[推送至 Telegram];
