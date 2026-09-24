# Axiomatic

[English](README.md) · [繁體中文](README.zh-TW.md) · [简体中文](README.zh-CN.md) · **日本語**

**チャットから操作する自動化ホスト。** いま自分がいるチャットプラットフォームから
マシンに指示を出すと、そのマシン上で作業してくれます。デスクトップとウィンドウの
自動化、プロセスとファイルの操作、定期ジョブ、差し替え可能なバックエンドによる
対話応答、そしてチャットで編集したキューに沿って画像を生成し続ける長時間の
ブラウザバッチ。

**特定のプラットフォームがこのプロジェクトの正体ではありません。** チャット面は
アダプタです。**1 プラットフォームにつき 1 プロセス**が監視付きで動き、それぞれ
個別にオン／オフでき、ロックもログも状態も自前で持ちます。プラットフォームを
増やすのは transport モジュール 1 本と設定 1 区画、減らすのは `false` 1 つ。
ワークロードはアダプタの後ろにいて、命令がどのプラットフォームから来たのかを
知りません。

**向いている人**：自分がそばにいない間も働き続けてほしいマシンを持っている人。
スマートフォンから開始・監視・調整・停止したい長時間の無人ジョブがあり、ホストを
操作する面はプラットフォームごとのオーナー ID に閉じておきたい、という使い方です。

> **表記の約束**：本文では外部依存を総称で書きます（画像サービス、対象サイト、
> ウェブ UI、応答バックエンド）。ファイル名・コマンド名・設定キー・コード上の
> シンボルはそのまま書きます。

<!-- section: contents -->
## 目次

- [これは何か](#これは何か)
- [必要なもの](#必要なもの)
- [セットアップ](#セットアップ)
- [1 つまたは複数のプラットフォームで動かす](#1-つまたは複数のプラットフォームで動かす)
- [設定ファイル](#設定ファイル)
- [キューとプロンプトファイル](#キューとプロンプトファイル)
- [コマンド](#コマンド)
- [バッチの挙動](#バッチの挙動)
- [監視と再起動](#監視と再起動)
- [実践的な使用例](#実践的な使用例)
- [さらに詳しいドキュメント](#さらに詳しいドキュメント)
- [開発](#開発)

---

<!-- section: what-this-is -->
## これは何か

```
  プラットフォーム A   プラットフォーム B   プラットフォーム C …
         │                   │                   │
         ▼                   ▼                   ▼
   ┌───────────┐       ┌───────────┐       ┌───────────┐
   │ bot プロセス│       │ bot プロセス│       │ bot プロセス│  1 つにつき 1 プロセス
   │ 専用ロック │       │ 専用ロック │       │ 専用ロック │  専用ログ・専用状態
   │ 専用の状態 │       │ 専用の状態 │       │ 専用の状態 │
   └─────┬─────┘       └─────┬─────┘       └─────┬─────┘
         └───────────────────┼───────────────────┘
                             │  ディスク上のファイル（唯一の結合）
        ┌────────────────────┼─────────────────┬──────────────────┐
        ▼                    ▼                 ▼                  ▼
  デスクトップと      ホストコマンド、     応答バックエンド    画像バッチ
  ウィンドウ制御      ジョブ、スケジュール （差し替え可能）  （ロックで担当決定）
```

- **プラットフォームプロセス**が意図の層です。コマンドを受け取り、誰が言ったのかを
  判定し、ディスク上のファイルを編集し、作業を起動して監視し、結果を返します。
  1 プロセスはちょうど 1 つのプラットフォームを担当し、他とは可変状態を一切
  共有しません。
- **ワークロード**が実行の層です。デスクトップ自動化はプラットフォームプロセス内で
  動き、画像バッチは監視付きの子プロセスとしてブラウザを操作します（ログイン、
  プロンプト入力、生成、ダウンロード）。
- **あいだにあるのはファイルだけ。** 共有状態はすべてディスク上にあるので、
  どちら側が再起動しても壊れません。バッチが bot を import することも、その逆も
  ありません。

唯一許された第 3 の経路が**受動的な共有モジュール**です（`_batch_config`、
`_queue_consume`、`_webrunner_shared` …）。独立していて状態を持たず、driver に
依存しない純粋なコードなので、両側から import してかまいません。

### 構成要素

**チャットプラットフォーム層**——bot をプラットフォーム非依存にしている部分

| ファイル | 役割 |
|---|---|
| `axiomatic/_chat_platform.py` | アダプタの継ぎ目：ID マッピング、能力フラグ、送信引数の正規化、transport レジストリ |
| `axiomatic/_telegram_transport.py` | プラットフォームの 1 つ。増やすたびに `_*_transport.py` が 1 本増えます |
| `axiomatic/_platform_runtime.py` | このプロセスがどのプラットフォーム担当か、自分の状態・ロック・ログがどこにあるか |

**コマンド本体**（モジュール名は歴史的なもので、特定プラットフォーム専用ではありません）

| ファイル | 役割 |
|---|---|
| `axiomatic/discord_bot.py` | トップレベル 13 個のスラッシュコマンドと 25 個のコマンドグループ（合計 269 個のサブコマンド）、ID ゲート、デスクトップとホストの制御、バッチ監視、応答バックエンドの調停、単発生成キュー |
| `axiomatic/_gui_control.py` | デスクトップ自動化のファサード（マウス、キーボード、ウィンドウ、クリップボード、文字認識、画像位置検出） |
| `axiomatic/dorossi_backend.py` | 応答バックエンドとそのセッション——複数のバックエンドを 1 つのインターフェースの裏に |

**画像生成というワークロード**

| ファイル | 役割 |
|---|---|
| `axiomatic/webrunner_novelai.py` | バッチ生成、Selenium 版（本番の既定） |
| `axiomatic/webrunner_je_only.py` | バッチ生成、wrapper 版（予備。`/run` はまずこちらを試します） |
| `axiomatic/_webrunner_shared.py` | 2 つの版が共有する中核：DOM 操作、純粋関数、バッチループ。driver 非依存 |

**ランチャー**（リポジトリ直下）

| ファイル | 役割 |
|---|---|
| `start_platforms.py` | 有効なプラットフォームごとに監視付きプロセスを 1 つずつ起動 |
| `start_discord_bot.py` | **1 つの**プラットフォームの監視ループ（`--platform <名前>`） |
| `start_webrunner.py` | 画像バッチの監視ループ |
| `run_batch.py` | ローカル単発バッチ：事前チェック、実行計画の表示、引き渡し |
| `install_autostart.py` | Windows タスクスケジューラのログオンタスクを登録／削除 |

---

<!-- section: requirements -->
## 必要なもの

Windows、Python 3.11 以上、ブラウザ、そして `requirements.txt` のパッケージ。
`requirements.txt` は**バージョンを固定しません**（新しい clone では各パッケージの
最新が入ります）が、このプロジェクトが実際に到達しうる既知の脆弱性があるものには
下限を設けています。

| パッケージ | 用途 |
|---|---|
| `selenium` | Selenium 版のブラウザ操作 |
| `je_web_runner` | wrapper 版のブラウザ操作 |
| `urllib3` | 両版とも `ReadTimeoutError` を直接捕捉します（selenium は再公開していません） |
| `discord.py` | ネイティブのスラッシュメニューを持つプラットフォームの transport とイベントループ |
| `aiohttp` | 外部 API 呼び出しと、他プラットフォームが使うロングポーリング transport |
| `psutil` | プロセス生存確認とウィンドウ照合（**必須**） |
| `je-auto-control` | デスクトップ自動化の**唯一の実装**：マウス、キーボード、ウィンドウ、クリップボード、文字認識、画像位置検出 |
| `pytesseract` | `/locate text find\|click\|wait` の文字認識ラッパー。**pip パッケージだけでは足りません**——認識エンジンを別途入れて PATH に通す必要があります。無い場合その 3 つは汎用メッセージを返すだけで落ちません |
| `comtypes` | `/locate ui …` の要素検出。導入できない場合そのグループだけ汎用メッセージを返し、他の検出方法は通常どおり動きます |
| `Pillow` | `/grid` の 2×2 モザイク生成 |
| `pyfiglet` | `/fun ascii` |
| `simpleeval` | `/fun calc`——`eval` ではなく安全な評価器 |
| `anthropic` | 応答バックエンドの `api` 方式（任意。無くても bot は起動し、その経路だけ縮退します） |
| `matplotlib` | 旧トークングラフヘルパーの互換テスト |

ブラウザ自動化ライブラリは、2 つのバッチ生成器が `sys.path` 経由で**隣り合う
チェックアウト**または `WEBRUNNER_PATH` から読み込みます。

```
<parent>/
├── Axiomatic/    ← このリポジトリ
└── WebRunner/    ← 隣り合う clone
```

---

<!-- section: setup -->
## セットアップ

### 1. 依存をインストール

```powershell
py -3 -m pip install -r requirements.txt
```

### 2. 資格情報を入れる

**資格情報と設定はバージョン管理に入っていません。** リポジトリにあるのは
テンプレートなので、最初にコピーします。

```powershell
copy auth.example.md                   auth.md
copy discord_bot_token.example.md      discord_bot_token.md
copy telegram_bot_token.example.md     telegram_bot_token.md
copy bot_config.example.json           bot_config.json
```

| ファイル | 中身 |
|---|---|
| `auth.md` | 画像サービスのログイン。2 行で `username: ...` / `password: ...`（最初のコロンで分割） |
| `discord_bot_token.md` | ネイティブのスラッシュメニューを持つプラットフォームの bot トークン（ファイル全体がトークン、または `Token: xxx` の 1 行） |
| `telegram_bot_token.md` | もう 1 つのプラットフォームの bot トークン。**そのプラットフォームを使うときだけコピー**——ファイルが無い、または空なら、そのプラットフォームは単にオフです |

これらをバージョン管理に戻さないでください。コミット時は**ファイルごとに
`git add`**し、`git add -A` は使いません。

### 3. bot を設定する

`bot_config.json` を編集します。最低限この 2 つ。

- `channel_id`——チャンネル限定コマンドはこのチャンネルでのみ応答します。
- `owner_user_id`——自分のユーザー ID。ホスト操作系のグループと `/dorossi` はこれ
  しか受け付けません。`0` のままだと全員に対して拒否されます。

どちらも未設定だと、bot は起動時に説明を表示してきれいに終了します
（traceback ではありません）。

ネイティブのスラッシュメニューを持つプラットフォームでは、開発者ポータルで
**Message Content**、**Presence**、**Server Members** の 3 つの特権 intent を
有効にしてください。無効のままだと起動時に `PrivilegedIntentsRequired` が出ます。

### 4. キューに中身を入れる

動かすにはキューが 1 つ埋まっていれば十分です。残り 3 つは `prompt.md`、
`character1.md`、`character2.md`、`undesired.md` へ自動的にフォールバックします。
このフォールバックもバージョン管理外なので、テンプレートをコピーしてください。

```powershell
copy prompt.example.md      prompt.md
copy character1.example.md  character1.md
copy undesired.example.md   undesired.md
```

キュー自体を手で作ることはほとんどありません——`/todo char1 add <説明>` が書いて
くれます。形式は**1 行 1 件**でカンマでは分割しません。`todo_character2.md` は
位置対応なので、空行は「このペアには 2 人目を出さない」という意味を持ち、
そのまま残す必要があります。

---

<!-- section: running-one-or-several-platforms -->
## 1 つまたは複数のプラットフォームで動かす

**1 プラットフォームにつき 1 プロセス。** 各プロセスは専用の単一インスタンス
ロック、専用のログ、専用の状態ファイルを `state/<プラットフォーム>/` の下に持ちます。
可変状態をまったく共有しないので、1 つが落ちても、再起動されても、オフにされても、
他には触れません。

有効なものをまとめて起動：

```powershell
py -3 start_platforms.py
```

どれが起動し、どれが起動せず、なぜかを見る：

```powershell
py -3 start_platforms.py --list
```

1 つだけ起動：

```powershell
py -3 start_discord_bot.py --platform telegram
```

bot をまったく使わないローカルバッチ：

```powershell
py -3 run_batch.py
```

> ⚠️ **bot の `/run` とローカルランチャーを同時に使わないでください**——どちらも
> バッチ生成器を起こし、同じブラウザプロファイルのロックを奪い合います。

### 2 つのプラットフォームを同時に動かすには

1. そのプラットフォームのトークンファイル（`<プラットフォーム>_bot_token.md`）を
   埋める。
2. `bot_config.json` で `platforms.<名前>.enabled` を `true` にし、そのプラット
   フォーム**上の** `owner_user_ids` と `allowed_chat_ids` を文字列で列挙する。
3. `start_platforms.py` を実行する。

あとは自動です。状態ディレクトリ、ロック、ログ、自動起動タスクはすべて
プラットフォーム名で命名されます。資格情報が無いプラットフォームは
**「壊れている」ではなく「不在」**——起動されませんし、毎回文句も言いません。
理由を知りたいときが `--list` の出番です。

既定のプラットフォームも同じ方法でオフにできます
（`platforms.discord.enabled: false`）。区画が無い場合はオン扱いです。新しい clone
の bot が何もしない状態は、設定が効いていない状態と見分けがつかないからです。

### 画像バッチは 1 つだけ

バッチはマシン全体の資源です（ブラウザ 1 つ、キューファイル 1 組、出力先 1 つ）。
したがってプラットフォームごとに分かれません。バッチ監視ロックを取ったプロセスが
それを監視し、他のプロセスはバッチ操作系のコマンドに「別のプロセスがすでに監視中」
と返します——2 つ目の監視者を立てると互いに終了させ合い、再生成し合い、しかも
両方のログが正常に見えてしまうからです。

キューの編集には影響しません。キューはディスク上にあるので、どのプラット
フォームから編集しても有効です。ロックが決めるのは「誰がバッチを監視するか」
だけです。

### ログオン時に自動起動（Windows）

```powershell
py -3 install_autostart.py --install    # 冪等
py -3 install_autostart.py --status
py -3 install_autostart.py --remove
```

有効なプラットフォームごとに 1 件（`\Axiomatic\Bot-<プラットフォーム>`）、
さらにバッチ用に 1 件（`\Axiomatic\Batch`）登録します。トリガーは起動時ではなく
**ログオン時**です。バッチにはブラウザを開ける対話的なデスクトップが要ります。

---

<!-- section: configuration-files -->
## 設定ファイル

| ファイル | 中身 | 追跡する？ |
|---|---|---|
| `batch_config.json` | バッチ生成のパラメータ：1 ペアあたりの枚数、待ち時間、稼働／休止の時間帯、ブラウザの定期再起動 | する |
| `bot_config.json` | チャンネルとオーナーの ID、ロール、応答バックエンドの調整値、起動許可リスト、`platforms.*` | しない（テンプレート：`bot_config.example.json`） |
| `presence_games.json`、`presence_music.json`、`presence_rpc.json` | ローカル状態の対応表 | しない（テンプレート：`*.example.json`） |
| `bot_prompts/` | 10 個のプレーンテキストファイル（人格、自走ループ各段の指針、単発生成の既定スタイル接尾辞） | する |

`bot_config.json` は**起動時に 1 回だけ**読まれます——変更を反映するには
`/sys restart`。知らないキーは警告 1 行を出して無視されます。キー名の打ち間違いは
「設定が効かない」とまったく同じ症状になるからです。

`batch_config.json` は `/config set` で動作中に変更でき、バッチは画像と画像の
あいだで読み直します。

---

<!-- section: queues-and-prompt-files -->
## キューとプロンプトファイル

| キュー | 空のときのフォールバック |
|---|---|
| `todo_prompt.md` | `prompt.md` |
| `todo_character1.md` | `character1.md` |
| `todo_character2.md` | `character2.md`（空行は「このペアに 2 人目なし」） |
| `todo_undesired.md` | `undesired.md` |

ペアリングは複数のキューを並べて進み、短いほうはフォールバックで埋めます。
`todo_prompt.md` の `end` は**停止マーカー**です。その手前のペアまで終えて
きれいに停止するので、長いキューを何も消さずに止めておけます。

読み取り側は行の境界だけで分割し（1 件の中にカンマ・コロン・括弧が入るのは正常
です）、ノーブレークスペースは通常の空白に正規化します。書き込み側は行の境界を
含む内容を拒否します。読み戻したときに複数件になってしまうからです。

---

<!-- section: commands -->
## コマンド

ネイティブのスラッシュメニューがあるプラットフォームでは、コマンドは
**スラッシュコマンド**です。`/` を打つと補完され、引数は送信前に型と範囲が
チェックされます。スラッシュメニューが無いプラットフォームでは同じコマンドを
テキスト面から使います（後述）。実装も権限ゲートも 1 組だけです。
**13 個のトップレベルスラッシュコマンド**と **25 個のコマンドグループ**
（トップレベル 37 枠、プラットフォームの上限は 100）があり、その下に
**269 個のスラッシュサブコマンド**があります。

グループはサブコマンドをいくつ抱えてもトップレベル 1 枠しか使わないので、使用頻度の
低いコマンドをグループにまとめるのが長く拡張し続ける唯一の方法です。コマンドごとの
詳細は [`COMMANDS.md`](COMMANDS.md)、[`commands/`](commands/README.md)、
あるいは `/help` で。

- 🔒 **チャンネル限定**：`channel_id` のチャンネルでのみ応答します
  （**オーナーはどこでも可**）。
- 🌐 **どこでも**：bot から見えるチャンネルならどこでも。
- 🔑 **オーナー限定**：bot が動いているマシンを操作するコマンド。
  **`user_roles` は参照しません**。

> 🔑 **ホスト操作は常にオーナー限定です。** `/input`、`/screen`、`/win`、`/clip`、
> `/locate`、`/macro`、`/watch`、`/proc`、`/host` は**グループごと**ゲートされ
> （新しいサブコマンドも自動的に保護されます）、加えて `/sys restart` のような
> 個別のコマンドも対象です。ゲートはディスパッチの**前**、かつロールゲートの
> **前**にあります——`user_roles` の 3 つのリストが空（既定）ならロールゲートは
> 何もしないので、その下にデスクトップ操作をぶら下げても保護になりません。

> **返信はすべて汎用化されます**：サービス名、ホストのパス、ファイル名、PID、
> 生の例外テキストは出しません。詳細は stderr とログにだけ書きます。

### 🔒 チャンネル限定

| コマンド | 内容 |
|---|---|
| `/eta` | 完了予想時刻（停止マーカーがあればそこまで） |
| `/latest` | 直近 N 枚をアップロード |
| `/queue` | キューごとの残数と、実際に実行されるペア数 |
| `/run` | バッチを開始（予約可：in 90m / at 02:00 / cancel） |
| `/status` | バッチの状態と実行中の版 |
| `/stop` | バッチを停止 |

| ファミリ | サブコマンド |
|---|---|
| **生成キュー** | `/todo dedupe\|duplicate\|find\|move\|shuffle\|swap`<br>`/todo char1 add\|addx3\|clear\|list\|pop\|remove`<br>`/todo char2 add\|clear\|default\|list\|pop\|remove`<br>`/todo negp add\|clear\|list\|pop\|remove`<br>`/todo prompt add\|clear\|default\|end\|insert\|list\|pop\|remove\|template\|unend` |
| **キューが空のときの既定値** | `/preset info`<br>`/preset main append\|clear\|set`<br>`/preset neg append\|clear\|set` |
| **バッチ制御** | `/gen current\|image\|image_queue\|pause\|plan\|preview\|progress\|resume` |
| **出力の閲覧** | `/out debug_show\|history\|latest_for\|rate\|sample\|stats` |
| **お気に入り** | `/fav clear\|list\|remove\|show` |
| **実行ログ** | `/log clear\|errors\|grep\|size\|tail` |
| **運用と診断** | `/sys audit\|backfill_paths\|cleanup_debug\|dashboard\|disk\|doctor\|git_pull\|health\|introspect_dom\|metrics\|probe_status\|restart\|undo\|update_check` |
| **プロセス制御** | `/proc kill\|launch\|list\|usage` |
| **バッチのパラメータ** | `/config reload\|reset\|set\|show` |
| **画面** | `/screen all\|gif\|info\|main\|pixel\|region\|text\|window` |
| **ウィンドウ** | `/win focus\|grid\|list\|move\|pos\|snap\|state\|wait`<br>`/win layout list\|remove\|restore\|save` |
| **キーボードとマウス** | `/input click\|hotkey\|type`<br>`/input key clear\|down\|press\|status\|up`<br>`/input mouse click\|dclick\|down\|drag\|move\|pos\|scroll\|up` |
| **クリップボード** | `/clip files\|formats\|image\|paste\|read\|set\|setimage` |
| **画面上の位置検出** | `/locate gone\|pixel`<br>`/locate image click\|find\|wait`<br>`/locate text click\|find\|wait`<br>`/locate ui click\|find\|gone\|read\|tree\|wait` |
| **マクロ** | `/macro delete\|edit\|insert\|list\|record\|rm_line\|run\|save\|show\|stop` |
| **ホストコマンドとファイル送受信** | `/host get\|panic\|put`<br>`/host job clear\|eof\|list\|log\|run\|send\|stop`<br>`/host sh cd\|run\|stop` |
| **条件監視** | `/watch clip\|job\|list\|pixel\|port\|process\|stop\|text\|ui\|window` |
| **定期スケジュール** | `/schedule add\|list\|remove\|run` |
| **独立監視プロセス** | `/launcher start\|status\|stop` |

### 🌐 どこでも

| コマンド | 内容 |
|---|---|
| `/booru` | 画像ボード検索：タグからランダムに 1 枚（あいまい可。タグ無しなら既定画像） |
| `/e621` | ケモノ系ボードからランダムに 1 枚（既定は NSFW。rating:safe を付ければ SFW） |
| `/grid` | 最新 4 枚を 2×2 に並べて投稿 |
| `/help` | コマンドヘルプ（tw / cn / en） |
| `/iqdb` | ボード横断の逆画像検索（上位の出典と類似度 %） |
| `/nsfw` | 画像ボード検索の NSFW ショートカット |
| `/safebooru` | 全年齢ボードからランダムに 1 枚 |

| ファミリ | サブコマンド |
|---|---|
| **応答バックエンド**（🔑 オーナー限定） | `/dorossi abort\|ai\|ask\|compact\|effort\|errors\|fullmode\|health\|logs\|model\|retry\|running\|status\|tokens\|workspace_clean`<br>`/dorossi allowdir add\|list\|remove`<br>`/dorossi queue clear\|detail\|failed_clear\|move\|remove\|retry_failed\|show\|undo`<br>`/dorossi session archive\|continue\|delete\|export\|list\|new\|rename\|reset\|switch` |
| **タグ用ツール** | `/tag autocomplete\|count\|suggest\|wiki` |
| **お遊び／ランダム** | `/fun 8ball\|ascii\|calc\|choose\|coinflip\|rand\|reverse\|roll\|rps\|timer` |
| **エンコードと小物** | `/tool base64\|color\|hash\|qr\|say\|unbase64\|urldecode\|urlencode` |
| **情報とメタデータ** | `/info avatar\|channel\|ping\|server\|uptime\|version` |
| **公開データ検索** | `/web anime\|cat\|crypto\|dict\|dog\|fact\|github\|joke\|quote\|wiki\|xkcd` |

### `@bot <text>`——自由質問

サブコマンドを伴わないメンションが自由質問の入口です。スラッシュコマンドにせず
**あえてメンションのまま**にしています。1 通のメッセージに複数行・添付・返信の
文脈を載せられますが、選択肢の入力欄には載せられません。加えて、インタラクションの
トークンは長いラウンドよりずっと早く失効します。

### リアクション

bot が投稿した画像に ⭐ を付けるとお気に入り、🗑️ でファイル削除です。書き込みは
バックアップ機構を通るので `/sys undo` で戻せます。

### スラッシュメニューが無いプラットフォーム

ネイティブのスラッシュメニューが無いプラットフォームでは、同じコマンドをテキスト面
から使います。その面はそうしたプラットフォーム向けにだけ
[`docs/platforms.md`](docs/platforms.md) に書かれています。スラッシュメニューが
ある場所では、スラッシュコマンドが唯一の公開インターフェースのままです。

---

<!-- section: batch-behaviour -->
## バッチの挙動

1. **起動ごとにフルセットアップを 1 回**（ブラウザ再起動のたびにも）：ログイン、
   設定スナップショットの適用、ページが想定どおりかの確認。
2. **キューを動的に消費**：キャラクターごとにキューを読み直すので、実行中の編集は
   次の実行ではなく次のペアから効きます。
3. **ペアごとのループ**：設定した枚数まで生成し、その都度ダウンロードします。
4. **完了しきい値**：十分な枚数が保存されて初めてキューから取り出します——途中で
   落ちたペアは黙って飛ばされず、やり直しになります。
5. **再開チェックポイント**：進捗はアトミックに書かれるので、いつ止められても
   正しい位置から再開します。
6. **ブラウザの定期再起動**：長時間実行ではブラウザのメモリが膨らむため、定期的に
   作り直します。
7. **イベントストリーム**：バッチが構造化イベントを追記し、bot がそれを見て報告
   します。

終了コード：`0` 正常終了、`1` やることなし、`2` セッション準備失敗、`3` 生成ゼロ、
`4` ブロック（再生成しない）、`5` 設定未完了（同じく再生成しない）。定数は
`axiomatic/_supervisor.py` にあります。

---

<!-- section: supervision-and-restart -->
## 監視と再起動

各ランチャーは子プロセスを監視ループで回し、指数バックオフと早期失敗時の打ち切りを
備え、子プロセスのコンソール出力をログファイルにも流します。ランチャーのウィンドウで
Ctrl+C を押すと、そのループはきれいに終わります。

**単一インスタンス保護**は OS が保持するファイルロックです。プロセスが消えた瞬間に
解放されるので、掃除すべき残留フラグはありません。

| ロック | 防ぐもの |
|---|---|
| `state/<プラットフォーム>/.<プラットフォーム>.discord_bot_supervisor.lock` | そのプラットフォームの 2 つ目の監視プロセス |
| `state/<プラットフォーム>/.<プラットフォーム>.discord_bot.lock` | そのプラットフォームの 2 つ目の bot プロセス |
| `.webrunner_supervisor.lock` | 2 つ目のバッチ監視プロセス |
| `.batch_supervisor.lock` | どの bot プロセスがバッチを監視するかを決める |
| `chrome_slot.lock` | プロセス横断のブラウザ枠。バッチと検証スクリプトが同時に 2 組のブラウザを開かないようにする |

---

<!-- section: worked-examples -->
## 実践的な使用例

この bot を日々どう操作するかを示す短いシナリオです。注釈付きのより詳しいウォーク
スルー（シナリオごとに 1 ファイル）は [`Examples/`](Examples/README.md) にあります。
以下のコマンドは**スラッシュメニューのあるプラットフォーム**を例にしています。メニュー
のないプラットフォームでは同じコマンドをテキストで入力します（
[`docs/platforms.md`](docs/platforms.md) 参照）。以下の id はすべてプレースホルダです。

**バッチを設定して開始する。** キューを埋め（1 行 1 件、実行中の編集は次のペアから
反映）、どこで止めるかを印し、プレビューしてから開始して見守ります:

```
/todo prompt add    scenery, wide shot, soft light
/todo char1 add     example-character
/todo prompt end          # 停止マーカー：バッチはここで終わる
/gen plan                 # 実際に走るペアをプレビュー
/run                      # 開始（/run in 90m、/run at 02:00 も可）
/gen current              # 実行中のペア
/gen progress             # 完了枚数と残り
/eta                      # 完了予想（停止マーカーまでで数える）
/gen pause                # および /gen resume——チェックポイントは保持
```

**応答バックエンドに尋ねる。** 自由文のメンションが質問の入口で、残りはオーナー限定の
セッション管理です:

```
@bot <質問を自由文で。例：前回のバッチの要約とおかしい点の指摘>
```

`/dorossi session continue all` は中断した自律ループを一度にすべて再開します——
**自分で中止したものも含めて**。あるセッションをこれでも再開させたくなければ
`/dorossi session delete` でアーカイブします。`/dorossi ai` はバックエンドを、
`@bot /model <エイリアス> …` はそのセッションのモデルを切り替えます。プランの使用量
上限に当たったターンは**自分でキューに停車**し、実時計の再実行時刻を持って自動で
戻ってきます——`/dorossi queue show` で停車中の質問が見え、`/dorossi queue remove`
（id 指定）で 1 件を取り消せます。

**このマシンが今何をしているか見る。** 長いバッチの実行中、これはブラウザのメモリ、
バッチの稼働時間、マシン全体の残りメモリとディスクを表示します——`/gen current` と
`/dorossi running` では見えない OS 側です:

```
/proc usage
```

**複数のプラットフォームを同時に動かす。** 1 プラットフォームにつき 1 つの監視付き
プロセスで、それぞれ自前のロック・ログ・`state/<platform>/` 以下の状態を持ち、バッチ
ロックを握っているプロセスがバッチを監視します:

```
py -3 start_platforms.py          # 有効なプラットフォームをすべて起動
py -3 start_platforms.py --list   # どれが起動するか、その理由
```

**見張らなくてよい復旧。** ネットワークが切れたバッチや自律ループはその場で止まり、
接続が戻ると同じ地点から再開します——回数上限はなく、`/stop`（バッチ）または
`/dorossi abort`（ループ）だけが止められます。プランの使用量上限で停車したターンは、
上限がリセットされると自分で再実行するので、こちらから再度尋ねるのを覚えておく必要は
ありません。

---

<!-- section: where-the-deeper-docs-are -->
## さらに詳しいドキュメント

| ドキュメント | 対象 |
|---|---|
| [`docs/`](docs/index.md) | 完全なマニュアル（Sphinx／Read the Docs 形式） |
| [`docs/setup.md`](docs/setup.md) | 初回インストールの手順 |
| [`docs/config.md`](docs/config.md) | すべての設定キー |
| [`docs/platforms.md`](docs/platforms.md) | スラッシュメニューが無いプラットフォームでの運用 |
| [`docs/workflow.md`](docs/workflow.md) | ペアリング規則、フォールバック、停止マーカー |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | 実行が止まる、ブラウザが落ちる、ログインに失敗する |
| [`COMMANDS.md`](COMMANDS.md) | コマンド一覧 |
| [`commands/`](commands/README.md) | グループごとの引数・範囲・権限リファレンス（コマンドツリーから生成） |
| [`architecture.md`](architecture.md) | レイヤー、エントリポイント、主要フロー、拡張点 |
| [`CLAUDE.md`](CLAUDE.md) | このリポジトリを触る人が常に守るべき要件 |

マニュアルをローカルでビルド：

```powershell
py -3 -m pip install -r docs/requirements.txt
py -3 -m sphinx -b html docs docs/_build/html
```

---

<!-- section: development -->
## 開発

```powershell
py -3 -m pytest              # すべて
py -3 -m pytest test/test_platform_processes.py   # 1 ファイルだけ
```

テストはパッケージ内ではなくリポジトリ直下の `test/` にあります。テストの大部分は
このプロジェクトの厳格な規則を静的な検査に変えたものです。モジュール境界、
オーナー限定ゲート、アトミック書き込み、テキストエンコーディングの明示、繁体字の
語彙、そしてこの README 群も含まれるドキュメントの整合性。

コミット前に、少なくとも次の 4 つ。

1. `py -3 -c "import sys; sys.path.insert(0, 'axiomatic'); import discord_bot, webrunner_novelai, webrunner_je_only; print('OK')"` が `OK` と表示する。
2. 新しいスラッシュコマンドがすべてのユーザー向けドキュメントに反映され、
   `commands/*.md` は手編集ではなく
   `py -3 axiomatic/gen_command_docs.py` で再生成されている。
3. 変更が `architecture.md` の記述に触れるなら、同時に更新する。
4. コミットの件名は何が変わったかを書く。ファイルは 1 つずつ stage する。

完全な一覧は [`CLAUDE.md`](CLAUDE.md) の Definition of Done です。
