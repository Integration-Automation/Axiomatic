# Axiomatic 架構

> 短版總覽：這個 repo 由哪些部分組成、從哪裡啟動、要擴充時該動哪個檔案。使用者面的
> 說明在 [`README.md`](README.md)、[`COMMANDS.md`](COMMANDS.md) 與 [`docs/`](docs/index.md)；
> 常駐硬規則在 [`CLAUDE.md`](CLAUDE.md)。
> 撰寫慣例：散文用泛稱指涉外部服務，檔名與程式符號照實寫。

## 1. 目的

影像生成自動化，由兩個長駐行程組成：

- **webrunner**（執行層）：以瀏覽器操作出圖服務，依佇列檔批次產圖——登入、填提示詞、
  按產生、下載圖檔，並把續跑檢查點與事件寫回磁碟。
- **bot**（意圖層）：對話平台上的 bot，負責接指令、編輯佇列檔、spawn 並監督 webrunner、
  回報結果；另外附帶單張產圖佇列、桌面控制指令、presence 鏡像與 Dorossi 對話後端（多種後端）。

兩者**只透過磁碟上的檔案溝通**，永遠不互相 `import`，所以任一邊重啟、崩潰或被殺，
另一邊都不會壞掉。

## 2. 分層與目錄

```
使用者（對話平台）
   │  斜線指令
   ▼
discord_bot.py ──磁碟檔案（todo_*.md、*_request.json、webrunner.pid/pause、events.ndjson）── webrunner_*.py
   │                                                                                    │
   └──────────── 被動共用模組（_batch_config、_webrunner_shared、_queue_consume …）──────┘
```

| 路徑 | 職責 |
| --- | --- |
| `axiomatic/discord_bot.py` | 意圖層：斜線指令樹（唯一對外介面）與隱藏的 `!`／`@bot` 相容層、佇列編輯、webrunner 監督、單張產圖佇列、Dorossi 編排、presence 與背景任務 |
| `axiomatic/webrunner_novelai.py`、`axiomatic/webrunner_je_only.py` | 執行層的兩個變體（Selenium 為正式預設、wrapper 為備援）：瀏覽器生命週期、登入、設定檔快照、`BrowserPort` adapter |
| `axiomatic/_webrunner_shared.py` | 兩個變體的共用核心，不 import 任何 driver：DOM 操作、佇列 I/O、輸出資料夾分配、產圖迴圈、單圖服務、`run_batch` |
| `axiomatic/_batch_config.py`、`_bot_config.py`、`_queue_consume.py`、`_run_progress.py`、`_supervisor.py`、`_chrome_slot.py`、`_code_fingerprint.py`、`_warn_dedup.py` | 無狀態的共用模組：設定載入、佇列消耗決策、續跑檢查點、退避與單一實例鎖、跨行程瀏覽器槽鎖、程式碼指紋、警告去重（允許當第三通道的完整清單以 `CLAUDE.md`「Module boundaries」為準） |
| `axiomatic/_process_control.py`、`_gui_control.py`、`_external_apis.py`、`_help_strings.py`、`_bot_prompts.py`、`dorossi_backend.py`、`presence_probe.py`、`discord_rpc.py` | bot 專屬模組：行程探查與終止、桌面自動化門面、外部圖庫／web API、說明文字資料、外部化 prompt 載入、Dorossi 後端與工作階段、本機狀態探測、本機 Rich Presence |
| `start_discord_bot.py`、`start_webrunner.py`、`run_batch.py`、`install_autostart.py`、`wake_autostart.py` | repo 根目錄的監督啟動器、一鍵批次入口、Windows 工作排程器自動啟動的註冊與手動叫醒 |
| `axiomatic/verify_*.py`、`dashboard_server.py` | 手動驗證腳本（瀏覽器、外部 API、額度對話框、後端 CLI）與本機唯讀狀態儀表板 |
| `axiomatic/gen_command_docs.py`、`audit_dependencies.py`、`audit_simplified_chars.py`、`mutation_harness.py` | 文件產生、相依與字形稽核、變異測試骨架 |
| `test/test_*.py`、`test/conftest.py`、`test/_test_*.py`；`axiomatic/_browser_killguard.py`；`pytest.ini` | pytest 測試（大量是把硬規則變成靜態守門）、全套共用的守門夾具與兩支手動 e2e；防止測試誤殺正式瀏覽器的執行期防線（留在套件裡，repo 外的探針也要 import 它）；`testpaths = test`、`pythonpath = axiomatic .` 讓測試照舊用頂層名字匯入正式模組 |
| `todo_prompt.md`、`todo_character1.md`、`todo_character2.md`、`todo_undesired.md`；`prompt.md`、`character1.md`、`character2.md`、`undesired.md`；`default_prompt.md` | 四條佇列、各自的 fallback 提示詞、主提示詞範本（bot 寫、webrunner 讀）。全部是**使用者內容**：repo 只帶 `*.example.md` 範本，實際檔案 gitignored |
| `batch_config.json`（追蹤）、`bot_config.example.json`、`presence_*.example.json`、`bot_prompts/` | 設定檔與外部化 prompt 文字。帶 `.example` 的要先複製成同名的正式檔再填 |
| `auth.md`、`discord_bot_token.md` | 憑證，**永不追蹤**（`.gitignore`）；範本是 `*.example.md` |
| `README.md`、`COMMANDS.md`、`commands/`、`docs/` | 使用者文件；`commands/*.md` 由指令樹產生，`docs/` 是 Sphinx 原始碼 |
| `CLAUDE.md`、`architecture.md` | 常駐硬規則與本檔 |
| `output/`、`.chrome_profile*/`、`*.log`、`*.ndjson`、`webrunner.pid` 等 | 執行期產物（gitignored） |

## 3. 進入點與對外介面

| 指令 | 用途 |
| --- | --- |
| `py -3 start_discord_bot.py` | bot 監督迴圈（單一實例鎖、致命 rc 不重試、輸出 tee 進 `discord_bot.log`） |
| `py -3 start_webrunner.py [selenium\|je]` | webrunner 監督迴圈（退避、快速失敗放棄、參與瀏覽器槽協定） |
| `py -3 run_batch.py [selenium\|je] [--clear-pause]` | 本機一鍵批次：前置檢查 → 印 run-plan → 交棒給 `start_webrunner.py` |
| `py -3 install_autostart.py --install\|--status\|--remove` | 登入時自動拉起兩支監督者（Windows 工作排程器，`\Axiomatic\Bot`／`\Axiomatic\Batch`） |
| `py -3 axiomatic/dashboard_server.py` | 本機唯讀狀態儀表板 |
| `py -3 axiomatic/verify_browser.py [--full] [--variant je]` | 隔離瀏覽器驗證，不碰正式設定檔 |
| `py -3 -m pytest` | 全部測試（`pytest.ini` 的 `testpaths` 指向 `test/`；單檔 `py -3 -m pytest test/test_x.py`） |

對外介面：

- **斜線指令樹**是唯一對外宣傳的介面；逐群參考在 `commands/*.md`。`!` 與 `@bot` 是隱藏
  相容層，唯一對外的 mention 用法是 `@bot <文字>` 自由提問。
- **磁碟契約**：四條佇列檔（換行分隔，格式見 `CLAUDE.md`「todo file format」）、`single_image_request.json`、
  `dom_request.json`、`webrunner.pause`、`webrunner.pid`、`events.ndjson`、
  `webrunner_progress.json`。
- **webrunner 回傳碼**：`0` 乾淨收工、`1` 無事可做、`2` session setup 失敗、`3` 零產出
  （`RC_ZERO_PROGRESS`）、`4` 被擋住不重生（`RC_GENERATION_BLOCKED`）、`5` 設定／憑證
  還沒填（`RC_SETUP_INCOMPLETE`，同樣不重生）。常數在 `_supervisor.py`。
- **bot 回傳碼**：`3` 已經有另一個實例在跑（`RC_ALREADY_RUNNING`）、`5` 設定還沒填
  （`RC_SETUP_INCOMPLETE`）。兩者都讓監督啟動器直接收工，不進退避重試
  （`child_exit_is_fatal`）。

## 4. 主要流程

1. **批次產圖（`/run`）**：bot 取瀏覽器槽鎖（`_chrome_slot`）→ 清掃既有實例 → spawn webrunner、
   寫 `webrunner.pid`、放鎖 → `_watch_for_fallback` 監督 → webrunner `main()` →
   `run_preflight` → 建立 driver、登入與設定 → `ws.run_batch`：每個角色重讀四條佇列 →
   `_queue_consume.decide` → `generate_loop` → 達門檻才 pop 佇列 → `emit_event` 寫
   `events.ndjson` → bot `_event_watcher` / `_handle_event` 回報頻道。
2. **單張產圖（`/gen image`）**：bot 以記憶體 FIFO 序列化請求 → 寫 `single_image_request.json`
   （單槽）→ 正在跑的批次帶內服務、既有單圖伺服器接手，或 bot 以
   `SINGLE_IMAGE_SERVER_FLAG` spawn 新的單圖伺服器 → 發 `single_image_done` 並刪除請求檔 →
   bot 送出下一筆。
3. **Dorossi 對話（`/dorossi ask`、`@bot Dorossi <提問>`）**：解析開頭的微調 token → 決定工作
   階段 slot → 取 per-session 鎖 → `dorossi_backend.py` 串流叫用後端並套兩段式看門狗 →
   即時更新單一訊息 → 記錄用量、存 `dorossi_session.json`。

## 5. 擴充點

| 要新增 | 動這些檔案 |
| --- | --- |
| 斜線指令 | `axiomatic/discord_bot.py` 指令樹；同步 `axiomatic/_help_strings.py`（三語）、`README.md`、`COMMANDS.md`、`docs/commands_*.md`；再跑 `py -3 axiomatic/gen_command_docs.py` 重建 `commands/*.md`（編輯說明放 `NOTES` / `COMMAND_NOTES`）。操作主機的指令歸入 `_OWNER_ONLY_GROUPS` 的群組 |
| 批次設定鍵／bot 設定鍵 | `axiomatic/_batch_config.py` 或 `axiomatic/_bot_config.py` 的 `_COERCERS` |
| webrunner 事件 | `_webrunner_shared.emit_event` 發出 ＋ `discord_bot._handle_event` 接住（`test_webrunner_shared.py` 兩邊對帳） |
| DOM／產圖行為 | 共用的放 `axiomatic/_webrunner_shared.py`；driver 專屬的兩個變體都要改 |
| 跨行程檔案 | 原子寫入，並同時加進 `CLAUDE.md` 原子寫入清單與 `test/test_atomic_writes.py` 的 `_CROSS_PROCESS_CONSTANTS`；新的根目錄檔案要分類為追蹤的專案資產或 gitignored 的執行期產物（`test/test_gitignore_coverage.py`） |
| 外部 API | `axiomatic/_external_apis.py`（唯一外送出口 `_http_get_json`）＋ `axiomatic/verify_external_apis.py` |
| 桌面控制功能 | `axiomatic/_gui_control.py`，錯誤以泛用訊息的 `GuiError` 拋出 |
| prompt 文字／presence 規則 | `bot_prompts/`（由 `_bot_prompts.py` 載入）；`presence_games.json`、`presence_music.json`、`presence_rpc.json`（各自的 `.example.json` 是範本） |

## 6. 跨專案邊界

- **WebRunner（`je_web_runner`）**：`axiomatic/webrunner_novelai.py` 與
  `axiomatic/webrunner_je_only.py` 把環境變數 `WEBRUNNER_PATH`，或 repo 上一層的
  `WebRunner/`（即 `<parent>/WebRunner`），插到 `sys.path[0]`；`requirements.txt` 另宣告
  `je_web_runner>=0.0.88`。`test/conftest.py` 會引用該函式庫的
  `je_web_runner.utils.logging.loggin_instance`。
- **AutoControlGUI（`je_auto_control`）**：`requirements.txt` 的 `je-auto-control`。門面是
  `axiomatic/_gui_control.py`；`axiomatic/_webrunner_shared.py` 與
  `axiomatic/presence_probe.py` 另有直接 import。pytest 以 `pytest.ini` 的
  `-p no:je_auto_control` 擋掉它的外掛。
- **變異測試的探針腳本**：`axiomatic/mutation_harness.py` 是被 repo **外**的一次性腳本
  import 的骨架；快照目錄預設在系統暫存區底下，可用 `AXIOMATIC_MUTATION_SNAPSHOT_DIR`
  覆寫。repo 裡刻意不放那些一次性腳本。
- **外部服務**：出圖服務（瀏覽器操作）、對話平台、外部圖庫／web API、Dorossi 對話後端
  （多種後端）、Windows 工作排程器。

## 7. 設計約束

以下只是摘要，正本是 `CLAUDE.md`：

- bot 與 webrunner 不得互相 import，只透過磁碟檔案與被動共用模組溝通（「Module boundaries」）。
- todo 檔換行分隔、不切逗號，`todo_character2.md` 的空行具位置意義（「todo file format」）。
- 操作主機的指令只限擁有者，以群組制 `_OWNER_ONLY_GROUPS` 在派發前把關（「Host control is owner-only」）。
- 送往對話平台的字串不得含外部服務名、主機路徑、PID 或原始例外文字；擁有者例外走
  `_owner_detail` 單一決策點；憑證值在任何表面都不得送出（「Secrecy」）。
- 所有中文使用台灣繁體詞彙，由 `test/test_language.py` 守門（「Language」）。
- Windows 上不得用 `os.kill(pid, 0)` 判斷行程存活（「Windows PID liveness」）。
- 跨行程檔案一律同目錄暫存檔 → `os.replace` 原子寫入，todo 佇列是刻意的例外（「Atomic writes for cross-process files」）。
- 文字 I/O 一律明寫 `encoding="utf-8"`，產生 Python 子行程時同時設定 `PYTHONIOENCODING`（「Text I/O always names its encoding」）。
- 每次變更：冒煙 import、新斜線指令同步五份文件、`requirements.txt` 只設下限不釘版本、保留直譯器
  探索順序（「Definition of Done」）。
- commit 不得透露作者身分、逐檔 `git add`；憑證檔永不進版本庫，
  新的根目錄執行期檔案必須分類（「Git Commits」）。
- 新工作一律放 `axiomatic/`；測試一律放 repo 根目錄的 `test/`，不放回套件（「Project structure」）。

## 8. 何時更新本檔

- 新增、刪除或改名一個長駐模組、被動共用模組或啟動器，或其職責改變；
- 進入點、回傳碼契約、磁碟契約的種類，或 bot↔webrunner 的溝通方式改變；
- 擴充點（§5）的位置改變，或出現新的跨專案相依；
- `CLAUDE.md` 的硬規則新增、刪除或改名。

本檔只寫總覽；單一模組的細節與陷阱寫在該模組自己的 docstring 與對應的測試裡。
