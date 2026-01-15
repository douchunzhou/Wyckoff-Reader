📈 AI Wyckoff Stock Analyst (A-Share)
全自动 A 股威科夫分析师 —— 这是一个基于 GitHub Actions 的自动化金融量化项目。它每天定时抓取 A 股分钟级数据，利用 Google Gemini / OpenAI 扮演“理查德·威科夫”，生成包含专业图表和深度逻辑分析的 PDF 研报，并自动推送到 Telegram。

核心理念：位置第一，形态第二。不预测，只推演。

✨ 核心功能 (Key Features)
📊 数据自动清洗与获取

集成 AkShare (东方财富接口)，支持 A 股 1 分钟/日线数据抓取。

智能修复：自动识别并修复分钟线 Open=0 的异常数据，确保 K 线完整。

🧠 双引擎 AI 分析 (Dual-Core AI)

首选通道：Google Gemini 1.5/2.0 Flash (HTTP 直连，速度快，免费额度高)。

灾备通道：OpenAI GPT-4o (当 Gemini 不可用时自动无缝切换)。

威科夫人格：AI 严格遵循威科夫操盘法，分析供求关系、努力与结果、TR 区间及关键行为 (Spring/UT/LPS)。

📈 专业级绘图

使用 mplfinance 绘制专业蜡烛图。

配置 MA50 / MA200 双均线系统作为背景趋势参考。

采用红涨绿跌（符合 A 股习惯）的高对比度配色。

📑 PDF 研报生成

自动将 K 线图表与 AI Markdown 分析报告合并。

完美支持中文：内置字体配置，解决 Linux 环境下 PDF 中文乱码问题。

🤖 自动化与推送

GitHub Actions：每天定时运行 (午盘 12:00 / 收盘 15:15)。

Telegram Bot：直接将生成的 PDF 文件发送到你的手机。

📂 项目结构
├── .github/
│   └── workflows/
│       └── daily.yml      # GitHub Actions 自动化配置
├── data/                  # 存放下载的 CSV 数据 (自动生成)
├── reports/               # 存放生成的 PNG/MD/PDF (自动生成)
├── main.py                # 核心主程序
├── requirements.txt       # 依赖列表
└── README.md              # 项目说明

⚠️ 免责声明 (Disclaimer)
本项目仅供技术研究与学习使用。
不构成投资建议：AI 生成的分析报告可能存在幻觉或错误，不能作为买卖依据。
数据延迟：免费数据接口可能存在延迟或不稳定性。
风险自负：股市有风险，入市需谨慎。

📧 联系与反馈
如有问题或建议，欢迎提交 Issue 或 Pull Request。
