"""設定檔警告的去重：同一段文字只往 stderr 印一次。

**為什麼這件小事需要一個模組。** 這個專案的設定檔載入器沒有快取，而呼叫它們的是
迴圈：`presence_probe` 的兩支載入器每個 presence tick（約 8 秒）重讀一次、
`_batch_config` 每個角色重讀一次、`_bot_config` 在每一次對外 HTTP 請求的路徑上。
所以一個放著沒改的錯字會用**同一行文字**洗掉整份記錄檔——一天一萬多行，而落地的
`discord_bot.log` 由 `trim_log` 只留尾段，於是真正有用的診斷會被自己的警告擠掉。
**一個把有用訊息趕出去的警告，比不警告還糟。**（同一份 log 現在就有 96% 的行是同
一句 `rpc apply ->`，那是這條規則的反面教材。）

**去重的鍵就是完整的訊息文字，不是「警告種類」。** 使用者改了設定檔、換成另一種錯
法時那是一段新文字，應該要再看得到一次；改對了則本來就不會再印。這也是為什麼訊息
裡要帶著實際收到的值——那讓「同一個錯」與「另一個錯」分得開。反過來說，用「種類」
當鍵會讓第一個錯字把之後所有同類診斷**永久靜音**。

**為什麼是共用模組（2026-09-09）。** 這六行原本在 `presence_probe` / `_batch_config`
/ `_bot_config` 各有一份。`_batch_config` 的註解當時就寫了「**若出現第三份**，就該
提成共用的被動模組」——第三份出現了，所以照做。副作用是好的：三份之間「行為必須
一致」原本要靠一支對帳測試守著，現在那個前提不存在了。

這是 `CLAUDE.md` 允許的**被動共用模組**：純標準函式庫、與 driver 無關、bot 與產圖
批次兩側都可以 import，而且它**不 import 專案裡的任何東西**（所以不可能造成循環）。

`_WARNED` 刻意是模組層的可變狀態，因為去重本來就要跨呼叫存活。測試之間會互相汙染
（兩支測試觸發同一段文字，後跑的那支什麼都看不到），所以 `conftest.py` 有一個
autouse 夾具負責清它——名字維持 `_WARNED` 就是為了讓那個夾具照舊運作。
"""
from __future__ import annotations

import sys

_WARNED: set[str] = set()


def warn_once(message: str) -> None:
    """同一段文字只往 stderr 印一次（理由見模組 docstring）。"""
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(message, file=sys.stderr)
