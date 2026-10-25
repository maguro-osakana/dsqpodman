# dshpodman

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`）を、Ubuntu 上の rootless Podman で実行するためのツールです。
ホストの `/usr` を読み取り専用で利用し、コンテナイメージを取得・ビルドせずに実行環境を作ります。

## 特徴

- 一般ユーザーの権限で実行し、ホストのパッケージを変更しません。
- コンテナの home を選択でき、コード・設定・履歴をそこに保存します。
- `dsh` の取得・ビルド・更新と、Web UI の起動・停止をコマンドで操作できます。
- 条件を満たすホストの Node.js・pnpm を利用し、必要に応じて選択した home に導入します。
- Podman の管理データを、このツール専用の `storage/` に保存します。

## 前提条件

Ubuntu の通常のログインセッションで、次の環境を用意してください。

- Python 3.12 以降。
- rootless 実行が可能な Podman と、`crun`、`newuidmap`、`newgidmap`、`pasta`、`catatonit`、`netavark`。
- Git と `ip` コマンド。
- 実行ユーザーに対する `/etc/subuid`・`/etc/subgid` の割り当て。それぞれ 65,536 個以上が必要です。
- GitHub、Node.js 配布サイト、npm レジストリなどへ接続できるネットワーク。

ホストの Node.js を利用して pnpm を導入する場合は、ホストの npm も必要です。
Node.js の自動ダウンロードは Linux x86_64 / aarch64 に対応しています。
このツールはホストのパッケージ導入や rootless Podman の初期設定を行いません。

## クイックスタート

このリポジトリを clone した後、次のコマンドを実行します。

```bash
cd dshpodman
bin/dshpod build
bin/dshpod dshupdate
bin/dshpod check
bin/dshpod start
```

1. `build` で最小 rootfs と専用の Podman 管理領域を準備します。
2. `dshupdate` で設定されたリポジトリの既定ブランチを clone し、依存関係の導入とビルドを行います。
3. `check` で書き込み範囲、ネットワーク、Node.js・pnpm・dsh を検査します。
4. `start` で Web UI を起動します。表示された URL をブラウザーで開いてください。

初回のモデルと API キーは Web UI で設定します。
ワークスペースにはコンテナ内の `/home/<username>` 配下のディレクトリを選択してください。

起動時の URL には認証トークンが含まれます。ログやスクリーンショットを共有する際は URL を伏せてください。
既定の接続は HTTP です。信頼できる LAN で利用してください。

停止するには次を実行します。

```bash
bin/dshpod stop
```

## コマンド

| コマンド | 用途 |
| --- | --- |
| `bin/dshpod build` | rootfs を作成・再生成する |
| `bin/dshpod start [--home DIR]` | Web UI を起動する |
| `bin/dshpod stop` | 常駐コンテナを停止・削除する |
| `bin/dshpod status` | 実行状態、home、dsh の版、起動中の URL を表示する |
| `bin/dshpod logs` | 現在の常駐コンテナのログを表示し、追従する |
| `bin/dshpod shell [--home DIR]` | コンテナ内の Bash を開く |
| `bin/dshpod exec [--home DIR] -- COMMAND [ARG...]` | コンテナ内でコマンドを実行する |
| `bin/dshpod dshupdate [--home DIR]` | dsh と必要なランタイムを導入・更新する |
| `bin/dshpod check [--home DIR]` | 実行環境を検査する |

`shell` と `exec` は、起動中ならそのコンテナを使い、停止中なら一時コンテナを使います。
`exec` の引数は shell で再解釈しません。パイプなどが必要な場合は `bash -c` を明示してください。

```bash
bin/dshpod exec -- dsh --version
bin/dshpod exec -- bash -c 'printf "hello\n" | cat'
```

`logs` は現在のコンテナの起動時からのログを表示します。`Ctrl+C` で追従を終了してもコンテナは動作を続けます。
停止・再作成されたコンテナの過去ログは保持しません。

### home の選択

既定の home はリポジトリ内の `workspace/` です。別のディレクトリを使う場合は、各コマンドに同じ `--home` を指定します。
相対パスはコマンドを呼び出したディレクトリが基準です。

```bash
bin/dshpod dshupdate --home /path/to/home
bin/dshpod start --home /path/to/home
bin/dshpod exec --home /path/to/home -- git status
bin/dshpod stop
```

コンテナ内のユーザー名・UID・GID は実行ユーザーに合わせ、home は `/home/<username>` になります。
起動中のコンテナと異なる home を指定するとエラーになります。home を切り替える前に `stop` を実行してください。
選択したディレクトリ内のファイルは、コンテナから読み書きできます。

### 更新

```bash
bin/dshpod dshupdate
```

更新では、取得先リポジトリの既定ブランチの最新コミットをビルドします。
成功すると `current` を新しいリリースへ切り替え、Web UI が起動中ならコンテナを再起動します。
失敗した場合は従来のリリースを維持し、取得したソースを診断用に残します。
過去のリリースは自動削除しません。

Node.js がバージョン、TypeScript 型削除機能、開発ヘッダーの条件を満たさない場合は、
公式 Linux 配布物をダウンロードし、配布元の SHA-256 と照合して home に展開します。
pnpm は取得先リポジトリが指定する版を使います。

## 設定

[config/dshpod.json](config/dshpod.json) を編集します。

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `lan_ip` | `"auto"` | ホスト側で待ち受ける IPv4。`auto` は外向き経路から自動選択 |
| `port` | `3080` | ホスト側の公開ポートの探索開始番号。1024〜65535 |
| `startup_timeout` | `120` | Web UI の起動待ち時間（秒） |
| `repository` | `"https://github.com/deepseek-ai/deepseek-harness.git"` | dsh の取得先 |
| `node_major` | `24` | Node.js のダウンロード時に優先する LTS メジャー |
| `build_concurrency` | `2` | dsh のビルド並列数 |
| `extra_ro` | `[]` | 同じ絶対パスへ読み取り専用で追加マウントするホストのファイル・ディレクトリ |

複数の NIC や VPN がある場合は、`lan_ip` に利用する LAN の IPv4 を明示してください。
新規起動時は指定した IPv4 で `port` から `port + 10` まで番号順に空きポートを探します（最大11候補、上限65535）。
たとえば `port` が 3080 なら 3080〜3090 を確認し、すべて使用中ならエラーになります。
選択したポートだけをホストで公開し、dsh の公開 URL と trusted host にも同じポートを使います。
コンテナ内の待ち受けポートは常に 3080 です。たとえば 3080 が使用中なら、ホスト 3081 → コンテナ内 3080 に転送します。
JSON の設定は書き換えません。起動中のコンテナには既存の公開ポートを使い、表示 URL は `status` で確認できます。
空き確認後に別プロセスがポートを取得した場合は、起動エラーで終了します。自動再試行は行わないため、`start` を再実行してください。
起動確認ではコンテナの実際の公開先とログの URL が一致することも検査し、不一致ならエラーにします。
ホストのファイアウォールは変更しません。

`node_major` が要件に合わない場合は、条件を満たす別の LTS を選びます。
`extra_ro` には、たとえば `["/opt/cuda"]` を指定できます。
ネットワークやマウントの設定を変更した場合は、`stop` → `start` でコンテナを作り直してください。

Podman の実行設定は [config/containers.conf](config/containers.conf) にあります。

## 保存先

| 場所 | 内容 |
| --- | --- |
| `bin/` | ホストで使う管理コマンド |
| `libexec/dshpod/` | コンテナ内の補助プログラム |
| `config/` | 設定ファイル |
| `tests/` | ユニットテスト |
| `rootfs/` | 再生成可能な最小 rootfs |
| `workspace/` | 既定の home |
| `storage/` | このツール専用の Podman 管理データ |

選択した home の `.local/share/dshpod/` に、ランタイムとリリースを保存します。
`current` は利用中のリリース、`previous` は直前のリリースを指すリンクです。
dsh の設定・履歴やパッケージキャッシュも選択した home に保存されます。

Podman の永続ストアは `storage/graph-v2/`、一時実行領域は `/run/user/<UID>/dshpod-<配置場所のハッシュ>/` です。
一時実行領域はホストの再起動時に消えます。

### 配置場所の移動

`stop` → ディレクトリ移動 → `start` の順に操作してください。
移動後は専用の Podman 管理データを再作成し、rootfs と home を再利用します。
外部の home を使っている場合は、移動後も同じ `--home` の指定が必要です。
別の UID で利用する場合は、home へのアクセス権をホスト側で用意してください。

`build` を再実行すると、コンテナを停止して rootfs を再生成します。選択した home 内のデータは保持します。

## 実行環境の範囲と制約

- ホストの `/usr` を共有します。ホスト側でパッケージを更新すると、その変更はコンテナからも見えます。必要に応じてプロセスを再起動してください。
- コンテナの rootfs は読み取り専用です。開発依存の追加には、venv や home へのインストール・ビルドを使ってください。
- コンテナが永続的に書き込めるホスト領域は選択した home です。ホストの home 全体、SSH agent、Podman ソケットは自動では渡しません。
- ネットワークはコンテナごとの `pasta` 接続を使い、外向き通信を許可します。
- `/tmp`・`/var/tmp` は実行可能な tmpfs です。ここに保存したファイルはコンテナ終了後に保持されません。
- ホストの補助グループや、ホスト全体の UID・GID 所有者表示は再現しません。
- home 外の絶対パスを参照する venv や Git worktree は、作り直しなどが必要です。
- `/etc/alternatives`・`/etc/ssl/certs` のリンク先は必要に応じて読み取り専用でマウントします。リンクの追加・変更後はコンテナを作り直してください。

## 動作確認とトラブルシューティング

環境の検査には次を使います。

```bash
bin/dshpod check
```

一時ファイルによる書き込み可否、DNS・HTTPS、Node.js・pnpm・dsh CLI を検査します。
Web UI が起動中なら、ポート公開と Web 応答も確認します。
パッケージの導入・更新は行いませんが、検査用コンテナや Podman 管理ファイルは作成します。

- 起動しない場合は `bin/dshpod status` と `bin/dshpod logs` を確認してください。
- LAN から開けない場合は `lan_ip` とホストのファイアウォールを確認してください。
- user namespace が制限されたサンドボックス内では Podman が動作しない場合があります。通常のホスト端末で実行してください。

開発用のユニットテストは次で実行できます。

```bash
python3 -m unittest discover -s tests -v
```

テストは一時ディレクトリでコマンド引数、home の保護、再配置、更新失敗時の動作などを検査します。
実際の Podman によるマウント・ネットワークの検証には `bin/dshpod check` を使ってください。

## ライセンス

このリポジトリのコードとドキュメントは [MIT License](LICENSE) で公開しています。
取得・利用する DeepSeek Harness、Node.js、pnpm などには、それぞれのライセンスが適用されます。
