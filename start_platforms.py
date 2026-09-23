"""一次把**每個開著的平台**各起一個受監督的行程。

一個平台一個行程。這支只是方便用的外層：它讀 `bot_config.json`，算出哪些平台
開著而且填了憑證，然後對每一個各起一支
`start_discord_bot.py --platform <名稱>`，自己隨即結束。真正的監督迴圈、退避、
單一實例鎖與記錄檔都在那支裡面，**逐平台各一份**。

## 為什麼不是一個行程跑所有平台

一個行程跑全部的話，一個平台的相依套件炸掉、一次重啟、一次記憶體用盡，都會把
其他平台一起帶走；而「重啟 telegram」這種再普通不過的維運動作會變成「重啟全部」。
分成幾個行程之後，每個平台有自己的鎖、自己的記錄檔、自己的排程工作，彼此的失敗
互不相干。代價是幾個常駐行程，對一台本來就跑著批次的機器來說不算代價。

## 哪些平台會被起來

`_platform_runtime.enabled_platforms()` 說了算，判準兩個都要成立：

* `bot_config.json` 的 `platforms.<名稱>.enabled` 是 true（預設平台沒有那一格時
  視為 true，其餘平台預設 false）；
* 憑證檔 `<名稱>_bot_token.md` 存在而且不是空的。

**沒填憑證的平台是「缺席」，不是「失敗」**：它不會被起來，也不會每次啟動都抱怨
一次——一個 repo 不會同時接四個平台，逐次抱怨只會變成下一個把記錄檔洗掉的雜訊源。
想知道為什麼某個平台沒起來就跑 `--list`，那裡會逐平台說明原因。

用法：
    py -3 start_platforms.py           # 把開著的平台全部起起來
    py -3 start_platforms.py --list    # 只列出哪些會起來、哪些不會與為什麼
"""
from __future__ import annotations

import argparse
import subprocess  # nosec B404 — 這支腳本的工作就是起幾個受監督的子行程
import sys
from pathlib import Path

from axiomatic import _platform_runtime
from axiomatic._bot_config import load_bot_config
# DoD #5 的直譯器探索順序（`.venv` → `py -3` → 目前的直譯器）。**直接沿用監督啟動器
# 那一份，不另抄一份**：這支決定用哪個直譯器去跑那些監督者，跟它們自己挑直譯器是同
# 一個問題，而 `test_supervisor` 對那份複本已經有一整組守門（順序、逐字相同、`.venv`
# 佈局、fresh clone 的退路）。抄第二份的話那組守門看不到它，而它會自己漂。
# import 那支啟動器沒有副作用：它的模組層只算路徑，鎖與記錄檔要到 `main()` 才碰。
from start_discord_bot import python_command

REPO_ROOT = Path(__file__).resolve().parent
SUPERVISOR_SCRIPT = REPO_ROOT / "start_discord_bot.py"


def launch(platform: str) -> int:
    """起一支 `start_discord_bot.py --platform <名稱>`，回 0＝生得出來。

    **不等它。** 那支是長命的監督迴圈，等下去等於把這支變成只能帶一個平台的殼。
    子行程繼承這個主控台，所以在互動 shell 裡跑得到全部平台的輸出；記錄檔照樣
    逐平台各一份，不依賴這個視窗。
    """
    cmd = python_command() + ["-u", str(SUPERVISOR_SCRIPT), "--platform", platform]
    try:
        subprocess.Popen(  # nosec B603 — 固定腳本、參數不經 shell  # pylint: disable=consider-using-with
            cmd, cwd=str(REPO_ROOT))
    except OSError as error:
        # `!r` 而不是 `{error}`：`OSError` 的 str 會帶出檔案路徑。
        print(f"platform {platform}: could not start ({error!r})",
              file=sys.stderr)
        return 1
    print(f"platform {platform}: supervisor started")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="把每個開著的平台各起一個受監督的行程。")
    parser.add_argument("--list", action="store_true",
                        help="只列出哪些平台會起來、哪些不會與原因")
    # **不要寫成 `parse_args(argv)`。** `argv=None` 時 argparse 會去讀
    # `sys.argv`，而這支被 import 進測試時那是 **pytest 的**命令列——旗標會被
    # 當成這支的參數。`test_suite_safety` 對整個 repo 釘住這個形狀。
    opts = parser.parse_args(list(argv or []))

    if not SUPERVISOR_SCRIPT.exists():
        print(f"missing {SUPERVISOR_SCRIPT}", file=sys.stderr)
        return 1

    config = load_bot_config()
    rows = _platform_runtime.platform_survey(config)
    if opts.list:
        for name, will_start, why in rows:
            mark = "V" if will_start else "-"
            print(f"  [{mark}] {name}：{why}")
        return 0

    wanted = [name for name, will_start, _why in rows if will_start]
    if not wanted:
        print("一個平台都沒開著，或是開著的平台都沒填憑證。", file=sys.stderr)
        print("跑 `py -3 start_platforms.py --list` 看逐平台的原因。",
              file=sys.stderr)
        return 1
    failures = sum(launch(name) for name in wanted)
    print(f"已啟動 {len(wanted) - failures}/{len(wanted)} 個平台監督者。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
