# KN20A Recorder

> 試験公開版です。正式リリース前の確認・検証用で、Ubuntuからの実機カメラ操作は未検証です。

FoMaKo KN20A向けのWindows / Ubuntu用予約録画・露出制御ツールです。ローカルのWeb画面から日時と画角プリセットを指定して録画し、終了後に1本のMP4へまとめます。メーカー非公式のツールです。

## 主な機能

- RTSP/TCP Pull録画、またはRTMP Publishの受信
- 日時予約、受信テスト、録画の手動停止、重複予約の拒否
- 予約ごとの画角プリセット指定（指定なし／0〜10）、接続復旧時の再呼び出し
- 再圧縮なしでMKVへ分割保存し、終了後に1本の `recording.mp4` へ結合
- MP4検証後のMKV削除、次回起動時の未完了変換の再開
- 最近の録画実績を使った容量見積もり
- 露出の手動設定と、回折を抑える画像解析AE（実験的機能）
- SYSTEM権限のWindows起動タスクと監視プロセスによる常駐
- Ubuntuのsystemdユーザーサービスによる常駐・異常終了時の再起動

## 必要なもの

- WindowsまたはUbuntu 22.04 / 24.04、Python 3.10以降
- WindowsではPython Launcher（`py` / `pyw`）
- FFmpegとFFprobe（同じフォルダ、またはPATH上に配置）
- 同一LANで接続できるKN20A
- Pythonパッケージ `websockets`（画像解析AEで使用）

FFmpegは同梱していません。対象OSの自動テストと露出シミュレーションを用意しています。カメラを使う録画・露出検証は別途必要です。他機種・異なるファームウェアの互換性は未確認です。

## Windowsのセットアップ

PowerShellでこのフォルダを開きます。

```powershell
py -3 -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

`config.json` を自分の環境に合わせて編集します。

| 項目 | 設定内容 |
| --- | --- |
| `camera_ip` | カメラのIPアドレス |
| `input_url` | メインストリームのRTSP URL |
| `analysis_url` | サブストリームのRTSP URL |
| `analysis_ws_url` | カメラWebプレビューのWebSocket URL |
| `ffmpeg_path` | FFmpegの絶対パス、またはPATH上のコマンド名（既定 `ffmpeg`） |
| `output_dir` | 録画先。相対パスはこのフォルダ基準 |
| `estimated_bitrate_mbps` | 実績がない場合の見積もり用ビットレート（Mbps） |

`192.0.2.10` は説明用アドレスです。`camera_ip` と各URLのアドレスは個別に変更してください。カメラ側はDHCP予約などでIPを固定すると運用しやすくなります。

```powershell
py -3 recorder.py --check
```

確認後、`start_recorder.cmd` を実行すると [操作画面](http://127.0.0.1:8765/) が開きます。画面なしの起動は `start_recorder_silent.vbs` を使います。Pythonパッケージは起動に使うPythonへインストールしてください。

## Ubuntuのセットアップ

このリポジトリを `~/kn20a-recorder` に配置して実行します。別の場所に置く場合は、後述のサービス定義のパスも変更してください。

```bash
sudo apt update
sudo apt install python3 python3-venv ffmpeg
cd ~/kn20a-recorder
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
```

前述の設定項目を編集します。`ffmpeg_path` は `ffmpeg` のまま利用できます。保存先には、このユーザーが書き込めるLinuxのパスを指定してください。

```bash
.venv/bin/python recorder.py --check
sh start_recorder.sh
```

サーバー環境では `sh start_recorder.sh --no-browser` で起動します。別PCから操作する場合は、例えば `ssh -L 8765:127.0.0.1:8765 user@server` で転送し、手元のブラウザーから `http://127.0.0.1:8765/` を開けます。

### Ubuntu起動時の常駐

systemdユーザーサービスを使います。アプリは一般ユーザー権限で実行します。

```bash
mkdir -p ~/.config/systemd/user
cp systemd/kn20a-recorder.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kn20a-recorder.service
sudo loginctl enable-linger "$USER"
```

lingerを有効にすると、サインイン前の起動とサインアウト後の常駐が可能になります。ホームディレクトリや録画先が起動時に利用できる構成で使ってください。暗号化ホームや後からマウントする保存先では、マウントとサービスの起動順を別途設定する必要があります。

```bash
systemctl --user status kn20a-recorder.service
journalctl --user -u kn20a-recorder.service -f
systemctl --user restart kn20a-recorder.service
```

自動起動だけを無効にするには `systemctl --user disable kn20a-recorder.service`、現在の実行も止めるには `systemctl --user disable --now kn20a-recorder.service` を実行します。lingerは他のユーザーサービスにも影響するため、本ツールの無効化時には自動解除しません。

停止時はSIGTERMを受けて録画・解析プロセスの終了処理を行います。異常終了時はsystemdが再起動し、停止期限を超えた子プロセスも回収します。動作は [systemdの終了制御](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html) に従います。

## 録画

カメラのメインストリームを設定してから、画面で名称・画角プリセット・開始・終了日時を指定します。日時はJST固定です。RTSP入力ではカメラのStream Publishを有効にする必要はありません。カメラ本体のエンコード設定はこのツールでは変更しません。

標準では10分単位のMKVへ映像と音声をコピーし、終了後に再エンコードせず結合します。変換中はMKVとMP4を同時に保持するため、その分の空き容量が必要です。完成MP4の検証後にMKVを削除します。残したい場合は `keep_mkv_after_remux` を `true` にします。

プリセットはカメラ側で先に登録してください。選択肢0〜10は実在確認を伴う一覧ではありません。録画開始前に呼び出し、再接続時にも同じ番号を呼び出します。サービスが開始日時を過ぎて起動した場合は終了まで録画し、期間全体を過ぎていれば未実行になります。

RTMP受信を使う場合は `input_mode` を `rtmp-listen` にし、`listen_url` とカメラ側の送信先を合わせます。入力は同時に1録画のため、予約の重複はできません。

## 画像解析AE（実験的）

SAEやAutoで返される絞り値は、この実機では実絞りではなく手動設定の記憶値でした。そのため、夕景AEはManualモードで絞り・SS・Gainを制御します。

日中の良好な露出で「現在映像を基準にする」を実行してから有効化します。回折上限の既定はF5.6です。暗くなるとSSを設定した最遅値まで遅くし、次に絞りを開き、最後に画質Gain上限まで増感します。明るくなると逆順で戻します。露出補正と夜間方針で、昼の明るさを夜間にどれだけ維持するかを調整できます。

解析用接続の失敗では露出変更を保留します。解析FFmpegを同時1個に制限し、終了回収、指数バックオフ、連続失敗時の15分休止を行います。WindowsではJob Objectで解析子プロセスの残留を抑制します。

既知の制約として、WebSocket解析ストリームがタイムアウトして復帰待ちが続く場合があります。ON表示だけでは露出調整が働いているとは限りません。解析値・最終調整・エラー表示を確認してください。初期状態はOFFです。1/30の可用性やシャッター番号の意味はファームウェアに依存するため、UIの候補だけで実際の露光時間を保証するものではありません。

## Windows起動時の常駐

セットアップと手動起動の確認後、管理者PowerShellで実行します。

```powershell
.\install_boot_task.ps1
```

`KN20A Recorder` タスクをSYSTEM権限で登録し、その場で開始します。再登録時はこのフォルダの既存プロセスを停止するため、録画中には実行しないでください。Python Launcherが選択するPythonを検出して登録します。SYSTEMがアクセスできるローカル保存先と実行ファイルを使ってください。ネットワークドライブやユーザー固有のアクセス権は別途設定が必要です。

自動起動の無効化（現在の実行は継続）:

```powershell
.\disable_boot_task.ps1
```

登録の削除は `uninstall_boot_task.ps1` です。停電からPC本体を自動復帰させるには、別途UEFI/BIOS側の設定が必要です。

## ファイル構成と開発

`recorder.py` が録画・HTTP API・カメラ制御・画像解析AE、`supervisor.py` が常駐監視、`web/` が画面です。ビルド工程はありません。

設定は `config.json`、予約は `schedules.json`、プリセット名は `presets.json` に保存します。後二者は初回起動時に作られます。`data/` にAE状態・変更履歴、`logs/` に実行ログ、`recordings/` に録画を保存します。これらは公開対象から除外しています。

```powershell
py -3 -m unittest discover -s tests -v
py -3 tests/simulate_exposure_guard.py
```

Ubuntuでは `py -3` を `.venv/bin/python` に置き換えます。GitHub ActionsではWindowsとUbuntu 22.04 / 24.04で実行します。これらは実機の露出・録画品質を保証するものではありません。利用環境の注意は [SECURITY.md](SECURITY.md) を参照してください。
