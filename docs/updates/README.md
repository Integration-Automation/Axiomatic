# docs/updates：更新紀錄索引

`Progress.md` 只放**還沒做**的事。做完的事、量到的數字、決策都記在這裡：**一個月一個批次檔**，
每筆紀錄有固定格式的 ID 與標籤，下方的索引表每筆一列。

> 這裡**不放待辦**。紀錄裡提到還沒做的，只寫一句指標（例如「待辦見 `Progress.md`」），待辦本身一律寫進
> `Progress.md`。

## 怎麼查

在 repo 根目錄執行：

| 想找什麼 | 指令 |
|---|---|
| 列出全部紀錄（一行一筆） | `rg -n "^## U-2" docs/updates` |
| 某種類型的紀錄 | `rg -n "^## U-2.*#done" docs/updates` |
| 某一天或某個月 | `rg -n "^## U-202610" docs/updates` |
| 某筆紀錄的全文 | `rg -n -A 40 "^## U-20261001-01" docs/updates` |
| 全文關鍵字 | `rg -n "儀表板" docs/updates` |

沒有 `rg` 時改用 `git grep -n "^## U-2" -- docs/updates`，或 PowerShell 的
`Select-String -Path docs/updates/*.md -Pattern '^## U-2'`。

## 紀錄格式

```markdown
## U-YYYYMMDD-NN · YYYY-MM-DD · 一句話標題 · #類型 #主題

- **做了什麼**：…
- **結果／數字**：…
- **改到的檔案**：`路徑`…
- **來源／證據**：commit hash、檔案:行號…
- **待辦**：沒有 ／ 見 `Progress.md`
```

- **ID**：`U-` + 日期 + 當天流水號（01、02…），不重編、不重用。
- **類型標籤**（選一個）：`#snapshot` 盤點、`#done` 完成的待辦、`#decision` 決策、`#incident` 事故、`#migration` 搬遷、`#docs` 文件。
- 每筆只留結論、數字、改到的檔案與證據。

## 批次規則

1. 一個月一個檔案 `docs/updates/YYYY-MM.md`，新紀錄加在檔案最後；超過約 800 行就續寫 `YYYY-MM-b.md`。
2. **取鎖之後才佔 ID**：`mkdir docs/updates/.id-lock`（建立資料夾是原子操作；已存在就等幾秒再試，超過 10 分鐘視為殘留），
   寫下標題行與索引列之後 `rmdir docs/updates/.id-lock`，再補內文。提交前確認這個 ID 只有一筆。
3. 索引一列只放一行標題，不放摘要。
4. 已寫入的紀錄不改內容；發現有誤就新增一筆更正，並在原紀錄末尾加一行「→ 更正見 U-…」。
5. 做完一條待辦：在同一個 commit 裡從 `Progress.md` 刪掉它、新增一筆 `#done` 紀錄、加一列索引。

`docs/` 是 Sphinx 原始碼，`updates/` 已排除在建置之外（`docs/conf.py` 的 `exclude_patterns`）。

## 索引

| ID | 日期 | 標題 | 標籤 |
|---|---|---|---|
| U-20261001-01 | 2026-10-01 | 移植第三個對話後端、用量上限停車續跑、文字指令提示 | #done |
| U-20261001-02 | 2026-10-01 | 指令表另外產生機器可讀的 commands.json | #done |
| U-20261001-03 | 2026-10-01 | 桌面自動化：打字與組合鍵的放開交給函式庫 | #done |
| U-20261001-04 | 2026-10-01 | 第三個對話後端的提示改從 stdin 送 | #done |
| U-20261001-05 | 2026-10-01 | /dorossi model 完整版本號與 /dorossi model_list | #done |
| U-20261001-06 | 2026-10-01 | 本機儀表板重新設計，加上平台切換 | #done |
| U-20261001-07 | 2026-10-01 | 回覆改成卡片與分頁：/dorossi running、/sys health、/gen plan、/out stats | #done |
| U-20261001-08 | 2026-10-01 | 補齊設定鍵說明，新增桌面自動化與 Dorossi 專頁 | #done |
| U-20261001-09 | 2026-10-01 | 移植 bot 修正：/sys version、長輸入截短、圖庫欄位、/gen current | #done |
| U-20261001-10 | 2026-10-01 | 移植測試：佇列、狀態、小工具、桌面控制指令與第三個後端 | #done |
