# ForgeX — Operator-Style RPA Agent

**ForgeX** is an autonomous, visible Robotic Process Automation (RPA) agent built with Python, FastAPI, and Playwright. Inspired by OpenAI's Operator, ForgeX drives a **real Google Chrome window** with a visible, human-like cursor gliding around the page. You can watch it open tabs, perform multi-angle Google searches, highlight and copy AI Overviews, hand off research to ChatGPT, and populate target forms field-by-field — both directly in the Chrome window and on a real-time live dashboard.

---

## ✨ Key Capabilities

- **Real Google Chrome Only** — Launches your installed Google Chrome (`channel="chrome"`) with dedicated profile isolation (`.chrome-profile`). Never falls back to Chromium bundles.
- **Visible Moving Cursor (Operator Engine)** — Dispatches real mouse events along cubic Bezier curves with ease-in-out, subtle curvature, and micro-jitter. An SVG arrow cursor overlay rendered in a Shadow DOM displays dynamic action labels ("Typing...", "Clicking 'Search'", "Copying AI Overview"), click ripple animations, and element highlight rings.
- **Multi-Source Google Research Playbook** — Runs 3–4 distinct Google searches across different tabs, automatically scrolling, expanding, and copying Google AI Overviews and top organic articles.
- **ChatGPT Web Hand-Off** — Opens a ChatGPT tab, pastes formatted prompts and research via clipboard (Ctrl+V), tracks generation, extracts fenced JSON, validates schema lengths and year formats, and sends corrective follow-ups on errors.
- **Live Agent Dashboard (`/agent`)** — Split-screen interface featuring a step timeline with thought bubbles and screenshot thumbnails on the left, and a live screencast with open tab strips on the right.
- **Strict Human Approval Gate** — The agent stops completely before form submission. The final submit click can only occur after an explicit user confirmation in the UI.
- **Batch CSV & Excel Ingestion** — Ingests `.csv`, `.tsv`, and `.xlsx` files with automatic delimiter sniffing, multi-sheet support, formula evaluation (`data_only=True`), and formula injection protection (escaping cells starting with `=`, `+`, `-`, `@`).
- **Vision LLM Fallback Loop** — Autonomous Observe -> Think -> Act loop with screenshot vision, element numbering, and action allowlists for non-deterministic web exploration.

---

## 🛠️ Prerequisites

| Requirement | Details |
|---|---|
| OS | Windows, macOS, or Linux |
| Python | 3.11+ |
| Browser | **Google Chrome** (must be installed on the system) |

> ⚠️ **Note:** ForgeX strictly requires Google Chrome. Playwright Chromium bundles are blocked by design.

---

## 🚀 Setup

### 1. Clone & create virtual environment

```powershell
git clone https://github.com/Kyron-data-tech/forgex.git
cd forgex

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Configure environment

```powershell
copy .env.example .env
```

Open `.env` and verify your settings:
```ini
BROWSER_CHANNEL=chrome
BROWSER_HEADLESS=false
CURSOR_ENABLED=true
CURSOR_COLOR=#FF3366
TYPE_DELAY_MS=45
SEARCHES_PER_SUBJECT=4
```

### 3. Verify installation with Self-Check

Run the diagnostic self-check to verify that Chrome launches, the pointer engine glides smoothly, and Google/ChatGPT selectors are active:

```powershell
python -m app.agent.selfcheck
```

### 4. Start ForgeX

```powershell
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **[http://127.0.0.1:8000/agent](http://127.0.0.1:8000/agent)** to launch Operator tasks or batch tabular runs.

---

## 📋 Security & Safety Non-Negotiables

1. **Human-in-the-Loop:** Irreversible actions (form submission) always halt the bot and require an explicit human click in the UI.
2. **Untrusted Data Isolation:** All web content and ChatGPT responses are treated as passive data, strictly wrapped in delimiters, and validated against Pydantic/JSON schemas.
3. **Dedicated Profile:** Automation uses `.chrome-profile` and never touches your personal or default Chrome profile.
4. **Formula Injection Protection:** All tabular exports escape potential spreadsheet formula triggers (`=`, `+`, `-`, `@`).
5. **Localhost Binding:** Bound strictly to `127.0.0.1` with double-submit CSRF cookie verification.

---

## 🧪 Running Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v
```
