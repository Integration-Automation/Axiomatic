#!/usr/bin/env python3
"""從斜線指令樹產生 `commands/*.md`（一個指令群一檔 ＋ 直接指令 ＋ 索引）。

`CLAUDE.md` DoD #2 寫「`commands/*.md` 是**生成**的，要重新產生而非手改」，但
repo 裡一直沒有產生器，所以「生成」實際上是手改。`test_docs_sync.py` 只比對指令
**名稱**、不驗參數表，於是參數表怎麼跟程式碼漂移都不會被抓到。這支就是那個一直
缺著的產生器。

用法（repo root）::

    py -3 axiomatic/gen_command_docs.py           # 重新產生
    py -3 axiomatic/gen_command_docs.py --check   # 只比對，不一致就 exit 1

唯一真實來源是 `discord_bot.py` 的宣告，全部用 **AST** 抽——不 import，因為
import 會讀設定檔、建 client、連帶要有憑證。

**手改 `commands/*.md` 沒有意義**：下次產生就會被蓋掉，而且
`test_docs_sync.test_commands_docs_are_generated` 會先擋下來。

抽取器刻意與 `test_docs_sync.py` 各留一份。那邊是守門，必須能在本檔壞掉時仍抓得
到漏文件的指令；這邊要的資訊多得多（description／參數型別／choices／兩種擁有者
閘）。兩份都從同一批宣告抽，所以不會出現「誰才是真相」的問題。
"""
import argparse
import ast
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PKG_ROOT.parent
BOT_SOURCE = PKG_ROOT / "discord_bot.py"
OUT_DIR = REPO_ROOT / "commands"

SLASH_TOP_LEVEL_LIMIT = 100

PREAMBLE = """> 本目錄由指令樹產生，**唯一真實來源是程式碼**；bot 內建的 `/help` 才是
> 即時的那一份。指令一律是原生斜線指令——打 `/` 就會自動補全，參數在送出
> 前就有型別與值域檢查。
>
> **回覆一律泛用**：不提外部服務名稱、不露出主機路徑或檔名、不回傳原始
> 錯誤字串，完整細節只進 log。寫新指令時要沿用。"""

SCOPE_CHANNEL = ("🔒 **限頻道**：只在 `bot_config.json` 的 `channel_id` "
                 "所指頻道回應（**擁有者可跨頻道**）。")
SCOPE_GLOBAL = "🌐 **跨頻道**：bot 看得到的任何頻道都能用。"

OWNER_PRE = "**限擁有者**（閘門在派發前，**不受 `user_roles` 設定影響**）"
OWNER_INNER = "**限擁有者**（閘門在指令內部）"
OWNER_SUFFIX = "（限擁有者）"

# 編輯性質的補充說明，指令樹裡沒有這種欄位。key 是群名（`_direct` 代表直接指令
# 那一檔）。要加新的就寫在這裡，**不要**寫進產生出來的 .md。
NOTES = {
    "config": ("`/config set` 只更新指定的那一個鍵、保留其他既有覆寫，值在寫入前"
               "驗證型別與範圍，不合法直接拒絕。多數鍵每個角色迴圈開頭熱重載，"
               "`debug_screenshots` 例外——它在啟動時讀一次，改了要 `/stop` ＋"
               "`/run`。"),
    "gen": ("`/gen image` 與 `/gen image_queue` 走的是單張產圖路徑（與批次共用同一"
            "個瀏覽器槽）；批次正在跑時會排在兩張批次圖之間服務，不會另外開一套。"),
    "macro": ("⚠️ 錄製會錄到期間打的**每一個字，包含密碼**。所以 `/macro record`"
              "限擁有者、有 300 秒硬性上限（到點自動存檔，並在開始錄製的頻道說一聲），"
              "而 `/macro show` 送出前會先去識別化。"
              "巨集**刻意不支援 `sh` 步驟**：巨集存在磁碟上、任何下得了指令的人都能"
              " `/macro run`，支援了就等於做出一個繞過擁有者閘門的後門。"),
    "watch": ("⚠️ `--then` 帶主機指令的那條路徑**另外硬綁擁有者**——它等於延後執行的"
              " `/host sh run`，閘門必須一樣硬。其餘 `/watch` 子指令走一般角色閘。"),
}

# 單一指令的補充段落，接在該指令的參數表後面。key 是完整名（不含前導斜線）。
# 一樣是編輯內容，指令樹裡沒有；換行照著寫死，產生器不重排。
COMMAND_NOTES = {
    "fun timer": (
        "時長 1 秒到 24 小時。計時器只在 bot 這次執行期間有效，重新啟動就不會提醒；",
        "每個人同時最多 10 個，到點之後名額就空出來。",
    ),
    "sys git_pull": (
        "拉成功之後會自動重啟（帶 `noreboot` 就不會）。bot 沒有監督者、或判斷不出來時，pull 照樣算數，",
        "只是不自動重啟，新程式要等 bot 下次啟動才載入。工作樹有未 commit 的改動時會拒絕，",
        "但四份佇列檔不算——批次一直在改寫它們；拉下來的更新若真的碰到佇列檔，git 自己會拒絕。",
    ),
    "sys restart": (
        "只有在 bot 是由主機上的啟動器帶起來的時候才會重啟。直接從編輯器執行的 bot 沒有人會把它",
        "拉回來，這時重啟只會把 bot 關掉，所以會拒絕並說明；判斷不出來時也不重啟。",
    ),
    "run": (
        "`in <N>` 以分鐘計（也可以寫 `h`、`s`），最長 7 天；`at HH:MM` 已經過了就排到明天。",
        "排好的延後啟動會存下來，bot 中途重新啟動也不會消失：重啟後還沒到點的照原時間",
        "繼續等；在重啟期間到點、晚了 10 分鐘以內的會立刻補啟動並說明晚了多久；晚更久的",
        "只在原頻道說一聲錯過了，不會突然動起來。`when` 填 `cancel`、或直接 `/run`，都會取消它。",
    ),
    "todo dedupe": (
        "**角色2 佇列有空白列時會拒做。** 該佇列是按位置配對的：第 N 列配第 N 對，",
        "空白列代表「這一對不要角色2」。去重是整批刪列，刪掉任何一列都會讓後面每一",
        "列往前位移；而空白列彼此看起來完全一樣，會被當成重複項清掉。要刪特定列請用",
        "`/todo char2 remove`。",
    ),
    "todo shuffle": (
        "洗牌只重排目前這一個佇列，不會連動其他佇列——原本第 N 對的組合會就此拆開。",
        "角色2 佇列的空白列（「這一對不要角色2」）數量不變，但會落到別的位置。",
    ),
    "macro run": (
        "**參數語法。** 步驟裡的 `$1`..`$9` 換成 `args` 的第 N 個值（以空白分隔；沒給的",
        "換成空字串；只看一位數，`$10` 是第 1 個值後面接 `0`）；`$$` 是一個字面的 `$`；",
        "其餘的 `$`（在結尾、或後面接的不是 1–9 也不是 `$`）照原樣留著。代入只做一遍，",
        "所以參數值裡的 `$1`、`$$` 會原樣送出。錄製下來的輸入裡的 `$` 會自動寫成 `$$`，",
        "重播時打出來的還是原本那個字。",
        "",
        "**整個程式在第一個動作之前先驗完**：代入參數之後的每一行（不管執行時走不走得",
        "到）、區塊的 `end`、每一個 `call` 的巨集是否存在與呼叫深度。任何一處不合法就",
        "一步都不做，直接回報是哪個巨集的第幾行。條件式的遞迴（`if_…` 裡 `call` 自己）",
        "也會被擋——整個展開一定超過深度上限。`/schedule add` 建立的巨集排程與",
        "`/watch` 的 `--run` 動作，在**建立的時候**就用同一套檢查驗存下來的參數。",
    ),
    "proc usage": (
        "**只講主機這一側。** 回答的是「此刻開了幾個行程、分別是什麼角色、吃掉多少記憶體",
        "與 CPU、最久的那個跑了多久、整台機器還剩多少」。批次做到哪一對、還差幾張要看",
        "`/gen current` 與 `/gen progress`，對話助理有哪幾輪在跑要看 `/dorossi running`——",
        "那兩份讀的是進度與工作佇列，這一支讀的是作業系統的行程表。刻意不重複，否則同一件",
        "事會由三個地方各講一半，然後各自漂移。",
        "",
        "**所有瀏覽器行程都算進「瀏覽器與驅動程式」**，判準與啟動前的清理流程一致；兩邊對",
        "不起來的話，下一次清理會比這裡說的多殺一些。「另有 N 個轉接殼」指的是虛擬環境多出",
        "來的那一層行程——它不是多一個實例，但它真的佔記憶體，所以兩個數字都列。",
        "",
        "CPU 百分比需要一段真實的取樣時間（約 0.4 秒），所以這支比其他狀態指令慢一點；",
        "本機實測整趟約 1 秒。讀不到的欄位一律顯示 `?`，並在最後一行說明少算了什麼——",
        "不會拿 0 充數，因為 0 會被加進總和然後被當成事實。",
    ),
    "proc kill": (
        "名稱沒帶 `.exe` 會自動補上，所以**這個指令只碰得到有可執行檔名的程式**。",
        "`/proc list` 也會列出幾個系統層的行程（名稱天生沒有副檔名），那些是刻意",
        "關不掉的——系統本身也不允許。從清單抄一個那樣的名字下來時，回覆會直接說",
        "它是系統層的行程，而不是含糊地說「沒在跑」。",
    ),
    "dorossi ask": (
        "多個工作階段可以同時進行：指定 `session` 送出後就在背景跑，接著換一個代號再送",
        "一次即可，不必來回切換。同一個工作階段仍然一次只跑一輪。`session` 欄位會列出你的",
        "工作階段（代號與標籤，已封存的不列）供挑選，也可以直接打代號；值必須就是一個代號，",
        "打錯或不存在時這一輪不送出，不會退回目前的工作階段。它和提問開頭指定工作階段的",
        "寫法是同一件事，兩者都給了卻指到不同的工作階段時，這一輪同樣不送出——猜錯的代價",
        "是把問題送進另一個專案。正在跑什麼、每一輪在等空位還是在跑，用 `/dorossi running` 看。",
        "",
        "斷線不會讓提問消失：後端連不上時這一題會停在原地等網路回來，再用同一段對話接著",
        "做；答案出來時對話平台連不上，就先存起來，連線恢復後補送到原本的頻道。排隊中的提問",
        "要跑完才會從佇列拿掉，斷線時會放回佇列，連線回來再跑。主機睡著又醒來也一樣：睡著",
        "的時間不算進這一題的時間上限。",
        "",
        "`prompt` 的**最前面**還收兩個微調 token。它們不是斜線指令，是打在提問文字開頭的",
        "字，送出前會被剝掉，剩下的才是真正送出去的問題：",
        "",
        "- `@bot /model <opus|sonnet|haiku|fable|default> <提問>` — 指定這個工作階段用哪個模型",
        "- `@bot /effort <low|medium|high|xhigh|max> <提問>` — 指定思考力度",
        "",
        "同一串寫法貼進這裡的 `prompt` 欄位開頭也一樣有效，所以「改設定」與「提問」可以在",
        "同一次送出裡完成——那是 token 形式唯一做得到、而斜線指令做不到的事。兩個都是",
        "**工作階段層級**：設過之後同一個工作階段每一輪都沿用，選 `default` 清除覆寫；要",
        "釘住版本就照同樣寫法指定，例如 `<opus-5|sonnet-4.6>`。順序不拘、大小寫不拘，兩個可以同時",
        "給，但必須連續放在最前面——句中出現的同名字樣會原樣保留，不會被當成設定。只想改",
        "設定不提問的話，用 `/dorossi model`、`/dorossi effort` 這兩個斜線指令。",
    ),
    "dorossi running": (
        "每一件進行中的工作一行：哪個工作階段（代號與標籤）、哪一種（單輪提問、自走任務",
        "第幾輪、回覆之後的背景壓縮、手動壓縮）、哪個後端、何時開始與經過多久、**在等空位",
        "還是真的在跑**，以及後面還排了幾筆。最上面是兩個上限：單輪回合（含背景壓縮）共用",
        "一組後端空位，全部被佔用時新的回合會顯示「等候空位」並附上上限，直到有空位釋出",
        "才換成「處理中」；自走任務不佔那些空位，另有自己的同時進行上限（可設成不設限）。",
        "斷線時停下來等網路回來的工作會標成「等網路恢復」並附上等了多久（連線回來會自動",
        "接著做）。只讀、不改任何東西。",
    ),
    "dorossi abort": (
        "中止的是自走任務，以及正在等網路回來的那一題；一般在跑的單輪提問有自己的時間上限，",
        "不在這裡。斷線時停下來、等著連線恢復就會被自動接回去的任務，也可以用它指名（或",
        "`all`）取消自動接續——任務停在原地，之後要接回去用 `/dorossi session continue <id>`。",
        "中止過的任務不會被 bot 重連或重啟時的**自動**接續碰到；但你自己下",
        "`/dorossi session continue all` 會接它（見該指令）。要永久停掉某個工作階段，",
        "改用 `/dorossi session delete`。",
    ),
    "dorossi yield": (
        "另一位編輯者要接手改**同一批檔案**時用它：讓正在跑的自走任務先把手上的改動",
        "提交掉、然後暫停，把那個工作階段讓出來。跟 `/dorossi abort` 不一樣——abort 會",
        "當場砍掉後端、可能把做到一半還沒提交的改動弄丟；yield 不砍，而是叫它把改動依",
        "這個 repo 的規則提交（逐檔提交、不 push、不加署名），提交完才停。所以停下來會",
        "比 abort 慢一輪（要等提交那一輪跑完）。",
        "",
        "`target` 留空 = 讓出目前這個工作階段的任務（沒有就找唯一在跑的那個），填代號",
        "只讓出那一個，填 `all` 全部讓出。停住的任務會留著，用 `/dorossi session continue`",
        "（或 `continue all`）接回來繼續。**注意**：讓出後 bot 就算重連或重啟也**不會**",
        "自動把它叫回來——讓出的用意就是別再去動那批檔案，要接手完再自己 `continue`。",
    ),
    "dorossi session continue": (
        "`id` 填 `all` 會一次接回所有中斷的自走任務——**包含你自己 `/dorossi abort` 過的**",
        "——並逐一回報：已接續、已在進行中、略過（已封存的、超過同時進行",
        "上限的、那個工作階段正在處理別的提問）或失敗（目前的後端或工具模式不能自走）。要讓",
        "某個工作階段連 `all` 都不接，用 `/dorossi session delete` 把它封存。",
        "",
        "斷線（網路、對話平台）時任務不會停：它會停在原地等連線回來，再從同一輪接著做，",
        "沒有次數上限，只有 `/dorossi abort` 能讓它停。bot 重新連上或重新啟動時，因斷線或",
        "重啟而中斷的任務會**自動**接回去；你中止過的不會自動接，但 `all` 會。",
    ),
    "dorossi model": (
        "這是**工作階段層級**的設定：設過之後，這個工作階段往後每一輪都沿用，直到再改",
        "一次；選「預設」就清除覆寫。合法值直接列在指令選單裡，不必背。各個工作階段互",
        "不影響，`/dorossi session new` 開的新脈絡一律從預設開始。任務進行中不能改——",
        "半途換模型會讓同一個任務前後不一致，請先 `/dorossi abort`。",
        "",
        "選單分兩種：**不帶版本**的（`opus`、`sonnet`、`haiku`、`fable`）意思是「這一族",
        "當下最新的那個」，新版本上線會自動跟上；**帶版本**的（例如 `opus-4.8`）則是釘死",
        "在那一版，不會被升級動到。要長期可重現就選帶版本的。",
        "",
        "**合法值是按後端算的。** 每個後端有自己的一組模型，所以選單只會列出這個工作階段",
        "目前的後端吃得下的那些（`/dorossi ai` 換了後端，選單就跟著換）。你設過的值不會被",
        "清掉：換到吃不下它的後端時，指令會直接告訴你「這個後端不吃這項設定」，`/dorossi",
        "session list` 也會標出來，換回來就自動生效——不會安靜失效。",
        "",
        "**清單會自己長。** bot 每天檢查一次後端有沒有新模型，有的話自動加進選單並在頻道",
        "說一聲，不必等人改程式。要關掉或改間隔看 `bot_config.json` 的 `dorossi_model_check`。",
    ),
    "dorossi effort": (
        "與 `/dorossi model` 同一組設定：工作階段層級、選「預設」清除覆寫、任務進行中",
        "不能改。目前生效的值會一併顯示在 `/dorossi session list`。",
    ),
    "dorossi workspace_clean": (
        "正在處理、排隊中（包括失敗後等著 `/dorossi queue retry_failed` 重跑的），或留有",
        "未完成任務（可用 `/dorossi session continue` 接續）的工作階段，它的工作目錄一律",
        "跳過，回覆會附上跳過了幾個。判斷多久沒用時同時看目錄的修改時間與那個工作階段",
        "最後一次使用的時間，只要其中一個落在 `days` 天內就保留。",
    ),
}

TYPE_WORDS = {
    "str": "文字",
    "int": "整數",
    "float": "數字",
    "bool": "是／否",
    "Attachment": "附件",
    "User": "使用者",
    "Member": "使用者",
}


# ---------------------------------------------------------------------------
# AST 抽取
# ---------------------------------------------------------------------------
def _tree() -> ast.Module:
    return ast.parse(BOT_SOURCE.read_text(encoding="utf-8"), str(BOT_SOURCE))


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _const(node):
    """常數字面值；`-65535` 是 `UnaryOp(USub, Constant)`，不是 `Constant`。

    漏掉負號那一支，`Range[int, -65535, 65535]` 會渲染成「整數（None–65535）」。
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        if isinstance(node.op, ast.USub):
            return -node.operand.value
        if isinstance(node.op, ast.UAdd):
            return node.operand.value
    return None


def _str_set(tree: ast.Module, name: str) -> list:
    """模組層 `NAME = frozenset({...})` / `{...}` 的字串元素，**保留宣告順序**。

    順序有用：README 的擁有者清單照宣告順序寫，排序過反而看不出分組意圖。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Call):          # frozenset({...})
            value = value.args[0] if value.args else None
        if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            return [e.value for e in value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    raise AssertionError("discord_bot.py 裡找不到 `%s`，抽取邏輯要跟著改" % name)


def _groups(tree: ast.Module) -> dict:
    """`變數名 -> {name, description, parent}`。只認 `Group(...)` 建構寫法。"""
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not _dotted(node.value.func).endswith("Group"):
            continue
        info = {"name": None, "description": "", "parent": None}
        for keyword in node.value.keywords:
            if keyword.arg == "name":
                info["name"] = _const(keyword.value)
            elif keyword.arg == "description":
                info["description"] = _const(keyword.value) or ""
            elif keyword.arg == "parent" and isinstance(keyword.value, ast.Name):
                info["parent"] = keyword.value.id
        if info["name"] and isinstance(node.targets[0], ast.Name):
            found[node.targets[0].id] = info
    return found


def _owner_aliases(tree: ast.Module) -> set:
    """擁有者 UID 常數的所有名字（`OWNER_USER_ID = DOROSSI_USER_ID` 這種別名）。"""
    aliases = {"OWNER_USER_ID"}
    for _ in range(4):          # 別名鏈很短，跑到不動為止即可
        grown = set(aliases)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target, value = node.targets[0], node.value
            if not isinstance(target, ast.Name) or not isinstance(value, ast.Name):
                continue
            if target.id in aliases:
                grown.add(value.id)
            if value.id in aliases:
                grown.add(target.id)
        if grown == aliases:
            break
        aliases = grown
    return aliases


def _owner_predicates(tree: ast.Module, aliases: set) -> set:
    """回傳「是不是擁有者」的述詞函式名（`_dorossi_owner_only` 之類）。

    判準是 `return <某處出現擁有者 UID 的運算式>`——這種函式一定是拿來當閘用的。
    """
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Return) or statement.value is None:
                continue
            names = {n.id for n in ast.walk(statement.value)
                     if isinstance(n, ast.Name)}
            if names & aliases:
                found.add(node.name)
    return found


def _owner_gated_handlers(tree: ast.Module) -> set:
    """擁有者閘寫在 handler 內部的那些函式名。

    有一批指令的閘不在派發前的 `_OWNER_ONLY_*`，而是 handler 自己第一件事就擋
    （整個 `/dorossi` 族都是這樣，見 `discord_bot.py` 該段註解）。文件要標得
    出來就得看得到這一種。

    形狀認得很緊：**`if <牽涉擁有者 UID 或擁有者述詞> : … return`**，也就是
    「不是擁有者就掉頭」。放寬成「內文出現 `OWNER_USER_ID`」會誤判——`slash_help`
    拿它決定要不要附上限頻道段落，那不是閘。
    """
    aliases = _owner_aliases(tree)
    predicates = _owner_predicates(tree, aliases)
    gated = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            names = {n.id for n in ast.walk(inner.test) if isinstance(n, ast.Name)}
            if not (names & aliases) and not (names & predicates):
                continue
            if any(isinstance(s, ast.Return)
                   for statement in inner.body for s in ast.walk(statement)):
                gated.add(node.name)
                break
    return gated


def _number(value) -> str:
    return repr(value) if isinstance(value, float) else str(value)


def _type_label(annotation, choice_names) -> str:
    if choice_names:
        return "選項：" + " / ".join("`%s`" % c for c in choice_names)
    if annotation is None:
        return "文字"
    if isinstance(annotation, ast.Subscript):
        base = _dotted(annotation.value).rsplit(".", 1)[-1]
        if base == "Range":
            elts = (annotation.slice.elts
                    if isinstance(annotation.slice, ast.Tuple) else [])
            kind = _dotted(elts[0]).rsplit(".", 1)[-1] if elts else "int"
            word = TYPE_WORDS.get(kind, "整數")
            low = _const(elts[1]) if len(elts) > 1 else None
            high = _const(elts[2]) if len(elts) > 2 else None
            if low is None and high is None:
                return word
            return "%s（%s–%s）" % (word, _number(low), _number(high))
        # Optional[X] / Union[X, None]：拿第一個具體型別。
        inner = annotation.slice
        if isinstance(inner, ast.Tuple) and inner.elts:
            inner = inner.elts[0]
        return _type_label(inner, None)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        # `discord.User | None` —— 新式 Optional，取左邊那個具體型別。
        return _type_label(annotation.left, None)
    return TYPE_WORDS.get(_dotted(annotation).rsplit(".", 1)[-1], "文字")


def _default_note(value, describe: str) -> str:
    """`（預設 \\`left\\`）`——describe 自己已經講過預設值時就不要再補一次。

    `是／否` 一律不補：那種參數讀起來本來就自明，補了只是雜訊。
    """
    # `0` 與 `""` 在這個 codebase 裡一律是「沒給」的哨兵值（`/win size` 的
    # `width=0`、`/web doujin` 的 `n=0`），寫成「預設 0」只會誤導。
    if not value or isinstance(value, bool):
        return ""
    if "預設" in describe:
        return ""
    return "（預設 `%s`）" % value


def _params(func, describes: dict, choices: dict) -> list:
    """簽章 → `[{name, type, required, desc}]`（跳過 `interaction`）。"""
    args = func.args.args
    defaults = func.args.defaults
    first_default = len(args) - len(defaults)
    out = []
    for index, arg in enumerate(args):
        if arg.arg in ("self", "interaction"):
            continue
        describe = describes.get(arg.arg, "—")
        required = index < first_default
        if not required:
            describe += _default_note(_const(defaults[index - first_default]),
                                      describe)
        out.append({
            "name": arg.arg,
            "type": _type_label(arg.annotation, choices.get(arg.arg)),
            "required": required,
            "desc": describe,
        })
    return out


def _choice_names(node, choice_vars: dict):
    """`choices=` 的值 → 選項顯示名清單；解不出來就回 None（退回型別字）。

    解不出來是正常的：`_CONFIG_KEY_CHOICES` 是 list comprehension、`/config reset`
    還在後面接了一個 `+ [...]`。那種情況本來就沒有靜態可讀的選項表，文件寫「文字」
    才誠實。
    """
    if isinstance(node, ast.Name):
        return choice_vars.get(node.id)
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    names = []
    for element in node.elts:
        if not isinstance(element, ast.Call):
            return None
        for keyword in element.keywords:
            if keyword.arg == "name":
                label = _const(keyword.value)
                if label is not None:
                    names.append(label)
    return names or None


def _choice_vars(tree: ast.Module) -> dict:
    """模組層 `_X_CHOICES = [Choice(name=…), …]` → 顯示名清單。"""
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        names = _choice_names(node.value, {})
        if names:
            found[target.id] = names
    return found


def _commands(tree: ast.Module, groups: dict, choice_vars: dict) -> list:
    """走訪所有 `@x.command(...)`，回一批 dict。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        owner = name = None
        description = ""
        extras = {}
        describes = {}
        choices = {}
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            dotted = _dotted(decorator.func)
            if dotted.endswith(".command"):
                owner = dotted[: -len(".command")]
                name = node.name
                for keyword in decorator.keywords:
                    if keyword.arg == "name":
                        name = _const(keyword.value)
                    elif keyword.arg == "description":
                        description = _const(keyword.value) or ""
                    elif keyword.arg == "extras":
                        try:
                            extras = ast.literal_eval(keyword.value)
                        except ValueError:
                            extras = {}
            elif dotted.endswith(".describe"):
                for keyword in decorator.keywords:
                    describes[keyword.arg] = _const(keyword.value) or "—"
            elif dotted.endswith(".choices"):
                for keyword in decorator.keywords:
                    names = _choice_names(keyword.value, choice_vars)
                    if names:
                        choices[keyword.arg] = names
        if owner is None or name is None:
            continue
        if owner != "tree" and owner not in groups:
            continue
        # `_slash_run(interaction, <handler>, …)` 的第二個引數＝真正做事的函式。
        handler = None
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) \
                    and _dotted(inner.func) == "_slash_run" \
                    and len(inner.args) >= 2 \
                    and isinstance(inner.args[1], ast.Name):
                handler = inner.args[1].id
                break
        out.append({
            "owner": owner,
            "name": name,
            "description": description,
            "extras": extras if isinstance(extras, dict) else {},
            "params": _params(node, describes, choices),
            "func": node.name,
            "handler": handler,
        })
    return out


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
class Surface:
    """指令樹的唯讀快照，外加文件要用的分類（範圍／閘門／角色）。"""

    def __init__(self):
        tree = _tree()
        self.groups = _groups(tree)
        self.commands = _commands(tree, self.groups, _choice_vars(tree))
        self.owner_groups_order = _str_set(tree, "_OWNER_ONLY_GROUPS")
        self.owner_slash_order = _str_set(tree, "_OWNER_ONLY_SLASH")
        self.owner_groups = set(self.owner_groups_order)
        self.owner_slash = set(self.owner_slash_order)
        self.viewer_bangs = set(_str_set(tree, "_VIEWER_COMMANDS"))
        self.admin_bangs = set(_str_set(tree, "_ADMIN_COMMANDS"))
        self.owner_inner_funcs = _owner_gated_handlers(tree)
        self.registered = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) \
                    and _dotted(node.func) == "tree.add_command" \
                    and node.args and isinstance(node.args[0], ast.Name):
                self.registered.add(node.args[0].id)
        for command in self.commands:
            command["path"] = self.path_of(command)
            command["qualified"] = " ".join(command["path"])

    def path_of(self, command) -> list:
        if command["owner"] == "tree":
            return [command["name"]]
        chain = []
        var = command["owner"]
        while var:
            info = self.groups[var]
            chain.append(info["name"])
            var = info["parent"]
        return list(reversed(chain)) + [command["name"]]

    def is_public(self, command) -> bool:
        return bool(command["extras"].get("public"))

    def owner_line(self, command):
        if (command["path"][0] in self.owner_groups
                or command["qualified"] in self.owner_slash):
            return OWNER_PRE
        if command["handler"] and command["handler"] in self.owner_inner_funcs:
            return OWNER_INNER
        if command["func"] in self.owner_inner_funcs:
            return OWNER_INNER
        return None

    def role_line(self, command):
        if self.is_public(command):
            return None
        bang = command["extras"].get("bang")
        if bang and bang in self.viewer_bangs:
            return "權限：viewer 以上"
        if bang and bang in self.admin_bangs:
            return "權限：admin 以上"
        return "權限：operator 以上"


# ---------------------------------------------------------------------------
# 產生
# ---------------------------------------------------------------------------
def _signature(command) -> str:
    parts = ["/" + command["qualified"]]
    for param in command["params"]:
        parts.append(("<%s>" if param["required"] else "[%s]") % param["name"])
    return " ".join(parts)


def _display_width(text: str) -> int:
    """全形字算兩欄——不這樣算，中文段落會被折在很奇怪的地方。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _wrap(text: str, width: int = 76) -> list:
    """在「、」與「，」之後斷行，把一段中文折成接近 `width` 欄寬的數行。

    只在標點**後面**斷，所以不會把 `` `/sys a|b` `` 這種反引號片段折成兩半。
    """
    chunks, chunk, depth = [], "", 0
    for char in text:
        chunk += char
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth = max(0, depth - 1)
        elif char in "、，" and depth == 0:
            # 括號內不斷行——折在「（群組制，／所以…）」中間很難讀。
            chunks.append(chunk)
            chunk = ""
    if chunk:
        chunks.append(chunk)

    lines, current = [], ""
    for piece in chunks:
        if current and _display_width(current + piece) > width:
            lines.append(current)
            current = piece
        else:
            current += piece
    if current:
        lines.append(current)
    return lines


def _cell(text: str) -> str:
    """表格欄位：`|` 會把欄位切斷，一律跳脫（在反引號裡面也一樣要跳）。"""
    return str(text).replace("|", "\\|")


def _render_command(surface: Surface, command) -> list:
    owner = surface.owner_line(command)
    description = command["description"]
    if owner and description.endswith(OWNER_SUFFIX):
        # 底下就有一行「限擁有者」，標題再帶一次是重複的。
        description = description[: -len(OWNER_SUFFIX)]
    lines = ["### `%s`" % _signature(command), "", description, ""]
    if owner:
        lines += [owner, ""]
    else:
        role = surface.role_line(command)
        if role:
            lines += [role, ""]
    if command["params"]:
        lines += ["| 參數 | 型別 | 必填 | 說明 |", "|---|---|:--:|---|"]
        for param in command["params"]:
            lines.append("| `%s` | %s | %s | %s |" % (
                _cell(param["name"]), _cell(param["type"]),
                "✅" if param["required"] else "—", _cell(param["desc"])))
        lines.append("")
    note = COMMAND_NOTES.get(command["qualified"])
    if note:
        lines += list(note) + [""]
    return lines


def _sorted_commands(commands) -> list:
    return sorted(commands, key=lambda c: c["name"])


def render_group(surface: Surface, var: str) -> str:
    info = surface.groups[var]
    name = info["name"]
    subgroup_vars = sorted(
        (v for v, g in surface.groups.items() if g["parent"] == var),
        key=lambda v: surface.groups[v]["name"])
    direct = _sorted_commands([c for c in surface.commands if c["owner"] == var])
    nested = {v: _sorted_commands([c for c in surface.commands if c["owner"] == v])
              for v in subgroup_vars}
    every = direct + [c for v in subgroup_vars for c in nested[v]]

    lines = ["# `/%s` — %s" % (name, info["description"]), "", PREAMBLE, ""]
    lines += [SCOPE_GLOBAL if every and all(surface.is_public(c) for c in every)
              else SCOPE_CHANNEL, ""]
    lines += ["共 **%d** 個子指令。" % len(every), ""]
    if name in NOTES:
        lines += ["> " + NOTES[name], ""]
    lines += ["---", ""]
    if direct:
        lines += ["## 直接子指令", ""]
        for command in direct:
            lines += _render_command(surface, command)
    for subgroup_var in subgroup_vars:
        sub = surface.groups[subgroup_var]
        lines += ["## `/%s %s` — %s" % (name, sub["name"], sub["description"]), ""]
        for command in nested[subgroup_var]:
            lines += _render_command(surface, command)
    return "\n".join(lines).rstrip("\n") + "\n"


def render_direct(surface: Surface) -> str:
    flat = _sorted_commands([c for c in surface.commands if c["owner"] == "tree"])
    channel = [c for c in flat if not surface.is_public(c)]
    public = [c for c in flat if surface.is_public(c)]
    lines = ["# 直接指令（不屬於任何指令群）", "", PREAMBLE, ""]
    lines += ["共 **%d** 個。這些各佔一個頂層額度，所以只有最常用的才留在這裡；"
              "其餘都收進指令群。" % len(flat), ""]
    if "_direct" in NOTES:
        lines += ["> " + NOTES["_direct"], ""]
    lines += ["---", ""]
    for header, scope, bucket in (("## 🔒 限頻道", SCOPE_CHANNEL, channel),
                                  ("## 🌐 跨頻道", SCOPE_GLOBAL, public)):
        if not bucket:
            continue
        lines += [header, "", scope, ""]
        for command in bucket:
            lines += _render_command(surface, command)
    return "\n".join(lines).rstrip("\n") + "\n"


def _owner_only_prose(surface: Surface) -> str:
    """README 的「受閘的是…」段落，從兩個常數長出來，不手寫。

    一群的子指令**全部**在名單上時寫成「`/x` 全部」，否則逐一列出——這正是舊版
    手寫時 `/schedule` 與 `/sys` 的差別，只是現在不會有人忘記更新。
    """
    groups = "、".join("`/%s`" % g for g in surface.owner_groups_order)
    buckets = {}
    for qualified in surface.owner_slash_order:
        head, _, leaf = qualified.partition(" ")
        buckets.setdefault(head, []).append(leaf)
    leaves_by_group = {}
    for command in surface.commands:
        if len(command["path"]) >= 2:
            leaves_by_group.setdefault(command["path"][0], set()).add(
                " ".join(command["path"][1:]))
    pieces = []
    for head, leaves in buckets.items():
        if leaves_by_group.get(head, set()) == set(leaves):
            pieces.append("`/%s` 全部" % head)
        else:
            pieces.append("`/%s %s`" % (head, "|".join(leaves)))
    body = ("受閘的是 %s **整群**（群組制，所以新增子指令會自動受閘），加上 %s。"
            "各檔標成「限擁有者」的就是這些。" % (groups, "、".join(pieces)))
    return "\n".join(_wrap(body))


def render_index(surface: Surface) -> str:
    flat = [c for c in surface.commands if c["owner"] == "tree"]
    top_vars = sorted(surface.registered, key=lambda v: surface.groups[v]["name"])
    top_total = len(flat) + len(top_vars)

    rows = []
    if flat:
        rows.append("| [`_direct.md`](_direct.md) | 🔒🌐 | 不屬於任何群的指令 | %d |"
                    % len(flat))
    for var in top_vars:
        info = surface.groups[var]
        members = [c for c in surface.commands if c["path"][0] == info["name"]]
        scope = "🌐" if all(surface.is_public(c) for c in members) else "🔒"
        rows.append("| [`%s.md`](%s.md) | %s | %s | %d |"
                    % (info["name"], info["name"], scope,
                       info["description"], len(members)))

    lines = ["# 指令總表", "", PREAMBLE, ""]
    lines += ["**%d 個直接指令 ＋ %d 個指令群 ＝ %d 個頂層指令**"
              "（平台上限 %d，餘裕 %d），底下合計 **%d 個斜線子指令**。"
              % (len(flat), len(top_vars), top_total, SLASH_TOP_LEVEL_LIMIT,
                 SLASH_TOP_LEVEL_LIMIT - top_total, len(surface.commands)), ""]
    lines += ["指令群只佔一個頂層額度、群內子指令不計——這是唯一能長期擴充的作法。", ""]
    lines += ["| 檔案 | 範圍 | 內容 | 子指令 |", "|---|:--:|---|---:|"] + rows + [""]
    lines += ["🔒 限頻道（擁有者可跨頻道）／🌐 跨頻道。", ""]
    lines += ["## 權限", "",
              "閘門是**單一一道**，斜線與隱藏的文字路徑共用：頻道 → 角色 → 計數 → 稽核。",
              "**預設 fail-closed**——沒有明確標成公開的指令一律受閘。", "",
              "| 角色 | 能用什麼 |", "|---|---|",
              "| `none` | 只有跨頻道指令 |",
              "| `viewer` | 加上唯讀查詢（狀態、佇列、log、輸出） |",
              "| `operator` | 加上會改狀態的指令（佇列編輯、批次控制、桌面操作） |",
              "| `admin` | 加上行程與主機層級的指令 |", "",
              "`bot_config.json` 的 `user_roles` 三份清單**都空時**（預設）角色閘等於停用，",
              "只剩頻道閘。", ""]
    lines += ["## 主機控制：硬綁擁有者", "",
              "**控制 bot 那台電腦的指令一律只有擁有者能用**，而且**不經過角色系統**——",
              "角色系統預設停用，把桌面控制掛在它下面等於沒有保護。閘門在派發之前，斜線、",
              "`!`、mention 三條路徑共用同一組規則。", "",
              _owner_only_prose(surface), "",
              "產圖佇列、批次控制與唯讀診斷（`/todo`、`/preset`、`/run`、`/stop`、",
              "`/sys health|doctor|disk`、`/log tail` …）不在此列，仍走頻道＋角色閘。", ""]
    lines += ["## 相容性", "",
              "舊的文字前綴指令仍然可用，但**不再是對外介面**、也不寫進文件：保留是為了",
              "手機打字、多行貼上與回覆脈絡這些選項輸入框做不到的情境。新功能一律只加",
              "斜線指令。唯一仍對外的 mention 用法是 `@bot <文字>` 自由提問入口。"]
    return "\n".join(lines).rstrip("\n") + "\n"


def build() -> dict:
    """檔名 -> 應有的內容。"""
    surface = Surface()
    out = {"README.md": render_index(surface),
           "_direct.md": render_direct(surface)}
    for var in surface.registered:
        out["%s.md" % surface.groups[var]["name"]] = render_group(surface, var)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="產生 commands/*.md")
    parser.add_argument("--check", action="store_true",
                        help="只比對；有差異就 exit 1，不寫檔")
    # `argv=None` 一律當成「沒有旗標」，**不是**「去讀 `sys.argv`」。argparse 的
    # 預設行為是後者，而這支在 pytest 底下被無參數呼叫時，那會去解析**pytest 自己
    # 的命令列**（`-q`、`--timeout=300`…）→ `error: unrecognized arguments` →
    # `SystemExit(2)`。2026-09-09 在 `audit_dependencies` 上真的踩過一次（六支既有
    # 測試同時轉紅），所以本專案的約定是：定義端寫 `argv or []`，`__main__` 那邊
    # 明確傳 `sys.argv[1:]`。守門在 `test_suite_safety.py`。
    options = parser.parse_args(argv or [])
    files = build()
    stale = []
    for name, text in sorted(files.items()):
        path = OUT_DIR / name
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        stale.append(name)
        if not options.check:
            # 換行刻意用平台預設（Windows 上是 CRLF）：`.gitattributes` 是
            # `* text=auto` ＋ `core.autocrlf=true`，checkout 出來就是 CRLF，
            # 寫死 LF 會讓產生器每次都跟 checkout 打架。比對那一側不受影響——
            # `read_text` 的 universal newlines 已經把 CRLF 正規化成 `\n`。
            path.write_text(text, encoding="utf-8")
    extra = sorted(path.name for path in OUT_DIR.glob("*.md")
                   if path.name not in files)
    if options.check:
        if stale or extra:
            print("commands/ 與指令樹不同步：", file=sys.stderr)
            for name in stale:
                print("  需要重新產生：%s" % name, file=sys.stderr)
            for name in extra:
                print("  指令樹裡沒有對應的檔案：%s" % name, file=sys.stderr)
            print("跑 `py -3 axiomatic/gen_command_docs.py` 重新產生。",
                  file=sys.stderr)
            return 1
        print("commands/ 與指令樹同步（%d 個檔案）。" % len(files))
        return 0
    print("寫入 %d 個檔案，其中 %d 個有變動。" % (len(files), len(stale)))
    for name in stale:
        print("  %s" % name)
    if extra:
        print("指令樹裡沒有對應的檔案（沒有動它們）：%s" % ", ".join(extra))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
