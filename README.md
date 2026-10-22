# KN20A Recorder

> 試験公開版です。正式リリース前の確認・検証用です。メーカー非公式。各機種の実機互換性を保証しません。

Windows / Ubuntuで動く予約録画コンソールです。RTSP/TCPまたはRTMP Publishを無再圧縮でMKVへ保存し、終了後にMP4へまとめます。Generic（制御なし）とKN20Aの画角・露出制御を独立して選択できます。

## 対応範囲

| 機能 | Generic | KN20A制御 |
| --- | --- | --- |
| RTSP/TCP / RTMP Listen予約録画・手動停止 | 対応 | 対応 |
| 分割、再接続、MP4結合、終了後再起動からの復旧 | 対応 | 対応 |
| 画角プリセット | 非対応 | 0〜254、通常候補0〜10 |
| 露出設定・画像解析AE | 非対応 | 実験的機能 |

ONVIF、SRT、複数カメラ同時録画、映像の自動再エンコードは未実装です。機種名より、実際の配信URL・codec・音声構成を確認してください。H.264/H.265＋AACまたは音声なしを基本とします。G.711などの構成は録画原本を保持し、勝手な音声削除・変換はしません。

## インストール

Python 3.10以降、FFmpegとFFprobeが必要です。FFmpegは同梱していません。

Windows（Python Launcherが必要）:

```powershell
py -3 -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

Ubuntu 22.04 / 24.04:

```bash
sudo apt update
sudo apt install python3 python3-venv ffmpeg
cd ~/kn20a-recorder
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
```

Genericには `config.example.json`、KN20A制御には `config.kn20a.example.json` をコピーして使用します。上記コピーは初回だけ行い、使用中のconfig.jsonを上書きしないでください。

## 設定

新規設定はschema_version 2です。`192.0.2.10` は説明用なので実際のアドレスへ変更してください。

- `input.type`: `rtsp` または `rtmp-listen`。
- `input.url`: RTSPの配信URL、またはRTMP受信の待受URL。RTSPはTCPのみ。
- `input.read_timeout_seconds`: RTSPのI/O timeout、既定5秒。
- `input.connect_timeout_seconds`: RTMP publisher待ち、既定30秒。
- `input.probe`: 任意の入力検査を抑止する場合は `off`。
- `controls.presets.driver` / `controls.exposure.driver`: 各々 `none` または `kn20a`。KN20Aでは `host` と `port`（通常5678）を指定。入力URLと別の接続先も指定できます。
- `ffmpeg_path`: PATH上の `ffmpeg`、または実行ファイルの絶対パス。FFprobeも同じフォルダまたはPATHに必要です。
- `recording.output_dir`: 保存先。相対パスはアプリのフォルダ基準。
- `recording.segment_seconds`: 分割の目安秒数、既定600。GOP境界で分割するため厳密な秒数ではありません。
- `recording.retain_sources`: 新規既定true。falseでも、原本との対応証跡・packet検査・全体デコード検証が成功するまで原本を削除しません。
- `recording.preset_failure_policy`: 新規既定 `continue`、画角必須なら `require`、旧動作は `legacy`。
- `recording.preset_deadline_seconds`: require時の開始後待機上限、既定10秒（最大60秒）。通信断後はプリセットを再適用し、最初の再準備から別の同じ長さの期限を設けます。再準備できない場合も収録済み素材は後処理へ進めます。
- `estimated_bitrate_mbps`: 同じ入力の実績がない場合の容量見積もり。
- `stall_timeout_seconds` / `connection_timeout_seconds`: FFmpeg進捗停止監視、既定15秒 / 45秒。
- `validation_timeout_seconds`: メディア検査・結合の上限、既定21600秒。長時間の全体検証はCPUと時間を使用します。
- `web_host` / `web_port`: 既定127.0.0.1 / 8765。外部公開せずローカルまたはSSH転送で利用してください。

KN20AのAE解析映像は `analysis_ws_url` / `analysis_url` で指定します。

## 起動と録画

```powershell
py -3 recorder.py --check
.\start_recorder.cmd
```

Ubuntuでは `py -3` を `.venv/bin/python` に置換し、`sh start_recorder.sh` で起動します。サーバーでは `sh start_recorder.sh --no-browser` を使います。[操作画面](http://127.0.0.1:8765/)で名称・開始・終了・必要なら画角と失敗時の方針を指定します。日時はJST固定、同時録画は1件です。

「入力を確認」は録画と独立した任意の短時間検査です。録画開始時は検査を中断し、追加接続を録画の成立条件にしません。RTMPでは別listenerを作らず、受信後のファイルで検査します。RTSP入力にカメラ側のStream Publish設定は不要です。

プリセットは先にカメラへ登録してください。前の予約の録画中には次予約のための画角変更を送りません。接する2予約では後の画角準備が遅れる場合があります。continueは録画を優先し、画角未確認を表示します。requireは保存した期限まで待ちます。取消後の未送信要求は破棄しますが、送信済みの物理移動を取り消せる保証はありません。結果不明のときは新たな自動制御を保留し、カメラの停止・画角を確認して画面から明示解除してください。

予約ごとに方針を保存するため、後から設定の既定値を変えても既存予約の契約は変わりません。

## 原本・MP4・復旧

変更のない互換ストリームは `recording.mp4` 1本にします。解像度・codec・音声構成などが変われば互換な連続区間ごとのMP4とし、理由はmanifestに残ります。単一segment内の解像度変更や破損、非対応codecは原本保持・後処理失敗または保持完了として扱います。

`manifest.json` が入力ファイルと成果物のハッシュ、stream構成、検証と削除の証跡を管理します。古いMP4の存在だけでは原本を削除しません。証跡のない既存MP4は `unverified_*.mp4` へ退避します。旧分割 `part_*.mp4` もMKVと同じ保持条件です。結合と全体検証には、原本と成果物を同時に置ける空き容量が必要です。

録画中に異常終了し、予約終了後に起動しても、残存原本があれば後処理へ回します。破損区間は黙って捨てず保持します。後処理失敗時は一覧から再試行できます。manifestが検証済みなら、削除途中に停止しても再起動時に安全に再判定します。

APIの `finalization` はverified/kept_mkv、`outcome` はpartial/unknownで録画欠落と後処理を分離します。completedやMP4生成成功は、予約時間全体を欠落なく収録できた保証ではありません。接続待ち・通信断は `gaps` に記録します。

## 既存設定の移行

schema_versionのない設定は従来のKN20Aとして読み、プリセット方針はlegacyです。読込だけでは設定ファイルを書き換えません。方針のない旧予約も、設定をv2へ変えてもlegacyのままです。

```powershell
py -3 recorder.py --migrate-config preview
# アプリを停止してから適用:
py -3 recorder.py --migrate-config apply
```

適用時は設定・予約・プリセット・AE状態を日時付き `.pre-v2-*` ファイルへバックアップします。旧版へ戻す場合はアプリ停止後にバックアップを戻してください。安全性検証はv1でも適用するため、変換・原本削除には従来より時間がかかります。

## KN20Aの画像解析AE

KN20A制御時だけ利用できます。良好な日中露出で「現在映像を基準にする」を実行し、有効化してください。ManualでSS→絞り→Gainの順に暗さへ追従し、明るくなれば逆順で戻します。回折上限は既定F5.6、露出補正・夜間方針・最遅SS・Gain上限を変更できます。

KN20AのAuto/SAEの絞り応答は手動設定の記憶値のため、物理的な実絞りとは扱いません。WebSocket解析映像がタイムアウトして復帰待ちになる場合があります。ON表示だけでなく解析値と最終調整を確認してください。解析FFmpegの単一起動・回収・指数バックオフ・連続失敗時休止を維持しています。1/30の可用性はファームウェアに依存します。

## 常駐起動

Windowsは手動動作確認後、管理者PowerShellで `.\install_boot_task.ps1` を実行します。SYSTEMの `KN20A Recorder` タスクを登録して起動します。再登録は既存プロセスを停止するため録画中に実行しないでください。自動起動だけ無効にするのは `disable_boot_task.ps1`、登録削除は `uninstall_boot_task.ps1` です。

Ubuntuはこのフォルダを `~/kn20a-recorder` に置き、次を実行します。別の配置先ではサービス定義のパスを変更してください。

```bash
mkdir -p ~/.config/systemd/user
cp systemd/kn20a-recorder.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kn20a-recorder.service
sudo loginctl enable-linger "$USER"
```

確認は `systemctl --user status kn20a-recorder.service`、ログは `journalctl --user -u kn20a-recorder.service -f`。自動起動だけの無効化は `systemctl --user disable kn20a-recorder.service`、現在の実行も停止するなら `--now` を追加します。lingerは他のサービスにも影響するため自動解除しません。起動時にホームと録画先がマウント済みである必要があります。PC本体の停電復帰は別途UEFI/BIOSで設定してください。

## 開発と検証

`recorder.py` はCLI・HTTP・予約/録画・AEの互換入口、`recorder_app/` は入力、制御、準備調停、設定移行、成果物検証です。ビルド工程はありません。

```bash
python -m unittest discover -s tests -v
python tests/simulate_exposure_guard.py
python tools/investigate_issue1.py --mediamtx /path/to/mediamtx --assert-network
```

最後の試験はLinuxのFFmpegとMediaMTX v1.21.2を使用します。GitHub Actionsは6種類のOS/Python matrixと、実RTSP/RTMPのloopback試験を分けています。カメラ本体の互換性・長時間安定性は別途実機で検証してください。

`config.json`、`schedules.json`、`presets.json`、`data/`、`logs/`、`recordings/` は個人データとしてGitから除外しています。[SECURITY.md](SECURITY.md)も参照してください。
