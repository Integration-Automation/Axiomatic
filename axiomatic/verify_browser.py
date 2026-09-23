#!/usr/bin/env python3
"""verify_browser.py — 獨立的瀏覽器 / driver 啟動驗證腳本。

目的：讓自動化 agent 在「完全不碰正式流程」的前提下，驗證跟瀏覽器 /
chromedriver / Selenium 啟動有關的程式碼改動是否健康。本腳本刻意做成
自包含、import 輕量，**不** import 任何一支 webrunner（它們 import 時
可能有副作用），也**不**讀憑證、不碰正式登入態。

設計上的硬性不變條件（之後要改這支、或改 webrunner 的 Chrome 啟動，
務必先讀這幾條，免得踩雷）：

* **絕對不可用正式 profile。** 每次驗證都用 `tempfile.mkdtemp()` 開一個
  用完即丟的暫時 profile 目錄，永遠不碰 `.chrome_profile/` /
  `.chrome_profile_snap/`。正式 profile 帶著登入態與 singleton lock，
  借用它驗證會污染登入、或被既有 webrunner 的 lock 擋住。暫時 profile
  是全新的，所以這支不需要做 webrunner 那套 SingletonLock 清理。

* **driver 解析務必走 Selenium Manager（不可寫死路徑）。** 建
  `ChromeService` 時不給 `executable_path`、不塞任何 driver 路徑，讓
  Selenium ≥ 4.6 自動用 Selenium Manager 解析 chromedriver——這正是
  `webrunner_novelai.build_stealth_driver` 現在的做法。唯有走同一條解析
  路徑，這支的驗證才對 production 有意義。

* **清理只能殺「自己這次起的」chrome / chromedriver，禁止 nuclear
  sweep。** webrunner 在 `_kill_orphan_chrome()` 會無條件殺光全機
  chrome.exe / chromedriver.exe（因為呼叫當下它自己的 driver 還沒生，
  沒有自家瀏覽器要保護）。**這支不行**——驗證可能與正式 webrunner 同時
  在跑，殺光全部會誤殺正式產圖的瀏覽器。所以這裡只回收
  `driver.service.process.pid` 那棵行程樹（chromedriver + 它生出來的
  chrome 子孫）。代價：若 `webdriver.Chrome(...)` 半途失敗、留下一個我們
  抓不到 handle 的半啟動 chrome，這支**不會**去殺它（無法在不誤傷別人的
  前提下指認它）——它會帶著自己的暫時 profile 變成無害 orphan，留給作業
  系統或人工收。寧可漏殺，不可錯殺。

* **整支時間有界、過程持續吐進度行。** 呼叫端是一個有「輸出沉默就砍」
  backstop 的自走迴圈；安靜太久會被砍。所以每個可能慢的步驟前都先印一行
  進度，且總時長設上限。

* **跨行程「單一 Chrome 槽」鎖是主要互斥機制。** smoke 與 full 在開任何
  Chrome 之前都先 `_chrome_slot.acquire("verify", timeout=…)`。拿不到 →
  **SKIP**「slot busy」、exit 3。拿到後依 `_chrome_slot` 的順序契約：先讀
  `webrunner.pid`，若有活著的正式 webrunner → 釋放槽並讓位（不開瀏覽器、
  **SKIP**「讓位」、exit 3）；否則才開瀏覽器、跑完在 finally 釋放槽。持鎖期間
  bot 不會 nuclear-sweep 我們，所以鎖比下面的「失敗重試」可靠得多。

* **抗外部 nuclear sweep：失敗自動重試（次要安全網）。** 即便有鎖，呼叫端
  別處仍可能無條件殺 chrome（理論上的競賽窗口）；smoke 啟動 / 載入失敗時
  仍自動重試（每次重建全新 mkdtemp 暫時 profile），仍失敗才印 FAIL。鎖是
  主、重試是備援。

CLI（固定契約，已對齊另一邊；只**新增**參數、不更動既有語意）：
  (無參數) / --mode smoke   輕量煙霧驗證（預設；暫時 profile、headless、不需憑證）
  --headed                  （僅 smoke）顯示視窗（預設 headless=new）
  --url <URL>               （僅 smoke）要載入的頁面（預設 about:blank；不內建網址）
  --variant {selenium,je}   （僅 smoke）要驗哪一個變體的啟動路徑。預設 selenium，
                            行為與這個旗標出現之前逐字相同。je 見下面那段；
                            `--full --variant je` 是參數錯誤（exit 2），因為 full
                            目前只接 selenium 變體。
  --full / --mode full      持鎖、隔離的「登入＋導航＋確認產圖介面/關鍵 DOM 可達」
                            端到端驗證；以**子行程**呼叫背景程式（selenium 變體）
                            的 opt-in 驗證模式（env `NAI_VERIFY_MODE=setup`），本腳本
                            **不** import 任何 webrunner。需要已登入的 profile。
  --generate                （僅 full）加分項：在隔離環境真的產 1 張圖到隔離目錄
                            再清掉（會實際送出一次生成）。預設關閉。
  --help                    正常 parse

`--full` 的隔離由 webrunner 端的驗證模式保證（opt-in、預設關閉）：快照
`.chrome_profile/` 到隔離目錄、跑完不寫回；不寫 `webrunner.pid`；不碰
`todo_*.md` / `prompt.md` / `undesired.md`；輸出進隔離目錄並清掉；不 nuclear-sweep
（只外科回收自己的 driver 行程樹）。注意 full 目前只接 selenium 變體
（`webrunner_novelai.py`）；je 變體只有 smoke。

`--variant je` 的 smoke 走 je 變體真正的啟動路徑：`je_web_runner` 的
`set_driver("chrome", options=…, experimental_options=…, service=ChromeService(log_output=…))`，
連同它裡面那一次 driver 管理器的 `install()`。它在**子行程**裡跑，工作目錄是丟棄式暫存
目錄（`je_web_runner` 匯入時就在工作目錄開記錄檔、`install()` 的快取也跟著工作目錄走，
在 repo 根目錄跑會寫進正式的 `webrunner.log` 與 `.wdm`）。槽與讓位仍由本行程負責；
子行程看不到槽握在祖先行程手上就拒絕開瀏覽器。FAIL 的原因會分清「driver 管理器
安裝失敗」與「瀏覽器起不來」。細節見 `run_smoke_je` 上面那段。

結束印機器可讀單行結果，三種結論互斥：
  `VERIFY-BROWSER: OK`（exit 0）                開了瀏覽器、驗過了、沒問題
  `VERIFY-BROWSER: FAIL <簡短原因>`（exit 1）   開了（或試著開）瀏覽器、壞了
  `VERIFY-BROWSER: SKIP <簡短原因>`（exit 3）   **什麼都沒驗**，晚點再來
SKIP 只有兩種來源：槽被佔住、以及偵測到活著的正式作業而讓位。它跟 FAIL 分開是
因為正式批次幾乎總是在跑，讓位其實是最常見的結果——混在 FAIL 裡的話，不是害人
去追一個不存在的瀏覽器問題，就是教會人「這支的 FAIL 不用理」。SKIP 仍然是**非零**
結束碼（沒驗到就不能回報成功），所以檢查 `== 0` / `!= 0` 的呼叫端完全不受影響。
用 3 不用 2，是因為 argparse 打錯參數時自己就 exit 2，而那條路連一行
`VERIFY-BROWSER:` 都不會印。子行程自己另印
`VERIFY-SETUP: OK/FAIL`，本腳本把它轉成上面的 `VERIFY-BROWSER:` 結果行
（轉印子行程輸出時一律加 `  | ` 前綴，避免 grep `^VERIFY-BROWSER:` 誤抓）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# 與其他模組一致：parent.parent ＝ repo root（webrunner.pid / chrome_slot.lock /
# 待呼叫的 webrunner 驗證入口都在這個基準下）。本腳本「不」import 任何 webrunner，
# `--full` 一律以子行程方式呼叫，避免 webrunner import 副作用。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# webrunner 的 selenium 變體即驗證子行程的目標（檔名是模組名、非品牌名，可寫死）。
WEBRUNNER_SCRIPT = Path(__file__).resolve().parent / "webrunner_novelai.py"
# 背景產圖期間由 **spawn 端**寫這個 pid 檔——bot，以及 `start_webrunner.py`
# （2026-08-23 起）。**2026-09-11 起多了第三個寫入端**：沒有父行程發布訊號時，
# webrunner 自己會認領（`_webrunner_shared.claim_liveness_signal`）——那是為了讓
# 裸跑 `py -3 axiomatic/webrunner_novelai.py` 也有互斥訊號，否則本腳本會判定
# 「沒有正式作業」而開出自己的瀏覽器，然後在下一個角色邊界被全機掃描殺掉。
# 我們依 _chrome_slot 的順序契約讀它判斷「是否有正式作業在跑」。本腳本永遠不寫它。
WEBRUNNER_PID_FILE = PROJECT_ROOT / "webrunner.pid"

# 機器可讀結果行的前綴。呼叫端靠這個 grep 出單行結論。
RESULT_PREFIX = "VERIFY-BROWSER:"

# 整支驗證（含所有重試）的總時長上限，秒。超過就不再重試，直接 FAIL。
TOTAL_DEADLINE_SEC = 240.0
# 啟動 / 載入失敗時的重試次數（含第一次）。抗外部 nuclear sweep。
MAX_ATTEMPTS = 3
# 單次頁面載入 / 指令逾時，秒。讓單一步驟不會無限卡住。
PAGE_LOAD_TIMEOUT_SEC = 30.0
SCRIPT_TIMEOUT_SEC = 15.0

# 跨行程「單一 Chrome 槽」：smoke 與 full 開瀏覽器前都先取得 owner="verify" 的槽，
# 持鎖期間 bot 不會 nuclear-sweep 我們；跑完一律在 finally release。
SLOT_OWNER = "verify"
# 取鎖等待上限：槽空就立刻拿到；只有在 bot 正卡在 sweep+spawn 的短臨界區時才會等。
SLOT_ACQUIRE_TIMEOUT_SEC = 60.0
# `--full` 子行程（登入＋導航＋setup，必要時再產 1 張圖）的總時長上限，秒。比 smoke
# 寬鬆但仍有界；超過就 kill 子行程並 FAIL（避免自走迴圈的沉默 backstop 誤砍）。
FULL_DEADLINE_SEC = 360.0


def _harden_console() -> None:
    """讓「這個字元印不出來」降級成一個逃脫序列，而不是讓這支工具自己死掉。

    Windows 上 `print()` 到**管線**（被重新導向、被工具擷取、被啟動器 spawn）走的
    是地區編碼（本機 cp950），編不出來就是 `UnicodeEncodeError`——**行程死掉**，
    不是印成亂碼。對這支腳本來說那個
    後果特別貴：它的硬性契約是**一定**要印一行 `VERIFY-BROWSER:` 結論，而死在一行
    進度訊息上，呼叫端讀到的是「沒有結論 ＋ 非零結束碼」——**一個假的 FAIL**。
    瀏覽器好好的，壞的是這支工具自己，而它正是專案規定用來判斷瀏覽器有沒有被改壞
    的那一支。回報錯的答案比沒有答案更貴。

    ※ **這不是在取代靜態守門**
    （`test_text_encoding.test_nothing_printed_is_unencodable_on_this_console`）。
    那一支守的是「讀得出來」——`※` 比 `\\u203b` 好讀，而且它在**沒有執行到**那一行
    的情況下也看得見。這一層守的是「就算有人繞過了那支守門，結論還是印得完」。
    兩層各自獨立，不要因為有了這一層就把那支拿掉。

    `stderr` 預設已經是 `backslashreplace`（實測），所以那一半其實是保險；
    會死的是 `stdout`（預設 `surrogateescape`，而 `surrogateescape` 在**編碼**方向
    只放行落單的代理字元，正常的不可編碼字元照樣丟例外）。

    **只在 `__main__` 呼叫，不在 import 時。** 整套測試會 import 這個模組；import
    時去動 `sys.stdout` 等於在別人的行程裡留副作用（pytest 的擷取物件、`-s` 底下
    的真 stdout）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            # 被換成非 `TextIOWrapper` 的替身（測試替身、pythonw 底下的 `None`）就
            # 算了：這一層是加分項，拿不到不該讓整支工具起不來。
            continue


def _progress(msg: str) -> None:
    """印一行進度（立即 flush，避免被輸出緩衝藏住而看起來像沉默）。"""
    print(msg, flush=True)


# 結束碼。0 / 1 的語意一個字都沒動；3 是新增的「沒驗到」，理由見 `_emit_skip`。
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 3


def _emit_ok() -> int:
    print(f"{RESULT_PREFIX} OK", flush=True)
    return EXIT_OK


def _short_reason(reason: str) -> str:
    """把任意例外字串壓成單行短原因（結果行必須是單行）。"""
    one_line = " ".join(str(reason).split())
    return one_line[:200] if one_line else "未知原因"


def _emit_fail(reason: str) -> int:
    print(f"{RESULT_PREFIX} FAIL {_short_reason(reason)}", flush=True)
    return EXIT_FAIL


def _emit_skip(reason: str) -> int:
    """「這次什麼都沒驗到」——與「驗了、壞了」是兩件事，不可以共用 FAIL。

    有兩條路會走到這裡，兩條都**不是**失敗，而是「現在不該開瀏覽器，晚點再來」：
    槽被佔住，以及讀到活著的 `webrunner.pid` 而讓位。原本它們跟真正的驗證失敗
    一樣印 `FAIL`、exit 1，於是同一個結論同時代表三件事——「瀏覽器壞了」「driver
    解析不到」「正式作業在跑」。

    為什麼值得分開：`verify_browser.py` 是本專案**規定**用來實證瀏覽器／driver
    改動的入口，而正式批次幾乎總是在跑
    ——也就是說「讓位」其實是最常見的結果。把它印成 FAIL 只有兩種下場，兩種都
    很糟：看到的人以為自己的改動把瀏覽器弄壞了，去追一個不存在的問題；或者學會
    「這支的 FAIL 不用理」，於是真的壞掉那次也一起被忽略。

    **仍然是非零結束碼**，這一點刻意不動：沒驗到就不能回報成功，否則呼叫端會把
    一次根本沒發生的驗證記成綠燈——那是更危險的方向。所以檢查 `== 0` 或 `!= 0`
    的呼叫端行為完全不變，只有想分辨的人才拿得到分辨。

    用 3 不用 2：argparse 打錯參數時自己就 exit 2，而那條路**一行
    `VERIFY-BROWSER:` 都不會印**。共用 2 的話，只看結束碼的自走迴圈會把「指令
    打錯」讀成「正式作業在跑、晚點重試」，然後對一個永遠不可能成功的指令重試到
    天荒地老。
    """
    print(f"{RESULT_PREFIX} SKIP {_short_reason(reason)}", flush=True)
    return EXIT_SKIP


# --------------------------------------------------------------------------
# Chrome options（最小化；只放穩定性 flag，不照抄 production 的 stealth 全套）
# --------------------------------------------------------------------------
def _make_options(profile_dir: str, headless: bool):
    """最小化 Chrome options：暫時 profile + headless + 必要穩定性 flag。

    刻意**不**加 `--disable-gpu`：webrunner 因為要靠 GPU 畫出結果 canvas
    才能偵測產圖，所以禁用 GPU 是它的雷；這支雖然不畫 canvas，但留著 GPU
    讓「GPU 初始化失敗」這種真問題不會被遮掉，驗證更誠實。
    """
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    # 容器 / 受限環境下的穩定性 flag（task 建議「視情況」）。
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # 降噪：避免擴充功能 / 背景連線干擾一個純啟動驗證。
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    if headless:
        opts.add_argument("--headless=new")
    return opts


# --------------------------------------------------------------------------
# 「只回收自己這棵行程樹」的外科式清理（禁止 nuclear sweep）
# --------------------------------------------------------------------------
def _capture_own_tree(service_pid: int | None) -> dict[int, float | None]:
    """chromedriver(service_pid) ＋ 其所有子孫 → `{pid: 建立時間}`。

    在 `driver.quit()` 之前呼叫，這樣即使待會兒有子行程被 reparent 成 orphan，
    我們仍握有它們明確的 PID，能精準回收而不誤傷別人的 chrome。

    **為什麼帶著建立時間，而不是只回一組 pid**（2026-09-09 實測，psutil 7.2.2）：
    PID 會被作業系統回收再發給別的行程，而 capture 與 reap 之間隔著一整個
    `driver.quit()`——那一步正好會讓幾十個 chrome 子行程結束、把它們的 PID 釋放
    回池子裡。之後拿裸 pid 去 `psutil.Process(pid).kill()` 殺的是**現在**那個
    pid 的擁有者，跟我們當初抓到的那個行程沒有任何關係。這台機器上同時有兩個正式
    批次的 Chrome 在跑，誤殺的代價是中斷一個已經跑了幾十小時的批次。

    `(pid, create_time)` 是行程的身分（psutil 的 `Process.__eq__` 用的就是這個）。
    實測兩種寫法的差別：把 capture 當下建立的 `Process` 物件留著再 `kill()`，
    psutil 會丟 `NoSuchProcess: process no longer exists and its PID has been
    reused`、目標活下來；事後才用裸 pid 重建物件，它沒有任何舊身分可比，**一定
    殺得下去**。這裡不留物件而留 `(pid, 建立時間)`，是因為沒有 psutil 的退路需要
    純 int 的 pid。

    值可能是 `None`（psutil 不可用，或那個子行程的建立時間讀不到）——那代表**身分
    無法確認**，`_reap_pids` 會據此跳過而不是照殺。方向是刻意的：留下一個帶著拋棄式
    profile 的 orphan 是無害的（`_rm_profile` 本來就容忍殘留），殺錯一個行程不是。
    """
    tree: dict[int, float | None] = {}
    if not service_pid:
        return tree
    tree[service_pid] = None
    try:
        import psutil  # type: ignore
    except ImportError:
        return tree
    try:
        parent = psutil.Process(service_pid)
        try:
            tree[service_pid] = parent.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
        for child in parent.children(recursive=True):
            try:
                tree[child.pid] = child.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                tree.setdefault(child.pid, None)
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass
    return tree


def _reap_pids(tree: dict[int, float | None]) -> int:
    """殺掉指定的行程（僅限我們這次自己起的那些）。回殺掉的數量。

    優先用 psutil，而且**殺之前先對身分**：`create_time()` 與 capture 當下記下的
    不一樣，就代表這個 PID 已經被回收給別的行程，跳過（理由見 `_capture_own_tree`）。
    對不起來或讀不到一律跳過——「不確定就不殺」。

    psutil 不可用時退回 `taskkill /F /T /PID`。`/T` 連子樹一起殺（catch 被
    reparent 的 renderer），而且永遠**指定 PID**、不是 nuclear `/IM` 全殺；但那條
    路**沒有辦法對身分**（`taskkill` 只認 pid），所以它同時也是 PID 重用風險最高
    的一條。可以接受是因為 psutil 是本專案的必要相依（`CLAUDE.md`），這條路實務上
    不會被走到；真的走到時，殘留一棵沒收乾淨的樹比誤殺好，所以**這裡不會為了「至少
    殺一點」而放寬 psutil 那條路的身分檢查**。
    """
    if not tree:
        return 0
    killed = 0
    use_psutil = True
    try:
        import psutil  # type: ignore
    except ImportError:
        use_psutil = False

    if use_psutil:
        # psutil 已在上面的可用性探測成功匯入、綁在函式作用域，不需再匯入一次。
        for pid, born in tree.items():
            # `born is None` 這條**不是**多餘的安全檢查——下面那個 `!= born` 本來
            # 就會擋掉它（任何真實的建立時間都不等於 `None`）。它存在的理由是**診斷
            # 分得開**：「身分從一開始就沒抓到」（psutil 讀不到那個子行程）與「抓到
            # 了、但這個 PID 已經換人」是兩件不同的主機狀態，前者指向權限問題、後者
            # 指向 PID 回收。兩者都印同一句話的話，看 log 的人分不出該查哪一邊。
            if born is None:
                _progress(f"清理：pid {pid} 的身分沒抓到，跳過（不確定就不殺）")
                continue
            try:
                proc = psutil.Process(pid)
                if proc.create_time() != born:
                    _progress(f"清理：pid {pid} 已被回收給別的行程，跳過")
                    continue
                proc.kill()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return killed

    # Fallback：沒有 psutil。逐個 PID taskkill /T（仍只動我們自己的樹）。
    if os.name == "nt":
        for pid in tree:
            try:
                proc = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=10, check=False,
                )
                if proc.returncode == 0:
                    killed += 1
            except Exception:  # pylint: disable=broad-except
                continue
    return killed


def _rm_profile(profile_dir: str, attempt: int) -> None:
    """刪掉 mkdtemp 的暫時 profile。Windows 上 chrome 剛被殺、handle 可能
    還沒釋放，所以帶幾次 retry；最後再 ignore_errors 兜底。"""
    for _ in range(6):
        try:
            shutil.rmtree(profile_dir)
            _progress(f"[嘗試 {attempt}] 清理：已刪除暫時 profile")
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)
    shutil.rmtree(profile_dir, ignore_errors=True)
    _progress(f"[嘗試 {attempt}] 清理：暫時 profile 已刪除（部分殘留已忽略）")


# --------------------------------------------------------------------------
# 單次 smoke 嘗試（含完整清理）
# --------------------------------------------------------------------------
def _smoke_attempt(url: str, headless: bool, attempt: int) -> tuple[bool, str]:
    """跑一次 smoke：開暫時 profile → Selenium Manager 解析 driver 起 Chrome
    → 載入 url → 讀 current_url / title → 完整清理。回 (是否成功, 原因)。"""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService

    profile_dir = tempfile.mkdtemp(prefix="verify_browser_profile_")
    _progress(f"[嘗試 {attempt}] 暫時 profile：{profile_dir}")

    driver = None
    service_pid: int | None = None
    ok = False
    reason = ""
    try:
        # 不給 executable_path → Selenium ≥4.6 走 Selenium Manager 解析
        # chromedriver（與 production 同一條路徑）。
        service = ChromeService()
        opts = _make_options(profile_dir, headless)
        _progress(f"[嘗試 {attempt}] 解析 driver 並啟動 Chrome（Selenium Manager）…")
        driver = webdriver.Chrome(options=opts, service=service)
        try:
            service_pid = driver.service.process.pid
        except Exception:  # pylint: disable=broad-except
            service_pid = None

        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SEC)
        driver.set_script_timeout(SCRIPT_TIMEOUT_SEC)

        _progress(f"[嘗試 {attempt}] 載入頁面：{url} …")
        driver.get(url)

        _progress(f"[嘗試 {attempt}] 讀取 current_url / title …")
        current_url = driver.current_url
        title = driver.title
        _progress(f"[嘗試 {attempt}] current_url={current_url!r} title={title!r}")
        ok = True
    except Exception as err:  # pylint: disable=broad-except
        reason = repr(err)
    finally:
        # 1) 先在 quit 之前抓下完整行程樹（含可能即將被 reparent 的子行程）。
        own_tree = _capture_own_tree(service_pid)
        # 2) 正常關閉 driver（這步通常已經把 chromedriver + chrome 收掉）。
        if driver is not None:
            try:
                _progress(f"[嘗試 {attempt}] 清理：driver.quit() …")
                driver.quit()
            except Exception as err:  # pylint: disable=broad-except
                _progress(f"[嘗試 {attempt}] driver.quit() 例外（忽略）：{err!r}")
        # 3) 回收 quit 後仍存活的「自己這棵樹」殘留（禁止 nuclear sweep）。
        reaped = _reap_pids(own_tree)
        if reaped:
            _progress(f"[嘗試 {attempt}] 清理：回收本次殘留行程 {reaped} 個")
        # 4) 刪掉暫時 profile。
        _rm_profile(profile_dir, attempt)

    return ok, reason


def run_smoke(url: str, headless: bool) -> int:
    """smoke 模式主流程：跑 `_smoke_attempt`，失敗時自動重試（抗外部 sweep），
    全程時間有界。回 process exit code。"""
    # 先確認 selenium 可 import；這種錯不是「被 sweep 砍掉」，重試無意義。
    try:
        import selenium  # noqa: F401  pylint: disable=unused-import
    except ImportError as err:
        return _emit_fail(f"無法 import selenium：{err}")

    # **截止時刻用 `time.monotonic()`，不是 `time.time()`。** 這裡量的是一段
    # **間隔**（總共可以花多久），而牆鐘是可以被調整的：NTP 的 step 修正、手動改
    # 時鐘、虛擬機快照還原都會讓它跳（**換時區與日光節約時間不會**——它回的是
    # UTC epoch 秒）。往回撥會讓這個上限永遠不觸發，於是「全程時間有界」這個
    # docstring 承諾直接失效；往前撥則讓它立刻觸發，只試一次就放棄。
    # 判準與 `_chrome_slot.acquire` 同一條：這個值不離開本行程 → 可以換；要寫進
    # 檔案或跟別的行程比對的時間點才必須留在 `time.time()`。
    deadline = time.monotonic() + TOTAL_DEADLINE_SEC
    last_reason = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if time.monotonic() > deadline:
            return _emit_fail(
                f"逾總時長上限 {TOTAL_DEADLINE_SEC:.0f}s，最後原因："
                f"{_short_reason(last_reason)}"
            )
        ok, reason = _smoke_attempt(url, headless, attempt)
        if ok:
            return _emit_ok()
        last_reason = reason
        _progress(f"[嘗試 {attempt}] 失敗：{_short_reason(reason)}")
        if attempt < MAX_ATTEMPTS:
            # 失敗徵狀可能是被外部 nuclear sweep 砍掉；重試一次，下一輪會
            # 重建一個全新的 mkdtemp 暫時 profile。
            _progress(
                "可能被外部 sweep 砍掉或啟動暫時失敗；將重試"
                "（重建全新暫時 profile）…"
            )
            time.sleep(2.0)
    return _emit_fail(last_reason)


# --------------------------------------------------------------------------
# 跨行程「單一 Chrome 槽」取鎖 / 讓位 / 釋放（smoke 與 full 共用）
# --------------------------------------------------------------------------
def _nt_pid_alive(pid: int) -> bool:
    """Windows 專用存活探測——**只查詢、不送任何訊號**。與
    `_process_control._nt_pid_alive` 同一套做法（那份是正本；這支刻意自包含、
    不 import bot 側模組，故在此保留一份）。無法判定時回 True（保守）。"""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 明確指定 argtypes/restype：預設 restype 是 c_int，64-bit HANDLE 會被截斷。
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        # PROCESS_QUERY_LIMITED_INFORMATION：夠查存活，且不需要 terminate 權限。
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            # ERROR_ACCESS_DENIED(5) ＝ 行程存在但無權限查詢 → 視為活著。
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pylint: disable=broad-except
        return True


def _pid_alive(pid: int | None) -> bool:
    """盡力判定 pid 是否還活著。**純探測，不得對目標造成任何副作用。** 無法判定回
    True（保守：寧可當成有正式作業在跑而讓位，也不要誤開第二個 Chrome stack）。

    Windows 上**不能**用 ``os.kill(pid, 0)`` 當探測：``signal.CTRL_C_EVENT == 0``，
    所以 CPython 的 Windows ``os.kill`` 會把 signal 0 導向
    ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``——把 ``pid`` 當成 **console
    process group id**、對共用 console 的整組行程送出一個真正的 Ctrl+C。本機實測
    （Windows 11 / CPython 3.14.4）：不存在的 pid 丟 OSError(winerror=11)、自己的
    pid 丟 OSError(winerror=87)、子行程 pid 則「成功」——所以它既不是可靠的存活
    判定，還可能產生副作用。對這支尤其致命：被探測的 pid 來自 ``webrunner.pid``，
    而 `run_batch.py` → `start_webrunner.py` → webrunner 這條鏈是**共用同一個
    console** 的，一旦真的送出 Ctrl+C 就會直接打斷正在跑的批次產圖——恰好是這支
    發誓絕不做的事。``ProcessLookupError`` 在 Windows 也幾乎不會被丟出，那條分支
    等同死碼。故 Windows 走 `_nt_pid_alive`，POSIX 才保留 ``os.kill(pid, 0)``。
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore
        try:
            return psutil.pid_exists(int(pid))
        except Exception:  # pylint: disable=broad-except
            return True
    except ImportError:
        pass
    if os.name == "nt":
        return _nt_pid_alive(int(pid))
    try:
        os.kill(int(pid), 0)  # POSIX：不送訊號、只探測存在性
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _live_webrunner_pid() -> tuple[int | None, bool]:
    """讀 PROJECT_ROOT/webrunner.pid。回 `(pid, 判定得出來嗎)`。

    `_chrome_slot` 順序契約：拿到槽後若這裡有活著的 webrunner → 讓位、不開瀏覽器。

    **第二個回傳值不是裝飾品。** 這個判斷是這支腳本唯一擋住「在正式批次旁邊另開
    一套瀏覽器」的關卡——正式的產圖程式**不會**去搶 Chrome 槽（實測：批次跑著的
    時候 `chrome_slot.lock` 並不存在），所以槽在這裡完全沒有保護作用。

    原本檔案存在但讀不出內容時一律回 `None`，而 `None` 在呼叫端的意思是**「沒有
    正式作業，可以開瀏覽器」**——判不出來卻走了樂觀的那一邊。CLAUDE.md 的
    Windows PID 存活硬規則對這個情境有明確指示：**凡是在問「我該不該不要啟動／
    讓位？」的地方，判不出來就要走保守的那一邊**，否則會樂觀地多開一整套瀏覽器。

    三種情況要分清楚：

    * 檔案**不存在** → 真的沒有正式作業 → `(None, True)`，可以開。
    * 檔案**存在但讀不出來**（權限、IO 錯誤、內容不是合法 UTF-8）→ **判不出來**
      → `(None, False)`，呼叫端要讓位。
    * 檔案存在、讀得出來但不是數字 → 同上，判不出來。

    `UnicodeDecodeError` 要單獨列：它是 `ValueError` 的子類、**不是** `OSError`，
    所以原本的 `except (FileNotFoundError, OSError)` 接不到它，內容不是合法 UTF-8
    時會直接往上炸穿這個函式。
    """
    if not WEBRUNNER_PID_FILE.exists():
        return None, True                    # 沒有檔案＝真的沒有正式作業
    try:
        raw = WEBRUNNER_PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, True                    # 剛剛才被刪掉，等同不存在
    except (OSError, UnicodeDecodeError):
        return None, False                   # 檔案在、但讀不出來 → 判不出來
    try:
        pid = int(raw)
    except ValueError:
        return None, False                   # 內容不是數字 → 判不出來
    return (pid if _pid_alive(pid) else None), True


def _run_with_slot(label: str, run_fn) -> int:
    """smoke / full 共用的外層流程：取得 Chrome 槽 → 依契約讓位 → 跑 run_fn →
    finally 釋放。run_fn 回傳 process exit code。

    1. `_chrome_slot.acquire("verify", timeout=…)` 拿不到 → SKIP「slot busy」、
       exit 3（呼叫端的自走迴圈晚點重試）。
    2. 拿到後讀 webrunner.pid；有活著的正式 webrunner → 釋放並 SKIP「讓位」、exit 3。
    2b. pid 檔在、但讀不出內容 → 判不出來 → 釋放並 SKIP、exit 3（保守，不開瀏覽器）。
    3. 否則跑 run_fn（真正開瀏覽器的工作）。
    4. 無論成敗，finally 釋放槽（release 只在本行程仍持有時刪檔、永不 raise、冪等）。
    """
    try:
        import _chrome_slot  # repo-local 被動共用模組（與 webrunner / bot 同一把鎖）
    except ImportError as err:
        return _emit_fail(f"無法 import _chrome_slot：{err}")

    _progress(f"取得跨行程 Chrome 槽（owner={SLOT_OWNER}, label={label!r}）…")
    if not _chrome_slot.acquire(
            SLOT_OWNER, timeout=SLOT_ACQUIRE_TIMEOUT_SEC, label=label):
        return _emit_skip("slot busy（有作業進行中，稍後再試）")
    try:
        live_pid, decided = _live_webrunner_pid()
        if live_pid is not None:
            # 讓位：finally 會釋放槽（release 冪等，這裡不重複呼叫）。
            _progress(f"偵測到正式作業仍在執行（pid={live_pid}）；讓位、不開瀏覽器")
            return _emit_skip("有正式作業在執行中，讓位")
        if not decided:
            # pid 檔在、但內容讀不出來：**判不出來就不要開瀏覽器**。這條也是
            # SKIP 不是 FAIL——瀏覽器根本沒被啟動過，沒有任何證據說它壞了。
            _progress("webrunner.pid 存在但讀不出內容；無法判定是否有正式作業"
                      "在執行，保守起見不開瀏覽器")
            return _emit_skip("pid 檔讀不出來，無法判定，保守略過")
        _progress("已取得 Chrome 槽且無正式作業在跑；開始驗證")
        return run_fn()
    finally:
        _chrome_slot.release(SLOT_OWNER)
        _progress("已釋放 Chrome 槽")


# --------------------------------------------------------------------------
# full 模式：持鎖、隔離、登入後端到端可達性驗證（以子行程呼叫 webrunner 驗證模式）
# --------------------------------------------------------------------------
def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """逾時殺掉 full 子行程（連同它生出來的 Chrome 子樹）。指定 PID、不 /IM 全殺。"""
    try:
        import psutil  # type: ignore
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except Exception:  # pylint: disable=broad-except
                    pass
            parent.kill()
            return
        except Exception:  # pylint: disable=broad-except
            pass
    except ImportError:
        pass
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10, check=False)
        else:
            proc.kill()
    except Exception:  # pylint: disable=broad-except
        pass


def _rm_workdir(workdir: str) -> None:
    """刪掉 full 的隔離 tempdir。Chrome 剛被殺、handle 可能還沒釋放，所以帶幾次
    retry，最後 ignore_errors 兜底（殘留是無害的、帶自己丟棄式 profile 的 orphan）。"""
    for _ in range(6):
        if not os.path.isdir(workdir):
            return
        try:
            shutil.rmtree(workdir)
            return
        except OSError:
            time.sleep(0.5)
    shutil.rmtree(workdir, ignore_errors=True)


def _extract_setup_fail(output: str) -> str:
    """從子行程輸出抓 `VERIFY-SETUP: FAIL <原因>` 那行的原因。找不到回空字串。"""
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("VERIFY-SETUP: FAIL"):
            return s[len("VERIFY-SETUP: FAIL"):].strip()
    return ""


# --------------------------------------------------------------------------
# full 模式：一次嘗試，外加**只對「瀏覽器不見了」**的重試
# --------------------------------------------------------------------------
# smoke 從第一天就會重試，理由寫在 `run_smoke` 裡：失敗徵狀可能是被外部 nuclear
# sweep 砍掉。**full 一直是一次定生死，而它才是最容易撞上那件事的那一個**——它跑
# 得久（分鐘級，smoke 是秒級）、開的是有頭的真瀏覽器、而且整條流程有幾十個 driver
# 指令，每一個都是「這一刻瀏覽器還在不在」的取樣點。
#
# 2026-09-12 量到這個不對稱的代價：同一道指令、同一份程式碼、相隔幾分鐘跑兩次，
# 第一次死在 `select_model`（`InvalidSessionIdException … not connected to
# DevTools`），第二次從登入到 Generate 按鈕全綠。`chromedriver.prev.log` 顯示點開
# 模型選單的 `PerformActions` 成功、約 1.5 秒後挑選項的 `ExecuteScript` 就已經連不
# 上 DevTools——**是瀏覽器行程不見了，不是腳本錯誤**；而且不是全機掃殺（跟這次無關、
# 14:02 就在的 15 個 `chrome.exe` 兩次都活得好好的）。本檔記著
# 瀏覽器死亡多半來自我們自己的測試工具或顯示驅動層。
#
# **代價不是「多花一次時間」，是假的紅燈。** 驗證守則叫人「改了瀏覽器／driver 就
# 跑這支」，所以一個環境性的 FAIL 會被讀成「我的改動把瀏覽器弄壞了」，而最可能的
# 下一步是去改一段本來沒問題的程式碼。
#
# ⚠️ **重試只對這一族開。** 登入失敗、DOM 找不到、子行程逾時一律**不重試**：那些
# 重跑一次只是把同一個真問題再確認一次，而且會讓一個穩定的缺陷看起來像「有時候會
# 過」。同理，成功時**一定要把重試講出來**——安靜地變綠等於把環境的不穩定藏起來，
# 那是另一種說謊。
#
# 每一筆判準都寫清楚它認的是什麼徵狀；**寧可漏認**（退回今天的一次定生死）也不要
# 誤認，誤認的代價是把真的紅燈變成偶爾綠。
_BROWSER_GONE_MARKERS = (
    # webrunner 自己把「瀏覽器在指令中途不見了」包成這個例外。
    "browsergoneerror",
    # chromedriver 對一個已經沒了的 session 的標準回覆。
    "invalid session id",
    # 同上，附帶說明 DevTools 連線已斷。
    "not connected to devtools",
    # chrome 行程還在但 DevTools port 連不上（多半正在死）。
    "chrome not reachable",
    # renderer 被砍掉時 chromedriver 的說法。
    "unable to connect to renderer",
    # chromedriver 行程自己不見了：selenium 這一端是 urllib3 的連線被拒。
    # 2026-09-07 那一族瀏覽器死亡最早的徵狀就是這個。
    "winerror 10061",
)
# ⚠️ **刻意不收 `connection refused` 與 `max retries exceeded`。** 兩者都是
# urllib3 的泛用字串，登入那一步連站台連不上時也會出現——收進來的話判準就從
# 「瀏覽器不見了」漂成「任何連線問題」，而那正是本段開頭說的誤認：把真的紅燈
# 變成偶爾綠。`winerror 10061` 夠具體（本機 TCP 連線被拒），而本專案與站台之間
# 的流量一律走瀏覽器、不走 urllib3，所以那個碼在這裡只可能是 chromedriver。

# full 跑一趟要分鐘級，所以只給**一次**重試；smoke 是秒級才給到三次。
FULL_MAX_ATTEMPTS = 2
# 兩趟各自的 watchdog 是 `FULL_DEADLINE_SEC`，這裡是**整體**上限（含重試與清理），
# 理由同 `run_smoke` 的 `TOTAL_DEADLINE_SEC`：沒有整體上限的話，「有界」只是每一趟
# 有界，趟數一多就不是了。一樣用 `time.monotonic()`——量的是間隔，不是時刻。
FULL_TOTAL_DEADLINE_SEC = 2 * FULL_DEADLINE_SEC + 60.0


def _is_browser_gone(reason: str) -> bool:
    """這個失敗原因是不是「瀏覽器／driver 在中途不見了」。

    ⚠️ 判準刻意只認**徵狀字串**，不認「訊息裡有沒有提到瀏覽器」——
    `找不到 Generate 按鈕` 這種訊息也會提到瀏覽器的狀態，而那是**真的**失敗，
    重跑一次只會再失敗一次（或更糟：偶爾過）。
    """
    low = (reason or "").lower()
    return any(marker in low for marker in _BROWSER_GONE_MARKERS)


def _full_attempt(generate: bool, attempt: int) -> tuple[bool, str]:
    """跑一趟隔離的端到端驗證。回 `(成功, 失敗原因)`。

    刻意**不**在這裡 `_emit_ok` / `_emit_fail`：結果行由 `run_full` 統一印一次，
    否則重試會印出兩行 `VERIFY-BROWSER:`，而那個字串正是別人拿來判定結果的契約。
    """
    workdir = tempfile.mkdtemp(prefix="verify_browser_full_")
    profile_dest = os.path.join(workdir, "profile")
    output_dir = os.path.join(workdir, "output")
    env = dict(os.environ)
    # 子行程是 **Python**，而且 stdout 接的是**管線不是主控台**——這種情況下
    # CPython 用系統地區編碼（本機 cp950）編碼它自己的輸出。下面 `Popen` 的
    # `encoding="utf-8"` 只管**我們這一端怎麼解碼**，管不到子行程怎麼編碼，
    # 而 `errors="replace"` 會把不一致變成一串 U+FFFD 而不是例外。
    # 實測（乾淨環境）：三行繁中進度行回來變成 26 個 U+FFFD；結論行
    # `VERIFY-BROWSER: OK` 是純 ASCII 所以照樣讀得到——工具**看起來**正常，
    # 只有進度行被吃掉，這就是它一直沒被發現的原因。補上之後同樣的量法是 0 個
    # （2026-09-12 以 `--full` 端到端實跑確認）。
    # ⚠️ 開發者的殼往往自己就 export 了 `PYTHONIOENCODING`，所以「我跑過沒問題」
    # 對這件事完全不算數；排程工作或一般 cmd.exe 起的行程才是真實情境。
    env["PYTHONIOENCODING"] = "utf-8"
    env["NAI_VERIFY_MODE"] = "setup"
    env["NAI_VERIFY_PROFILE_DEST"] = profile_dest
    env["NAI_VERIFY_OUTPUT_DIR"] = output_dir
    if generate:
        env["NAI_VERIFY_GENERATE"] = "1"
    else:
        env.pop("NAI_VERIFY_GENERATE", None)

    cmd = [sys.executable, "-u", str(WEBRUNNER_SCRIPT)]
    _progress(f"[full 嘗試 {attempt}] 以子行程啟動隔離驗證模式"
              f"（generate={generate}）…")
    _progress("（會嘗試以快照登入態登入並導航到產圖頁，可能需要數十秒～數分鐘）")
    _progress(f"[full 嘗試 {attempt}] 隔離工作目錄：{workdir}")

    try:
        # 子行程印繁中進度行。**兩端都要指定**：這裡的 `encoding="utf-8"` 是
        # 解碼端，編碼端在上面的 `env["PYTHONIOENCODING"]`。原本這段註解只寫了
        # 解碼端，而且把原因寫成「會 UnicodeDecodeError 砍掉整個 full 驗證」——
        # 真正的下場比那個安靜：`errors="replace"` 不會拋，只是把中文換成
        # U+FFFD。
        proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        timed_out = {"flag": False}

        def _on_deadline() -> None:
            timed_out["flag"] = True
            _kill_proc_tree(proc)

        watchdog = threading.Timer(FULL_DEADLINE_SEC, _on_deadline)
        watchdog.start()
        captured: list[str] = []
        saw_ok = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                captured.append(line)
                _progress(f"  | {line}")  # 前綴避免與外層 VERIFY-BROWSER: 結果行混淆
                if "VERIFY-SETUP: OK" in line:
                    saw_ok = True
            proc.wait()
        finally:
            watchdog.cancel()

        output = "\n".join(captured)
        if timed_out["flag"]:
            # **逾時刻意不進重試族**：子行程靜默卡死是真的訊號，重跑一次只是再等
            # 一次 `FULL_DEADLINE_SEC`。
            return False, f"full 子行程逾時（>{FULL_DEADLINE_SEC:.0f}s）已中止"
        if proc.returncode == 0 and saw_ok:
            return True, ""
        return False, (_extract_setup_fail(output)
                       or f"子行程 rc={proc.returncode}")
    finally:
        # 子行程的隔離 profile/output 自己也會清；這裡 rmtree 整個 tempdir 當兜底。
        # 放 finally 確保任何例外（含 Popen 失敗）都不留 workdir。chrome 剛被殺、
        # handle 可能還沒釋放，所以帶幾次 retry 再 ignore_errors 收尾。
        _rm_workdir(workdir)


def run_full(generate: bool) -> int:
    """持鎖、隔離的「登入＋導航＋確認產圖介面/關鍵 DOM 可達」端到端驗證。

    以**子行程**帶隔離環境呼叫 webrunner 的 opt-in 驗證模式
    （`NAI_VERIFY_MODE=setup`），不 import webrunner（避免 import 副作用）。隔離由
    webrunner 端保證：快照登入 profile 到隔離目錄（不寫回 `.chrome_profile/`）、不寫
    `webrunner.pid`、不碰 todo/prompt/undesired、輸出進隔離目錄並清掉。本端把這些隔離
    目的地放在一個用完即丟的 tempdir 下，子行程結束後再 rmtree 兜底。

    子行程輸出即時轉印（webrunner 會吐進度行），同時用 watchdog 在 FULL_DEADLINE_SEC
    硬性逾時（即使子行程靜默卡死也砍得掉）。回 process exit code。

    失敗徵狀若是「瀏覽器中途不見了」會**重試一次**（見 `_is_browser_gone` 上面那段）；
    其他失敗一次定生死。重試成功時會明確印出來，不會安靜地變綠。
    """
    if not WEBRUNNER_SCRIPT.exists():
        return _emit_fail("找不到背景驗證入口檔")
    try:
        import selenium  # noqa: F401  pylint: disable=unused-import
    except ImportError as err:
        return _emit_fail(f"無法 import selenium：{err}")

    deadline = time.monotonic() + FULL_TOTAL_DEADLINE_SEC
    last_reason = ""
    for attempt in range(1, FULL_MAX_ATTEMPTS + 1):
        ok, reason = _full_attempt(generate, attempt)
        if ok:
            if attempt > 1:
                # **不要安靜地變綠。** 前一趟真的死了一次瀏覽器，那是這台機器的
                # 狀態資訊；吞掉它等於把環境的不穩定藏起來。
                _progress(
                    f"※ 這次是**第 {attempt} 趟**才過的：第 {attempt - 1} 趟的"
                    f"瀏覽器中途不見了（{_short_reason(last_reason)}）。"
                    "程式碼沒問題，但這台機器上有東西在殺瀏覽器——若反覆出現，"
                    "記下當下有沒有別的行程在跑測試。")
            return _emit_ok()
        last_reason = reason
        _progress(f"[full 嘗試 {attempt}] 失敗：{_short_reason(reason)}")
        if attempt >= FULL_MAX_ATTEMPTS:
            break
        if not _is_browser_gone(reason):
            # 真的失敗：不重試。重跑一次只會把同一個問題再確認一次，而且會讓一個
            # 穩定的缺陷看起來像「有時候會過」。
            _progress("徵狀不是「瀏覽器不見了」（登入／DOM／逾時都算真的失敗）；"
                      "不重試。")
            break
        if time.monotonic() > deadline:
            _progress(f"已逾整體上限 {FULL_TOTAL_DEADLINE_SEC:.0f}s，不再重試。")
            break
        _progress("徵狀是「瀏覽器中途不見了」（不是登入或 DOM 失敗）；重試一次…")
        time.sleep(3.0)
    return _emit_fail(last_reason)


# --------------------------------------------------------------------------
# je 變體的 smoke（`--variant je`）：子行程裡走 `je_web_runner` 的 `set_driver`
# --------------------------------------------------------------------------
# 為什麼需要它（2026-09-20）：兩個變體的 driver 其實都由 Selenium Manager 解析——je 那
# 一側 `set_driver` 裡的 `ChromeDriverManager(...).install()` 回傳值被丟掉，真正用的是
# 呼叫端那個沒帶 `executable_path` 的 `ChromeService`。所以上面那條 smoke 過了，driver 解析就過了。**je 變體獨有、在此之前沒有任何
# 入口驗得到的是另外兩件事**：`je_web_runner` 自己的 `set_driver`／選項組裝，以及那
# 一次 `install()`——一個會丟例外、一丟就讓整個 `set_driver` 失敗的網路相依。而 je 是
# `/run` 的預設變體，bot 的退路又會在 je 起不來時安靜改跑 selenium，所以「je 壞了」
# 在正式環境裡通常看不見。
#
# **為什麼一定是子行程、工作目錄一定在丟棄式暫存目錄：**
#   * `je_web_runner` **匯入時**就開一個 `RotatingFileHandler`，檔名是相對路徑
#     `WEBRunner.log`、附加模式。工作目錄在 repo 根目錄時，在大小寫不分的磁碟上那個檔
#     **就是**正式批次正在寫的 `webrunner.log`。（做這一段時自己就踩了一次：在 repo
#     的上一層跑一行 `import je_web_runner` 探路，當場在那裡留下一個空的記錄檔。）
#   * `install()` 的快取跟著 `Path.cwd()` 走；正式環境落在 repo 根目錄的 `.wdm`。
#   * 同一個行程裡「先換工作目錄、匯入、再換回來」做不乾淨：handler 會一直握著暫存
#     目錄裡那個檔（Windows 上整個目錄刪不掉），匯入還會把 root logger 設成 DEBUG，
#     兩者都留在呼叫端的行程裡。子行程一結束就全部放掉，父行程再整個刪掉。
#   * 環境變數 `WDM_LOCAL` 為真時，快取改落在 `sys.path[0]/.wdm`——子行程的
#     `sys.path[0]` 是本檔所在的 `axiomatic/`，也就是 repo 裡面。所以子行程的環境
#     一律拿掉它。
#
# 代價：丟棄式快取裡沒有舊的 driver，所以每一趟都會真的下載一次。這比正式環境**嚴格**
# （正式環境的快取有效一天），方向是對的——下載會失敗的話，正式環境最晚隔天也會失敗。

_THIS_FILE = Path(__file__).resolve()
# 父行程叫子行程時帶的內部旗標。**不在 argparse 裡**（`--help` 看不到），由 `main` 在
# 解析之前分流。手打它也開不出瀏覽器：子行程要看到 Chrome 槽握在自己的祖先行程手上
# 才肯動（`_slot_held_by_an_ancestor`）。
JE_CHILD_FLAG = "--internal-je-child"
# 子行程唯一的結論行前綴。刻意**不是** `VERIFY-BROWSER:`——那一行只能由父行程印一次。
JE_CHILD_PREFIX = "VERIFY-JE-CHILD:"
# 單趟子行程的看門狗：一趟要真的下載一次 driver 再開 Chrome，所以比 smoke 的單步逾時
# 寬，但仍然有界。整體上限（含重試）理由同 `TOTAL_DEADLINE_SEC`。
JE_ATTEMPT_DEADLINE_SEC = 180.0
JE_TOTAL_DEADLINE_SEC = 420.0
# 子行程從哪裡載入 `je_web_runner`：與 `webrunner_je_only` 開頭那段**同一條規則**
# （`WEBRUNNER_PATH` 環境變數，否則 repo 旁邊的 `WebRunner/` 開發簽出，都沒有就是已安裝
# 的套件）。本檔不 import 任何 webrunner，只能照抄；兩份由 `test_verify_browser` 對帳
# ——正式變體換了來源而這裡沒跟上的話，驗到的就是另一份程式碼。
_JE_SIBLING_WEBRUNNER = Path(__file__).resolve().parent.parent.parent / "WebRunner"

# 與 `webrunner_je_only.start_driver` 的 `cli_args` 同一組旗標，差別只有三處：
#   * 正式變體那兩個「開在螢幕外」的旗標（`--window-position` / `--window-size`）換成
#     `--headless=new`；`--headed` 時換成 `--start-maximized`——那個模式就是要讓人看，
#     而它只開 `about:blank`，不會有產出的圖。預設（headless）驗證本來就不該在
#     桌面上彈一個最大化視窗；
#   * 不帶 `--user-agent=`：那是對站台的偽裝，值是正式變體的常數，照抄一份只會多一個
#     會漂移的拷貝，而 `about:blank` 不看它；
#   * `--user-data-dir=` 指向丟棄式 profile，不是正式登入態的 snapshot。
# 另外多兩個全新 profile 才需要的首次執行旗標。其餘逐字相同，由 `test_verify_browser`
# 對著正式那一份的 AST 對帳：正式變體加了一個會讓 Chrome 起不來的旗標而這裡沒跟上，
# 這支就會印一個看起來很正常的 OK。
_JE_SMOKE_FLAGS = (
    "--disable-blink-features=AutomationControlled",
    "--lang=en-US",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disk-cache-size=33554432",
    "--media-cache-size=33554432",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-background-timer-throttling",
    "--no-first-run",
    "--no-default-browser-check",
)

# 子行程回報的失敗階段 → 結論行上的說明。`install` 與 `launch` 分開是這一段的重點：
# 前者去查網路／driver 管理器，後者去查瀏覽器本體與 chromedriver 記錄。
_JE_STAGE_LABELS = {
    "install": "driver 管理器安裝失敗（set_driver 裡的 install()）",
    "launch": "瀏覽器起不來（set_driver 在 install() 之後的選項組裝或啟動瀏覽器失敗）",
    "page": "瀏覽器起來了，但載入或讀取頁面失敗",
    "import": "載不進 je 變體的相依",
    "refused": "子行程拒絕執行",
    "timeout": "子行程逾時已中止",
    "child": "子行程沒有留下可辨識的結論",
}
# 子行程自己會印的階段（其餘兩個由父行程判定）。
_JE_CHILD_STAGES = frozenset({"install", "launch", "page", "import", "refused"})
# 會重試的階段。`install` 也在內：正式的 `start_driver` 把整個 `set_driver`（含
# `install()`）重試三次，所以驗證這邊重試同樣次數才是「正式環境會不會起不來」的答案。
# `import`／`refused`／`timeout`／`child` 重跑也不會變，重試只會讓一個穩定的問題看起來
# 像「有時候會過」。
_JE_RETRY_STAGES = frozenset({"install", "launch", "page"})


def _je_experimental_options() -> dict:
    """與 `webrunner_je_only.start_driver` 的 `experimental` 逐字相同（每次給新的一份）。"""
    return {
        "excludeSwitches": ["enable-automation"],
        "useAutomationExtension": False,
    }


def _je_cli_args(profile_dir: str, headless: bool) -> list[str]:
    """je smoke 交給 `set_driver(options=…)` 的旗標（差異的理由見 `_JE_SMOKE_FLAGS`）。"""
    args = list(_JE_SMOKE_FLAGS)
    args.append("--headless=new" if headless else "--start-maximized")
    args.append(f"--user-data-dir={profile_dir}")
    return args


def _is_inside_repo(path) -> bool:
    """`path` 是不是 repo 本身或它底下的東西（解開連結、不分大小寫）。

    用 `commonpath` 而不是字串前綴：`Axiomatic_old` 跟 `Axiomatic` 共用前綴，卻不在
    repo 裡。不同磁碟機時 `commonpath` 丟 `ValueError`，那一定不在 repo 裡。
    """
    root = os.path.normcase(os.path.realpath(PROJECT_ROOT))
    target = os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        return False


def _je_workdir(base: str | None = None) -> str | None:
    """在 `base`（預設系統暫存目錄）底下開一個丟棄式工作目錄；`base` 落在 repo 裡就回
    `None`，**什麼都不建**。

    系統暫存目錄可以被環境變數改掉（`TMP`／`TEMP`），指到 repo 裡的話，driver 快取、
    chromedriver 記錄與函式庫的記錄檔全都會寫進 repo——所以先看 `base` 再建，而不是建完
    才發現。`base` 刻意是 `None` 再當場查，不寫成預設引數：預設引數在定義時就綁定，測試
    換掉 `tempfile.gettempdir` 也換不到它。
    """
    if base is None:
        base = tempfile.gettempdir()
    if _is_inside_repo(base):
        return None
    return tempfile.mkdtemp(prefix="verify_browser_je_", dir=base)


def _je_failure_stage(err: BaseException) -> str:
    """`set_driver` 丟出來的例外是 driver 管理器那一步（`install`）還是之後（`launch`）。

    `set_driver` 把兩者包成同一個 `WebRunnerException`（`raise … from error`），訊息也
    一樣以 `set_driver failed:` 開頭，所以只能看**原始例外經過了哪些程式碼**：沿著
    `__cause__`／`__context__` 把整條鏈的 traceback 走一遍，任何一個 frame 的檔案路徑有
    一段**剛好是** `webdriver_manager` 就是安裝那一步（網路錯誤的 frame 在 requests／
    urllib3 裡，但它是從 driver 管理器的下載函式一路呼叫下去的，那幾格一定在鏈上）。
    比對的是路徑的**一段**，不是子字串——`my_webdriver_manager_notes` 不算。
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [err]
    while stack:
        exc = stack.pop()
        if exc is None or id(exc) in seen:
            continue
        seen.add(id(exc))
        tb = exc.__traceback__
        while tb is not None:
            parts = {p.lower() for p in Path(tb.tb_frame.f_code.co_filename).parts}
            if "webdriver_manager" in parts:
                return "install"
            tb = tb.tb_next
        stack.extend((exc.__cause__, exc.__context__))
    return "launch"


def _slot_held_by_an_ancestor() -> bool:
    """Chrome 槽現在是不是握在本行程的某個**祖先**手上（owner 是 `SLOT_OWNER`）。

    子行程入口的守門：槽與讓位都由父行程負責，所以子行程只在「持有者是我的祖先」時才
    開瀏覽器。只比父行程 pid 不夠——`.venv` 的 `python.exe` 是轉接殼，真正的父行程常常
    是祖父。持有者是**自己**也不算：那代表有人在同一個行程裡直接呼叫子行程入口，而這條
    路之所以要子行程，正是因為匯入 `je_web_runner` 會把副作用留在呼叫端的行程裡。

    判不出來一律回 False（不開瀏覽器）：讀不到槽、沒有 psutil、列不出祖先都算。
    """
    try:
        import _chrome_slot  # repo-local 被動共用模組
        import psutil  # type: ignore
    except ImportError:
        return False
    try:
        holder = _chrome_slot.read_holder()
    except Exception:  # pylint: disable=broad-except
        return False
    if not isinstance(holder, dict) or holder.get("owner") != SLOT_OWNER:
        return False
    pid = holder.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        ancestors = {proc.pid for proc in psutil.Process().parents()}
    except Exception:  # pylint: disable=broad-except
        return False
    return pid in ancestors


def _je_log_tail(path: str, max_lines: int = 15) -> list[str]:
    """丟棄式 chromedriver 記錄的最後幾行。父行程會把整個目錄刪掉，這是唯一的紀錄。

    selenium 包出來的啟動失敗往往沒有原因（例外的 `args` 是空的），原因在這份記錄裡。
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 16384))
            raw = handle.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def _je_child_main(argv: list[str]) -> int:
    """（子行程）用 `je_web_runner` 的 `set_driver` 起 Chrome、載入頁面、收掉。

    `argv` 是 `[url, "1"|"0"]`（第二個是 headless）。結論印一行
    `VERIFY-JE-CHILD: OK` 或 `VERIFY-JE-CHILD: FAIL <階段> <原因>`，由父行程轉成
    `VERIFY-BROWSER:` 那一行。

    **兩道守門都在匯入 `je_web_runner` 之前**，順序不能換：工作目錄在 repo 裡就拒絕
    （匯入本身就會寫檔），槽不在祖先手上就拒絕（否則這個入口等於一條繞過讓位的路）。

    載入與讀頁面用的是 `set_driver` 回來的那個 selenium driver，**不是**包裝層的
    `to_url`／`get_current_url`：包裝層的存取函式會吞掉例外、回 None，拿來判結論的話，
    一個沒載入的頁面也會被讀成成功。
    """
    url = argv[0] if argv else "about:blank"
    headless = not (len(argv) > 1 and argv[1] == "0")

    def _fail(stage: str, reason: str) -> int:
        print(f"{JE_CHILD_PREFIX} FAIL {stage} {_short_reason(reason)}", flush=True)
        return EXIT_FAIL

    cwd = os.getcwd()
    if _is_inside_repo(cwd):
        return _fail("refused", "工作目錄在 repo 裡；je_web_runner 匯入時會在工作目錄"
                                "開記錄檔，拒絕匯入")
    if not _slot_held_by_an_ancestor():
        return _fail("refused", "Chrome 槽不在祖先行程手上；這個入口只給 --variant je "
                                "的父行程用")

    env_path = os.environ.get("WEBRUNNER_PATH")
    webrunner_path = str(Path(env_path)) if env_path else str(_JE_SIBLING_WEBRUNNER)
    if webrunner_path not in sys.path:
        sys.path.insert(0, webrunner_path)
    try:
        from selenium.webdriver.chrome.service import Service as ChromeService
        from je_web_runner import webdriver_wrapper_instance as wr
    except Exception as err:  # pylint: disable=broad-except
        return _fail("import", repr(err))
    origin = getattr(sys.modules.get("je_web_runner"), "__file__", None) or "?"
    _progress(f"[je] je_web_runner 來源：{origin}")

    profile_dir = tempfile.mkdtemp(prefix="verify_browser_je_profile_", dir=cwd)
    log_path = os.path.join(cwd, "chromedriver.log")
    _progress(f"[je] 丟棄式 profile：{profile_dir}")

    stage, reason = "", ""
    service_pid: int | None = None
    try:
        try:
            service = ChromeService(log_output=log_path,
                                    service_args=["--log-level=INFO"])
            _progress("[je] 呼叫 set_driver（driver 管理器 install() ＋ 啟動 Chrome）…")
            wr.set_driver("chrome", options=_je_cli_args(profile_dir, headless),
                          experimental_options=_je_experimental_options(),
                          service=service)
        except Exception as err:  # pylint: disable=broad-except
            stage, reason = _je_failure_stage(err), repr(err.__cause__ or err)
        else:
            driver = wr.current_webdriver
            try:
                service_pid = driver.service.process.pid
            except Exception:  # pylint: disable=broad-except
                service_pid = None
            try:
                driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SEC)
                driver.set_script_timeout(SCRIPT_TIMEOUT_SEC)
                _progress(f"[je] 載入頁面：{url} …")
                driver.get(url)
                _progress(f"[je] current_url={driver.current_url!r} "
                          f"title={driver.title!r}")
            except Exception as err:  # pylint: disable=broad-except
                stage, reason = "page", repr(err)
    finally:
        # 與 `_smoke_attempt` 同一個順序：quit **之前**抓下自己那棵樹，quit 之後只回收
        # 身分對得上的那些（禁止 nuclear sweep）。
        own_tree = _capture_own_tree(service_pid)
        try:
            _progress("[je] 清理：wr.quit() …")
            wr.quit()
        except Exception as err:  # pylint: disable=broad-except
            _progress(f"[je] wr.quit() 例外（忽略）：{err!r}")
        reaped = _reap_pids(own_tree)
        if reaped:
            _progress(f"[je] 清理：回收本次殘留行程 {reaped} 個")

    if stage:
        for line in _je_log_tail(log_path):
            _progress(f"[je] chromedriver 記錄 | {line}")
        return _fail(stage, reason)
    print(f"{JE_CHILD_PREFIX} OK", flush=True)
    return EXIT_OK


def _parse_je_child_line(line: str) -> tuple[bool, str, str] | None:
    """子行程的一行輸出 → `(成功, 階段, 原因)`；不是結論行就回 None。"""
    text = line.strip()
    if not text.startswith(JE_CHILD_PREFIX):
        return None
    rest = text[len(JE_CHILD_PREFIX):].strip()
    if rest == "OK":
        return True, "", ""
    word, _, tail = rest.partition(" ")
    stage, _, reason = tail.strip().partition(" ")
    if word != "FAIL" or stage not in _JE_CHILD_STAGES:
        return False, "child", f"結論行無法辨識：{text[:120]!r}"
    return False, stage, reason.strip() or "未知原因"


def _je_attempt(url: str, headless: bool, attempt: int) -> tuple[bool, str, str]:
    """開一個子行程跑一趟 je smoke。回 `(成功, 階段, 原因)`。

    與 `_full_attempt` 同一個形狀：看門狗、逐行轉印（加 `  | ` 前綴）、`finally` 刪掉
    整個丟棄式工作目錄。結果行由 `run_smoke_je` 統一印一次。
    """
    workdir = _je_workdir()
    if workdir is None:
        return (False, "refused",
                "系統暫存目錄落在 repo 裡；拒絕在 repo 裡建 driver 快取與記錄檔")
    _progress(f"[je 嘗試 {attempt}] 丟棄式工作目錄：{workdir}")
    # 子行程是 Python、stdout 是管線：編碼端也要指定（理由見 `_full_attempt`）。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("WDM_LOCAL", None)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(_THIS_FILE), JE_CHILD_FLAG, url,
             "1" if headless else "0"],
            cwd=workdir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        timed_out = {"flag": False}

        def _on_deadline() -> None:
            timed_out["flag"] = True
            _kill_proc_tree(proc)

        watchdog = threading.Timer(JE_ATTEMPT_DEADLINE_SEC, _on_deadline)
        watchdog.start()
        verdict = None
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                _progress(f"  | {line}")
                parsed = _parse_je_child_line(line)
                if parsed is not None:
                    verdict = parsed
            proc.wait()
        finally:
            watchdog.cancel()
        if timed_out["flag"]:
            return (False, "timeout",
                    f"je 子行程逾時（>{JE_ATTEMPT_DEADLINE_SEC:.0f}s）已中止")
        if verdict is None:
            return False, "child", f"子行程 rc={proc.returncode}"
        if verdict[0] and proc.returncode != 0:
            return False, "child", f"子行程印了 OK 卻以 rc={proc.returncode} 結束"
        return verdict
    finally:
        _rm_workdir(workdir)


def run_smoke_je(url: str, headless: bool) -> int:
    """`--variant je` 的主流程：跑 `_je_attempt`，暫時性的失敗重試，全程時間有界。

    重試的範圍與理由見 `_JE_RETRY_STAGES`；重試之後才過會明講，不會安靜地變綠。
    """
    deadline = time.monotonic() + JE_TOTAL_DEADLINE_SEC
    last_stage, last_reason = "child", "一趟都沒有跑"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if time.monotonic() > deadline:
            _progress(f"已逾整體上限 {JE_TOTAL_DEADLINE_SEC:.0f}s，不再重試。")
            break
        ok, stage, reason = _je_attempt(url, headless, attempt)
        if ok:
            if attempt > 1:
                _progress(
                    f"※ je 變體第 {attempt} 趟才過：前一趟失敗在「"
                    f"{_JE_STAGE_LABELS.get(last_stage, last_stage)}」"
                    f"（{_short_reason(last_reason)}）。")
            return _emit_ok()
        last_stage, last_reason = stage, reason
        _progress(f"[je 嘗試 {attempt}] 失敗（{_JE_STAGE_LABELS.get(stage, stage)}）："
                  f"{_short_reason(reason)}")
        if stage not in _JE_RETRY_STAGES:
            _progress("這一類失敗重跑也不會變，不重試。")
            break
        if attempt < MAX_ATTEMPTS:
            _progress("可能是暫時性的（外部 sweep、網路抖動）；重試"
                      "（全新的丟棄式工作目錄）…")
            time.sleep(2.0)
    return _emit_fail(
        f"je 變體：{_JE_STAGE_LABELS.get(last_stage, last_stage)}：{last_reason}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_browser.py",
        description=(
            "獨立瀏覽器 / driver 啟動驗證腳本：用暫時 profile 與 Selenium "
            "Manager 解析路徑驗證 Chrome 能否起得來，完全不碰正式流程、"
            "不需憑證。"
        ),
    )
    parser.add_argument(
        "--mode", choices=["smoke", "full"], default="smoke",
        help="驗證模式：smoke（預設，輕量煙霧驗證）或 full（隔離端到端可達性）。",
    )
    parser.add_argument(
        "--full", action="store_true",
        help=(
            "full 模式：持有跨行程 Chrome 槽、以隔離環境（快照登入 profile，不碰"
            "正式 profile/pid/todo/輸出）跑一次「登入＋導航＋確認產圖介面/關鍵 DOM "
            "可達」的端到端驗證。以子行程呼叫背景程式的 opt-in 驗證模式，需要已登入"
            "的 profile（無有效登入態會在登入步驟 FAIL，並如實回報停在哪一步）。"
        ),
    )
    parser.add_argument(
        "--generate", action="store_true",
        help=(
            "（僅 full）加分項：在隔離環境裡用臨時 prompt 真的產 1 張圖到隔離目錄"
            "再清掉（會實際送出一次生成）。預設關閉，只做不送生成的可達性驗證。"
        ),
    )
    parser.add_argument(
        "--headed", action="store_true",
        help="（僅 smoke）顯示瀏覽器視窗（預設為 headless=new）。full 一律比照"
             "正式啟動為有頭模式。",
    )
    parser.add_argument(
        "--url", default="about:blank",
        help="（僅 smoke）要載入的頁面網址（預設 about:blank，刻意不內建任何"
             "站台網址）。",
    )
    parser.add_argument(
        "--variant", choices=["selenium", "je"], default="selenium",
        help="（僅 smoke）要驗哪一個變體的啟動路徑：selenium（預設）直接用 "
             "Selenium Manager 起 Chrome；je 走 je_web_runner 的 set_driver"
             "（含 driver 管理器的 install()），在丟棄式工作目錄的子行程裡跑。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # `argv=None` 一律當成「沒有旗標」，**不是**「去讀 `sys.argv`」。argparse 的
    # 預設行為是後者，而這支在 pytest 底下被無參數呼叫時，那會去解析**pytest 自己
    # 的命令列**（`-q`、`--timeout=300`…）→ `error: unrecognized arguments` →
    # `SystemExit(2)`。2026-09-09 在 `audit_dependencies` 上真的踩過一次（六支既有
    # 測試同時轉紅），所以本專案的約定是：定義端寫 `argv or []`，`__main__` 那邊
    # 明確傳 `sys.argv[1:]`。守門在 `test_suite_safety.py`。
    # 內部入口：`--variant je` 的父行程叫子行程時帶的旗標，在 argparse 之前分流（見
    # `JE_CHILD_FLAG`）。子行程印的是 `VERIFY-JE-CHILD:`，`VERIFY-BROWSER:` 那一行只由
    # 父行程印，所以這條路刻意不走下面的兜底。
    if argv and argv[0] == JE_CHILD_FLAG:
        return _je_child_main(list(argv[1:]))
    parser = _build_parser()
    args = parser.parse_args(argv or [])

    mode = "full" if (args.full or args.mode == "full") else "smoke"
    if mode == "full" and args.variant == "je":
        # 參數錯誤，不是驗證結論：走 argparse 自己的 exit 2，**不**印
        # `VERIFY-BROWSER:`。印成 FAIL 會被讀成「瀏覽器壞了」，印成 SKIP 會讓自走迴圈
        # 晚點再重試一個永遠不可能成功的指令（見 `_emit_skip`）。
        parser.error("--full 目前只接 selenium 變體；je 變體只有 smoke。"
                     "請拿掉 --full，或拿掉 --variant je。")
    if args.generate and mode != "full":
        # 同上是參數錯誤。原本會被**安靜忽略**：跑一次 smoke、印 `OK`，而下指令的人以為
        # 「真的產一張圖」那一步也驗過了——這支工具最不該給的就是看起來成功、其實沒驗
        # 到的結論（2026-09-20 補）。
        parser.error("--generate 只在 --full 有作用（smoke 不會送出生成）。"
                     "請加上 --full，或拿掉 --generate。")
    if mode == "full" and args.url != "about:blank":
        # 反方向的同一件事：full 走的是正式站台的固定流程，不載入 `--url`。忽略的話
        # `OK` 會被讀成「我指定的那個網址驗過了」。`--headed` 刻意不擋——full 本來就
        # 一律有頭，加了它結論的意思也不變。
        parser.error("--url 只在 smoke 有作用（full 走固定的端到端流程）。"
                     "請拿掉 --full，或拿掉 --url。")
    # 硬性契約：這支**一定**要印一行機器可讀結果。`_smoke_attempt` /
    # `run_smoke` 內部已自行收斂例外，但仍有路徑會逃出去（`_chrome_slot.acquire`
    # 的 OSError、`run_full` 裡 `subprocess.Popen` 起不來、psutil 的非預期例外
    # …）。那些一旦逃出 main 就只會噴 traceback、沒有 `VERIFY-BROWSER:` 那行，
    # 呼叫端 grep 不到結論會誤判成「腳本靜默」。這裡兜底轉成 FAIL 結果行。
    # 只接 Exception，不接 BaseException — Ctrl+C 仍照常往上傳。
    try:
        if mode == "full":
            _progress("瀏覽器驗證開始（full 模式：隔離端到端可達性）")
            _progress(f"參數：generate={args.generate}")
            return _run_with_slot("full", lambda: run_full(args.generate))

        if args.variant == "je":
            # 槽與讓位留在本行程：子行程只在槽握在祖先手上時才開瀏覽器。
            _progress("瀏覽器驗證開始（smoke 模式，je 變體）")
            _progress(f"參數：headless={not args.headed}　url={args.url!r}")
            return _run_with_slot(
                "smoke-je",
                lambda: run_smoke_je(args.url, headless=not args.headed))

        _progress("瀏覽器驗證開始（smoke 模式）")
        _progress(f"參數：headless={not args.headed}　url={args.url!r}")
        return _run_with_slot(
            "smoke", lambda: run_smoke(args.url, headless=not args.headed))
    except Exception as err:  # pylint: disable=broad-except
        return _emit_fail(f"未預期的例外：{err!r}")


if __name__ == "__main__":
    # 先硬化主控台再做任何事：這支的契約是一定要印出結論那一行，而一個編不出來
    # 的字元會讓行程死在半路、看起來像瀏覽器壞了（見 `_harden_console`）。
    _harden_console()
    sys.exit(main(sys.argv[1:]))
