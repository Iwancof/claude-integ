# claude-integ + claude-router

Claude Code のモデル選択を1セッションに統合する2点セット。
Anthropic 純正(サブスク OAuth のまま)・Kimi・GLM・GPT(Codex) を、
1つの Claude Code セッションから使い分けられる。

- **claude-router** (`claude_router.py`) — モデル名で振り分ける常駐プロキシ
  (systemd user service, `127.0.0.1:8399`, Python stdlib のみ)。
- **claude-integ** (`claude-integ`) — 統合ランチャー。ベンダー別に
  BASE_URL を切り替えていた複数の起動 alias を1本に置き換える。

## 何を解決するか

`ANTHROPIC_BASE_URL` を1ベンダーに向けると他ベンダーが使えなくなり、
web search / ToolSearch / teammate 等の純正機能も壊れがち。
router は **JSON body の `model` プレフィックス**で振り分ける:

| model | 行き先 | 認証 |
| --- | --- | --- |
| `claude-*`(および不明・model なし) | `api.anthropic.com` | **ヘッダ完全素通し**(サブスク OAuth のまま) |
| `kimi-*`, `k3*` | `api.kimi.com/coding` | config の key に差し替え |
| `glm-*` | `api.z.ai/api/anthropic` | 同上 |
| `gpt-*` | CLIProxyAPI `127.0.0.1:8317`(Codex OAuth) | 同上 |

全バックエンドが Anthropic Messages API 互換なので、原則**ペイロード変換なし**の
純粋転送(唯一の例外は次節の WebSearch)。非 `/v1/messages` エンドポイント(usage 等)も
Anthropic へ素通しされるため、純正パスはプロキシ無しと bit-identical。
プレフィックス・バックエンドは config.toml で自由に増減できる
(使わないベンダーは消せばよい)。

### WebSearch は model に関わらず Anthropic へ

WebSearch / WebFetch は Claude Code が**別の `/v1/messages` リクエスト**として投げ、
そこに Anthropic の server tool `web_search_*` を載せる。この tool はベンダー側に
存在しないので、素直に転送すると**ベンダーモデルが自分の知識で書いた文章が
そのまま「検索結果」として CLI に取り込まれる**(UI は "Did 0 searches in 40s"、
記録上も `searchCount: 0`。それでも結果らしきテキストは表示される)。

router は `tools` 配列に server tool を見つけたリクエストだけ Anthropic へ回し、
`model` を `server_tool_model`(既定 `claude-sonnet-5`)に差し替える。
これでベンダーセッションでも WebSearch が実際に検索する。判定は `tools` 配列のみを
見るため、会話本文に `web_search_20260209` の文字列が出てもセッション本体の
振り分けは変わらない。

## 前提条件

- Claude Code CLI(サブスクリプションでログイン済み)
- Python 3.11+(stdlib のみ、追加パッケージ不要)/ jq / curl
- 使いたいベンダーの API key(Kimi / GLM 等、Anthropic 互換エンドポイントを持つもの)
- GPT を使う場合: [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) を
  別途常駐させ、Codex アカウントでログインしておく(`gpt-*` はそこへ転送される)

## インストール

```console
$ git clone https://github.com/Iwancof/claude-integ
$ cd claude-integ && ./install.sh
$ $EDITOR ~/.config/claude-router/config.toml   # ベンダー key を記入(chmod 600 済み)
$ systemctl --user restart claude-router
```

`install.sh` は冪等(再実行可)。既存の config は上書きしない。
systemd の無い環境では `claude_router.py --config ...` を手動常駐させる。

## 使い方

```console
$ claude-integ              # 既定(integ.conf があればその配置)
$ claude-integ kimi         # kimi-for-coding
$ claude-integ k3           # Kimi K3
$ claude-integ sol          # gpt-5.6-sol(terra / luna / 5.5 も)
$ claude-integ glm --fast haiku      # GLM メイン + background は純正 haiku
$ claude-integ opus --subagent kimi  # subagent だけ Kimi
$ claude-integ haiku --sonnet glm-5.2   # teammate を model "sonnet" で作ると GLM で動く
$ claude-integ --safe       # --dangerously-skip-permissions を付けない
```

セッション内切替は `/model` の **picker から選択**(全ベンダー表示、「From gateway」ラベル)
または `/model kimi-for-coding` のようにフルモデル名指定。

既定配置は `~/.config/claude-router/integ.conf`(雛形: `integ.conf.example`、
考え方の一例: `examples/model-placement-2026-07.md`)。明示引数が常に優先、
`--plain` で無効化、`--subagent ""` のように空指定で個別解除。

> **注意**: gpt/kimi/glm 系モデルは claude-integ 経由のセッションでのみ動く。
> 素の `claude` セッションで `/model gpt-...` を指定すると Anthropic 直行で
> エラーになり、しかもそれが既定モデルとして settings.json に保存される。

### /model picker への表示

CLI の gateway model discovery(`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` +
非 first-party BASE_URL + first-party OAuth)を利用。claude-integ が起動時に
router の `/claude-router/models` カタログ(config.toml の `picker_models`)を
`~/.claude/cache/gateway-models.json` に書き込むことで picker に載る
(OAuth では CLI の自動フェッチが走らないため上書きされない)。

### ベンダー混成 teammate(agent teams)

teammate の model パラメータは enum `sonnet|opus|haiku|fable` のみ。これは alias で、
`--sonnet glm-5.2` 等の remap(`ANTHROPIC_DEFAULT_*_MODEL`)により任意ベンダーへ割当可能。
tmux teammate 起動時の env allowlist がこれらを落とすため、claude-integ は
`CLAUDE_CODE_TEAMMATE_COMMAND` wrapper を自動生成して remap を注入する。
検証済み: Kimi リーダー + Claude teammate / Claude リーダー + GLM teammate /
Claude リーダー + GPT teammate。
**user settings の env で `ANTHROPIC_DEFAULT_*_MODEL` を固定している alias は
remap 不可**(settings env が process env に勝つ)— その場合は別の alias を使う。

## ハマりどころ(CLI 側ゲート)

- **ToolSearch**: `ANTHROPIC_BASE_URL` が first-party でないと CLI が自動無効化する。
  プロキシが tool_reference を素通しするなら `ENABLE_TOOL_SEARCH=true` で解除
  (CLI 内部のログメッセージに明記された公式 escape hatch)。claude-integ は
  claude-* メイン時に自動設定する。
- **teammate (agent teams)**: gate は `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 等で、
  BASE_URL では無効化されない(実測で動作)。
- **セッション途中のベンダー切替**: claude モデルの重い履歴(署名付き thinking /
  tool_reference)を持つセッションを `/model` で他ベンダーに切替えると 400 になりうる
  (Kimi 実測: "tokenization failed")。軽い履歴なら成功する。ベンダー混在は
  「メイン+subagent/teammate のロール分担」で行うのが安全。
- **コンテキスト窓 / auto-compact**: CLI の有効窓は
  `min(モデル窓, CLAUDE_CODE_AUTO_COMPACT_WINDOW)`。モデル窓は id に `[1m]`
  サフィックスがあれば 1M(任意ベンダーで有効、CLI が送信時に suffix を
  剥がすため router 変更不要)、それ以外の未知モデルは 200k 固定。
  claude-integ は `k3` を `k3[1m]` として起動し 512k にキャップする
  (picker から選んだ場合は cap なし = 1M フル。cap は起動時 env でしか
  設定できない構造制限)。**`CLAUDE_CODE_MAX_CONTEXT_TOKENS` は使わない** —
  全非 claude モデルの窓をプロセス全体で膨らませ、/model 切替先の
  実窓(gpt 272k / glm 200k)を超過して 400 や無言打ち切りを招く。

## 注意(免責)

- 本ツールは Claude Code CLI の**非公開の内部挙動**(gateway-models cache、
  `ANTHROPIC_DEFAULT_*_MODEL`、`CLAUDE_CODE_TEAMMATE_COMMAND` 等の env)に
  依存する。CLI 更新で予告なく壊れうる。動作検証は Claude Code 2.1.221。
- 各ベンダーの利用規約・サブスクリプション条件の順守は利用者の責任で。
- router は 127.0.0.1 でのみ待ち受け、認証情報をログに出さない設計だが、
  config.toml には生の API key が入る(install.sh が chmod 600 にする)。

## アンインストール

```console
$ systemctl --user disable --now claude-router
$ rm ~/.config/systemd/user/claude-router.service ~/.local/bin/claude-integ
$ rm -r ~/.config/claude-router   # ベンダー key ごと消える点に注意
```
