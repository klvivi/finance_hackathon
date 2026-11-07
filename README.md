# Ledgerly: the month-end books, closed by an agent

**User:** the ops/finance person at a 5–15 person agency, at the end of the month: *"Close October."*
Ledgerly pulls the transactions, categorizes them, reconciles the bank, prepares the books for review, and
delivers them. It runs on **ZooWork Managed Agents** and stops only to ask before an email goes to a client.

All data is synthetic (Brightwave Studio LLC, a fictional agency). No real financial data is used.

## The guideline flow

| Step | What happens | Who |
|---|---|---|
| **Pull transactions** | One CSV with 100 October transactions across checking and credit card (`Date, Description, Account, Amount, Balance, Receipt`). It removes a row exported twice, fixes a broken date and retries, and pulls the bank statement balance from the `Balance` column. Receipt photos are read with the agent's `image` tool and attached, turning *Missing* into *OK*. | Agent |
| **Categorize** | Only **Income, Rent, Contractors, Software, Bank fees**. Rules, then similarity to last month, then model judgment with a confidence and a reason. Anything that fits none of them (travel, cash, transfers, unexplained payments) becomes an **open item**; the agent never forces a guess, and the backend rejects any other category. | Agent + code |
| **Types** | Checking: + = Income, − = Expense. Credit card: − = Income, + = Expense. | Code |
| **Reconcile** | Money we already have + income − spend = theoretical money, compared with the bank statement balance. If there's a difference, the agent **investigates**: it walks the statement's running balance line by line, finds the exact gap, fixes import errors, and books provable gaps as open items. | Agent + code |
| **You review** | The *Open items* page: pick a category, upload or waive a receipt, document income, acknowledge a difference, record a W-9. Your category choices become rules the agent saves to memory. | Human |
| **Deliver** | One PDF with **Cover → Ledger → Expenses → Reconciliation → Income → Contractors → Open items**. It's a draft after the run and Final when you click *Deliver books*. | Human click, code renders |

Also built in: invoices for delivered work (sent only after approval), payment matching (income "Document" = invoice
number), anomaly detection (duplicate Figma charge, AWS 4.7× spike, new subscriptions), contractor year-to-date totals
with tax-form status and the 1099-NEC flag, **Ask your books** (questions answered from the ledger), and a per-transaction
**audit trail** (how it was categorized, journal entries, receipt image, audit log).

## Architecture

```
Browser (static/index.html) ──HTTP/SSE──► FastAPI backend (holds ZOOWORK_API_KEY)
                                           ├─ ledger.sqlite   single source of truth; journal is append-only (triggers)
                                           ├─ tools.py        deterministic money math exposed as 9 custom tools
                                           ├─ books.py        ledger / expenses / reconciliation / income / contractors / open items
                                           ├─ runtime.py      sessions, event stream → UI, custom-tool executor, approvals
                                           ├─ /mcp            "Ledgerly Outbox" MCP server (needs a public tunnel)
                                           └─ /webhooks/zoowork
ZooWork Managed Agent "ledgerly" (claude-sonnet-5-5)
  persona playbook · custom tools · memory · image tool · artifact_publish · approval gate · schedule
```

The model plans, decides and explains; **it never does arithmetic**. Every `*_minor` integer in a tool result
comes with a formatted sibling (`total_minor` → `total: "$1,234.56"`), and shell and web tools are denied.

## How the platform is used

| Feature | Use |
|---|---|
| **Model** | `litellm/claude-sonnet-5-5`, thinking `medium`. Reliable long tool sequences, image input for receipts, and honest confidence. The hard parts are deterministic code, so a larger model isn't needed. |
| **Custom tools** | `ingest_file, categorize, post_entry, reconcile, create_invoice, match_payment, run_report, query_ledger, save_close_summary`. The run pauses mid-turn while our backend does the math. |
| **Sessions + events** | One session per close and a separate session for *Ask your books*. The UI shows plain-language progress from `agent.tool` / `agent.custom_tool_use` / `agent.approval` / `run.finished`. Cursors persist, so a restarted server resumes. |
| **Files / Artifacts** | Uploads go into `/workspace/inbox` (receipts are read with the `image` tool). The books PDF and invoices are published with `artifact_publish`. |
| **Memory** | Review decisions become categorization rules (`memory_create`). The next close recalls them (`memory_search`) and passes them to `categorize`. Memory survives a demo reset, so the effect shows on re-run. |
| **Permissions** | `always_ask` only on outbound email. Everything inside the books runs without a human. |
| **Schedules / Webhooks / MCP** | Weekly overdue-invoice check; signed webhooks for run events; the MCP outbox. Wired up; they need a public HTTPS URL (tunnel) to go live. |
| **Agent Database** | *Not used, deliberately*: the app must read the ledger directly, and only the agent can query `agent_db`. |

## Key tradeoffs
1. **Rules first, model last.** Rules and similarity settle most rows for free and identically every month; the model only sees the ambiguous rest.
2. **Unknown means open item, not a guess.** The category set is fixed. A confident wrong category is worse than an honest "I don't know", and a human resolves it in seconds. The Close page also offers an explicitly opt-in demo assist, restricted to bundled synthetic fixtures: it estimates categories for items beyond the five oldest review candidates, labels them unverified, and never fabricates missing receipts, documents, tax forms, or reconciliation sign-offs.
3. **Reconcile from evidence, never force it.** The agent may fix import errors and book only what the bank's running balance proves; anything else stays flagged.
4. **Double-entry, immutable journal.** Every posting balances or is rejected; corrections are reversing entries, so the audit trail shows what the agent did and what a human changed.
5. **Human approval only where it can't be undone.** Internal entries can be reversed; an email to a client can't.

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Python 3.11+
cp .env.example .env            # put ZOOWORK_API_KEY in .env (server-side only)
python -m seed.generate         # synthetic dataset (100 October transactions + scenario files)
python -m backend.reset         # clean ledger: September closed, October open
python -m tests.test_pipeline   # offline end-to-end tests (no API calls)
python -m backend.provision create --model litellm/claude-sonnet-5-5
python -m uvicorn backend.app:app --port 8000      # http://localhost:8000   (admin view: ?admin=1)
```

Reset: `python -m backend.reset` (agent memory is kept on purpose). Clean up: `python -m backend.provision teardown`.
Check charges under **Usage** in ZooWork Platform.

## 3-minute demo script
1. **(0:00)** "End of the month." *Close the books* → upload your own bank or card CSV and any receipts → **Close October**.
2. **(0:15)** The progress checklist moves on its own: pulling transactions (one broken date fixed and retried, a duplicate removed), receipts read from photos, income matched to invoices, categorizing.
3. **(1:00)** **Bank reconciliation: Matched.** $49,613.87 + income − spend = $49,604.39 = bank statement.
4. **(1:15)** **Open items**: travel, cash, a transfer, an unexplained Venmo, a contractor with no W-9, income with no invoice. Resolve two live; the agent will remember them.
5. **(1:45)** **Ask your books**: "Why is software spend up?" It answers with the drivers (AWS spike, new subscriptions, duplicate Figma).
6. **(2:10)** Click a ledger line: the audit trail shows the rule, journal entries and receipt image.
7. **(2:25)** **Deliver books** → the 9-page PDF. Approve one invoice email in **Approvals**.
8. **(2:45)** *(Optional)* Load the "missing transaction" scenario: the agent finds the $1,250 gap between Oct 24 and Oct 27 from the bank balance.

## Layout
`backend/` (ledger, books, tools, runtime, MCP, API) · `static/index.html` (UI) · `seed/generate.py` (dataset) ·
`data/demo` (sample upload) · `data/scenarios/missing_transaction` · `data/seed/history.json` (September) · `tests/test_pipeline.py`.

## Netlify landing page
The standalone, static product website is in `netlify-site/`; it does not depend on the Ledgerly backend or expose its API key.
Deploy the repository with Netlify (the root `netlify.toml` publishes `netlify-site/`), or run
`netlify deploy --prod --dir=netlify-site` with the Netlify CLI.
